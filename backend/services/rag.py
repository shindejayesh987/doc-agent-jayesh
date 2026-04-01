"""
rag.py — RAG pipeline: search document trees, collect context, generate answers.
"""
import asyncio
import json
import logging
import re
from typing import List, Dict, Any, Optional, Tuple

from pageindex.llm.base import Message

from backend.services.shingling import compute_shingles, ground_answer, jaccard_similarity, max_score, rank_texts

logger = logging.getLogger(__name__)


_LEXICAL_SHORTLIST_SIZE = 15
_LEXICAL_MIN_SCORE = 0.01
_LEXICAL_SHINGLE_K = 2
_SECTION_PREVIEW_CHARS = 800
_DUPLICATE_SUPPRESSION_THRESHOLD = 0.85


def _strip_text(node):
    """Remove 'text' fields from tree nodes for lighter search prompts."""
    if isinstance(node, list):
        return [_strip_text(item) for item in node]
    if not isinstance(node, dict):
        return node
    n = {k: v for k, v in node.items() if k != "text"}
    if "nodes" in n:
        n["nodes"] = [_strip_text(c) for c in n["nodes"]]
    return n


def _node_preview(node: dict, page_list: list) -> str:
    """Build a short lexical preview for a node from its title and page span."""
    start = max(1, int(node.get("start_index", 1)))
    end = max(start, int(node.get("end_index", start)))
    snippet = "\n".join(p[0] for p in page_list[start - 1:end])[:_SECTION_PREVIEW_CHARS]
    return f"{node.get('title', 'Section')}\n{snippet}"


def _collect_candidates(tree: dict, page_list: list) -> list[dict]:
    """Flatten the tree into lexical search candidates."""
    candidates = []

    def walk(node):
        if not isinstance(node, dict):
            return
        node_id = str(node.get("node_id", "")).strip()
        if node_id:
            candidates.append(
                {
                    "node_id": node_id,
                    "title": node.get("title", "Section"),
                    "start_index": node.get("start_index", 1),
                    "end_index": node.get("end_index", node.get("start_index", 1)),
                    "preview": _node_preview(node, page_list),
                }
            )
        for child in node.get("nodes", []):
            walk(child)

    structure = tree.get("structure", tree) if isinstance(tree, dict) else tree
    if isinstance(structure, list):
        for node in structure:
            walk(node)
    elif isinstance(structure, dict):
        walk(structure)
    return candidates


def _suppress_duplicate_candidates(shortlist: list[dict]) -> Tuple[list[dict], int]:
    """Greedily suppress near-duplicate candidates within the lexical shortlist."""
    kept = []
    kept_shingles = []
    suppressed = 0

    for candidate in shortlist:
        _, _, content = candidate["preview"].partition("\n")
        shingles = compute_shingles(content or candidate["preview"], k=_LEXICAL_SHINGLE_K)
        is_duplicate = any(
            jaccard_similarity(shingles, existing) >= _DUPLICATE_SUPPRESSION_THRESHOLD
            for existing in kept_shingles
        )
        if is_duplicate:
            suppressed += 1
            continue
        kept.append(candidate)
        kept_shingles.append(shingles)

    return kept, suppressed


def _build_search_payload(tree: dict, query: str, page_list: Optional[list]) -> Tuple[str, dict]:
    """
    Build the search prompt payload.

    Returns ``(payload, telemetry)``.
    """
    structure = tree.get("structure", tree) if isinstance(tree, dict) else tree
    tree_lite = _strip_text(structure)
    if not page_list:
        return json.dumps(tree_lite, indent=2), {
            "used_lexical_shortlist": False,
            "candidate_count": 0,
            "shortlist_size": 0,
            "top_lexical_score": 0.0,
            "duplicates_suppressed": 0,
        }

    candidates = _collect_candidates(tree, page_list)
    ranked = rank_texts(
        query,
        [(c["node_id"], c["preview"]) for c in candidates],
        top_n=_LEXICAL_SHORTLIST_SIZE,
        k=_LEXICAL_SHINGLE_K,
    )
    top_lexical_score = round(max_score(ranked), 4) if ranked else 0.0
    if not ranked or top_lexical_score < _LEXICAL_MIN_SCORE:
        return json.dumps(tree_lite, indent=2), {
            "used_lexical_shortlist": False,
            "candidate_count": len(candidates),
            "shortlist_size": 0,
            "top_lexical_score": top_lexical_score,
            "duplicates_suppressed": 0,
        }

    candidate_map = {candidate["node_id"]: candidate for candidate in candidates}
    shortlist = []
    for node_id, score in ranked:
        candidate = candidate_map.get(node_id)
        if candidate:
            shortlist.append(
                {
                    "node_id": candidate["node_id"],
                    "title": candidate["title"],
                    "start_index": candidate["start_index"],
                    "end_index": candidate["end_index"],
                    "preview": candidate["preview"],
                    "lexical_score": round(score, 4),
                }
            )
    shortlist, duplicates_suppressed = _suppress_duplicate_candidates(shortlist)
    return json.dumps(shortlist, indent=2), {
        "used_lexical_shortlist": True,
        "candidate_count": len(candidates),
        "shortlist_size": len(shortlist),
        "top_lexical_score": top_lexical_score,
        "duplicates_suppressed": duplicates_suppressed,
    }


async def search_nodes(tree: dict, query: str, provider, page_list: Optional[list] = None) -> dict:
    """Ask the LLM to pick the most relevant node IDs from a document tree."""
    payload, telemetry = _build_search_payload(tree, query, page_list)
    payload_label = "Candidate sections" if telemetry["used_lexical_shortlist"] else "Document tree"
    prompt = (
        "You are a document search assistant.\n"
        "Given the document tree and user question, return ONLY a JSON array "
        "of the most relevant node_id strings (max 5).\n"
        'Example: ["1", "1.2", "3"]\n\n'
        f"Question: {query}\n\n"
        f"{payload_label}:\n{payload}"
    )
    resp = await provider.complete([Message(role="user", content=prompt)])
    raw = resp.content or "[]"
    try:
        from pageindex.utils import parse_json_robust
        ids = parse_json_robust(raw)
        if isinstance(ids, list):
            telemetry["selected_node_count"] = len(ids)
            return {
                "node_ids": [str(i) for i in ids],
                "telemetry": telemetry,
            }
    except Exception:
        pass
    ids = re.findall(r'"([^"]+)"', raw)[:5]
    telemetry["selected_node_count"] = len(ids)
    return {
        "node_ids": ids,
        "telemetry": telemetry,
    }


def collect_node_text(tree: dict, node_ids: list, page_list: list) -> str:
    """Walk the tree and concatenate page text for matching nodes."""
    chunks = []

    def walk(node):
        if not isinstance(node, dict):
            return
        nid = str(node.get("node_id", ""))
        if not node_ids or nid in node_ids:
            start = node.get("start_index", 1)
            end = node.get("end_index", start)
            text = "\n".join(p[0] for p in page_list[start - 1: end])
            chunks.append(f"[{node.get('title', 'Section')}]\n{text}")
        for child in node.get("nodes", []):
            walk(child)

    root = tree.get("structure", tree) if isinstance(tree, dict) else tree
    if isinstance(root, list):
        for n in root:
            walk(n)
    elif isinstance(root, dict):
        walk(root)

    return "\n\n".join(chunks)[:12_000]


async def generate_answer(context: str, query: str, history: list, provider) -> str:
    """Generate an answer from document context using chat history."""
    messages = [
        Message(
            role="system",
            content=(
                "You are a document Q&A assistant. Answer questions using only the "
                "document context provided. Be concise and accurate. If the context "
                "doesn't contain the answer, say so. When referencing information, "
                "mention which document section it came from."
            ),
        ),
    ]
    for h in history[-6:]:
        messages.append(Message(role=h["role"], content=h["content"]))
    messages.append(Message(
        role="user",
        content=f"Question: {query}\n\nDocument context:\n{context}",
    ))
    resp = await provider.complete(messages)
    return resp.content or "No answer generated."


async def run_rag_multi(query: str, doc_data_list: list, provider, history: list) -> dict:
    """Run RAG across multiple documents and return answer metadata."""
    all_context_parts = []

    search_tasks = [search_nodes(d["tree"], query, provider, d["pages"]) for d in doc_data_list]
    search_results = await asyncio.gather(*search_tasks)
    retrieval_telemetry = []
    per_doc_text = []

    for doc_data, search_result in zip(doc_data_list, search_results):
        node_ids = search_result["node_ids"]
        telemetry = {
            "document_name": doc_data["name"],
            **search_result["telemetry"],
        }
        retrieval_telemetry.append(telemetry)
        text = collect_node_text(doc_data["tree"], node_ids, doc_data["pages"])
        per_doc_text.append((doc_data["name"], text, telemetry))

    has_positive_lexical_signal = any(
        item["top_lexical_score"] > 0 for item in retrieval_telemetry
    )

    for doc_name, text, telemetry in per_doc_text:
        if not text.strip():
            continue
        # When at least one document has lexical signal, drop zero-signal docs to
        # avoid polluting the answer prompt with irrelevant context.
        if has_positive_lexical_signal and telemetry["top_lexical_score"] == 0:
            continue
        all_context_parts.append(f"[Document: {doc_name}]\n{text}")

    if not all_context_parts:
        first = doc_data_list[0]
        fallback = "\n".join(p[0] for p in first["pages"][:5])
        all_context_parts.append(f"[Document: {first['name']}]\n{fallback}")

    merged = "\n\n---\n\n".join(all_context_parts)
    if len(merged) > 15_000:
        merged = merged[:15_000] + "\n...(truncated)"

    answer = await generate_answer(merged, query, history, provider)
    lexical_grounding_score = round(ground_answer(answer, merged), 3)
    logger.info(
        "RAG retrieval telemetry query_len=%d docs=%d shortlist_docs=%d top_scores=%s duplicates=%s grounding=%0.3f",
        len(query),
        len(doc_data_list),
        sum(1 for item in retrieval_telemetry if item["used_lexical_shortlist"]),
        [item["top_lexical_score"] for item in retrieval_telemetry],
        [item["duplicates_suppressed"] for item in retrieval_telemetry],
        lexical_grounding_score,
    )
    return {
        "answer": answer,
        "lexical_grounding_score": lexical_grounding_score,
        "retrieval_telemetry": retrieval_telemetry,
    }

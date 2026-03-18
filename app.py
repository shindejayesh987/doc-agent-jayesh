"""
app.py — PageIndex local Streamlit UI
"""

import asyncio
import json
import logging
import queue
import re
import threading
import time
from io import BytesIO
from types import SimpleNamespace

import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PageIndex",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.main .block-container { max-width: 860px; padding: 2rem 2rem 6rem; }
#MainMenu, footer, header { visibility: hidden; }
section[data-testid="stSidebar"] {
    background: #f9f9f9;
    border-right: 1px solid #e5e5e5;
}
.progress-box {
    background: #111;
    color: #d4d4d4;
    border-radius: 8px;
    padding: 0.75rem 1rem;
    font-size: 0.82rem;
    font-family: "SF Mono", "Fira Code", monospace;
    max-height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.6;
}
</style>
""", unsafe_allow_html=True)

# ── Configure logging so pageindex logs appear in terminal + are captured ─────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)

# ── Session-state defaults ────────────────────────────────────────────────────
def _init_state():
    defaults = {
        "tree": None,
        "page_list": None,
        "provider_obj": None,
        "provider_key": "anthropic",
        "messages": [],
        "index_status": "idle",   # idle | running | done | error
        "index_log": [],
        "index_error": "",
        "log_queue": None,        # queue.Queue – created when indexing starts
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ── Custom log handler that pushes records into a queue ───────────────────────
class _QueueHandler(logging.Handler):
    """Pushes log records into a queue for the Streamlit progress box."""

    # Logger names whose messages are too noisy for the UI progress box
    _SUPPRESSED = frozenset({"httpx", "httpcore", "urllib3", "openai._base_client"})

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            # Suppress noisy HTTP-layer logs from the UI (they still appear in terminal)
            if any(record.name.startswith(p) for p in self._SUPPRESSED):
                return
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


# ── Provider catalogue ────────────────────────────────────────────────────────
_PROVIDERS = {
    # chunk_budget: max tokens sent per LLM request (stay under provider's per-request limit)
    # concurrency:  parallel LLM calls (keep low for free-tier rate limits)
    "anthropic": {
        "label":        "Anthropic",
        "factory":      "anthropic",
        "base_url":     "",
        "key_hint":     "sk-ant-...  — console.anthropic.com",
        "models":       ["claude-haiku-4-5-20251001", "claude-sonnet-4-6",
                         "claude-opus-4-6"],
        "free":         False,
        "chunk_budget": 20_000,
        "concurrency":  2,
        "inter_call_delay": 2.0,
    },
    "openai": {
        "label":        "OpenAI",
        "factory":      "openai",
        "base_url":     "",
        "key_hint":     "sk-...  — platform.openai.com",
        "models":       ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "gpt-4.1-mini"],
        "free":         False,
        "chunk_budget": 20_000,
        "concurrency":  8,
        "inter_call_delay": 0.1,
    },
}


# ── Provider builder ──────────────────────────────────────────────────────────
def _build_provider(provider_key: str, model: str, api_key: str = "") -> object:
    from pageindex.llm.factory import build_provider
    cfg = _PROVIDERS[provider_key]
    llm_cfg = {"provider": cfg["factory"], "model": model}
    api_key = api_key.strip()   # remove accidental leading/trailing whitespace
    if api_key:
        llm_cfg["api_key"] = api_key
    if cfg["base_url"]:
        llm_cfg["base_url"] = cfg["base_url"]
    return build_provider(
        llm_cfg,
        retry_config={"max_attempts": 3, "base_delay_seconds": 2.0,
                      "max_delay_seconds": 60.0, "backoff_factor": 2.0},
        pipeline_config={"concurrency": cfg["concurrency"]},
    )


# ── Background indexing thread ────────────────────────────────────────────────
def _run_indexing(pdf_bytes: bytes, provider_obj, opt, q: queue.Queue):
    """
    Runs in a daemon thread. All results are communicated via `q`.
    Never writes to st.session_state directly.
    """
    # Attach queue handler to the root pageindex logger only.
    # Child loggers (pageindex.page_index, pageindex.llm.retry, …) propagate
    # up automatically — attaching to children too causes duplicate messages.
    handler = _QueueHandler(q)
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    root_pi = logging.getLogger("pageindex")
    root_pi.addHandler(handler)
    root_pi.setLevel(logging.INFO)

    try:
        from pageindex.page_index import page_index_main
        from pageindex.utils import get_page_tokens

        q.put(("log", "Extracting pages from PDF…"))
        doc_for_count = BytesIO(pdf_bytes)
        page_list = get_page_tokens(doc_for_count)

        if not page_list:
            q.put(("error", "No pages found in PDF — is it a scanned/image PDF?"))
            return

        total_tokens = sum(p[1] for p in page_list)
        q.put(("log", f"Found {len(page_list)} pages · {total_tokens:,} tokens total"))
        q.put(("log", "Building document tree — this can take several minutes…"))
        q.put(("log", "Watch the terminal for detailed LLM call logs."))

        doc_for_index = BytesIO(pdf_bytes)
        result = page_index_main(doc_for_index, opt=opt)

        q.put(("log", "✓ Indexing complete!"))
        q.put(("done", result, page_list))

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        q.put(("log", f"✗ {exc}"))
        q.put(("log", tb))
        q.put(("error", str(exc)))
    finally:
        logging.getLogger("pageindex").removeHandler(handler)


# ── Drain the log queue into session_state (called each rerun) ────────────────
def _drain_queue():
    q = st.session_state.log_queue
    if q is None:
        return

    while True:
        try:
            msg = q.get_nowait()
        except queue.Empty:
            break

        kind = msg[0]
        if kind == "log":
            st.session_state.index_log.append(msg[1])
        elif kind == "done":
            st.session_state.tree = msg[1]
            st.session_state.page_list = msg[2]
            st.session_state.index_status = "done"
            st.session_state.log_queue = None
        elif kind == "error":
            st.session_state.index_status = "error"
            st.session_state.index_error = msg[1]
            st.session_state.log_queue = None


# Drain on every rerun before rendering
_drain_queue()


# ── RAG pipeline ──────────────────────────────────────────────────────────────
async def _search_nodes(tree: dict, query: str, provider) -> list:
    from pageindex.llm.base import Message

    def strip_text(node):
        if isinstance(node, list):
            return [strip_text(item) for item in node]
        if not isinstance(node, dict):
            return node
        n = {k: v for k, v in node.items() if k != "text"}
        if "nodes" in n:
            n["nodes"] = [strip_text(c) for c in n["nodes"]]
        return n

    structure = tree.get("structure", tree) if isinstance(tree, dict) else tree
    tree_lite = strip_text(structure)
    prompt = (
        "You are a document search assistant.\n"
        "Given the document tree and user question, return ONLY a JSON array "
        "of the most relevant node_id strings (max 5).\n"
        "Example: [\"1\", \"1.2\", \"3\"]\n\n"
        f"Question: {query}\n\n"
        f"Document tree:\n{json.dumps(tree_lite, indent=2)}"
    )
    resp = await provider.complete([Message(role="user", content=prompt)])
    raw = resp.content or "[]"
    try:
        from pageindex.utils import parse_json_robust
        ids = parse_json_robust(raw)
        if isinstance(ids, list):
            return [str(i) for i in ids]
    except Exception:
        pass
    return re.findall(r'"([^"]+)"', raw)[:5]


def _collect_node_text(tree, node_ids: list, page_list: list) -> str:
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


async def _generate_answer(context: str, query: str, provider) -> str:
    from pageindex.llm.base import Message
    prompt = (
        "Answer the question using only the document context below.\n"
        "Be concise and accurate. If the context doesn't contain the answer, say so.\n\n"
        f"Question: {query}\n\nContext:\n{context}"
    )
    resp = await provider.complete([Message(role="user", content=prompt)])
    return resp.content or "No answer generated."


def _run_rag(query: str, tree: dict, page_list: list, provider) -> str:
    async def _go():
        node_ids = await _search_nodes(tree, query, provider)
        context = _collect_node_text(tree, node_ids, page_list)
        if not context.strip():
            context = "\n".join(p[0] for p in page_list[:5])
        return await _generate_answer(context, query, provider)
    return asyncio.run(_go())


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Model settings")

    provider_key = st.selectbox(
        "Provider",
        list(_PROVIDERS.keys()),
        format_func=lambda k: _PROVIDERS[k]["label"],
    )
    cfg = _PROVIDERS[provider_key]

    model = st.selectbox("Model", cfg["models"])

    api_key = st.text_input(
        "API key", type="password",
        placeholder=cfg["key_hint"],
    )

    if cfg["free"]:
        st.caption("✓ Free tier available")

    if st.button("Apply settings", use_container_width=True):
        with st.spinner("Connecting…"):
            try:
                st.session_state.provider_obj = _build_provider(provider_key, model, api_key)
                st.session_state.provider_key = provider_key  # persist for opt building
                st.success(f"✓ {cfg['label'].split()[0]} / {model}")
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()

    with st.expander("Pipeline settings"):
        timeout_val = st.number_input("Timeout (s, 0=none)", 0, value=3600, step=60)
        concurrency_val = st.number_input("Max concurrency", 1, value=4, step=1)

    st.divider()

    # Status
    status = st.session_state.index_status
    if st.session_state.tree:
        doc_name = st.session_state.tree.get("doc_name", "document")
        st.success(f"● Indexed: {doc_name}")
    elif status == "running":
        st.warning("● Indexing in progress…")
    elif status == "error":
        st.error("● Indexing failed")
    else:
        st.caption("○ No document loaded")

    if st.session_state.tree:
        if st.button("Clear document", use_container_width=True):
            for k in ("tree", "page_list", "messages",
                      "index_status", "index_log", "index_error", "log_queue"):
                st.session_state[k] = None if k in ("tree","page_list","provider_obj","log_queue") else \
                                       [] if k in ("messages","index_log") else \
                                       "idle" if k == "index_status" else ""
            st.rerun()

    st.divider()
    st.caption("Logs: run `python3 -m streamlit run app.py` in terminal to see full LLM call logs.")


# ── Main ──────────────────────────────────────────────────────────────────────
st.markdown("# 📄 PageIndex")
st.markdown("Index a PDF document then ask questions about it.")
st.divider()

# ── Upload section ────────────────────────────────────────────────────────────
if st.session_state.tree is None and st.session_state.index_status not in ("running",):
    uploaded = st.file_uploader("Upload a PDF", type=["pdf"], label_visibility="collapsed")

    if uploaded:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"**{uploaded.name}** — {uploaded.size / 1024:.0f} KB")
        with col2:
            go = st.button("Index document", type="primary", use_container_width=True)

        if go:
            if st.session_state.provider_obj is None:
                st.warning("Click **Apply settings** in the sidebar first.")
                st.stop()

            pdf_bytes = uploaded.read()
            provider_obj = st.session_state.provider_obj
            prov_cfg = _PROVIDERS.get(st.session_state.get("provider_key", "anthropic"), _PROVIDERS["anthropic"])

            opt = SimpleNamespace(
                provider=provider_obj,
                toc_check_page_num=20,
                max_page_num_each_node=10,
                max_token_num_each_node=prov_cfg["chunk_budget"],
                if_add_node_id="yes",
                if_add_node_text="no",
                if_add_node_summary="no",
                if_add_doc_description="no",
                pipeline=SimpleNamespace(
                    timeout_seconds=timeout_val if timeout_val > 0 else None,
                    concurrency=prov_cfg["concurrency"],
                    chunk_token_budget=prov_cfg["chunk_budget"],
                    inter_call_delay=prov_cfg.get("inter_call_delay", 0.5),
                ),
            )

            q = queue.Queue()
            st.session_state.log_queue = q
            st.session_state.index_status = "running"
            st.session_state.index_log = []
            st.session_state.index_error = ""

            t = threading.Thread(
                target=_run_indexing,
                args=(pdf_bytes, provider_obj, opt, q),
                daemon=True,
            )
            t.start()
            st.rerun()

# ── Progress display ──────────────────────────────────────────────────────────
if st.session_state.index_status == "running":
    st.markdown("### Indexing in progress…")

    log_lines = st.session_state.index_log
    display = "\n".join(log_lines) if log_lines else "Starting…"
    st.markdown(f'<div class="progress-box">{display}</div>', unsafe_allow_html=True)

    st.caption("Tip: full LLM call-level logs are printed in your terminal.")

    # Poll: re-run every 1.5 s while background thread is alive
    time.sleep(1.5)
    st.rerun()

elif st.session_state.index_status == "error":
    st.error(f"**Indexing failed:** {st.session_state.index_error}")

    if st.session_state.index_log:
        with st.expander("Error details", expanded=True):
            st.markdown(
                f'<div class="progress-box">{"<br>".join(st.session_state.index_log)}</div>',
                unsafe_allow_html=True,
            )

    if st.button("Try again"):
        st.session_state.index_status = "idle"
        st.rerun()

# ── Completion log ─────────────────────────────────────────────────────────────
if st.session_state.index_status == "done" and st.session_state.tree is not None:
    if st.session_state.index_log:
        with st.expander("Indexing log", expanded=False):
            st.markdown(
                f'<div class="progress-box">{"<br>".join(st.session_state.index_log)}</div>',
                unsafe_allow_html=True,
            )

# ── Chat ───────────────────────────────────────────────────────────────────────
if st.session_state.tree is not None:
    st.divider()
    st.markdown("### Ask a question")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    question = st.chat_input("Ask anything about the document…")

    if question:
        if st.session_state.provider_obj is None:
            st.warning("Apply provider settings first.")
        else:
            st.session_state.messages.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.write(question)

            with st.chat_message("assistant"):
                with st.spinner("Searching document…"):
                    try:
                        answer = _run_rag(
                            question,
                            st.session_state.tree,
                            st.session_state.page_list,
                            st.session_state.provider_obj,
                        )
                    except Exception as exc:
                        answer = f"Error: {exc}"

                st.write(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})

elif st.session_state.index_status == "idle":
    st.markdown("""
**Get started:**
1. Configure your LLM provider in the sidebar → **Apply settings**
2. Upload a PDF above
3. Click **Index document**
4. Ask questions once indexing completes
""")

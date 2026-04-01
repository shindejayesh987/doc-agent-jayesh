"""
shingling.py — lightweight lexical ranking helpers for RAG prefiltering.
"""
import re
from typing import Iterable, List, Sequence, Tuple


_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_text(text: str) -> List[str]:
    """Normalize text into lowercase word tokens."""
    if not text:
        return []
    return _WORD_RE.findall(text.lower())


def compute_shingles(text: str, k: int = 5) -> set[int]:
    """Return hashed word-level shingles for the input text."""
    words = normalize_text(text)
    if not words:
        return set()
    if len(words) < k:
        return {hash(tuple(words))}
    return {hash(tuple(words[i:i + k])) for i in range(len(words) - k + 1)}


def jaccard_similarity(a: set[int], b: set[int]) -> float:
    """Return Jaccard similarity for two shingle sets."""
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def rank_texts(
    query: str,
    items: Sequence[Tuple[str, str]],
    *,
    top_n: int = 10,
    k: int = 5,
) -> List[Tuple[str, float]]:
    """
    Rank ``(item_id, text)`` pairs by Jaccard similarity to the query.

    Items with zero lexical overlap are omitted.
    """
    query_shingles = compute_shingles(query, k=k)
    if not query_shingles:
        return []

    scored: List[Tuple[str, float]] = []
    for item_id, text in items:
        score = jaccard_similarity(query_shingles, compute_shingles(text, k=k))
        if score > 0:
            scored.append((item_id, score))

    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_n]


def max_score(scores: Iterable[Tuple[str, float]]) -> float:
    """Return the highest similarity score from a ranked list."""
    best = 0.0
    for _, score in scores:
        if score > best:
            best = score
    return best


def ground_answer(answer: str, context: str, *, k: int = 2) -> float:
    """
    Return a containment score: what fraction of the answer's shingles
    appear in the retrieved context.

    Uses containment (|A ∩ B| / |A|) instead of Jaccard (|A ∩ B| / |A ∪ B|)
    because the context is typically much larger than the answer, which
    unfairly deflates Jaccard for short, accurate answers.
    """
    answer_shingles = compute_shingles(answer, k=k)
    if not answer_shingles:
        return 0.0
    context_shingles = compute_shingles(context, k=k)
    if not context_shingles:
        return 0.0
    return len(answer_shingles & context_shingles) / len(answer_shingles)

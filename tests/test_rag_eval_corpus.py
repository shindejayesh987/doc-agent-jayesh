import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.rag import _build_search_payload, run_rag_multi


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "rag_eval_cases.json"


def _load_cases():
    return json.loads(_FIXTURE_PATH.read_text())


def _iter_node_ids(structure):
    if isinstance(structure, list):
        for node in structure:
            yield from _iter_node_ids(node)
        return
    if not isinstance(structure, dict):
        return
    node_id = structure.get("node_id")
    if node_id is not None:
        yield str(node_id)
    for child in structure.get("nodes", []):
        yield from _iter_node_ids(child)


class DeterministicProvider:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages):
        prompt = messages[-1].content
        self.prompts.append(prompt)

        if "Candidate sections:\n" in prompt:
            payload = prompt.split("Candidate sections:\n", 1)[1]
            candidates = json.loads(payload)
            return SimpleNamespace(content=json.dumps([candidates[0]["node_id"]]))

        if "Document tree:\n" in prompt:
            payload = prompt.split("Document tree:\n", 1)[1]
            structure = json.loads(payload)
            node_ids = list(_iter_node_ids(structure))
            return SimpleNamespace(content=json.dumps(node_ids[:1]))

        return SimpleNamespace(content="Synthetic evaluation answer.")


@pytest.fixture(scope="module")
def rag_eval_cases():
    return _load_cases()


@pytest.mark.parametrize(
    "case",
    [case for case in _load_cases() if case["type"] == "payload"],
    ids=[case["id"] for case in _load_cases() if case["type"] == "payload"],
)
def test_rag_eval_payload_cases(case):
    doc = case["documents"][0]
    payload, telemetry = _build_search_payload(doc["tree"], case["query"], doc["pages"])
    data = json.loads(payload)
    expected = case["expected"]

    assert telemetry["used_lexical_shortlist"] is expected["used_lexical_shortlist"]

    if telemetry["used_lexical_shortlist"]:
        assert data[0]["node_id"] == expected["top_node_id"]
        assert telemetry["duplicates_suppressed"] == expected["duplicates_suppressed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [case for case in _load_cases() if case["type"] == "run"],
    ids=[case["id"] for case in _load_cases() if case["type"] == "run"],
)
async def test_rag_eval_run_cases(case):
    provider = DeterministicProvider()
    result = await run_rag_multi(case["query"], case["documents"], provider, [])
    prompt = provider.prompts[-1]
    telemetry = {
        item["document_name"]: item
        for item in result["retrieval_telemetry"]
    }

    for doc_name in case["expected"]["context_docs_present"]:
        assert f"[Document: {doc_name}]" in prompt

    for doc_name in case["expected"]["context_docs_absent"]:
        assert f"[Document: {doc_name}]" not in prompt

    for doc_name in case["expected"]["positive_signal_docs"]:
        assert telemetry[doc_name]["top_lexical_score"] > 0

    for doc_name in case["expected"]["zero_signal_docs"]:
        assert telemetry[doc_name]["top_lexical_score"] == 0.0

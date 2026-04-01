import json
from types import SimpleNamespace

import pytest

from backend.services.rag import _build_search_payload, run_rag_multi, search_nodes
from backend.services.shingling import compute_shingles, ground_answer, jaccard_similarity, rank_texts


def test_compute_shingles_and_jaccard_rank_similar_text_higher():
    a = "cloud run deployment with secret manager and supabase"
    b = "deploy to cloud run using secret manager and supabase"
    c = "basketball playoffs and player scoring statistics"

    ab = jaccard_similarity(compute_shingles(a, k=3), compute_shingles(b, k=3))
    ac = jaccard_similarity(compute_shingles(a, k=3), compute_shingles(c, k=3))

    assert ab > ac
    assert ab > 0


def test_rank_texts_returns_top_scored_ids():
    ranked = rank_texts(
        "supabase service key rotation",
        [
            ("1", "database indexing progress and page parsing"),
            ("2", "supabase service key rotation and deployment secrets"),
            ("3", "frontend design tokens and css variables"),
        ],
        top_n=2,
        k=3,
    )

    assert ranked
    assert ranked[0][0] == "2"


def test_ground_answer_returns_higher_overlap_for_related_text():
    good = ground_answer(
        "Rotate the Supabase service key in Cloud Run.",
        "Cloud Run deployment uses a Supabase service key. Rotate the Supabase service key regularly.",
    )
    bad = ground_answer(
        "Rotate the Supabase service key in Cloud Run.",
        "Basketball playoffs and player scoring statistics.",
    )

    assert good > bad
    assert good > 0


def test_build_search_payload_prefers_lexical_shortlist_when_page_text_exists():
    tree = {
        "structure": [
            {"node_id": "1", "title": "Collections", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Deployment", "start_index": 2, "end_index": 2, "nodes": []},
        ]
    }
    pages = [
        ("collections and uploads overview", 100),
        ("cloud run deploy with supabase service key rotation guidance", 100),
    ]

    payload, used_shortlist = _build_search_payload(tree, "How do we rotate the supabase service key for deploy?", pages)

    assert used_shortlist["used_lexical_shortlist"] is True
    assert used_shortlist["shortlist_size"] >= 1
    assert used_shortlist["duplicates_suppressed"] == 0
    data = json.loads(payload)
    assert data[0]["node_id"] == "2"


def test_build_search_payload_suppresses_near_duplicate_shortlist_entries():
    tree = {
        "structure": [
            {"node_id": "1", "title": "Deploy A", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Deploy B", "start_index": 2, "end_index": 2, "nodes": []},
            {"node_id": "3", "title": "Collections", "start_index": 3, "end_index": 3, "nodes": []},
        ]
    }
    pages = [
        ("cloud run deploy with supabase service key rotation guidance", 100),
        ("cloud run deploy with supabase service key rotation guidance", 100),
        ("collections and uploads overview", 100),
    ]

    payload, telemetry = _build_search_payload(
        tree,
        "How do we rotate the supabase service key for deploy?",
        pages,
    )

    data = json.loads(payload)
    assert telemetry["used_lexical_shortlist"] is True
    assert telemetry["duplicates_suppressed"] == 1
    assert telemetry["shortlist_size"] == 1
    assert [item["node_id"] for item in data] == ["1"]


def test_build_search_payload_falls_back_when_lexical_signal_is_too_weak():
    tree = {
        "structure": [
            {"node_id": "1", "title": "Deployment", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Collections", "start_index": 2, "end_index": 2, "nodes": []},
        ]
    }
    pages = [
        ("cloud run deploy with supabase service key rotation guidance", 100),
        ("collections and uploads overview", 100),
    ]

    payload, telemetry = _build_search_payload(tree, "What does it say?", pages)

    data = json.loads(payload)
    assert telemetry["used_lexical_shortlist"] is False
    assert telemetry["shortlist_size"] == 0
    assert telemetry["top_lexical_score"] == 0.0
    assert isinstance(data, list)
    assert len(data) == 2


def test_build_search_payload_keeps_distinct_related_sections():
    tree = {
        "structure": [
            {"node_id": "1", "title": "Deploy Overview", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Deploy Rollback", "start_index": 2, "end_index": 2, "nodes": []},
            {"node_id": "3", "title": "Collections", "start_index": 3, "end_index": 3, "nodes": []},
        ]
    }
    pages = [
        ("cloud run deploy release rollout steps and service key setup", 100),
        ("cloud run deploy rollback procedure and rollback verification", 100),
        ("collections and uploads overview", 100),
    ]

    payload, telemetry = _build_search_payload(
        tree,
        "How do we deploy and rollback safely on cloud run?",
        pages,
    )

    data = json.loads(payload)
    assert telemetry["used_lexical_shortlist"] is True
    assert telemetry["duplicates_suppressed"] == 0
    assert [item["node_id"] for item in data[:2]] == ["2", "1"]


@pytest.mark.asyncio
async def test_search_nodes_uses_shortlist_prompt_when_available():
    captured = {}

    class FakeProvider:
        async def complete(self, messages):
            captured["prompt"] = messages[0].content
            return SimpleNamespace(content='["2"]')

    tree = {
        "structure": [
            {"node_id": "1", "title": "Collections", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Deployment", "start_index": 2, "end_index": 2, "nodes": []},
        ]
    }
    pages = [
        ("collections and uploads overview", 100),
        ("cloud run deploy with supabase service key rotation guidance", 100),
    ]

    result = await search_nodes(tree, "How do we rotate the supabase service key for deploy?", FakeProvider(), pages)

    assert result["node_ids"] == ["2"]
    assert result["telemetry"]["used_lexical_shortlist"] is True
    assert result["telemetry"]["duplicates_suppressed"] == 0
    assert "Candidate sections" in captured["prompt"]
    assert "Deployment" in captured["prompt"]
    assert "Collections" not in captured["prompt"]


@pytest.mark.asyncio
async def test_search_nodes_uses_full_tree_prompt_when_signal_is_weak():
    captured = {}

    class FakeProvider:
        async def complete(self, messages):
            captured["prompt"] = messages[0].content
            return SimpleNamespace(content='["1"]')

    tree = {
        "structure": [
            {"node_id": "1", "title": "Deployment", "start_index": 1, "end_index": 1, "nodes": []},
            {"node_id": "2", "title": "Collections", "start_index": 2, "end_index": 2, "nodes": []},
        ]
    }
    pages = [
        ("cloud run deploy with supabase service key rotation guidance", 100),
        ("collections and uploads overview", 100),
    ]

    result = await search_nodes(tree, "What does it say?", FakeProvider(), pages)

    assert result["node_ids"] == ["1"]
    assert result["telemetry"]["used_lexical_shortlist"] is False
    assert "Document tree" in captured["prompt"]
    assert "Candidate sections" not in captured["prompt"]


@pytest.mark.asyncio
async def test_run_rag_multi_returns_answer_with_lexical_grounding_score():
    class FakeProvider:
        async def complete(self, messages):
            last = messages[-1].content
            if "Candidate sections" in last or "Document tree" in last:
                return SimpleNamespace(content='["2"]')
            return SimpleNamespace(content="Rotate the Supabase service key in Cloud Run.")

    doc_data = {
        "name": "Deploy Guide",
        "tree": {
            "structure": [
                {"node_id": "1", "title": "Collections", "start_index": 1, "end_index": 1, "nodes": []},
                {"node_id": "2", "title": "Deployment", "start_index": 2, "end_index": 2, "nodes": []},
            ]
        },
        "pages": [
            ("collections and uploads overview", 100),
            ("cloud run deploy with supabase service key rotation guidance", 100),
        ],
    }

    result = await run_rag_multi(
        "How do we rotate the supabase service key for deploy?",
        [doc_data],
        FakeProvider(),
        [],
    )

    assert result["answer"] == "Rotate the Supabase service key in Cloud Run."
    assert isinstance(result["lexical_grounding_score"], float)
    assert 0 <= result["lexical_grounding_score"] <= 1
    assert result["retrieval_telemetry"][0]["used_lexical_shortlist"] is True
    assert result["retrieval_telemetry"][0]["top_lexical_score"] > 0
    assert result["retrieval_telemetry"][0]["duplicates_suppressed"] == 0


@pytest.mark.asyncio
async def test_run_rag_multi_prefers_relevant_document_in_multi_doc_case():
    prompts = []

    class FakeProvider:
        async def complete(self, messages):
            last = messages[-1].content
            prompts.append(last)
            if "Candidate sections" in last or "Document tree" in last:
                if "Deploy Guide" in last:
                    return SimpleNamespace(content='["2"]')
                return SimpleNamespace(content='["1"]')
            return SimpleNamespace(content="Rotate the Supabase service key in Cloud Run.")

    deploy_doc = {
        "name": "Deploy Guide",
        "tree": {
            "structure": [
                {"node_id": "1", "title": "Collections", "start_index": 1, "end_index": 1, "nodes": []},
                {"node_id": "2", "title": "Deployment", "start_index": 2, "end_index": 2, "nodes": []},
            ]
        },
        "pages": [
            ("collections and uploads overview", 100),
            ("cloud run deploy with supabase service key rotation guidance", 100),
        ],
    }
    collections_doc = {
        "name": "Collections Guide",
        "tree": {
            "structure": [
                {"node_id": "1", "title": "Collections", "start_index": 1, "end_index": 1, "nodes": []},
                {"node_id": "2", "title": "Search", "start_index": 2, "end_index": 2, "nodes": []},
            ]
        },
        "pages": [
            ("collections and uploads overview", 100),
            ("search filters and browsing collections", 100),
        ],
    }

    result = await run_rag_multi(
        "How do we rotate the supabase service key for deploy?",
        [deploy_doc, collections_doc],
        FakeProvider(),
        [],
    )

    assert result["answer"] == "Rotate the Supabase service key in Cloud Run."
    assert result["retrieval_telemetry"][0]["document_name"] == "Deploy Guide"
    assert result["retrieval_telemetry"][0]["top_lexical_score"] > 0
    assert result["retrieval_telemetry"][1]["top_lexical_score"] == 0.0
    assert "Document: Deploy Guide" in prompts[-1]
    assert "Document: Collections Guide" not in prompts[-1]

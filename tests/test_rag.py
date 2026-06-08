"""RAG over the collections policy — fully offline.

These run without a network or a model download: the index uses an in-memory
ChromaDB client and the ``DeterministicEmbeddingFunction`` (a hashing
vectorizer), so they're a fast, reproducible companion to the SQL-guard suite.
They prove the two properties Phase 3 is about: the indexer is **idempotent**
(same input → same IDs and count, no duplicates, stale chunks pruned) and
retrieval **finds the right rule**.
"""

from __future__ import annotations

import json
import os

import chromadb
import pytest

from src.agent.tools import make_search_policy_tool
from src.rag.chunking import chunk_policy
from src.rag.embeddings import DeterministicEmbeddingFunction
from src.rag.index import build_index, get_collection

POLICY_PATH = os.path.join("data", "collections_policy.md")
COLLECTION = "collections_policy_test"


@pytest.fixture
def embedding_function() -> DeterministicEmbeddingFunction:
    return DeterministicEmbeddingFunction()


@pytest.fixture
def client() -> chromadb.ClientAPI:
    return chromadb.EphemeralClient()


# --- chunking ---------------------------------------------------------------


def test_chunks_one_per_heading_with_stable_ids() -> None:
    text = "# Title\n\nintro to drop\n\n## Credit holds\n\nbody A\n\n## Disputes\n\nbody B\n"
    chunks = chunk_policy(text, source="policy.md")

    assert [c.heading for c in chunks] == ["Credit holds", "Disputes"]
    # Content before the first `##` (title + intro) is dropped.
    assert "intro to drop" not in chunks[0].text
    # The heading is kept in the chunk text so it stays self-describing.
    assert chunks[0].text.startswith("## Credit holds")
    # IDs are deterministic: namespace + heading slug.
    assert chunks[0].id == "policy.md::credit-holds"
    # Re-chunking the same text yields identical IDs.
    assert [c.id for c in chunks] == [c.id for c in chunk_policy(text, "policy.md")]


# --- idempotent indexing ----------------------------------------------------


def test_indexing_is_idempotent(client, embedding_function) -> None:
    first = build_index(client, POLICY_PATH, COLLECTION, embedding_function)
    ids_first = sorted(first.get(include=[])["ids"])
    count_first = first.count()

    # Re-index the same document: same IDs, same count — no duplicates.
    second = build_index(client, POLICY_PATH, COLLECTION, embedding_function)
    ids_second = sorted(second.get(include=[])["ids"])

    assert count_first > 0
    assert second.count() == count_first
    assert ids_second == ids_first


def test_indexing_prunes_removed_sections(client, embedding_function, tmp_path) -> None:
    doc = tmp_path / "policy.md"
    doc.write_text("## Alpha\n\nbody A\n\n## Beta\n\nbody B\n", encoding="utf-8")
    collection = build_index(client, str(doc), COLLECTION, embedding_function)
    assert collection.count() == 2

    # Drop a section and re-index: the orphaned chunk is pruned, not left behind.
    doc.write_text("## Alpha\n\nbody A\n", encoding="utf-8")
    collection = build_index(client, str(doc), COLLECTION, embedding_function)
    assert collection.count() == 1
    assert collection.get(include=[])["ids"] == ["policy.md::alpha"]


# --- retrieval --------------------------------------------------------------


def test_search_finds_the_expected_rule(client, embedding_function) -> None:
    build_index(client, POLICY_PATH, COLLECTION, embedding_function)
    collection = get_collection(client, COLLECTION, embedding_function)

    result = collection.query(
        query_texts=["when is a customer placed on credit hold"], n_results=3
    )
    top_headings = [meta["heading"] for meta in result["metadatas"][0]]
    assert "Credit holds" in top_headings


def test_search_policy_tool_returns_cited_json(client, embedding_function) -> None:
    build_index(client, POLICY_PATH, COLLECTION, embedding_function)
    collection = get_collection(client, COLLECTION, embedding_function)
    tool = make_search_policy_tool(collection, k=3)

    payload = json.loads(tool.invoke({"query": "how long until an invoice is written off"}))
    assert payload["result_count"] == 3
    assert all(item["heading"] for item in payload["results"])  # every hit is citable
    headings = [item["heading"] for item in payload["results"]]
    assert "Write-off thresholds" in headings

"""Idempotent ChromaDB indexer over the collections policy.

``build_index`` is safe to run any number of times: chunk IDs are deterministic
(``chunking``), so it ``upsert``s every section by ID — re-running the same
document overwrites in place rather than appending. It also **prunes** any IDs
left in the collection that the current document no longer produces, so a
deleted or renamed section can't leave a stale chunk behind. Net effect: the
collection always mirrors the document exactly (see ADR-006).
"""

from __future__ import annotations

from pathlib import Path

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import EmbeddingFunction

from src.rag.chunking import chunk_policy

# MiniLM (and the hashing fallback) are best compared with cosine distance.
_COLLECTION_METADATA = {"hnsw:space": "cosine"}


def get_collection(
    client: ClientAPI,
    name: str,
    embedding_function: EmbeddingFunction,
) -> Collection:
    """Open (or create) the policy collection bound to ``embedding_function``."""
    return client.get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
        metadata=_COLLECTION_METADATA,
    )


def build_index(
    client: ClientAPI,
    policy_path: str,
    collection_name: str,
    embedding_function: EmbeddingFunction,
) -> Collection:
    """Index ``policy_path`` into ``collection_name``; return the collection.

    Idempotent: same input → same IDs and the same final count, with no
    duplicates and no orphaned chunks.
    """
    path = Path(policy_path)
    text = path.read_text(encoding="utf-8")
    chunks = chunk_policy(text, source=path.name)
    if not chunks:
        raise ValueError(f"No '##' sections found in {policy_path!r}; nothing to index.")

    collection = get_collection(client, collection_name, embedding_function)
    collection.upsert(
        ids=[c.id for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[
            {"heading": c.heading, "source": c.source, "index": c.index}
            for c in chunks
        ],
    )

    # Prune chunks that the current document no longer produces.
    current_ids = {c.id for c in chunks}
    existing_ids = set(collection.get(include=[])["ids"])
    stale = existing_ids - current_ids
    if stale:
        collection.delete(ids=list(stale))

    return collection


def ensure_policy_index(settings) -> Collection:
    """Open the persistent policy collection, indexing it on first use.

    Used by the agent at build time; the indexer is idempotent, so an already
    populated collection is reused as-is and a fresh one is built once.
    """
    from src.rag.embeddings import default_embedding_function

    client = chromadb.PersistentClient(path=settings.chroma_path)
    embedding_function = default_embedding_function()
    collection = get_collection(client, settings.policy_collection, embedding_function)
    if collection.count() == 0:
        collection = build_index(
            client,
            settings.policy_path,
            settings.policy_collection,
            embedding_function,
        )
    return collection

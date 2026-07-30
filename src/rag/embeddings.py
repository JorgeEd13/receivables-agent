"""Embedding functions for the policy index.

The default is **local** (ChromaDB's bundled ONNX ``all-MiniLM-L6-v2``): no API
key, no per-call cost, and the demo Space stays self-contained — embeddings
don't depend on the LLM provider or its quota (ADR-005).

``DeterministicEmbeddingFunction`` is a tiny, dependency-free hashing
vectorizer. It is **not** semantic, but it is fully offline and reproducible
(no model download), so the unit tests can index and retrieve without touching
the network. Shared tokens between a query and a chunk produce overlap under
cosine distance, which is enough for the tests to assert the right rule is
found.
"""

from __future__ import annotations

import hashlib
import re
from typing import cast

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.utils.embedding_functions import register_embedding_function

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def default_embedding_function() -> EmbeddingFunction:
    """The local, no-key default: ChromaDB's ONNX MiniLM (downloaded once)."""
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    return DefaultEmbeddingFunction()


@register_embedding_function
class DeterministicEmbeddingFunction(EmbeddingFunction[Documents]):
    """Offline hashing bag-of-words embedding (for tests and as a no-download
    fallback). Tokenises to words, hashes each into ``dim`` buckets, and L2
    normalises — so cosine similarity reflects shared vocabulary."""

    def __init__(self, dim: int = 256) -> None:
        self._dim = dim

    def __call__(self, input: Documents) -> Embeddings:
        # Chroma's `Embeddings` alias is list[np.ndarray], but the runtime
        # accepts plain float lists and that is what this deterministic stub
        # produces. Casting states the stub/runtime mismatch instead of hiding
        # it behind a bare ignore.
        return cast(Embeddings, [self._embed(text) for text in input])

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
            vec[int(digest, 16) % self._dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def name() -> str:
        return "deterministic_hashing"

    def get_config(self) -> dict:
        return {"dim": self._dim}

    @classmethod
    def build_from_config(cls, config: dict) -> DeterministicEmbeddingFunction:
        return cls(dim=config.get("dim", 256))

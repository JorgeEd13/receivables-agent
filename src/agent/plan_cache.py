"""Semantic plan-cache: cache the *reasoning*, never the answer (ADR-009).

The demo runs on a free CPU tier with a tiny model, so the LLM is the slow,
expensive part of every turn. This cache skips it for questions the agent has
effectively answered before — but it does so **without ever caching a stale
number**.

The load-bearing rule (do not violate): we cache the question → **plan**, not
the question → **answer**. A *plan* is the sequence of tool calls the agent
chose (the validated ``query_ledger`` SQL, the ``search_policy`` query text) —
i.e. *which tools to call with which arguments*. On a hit the plan is
**re-executed live** against the read-only ledger (see ``plan_replay``), so the
number is always fresh even if the ledger was regenerated with more customers or
a new scenario. Only the LLM's *reasoning / tool-selection* is skipped; the data
is never frozen.

Storage reuses the RAG stack already in the repo (ChromaDB + the local ONNX
MiniLM embeddings), so semantic lookup adds **no new dependency**. Similarity is
cosine over the question embedding; a conservative threshold keeps
"check this account" from ever matching "send this account" — and even a wrong
match cannot do harm, because every replayed plan is re-validated by
``sql_guard`` and runs read-only before it is used (``plan_replay``).

Honesty guardrails baked in here:

* only **read-only** plans are ever stored (a plan whose SQL fails the guard, or
  that includes no groundable tool call, is not cached);
* the plan carries only tool calls — no answer text, no numbers;
* a conservative default similarity threshold (miss → fall through to the LLM).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import chromadb
from chromadb.api.models.Collection import Collection
from chromadb.api.types import EmbeddingFunction

from src.agent.sql_guard import GuardrailError, guard_query

# The tools whose calls we know how to replay live. A plan referencing anything
# else is not cacheable (we can't guarantee we can reproduce it deterministically
# and read-only), so it falls through to the LLM.
REPLAYABLE_TOOLS: frozenset[str] = frozenset({"query_ledger", "search_policy"})


@dataclass(frozen=True)
class ToolStep:
    """One grounded step of a plan: a tool name and the arguments to call it with.

    For ``query_ledger`` the argument is the SQL; for ``search_policy`` it is the
    natural-language query. This is *intent*, not output — it is re-executed live
    on a hit.
    """

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Plan:
    """An ordered list of tool steps the agent chose for a question.

    Deliberately holds **no answer and no numbers** — only the reasoning
    (which tools, which arguments). Replayed live to produce a fresh answer.
    """

    steps: tuple[ToolStep, ...]

    def to_json(self) -> str:
        return json.dumps([{"tool": s.tool, "args": s.args} for s in self.steps])

    @classmethod
    def from_json(cls, raw: str) -> "Plan":
        data = json.loads(raw)
        return cls(steps=tuple(ToolStep(tool=d["tool"], args=d["args"]) for d in data))


def plan_from_messages(messages: list[Any]) -> Plan | None:
    """Extract a cacheable plan from a finished agent turn, or ``None``.

    Reads the tool calls the agent made (in order, de-duplicated) from the
    LangGraph message list. Returns ``None`` — meaning "do not cache this turn" —
    when the turn is not safely reproducible:

    * it called **no** replayable tool (nothing to ground / re-execute), or
    * any ``query_ledger`` SQL does not pass ``sql_guard`` (never store a plan we
      couldn't re-validate and run read-only).

    A returned plan is guaranteed re-validatable and read-only.
    """
    steps: list[ToolStep] = []
    seen: set[tuple[str, str]] = set()
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
            if name not in REPLAYABLE_TOOLS or not isinstance(args, dict):
                continue
            key = (name, json.dumps(args, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            steps.append(ToolStep(tool=name, args=args))

    if not steps:
        return None

    # Never cache a plan we cannot re-validate as read-only.
    for step in steps:
        if step.tool == "query_ledger":
            sql = step.args.get("sql")
            if not isinstance(sql, str):
                return None
            try:
                guard_query(sql)
            except GuardrailError:
                return None

    return Plan(steps=tuple(steps))


class PlanCache:
    """A semantic cache of question → plan, backed by a ChromaDB collection.

    Keys are the *question embedding* (the same local MiniLM embeddings the RAG
    index uses); the stored document is the question text and the metadata holds
    the serialized plan. Lookup is cosine similarity with a conservative
    threshold; a miss returns ``None`` so the caller falls through to the LLM.
    """

    def __init__(
        self,
        collection: Collection,
        *,
        similarity_threshold: float = 0.90,
    ) -> None:
        self._collection = collection
        self._threshold = similarity_threshold

    def lookup(self, question: str) -> Plan | None:
        """Return a cached plan for a semantically similar question, or ``None``.

        ChromaDB returns cosine *distance* (``1 - cosine_similarity``); we accept
        a hit only when similarity ``>= threshold`` (distance ``<= 1 - threshold``).
        """
        if self._collection.count() == 0:
            return None
        result = self._collection.query(query_texts=[question], n_results=1)
        ids = result["ids"][0]
        if not ids:
            return None
        raw_distances = result.get("distances")
        distances: list[float | None] = (
            list(raw_distances[0]) if raw_distances else [None]
        )
        distance = distances[0]
        if distance is None or distance > (1.0 - self._threshold):
            return None
        raw_metas = result["metadatas"]
        if raw_metas is None:
            return None
        metadata = raw_metas[0][0] or {}
        raw = metadata.get("plan")
        if not isinstance(raw, str) or not raw:
            return None
        return Plan.from_json(raw)

    def warm(self, question: str, plan: Plan) -> None:
        """Store (or overwrite) a plan for ``question``.

        Idempotent by question text: the ID is the question itself, so asking the
        same thing twice updates in place instead of duplicating.
        """
        self._collection.upsert(
            ids=[question],
            documents=[question],
            metadatas=[{"plan": plan.to_json()}],
        )


_CACHE_METADATA = {"hnsw:space": "cosine"}


def get_plan_cache(
    client: chromadb.api.ClientAPI,
    collection_name: str,
    embedding_function: EmbeddingFunction,
    *,
    similarity_threshold: float = 0.90,
) -> PlanCache:
    """Open (or create) the plan-cache collection and wrap it in a ``PlanCache``."""
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata=_CACHE_METADATA,
    )
    return PlanCache(collection, similarity_threshold=similarity_threshold)

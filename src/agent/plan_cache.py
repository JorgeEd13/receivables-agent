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
cosine over the question embedding, under two rules: a conservative *threshold*
(how close is close enough) and an *ambiguity margin* (how much closer than the
nearest **different** plan). What a wrong match can and cannot do, precisely —
the loose version of this sentence was corrected on 2026-08-01, see ADR-009
Amendment:

* it **cannot** corrupt data or serve a stale number: every replayed plan is
  re-validated by ``sql_guard`` and runs read-only against the live ledger
  (``plan_replay``), so a hit is never a frozen answer;
* it **can** answer a *neighbouring* question. Measured on the shipped corpus:
  "Which customers have the largest overdue balances?" (a list) is 0.9767 from
  the curated plan that returns ``LIMIT 1``. The threshold does not catch that —
  raising it would only cost legitimate hits. The ambiguity margin does: that
  question is also 0.9285 from the ``LIMIT 10`` plan, and a gap that small
  between two *different* plans means the question sits between them, so the
  lookup misses and the LLM answers it;
* it **can still** be a confident wrong match. The margin rejects *ambiguity*,
  not *wrongness*: "top 10 customers by overdue balance in each aging bucket" is
  0.9841 from the top-5-per-bucket plan and is served — right shape, wrong N. Its
  neighbours are close (0.0853 behind) but all of them are other phrasings of the
  *same* plan, so there is no rival to reject. Telling "closest" from "right"
  needs the model, which is what a cache hit skips by definition.

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

# How much closer to the winner than to the runner-up a question must be before
# we trust the routing, when the two point at *different* plans. One owner: both
# entry points below default to this name, never to a second literal.
#
# Calibrated against the shipped corpus with the production MiniLM embeddings
# (`tests/test_plan_routing.py` re-measures both walls on every run):
#   * the widest measured *ambiguous* gap is 0.0482 — must be rejected;
#   * the tightest measured *legitimate* paraphrase gap is 0.1589 — must survive.
# 0.10 sits between them with room on both sides, and errs on the side ADR-009
# already chose: a slow answer beats a confidently wrong one.
AMBIGUITY_MARGIN: float = 0.10

# How many neighbours the ambiguity check looks at. Two is not enough and the
# reason is this corpus: four curated phrasings share the top-5-per-bucket plan,
# so the two nearest neighbours of a question in that cluster are the *same* plan
# and the genuinely different rival sits at rank 2, unexamined. Found by a blind
# adversarial pass on 2026-08-01, three reproducing questions.
#
# The scan stops at the margin, so this is only a ceiling on how many same-plan
# near-duplicates can hide a rival: if all 32 nearest neighbours hold one plan and
# every one of them is inside the margin, there is no rival to find.
_NEIGHBOURS_SCANNED: int = 32


def _same_question(stored_id: str, question: str) -> bool:
    """Whether a stored ID and a typed question are the same question.

    ``warm`` stores the question text as the ID, so this is a **key** comparison,
    not a distance one. It cannot be byte equality: a shipped one-click question
    arrives with a trailing space or a lower-cased first letter often enough, and
    MiniLM places those at distance **0.0000** from the original — measured, when
    this was ``ids[0] == question``, those variants missed the shortcut, tripped
    the margin against a 0.0844 neighbour and fell through to the slow model.

    One owner for the normalisation, applied to both sides here, so the two sides
    cannot drift apart the way two copies of a ``strip`` did in ADR-014.
    """
    return " ".join(stored_id.split()).casefold() == " ".join(question.split()).casefold()


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
    def from_json(cls, raw: str) -> Plan:
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
        ambiguity_margin: float = AMBIGUITY_MARGIN,
    ) -> None:
        self._collection = collection
        self._threshold = similarity_threshold
        self._margin = ambiguity_margin

    def lookup(self, question: str) -> Plan | None:
        """Return a cached plan for a semantically similar question, or ``None``.

        ChromaDB returns cosine *distance* (``1 - cosine_similarity``); we accept
        a hit only when similarity ``>= threshold`` (distance ``<= 1 - threshold``).
        Distance gaps and similarity gaps are the same number under cosine, which
        is why the margin below can be compared against distances directly.

        Several neighbours are fetched, not one, because a threshold alone cannot
        tell "the same question, reworded" from "a question that sits *between*
        two different plans". Every neighbour within ``ambiguity_margin`` of the
        winner is examined, and the first one carrying a **different** plan makes
        this a miss, so the caller falls through to the LLM (ADR-009 Amendment
        2026-08-01). Near-duplicates of the winner's *own* plan are not rivals —
        that is what lets four phrasings of one curated question keep hitting.

        An **exact** question skips that check: the stored ID *is* the question
        text, so an exact match is a key lookup, not a nearest-neighbour guess.
        This is load-bearing, not an optimisation — two of the demo's one-click
        questions are 0.9156 apart, closer to each other than the margin, so
        without it every hit on those two chips would fall to the slow model.
        """
        stored = self._collection.count()
        if stored == 0:
            return None
        result = self._collection.query(
            query_texts=[question], n_results=min(stored, _NEIGHBOURS_SCANNED)
        )
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
        plan = self._plan_at(result, 0)
        if plan is None:
            return None
        if _same_question(ids[0], question):
            return plan

        window = distance + self._margin
        for index in range(1, len(ids)):
            if index >= len(distances):
                break
            neighbour_distance = distances[index]
            if neighbour_distance is None or neighbour_distance >= window:
                break  # neighbours are ordered: everything left is outside the margin
            rival = self._plan_at(result, index)
            if rival is not None and rival != plan:
                return None
        return plan

    @staticmethod
    def _plan_at(result: Any, index: int) -> Plan | None:
        """The plan stored on the ``index``-th neighbour, or ``None`` if absent.

        ChromaDB nests results by query then by neighbour, and ``metadatas`` is
        one of the fields a caller may not have asked for — hence the two levels
        of indexing and the ``None`` handling.
        """
        raw_metas = result["metadatas"]
        if raw_metas is None:
            return None
        row = raw_metas[0]
        if row is None or index >= len(row):
            return None
        metadata = row[index] or {}
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
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> PlanCache:
    """Open (or create) the plan-cache collection and wrap it in a ``PlanCache``."""
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=embedding_function,
        metadata=_CACHE_METADATA,
    )
    return PlanCache(
        collection,
        similarity_threshold=similarity_threshold,
        ambiguity_margin=ambiguity_margin,
    )

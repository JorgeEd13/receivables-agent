"""A drop-in agent wrapper that adds the semantic plan-cache (ADR-009).

``CachedAgent`` presents the *same* interface the rest of the app already uses —
``invoke`` / ``ainvoke`` returning a LangGraph-shaped ``{"messages": [...]}`` —
so the API, the evals and the tests need no change. It wraps the compiled ReAct
agent and, on each turn:

1. **Lookup.** Embed the question and search the plan-cache. On a hit, **replay
   the cached plan live** (re-validate SQL → read-only execute; re-run policy
   retrieval) and return a deterministic answer built from the *fresh* results —
   the LLM is never called. If the replay can't be reproduced safely
   (``ReplayError``), fall through as if it were a miss.
2. **Miss → LLM.** Call the wrapped agent, then extract a cacheable plan from the
   turn (``plan_from_messages`` — only read-only, guard-validated plans qualify)
   and **warm** the cache for next time.

The correctness contract lives in ``plan_cache`` / ``plan_replay``: we cache the
*plan*, never the answer, and always re-execute live — so a hit is fast but the
number is current even after the ledger changes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncIterator

import duckdb
from chromadb.api.models.Collection import Collection

from src.agent.message_utils import final_text as _final_of
from src.agent.message_utils import tools_used as _tools_used_of
from src.agent.plan_cache import PlanCache, plan_from_messages
from src.agent.plan_replay import ReplayError, ReplayResult, replay_plan

# Streaming event vocabulary (see `astream`): the API turns each dict into one SSE
# event so the UI can show live progress. Kept tiny and stable.
#   {"type": "cached"}                         — a plan-cache hit; answer is imminent
#   {"type": "tool",   "name": "query_ledger"} — the agent invoked a tool
#   {"type": "answer", "reply": str, "tools_used": [str]} — the final answer
#   {"type": "error",  "message": str}         — the turn failed


def _last_user_message(messages: list[dict[str, Any]]) -> str | None:
    """The content of the final user turn in the request, if any."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else None
    return None


def _replay_as_messages(
    inbound: list[dict[str, Any]], result: ReplayResult
) -> dict[str, Any]:
    """Shape a replay result like a finished LangGraph turn.

    Produces one synthetic tool-call message per tool used (so the API's
    ``tools_used`` extraction sees them) plus the final answer message — matching
    what ``src/api/app.py`` reads out of a real turn.
    """
    tool_turns = [
        SimpleNamespace(
            content="",
            tool_calls=[{"name": name, "args": {}, "id": f"replay-{i}"}],
        )
        for i, name in enumerate(result.tools_used)
    ]
    final = SimpleNamespace(content=result.reply, tool_calls=[])
    return {"messages": inbound + tool_turns + [final]}


class CachedAgent:
    """Wrap a compiled agent with a live-replay semantic plan-cache."""

    def __init__(
        self,
        agent: Any,
        cache: PlanCache,
        con: duckdb.DuckDBPyConnection,
        policy: Collection,
        *,
        max_rows: int,
        search_k: int,
        recursion_limit: int = 8,
    ) -> None:
        self._agent = agent
        self._cache = cache
        self._con = con
        self._policy = policy
        self._max_rows = max_rows
        self._search_k = search_k
        # Cap ReAct steps so a looping tiny model fails fast (ADR-013).
        self._config = {"recursion_limit": recursion_limit}

    # --- the interface the app/evals call -------------------------------- #

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload["messages"]
        hit = self._try_cache(messages)
        if hit is not None:
            return hit
        result = self._agent.invoke(payload, config=self._config)
        self._warm(messages, result["messages"])
        return result

    async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload["messages"]
        hit = self._try_cache(messages)
        if hit is not None:
            return hit
        result = await self._agent.ainvoke(payload, config=self._config)
        self._warm(messages, result["messages"])
        return result

    async def astream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield progress events for one turn (for the SSE endpoint).

        On a **cache hit** we emit a ``cached`` marker, one ``tool`` event per tool
        the replay used, then the final ``answer`` — fast, no LLM. On a **miss** we
        stream the wrapped agent's real step events (a ``tool`` event as each tool
        starts) and finish with the ``answer`` once the loop ends, warming the cache.
        Any failure surfaces as an ``error`` event rather than a broken stream.
        """
        messages = payload["messages"]
        hit = self._try_cache(messages)
        if hit is not None:
            out = hit["messages"]
            yield {"type": "cached"}
            for name in _tools_used_of(out):
                yield {"type": "tool", "name": name}
            yield {"type": "answer", "reply": _final_of(out), "tools_used": _tools_used_of(out)}
            return

        # Miss: stream the real ReAct loop. `astream_events` surfaces a
        # `on_tool_start` per tool call; the final state carries the answer.
        from langgraph.errors import GraphRecursionError

        final_messages: list[Any] | None = None
        seen: set[str] = set()
        try:
            async for event in self._agent.astream_events(
                payload, version="v2", config=self._config
            ):
                kind = event.get("event")
                if kind == "on_tool_start":
                    name = event.get("name", "")
                    if name and name not in seen:
                        seen.add(name)
                        yield {"type": "tool", "name": name}
                elif kind == "on_chain_end":
                    data = event.get("data", {}) or {}
                    output = data.get("output")
                    if isinstance(output, dict) and "messages" in output:
                        final_messages = output["messages"]
        except GraphRecursionError:
            # The tiny model looped without converging (over-calling tools). Fail
            # gracefully with an honest, actionable answer instead of thrashing.
            yield {
                "type": "answer",
                "reply": (
                    "I couldn't finish this one on the free-tier tiny model — it went "
                    "back and forth without settling on an answer. Try one of the "
                    "example questions (they're instant), or rephrase this more simply."
                ),
                "tools_used": sorted(seen),
            }
            return
        except Exception as exc:  # keep the stream well-formed on any failure
            yield {"type": "error", "message": str(exc)}
            return

        if final_messages is None:
            yield {"type": "error", "message": "no response produced"}
            return
        self._warm(messages, final_messages)
        yield {
            "type": "answer",
            "reply": _final_of(final_messages),
            "tools_used": _tools_used_of(final_messages),
        }

    # --- internals ------------------------------------------------------- #

    def _try_cache(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return a replayed-turn dict on a usable cache hit, else ``None``.

        Only single-turn requests are cache-eligible: a follow-up that depends on
        prior conversation context is answered by the LLM, not a cached plan.
        """
        if len(messages) != 1:
            return None
        question = _last_user_message(messages)
        if not question:
            return None
        plan = self._cache.lookup(question)
        if plan is None:
            return None
        try:
            result = replay_plan(
                plan,
                self._con,
                self._policy,
                max_rows=self._max_rows,
                search_k=self._search_k,
            )
        except ReplayError:
            return None  # can't reproduce safely → fall through to the LLM
        return _replay_as_messages(messages, result)

    def _warm(
        self, inbound: list[dict[str, Any]], out_messages: list[Any]
    ) -> None:
        """Extract a cacheable plan from a finished turn and store it."""
        if len(inbound) != 1:
            return
        question = _last_user_message(inbound)
        if not question:
            return
        plan = plan_from_messages(out_messages)
        if plan is not None:
            self._cache.warm(question, plan)

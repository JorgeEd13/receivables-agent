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

import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import duckdb
from chromadb.api.models.Collection import Collection

from src.agent.message_utils import final_text as _final_of
from src.agent.message_utils import tools_used as _tools_used_of
from src.agent.plan_cache import PlanCache, plan_from_messages
from src.agent.plan_replay import ReplayError, ReplayResult, replay_plan
from src.agent.turn_control import (
    ToolCallTracker,
    finalize_answer,
    narrate_end,
    narrate_start,
)

# Streaming event vocabulary (see `astream`): the API turns each dict into one SSE
# event so the UI can show live progress. Kept tiny and stable.
#   {"type": "cached"}                         — a plan-cache hit; answer is imminent
#   {"type": "tool",   "name": "query_ledger"} — the agent invoked a tool
#   {"type": "answer", "reply": str, "tools_used": [str]} — the final answer
#   {"type": "error",  "message": str}         — the turn failed


def _tool_output_text(output: Any) -> str | None:
    """Best-effort extraction of a tool's string result from an astream event.

    ``on_tool_end`` carries the tool's return as either the raw string our tools
    produce or a ``ToolMessage``-like object with a ``.content``. Narration is
    cosmetic, so anything unexpected degrades to ``None``.
    """
    if output is None:
        return None
    content = getattr(output, "content", output)
    return content if isinstance(content, str) else None


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
        narrated_step_cap: int | None = None,
        wall_clock_budget_s: float = 0.0,
        tracker: ToolCallTracker | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._agent = agent
        self._cache = cache
        self._con = con
        self._policy = policy
        self._max_rows = max_rows
        self._search_k = search_k
        # The turn tracker wrapping the tools (dedup + observation log, ADR-014).
        # Optional so tests can construct a bare CachedAgent; when absent,
        # finalization degrades to the honest no-progress message.
        self._tracker = tracker
        # Silent (non-streaming) path: a tight loop guard so a long wait never reads
        # as hung (ADR-013).
        self._config = {"recursion_limit": recursion_limit}
        # Narrated streaming path (ADR-014, 8.5): a higher step cap — safe because
        # progress is visible + dedup keeps repeats cheap — bounded by a soft
        # wall-clock budget (the wait guard). Defaults to the silent cap if unset.
        self._stream_config = {
            "recursion_limit": narrated_step_cap or recursion_limit
        }
        self._wall_clock_budget_s = wall_clock_budget_s
        # Injectable monotonic clock (tests supply a fake; patching the global
        # `time.monotonic` would collide with the asyncio event loop's own use).
        self._clock = clock or time.monotonic

    def _begin_turn(self) -> None:
        """Install a fresh tracker state for this turn (before driving the agent)."""
        if self._tracker is not None:
            self._tracker.begin()

    def _finalized(self, question: str | None) -> str:
        """A graceful partial answer from what the turn gathered (ADR-014)."""
        observations = self._tracker.observations() if self._tracker else []
        return finalize_answer(observations, question)

    # --- the interface the app/evals call -------------------------------- #

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        from langgraph.errors import GraphRecursionError

        messages = payload["messages"]
        hit = self._try_cache(messages)
        if hit is not None:
            return hit
        self._begin_turn()
        try:
            result = self._agent.invoke(payload, config=self._config)
        except GraphRecursionError:
            # Ceiling hit: finalize from what the turn gathered instead of raising
            # (ADR-014). The partial run isn't cacheable, so we don't warm.
            return self._finalized_messages(messages)
        self._warm(messages, result["messages"])
        return result

    async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        from langgraph.errors import GraphRecursionError

        messages = payload["messages"]
        hit = self._try_cache(messages)
        if hit is not None:
            return hit
        self._begin_turn()
        try:
            result = await self._agent.ainvoke(payload, config=self._config)
        except GraphRecursionError:
            return self._finalized_messages(messages)
        self._warm(messages, result["messages"])
        return result

    async def astream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield progress events for one turn (for the SSE endpoint).

        On a **cache hit** we emit a ``cached`` marker, one ``tool`` event per tool
        the replay used, then the final ``answer`` — fast, no LLM. On a **miss** we
        stream the wrapped agent's real step events — a ``tool`` event and a human
        ``step`` narration line as each tool starts / ends (ADR-014, 8.5) — and
        finish with the ``answer`` once the loop ends, warming the cache. A **soft
        wall-clock budget** (the wait guard) finalizes early from what's gathered if
        the run runs long; the ceiling (loop guard) finalizes the same way. Any
        failure surfaces as an ``error`` event rather than a broken stream.
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

        # Miss: stream the real ReAct loop. `astream_events` surfaces
        # `on_tool_start` / `on_tool_end` per tool call; the final state carries the
        # answer. We run it on the *narrated* config (a higher step cap) and bound it
        # with a soft wall-clock budget.
        from langgraph.errors import GraphRecursionError

        self._begin_turn()
        question = _last_user_message(messages)
        deadline = (
            self._clock() + self._wall_clock_budget_s
            if self._wall_clock_budget_s > 0
            else None
        )
        final_messages: list[Any] | None = None
        seen: set[str] = set()
        try:
            async for event in self._agent.astream_events(
                payload, version="v2", config=self._stream_config
            ):
                kind = event.get("event")
                if kind == "on_tool_start":
                    name = event.get("name", "")
                    if name:
                        if name not in seen:
                            seen.add(name)
                            yield {"type": "tool", "name": name}
                        args = (event.get("data") or {}).get("input") or {}
                        yield {"type": "step", "text": narrate_start(name, args)}
                elif kind == "on_tool_end":
                    name = event.get("name", "")
                    if name:
                        result = _tool_output_text((event.get("data") or {}).get("output"))
                        yield {"type": "step", "text": narrate_end(name, result)}
                elif kind == "on_chain_end":
                    data = event.get("data", {}) or {}
                    output = data.get("output")
                    if isinstance(output, dict) and "messages" in output:
                        final_messages = output["messages"]

                # Soft wall-clock budget (8.5): a *visible* long run is fine, but past
                # the budget we stop spending steps and finalize from what we have —
                # narrated, never a silent stall or a bare timeout.
                if deadline is not None and self._clock() > deadline:
                    yield {
                        "type": "step",
                        "text": "This is taking a while — wrapping up with what I have so far…",
                    }
                    yield {
                        "type": "answer",
                        "reply": self._finalized(question),
                        "tools_used": sorted(seen),
                    }
                    return
        except GraphRecursionError:
            # The tiny model looped without converging (over-calling tools). Instead
            # of a canned apology, finalize from what the turn actually gathered
            # (ADR-014): name the partial ledger/policy findings + the specific gap.
            yield {
                "type": "answer",
                "reply": self._finalized(question),
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

    def _finalized_messages(self, inbound: list[dict[str, Any]]) -> dict[str, Any]:
        """Shape a ceiling finalization like a finished turn (for invoke/ainvoke).

        Mirrors ``_replay_as_messages``: one synthetic tool-call message per tool
        the turn used (so ``tools_used`` extraction still works) + the finalized
        answer built from the recorded observations (ADR-014).
        """
        observations = self._tracker.observations() if self._tracker else []
        question = _last_user_message(inbound)
        used: list[str] = []
        for obs in observations:
            if obs.tool not in used:
                used.append(obs.tool)
        tool_turns = [
            SimpleNamespace(
                content="",
                tool_calls=[{"name": name, "args": {}, "id": f"final-{i}"}],
            )
            for i, name in enumerate(used)
        ]
        final = SimpleNamespace(content=self._finalized(question), tool_calls=[])
        return {"messages": inbound + tool_turns + [final]}

    def _try_cache(self, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Return a replayed-turn dict on a usable cache hit, else ``None``.

        Look up the **latest user question** against the cache regardless of prior
        history. A cached plan is by construction *self-contained* (it embeds its own
        SQL / policy query, ADR-009), so recognizing a known question mid-conversation
        is safe — and it's exactly what a demo needs (clicking a suggested question
        after the first answer must still be instant, not fall to the slow LLM). A
        genuine context-dependent follow-up ("and for enterprise only?") simply won't
        clear the conservative similarity threshold, so it falls through to the LLM.
        (Warming still requires a single-turn request — see ``_warm``.)
        """
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

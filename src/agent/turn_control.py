"""Turn-scoped control for the tiny-model ReAct loop (Phase 8, ADR-014).

Three seams that make a tiny model on a free CPU **degrade gracefully** instead of
thrashing then apologizing. They *improve* the ADR-013 ceiling (a low step cap +
a graceful `GraphRecursionError` catch); they do not replace it. (Slice 1 shipped
the first two; narration arrived with slice 2.)

* **Dedup / redundant-call short-circuit (8.2).** A tiny model tends to re-issue
  the *same* tool call — burning its small step budget on work it already did.
  ``ToolCallTracker`` wraps each tool so an **identical** call within one turn
  returns the *memoized* result plus a firm nudge ("you already have this — answer
  now"), instead of re-executing. This doesn't lower the step count, but it stops
  the loop paying for the same query twice and pushes the model to finalize.
  What counts as "identical" is the wrapped tool's business: a tool whose argument
  is *code* passes a ``key`` that asks the parser (``query_ledger`` keys on the
  syntax tree, ``tools.py``), because text normalization cannot tell a cosmetic
  rewrite from a different literal. Tools that take prose keep the text key.

* **Forced finalization (8.1 + 8.3).** The tracker also records every
  ``(tool, args, result)`` of the turn, so when the loop still hits the step
  ceiling ``finalize_answer`` can turn that partial work into a **best-effort
  answer** — naming what the agent *did* gather and the specific gap — rather than
  the generic "couldn't finish, rephrase" dead-end.

* **Progress narration (8.5).** ``narrate_start`` / ``narrate_end`` turn each tool
  call into a human "what I'm doing / what I found" line the UI streams live. A
  *visible*, productive wait is tolerable where a *silent* one wasn't (ADR-013), so
  the narrated streaming path (``CachedAgent.astream``) takes a **raised step cap**
  bounded by a **soft wall-clock budget** — the honest split of the single ADR-013
  cap into a loop guard (steps) and a wait guard (seconds).

Turn state lives in a ``ContextVar`` so concurrent requests against the single
shared agent (built once into ``app.state``) never cross-contaminate: each turn
calls ``begin()`` to install a fresh state, and the wrapped tools read/mutate
whatever state is current. That isolation is load-bearing, and until 2026-08-01
it was **untested**: swapping the ``ContextVar`` for a module global kept the
whole suite green while two interleaved turns read each other's rows, because the
only isolation test ran turns in *sequence*, which a global also passes.
``test_interleaved_turns_do_not_cross_contaminate`` now runs two turns
concurrently through ``asyncio.gather`` and is the owner of this paragraph.

**The unit of isolation is the asyncio context, not the call.** ``begin()`` writes
into the *caller's* context; it does not create one. Requests are isolated because
the ASGI server runs each in its own task and a task starts from a copy of the
context — not because anything here forces it. Two turns driven from the *same*
task (``await agent.ainvoke(...)`` twice, interleaved by hand) share one state.
That was found by attacking this file on 2026-08-01 and is unreachable through the
HTTP surface; it is written down because "never cross-contaminate" without this
sentence is a promise wider than the mechanism.

The 8.4 "continue" affordance is met by the API's existing full-history-per-turn
contract (a follow-up already carries prior context) plus finalization's narrowed
next-step invite, so no server-side checkpointer is added here (see ADR-014).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# How a tool's arguments become the identity used for dedup. The default is text
# (`text_arg_key`); a tool whose arguments are code supplies its own (see `tools.py`).
ArgKey = Callable[[dict[str, Any]], str]

# The nudge appended to a de-duplicated tool result. Terse and imperative — a tiny
# model follows one firm instruction better than a paragraph (ADR-013).
_DEDUP_NUDGE = (
    "You already ran this exact call and its result is above. Do not repeat it — "
    "give your final answer now from what you have, or try a genuinely different query."
)


@dataclass
class Observation:
    """One tool call made during a turn: what was asked and what came back."""

    tool: str
    args: dict[str, Any]
    result: str  # the tool's raw JSON-string result


@dataclass
class _TurnState:
    """Mutable per-turn bookkeeping (dedup memo + the ordered observation log)."""

    observations: list[Observation] = field(default_factory=list)
    memo: dict[tuple[str, str], str] = field(default_factory=dict)


_TURN: ContextVar[_TurnState | None] = ContextVar("receivables_turn_state", default=None)


def text_arg_key(args: dict[str, Any]) -> str:
    """The default dedup key for a tool call's arguments: normalized text.

    Whitespace in string args is collapsed and trimmed so cosmetically-different
    but identical calls still match. Case is preserved.

    **This key is only honest for arguments that are prose** (`search_policy`'s
    question), where collapsing whitespace changes nothing a reader means. It is
    *wrong* for arguments that are code, because the collapse reaches **inside**
    string literals: until 2026-08-01 `query_ledger` used this key, and
    ``WHERE name = 'John  Doe'`` and ``WHERE name = 'John Doe'`` produced the same
    one — the second query never ran and was served the first's rows plus a "you
    already ran this exact call" note (measured 2026-07-31). A tool whose argument
    is code passes its own ``key=`` to `ToolCallTracker.wrap` instead; the SQL one
    keys on the parse tree (`sql_guard.statement_identity`), which is the only
    comparison that cannot mistake a rewrite for a different statement.
    """
    normalized = {
        k: " ".join(v.split()) if isinstance(v, str) else v for k, v in args.items()
    }
    return json.dumps(normalized, sort_keys=True, default=str)


def _with_nudge(prior_result: str) -> str:
    """Return the memoized tool result with the dedup nudge attached."""
    try:
        obj = json.loads(prior_result)
    except (ValueError, TypeError):
        return f"{prior_result}\n\n{_DEDUP_NUDGE}"
    if isinstance(obj, dict):
        obj["note"] = _DEDUP_NUDGE
        return json.dumps(obj, default=str)
    return f"{prior_result}\n\n{_DEDUP_NUDGE}"


class ToolCallTracker:
    """Wrap the agent's tools to dedup repeats and record each turn's tool calls.

    One tracker is shared by the single process-wide agent; per-turn isolation is
    provided by the ``ContextVar`` (``begin`` installs a fresh state per request).
    """

    def begin(self) -> None:
        """Start a fresh turn in the current context (call before driving the agent)."""
        _TURN.set(_TurnState())

    def observations(self) -> list[Observation]:
        """The tool calls recorded so far in the current turn (empty outside one)."""
        state = _TURN.get()
        return list(state.observations) if state is not None else []

    def wrap(
        self,
        name: str,
        func: Callable[..., str],
        *,
        key: ArgKey | None = None,
    ) -> Callable[..., str]:
        """Decorate a tool's callable with dedup + observation recording.

        ``key`` builds the identity of a call's arguments; it defaults to
        normalized text (`text_arg_key`). A tool whose arguments are code passes its
        own — the tracker deliberately knows nothing about SQL, and the tool that
        owns the language is the one that can say when two calls are the same.
        The tool *name* is always part of the key, so two tools that happen to
        share an argument name never share a memo entry.

        Outside a tracked turn (no ``begin`` — e.g. a direct unit test of a tool)
        the wrapper is a transparent pass-through, so tools stay independently
        testable.
        """
        arg_key = key or text_arg_key

        @functools.wraps(func)
        def wrapped(**kwargs: Any) -> str:
            state = _TURN.get()
            if state is None:
                return func(**kwargs)

            memo_key = (name, arg_key(kwargs))
            if memo_key in state.memo:
                # A redundant call: return the prior result + a nudge, don't re-run.
                return _with_nudge(state.memo[memo_key])

            result = func(**kwargs)
            state.memo[memo_key] = result
            state.observations.append(Observation(name, dict(kwargs), result))
            return result

        return wrapped


# --- Forced finalization (8.1 + 8.3) ------------------------------------- #

# Shown when the loop hit the ceiling having gathered nothing usable. Keeps the
# ADR-013 "couldn't finish" phrasing + an actionable hint (the honest floor).
_NO_PROGRESS = (
    "I couldn't finish this one on the free-tier tiny model — it went back and forth "
    "without settling on an answer, and didn't gather a usable result along the way. "
    "Try one of the example questions (they're instant), or rephrase this more simply."
)

# Shown when the tiny model *did* try the ledger but every query errored — typically a
# question that needs a more complex query (e.g. a top-N *within each* group) than the
# tiny model can write. Naming the attempt + a decomposition beats the bare floor.
_NO_USABLE_RESULT = (
    "I tried to answer this from the ledger, but my query didn't run cleanly on the "
    "free-tier tiny model — this looks like one that needs a more complex query (for "
    "example, a top-N *within each* group). Two quick ways forward: ask for one group "
    'at a time (e.g. "the top 5 overdue customers in the 90+ bucket"), or try one of '
    "the instant example questions."
)


def finalize_answer(
    observations: list[Observation], question: str | None = None
) -> str:
    """Compose an honest partial answer from a ceiling-hit turn's observations.

    Deterministic (no LLM): it surfaces what the agent *did* retrieve — the most
    recent successful ledger query and/or a policy lookup — plus the specific gap,
    so the fallback is *useful* work rather than a generic apology. When nothing
    usable was gathered it returns the honest no-progress message.
    """
    ledger = _last_ok_ledger(observations)
    policy = _last_policy(observations)

    if ledger is None and policy is None:
        # Nothing usable — but did it at least *try* the ledger? An attempt whose SQL
        # errored (e.g. a too-complex top-N-per-group query the tiny model can't write)
        # is more honest to name, with a concrete decomposition, than the bare floor.
        if _attempted_ledger(observations):
            return _NO_USABLE_RESULT
        return _NO_PROGRESS

    parts = [
        "I couldn't fully finish this on the free-tier tiny model, but here's what "
        "I gathered before running out of steps:"
    ]
    next_steps: list[str] = []

    if ledger is not None:
        summary, count = ledger
        parts.append(summary)
        if count:
            next_steps.append(
                "ask me to sort or narrow this list (e.g. by amount, or a single segment)"
            )
    if policy is not None:
        parts.append(policy)
        next_steps.append("ask specifically which rule to apply")

    if next_steps:
        parts.append(
            "I didn't combine these into a final answer in time — "
            + ", or ".join(next_steps)
            + " and I'll finish quickly."
        )
    return "\n\n".join(parts)


def _attempted_ledger(observations: list[Observation]) -> bool:
    """Whether the turn made any ``query_ledger`` call (successful or errored)."""
    return any(obs.tool == "query_ledger" for obs in observations)


def _last_ok_ledger(observations: list[Observation]) -> tuple[str, int] | None:
    """The most recent successful ``query_ledger`` result, rendered compactly.

    Returns ``(summary_text, row_count)`` or ``None`` if there was no successful
    ledger query (errors and rejections don't count as gathered facts).
    """
    for obs in reversed(observations):
        if obs.tool != "query_ledger":
            continue
        try:
            payload = json.loads(obs.result)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or "error" in payload:
            continue
        rows = payload.get("rows")
        if not isinstance(rows, list):
            continue
        return _render_ledger(payload), len(rows)
    return None


def _render_ledger(payload: dict[str, Any]) -> str:
    """A compact, honest rendering of a ``query_ledger`` JSON payload."""
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    if not rows:
        return "I ran a ledger query, but it returned no rows."

    preview = rows[:5]
    if len(columns) == 1 and len(preview) == 1:
        col = columns[0]
        body = f"**{col}**: {preview[0].get(col)}"
    else:
        header = " | ".join(str(c) for c in columns)
        divider = " | ".join("---" for _ in columns)
        lines = [
            " | ".join(str(row.get(c, "")) for c in columns) for row in preview
        ]
        body = "\n".join([header, divider, *lines])

    note = ""
    if len(rows) > len(preview):
        note = f"\n\n_(showing {len(preview)} of {len(rows)} rows)_"
    return f"From the ledger:\n{body}{note}"


# --- Progress narration (8.5) -------------------------------------------- #
#
# A tiny model on a free CPU is slow; ADR-013 kept the wait short by capping steps
# low, but the real problem was a *silent* wait ending in nothing. Narration makes a
# longer, productive run tolerable — the UI shows a human intent line per step
# ("Checking the collections policy…") and a finding line per result ("Found 12
# rows"). This is what lets the narrated step cap rise safely (config
# `agent_narrated_step_cap`), paired with dedup (8.2) + finalization (8.3).


def narrate_start(tool: str, args: dict[str, Any] | None) -> str:
    """A human-readable "what I'm about to do" line for a tool call (8.5)."""
    if tool == "query_ledger":
        return "Querying the ledger for the figures…"
    if tool == "search_policy":
        query = (args or {}).get("query")
        if isinstance(query, str) and query.strip():
            return f"Checking the collections policy on “{query.strip()}”…"
        return "Checking the collections policy…"
    return f"Running {tool}…"


def narrate_end(tool: str, result: str | None) -> str:
    """A human-readable "what I found" line from a tool's result (8.5).

    Best-effort and cosmetic: any parse failure degrades to a neutral "done" line.
    """
    payload: Any = None
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            payload = None

    if tool == "query_ledger":
        if isinstance(payload, dict) and "error" in payload:
            return "That query didn't run — adjusting the SQL…"
        if isinstance(payload, dict) and isinstance(payload.get("row_count"), int):
            count = payload["row_count"]
            if count == 0:
                return "The ledger returned no matching rows."
            return f"Found {count} row{'s' if count != 1 else ''} in the ledger."
        return "Got the ledger results."
    if tool == "search_policy":
        results = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(results, list) and results:
            heading = (results[0] or {}).get("heading")
            if heading:
                return f"Found the policy section “{heading}”."
            return "Found the relevant policy text."
        return "No matching policy section."
    return "Done."


def _last_policy(observations: list[Observation]) -> str | None:
    """The most recent ``search_policy`` top result, rendered with its heading."""
    for obs in reversed(observations):
        if obs.tool != "search_policy":
            continue
        try:
            payload = json.loads(obs.result)
        except (ValueError, TypeError):
            continue
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            continue
        top = results[0]
        heading = top.get("heading")
        text = (top.get("text") or "").strip()
        cite = f"**{heading}**\n" if heading else ""
        return f"From the collections policy:\n{cite}{text}"
    return None

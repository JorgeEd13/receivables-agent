"""Replay a cached plan **live**, so a cache hit is fast *and* always correct.

This is the other half of the honesty contract in ``plan_cache``: the cache
stores *what to do* (the tool calls), and this module *does it again from
scratch* on every hit:

* ``query_ledger`` SQL is **re-validated through** ``sql_guard`` and executed on
  the **read-only** ledger connection — the same guarded path the live tool
  uses. If the SQL no longer passes the guard (e.g. the schema changed), the
  replay is abandoned and the caller falls back to the LLM.
* ``search_policy`` retrieval is re-run against the live index.

The answer is then **composed deterministically from the fresh tool results** —
no LLM. It is a plain, honest rendering of the numbers/rows the plan just
produced, prefixed to make clear it is a cached-plan replay. This is why the hit
path is ~tens of milliseconds: the reasoning was cached, the data is live.

**What the prefix may claim, precisely** (the loose version of this said
"nothing is served from a frozen snapshot" and was falsified three ways by a
blind adversarial pass on 2026-08-01 — see ADR-009 Amendment 2026-08-05):

* the claim is assembled from the tools that **actually ran**, per
  ``_FRESHNESS_CLAIMS``. A policy-only plan executes no SQL, and it used to
  carry the ledger sentence anyway — on three of the twelve shipped one-click
  questions;
* a plan whose SQL reads no relation, or pins a date, is not replayed at all
  (``plan_cache.freshness_violation``), because re-running either one produces a
  fresh-looking number that is evidence of nothing, or answers the question of
  the day it was written;
* what is still true and is the point of the module: for a step that *does*
  run, every number in the rendering comes from a statement executed **now**,
  against the current ledger, through the guard.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import duckdb
from chromadb.api.models.Collection import Collection

from src.agent.ledger import run_query
from src.agent.plan_cache import Plan, freshness_violation
from src.agent.sql_guard import GuardrailError, guard_query

# What each replayable tool lets the reply claim, in the reply's own words. The
# prefix is assembled from the tools that **actually ran**, never from the fact
# that a plan was replayed at all: three of the twelve one-click questions the
# demo ships are policy-only, and every one of them was printing "the query was
# re-run live against the ledger, so the numbers are current" over **zero**
# executed statements. Found by a blind adversarial pass (2026-08-01) that
# attacked the sentence rather than the code — no mutation finds this, because
# there is no line to delete: the claim was simply wider than the work.
#
# Keys are checked against `REPLAYABLE_TOOLS` by `tests/test_replay_claims.py`,
# so a third replayable tool cannot be added and quietly inherit a sentence
# written about the other two.
_FRESHNESS_CLAIMS: dict[str, str] = {
    "query_ledger": (
        "the query was re-run live against the ledger, so the numbers are current"
    ),
    "search_policy": "the policy text was re-read from the live index",
}


class ReplayError(RuntimeError):
    """A cached plan could not be replayed live (e.g. SQL no longer valid).

    Signals the caller to fall through to the LLM rather than serve a bad answer.
    """


@dataclass(frozen=True)
class ReplayResult:
    """The outcome of replaying a plan: a fresh answer + which tools ran."""

    reply: str
    tools_used: list[str]


def replay_plan(
    plan: Plan,
    con: duckdb.DuckDBPyConnection,
    policy: Collection,
    *,
    max_rows: int,
    search_k: int,
) -> ReplayResult:
    """Execute every step of ``plan`` live and render a deterministic answer.

    Raises ``ReplayError`` if any step cannot be reproduced safely (the caller
    then falls back to the LLM). Never returns a stale value: every number comes
    from a query run *now*.
    """
    tools_used: list[str] = []
    sections: list[str] = []

    for step in plan.steps:
        if step.tool == "query_ledger":
            sections.append(_replay_query(step.args, con, max_rows))
            if "query_ledger" not in tools_used:
                tools_used.append("query_ledger")
        elif step.tool == "search_policy":
            sections.append(_replay_search(step.args, policy, search_k))
            if "search_policy" not in tools_used:
                tools_used.append("search_policy")
        else:  # not a replayable tool — should never reach here (plan is filtered)
            raise ReplayError(f"Plan step is not replayable: {step.tool!r}.")

    if not sections:
        raise ReplayError("Plan produced no replayable steps.")

    body = "\n\n".join(sections)
    claims: list[str] = []
    for tool in tools_used:
        claim = _FRESHNESS_CLAIMS.get(tool)
        if claim is None:
            # Unreachable while the loop above and `_FRESHNESS_CLAIMS` agree, and
            # deliberately not an assert: the failure mode this closes is a third
            # replayable tool added to the loop and not to the map, and the wrong
            # outcome for that is a *sentence about the ledger over a tool that
            # never touched it*. Falling back to the LLM costs a slow turn.
            raise ReplayError(f"Replayed tool {tool!r} has no freshness claim.")
        claims.append(claim)
    reply = "_(Answered from a cached plan — " + "; ".join(claims) + ".)_\n\n" + body
    return ReplayResult(reply=reply, tools_used=tools_used)


def _replay_query(
    args: dict[str, Any], con: duckdb.DuckDBPyConnection, max_rows: int
) -> str:
    """Re-validate and re-run one ``query_ledger`` step; render its fresh rows."""
    sql = args.get("sql")
    if not isinstance(sql, str):
        raise ReplayError("Cached query_ledger step has no SQL.")
    try:
        guarded = guard_query(sql, max_rows=max_rows)  # re-validate every time
    except GuardrailError as exc:
        raise ReplayError(f"Cached SQL no longer passes the guardrail: {exc}") from exc
    # Re-checked on the way *out*, not only on the way in. `plan_from_messages`
    # refuses to store these, and that would be the whole fix if the cache were
    # a process-local dict — it is a persistent collection that ships pre-seeded
    # in the demo image and is warmed by a long-running Space, so plans written
    # before this rule existed are being served right now. Same owner, both
    # sides (`plan_cache.freshness_violation`); the caller falls back to the LLM.
    unfit = freshness_violation(sql)
    if unfit is not None:
        raise ReplayError(f"Cached SQL cannot be replayed as current: {unfit}")
    try:
        columns, rows = run_query(con, guarded)
    except duckdb.Error as exc:
        raise ReplayError(f"Cached SQL failed to execute: {exc}") from exc

    return _render_rows(columns, rows)


def _replay_search(args: dict[str, Any], policy: Collection, search_k: int) -> str:
    """Re-run one ``search_policy`` step; render the retrieved rule(s)."""
    query = args.get("query")
    if not isinstance(query, str):
        raise ReplayError("Cached search_policy step has no query.")
    result = policy.query(query_texts=[query], n_results=search_k)
    raw_docs, raw_metas = result["documents"], result["metadatas"]
    if raw_docs is None or raw_metas is None:
        raise ReplayError("Cached policy search returned nothing.")
    documents = raw_docs[0]
    metadatas = raw_metas[0]
    if not documents:
        raise ReplayError("Cached policy search returned nothing.")

    top_doc = documents[0]
    heading = (metadatas[0] or {}).get("heading") if metadatas else None
    cite = f"**{heading}**\n" if heading else ""
    return f"From the collections policy:\n{cite}{top_doc.strip()}"


def _render_rows(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    """Render query rows as a compact, honest text table.

    A scalar (one row, one column) is rendered inline; anything larger becomes a
    small Markdown table. Purely mechanical — no interpretation, no LLM.
    """
    if not rows:
        return "The query returned no rows."

    if len(rows) == 1 and len(columns) == 1:
        return f"**{columns[0]}**: {_fmt(rows[0][0])}"

    header = " | ".join(columns)
    divider = " | ".join("---" for _ in columns)
    body = "\n".join(" | ".join(_fmt(v) for v in row) for row in rows)
    table = f"{header}\n{divider}\n{body}"
    note = "" if len(rows) < 25 else f"\n\n_(showing the first {len(rows)} rows)_"
    return table + note


def _fmt(value: Any) -> str:
    """Human-friendly rendering of a single cell value."""
    if value is None:
        return "—"
    if isinstance(value, float):
        # Trim noise but keep cents; drop a trailing ".0" on whole numbers.
        text = f"{value:,.2f}"
        return text[:-3] if text.endswith(".00") else text
    if isinstance(value, int):
        return f"{value:,}"
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)

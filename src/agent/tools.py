"""The agent's tools.

Two grounded tools, used together: ``query_ledger`` answers the *numbers* with
guarded text-to-SQL, and ``search_policy`` answers the *rules* by retrieving
from the collections policy (RAG). Both return JSON as text; ``query_ledger``
returns guardrail rejections and SQL errors as text too (not raised), so a
ReAct loop can read the reason and try again.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import duckdb
from chromadb.api.models.Collection import Collection
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agent.ledger import run_query
from src.agent.schema_hints import SCHEMA_HINTS
from src.agent.sql_guard import GuardrailError, guard_query, strip_wrapper_line_echo

# A tool wrapper (see ``src.agent.turn_control.ToolCallTracker.wrap``): decorates a
# tool's callable to dedup repeats + record the turn's calls. ``None`` = unwrapped.
ToolWrap = Callable[[str, Callable[..., str]], Callable[..., str]]

_TOOL_DESCRIPTION = (
    "Run a single read-only DuckDB SELECT against the receivables ledger and "
    "return the rows as JSON. Use this for any factual/number question. Only "
    "SELECT/WITH over the allow-listed relations is permitted; writes, multiple "
    "statements and unknown tables are rejected, and rows are capped.\n\n"
    + SCHEMA_HINTS
)


class QueryLedgerInput(BaseModel):
    sql: str = Field(
        description="A single read-only DuckDB SELECT (or WITH ... SELECT) query."
    )


def _jsonify(value: Any) -> Any:
    """Coerce DuckDB scalar types into JSON-serializable values."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def make_query_ledger_tool(
    con: duckdb.DuckDBPyConnection, max_rows: int, *, wrap: ToolWrap | None = None
) -> StructuredTool:
    """Build the `query_ledger` tool bound to a connection and a row cap.

    ``wrap`` optionally decorates the callable with the turn tracker (dedup +
    observation recording, ADR-014); ``None`` leaves the tool bare (tests).
    """

    def query_ledger(sql: str) -> str:
        try:
            guarded = guard_query(sql, max_rows=max_rows)
        except GuardrailError as exc:
            return json.dumps({"error": "rejected_by_guardrail", "detail": str(exc)})
        try:
            columns, rows = run_query(con, guarded)
        except duckdb.Error as exc:
            # The model is about to read this and try again, so it must not
            # contain line numbers into the guard's wrapper — text it never
            # wrote. The diagnosis itself is passed through untouched.
            detail = strip_wrapper_line_echo(str(exc))
            return json.dumps({"error": "sql_error", "detail": detail})

        records = [
            {col: _jsonify(val) for col, val in zip(columns, row, strict=False)} for row in rows
        ]
        return json.dumps(
            {
                "columns": columns,
                "row_count": len(records),
                "truncated": len(records) >= max_rows,
                "rows": records,
            },
            default=str,
        )

    func = wrap("query_ledger", query_ledger) if wrap else query_ledger
    return StructuredTool.from_function(
        func=func,
        name="query_ledger",
        description=_TOOL_DESCRIPTION,
        args_schema=QueryLedgerInput,
    )


_SEARCH_DESCRIPTION = (
    "Search the company collections policy and return the most relevant rules as "
    "JSON, each with its section heading so you can cite it. Use this for any "
    'question about *policy* — what the rule, threshold, cadence or process is '
    "(e.g. dunning timing, credit holds, payment plans, write-off thresholds, "
    "prioritisation). It does not see the ledger data; pair it with `query_ledger` "
    "when an answer needs both a number and the rule that governs it."
)


class SearchPolicyInput(BaseModel):
    query: str = Field(
        description="A natural-language question about the collections policy."
    )


def make_search_policy_tool(
    collection: Collection, k: int, *, wrap: ToolWrap | None = None
) -> StructuredTool:
    """Build the `search_policy` tool bound to a policy collection and a top-k.

    ``wrap`` optionally decorates the callable with the turn tracker (ADR-014).
    """

    def search_policy(query: str) -> str:
        result = collection.query(query_texts=[query], n_results=k)
        raw_docs, raw_metas = result["documents"], result["metadatas"]
        if raw_docs is None or raw_metas is None:
            return "Policy search returned no documents."
        ids = result["ids"][0]
        documents = raw_docs[0]
        metadatas = raw_metas[0]
        raw_distances = result.get("distances")
        distances: list[float | None] = (
            list(raw_distances[0]) if raw_distances else [None] * len(ids)
        )

        results = [
            {
                "heading": (meta or {}).get("heading"),
                "source": (meta or {}).get("source"),
                "text": document,
                "distance": distance,
            }
            for document, meta, distance in zip(documents, metadatas, distances, strict=False)
        ]
        return json.dumps({"query": query, "result_count": len(results), "results": results})

    func = wrap("search_policy", search_policy) if wrap else search_policy
    return StructuredTool.from_function(
        func=func,
        name="search_policy",
        description=_SEARCH_DESCRIPTION,
        args_schema=SearchPolicyInput,
    )

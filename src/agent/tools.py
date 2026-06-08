"""The agent's tools. Phase 2 ships the guarded text-to-SQL tool.

``query_ledger`` is the only way the agent touches data. It runs every query
through ``sql_guard.guard_query`` (allow/deny + row cap) and then a read-only
connection (``ledger``). Guardrail rejections and SQL errors are returned to the
model as text, not raised, so a ReAct loop can read the reason and try again.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import duckdb
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.agent.ledger import run_query
from src.agent.schema_hints import SCHEMA_HINTS
from src.agent.sql_guard import GuardrailError, guard_query

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
    con: duckdb.DuckDBPyConnection, max_rows: int
) -> StructuredTool:
    """Build the `query_ledger` tool bound to a connection and a row cap."""

    def query_ledger(sql: str) -> str:
        try:
            guarded = guard_query(sql, max_rows=max_rows)
        except GuardrailError as exc:
            return json.dumps({"error": "rejected_by_guardrail", "detail": str(exc)})
        try:
            columns, rows = run_query(con, guarded)
        except duckdb.Error as exc:
            return json.dumps({"error": "sql_error", "detail": str(exc)})

        records = [
            {col: _jsonify(val) for col, val in zip(columns, row)} for row in rows
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

    return StructuredTool.from_function(
        func=query_ledger,
        name="query_ledger",
        description=_TOOL_DESCRIPTION,
        args_schema=QueryLedgerInput,
    )

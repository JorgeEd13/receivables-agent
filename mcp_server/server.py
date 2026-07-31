"""An MCP server exposing the receivables ledger as a tool.

Model Context Protocol (MCP) lets any MCP-aware client — Claude Code, Claude
Desktop, other agents — call the same guarded ledger query this project's own
agent uses. The point of this layer: the ledger becomes a reusable, governed
tool, not something locked inside one app.

Security parity (ADR-008): this server runs the **exact same guardrail** as the
in-app tool — a read-only DuckDB connection (`connect_readonly`) plus the
prompt-layer `guard_query` (SELECT/WITH only, allow/deny lists, single statement,
row cap). A different surface must not be a weaker surface.

The package is `mcp_server`, not `mcp`, so it doesn't shadow the MCP SDK's own
top-level `mcp` package on the path (ADR-008).

Run it over stdio:
    python -m mcp_server.server
Register it with Claude Code (see `mcp_server/README.md`).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import duckdb
from mcp.server.fastmcp import FastMCP

from src.agent.ledger import connect_readonly, run_query
from src.agent.schema_hints import SCHEMA_HINTS
from src.agent.sql_guard import GuardrailError, guard_query, strip_wrapper_line_echo
from src.core.config import Settings, get_settings


def _jsonify(value: Any) -> Any:
    """Coerce DuckDB scalar types into JSON-serializable values."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def run_guarded_query(
    con: duckdb.DuckDBPyConnection, sql: str, max_rows: int
) -> str:
    """Guard → read-only execute → JSON. The reusable core of the MCP tool.

    Guardrail rejections and SQL errors are returned as JSON (not raised), so an
    MCP client gets a readable reason and can self-correct — same contract as the
    in-app `query_ledger` tool.
    """
    try:
        guarded = guard_query(sql, max_rows=max_rows)
    except GuardrailError as exc:
        return json.dumps({"error": "rejected_by_guardrail", "detail": str(exc)})
    try:
        columns, rows = run_query(con, guarded)
    except duckdb.Error as exc:
        # Same contract as the in-app tool: an MCP client self-correcting from
        # this message must not be handed line numbers into the guard's wrapper.
        return json.dumps(
            {"error": "sql_error", "detail": strip_wrapper_line_echo(str(exc))}
        )

    records = [{col: _jsonify(val) for col, val in zip(columns, row, strict=False)} for row in rows]
    return json.dumps(
        {
            "columns": columns,
            "row_count": len(records),
            "truncated": len(records) >= max_rows,
            "rows": records,
        },
        default=str,
    )


def create_server(settings: Settings | None = None) -> FastMCP:
    """Build the MCP server bound to a read-only ledger connection."""
    settings = settings or get_settings()
    con = connect_readonly(settings.ledger_path)
    server = FastMCP("receivables-ledger")

    @server.tool()
    def query_ledger(sql: str) -> str:
        """Run a single read-only DuckDB SELECT against the receivables ledger.

        Only SELECT/WITH over the allow-listed relations is permitted; writes,
        multiple statements and unknown tables are rejected, and rows are capped.
        Returns the rows as JSON (or a JSON error with a reason).
        """
        return run_guarded_query(con, sql, settings.max_rows)

    @server.resource("schema://ledger")
    def ledger_schema() -> str:
        """The ledger schema (views, tables, columns) to write SQL against."""
        return SCHEMA_HINTS

    return server


def main() -> None:
    create_server().run()  # stdio transport


if __name__ == "__main__":
    main()

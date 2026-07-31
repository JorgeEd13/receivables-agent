"""What a failed query is allowed to tell the model — at both tool surfaces.

`strip_wrapper_line_echo` is tested against DuckDB's real messages in
`test_sql_guard.py` (ROUND 7). This file tests something else, and the
difference is the whole point: that the two callers actually *call* it.

The guard's wrapper is applied by `guard_query`, but the leak it caused came out
through `tools.py` and `mcp_server/server.py`, which catch `duckdb.Error` and
hand `str(exc)` to the model. A test that only exercises the strip function
stays green while both call sites go back to `str(exc)` — so these drive the
tool and the MCP entry point end to end, and assert on the JSON the caller
receives.

No ledger: both surfaces take a connection, so an in-memory table with the same
column types answers them. This runs in CI, where the ledger is not built and
`test_ledger.py` / the ledger-gated MCP tests do not run at all.
"""

from __future__ import annotations

import json
import re

import duckdb
import pytest

from src.agent.tools import make_query_ledger_tool

ECHO_LINE = re.compile(r"^LINE \d+: ", re.MULTILINE)

# A query that passes the guard (allow-listed relation, allow-listed functions)
# and then fails in the binder — the only shape that reaches the wrapper echo.
BAD_COLUMN = "SELECT amont FROM invoices"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE invoices ("
        "invoice_id BIGINT, customer_id INTEGER, issue_date DATE, due_date DATE, "
        "amount DOUBLE, currency VARCHAR, status VARCHAR)"
    )
    yield c
    c.close()


def _assert_actionable_sql_error(payload: dict) -> None:
    """The contract both surfaces owe the model: a reason, minus our coordinates."""
    assert payload["error"] == "sql_error"
    detail = payload["detail"]
    # Nothing about the wrapper the caller never wrote...
    assert not ECHO_LINE.search(detail), detail
    assert "_guarded" not in detail, detail
    assert "LIMIT 50" not in detail, detail
    # ...and everything about what is wrong with the query it did write.
    assert "amont" in detail, detail
    assert "Candidate bindings" in detail, detail


def test_in_app_tool_returns_an_error_the_model_can_act_on(con) -> None:
    tool = make_query_ledger_tool(con, max_rows=50)
    _assert_actionable_sql_error(json.loads(tool.invoke({"sql": BAD_COLUMN})))


def test_mcp_surface_owes_the_same_contract(con) -> None:
    """ADR-008: a different surface must not be a weaker surface. It applies to
    what comes back from a failure, not only to what gets refused."""
    pytest.importorskip(
        "mcp.server.fastmcp",
        reason="needs the 'mcp' extra with FastMCP (mcp.server.fastmcp)",
    )
    from mcp_server.server import run_guarded_query

    _assert_actionable_sql_error(json.loads(run_guarded_query(con, BAD_COLUMN, 50)))


def test_a_guardrail_refusal_is_still_reported_as_a_refusal(con) -> None:
    """Pins the branch next door. The two error paths return different `error`
    values on purpose — `rejected_by_guardrail` means the query never ran, and a
    strip applied to the wrong one would erase that distinction."""
    tool = make_query_ledger_tool(con, max_rows=50)
    payload = json.loads(tool.invoke({"sql": "SELECT * FROM secret_table"}))
    assert payload["error"] == "rejected_by_guardrail"
    assert "allow-list" in payload["detail"]

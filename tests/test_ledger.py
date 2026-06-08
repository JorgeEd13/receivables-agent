"""Integration checks against the real ledger (skipped if it isn't built).

These prove the *other* half of the guardrail — the read-only connection — and
that guarded queries actually run. Build the ledger with
``python data/generate.py`` to enable them.
"""

from __future__ import annotations

import json
import os

import duckdb
import pytest

from src.agent.ledger import connect_readonly, run_query
from src.agent.sql_guard import guard_query
from src.agent.tools import make_query_ledger_tool

LEDGER_PATH = os.environ.get("LEDGER_PATH", os.path.join("data", "ledger.duckdb"))
pytestmark = pytest.mark.skipif(
    not os.path.exists(LEDGER_PATH), reason="ledger not built (run data/generate.py)"
)


@pytest.fixture(scope="module")
def con():
    c = connect_readonly(LEDGER_PATH)
    yield c
    c.close()


def test_guarded_select_runs(con) -> None:
    cols, rows = run_query(con, guard_query("SELECT count(*) AS n FROM invoices"))
    assert cols == ["n"]
    assert rows[0][0] > 0


def test_connection_is_read_only(con) -> None:
    # The connection itself must reject any write, independent of the guard.
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE evil AS SELECT 1")
    with pytest.raises(duckdb.Error):
        con.execute("DELETE FROM invoices")


def test_tool_returns_json_and_caps_rows(con) -> None:
    tool = make_query_ledger_tool(con, max_rows=3)
    payload = json.loads(tool.invoke({"sql": "SELECT customer_id FROM customers"}))
    assert payload["row_count"] <= 3
    assert payload["truncated"] is True
    assert payload["columns"] == ["customer_id"]


def test_tool_reports_guardrail_rejection(con) -> None:
    tool = make_query_ledger_tool(con, max_rows=10)
    payload = json.loads(tool.invoke({"sql": "DROP TABLE customers"}))
    assert payload["error"] == "rejected_by_guardrail"


def test_tool_reports_sql_error(con) -> None:
    tool = make_query_ledger_tool(con, max_rows=10)
    payload = json.loads(tool.invoke({"sql": "SELECT no_such_col FROM invoices"}))
    assert payload["error"] == "sql_error"

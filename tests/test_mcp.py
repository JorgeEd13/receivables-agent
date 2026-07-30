"""MCP server tests — the guarded query core, offline.

The MCP surface must enforce the *same* guardrail as the in-app tool (ADR-008).
We test `run_guarded_query` directly: it returns JSON rows for a legal SELECT,
and JSON error objects (never raises, never executes) for guardrail violations.
Tests that need the ledger skip if it hasn't been generated.
"""

from __future__ import annotations

import json
import os

import duckdb
import pytest

# The MCP server lives behind the `mcp` extra. Without this, a clone that did not
# install it fails at COLLECTION — the whole suite errors out rather than
# reporting the tests it could run. CI installs `.[dev,mcp]` so these do execute.
# Skip on the EXACT module the server imports, not the top-level package:
# `mcp` can import fine while `mcp.server.fastmcp` is absent in some releases,
# and then the guard passes and collection dies anyway.
pytest.importorskip(
    "mcp.server.fastmcp",
    reason="needs the 'mcp' extra with FastMCP (mcp.server.fastmcp)",
)
import pytest
from mcp_server.server import run_guarded_query

from src.core.config import get_settings

LEDGER = get_settings().ledger_path
needs_ledger = pytest.mark.skipif(
    not os.path.exists(LEDGER), reason="ledger not generated (run data/generate.py)"
)


@pytest.fixture
def con():
    c = duckdb.connect(LEDGER, read_only=True)
    yield c
    c.close()


@needs_ledger
def test_legal_select_returns_rows(con):
    out = json.loads(run_guarded_query(con, "SELECT count(*) AS n FROM customers", 200))
    assert out["columns"] == ["n"]
    assert out["row_count"] == 1
    assert out["rows"][0]["n"] == 13000


@needs_ledger
def test_row_cap_truncates(con):
    out = json.loads(run_guarded_query(con, "SELECT customer_id FROM customers", 5))
    assert out["row_count"] == 5
    assert out["truncated"] is True


@needs_ledger
def test_write_is_rejected_by_guardrail(con):
    out = json.loads(run_guarded_query(con, "DROP TABLE customers", 200))
    assert out["error"] == "rejected_by_guardrail"
    # The table is still there — nothing executed.
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 13000


@needs_ledger
def test_unknown_relation_is_rejected(con):
    out = json.loads(run_guarded_query(con, "SELECT * FROM secrets", 200))
    assert out["error"] == "rejected_by_guardrail"


def test_guardrail_rejection_needs_no_connection():
    # A multi-statement attack is rejected before any execution, so a dummy
    # connection is never touched.
    out = json.loads(run_guarded_query(None, "SELECT 1; DROP TABLE customers", 200))
    assert out["error"] == "rejected_by_guardrail"

"""Guardrail tests — the priority suite. Pure, offline, no LLM, no data.

The guard parses through DuckDB itself (ADR-022), so these do touch the DuckDB
library — but only its parser, on an empty in-memory connection. No ledger, no
network, no model.

Covers the two failure modes that matter for a text-to-SQL agent:
  1. injection / escape attempts must be *rejected*;
  2. legitimate analytical queries must *pass* (no over-blocking).
"""

from __future__ import annotations

import re

import pytest

from src.agent.sql_guard import (
    DEFAULT_MAX_ROWS,
    GuardrailError,
    guard_query,
)

# --------------------------------------------------------------------------- #
# Attacks and policy violations — must raise GuardrailError.
# --------------------------------------------------------------------------- #

REJECTED = [
    # empty / non-read
    ("", "empty"),
    ("   \n  ", "whitespace only"),
    ("DROP TABLE customers", "ddl drop"),
    ("CREATE TABLE t AS SELECT 1", "ddl create"),
    ("CREATE OR REPLACE VIEW v_invoices AS SELECT 1", "create or replace"),
    ("ALTER TABLE customers ADD COLUMN x INT", "ddl alter"),
    ("TRUNCATE invoices", "ddl truncate"),
    ("INSERT INTO customers VALUES (1)", "dml insert"),
    ("UPDATE customers SET name = 'x'", "dml update"),
    ("DELETE FROM invoices", "dml delete"),
    ("MERGE INTO invoices USING customers ON true", "dml merge"),
    # stacked statements
    ("SELECT 1; DROP TABLE customers", "stacked drop"),
    ("SELECT * FROM customers; SELECT * FROM invoices", "stacked select"),
    # stacking hidden behind a comment
    ("SELECT 1 /* x */ ; DROP TABLE customers", "comment then stack"),
    # catalog / session / filesystem
    ("PRAGMA database_list", "pragma"),
    ("SET threads TO 4", "set"),
    ("ATTACH 'evil.db' AS e", "attach"),
    ("COPY invoices TO 'out.csv'", "copy out"),
    ("INSTALL httpfs", "install"),
    ("LOAD httpfs", "load"),
    ("SELECT * FROM read_csv('/etc/passwd')", "read_csv exfil"),
    ("SELECT * FROM read_parquet('s3://x/y')", "read_parquet exfil"),
    ("SELECT * FROM glob('/**')", "glob"),
    ("CALL pragma_version()", "call"),
    # relation not in allow-list
    ("SELECT * FROM secret_table", "unknown relation"),
    ("SELECT * FROM duckdb_settings()", "duckdb internal fn"),
    ("SELECT * FROM customers JOIN pg_tables ON true", "unknown join target"),
    ("SELECT * FROM invoices, sqlite_master", "unknown comma join"),
    # escape attempts that should NOT start a read query after cleaning
    ("SE-- comment\nLECT 1", "comment splits keyword"),
    ("WITH x AS (SELECT 1) DELETE FROM x", "cte then delete"),
]


@pytest.mark.parametrize("sql,label", REJECTED, ids=[r[1] for r in REJECTED])
def test_rejected(sql: str, label: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


# --------------------------------------------------------------------------- #
# Legitimate analytical queries — must pass and come back row-capped.
# --------------------------------------------------------------------------- #

ALLOWED = [
    ("SELECT 1", "constant"),
    ("select * from invoices", "lowercase select star"),
    ("SELECT count(*) FROM v_invoices WHERE aging_bucket = '90+'", "view filter"),
    (
        "SELECT name, overdue_amount FROM v_customer_ar "
        "ORDER BY overdue_amount DESC",
        "top customers",
    ),
    (
        "WITH overdue AS (SELECT customer_id, amount FROM v_invoices "
        "WHERE status = 'overdue') "
        "SELECT customer_id, sum(amount) FROM overdue GROUP BY customer_id",
        "cte aggregation",
    ),
    (
        "SELECT c.name, i.amount FROM customers c "
        "JOIN v_invoices i ON c.customer_id = i.customer_id",
        "join allow-listed",
    ),
    ("SELECT * FROM customers, payment_profiles", "comma join allow-listed"),
    ("SELECT dso_days FROM v_dso", "dso view"),
    # the word 'create'/'drop' inside a STRING LITERAL is data, not a command
    ("SELECT * FROM customers WHERE name = 'DROP TABLE x'", "deny word in literal"),
    ("SELECT * FROM customers WHERE name = 'ACME; DELETE'", "semicolon in literal"),
    # deny words as substrings of legit identifiers must not trip \b matching
    ("SELECT onboarded_at, credit_limit FROM customers", "substring identifiers"),
    # trailing semicolon(s) on a single statement are fine (no real 2nd stmt)
    ("SELECT 1;", "trailing semicolon"),
    ("SELECT 1; ; ", "trailing empty statements"),
    # a benign comment is stripped, query still valid
    ("SELECT 1 -- DROP TABLE customers\n", "deny word in trailing comment"),
    # a semicolon FOLLOWED by a comment — the combination a model writes and
    # the guard used to refuse, because the separator travelled into the
    # wrapper. Comments never reach the syntax tree, so they cannot come back
    # out of it (ADR-022).
    ("SELECT count(*) FROM invoices; -- total", "semicolon then comment"),
    ("SELECT count(*) FROM invoices; /* total */", "semicolon then block comment"),
    # operator spellings of allow-listed functions: `||` is `concat`, `^` is
    # `pow`. Refusing one spelling while allowing the other cost the product a
    # query and bought no safety.
    ("SELECT name || ' x' FROM customers", "concat as operator"),
    ("SELECT amount ^ 2 FROM invoices", "power as operator"),
]


@pytest.mark.parametrize("sql,label", ALLOWED, ids=[a[1] for a in ALLOWED])
def test_allowed(sql: str, label: str) -> None:
    out = guard_query(sql)
    assert re.search(rf"LIMIT {DEFAULT_MAX_ROWS}\s*$", out)
    assert out.lower().lstrip().startswith("select * from (")


def test_row_cap_is_applied() -> None:
    out = guard_query("SELECT * FROM invoices", max_rows=5)
    assert out.rstrip().endswith("LIMIT 5")


def test_max_rows_must_be_positive() -> None:
    with pytest.raises(GuardrailError):
        guard_query("SELECT 1", max_rows=0)


def test_cte_name_is_not_treated_as_unknown_relation() -> None:
    # `recent` is defined by the CTE, so it must not be flagged by the allow-list.
    sql = (
        "WITH recent AS (SELECT * FROM invoices WHERE issue_date > '2026-01-01') "
        "SELECT count(*) FROM recent"
    )
    assert "LIMIT" in guard_query(sql)


def test_separators_alone_are_not_a_statement() -> None:
    """A string of separators used to be refused by a length check on text that
    had been peeled by hand. The parser decides it now — `extract_statements`
    reports zero statements — so the message names the real reason."""
    for sql in [";", " ; ; ", ";\n;"]:
        with pytest.raises(GuardrailError, match=r"(?i)single statement"):
            guard_query(sql)


def test_error_message_names_the_violation() -> None:
    with pytest.raises(GuardrailError, match=r"(?i)allow-list"):
        guard_query("SELECT * FROM secret_table")
    with pytest.raises(GuardrailError, match=r"(?i)single statement"):
        guard_query("SELECT 1; SELECT 2")

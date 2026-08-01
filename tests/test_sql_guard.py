"""Guardrail tests — the priority suite. Pure, offline, no LLM, no ledger.

The guard parses through DuckDB itself (ADR-022), so these do touch the DuckDB
library — its parser for the guard proper, and (in the last section) its binder,
on a throwaway in-memory table. No ledger file, no network, no model.

Covers the two failure modes that matter for a text-to-SQL agent:
  1. injection / escape attempts must be *rejected*;
  2. legitimate analytical queries must *pass* (no over-blocking).

...and, since ROUND 7, a third that is neither: what a *failed execution* is
allowed to tell the model about the guard's own wrapper.
"""

from __future__ import annotations

import re

import duckdb
import pytest

from src.agent.sql_guard import (
    DEFAULT_MAX_ROWS,
    GuardrailError,
    guard_query,
    statement_identity,
    strip_wrapper_line_echo,
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


# --------------------------------------------------------------------------- #
# ROUND 7 — what an EXECUTION failure may say about the wrapper.
#
# The refusals above never quote the wrapper back at the caller: R1-C2 replaced
# the hand-repaired text with the parser's own print, and the last check's
# failure message speaks in the guard's words instead of DuckDB's. That rule
# stopped at the guard's edge. A query that PASSED the guard and then failed in
# the binder came back through `tools.py` / `mcp_server/server.py` carrying
#
#     LINE 2: SELECT amont FROM invoices
#                    ^
#
# `LINE 2` is the wrapper's numbering and the echoed text is DuckDB's print of
# the validated tree, so a model reading this to fix itself was handed a
# coordinate into a string it never wrote. Not a confidentiality problem — this
# repo is public and the wrapper is spelled out in `sql_guard.py`'s docstring.
# It is a self-correction problem, and it was found by R1-C2 while measuring the
# refusal path, not by the tests.
#
# What these pin: the echo goes, the diagnosis stays. The second half is the
# half that can rot silently — a strip that eats `Candidate bindings` passes any
# test that only asserts `"LINE" not in message`.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def binder_con():
    """A table the guard allows, with columns to get wrong. No ledger needed.

    Deliberately a real DuckDB binder rather than hand-written message strings:
    the format of that echo is DuckDB's, not ours, and a fixture that spells it
    out by hand would keep passing after an upgrade changed it.

    The column types mirror `data/generate.py`'s `invoices` exactly. That is not
    decoration — the first version of this fixture typed `amount` as DECIMAL,
    and `WHERE amount > 'x'` then bound cleanly instead of raising the
    Conversion Error it raises against the real ledger, where `amount` is
    DOUBLE. A fixture one type away from production silently drops a case.
    """
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE invoices ("
        "invoice_id BIGINT, customer_id INTEGER, issue_date DATE, due_date DATE, "
        "amount DOUBLE, currency VARCHAR, status VARCHAR)"
    )
    # And it has to hold a row. An empty table made `WHERE amount > 'x'` succeed:
    # that cast fails while *evaluating*, and with nothing to evaluate the query
    # returned an empty result instead of the Conversion Error. Two different
    # ways for an under-built fixture to lose the same case.
    con.execute(
        "INSERT INTO invoices VALUES "
        "(1, 7, DATE '2026-01-05', DATE '2026-02-04', 1250.00, 'BRL', 'overdue')"
    )
    yield con
    con.close()


def _failing_detail(con, sql: str) -> str:
    """Run a guarded query expected to fail in the binder; return what the model sees."""
    with pytest.raises(duckdb.Error) as exc:
        con.execute(guard_query(sql, max_rows=50))
    return strip_wrapper_line_echo(str(exc.value))


ECHO_LINE = re.compile(r"^LINE \d+: ", re.MULTILINE)


@pytest.mark.parametrize(
    "sql,keep",
    [
        ("SELECT amont FROM invoices", "Candidate bindings"),
        ("SELECT sum(status) FROM invoices", "Candidate functions"),
        ("SELECT CAST('abc' AS INTEGER)", "Conversion Error"),
        ("SELECT amount FROM invoices WHERE amount > 'x'", "Conversion Error"),
        # A string literal containing a real newline pushes the failure onto the
        # wrapper's LINE 3 — the number is not a constant, which is why the code
        # matches `LINE \d+` and not the `LINE 2` that every example shows.
        ("SELECT 'a\nb' AS x, amont FROM invoices", "Candidate bindings"),
    ],
)
def test_execution_error_keeps_the_diagnosis_and_drops_the_echo(binder_con, sql, keep) -> None:
    detail = _failing_detail(binder_con, sql)
    assert not ECHO_LINE.search(detail), detail
    assert "_guarded" not in detail, detail
    # The half that matters: the model must still be told what is actually wrong.
    assert keep in detail, detail
    assert detail.startswith(("Binder Error", "Conversion Error")), detail


def test_a_forged_echo_cannot_cost_the_model_its_diagnosis(binder_con) -> None:
    """A quoted identifier may contain a newline, so the caller can write a line
    that looks exactly like DuckDB's echo INTO the diagnosis:

        SELECT "a\\nLINE 9: injected" FROM invoices
          ->  Binder Error: Referenced column "a
              LINE 9: injected" not found in FROM clause!
              Candidate bindings: "invoice_id"

              LINE 2: SELECT "a
                             ^

    Cutting at the first `LINE n:` would take `Candidate bindings` with it —
    the caller would be able to blank its own error message. The cut is the
    last one, and only inside the final two lines.
    """
    detail = _failing_detail(binder_con, 'SELECT "a\nLINE 9: injected" FROM invoices')
    assert "Candidate bindings" in detail, detail
    assert "LINE 9: injected" in detail, detail  # kept: it is data, not an echo
    assert "LINE 2:" not in detail, detail  # dropped: it is the wrapper's


def test_an_error_without_an_echo_is_untouched(binder_con) -> None:
    """Six of the twenty failures measured carry no echo at all. Passing those
    through unchanged is the branch that stops the strip from being a rewriter.
    """
    with pytest.raises(duckdb.Error) as exc:
        binder_con.execute(guard_query("SELECT count(*) FROM invoices GROUP BY 9", max_rows=50))
    raw = str(exc.value)
    assert not ECHO_LINE.search(raw)  # the premise of this test, not an assumption
    assert strip_wrapper_line_echo(raw) == raw


def test_an_echo_head_without_its_caret_is_not_an_echo() -> None:
    """The reason the strip needs BOTH lines, written as a test because the
    first version of it needed only the head and this case caught it.

    A caller who lands an echo-shaped line at the second-to-last position would
    otherwise truncate the message there — deleting its own diagnosis on
    purpose, or the next reader's by accident. Requiring the caret underneath
    closes it: DuckDB's caret line is bare, and injected text is followed by the
    rest of DuckDB's sentence.
    """
    forged = (
        'Binder Error: Referenced column "x\n'
        'LINE 4: y" not found!\n'
        'Candidate bindings: "amount"'
    )
    assert strip_wrapper_line_echo(forged) == forged


def test_the_shape_recognised_is_duckdbs_and_not_merely_echo_ish() -> None:
    """Both halves of the pattern are pinned to what DuckDB actually emits,
    because loosening either one passes every other test in this file.

    Measured: widening the caret to `^ *\\^.*$`, or the head to `^LINE `, leaves
    all 335 green. A strip that fires on anything echo-shaped is a strip whose
    real boundary nobody knows — and this one runs on text a caller can steer.
    """
    tail = "Binder Error: something went wrong\nCandidate bindings: \"amount\"\n{head}\n{caret}"
    # A caret line is bare. DuckDB puts nothing after it.
    assert strip_wrapper_line_echo(
        tail.format(head="LINE 2: SELECT amont FROM invoices", caret='       ^" not found!')
    ) == tail.format(head="LINE 2: SELECT amont FROM invoices", caret='       ^" not found!')
    # A head carries a line NUMBER and a colon.
    assert strip_wrapper_line_echo(
        tail.format(head="LINE up next: whatever", caret="       ^")
    ) == tail.format(head="LINE up next: whatever", caret="       ^")


def test_anything_the_guard_runs_can_be_identified() -> None:
    """If the guard accepts a statement, `statement_identity` must have an opinion.

    The two entry points parse the *same* text (`_statement_text`), and this is
    what says so. It matters because a caller that gets `None` falls back to a
    text key, and the text key is the thing the tree identity exists to replace:
    measured 2026-08-01, a single leading `\x0b` was enough — the guard stripped it
    and ran the query, while the identity refused to parse it, so two statements
    differing inside a string literal collapsed into one memo entry again.

    The padding is *derived* from Python's own definition of the characters
    `str.strip()` removes, not hand-picked. A list would prove only that the
    listed characters were thought of; the property is that there is no character
    one side strips and the other does not.
    """
    base = "SELECT name FROM customers WHERE name = 'John  Doe'"
    identity = statement_identity(base)
    assert identity is not None

    stripped = [chr(code) for code in range(0x110000) if chr(code).isspace()]
    assert len(stripped) > 20, "the surface, not a hand-picked list"

    for char in stripped:
        padded = char + base + char
        guard_query(padded)  # accepted → this statement will be executed
        assert statement_identity(padded) == identity, repr(char)

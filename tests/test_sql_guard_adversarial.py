"""Adversarial guardrail suite — written to BREAK the guarantee, not confirm it.

Threat model: the SQL string is fully attacker-controlled (an LLM composes it,
and a prompt-injected LLM composes what the attacker wants) and the attacker has
read ``src/agent/sql_guard.py``. The guarantee under attack:

  1. nothing may be created / altered / deleted;
  2. only ``ALLOWED_RELATIONS`` inside the ledger file may be read;
  3. nothing outside the ledger file may be reached at all — including DuckDB's
     own catalog, settings and metadata;
  4. legitimate analytical queries must not be refused.

Two kinds of test live here:

* ``guard_query`` as a pure function (fast, no DuckDB);
* end-to-end, through ``connect_readonly()`` against a throwaway ledger built in
  ``tmp_path`` — because a string the guard accepts is only a *finding* when the
  engine executes it and it reads something forbidden.

Everything is offline and takes well under a second. No LLM.

HOW TO READ THE SECTION HEADERS. Each numbered block below states the defect
**as it was found in that round**, in the implementation that existed then — it
is a record of the break, not a description of today's guard. What is true today
is what the assertions say: everything here is green against the current
implementation. Where a round closed an earlier round's findings, a banner says
so explicitly; the newest block never carries one, because nothing has come
after it yet to close it.

ROUND 3 IS CLOSED. `_collect` now walks each CTE definition with only the CTEs
declared *before* it in scope, so the forward-reference and self-reference
payloads in R3-1 are refused. The R3 section header describes the walk as it was
when those payloads were written.

ROUND 4 did not come from an attack round. It came from an audit that read only
`sql_guard.py` and this file, with no access to the design notes: two readers,
independently, asked the same question — the tree carries `catalog_name`, and
nothing ever reads it. R4 is that finding.
"""

from __future__ import annotations

import contextlib

import duckdb
import pytest

from src.agent.ledger import connect_readonly
from src.agent.sql_guard import GuardrailError, guard_query

# A row value that exists in the throwaway ledger but in a relation that is NOT
# on the allow-list. If this string ever comes back from a guarded query, the
# allow-list has been bypassed.
SECRET_MARKER = "NOT-ON-THE-ALLOW-LIST"


# --------------------------------------------------------------------------- #
# Throwaway ledger + helpers
# --------------------------------------------------------------------------- #


def _build_ledger(path: str) -> None:
    """Create a minimal ledger with every allow-listed relation, plus one that
    is deliberately *not* allow-listed."""
    con = duckdb.connect(path)
    con.execute("CREATE TABLE customers(customer_id INT, name VARCHAR)")
    con.execute("INSERT INTO customers VALUES (1, 'ACME'), (2, 'Globex')")
    con.execute(
        "CREATE TABLE invoices(invoice_id INT, customer_id INT, amount DOUBLE, "
        "status VARCHAR, issue_date DATE, due_date DATE)"
    )
    con.execute(
        "INSERT INTO invoices VALUES "
        "(1, 1, 100.0, 'overdue', DATE '2025-12-01', DATE '2026-01-01'), "
        "(2, 2,  50.0, 'paid',    DATE '2026-01-01', DATE '2026-02-01')"
    )
    con.execute("CREATE TABLE payments(payment_id INT, invoice_id INT, amount DOUBLE)")
    con.execute("CREATE TABLE communications(comm_id INT, invoice_id INT)")
    con.execute("CREATE TABLE payment_profiles(profile VARCHAR)")
    con.execute("CREATE TABLE meta(k VARCHAR, v VARCHAR)")
    con.execute("CREATE VIEW v_invoices AS SELECT * FROM invoices")
    con.execute("CREATE VIEW v_customer_ar AS SELECT * FROM customers")
    con.execute("CREATE VIEW v_dso AS SELECT 30 AS dso_days")
    # Off-limits by policy: present in the ledger file, absent from ALLOWED_RELATIONS.
    con.execute("CREATE TABLE internal_notes(secret VARCHAR)")
    con.execute(f"INSERT INTO internal_notes VALUES ('{SECRET_MARKER}')")
    con.close()


@pytest.fixture(scope="module")
def ledger_path(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("ledger") / "ledger.duckdb")
    _build_ledger(path)
    return path


@pytest.fixture
def con(ledger_path):
    """A *fresh* hardened connection per test (temp objects must not leak)."""
    c = connect_readonly(ledger_path)
    yield c
    c.close()


def run_guarded(con, sql: str) -> list[tuple]:
    """Guard `sql` and execute the result. Raises GuardrailError if refused."""
    return con.execute(guard_query(sql)).fetchall()


def assert_blocked(con, sql: str, forbidden: str | None = None) -> None:
    """The end-to-end guarantee: this query must not succeed in reading.

    Passes if the guard refuses it *or* the hardened engine refuses it. Fails if
    it runs to completion (and, when `forbidden` is given, doubly fails if that
    marker appears in the result).
    """
    try:
        guarded = guard_query(sql)
    except GuardrailError:
        return
    try:
        rows = con.execute(guarded).fetchall()
    except duckdb.Error:
        return
    if forbidden is not None:
        leaked = any(forbidden in str(cell) for row in rows for cell in row)
        assert not leaked, f"LEAKED {forbidden!r} via: {sql}\nrows={rows[:3]}"
    raise AssertionError(f"query was accepted AND executed: {sql}\nrows={rows[:3]}")


# --------------------------------------------------------------------------- #
# 1. Parser/executor disagreement: the guard checks a MASKED copy, DuckDB
#    executes the ORIGINAL. Any construct DuckDB treats as a string but the
#    masker does not (or vice versa) desynchronises quote parity and blanks the
#    rest of the statement out of every check.
# --------------------------------------------------------------------------- #

# `$a$'$a$` is a dollar-quoted string whose *content* is a single quote. DuckDB
# reads one string constant; `_mask_literals` sees a bare `'`, opens a literal
# that never closes, and masks the whole remainder of the query to `''`.
DOLLAR_QUOTE_DESYNC = (
    "SELECT $a$'$a$ AS z, (SELECT count(*) FROM duckdb_settings()) AS leak "
    "FROM invoices"
)

# Same desync with no `$` anywhere: `_mask_literals` has no notion of
# double-quoted identifiers, so an apostrophe inside one opens a literal.
DQUOTE_IDENT_DESYNC = (
    "SELECT 1 AS \"a'b\", (SELECT count(*) FROM duckdb_settings()) AS leak "
    "FROM invoices"
)

# Third desync source: DuckDB's `e'...'` escape strings honour backslash
# escapes, the scanner does not, so `e'\''` shifts quote parity by one.
ESTRING_DESYNC = (
    "SELECT e'\\'' AS z, (SELECT count(*) FROM duckdb_settings()) AS leak "
    "FROM invoices"
)


@pytest.mark.parametrize(
    "sql",
    [DOLLAR_QUOTE_DESYNC, DQUOTE_IDENT_DESYNC, ESTRING_DESYNC],
    ids=["dollar_quoted_string", "double_quoted_identifier", "escape_string"],
)
def test_literal_masking_cannot_be_desynchronised(sql: str) -> None:
    """The masked copy must not hide SQL that DuckDB will actually execute."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_desync_does_not_reach_duckdb_catalog(con) -> None:
    assert_blocked(con, DOLLAR_QUOTE_DESYNC)


def test_desync_does_not_reach_duckdb_catalog_without_dollar_quoting(con) -> None:
    assert_blocked(con, DQUOTE_IDENT_DESYNC)


def test_desync_does_not_leak_the_ledger_file_path(con) -> None:
    # duckdb_databases() returns the absolute on-disk path of the ledger.
    assert_blocked(
        con,
        "SELECT $a$'$a$ AS z, (SELECT max(path) FROM duckdb_databases()) AS leak "
        "FROM invoices",
    )


def test_desync_does_not_read_a_non_allowlisted_relation(con) -> None:
    assert_blocked(
        con,
        "SELECT $a$'$a$ AS z, (SELECT max(secret) FROM internal_notes) AS leak "
        "FROM invoices",
        forbidden=SECRET_MARKER,
    )


# --------------------------------------------------------------------------- #
# 2. The desync also hides `;` from the statement splitter, so the guard's
#    single-statement rule and its `SELECT * FROM (...) LIMIT n` wrapper can
#    both be escaped: the wrapper's closing `)` is closed early and arbitrary
#    statements are appended.
# --------------------------------------------------------------------------- #

WRAPPER_ESCAPE = (
    "SELECT $a$'$a$ AS z FROM invoices) AS q; "
    "CREATE TEMP TABLE pwned AS SELECT 1 AS v; "
    "SELECT * FROM (SELECT 1"
)


def test_wrapper_cannot_be_escaped_by_hidden_semicolons() -> None:
    with pytest.raises(GuardrailError):
        guard_query(WRAPPER_ESCAPE)


def test_nothing_can_be_created_end_to_end(con) -> None:
    """Guarantee 1: nothing may be created. Temp objects are still objects, and
    a read-only DuckDB connection happily creates them."""
    with contextlib.suppress(GuardrailError, duckdb.Error):
        con.execute(guard_query(WRAPPER_ESCAPE))
    with pytest.raises(duckdb.Error):
        con.execute("SELECT * FROM pwned")


def test_prepared_statements_cannot_be_created(con) -> None:
    sql = (
        "SELECT $a$'$a$ AS z FROM invoices) AS q; "
        "PREPARE evil AS SELECT 1; "
        "SELECT * FROM (SELECT 1"
    )
    with contextlib.suppress(GuardrailError, duckdb.Error):
        con.execute(guard_query(sql))
    with pytest.raises(duckdb.Error):
        con.execute("EXECUTE evil")


# --------------------------------------------------------------------------- #
# 3. Name shadowing: a CTE name is exempted from BOTH the relation allow-list
#    and the function allow-list. Declaring a throwaway CTE named after a table
#    function therefore whitelists that function — and DuckDB resolves `name(`
#    to the function regardless of the CTE.
# --------------------------------------------------------------------------- #

CTE_SHADOW_SETTINGS = (
    "WITH duckdb_settings AS (SELECT 1) "
    "SELECT name, value FROM duckdb_settings()"
)
CTE_SHADOW_TABLES = (
    "WITH duckdb_tables AS (SELECT 1) SELECT table_name, sql FROM duckdb_tables()"
)
CTE_SHADOW_VERSION = (
    "WITH pragma_version AS (SELECT 1) SELECT * FROM pragma_version()"
)


@pytest.mark.parametrize(
    "sql",
    [CTE_SHADOW_SETTINGS, CTE_SHADOW_TABLES, CTE_SHADOW_VERSION],
    ids=["duckdb_settings", "duckdb_tables", "pragma_version"],
)
def test_cte_name_does_not_whitelist_a_table_function(sql: str) -> None:
    """A locally defined CTE name must never satisfy the FUNCTION allow-list."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_cte_shadow_does_not_reach_duckdb_settings(con) -> None:
    assert_blocked(con, CTE_SHADOW_SETTINGS)


def test_cte_shadow_does_not_reach_the_duckdb_catalog(con) -> None:
    assert_blocked(con, CTE_SHADOW_TABLES, forbidden="internal_notes")


def test_cte_shadow_plus_schema_qualification_reads_forbidden_table(con) -> None:
    """The CTE exemption is matched on the *last* dotted component, but DuckDB
    resolves a schema-qualified name to the real table, not the CTE."""
    assert_blocked(
        con,
        "WITH internal_notes AS (SELECT 1) SELECT * FROM main.internal_notes",
        forbidden=SECRET_MARKER,
    )


# --------------------------------------------------------------------------- #
# 4. Identifier quoting: `_FUNC_CALL_RE` requires the name to sit immediately
#    before `(`. A quoted function name puts a `"` in between, so the function
#    allow-list never sees the call at all.
# --------------------------------------------------------------------------- #

QUOTED_FN_SETTING = (
    "SELECT \"current_setting\"('memory_limit') AS leak FROM invoices"
)
QUOTED_FN_TEMPDIR = (
    "SELECT \"current_setting\"('temp_directory') AS leak FROM invoices"
)
QUOTED_FN_DATABASE = "SELECT \"current_database\"() AS leak FROM invoices"


@pytest.mark.parametrize(
    "sql",
    [QUOTED_FN_SETTING, QUOTED_FN_TEMPDIR, QUOTED_FN_DATABASE],
    ids=["current_setting", "current_setting_tempdir", "current_database"],
)
def test_quoted_function_name_is_still_allow_listed(sql: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_quoted_function_name_does_not_read_duckdb_settings(con) -> None:
    assert_blocked(con, QUOTED_FN_SETTING)


def test_quoted_function_name_does_not_leak_the_temp_directory(con) -> None:
    assert_blocked(con, QUOTED_FN_TEMPDIR)


# --------------------------------------------------------------------------- #
# 5. Guard-layer-only defect: a string constant used as a relation. DuckDB reads
#    `FROM '/path/x.csv'` as a file scan, but the relation scanner tokenised no
#    identifier there (the literal was masked to ''), so *zero* relations were
#    reported and the allow-list check passed trivially. At the time only
#    `enable_external_access=false` stopped this — a single layer, which is
#    exactly the situation ADR-022 set out to end.
#
#    CLOSED by the tree walk: the parser reports the file string as the
#    `table_name` of a BASE_TABLE, so the allow-list sees it. Measured on DuckDB
#    1.5.3: `Relation(s) not in allow-list: /etc/passwd.`
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM '/etc/passwd'",
        "SELECT * FROM 'https://evil.example/x.parquet'",
        "SELECT * FROM invoices JOIN '/etc/hosts' ON true",
    ],
    ids=["local_file", "remote_url", "join_on_file"],
)
def test_string_constant_used_as_a_relation_is_rejected(sql: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


# --------------------------------------------------------------------------- #
# 6. Near-misses that the current implementation gets RIGHT. Each one
#    discriminates: a naive guard (deny-list only, or an allow-list that ignores
#    quoting / qualification) would let it through.
# --------------------------------------------------------------------------- #

CORRECTLY_REJECTED = [
    # Quoting an identifier must not hide it from the relation allow-list.
    ('SELECT * FROM "internal_notes"', "quoted relation name"),
    ('SELECT * FROM "duckdb_settings"()', "quoted table function"),
    # Qualification must be resolved to the last component, not skipped.
    ("SELECT * FROM main.internal_notes", "schema qualified"),
    ("SELECT * FROM memory.main.internal_notes", "catalog qualified"),
    # `\bpragma\b` does NOT match `pragma_version`; only the relation/function
    # allow-list catches this. A deny-list-only guard would accept it.
    ("SELECT * FROM pragma_version()", "pragma_version not caught by deny-list"),
    ("SELECT * FROM duckdb_secrets()", "secrets table function"),
    ("SELECT * FROM read_ndjson('/etc/passwd')", "read_ndjson not on deny-list"),
    ("SELECT * FROM read_duckdb('/tmp/other.duckdb')", "read_duckdb not on deny-list"),
    # The masked copy must not let a hostile name ride inside a string literal
    # and then be *unmasked* anywhere.
    ("SELECT * FROM invoices WHERE status = 'x' UNION SELECT * FROM internal_notes",
     "set operation second branch"),
    # A derived table is skipped by the relation-list walk; the outer scan must
    # still descend into it. A scanner that jumps past the balanced group sees
    # nothing here at all.
    ("SELECT * FROM (SELECT * FROM internal_notes) AS t", "derived table"),
    (
        "SELECT * FROM (SELECT * FROM (SELECT * FROM internal_notes) AS a) AS b",
        "doubly nested derived table",
    ),
    (
        "SELECT * FROM invoices JOIN (SELECT * FROM internal_notes) AS t ON true",
        "derived table behind a join",
    ),
    # Only the FUNCTION allow-list can catch a scalar catalog function in the
    # select list — no relation is named, and `current_setting` is on no
    # deny-list. A deny-list-only guard accepts this.
    (
        "SELECT current_setting('memory_limit') AS leak FROM invoices",
        "scalar catalog function",
    ),
    ("SELECT version() AS leak FROM invoices", "scalar version function"),
    (
        "SELECT sum(amount) AS s, getenv('AWS_SECRET_ACCESS_KEY') AS leak "
        "FROM invoices",
        "scalar env function",
    ),
]


@pytest.mark.parametrize(
    "sql,label", CORRECTLY_REJECTED, ids=[c[1] for c in CORRECTLY_REJECTED]
)
def test_near_miss_attacks_stay_rejected(sql: str, label: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_deny_word_inside_an_ordinary_literal_is_still_data() -> None:
    """Symmetry check. Rejecting the exotic quoting forms outright is a fine
    repair; degrading to "reject anything containing a quote or a deny word" is
    not. Ordinary literals — including escaped apostrophes and semicolons — must
    keep working, which is what makes the masking layer worth having.
    """
    guard_query("SELECT * FROM customers WHERE name = 'O''Brien; DROP TABLE x'")
    guard_query("SELECT * FROM customers WHERE name LIKE '%create%' OR name = ';'")


# --------------------------------------------------------------------------- #
# 7. Over-blocking (guarantee 4). Every query below is valid DuckDB, reads only
#    allow-listed relations, and was refused *when this block was written* — each
#    one is a false positive that had to be repaired, and the assertion is that
#    it stays repaired. Each is executed against the real connection first, so
#    the test cannot be satisfied by "fixing" the guard to accept something
#    DuckDB would reject anyway.
# --------------------------------------------------------------------------- #

LEGITIMATE = [
    # `EXTRACT(part FROM col)` — the FROM inside the function is scanned as a
    # relation list, so `due_date` (and then `count`) are reported as unknown
    # relations. This is the single most common analytical date idiom.
    (
        "SELECT extract(year FROM due_date) AS y, count(*) AS n "
        "FROM invoices GROUP BY 1",
        "extract_year_from",
    ),
    (
        "SELECT substring(status FROM 1 FOR 3) AS s FROM invoices",
        "substring_from_for",
    ),
    (
        "SELECT trim(BOTH ' ' FROM status) AS s FROM invoices",
        "trim_both_from",
    ),
    # Type names with a precision look like function calls to _FUNC_CALL_RE.
    (
        "SELECT cast(amount AS DECIMAL(18,2)) AS a FROM invoices",
        "cast_to_decimal",
    ),
    (
        "SELECT cast(customer_id AS VARCHAR(10)) AS c FROM invoices",
        "cast_to_varchar_n",
    ),
    # NOTE (was `recursive_cte`, moved to RECURSIVE_CTE_IS_REFUSED below).
    # Recursive CTEs are now refused ON PURPOSE, not over-blocked by accident.
    # See the block at the end of this file for the measurement that forced it.
    (
        "WITH t(a) AS (SELECT amount FROM invoices) SELECT sum(a) AS s FROM t",
        "cte_with_column_list",
    ),
    # A plain wall-clock function, absent from the allow-list.
    ("SELECT count(*) AS n FROM invoices WHERE due_date < now()", "now"),
]


@pytest.mark.parametrize("sql,label", LEGITIMATE, ids=[q[1] for q in LEGITIMATE])
def test_legitimate_query_is_not_over_blocked(con, sql: str, label: str) -> None:
    # Sanity: DuckDB itself accepts this query on the throwaway ledger.
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    rows = run_guarded(con, sql)  # must not raise GuardrailError
    assert rows is not None


# --------------------------------------------------------------------------- #
# 8. Regression floor: the ordinary analytical queries the agent lives on must
#    keep working end to end through the hardened connection.
# --------------------------------------------------------------------------- #

STILL_WORKS = [
    ("SELECT count(*) AS n FROM invoices", "count"),
    (
        "SELECT c.name, sum(i.amount) AS total FROM customers c "
        "JOIN v_invoices i ON c.customer_id = i.customer_id "
        "GROUP BY c.name ORDER BY total DESC",
        "join_group_order",
    ),
    (
        "WITH overdue AS (SELECT customer_id, amount FROM v_invoices "
        "WHERE status = 'overdue') "
        "SELECT customer_id, sum(amount) AS s FROM overdue GROUP BY customer_id",
        "cte",
    ),
    (
        "SELECT customer_id, row_number() OVER "
        "(PARTITION BY customer_id ORDER BY amount DESC) AS rn FROM invoices "
        "QUALIFY rn = 1",
        "window_qualify",
    ),
    (
        "SELECT date_trunc('month', due_date) AS m, "
        "sum(amount) FILTER (WHERE status = 'overdue') AS od FROM invoices "
        "GROUP BY 1",
        "date_trunc_filter",
    ),
    ("SELECT * FROM customers WHERE name = 'O''Brien'", "escaped_apostrophe"),
]


@pytest.mark.parametrize("sql,label", STILL_WORKS, ids=[q[1] for q in STILL_WORKS])
def test_ordinary_analytics_still_runs(con, sql: str, label: str) -> None:
    run_guarded(con, sql)


# =========================================================================== #
# ROUND 2 — against the tree-walking guard.
#
# The string scanner was replaced by "parse with DuckDB's own parser, then walk
# the serialized tree". That closes every round-1 finding at the root: there is
# no second parser to disagree with, so §1's masking desyncs, §2's wrapper
# escape and §4's quoted function names are all gone. The tests above still
# reject, and they reject for the right reason (`duckdb_settings` is now caught
# as a *function*, `"current_setting"(…)` normalises to `current_setting`).
#
# Two exceptions worth stating rather than leaving quiet:
#
# * `test_wrapper_cannot_be_escaped_by_hidden_semicolons` now fails at "Query
#   does not parse" (the payload's `)` is unbalanced once the parser is real),
#   so it no longer exercises the single-statement rule at all.
#   `test_valid_stacked_statements_are_rejected` below restores that coverage
#   with a payload that parses cleanly.
# * `test_string_constant_used_as_a_relation_is_rejected[join_on_file]` passed
#   in round 1 for the wrong reason — the old scanner recorded the token `on`
#   as a relation name. Under the tree walk it rejects on `/etc/hosts`, which
#   is the reason it was written for.
#
# What the new mechanism does not do is decide *which* names in the tree count.
# Everything below is a name the walk can see but does not check.
# =========================================================================== #


def test_valid_stacked_statements_are_rejected() -> None:
    """Restores the coverage the round-1 wrapper payload used to give: a
    syntactically clean two-statement string must be refused by the parser's
    statement count, not by a syntax error."""
    with pytest.raises(GuardrailError, match=r"(?i)single statement"):
        guard_query("SELECT 1 AS a; SELECT 2 AS b")


# --------------------------------------------------------------------------- #
# R2-1. SHOW / DESCRIBE parse to a SELECT whose FROM is a `SHOW_REF` node, not a
#       `BASE_TABLE`. `_collect` special-cases `type == "BASE_TABLE"` before
#       reading `table_name`, so the SHOW_REF's own `table_name`
#       ("__show_tables_expanded") is walked straight past. Nothing is
#       collected, and both allow-lists then pass on empty sets.
# --------------------------------------------------------------------------- #

SHOW_STATEMENTS = [
    ("SHOW ALL TABLES", "show_all_tables"),
    ("SHOW TABLES", "show_tables"),
    ("SHOW TABLES FROM main", "show_tables_from_schema"),
    ("SHOW DATABASES", "show_databases"),
    ("SHOW SCHEMAS", "show_schemas"),
    ("SHOW ALL", "show_all"),
    ("SHOW VARIABLES", "show_variables"),
]


@pytest.mark.parametrize("sql,label", SHOW_STATEMENTS, ids=[s[1] for s in SHOW_STATEMENTS])
def test_show_statements_are_rejected(sql: str, label: str) -> None:
    """A SHOW is a catalog read. It names no allow-listed relation, so it must
    not be reachable just because the parser models it as a SELECT."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_show_all_tables_does_not_enumerate_the_catalog(con) -> None:
    """Guarantee 2 + 3: this returns every relation in the ledger — including
    ones off the allow-list — with their column names and types."""
    assert_blocked(con, "SHOW ALL TABLES", forbidden="internal_notes")


def test_show_tables_does_not_enumerate_the_catalog(con) -> None:
    assert_blocked(con, "SHOW TABLES", forbidden="internal_notes")


def test_show_nested_in_a_subquery_does_not_enumerate_the_catalog(con) -> None:
    # The guard's own wrapper already puts the SHOW in a subquery; naming the
    # columns just makes the exfiltration selective.
    assert_blocked(
        con,
        "SELECT * FROM (SHOW ALL TABLES) AS t(db, sch, nm, cols, typs, tmp) "
        "WHERE nm LIKE 'internal%'",
        forbidden="internal_notes",
    )


def test_show_databases_does_not_leak_attached_databases(con) -> None:
    assert_blocked(con, "SHOW DATABASES")


# --------------------------------------------------------------------------- #
# R2-2. CTE names are collected into ONE flat set for the whole tree, with no
#       regard for the scope they were declared in. A CTE declared inside a
#       nested subquery therefore exempts that table name everywhere else in
#       the statement — including where DuckDB resolves it to the real table.
# --------------------------------------------------------------------------- #

CTE_SCOPE_LEAK_COMMA = (
    "SELECT * FROM internal_notes, "
    "(WITH internal_notes AS (SELECT 1 AS a) SELECT a FROM internal_notes) AS q"
)
CTE_SCOPE_LEAK_SIBLING = (
    "WITH q AS (WITH internal_notes AS (SELECT 1 AS a) SELECT a FROM internal_notes) "
    "SELECT (SELECT max(secret) FROM internal_notes) AS leak FROM q"
)
CTE_SCOPE_LEAK_SCALAR = (
    "SELECT (SELECT 1 FROM (WITH internal_notes AS (SELECT 1 AS a) "
    "SELECT a FROM internal_notes)) AS z, "
    "(SELECT max(secret) FROM internal_notes) AS leak FROM invoices"
)


@pytest.mark.parametrize(
    "sql",
    [CTE_SCOPE_LEAK_COMMA, CTE_SCOPE_LEAK_SIBLING, CTE_SCOPE_LEAK_SCALAR],
    ids=["comma_join", "sibling_scope", "scalar_subquery"],
)
def test_cte_exemption_does_not_escape_its_scope(sql: str) -> None:
    """A CTE only shadows the name inside its own query. The exemption must not
    be granted to a reference the CTE cannot possibly bind."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_out_of_scope_cte_does_not_read_a_forbidden_table(con) -> None:
    assert_blocked(con, CTE_SCOPE_LEAK_SIBLING, forbidden=SECRET_MARKER)


def test_out_of_scope_cte_does_not_read_a_forbidden_table_via_comma_join(con) -> None:
    assert_blocked(con, CTE_SCOPE_LEAK_COMMA, forbidden=SECRET_MARKER)


# --------------------------------------------------------------------------- #
# R2-3. The schema-qualified guard builds the string `f"{schema}.{table}"` and
#       compares it against the CTE set. A quoted CTE name is a single
#       identifier that may itself contain a dot, so it can be made to match
#       that synthesised string exactly — which is precisely the exemption the
#       code comment says a CTE must never get.
# --------------------------------------------------------------------------- #

DOTTED_CTE_TABLE = (
    'WITH internal_notes AS (SELECT 1), "main.internal_notes" AS (SELECT 1) '
    "SELECT * FROM main.internal_notes"
)
DOTTED_CTE_INFO_SCHEMA = (
    'WITH "information_schema.tables" AS (SELECT 1), tables AS (SELECT 1) '
    "SELECT * FROM information_schema.tables"
)
DOTTED_CTE_INFO_COLUMNS = (
    'WITH "information_schema.columns" AS (SELECT 1), columns AS (SELECT 1) '
    "SELECT table_name, column_name FROM information_schema.columns"
)
DOTTED_CTE_PG_TABLES = (
    'WITH "pg_catalog.pg_tables" AS (SELECT 1), pg_tables AS (SELECT 1) '
    "SELECT * FROM pg_catalog.pg_tables"
)


@pytest.mark.parametrize(
    "sql",
    [
        DOTTED_CTE_TABLE,
        DOTTED_CTE_INFO_SCHEMA,
        DOTTED_CTE_INFO_COLUMNS,
        DOTTED_CTE_PG_TABLES,
    ],
    ids=["real_table", "information_schema_tables", "information_schema_columns",
         "pg_catalog_pg_tables"],
)
def test_a_cte_name_containing_a_dot_does_not_exempt_a_qualified_table(
    sql: str,
) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_dotted_cte_does_not_read_a_forbidden_table(con) -> None:
    assert_blocked(con, DOTTED_CTE_TABLE, forbidden=SECRET_MARKER)


def test_dotted_cte_does_not_reach_information_schema(con) -> None:
    assert_blocked(con, DOTTED_CTE_INFO_SCHEMA, forbidden="internal_notes")


def test_dotted_cte_does_not_reach_information_schema_columns(con) -> None:
    # Leaks every column name of every relation, allow-listed or not.
    assert_blocked(con, DOTTED_CTE_INFO_COLUMNS, forbidden="internal_notes")


def test_dotted_cte_does_not_reach_pg_catalog(con) -> None:
    assert_blocked(con, DOTTED_CTE_PG_TABLES, forbidden="internal_notes")


# --------------------------------------------------------------------------- #
# R2-4. Near-misses the tree walk gets RIGHT. Each discriminates against a
#       plausible weaker walk.
# --------------------------------------------------------------------------- #

R2_CORRECTLY_REJECTED = [
    # DESCRIBE / SUMMARIZE embed the target as a real sub-tree, so the walk sees
    # it. A walk that stopped at SHOW_REF without descending would miss these.
    ("DESCRIBE internal_notes", "describe_forbidden_table"),
    ("SUMMARIZE internal_notes", "summarize_forbidden_table"),
    ("SUMMARIZE SELECT * FROM internal_notes", "summarize_forbidden_select"),
    # An in-scope CTE must not launder a forbidden table in its own body.
    (
        "WITH customers AS (SELECT * FROM internal_notes) SELECT * FROM customers",
        "cte_body_reads_forbidden_table",
    ),
    # Function names are canonicalised by the parser, so alternate spellings and
    # quoted forms collapse onto the same checked name.
    ('SELECT "duckdb_settings"() AS x', "quoted_table_function"),
    ("SELECT * FROM main.duckdb_settings", "qualified_catalog_view"),
    # The bare CTE name alone must not carry the qualified reference.
    (
        "WITH tables AS (SELECT 1) SELECT * FROM information_schema.tables",
        "bare_cte_does_not_cover_qualified",
    ),
]


@pytest.mark.parametrize(
    "sql,label", R2_CORRECTLY_REJECTED, ids=[c[1] for c in R2_CORRECTLY_REJECTED]
)
def test_r2_near_miss_attacks_stay_rejected(sql: str, label: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


# --------------------------------------------------------------------------- #
# R2-5. Over-blocking introduced by the rewrite (guarantee 4) — same convention
#       as §7: refused when written, repaired since, asserted so it stays that way.
# --------------------------------------------------------------------------- #

R2_LEGITIMATE = [
    # `_collect` adds `schema.table` to the checked set, but ALLOWED_RELATIONS
    # holds bare names only — so qualifying an allow-listed table is refused.
    ("SELECT * FROM main.customers", "schema_qualified_allowed_table"),
    (
        "SELECT c.name FROM main.customers c JOIN main.invoices i "
        "ON c.customer_id = i.customer_id",
        "schema_qualified_join",
    ),
    # DuckDB's parser rewrites `INTERVAL n DAY` to `to_days(n)`; none of
    # to_days / to_months / to_years is on the allow-list, so the most natural
    # spelling of an aging window is refused while INTERVAL '30 days' passes.
    (
        "SELECT count(*) AS n FROM invoices "
        "WHERE due_date < CURRENT_DATE - INTERVAL 30 DAY",
        "interval_n_day",
    ),
    (
        "SELECT count(*) AS n FROM invoices "
        "WHERE due_date < CURRENT_DATE - INTERVAL 3 MONTH",
        "interval_n_month",
    ),
    (
        "SELECT count(*) AS n FROM invoices "
        "WHERE due_date > CURRENT_DATE - INTERVAL 1 YEAR",
        "interval_n_year",
    ),
    ("SELECT char_length(name) AS n FROM customers", "char_length"),
]


@pytest.mark.parametrize("sql,label", R2_LEGITIMATE, ids=[q[1] for q in R2_LEGITIMATE])
def test_r2_legitimate_query_is_not_over_blocked(con, sql: str, label: str) -> None:
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    assert run_guarded(con, sql) is not None


def test_row_cap_survives_end_to_end(con) -> None:
    """The wrapper is a guarantee, not decoration: whatever the model asks for,
    the executed statement must not return more than `max_rows` rows. Asserted
    against the engine, not against the returned string."""
    rows = con.execute(guard_query("SELECT * FROM invoices", max_rows=1)).fetchall()
    assert len(rows) == 1
    assert con.execute("SELECT count(*) FROM invoices").fetchone()[0] > 1


def test_row_cap_cannot_be_widened_by_the_query(con) -> None:
    rows = con.execute(
        guard_query("SELECT * FROM invoices LIMIT 100", max_rows=1)
    ).fetchall()
    assert len(rows) == 1


# =========================================================================== #
# ROUND 3 — against the scoped tree walk.
#
# R2-1/2/3 are all closed, and closed properly: `table_name` is read wherever it
# appears (so SHOW_REF is caught as a pseudo-relation), CTE scope is lexical, and
# a schema-qualified name skips the CTE set instead of being string-compared
# against it. Every round-1 and round-2 attack string in this file is still
# refused — re-verified by replaying the whole corpus, not by assuming.
#
# What is left is the one thing scoping is easy to get subtly wrong: WHERE a CTE
# name is visible. `scope` is unioned in at the node carrying `cte_map` and then
# passed to *every* child of that node — and the CTE definitions are themselves
# children of that node. So each CTE body is validated as though every CTE name
# in the list were already in scope, which is not how SQL binds them.
# =========================================================================== #

# `leak` is validated with `internal_notes` in scope because a LATER CTE declares
# that name. SQL makes a CTE visible only to CTEs that follow it and to the main
# query, so inside `leak` the name still binds to the real base table.
CTE_FORWARD_REFERENCE = (
    "WITH leak AS (SELECT max(secret) AS s FROM internal_notes), "
    "internal_notes AS (SELECT 1 AS x) "
    "SELECT * FROM leak"
)
CTE_FORWARD_REFERENCE_NESTED = (
    "WITH leak AS (SELECT * FROM (SELECT secret FROM internal_notes) z), "
    "internal_notes AS (SELECT 1 AS x) "
    "SELECT * FROM leak"
)
# A non-recursive CTE's own name is not in scope inside its own body either:
# DuckDB binds this `internal_notes` to the base table, the guard exempts it.
CTE_SELF_REFERENCE = (
    "WITH internal_notes AS (SELECT * FROM internal_notes) "
    "SELECT * FROM internal_notes"
)
CTE_SELF_REFERENCE_RECURSIVE = (
    "WITH RECURSIVE internal_notes AS (SELECT * FROM internal_notes) "
    "SELECT * FROM internal_notes"
)


@pytest.mark.parametrize(
    "sql",
    [
        CTE_FORWARD_REFERENCE,
        CTE_FORWARD_REFERENCE_NESTED,
        CTE_SELF_REFERENCE,
        CTE_SELF_REFERENCE_RECURSIVE,
    ],
    ids=["forward_reference", "forward_reference_nested", "self_reference",
         "self_reference_recursive"],
)
def test_cte_scope_does_not_extend_into_the_cte_definitions(sql: str) -> None:
    """A CTE name must not be treated as in-scope inside a CTE body that SQL
    binds before that name exists."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


def test_forward_declared_cte_does_not_read_a_forbidden_table(con) -> None:
    assert_blocked(con, CTE_FORWARD_REFERENCE, forbidden=SECRET_MARKER)


def test_forward_declared_cte_does_not_read_a_forbidden_table_when_nested(con) -> None:
    assert_blocked(con, CTE_FORWARD_REFERENCE_NESTED, forbidden=SECRET_MARKER)


def test_self_referencing_cte_does_not_read_a_forbidden_table(con) -> None:
    assert_blocked(con, CTE_SELF_REFERENCE, forbidden=SECRET_MARKER)


def test_recursive_self_referencing_cte_does_not_read_a_forbidden_table(con) -> None:
    assert_blocked(con, CTE_SELF_REFERENCE_RECURSIVE, forbidden=SECRET_MARKER)


# --------------------------------------------------------------------------- #
# R3-2. Scoping in the other direction: names that MUST stay exempt. These pass
#       today and discriminate — a guard that "fixed" the holes above by simply
#       narrowing or dropping the CTE exemption turns every one of them red.
# --------------------------------------------------------------------------- #

R3_MUST_STAY_ALLOWED = [
    ("WITH a AS (SELECT 1 AS x), b AS (SELECT * FROM a) SELECT * FROM b",
     "later_cte_sees_earlier_cte"),
    ("SELECT * FROM (WITH a AS (SELECT 1 AS x) SELECT * FROM a) AS z",
     "cte_declared_inside_a_subquery"),
    ("WITH o AS (WITH i AS (SELECT 1 AS x) SELECT * FROM i) SELECT * FROM o",
     "cte_declared_inside_a_cte_body"),
    ("WITH a AS (SELECT 1 AS x) SELECT x FROM a UNION ALL SELECT x FROM a",
     "cte_used_in_both_set_operation_branches"),
    ("WITH a AS (SELECT 1 AS x) SELECT (SELECT max(x) FROM a) AS m FROM invoices",
     "cte_used_in_a_scalar_subquery"),
    ("WITH a AS MATERIALIZED (SELECT 1 AS x) SELECT * FROM a", "materialized_cte"),
    ("WITH invoices AS (SELECT 1 AS x) SELECT * FROM invoices",
     "cte_shadowing_an_allow_listed_table"),
]


@pytest.mark.parametrize(
    "sql,label", R3_MUST_STAY_ALLOWED, ids=[q[1] for q in R3_MUST_STAY_ALLOWED]
)
def test_in_scope_cte_reference_stays_allowed(con, sql: str, label: str) -> None:
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    assert run_guarded(con, sql) is not None


# --------------------------------------------------------------------------- #
# R3-3. Over-blocking: the pseudo-relation rule refuses every SHOW_REF, but
#       DESCRIBE and SUMMARIZE carry their target as a real sub-tree. Refusing
#       the whole node type also refuses descriptive statistics over an
#       allow-listed relation, which is a legitimate analytical query.
#
#       These are paired on purpose with `SHOW ALL TABLES` (must stay rejected)
#       and `SUMMARIZE internal_notes` (must stay rejected). A repair that
#       distinguishes "SHOW_REF with a query sub-tree" from "SHOW_REF naming a
#       catalog listing" satisfies all four; a repair that simply re-admits
#       SHOW_REF turns the R2 tests red.
# --------------------------------------------------------------------------- #

R3_LEGITIMATE = [
    ("SUMMARIZE invoices", "summarize_allow_listed_table"),
    ("SUMMARIZE SELECT amount FROM invoices", "summarize_allow_listed_select"),
    ("DESCRIBE invoices", "describe_allow_listed_table"),
]


@pytest.mark.parametrize("sql,label", R3_LEGITIMATE, ids=[q[1] for q in R3_LEGITIMATE])
def test_r3_legitimate_query_is_not_over_blocked(con, sql: str, label: str) -> None:
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    assert run_guarded(con, sql) is not None


# --------------------------------------------------------------------------- #
# R3-4. Near-misses the scoped walk gets right. Each discriminates.
# --------------------------------------------------------------------------- #

R3_CORRECTLY_REJECTED = [
    # Two-part and three-part names whose schema is not the ledger's own.
    ("SELECT * FROM information_schema.tables", "qualified_information_schema"),
    ("SELECT * FROM pg_catalog.pg_tables", "qualified_pg_catalog"),
    # NOTE: this one does NOT discriminate on the catalog. It was written as a
    # three-part case, but `tables` is not an allow-listed relation, so it is
    # refused on the bare name and would stay green with the catalog unread.
    # The tests that actually pin the catalog are in R4.
    ("SELECT * FROM system.information_schema.tables", "three_part_catalog_qualified"),
    # A quoted dot in a CTE name no longer buys anything, because a qualified
    # reference does not consult the CTE set at all.
    ('WITH "main.internal_notes" AS (SELECT 1) SELECT * FROM main.internal_notes',
     "dotted_cte_vs_qualified_name"),
    # `main` is allow-listed as a schema, but the bare name behind it is not.
    ("SELECT * FROM main.internal_notes", "allowed_schema_forbidden_table"),
    ("SELECT * FROM main.pg_tables", "allowed_schema_catalog_view"),
    # A CTE is not a licence to read the base table it shadows.
    ("WITH customers AS (SELECT * FROM internal_notes) SELECT * FROM customers",
     "cte_body_reads_forbidden_table"),
    # Deferred / indirect relation names.
    ("SELECT * FROM query('SELECT * FROM internal_notes')", "query_function"),
    ("SELECT * FROM query_table('internal_notes')", "query_table_function"),
    ("TABLE internal_notes", "table_shorthand"),
    ("FROM internal_notes", "from_first_syntax"),
    ("SELECT * FROM invoices, LATERAL (SELECT * FROM internal_notes) l",
     "lateral_subquery"),
    ("SELECT internal_notes.secret FROM invoices, internal_notes", "comma_join"),
]


@pytest.mark.parametrize(
    "sql,label", R3_CORRECTLY_REJECTED, ids=[c[1] for c in R3_CORRECTLY_REJECTED]
)
def test_r3_near_miss_attacks_stay_rejected(sql: str, label: str) -> None:
    with pytest.raises(GuardrailError):
        guard_query(sql)


# --------------------------------------------------------------------------- #
# A deliberate capability reduction, recorded rather than hidden.
#
# Two cases in this file originally asserted that recursive CTEs are accepted.
# They are now refused, and that is a decision, not a regression.
#
# PRECISION (2026-07-30, from the claim audit): "recursive CTEs are refused" is
# too broad and was corrected in ADR-022. There is no recursion-specific rule.
# A self-reference is checked against the relation allow-list like any other name,
# so `WITH RECURSIVE t AS (… FROM t …)` is refused because `t` is not allow-listed,
# while `WITH RECURSIVE invoices AS (… FROM invoices …)` is ACCEPTED. The cases
# below use a non-allow-listed name, which is why they are refused.
#
# The reason is measured, not assumed. On DuckDB 1.5.3 a CTE's own name inside
# its own body binds to a real base table of that name — including under
# RECURSIVE, where the *anchor* term resolves to the table while only the
# recursive term resolves to the CTE:
#
#   WITH RECURSIVE internal_notes AS (
#       SELECT secret FROM internal_notes UNION ALL SELECT 'x')
#   SELECT * FROM internal_notes
#   -->  [('FORBIDDEN',), ('x',)]      <-- read a non-allow-listed table
#
# So "a CTE's own name is exempt inside its own body" cannot be made safe by
# restricting it to the recursive form. The exemption is gone entirely. The
# price is that genuine recursion is refused; the price of keeping it was a
# read of any table in the ledger. A refusal is visible, a leak is not.
#
# If recursive CTEs are ever needed, the fix is NOT to restore the exemption —
# it is to resolve names against the actual schema instead of a name list.
# --------------------------------------------------------------------------- #

RECURSIVE_CTE_IS_REFUSED = [
    (
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t "
        "WHERE n < 5) SELECT sum(n) AS s FROM t",
        "recursive_cte_anchor_and_term",
    ),
    (
        "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t "
        "WHERE n < 4) SELECT sum(n) AS s FROM t",
        "recursive_cte_self_reference",
    ),
]


@pytest.mark.parametrize(
    "sql,label", RECURSIVE_CTE_IS_REFUSED, ids=[c[1] for c in RECURSIVE_CTE_IS_REFUSED]
)
def test_recursive_ctes_are_refused_on_purpose(sql: str, label: str) -> None:
    """Documents the trade-off above. If this ever passes, the exemption is back."""
    with pytest.raises(GuardrailError):
        guard_query(sql)


# --------------------------------------------------------------------------- #
# ROUND 4 — the third part of the name.
#
# `_collect` read `table_name` and `schema_name` and never read `catalog_name`.
# A `BASE_TABLE` node carries all three:
#
#   SELECT * FROM evildb.main.customers
#   -->  {"type": "BASE_TABLE", "catalog_name": "evildb",
#         "schema_name": "main", "table_name": "customers"}
#
# In a three-part name the database goes to `catalog_name` and `main` goes to
# `schema_name`, so the schema branch saw an allow-listed schema, fell through
# to the bare-name check, and `customers` is allow-listed. Measured then:
#
#   SELECT * FROM evildb.main.customers               -> ACCEPTED
#   SELECT * FROM "/tmp/other.duckdb".main.customers  -> ACCEPTED
#
# Severity, stated honestly: LATENT, not live. Nothing could be attached —
# `ATTACH` is refused before the walk (it does not serialize as a select) and
# the ledger connection carries `enable_external_access=false` with
# `lock_configuration=true`. It becomes an exploit the day a second catalog is
# attached. The cost that was already real is that the confidentiality half of
# the guard was one layer again, which is the thing ADR-022 exists to prevent.
#
# WHY THESE TESTS ASSERT ON THE MESSAGE. End-to-end blocking proves nothing
# here: `evildb` does not exist on the connection, so DuckDB refuses the query
# by itself and `assert_blocked` stays green whether or not the guard ever looks
# at the catalog. The only assertion that discriminates is that the guard names
# the whole path in its refusal. That was the flaw in the pre-existing
# `three_part_catalog_qualified` case in R3-4 — it was refused on `tables`, a
# name that is not allow-listed anyway.
# --------------------------------------------------------------------------- #

# Every part except the catalog is allow-listed: `main` is in ALLOWED_SCHEMAS
# and the bare name is in ALLOWED_RELATIONS. Nothing but the catalog check can
# refuse these, which is what makes them a discriminator.
R4_CATALOG_QUALIFIED = [
    ("SELECT * FROM evildb.main.customers", "evildb.main.customers"),
    ("SELECT * FROM pg_catalog.main.invoices", "pg_catalog.main.invoices"),
    ('SELECT * FROM "/tmp/other.duckdb".main.payments',
     "/tmp/other.duckdb.main.payments"),
    ("SELECT * FROM memory.main.v_dso", "memory.main.v_dso"),
    ("SELECT c.name FROM evildb.main.customers AS c", "evildb.main.customers"),
    ("WITH t AS (SELECT * FROM evildb.main.invoices) SELECT * FROM t",
     "evildb.main.invoices"),
]


@pytest.mark.parametrize(
    "sql,path", R4_CATALOG_QUALIFIED, ids=[p[1] for p in R4_CATALOG_QUALIFIED]
)
def test_r4_catalog_qualified_name_is_refused_naming_the_catalog(
    sql: str, path: str
) -> None:
    with pytest.raises(GuardrailError) as exc:
        guard_query(sql)
    # The full three-part path, not just the bare name: a guard that refused
    # this on `customers` alone would be refusing the wrong thing.
    assert path in str(exc.value), str(exc.value)


def test_r4_a_cte_cannot_launder_a_catalog_qualified_name() -> None:
    """A CTE named after the table does not exempt the qualified reference.

    Same property the schema branch has: a qualified name never consults the CTE
    set, because a CTE cannot be catalog-qualified in the first place.
    """
    with pytest.raises(GuardrailError) as exc:
        guard_query(
            'WITH "evildb.main.customers" AS (SELECT 1), customers AS (SELECT 1) '
            "SELECT * FROM evildb.main.customers"
        )
    assert "evildb.main.customers" in str(exc.value)


# The decision, recorded rather than left implicit: catalog qualification is
# refused OUTRIGHT — there is no allow-list of catalog names. The ledger's own
# catalog is named after its file (`data/ledger.duckdb` -> `ledger`, and this
# suite's throwaway ledger is also `ledger`), so an allow-list here would pin
# a deployment's file name into the guard and grow a second thing to keep in
# sync. Exactly one database is open on the ledger connection and nothing can
# attach another, so a three-part name is never needed to reach the data.
#
# The price is this test: the true name of the ledger's own catalog is refused.
# It is paid knowingly. If it ever costs a real query, the fix is a schema hint
# that stops the model writing three-part names — not an allow-list of catalogs.
def test_r4_the_ledgers_own_catalog_is_refused_too() -> None:
    with pytest.raises(GuardrailError):
        guard_query("SELECT * FROM ledger.main.customers")


# Paired against the block above so a lazy repair cannot pass: refusing *all*
# qualification would turn these red. Two-part names against the ledger's own
# schema are the form the agent actually emits.
R4_STILL_ALLOWED = [
    ("SELECT * FROM main.customers", "two_part_allowed_schema"),
    ("SELECT count(*) AS n FROM main.invoices", "two_part_with_aggregate"),
    ("SELECT * FROM customers", "bare_name"),
]


@pytest.mark.parametrize(
    "sql,label", R4_STILL_ALLOWED, ids=[q[1] for q in R4_STILL_ALLOWED]
)
def test_r4_unqualified_and_schema_qualified_names_still_pass(
    con, sql: str, label: str
) -> None:
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    assert run_guarded(con, sql) is not None

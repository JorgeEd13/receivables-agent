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

import duckdb
import pytest

from src.agent import sql_guard
from src.agent.ledger import connect_readonly
from src.agent.sql_guard import (
    ALLOWED_FUNCTIONS,
    ALLOWED_RELATIONS,
    DEFAULT_MAX_ROWS,
    MAX_ROWS_CEILING,
    MAX_WALK_DEPTH,
    GuardrailError,
    guard_query,
)

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
# 1. Parser/executor disagreement: the guard checked a MASKED copy, DuckDB
#    executed the ORIGINAL. Any construct DuckDB treated as a string but the
#    masker did not (or vice versa) desynchronised quote parity and blanked the
#    rest of the statement out of every check.
#
#    CLOSED by the tree walk (ADR-022), and this banner is dated 2026-07-30
#    because the block went on describing a live break for two rounds after the
#    break was gone. There is no masked copy: `guard_query` validates the parse
#    tree, and `_mask_literals` no longer exists in `sql_guard.py`. What the
#    three payloads below actually do today, measured on DuckDB 1.5.3:
#
#    * all three are refused by the same sentence — `Function(s) not in
#      allow-list: duckdb_settings.` The quoting construct is not what refuses
#      them; the sub-select in the payload is, and it would be refused with no
#      quoting trick at all. They are a regression floor for a dead attack, not
#      a discriminating test, so the assertion below now names the layer that
#      refuses. If that sentence ever changes, this section is testing something
#      other than what it says it is.
#    * strip the payload and all three are ACCEPTED — which is the correct
#      answer, not a second finding. `$a$'$a$`, `"a'b"` and `e'\''` are ordinary
#      DuckDB; refusing them would break guarantee 4. That acceptance is the one
#      property still standing here, so it is pinned below instead of assumed.
#
#    The test name `test_literal_masking_cannot_be_desynchronised` is kept
#    deliberately: like every round header in this file it names the break the
#    payload was written against, not the mechanism in force today.
#
#    MEASURED AFTER THIS AMENDMENT (324 tests, was 319). The pair that justifies
#    the `match=`: rot the three payloads into a parse error and, with the reason
#    asserted, three tests go red — with a bare `pytest.raises(GuardrailError)`
#    the same rot is 324 green, which is this section's original defect
#    reappearing. Putting `duckdb_settings` on the function allow-list is 11 red.
#    The masker mutation described above is 8 red. And the number that is not
#    flattering, in the spirit of ROUND 6: three mutations applied to these
#    tests instead of to the guard — dropping the `match=`, and reducing either
#    acceptance assertion to `is not None` — are all three green.
# --------------------------------------------------------------------------- #

# `$a$'$a$` is a dollar-quoted string whose *content* is a single quote. DuckDB
# read one string constant; `_mask_literals` saw a bare `'`, opened a literal
# that never closed, and masked the whole remainder of the query to `''`.
DOLLAR_QUOTE_DESYNC = (
    "SELECT $a$'$a$ AS z, (SELECT count(*) FROM duckdb_settings()) AS leak "
    "FROM invoices"
)

# Same desync with no `$` anywhere: `_mask_literals` had no notion of
# double-quoted identifiers, so an apostrophe inside one opened a literal.
DQUOTE_IDENT_DESYNC = (
    "SELECT 1 AS \"a'b\", (SELECT count(*) FROM duckdb_settings()) AS leak "
    "FROM invoices"
)

# Third desync source: DuckDB's `e'...'` escape strings honour backslash
# escapes, the scanner did not, so `e'\''` shifted quote parity by one.
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
    """The masked copy must not hide SQL that DuckDB will actually execute.

    The `match` is the point of the amended test: without it, this passes if
    *any* layer says no — including a parse error on a payload that has rotted,
    which is how a test keeps its green light after it stopped testing anything.
    """
    with pytest.raises(GuardrailError, match=r"not in allow-list: duckdb_settings"):
        guard_query(sql)


# The same constructs with the payload removed. This is the half of the
# statement the desync payloads never asserted: the guard must see *through* the
# quoting, not around it. A masker coming back by any route — an optimisation, a
# "sanitise before execute" step — blanks or mangles these, and they go red here
# rather than silently changing what reaches the engine.
#
# The two cases carrying content *around* the apostrophe are the ones doing the
# work, and that was measured, not assumed: a mutation that blanks string
# literals with `re.sub(r"'[^']*'", "''", ...)` leaves `''''` — the printed form
# of a lone apostrophe — untouched, so the first and third cases stay green
# under it while `a'b` turns red. A payload whose whole content is the escaped
# character cannot tell mangling from fidelity.
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT $a$'$a$ AS z FROM invoices", "'"),
        ("SELECT $tag$a'b$tag$ AS z FROM invoices", "a'b"),
        ("SELECT e'\\'' AS z FROM invoices", "'"),
        ("SELECT e'a\\'b' AS z FROM invoices", "a'b"),
    ],
    ids=[
        "dollar_quoted_string",
        "dollar_quoted_with_tag",
        "escape_string",
        "escape_string_with_content",
    ],
)
def test_quoting_constructs_reach_the_engine_unchanged(
    con, sql: str, expected: str
) -> None:
    """An apostrophe inside a quoted construct must survive to execution.

    The guard re-prints the statement from the validated tree (ADR-022), so
    DuckDB's own printer chooses the quoting: `$a$'$a$` comes back as `''''`.
    The assertion is on the *value*, not on the text, because the text is
    allowed to change and the value is not.
    """
    assert {row[0] for row in run_guarded(con, sql)} == {expected}


def test_quoted_identifier_alias_reaches_the_engine_unchanged(con) -> None:
    """Same property for `"a'b"` — an apostrophe inside an identifier. Here the
    survivor is the column name, so the assertion reads the cursor description
    instead of the rows."""
    cursor = con.execute(guard_query('SELECT 1 AS "a\'b" FROM invoices'))
    assert [column[0] for column in cursor.description] == ["a'b"]


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
# 2. The desync also hid `;` from the statement splitter, so the guard's
#    single-statement rule and its `SELECT * FROM (...) LIMIT n` wrapper could
#    both be escaped: the wrapper's closing `)` was closed early and arbitrary
#    statements were appended.
#
#    CLOSED by the tree walk (ADR-022), and this banner is dated 2026-07-31.
#    The payload below stopped working when the masker died, and the tests that
#    used it went on passing — which is the failure mode this file exists to
#    catch. Measured on DuckDB 1.5.3, in this order:
#
#    * `WRAPPER_ESCAPE` is refused by `Parser Error: syntax error at or near
#      ")"`. Not by the single-statement rule, not by the wrapper. There is no
#      masked copy to desynchronise, so the trailing `) AS q` that used to close
#      the wrapper early is now just a syntax error in the caller's own text.
#    * the two end-to-end tests below passed with `guard_query` REMOVED from the
#      call: 184 green in this file, zero red. `contextlib.suppress` swallowed
#      the parse error exactly as it swallowed the refusal, and `pwned` never
#      existed under any implementation. They asserted nothing about the guard.
#    * the thing they claimed to protect is real and was never tested. On a
#      `connect_readonly` connection — read-only, `enable_external_access=false`,
#      `lock_configuration=true` — DuckDB ACCEPTS `CREATE TEMP TABLE`,
#      `CREATE TEMPORARY VIEW`, `PREPARE` and `CREATE TEMP MACRO`, and the
#      objects are then readable. Only a non-temp `CREATE` is refused by the
#      engine (`Cannot execute statement of type "CREATE"`), and `ATTACH
#      ':memory:'` by read-only mode. So for four kinds of object the guard is
#      the *only* thing standing there, and a dead payload was all that guarded
#      it.
#
#    The rewrite keeps the historical payload as a regression floor with its
#    reason named, and adds live ones: statements the engine really does execute,
#    refused for a reason the assertion states. `contextlib.suppress` is gone —
#    it is the mechanism that let this rot in the first place.
#
#    MEASURED AFTER THIS AMENDMENT (346 tests, was 336). The mutation that this
#    whole block exists for — removing `guard_query` from the end-to-end calls —
#    is now 8 red, where the old pair was 0.
#
#    AND THE FINDING THAT CAME OUT OF MEASURING IT, which is bigger than the
#    dead payload: deleting the SELECT/WITH-only check in `_syntax_tree` left
#    the pre-amendment suite at 336 GREEN. Guarantee 1 — the first one in this
#    file's threat model — was asserted by exactly the two tests that had
#    stopped asserting anything. It is 4 red now, and all four are from this
#    block.
#
#    State the other half, because it cuts the other way. That mutation is NOT
#    a bypass: with the check deleted, a `CREATE TEMP TABLE` is still refused
#    one layer later, by `json_deserialize_sql` ("Query could not be
#    normalized"). Relaxing the statement count to allow a trailing statement
#    (the shape a real "support trailing semicolons" PR would take) is 6 red
#    and also not a bypass — the refusal moves to the SELECT/WITH check,
#    because `json_serialize_sql` rejects two-statement text as a unit. No
#    single-point mutation found here creates the object. The guarantee is held
#    redundantly by three layers, and what these tests pin is WHICH layer
#    speaks and what it says — the sentence the two callers hand back to the
#    LLM — not whether the object gets created.
#
#    Mutations of the guard, for the record: delete the single-statement check
#    6 red (4 of them from this block; 2 pre-existing) · delete the
#    SELECT/WITH-only check 4 red (all from this block) · allow a trailing
#    statement 6 red. The test-side measurement is at the end of the block.
# --------------------------------------------------------------------------- #

WRAPPER_ESCAPE = (
    "SELECT $a$'$a$ AS z FROM invoices) AS q; "
    "CREATE TEMP TABLE pwned AS SELECT 1 AS v; "
    "SELECT * FROM (SELECT 1"
)


def test_wrapper_cannot_be_escaped_by_hidden_semicolons() -> None:
    """A regression floor for a dead attack, and the assertion says so.

    Naming the parse error is not a decoration: with a bare
    `pytest.raises(GuardrailError)` this test cannot tell "the wrapper held"
    from "the payload rotted", which is the state it was actually in.
    """
    with pytest.raises(GuardrailError, match=r"does not parse.*syntax error"):
        guard_query(WRAPPER_ESCAPE)


# The live half. Each pair is (statement, how to prove the object exists).
# Every one of these is ACCEPTED by a `connect_readonly` connection when the
# guard is not in the way — that is asserted below rather than trusted, because
# a refusal test whose payload the engine would have refused anyway proves
# nothing about the guard.
CREATABLE_ON_A_READ_ONLY_CONNECTION = [
    ("CREATE TEMP TABLE pwned AS SELECT 1 AS v", "SELECT * FROM pwned"),
    ("CREATE TEMPORARY VIEW pwned_view AS SELECT 1 AS v", "SELECT * FROM pwned_view"),
    ("PREPARE evil AS SELECT 1", "EXECUTE evil"),
    ("CREATE TEMP MACRO pwned_macro() AS 1", "SELECT pwned_macro()"),
]
CREATABLE_IDS = ["temp_table", "temp_view", "prepared_statement", "temp_macro"]


@pytest.mark.parametrize(
    ("statement", "read_it_back"), CREATABLE_ON_A_READ_ONLY_CONNECTION, ids=CREATABLE_IDS
)
def test_the_engine_really_does_create_these(con, statement: str, read_it_back: str) -> None:
    """The premise of the two tests below: read-only does NOT mean "creates
    nothing". Temp objects live in the `temp` catalog, which is writable on a
    read-only connection by design. If DuckDB ever closes this, these refusal
    tests stop being about the guard and this one goes red to say so."""
    con.execute(statement)
    assert con.execute(read_it_back).fetchall() == [(1,)]


@pytest.mark.parametrize(
    ("statement", "read_it_back"), CREATABLE_ON_A_READ_ONLY_CONNECTION, ids=CREATABLE_IDS
)
def test_nothing_can_be_created_end_to_end(con, statement: str, read_it_back: str) -> None:
    """Guarantee 1, on its own: a creating statement is not a SELECT."""
    with pytest.raises(GuardrailError, match=r"read-only SELECT / WITH"):
        con.execute(guard_query(statement))
    with pytest.raises(duckdb.Error):
        con.execute(read_it_back)


@pytest.mark.parametrize(
    ("statement", "read_it_back"), CREATABLE_ON_A_READ_ONLY_CONNECTION, ids=CREATABLE_IDS
)
def test_appended_statements_never_reach_the_engine(
    con, statement: str, read_it_back: str
) -> None:
    """Guarantee 1 again, through the door the original payload tried to force —
    with a plain `;` instead of a quoting trick, because the trick is what died.

    Unguarded, DuckDB executes both halves of this string and the object exists;
    that is what makes the refusal below attributable to the guard.
    """
    with pytest.raises(GuardrailError, match=r"single statement"):
        con.execute(guard_query(f"SELECT amount FROM invoices; {statement}"))
    with pytest.raises(duckdb.Error):
        con.execute(read_it_back)


# MEASUREMENT NOTE for the block above, kept next to what it measures.
# Six mutations applied to THESE TESTS instead of to the guard, and four stay
# green: dropping either `match=`, reducing the engine probe to `is not None`,
# and deleting the read-it-back assertions all pass at 346. Two go red — taking
# `guard_query` out of the calls (8), which is the point of the rewrite, and
# swapping a live statement back for the dead `WRAPPER_ESCAPE` (3), which is
# red only because `test_the_engine_really_does_create_these` refuses to
# execute it. That probe is the one thing holding the other two honest; it is a
# test and not a comment for exactly that reason. Four green out of six is the
# same ratio ROUND 6 measured, and the same conclusion: nothing covers a test
# file except having run the mutations.


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


# =========================================================================== #
# ROUND 5 — what the guard refused that it should not have, and what it said
# while refusing.
#
# Like ROUND 4, this did not come from an attack. It came from the same blind
# audit, which pointed at two queries a paying user writes and the guard threw
# away, plus one thing the refusal message handed back to the model:
#
#   SELECT customer_name || ' x' FROM customers
#     -->  REFUSED: Function(s) not in allow-list: ||.
#   SELECT concat(customer_name, ' x') FROM customers
#     -->  accepted
#
# The same operation, spelled two ways, decided differently — `concat` was on
# the list and `||` was not. `^` was the same omission against `pow` / `power`.
# That is the cost of enumerating spellings by hand, and it is paid by the
# product, not by an attacker: string concatenation is the most common idiom in
# a collections report.
#
#   SELECT count(*) FROM invoices; -- total
#     -->  REFUSED: Query does not parse: Parser Error: syntax error at or
#          near ";"
#          LINE 2: SELECT count(*) FROM invoices; -- total
#
# The refusal came from the LAST check, on the guard's own wrapper — the strip
# loop peeled only what was literally last, so the semicolon travelled into
# `SELECT * FROM ( … )` and broke it there. Two defects in one message: a query
# a model writes constantly was refused, and the refusal was a syntax error
# about `LINE 2` of a text the caller never wrote.
#
# Stating the severity of that second one honestly, because the first framing
# of it was wrong: this is NOT hiding the wrapper from an attacker. The repo is
# public and the wrapper is written out in `sql_guard.py`'s docstring, so there
# is nothing to hide. The cost is that the caller is a language model trying to
# self-correct, and it was handed a line number into a query it did not compose
# — feedback it can only act on by guessing.
#
# The repair replaced the text handling rather than patching it: the statement
# that gets wrapped is now printed by DuckDB from the validated tree
# (`json_deserialize_sql`), and the printed text is re-parsed and compared with
# that tree before anything is wrapped. Comments and separators are lexical —
# they never reach a tree, so they cannot come out of a print.
#
# Note what this block does NOT claim. Removing the fixed-point comparison
# leaves the whole suite green, because DuckDB 1.5.3's printer round-trips
# every payload here exactly. Only `test_r5_a_printer_that_lies_is_refused`
# turns red, and it has to fake the divergence to do it. The check is insurance
# against a future release, and insurance nobody has collected on cannot be
# proved by a passing test.
# =========================================================================== #

R5_SPELLED_AS_AN_OPERATOR = [
    ("SELECT name || ' x' AS z FROM customers", "concat_pipes"),
    ("SELECT c.name || ' — ' || i.status AS label FROM customers c "
     "JOIN v_invoices i ON c.customer_id = i.customer_id", "concat_chain"),
    ("SELECT amount ^ 2 AS p FROM invoices", "power_caret"),
]


@pytest.mark.parametrize(
    "sql,label", R5_SPELLED_AS_AN_OPERATOR, ids=[q[1] for q in R5_SPELLED_AS_AN_OPERATOR]
)
def test_r5_operator_spelling_is_not_refused(con, sql: str, label: str) -> None:
    """Guarantee 4. Executed for real first, so the test cannot be satisfied by
    a guard that accepts something DuckDB would reject anyway."""
    con.execute(f"SELECT * FROM (\n{sql}\n) AS _sanity LIMIT 1").fetchall()
    assert run_guarded(con, sql) is not None


def test_r5_operator_and_call_spelling_agree() -> None:
    """The defect was not "`||` is missing", it was that two spellings of one
    operation were decided differently. Pinning the pair is what keeps a future
    edit from re-opening it on the other side."""
    assert guard_query("SELECT concat(name, ' x') AS z FROM customers")
    assert guard_query("SELECT name || ' x' AS z FROM customers")
    assert guard_query("SELECT pow(amount, 2) AS p FROM invoices")
    assert guard_query("SELECT amount ^ 2 AS p FROM invoices")


def test_r5_the_rest_of_the_operator_family_is_still_refused() -> None:
    """Paired against the two above. The repair was to add two names, not to
    stop checking operator-named functions — a guard that had exempted the
    whole family would pass every test above and fail this one.

    Asserting the *reason* matters here: a bit shift on a text column is a type
    error in DuckDB too, so "it raised" would go green with the allow-list
    switched off entirely.
    """
    for sql, name in [
        ("SELECT amount << 2 AS z FROM invoices", "<<"),
        ("SELECT amount | 2 AS z FROM invoices", "|"),
        ("SELECT amount & 2 AS z FROM invoices", "&"),
    ]:
        with pytest.raises(GuardrailError) as exc:
            guard_query(sql)
        assert "allow-list" in str(exc.value), str(exc.value)
        assert name in str(exc.value), str(exc.value)


R5_TRAILING_NOISE = [
    ("SELECT count(*) AS n FROM invoices; -- total", "semicolon_then_line_comment"),
    ("SELECT count(*) AS n FROM invoices; /* total */", "semicolon_then_block_comment"),
    ("SELECT count(*) AS n FROM invoices;\n-- total\n", "semicolon_then_comment_line"),
    ("-- how many\nSELECT count(*) AS n FROM invoices;", "leading_comment_and_semicolon"),
]


@pytest.mark.parametrize(
    "sql,label", R5_TRAILING_NOISE, ids=[q[1] for q in R5_TRAILING_NOISE]
)
def test_r5_a_comment_after_the_semicolon_is_not_a_second_statement(
    con, sql: str, label: str
) -> None:
    guarded = guard_query(sql)
    # Executed for real: a guard that accepted the query but produced text
    # DuckDB will not run has fixed nothing.
    assert con.execute(guarded).fetchall() is not None
    # The comment is gone from what will execute — it was never in the tree.
    # Checking the output, not just the absence of an exception: a guard that
    # accepted the query and carried the comment into the wrapper would be one
    # DuckDB release away from breaking again.
    assert "--" not in guarded and "/*" not in guarded, guarded


def test_r5_stacking_behind_a_comment_is_still_two_statements() -> None:
    """The discriminator for the case above. Tolerating a trailing comment must
    not tolerate a real second statement hidden behind one — the two payloads
    differ only in what follows the comment."""
    with pytest.raises(GuardrailError, match=r"(?i)single statement"):
        guard_query("SELECT 1 AS a; -- note\n; SELECT 2 AS b")
    with pytest.raises(GuardrailError, match=r"(?i)single statement"):
        guard_query("SELECT 1 AS a; /* note */ DROP TABLE customers")


def test_r5_a_refusal_never_quotes_the_wrapper_back() -> None:
    """The message contract, separately from the false positive.

    A refusal from the guard has to be phrased in the guard's own vocabulary —
    a policy reason the caller can act on — not as a DuckDB syntax error about
    a line of the wrapper. Refusals that quote the *caller's* own text are fine
    and still happen: the model already has that text and can fix it.

    NOTE the limit of this test: it covers the guard's refusals only. Errors
    raised while *executing* the guarded query used to reach the model with the
    wrapper's line numbering, through `tools.py` and `mcp_server/server.py` —
    the same defect on the other side of the guard, left open when this test was
    written. That was closed on 2026-07-31 by `strip_wrapper_line_echo`, and it
    is pinned where it belongs: ROUND 7 of `test_sql_guard.py` for the strip
    itself, `test_tool_error_contract.py` for the two call sites. This test's
    scope did not change.
    """
    refusals = [
        "SELECT 1) AS x; DROP TABLE customers; SELECT * FROM (SELECT 1",
        "SELECT * FROM secret_table",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM evildb.main.customers",
        "SHOW ALL TABLES",
    ]
    for sql in refusals:
        with pytest.raises(GuardrailError) as exc:
            guard_query(sql)
        message = str(exc.value)
        assert "_guarded" not in message, message
        assert "LIMIT 200" not in message, message
        assert "LINE 2" not in message, message


def test_r5_a_failing_wrapper_still_does_not_quote_itself(monkeypatch) -> None:
    """Pins the message contract for a branch nothing can reach today.

    Measured: deleting the hand-off that produces this message leaves all 290
    tests green, because once the wrapped text is printed from a validated tree
    there is no payload that can make the final check fail. That makes the
    branch insurance against a future refactor putting caller text back in the
    wrapper — and insurance nobody can trigger is indistinguishable from dead
    code, so the trigger is faked here: a normalizer that returns an unbalanced
    statement. The assertion is not that it is refused, it is *how*.
    """
    monkeypatch.setattr(
        sql_guard, "_canonical_statement", lambda sql, tree: "SELECT 1) AS x"
    )
    with pytest.raises(GuardrailError) as exc:
        guard_query("SELECT * FROM customers")
    message = str(exc.value)
    assert "_guarded" not in message, message
    assert "LINE" not in message, message
    assert "wrapped as a single statement" in message, message


def test_r5_the_executed_statement_is_printed_from_the_validated_tree() -> None:
    """What the wrapper contains is no longer the caller's text.

    `count(*)` comes back as `count_star()` because that is the name the tree
    carries — which is also the name the allow-list checks. The assertion is
    that the two agree: whatever executes is what was validated.
    """
    guarded = guard_query("SELECT count(*) FROM invoices; -- total")
    assert "count_star()" in guarded, guarded
    assert "count(*)" not in guarded, guarded


def test_r5_a_printer_that_lies_is_refused(monkeypatch) -> None:
    """The fixed-point check, proved by faking the failure it exists for.

    Executing generated text is only safe while DuckDB's printer and parser
    agree. If a release ever prints something that parses differently, the
    statement that runs stops being the statement that was checked — so the
    printed text is parsed again and compared, and a mismatch is refused. This
    substitutes a printer that swaps the relation for one off the allow-list.
    """
    real_parser = sql_guard._parser

    class LyingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            result = self._inner.execute(sql, params)
            if "json_deserialize_sql" in sql:
                return _Fetchone("SELECT * FROM internal_notes")
            return result

    class _Fetchone:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return (self._value,)

    monkeypatch.setattr(
        sql_guard, "_parser", lambda: LyingConnection(real_parser())
    )
    with pytest.raises(GuardrailError, match=r"(?i)normali[sz]ed"):
        guard_query("SELECT * FROM customers")


# =========================================================================== #
# ROUND 6 — what the suite did not pin.
#
# Third block from the same blind audit that produced ROUND 4 and ROUND 5, and
# the only one of the three that found no bypass. It found the opposite: places
# where the tests above go green whether the guard is right or wrong. Three
# families, each measured before it was written.
#
# (a) THE ALLOW-LISTS WERE TESTED AS A MECHANISM, NEVER AS A POLICY. Every test
#     above proves that *something not on the list* is refused. Nothing proved
#     that the list holds the right names. Measured against the 291-test suite
#     as it stood before this block:
#
#       + read_text, read_blob, sniff_csv to ALLOWED_FUNCTIONS  -> 291 green,
#         and `SELECT * FROM read_text('/etc/passwd')` comes back guarded
#       - payments, communications from ALLOWED_RELATIONS       -> 291 green,
#         and `SELECT * FROM payments` comes back refused
#       + information_schema, pg_catalog to ALLOWED_SCHEMAS     -> 291 green
#
#     Three of the nine allow-listed relations appeared in no payload at all.
#     This is the mutation that a real pull request looks like — it *relaxes*
#     the policy instead of deleting a check — and it is the one an enumerated
#     test cannot see. What follows compares the lists against a full surface
#     (DuckDB's function catalog, the ledger's own catalog) instead of against
#     more examples.
#
# (b) THE FUNCTION PROMISED `GuardrailError` "ON ANY VIOLATION" AND FIVE INPUTS
#     ESCAPED IT. `guard_query(123)` raised AttributeError, `max_rows="5"`
#     TypeError, `max_rows=float("inf")` OverflowError — none of which the two
#     callers catch. Two more did not raise at all: `max_rows=2.9` truncated to
#     `LIMIT 2` in silence, and `max_rows=10**30` produced a LIMIT with thirty
#     zeroes, which is no row cap. And a 996-character query of the form
#     `SELECT 1+1+1+…` hit Python's stack limit inside the tree walk and left as
#     a RecursionError.
#
# (c) NOTHING ASSERTED, AS A CONTRACT, THAT THE WRAPPER CONTAINS THE VALIDATED
#     STATEMENT. A `guard_query` that threw its input away and returned
#     `SELECT * FROM (SELECT 1) AS _guarded LIMIT n` passed every one of the 18
#     `test_allowed` cases — they check the shape of the string, not what is
#     inside it. Stating the rest of that measurement, because it is the part
#     that weakens the finding: 12 other tests did turn red, and all 12 are
#     end-to-end ones that execute the guarded text and look at the rows. So the
#     property was held by the tests that happen to run queries, not by anything
#     that says it. ROUND 5 pinned one instance of it (`count(*)` must come back
#     as `count_star()`); the general form is below.
#
# MEASURED AFTER THIS BLOCK (319 tests). Twelve mutations of the guard, all red:
# poisoning the function list 1 · removing two relations 2 · adding
# `internal_notes` 35 · opening ALLOWED_SCHEMAS 3 · deleting the `sql` type check
# 3 · deleting the `max_rows` type check 3 · deleting the ceiling 2 · drifting
# the ceiling from the settings bound 1 · deleting the depth guard 1 · setting
# the depth limit to 4 (the over-restrictive direction) 101 · the `SELECT 1`
# stand-in 19 · re-opening the ROUND 4 catalog hole 9.
#
# And the number that is not flattering: of seven mutations applied to these
# tests instead of to the guard, six stay green. Deleting the reason assertions,
# emptying the loops, comparing a tree to `is not None` — the suite cannot see
# any of it. Only the one that broke the catalog probe went red, and only
# because that probe has a sanity assertion of its own. A test file is not
# covered by anything; what holds it is that the mutations above were run.
# =========================================================================== #

# --------------------------------------------------------------------------- #
# 6a. The allow-lists, checked against a surface instead of against examples.
# --------------------------------------------------------------------------- #

# The only table functions on the allow-list, and why each is there: all five
# generate rows from their arguments. None of them opens anything — that is the
# property the test below is really about, and it is stated here by name because
# DuckDB's catalog has no column for "reads a file".
GENERATOR_TABLE_FUNCTIONS = frozenset(
    {"unnest", "range", "generate_series", "repeat", "histogram"}
)


def test_r6_no_reading_table_function_is_on_the_function_allow_list() -> None:
    """The allow-list must not contain a table function that opens a resource.

    Every exfiltration payload in this file is a table function: `read_csv`,
    `read_text`, `read_parquet`, `glob`, `duckdb_settings`. So instead of adding
    three more names to a REJECTED list — which is how the list stayed
    untestable in the first place — this asks DuckDB for *every* table function
    it knows and intersects that with the allow-list. A future release that adds
    a new file reader is covered on the day it ships, and so is a pull request
    that adds one to the list by hand.

    Honest boundary: this catches table functions. A *scalar* function that
    reached outside the database would pass it. DuckDB's scalar surface holds
    two that come close — `getvariable` and `current_setting` — and neither is
    on the allow-list, but that is checked by reading, not by this test.
    """
    catalog = duckdb.connect(":memory:")
    try:
        rows = catalog.execute(
            "SELECT DISTINCT lower(function_name), function_type FROM duckdb_functions()"
        ).fetchall()
    finally:
        catalog.close()

    table_functions = {name for name, ftype in rows if ftype in ("table", "table_macro")}
    # Sanity on the probe itself: an empty or mistyped catalog query would make
    # the assertion below vacuous.
    assert "read_text" in table_functions and "glob" in table_functions

    leaked = sorted((ALLOWED_FUNCTIONS & table_functions) - GENERATOR_TABLE_FUNCTIONS)
    assert not leaked, f"table function(s) on the allow-list: {leaked}"


# The relations this suite's ledger contains that policy keeps off-limits. It is
# written out here, in the test, so that the *partition* of the ledger — what may
# be read and what may not — is stated somewhere other than in the list under
# test. See `test_r6_the_ledger_surface_is_partitioned_by_policy`.
LEDGER_OFF_LIMITS = frozenset({"internal_notes"})


def _ledger_relations(con) -> set[str]:
    """Every non-internal table and view in the throwaway ledger."""
    return {
        row[0].lower()
        for row in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE NOT internal "
            "UNION ALL "
            "SELECT view_name FROM duckdb_views() WHERE NOT internal"
        ).fetchall()
    }


def test_r6_the_ledger_surface_is_partitioned_by_policy(con) -> None:
    """Every relation the ledger holds is decided — allowed or refused.

    The first version of this test was parametrized over `ALLOWED_RELATIONS` and
    asserted each name reads. That is the same defect one level up: removing
    `payments` from the list removed the test case along with it, and the mutation
    stayed green (measured: 0 red). A list cannot pin itself. So the surface here
    is the ledger's own catalog, and the allow-list is checked *against* it.

    Honest boundary: the surface is this suite's throwaway ledger, not
    `data/generate.py`. `_build_ledger` promising to mirror the real schema is
    what carries that half, and it is a promise in a docstring, not a check.
    """
    present = _ledger_relations(con)
    assert not (ALLOWED_RELATIONS & LEDGER_OFF_LIMITS), "a relation is on both sides"
    # Nothing in the ledger is undecided, and nothing on the allow-list is absent
    # from the ledger. Removing a name from ALLOWED_RELATIONS fails here because
    # the ledger still has it and no policy line excludes it.
    assert present == ALLOWED_RELATIONS | LEDGER_OFF_LIMITS


def test_r6_every_relation_the_policy_allows_is_actually_readable(con) -> None:
    """The allow-list is a promise in both directions.

    `payments`, `communications` and `meta` were on the list and appeared in no
    payload in either suite, so the agent could have lost three relations without
    a test noticing. Each is now exercised end-to-end — guarded, then executed —
    against the ledger's own catalog rather than against the list, so this stays
    honest if the list shrinks.
    """
    for relation in sorted(_ledger_relations(con) - LEDGER_OFF_LIMITS):
        assert run_guarded(con, f"SELECT * FROM {relation}") is not None


def test_r6_every_relation_the_policy_forbids_is_refused(con) -> None:
    """The other direction, off the same catalog.

    Enumerating forbidden names in the test is what let the list drift: the suite
    only ever knew about `internal_notes`. A table added to the schema is refused
    by default here, and adding one to ALLOWED_RELATIONS turns this red.
    """
    for name in sorted(_ledger_relations(con) - ALLOWED_RELATIONS):
        with pytest.raises(GuardrailError) as exc:
            guard_query(f"SELECT * FROM {name}")
        assert name in str(exc.value), str(exc.value)


# The bare name in each of these IS allow-listed, so the relation list cannot be
# what refuses them — only the schema check can. That is what makes them
# discriminating: the pre-existing "schema" tests all used names like
# `information_schema.tables`, which are refused on `tables` alone and stay green
# with ALLOWED_SCHEMAS wide open.
R6_CATALOG_SCHEMA_WITH_ALLOWED_TABLE = [
    ("SELECT * FROM information_schema.customers", "information_schema.customers"),
    ("SELECT * FROM pg_catalog.invoices", "pg_catalog.invoices"),
    ("SELECT * FROM system.v_dso", "system.v_dso"),
]


@pytest.mark.parametrize(
    "sql,path",
    R6_CATALOG_SCHEMA_WITH_ALLOWED_TABLE,
    ids=[q[1] for q in R6_CATALOG_SCHEMA_WITH_ALLOWED_TABLE],
)
def test_r6_a_foreign_schema_is_refused_even_with_an_allow_listed_table_name(
    sql: str, path: str
) -> None:
    with pytest.raises(GuardrailError) as exc:
        guard_query(sql)
    # The refusal names the qualified path, not the bare name: a guard that said
    # "customers" here would be refusing the wrong thing, and a guard that let
    # the schema through would not be refusing at all.
    assert path in str(exc.value), str(exc.value)
    assert "allow-list" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------------- #
# 6b. The argument contract: refusals, not crashes; a cap that caps.
# --------------------------------------------------------------------------- #

R6_BAD_ARGUMENTS = [
    ((123,), {}, "must be a string", "sql_is_an_int"),
    ((None,), {}, "must be a string", "sql_is_none"),
    ((["SELECT 1"],), {}, "must be a string", "sql_is_a_list"),
    (("SELECT 1",), {"max_rows": "5"}, "max_rows", "max_rows_is_a_string"),
    (("SELECT 1",), {"max_rows": 2.9}, "max_rows", "max_rows_is_a_float"),
    (("SELECT 1",), {"max_rows": float("inf")}, "max_rows", "max_rows_is_infinity"),
    (("SELECT 1",), {"max_rows": True}, "max_rows", "max_rows_is_a_bool"),
    (("SELECT 1",), {"max_rows": 10**30}, "max_rows", "max_rows_is_astronomical"),
    (("SELECT 1",), {"max_rows": MAX_ROWS_CEILING + 1}, "max_rows", "max_rows_over_cap"),
]


@pytest.mark.parametrize(
    "args,kwargs,offender,label", R6_BAD_ARGUMENTS, ids=[a[3] for a in R6_BAD_ARGUMENTS]
)
def test_r6_a_bad_argument_is_a_refusal_not_a_crash(
    args: tuple, kwargs: dict, offender: str, label: str
) -> None:
    """`tools.py` and `mcp_server/server.py` catch `GuardrailError` and
    `duckdb.Error`. Anything else leaves the tool as a traceback instead of a
    message the model can act on, and two of these did not raise at all.

    Asserting the *reason*, not just the exception: the SQL in the `max_rows`
    cases is valid and allow-listed, so a `GuardrailError` naming anything other
    than `max_rows` would mean the payload was refused for an unrelated reason
    and the test proves nothing.
    """
    with pytest.raises(GuardrailError) as exc:
        guard_query(*args, **kwargs)
    assert offender in str(exc.value), str(exc.value)


def test_r6_the_row_cap_is_bounded_on_both_sides() -> None:
    """Paired with the block above so a lazy repair cannot pass it: refusing
    *every* max_rows would satisfy the table and break the product."""
    assert guard_query("SELECT 1", max_rows=1).endswith("LIMIT 1")
    assert guard_query("SELECT 1", max_rows=MAX_ROWS_CEILING).endswith(
        f"LIMIT {MAX_ROWS_CEILING}"
    )
    assert guard_query("SELECT 1").endswith(f"LIMIT {DEFAULT_MAX_ROWS}")


def test_r6_the_guard_ceiling_and_the_settings_bound_agree() -> None:
    """`Settings.max_rows` carries `le=10_000` and its comment says "the guard
    enforces it". That was false until the ceiling existed: the bound applied to
    the environment variable, while `plan_replay`, the MCP server and every test
    passed their own number straight into `guard_query`.

    `sql_guard` cannot import the settings — it is the security surface and
    depends on nothing in the app — so the number is written twice. This is what
    keeps the two copies equal; a comment would not.
    """
    from annotated_types import Le

    from src.core.config import Settings

    bounds = [m.le for m in Settings.model_fields["max_rows"].metadata if isinstance(m, Le)]
    assert bounds == [MAX_ROWS_CEILING], bounds


def test_r6_a_deeply_nested_query_is_refused_rather_than_crashing() -> None:
    """`SELECT 1+1+1+…` is one flat-looking line and a very deep tree.

    Measured before the fix: 494 terms (996 characters) raised `RecursionError`
    out of `guard_query`. DuckDB's own parser refuses at depth 1000, so the band
    in between was a Python crash rather than a decision — and where exactly it
    fell depended on how deep the *caller's* stack already was, which made it a
    different number under pytest than under the API.

    The message assertion is the discriminator: at some length DuckDB refuses
    the query on its own ("Max expression depth"), and a test that only checked
    for `GuardrailError` would go green with this guard's own limit deleted.
    """
    with pytest.raises(GuardrailError) as exc:
        guard_query("SELECT 1" + "+1" * 600)
    assert "nested too deeply" in str(exc.value), str(exc.value)


def test_r6_ordinary_nesting_is_not_refused(con) -> None:
    """Paired with the depth cap: a limit of 1 would satisfy the test above.

    This is a five-CTE aging report with a CASE ladder and a window function —
    deliberately worse than anything else in either suite. It reaches walk depth
    18 against a limit of 250, which is the headroom the limit was chosen for.
    """
    sql = """
        WITH base AS (
          SELECT c.customer_id, c.name, i.amount, i.status, i.due_date
          FROM customers c JOIN v_invoices i ON c.customer_id = i.customer_id
          WHERE i.status <> 'paid' AND i.amount > 0
        ),
        bucketed AS (
          SELECT customer_id, name, amount,
                 CASE WHEN date_diff('day', due_date, CURRENT_DATE) > 90 THEN '90+'
                      WHEN date_diff('day', due_date, CURRENT_DATE) > 60 THEN '61-90'
                      WHEN date_diff('day', due_date, CURRENT_DATE) > 30 THEN '31-60'
                      ELSE '0-30' END AS bucket
          FROM base
        ),
        ranked AS (
          SELECT customer_id, name, bucket, sum(amount) AS total,
                 row_number() OVER (PARTITION BY bucket ORDER BY sum(amount) DESC) AS rn
          FROM bucketed GROUP BY customer_id, name, bucket
        )
        SELECT name || ' — ' || bucket AS label, round(total, 2) AS total
        FROM ranked WHERE rn <= 5 ORDER BY bucket, total DESC
    """
    assert run_guarded(con, sql) is not None
    assert MAX_WALK_DEPTH >= 250


# --------------------------------------------------------------------------- #
# 6c. What is inside the wrapper is the statement that was validated.
# --------------------------------------------------------------------------- #

R6_FIDELITY = [
    ("SELECT * FROM customers", "bare_select"),
    ("SELECT count(*) AS n FROM invoices; -- total", "comment_and_semicolon"),
    ("SELECT name || ' x' AS z FROM customers", "operator_spelling"),
    ("SELECT * FROM main.invoices", "schema_qualified"),
    (
        "WITH overdue AS (SELECT customer_id, amount FROM v_invoices "
        "WHERE status = 'overdue') "
        "SELECT customer_id, sum(amount) AS total FROM overdue GROUP BY customer_id",
        "cte_aggregation",
    ),
    (
        "SELECT c.name, i.amount FROM customers c "
        "JOIN v_invoices i ON c.customer_id = i.customer_id ORDER BY i.amount DESC",
        "join_with_order",
    ),
    ("SELECT extract(year FROM issue_date) AS y FROM invoices", "extract_idiom"),
]


def _unwrap(guarded: str) -> str:
    """Return the statement the guard put inside its own wrapper."""
    prefix, suffix = "SELECT * FROM (\n", "\n) AS _guarded LIMIT "
    assert guarded.startswith(prefix), guarded
    inner, marker, limit = guarded[len(prefix) :].rpartition(suffix)
    assert marker == suffix, guarded
    assert limit.isdigit(), guarded
    return inner


@pytest.mark.parametrize("sql,label", R6_FIDELITY, ids=[q[1] for q in R6_FIDELITY])
def test_r6_the_wrapper_contains_the_statement_that_was_validated(
    sql: str, label: str
) -> None:
    """The general form of the ROUND 5 `count_star()` assertion.

    The guard now executes text it generated itself, so "it returned something
    that parses and is capped" is not enough: a `guard_query` that threw the
    input away and returned `SELECT * FROM (SELECT 1) AS _guarded LIMIT 200`
    satisfied every other accepted-query test in both suites. What is asserted
    here is equality of *trees* — the wrapped statement parses to the same tree
    the allow-lists were run against, once the byte offsets that any reprint
    shifts are dropped.

    Comparing trees rather than text is deliberate: the text legitimately
    changes (`count(*)` prints as `count_star()`, comments disappear), and
    asserting on text would make this a test of DuckDB's printer.
    """
    guarded = guard_query(sql)
    validated = sql_guard._without_locations(sql_guard._syntax_tree(sql.strip()))
    executed = sql_guard._without_locations(sql_guard._syntax_tree(_unwrap(guarded)))
    assert executed == validated


def test_r6_a_guard_that_ignored_its_input_would_fail_the_fidelity_test() -> None:
    """The fidelity test's own discriminator.

    It asserts equality against a tree it computes from the input, so it is only
    meaningful if a wrapper carrying *different* SQL is unequal. Cheap to state,
    and it is the assertion the whole block above rests on.
    """
    stand_in = f"SELECT * FROM (\nSELECT 1\n) AS _guarded LIMIT {DEFAULT_MAX_ROWS}"
    validated = sql_guard._without_locations(
        sql_guard._syntax_tree("SELECT * FROM customers")
    )
    executed = sql_guard._without_locations(sql_guard._syntax_tree(_unwrap(stand_in)))
    assert executed != validated

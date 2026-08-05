"""Guardrail for the text-to-SQL tool — the priority security surface.

The agent may *only* run read-only analytical queries against a fixed set of
relations. This module is the prompt-layer filter, paired with a hardened
DuckDB connection (see ``ledger.py``).

Why this validates through DuckDB's own parser (ADR-022)
--------------------------------------------------------
The first version of this guard was a hand-written string scanner: it stripped
comments, masked string literals, split on top-level ``;`` and matched relation
names with a regex. Every one of its holes was the same hole — **the scanner
and the executor disagreed about where a string ended.** DuckDB understands
dollar-quoting (``$a$…$a$``), escape strings (``e'\\''``) and quoted identifiers
containing apostrophes; the scanner understood none of them. One unbalanced
quote shifted the parity of everything after it, so the checks ran on text that
did not resemble what DuckDB would execute. That let a query read the internal
catalog, read tables outside the allow-list, and — by closing the guard's own
``SELECT * FROM (…)`` wrapper early — smuggle extra statements past the
single-statement rule and create objects on a ``read_only=True`` connection.

A scanner can be patched construct by construct forever and stay one DuckDB
release behind. So it was replaced: the statement is now parsed by **the same
parser that will execute it**, and the checks run on the resulting syntax tree.
Parser-vs-executor disagreement is not reduced here, it is structurally
impossible.

Design rules (do not relax these to "make a query pass" — fix the query):

* exactly one statement, established by the parser rather than by counting
  semicolons in text;
* it must be a ``SELECT`` / ``WITH`` read query — enforced by the fact that
  DuckDB refuses to serialize anything else as a select statement;
* every table in the tree must be in the allow-list (CTE names excepted), and a
  name qualified with a *catalog* (``db.main.t``) is refused outright — one
  database is open, none can be attached, so no legitimate query needs one;
* every function in the tree must be in the allow-list. A CTE name **never**
  exempts a function: DuckDB resolves ``name(`` to a function no matter what a
  CTE is called, so allowing that would let ``WITH duckdb_settings AS (…)``
  whitelist ``duckdb_settings()``;
* what gets executed is **printed by the parser from the validated tree**, not
  copied from the caller's text (see below);
* the result is wrapped in an outer ``LIMIT`` so the model can never dump the
  whole ledger — and the wrapped text is re-parsed to prove it is still one
  statement.

Why the executed text is regenerated, not the caller's
------------------------------------------------------
The guard validates a *tree* and used to execute a *string* — the caller's
string, pasted into ``SELECT * FROM (…) LIMIT n``. Those are two different
artefacts, and everything between them was handled by hand: a loop that peeled
trailing separators off the text so the wrapper would still parse. It peeled
only what was literally last, so ``SELECT count(*) FROM invoices; -- total``
reached the wrapper with a semicolon inside the parentheses and died there, on
a DuckDB syntax error that quoted the wrapper's own second line back to the
model. A trailing comment is something a language model writes constantly.

So the text is no longer repaired: ``json_deserialize_sql`` asks DuckDB to
print the statement back from the tree that just passed every check, and *that*
is what gets wrapped. Comments, trailing separators and exotic quoting do not
survive a print — they were never in the tree. The printed text is then parsed
again and its tree compared with the validated one; a mismatch is refused
rather than executed, so a printer bug in some future DuckDB release costs a
query instead of becoming a difference between what was checked and what runs.
The caller's text now reaches nothing but the parser.

That rule covers what the guard *refuses*. What it executes can still fail in
the binder, and DuckDB then quotes the wrapper's own line numbering back at
whoever asked — so the same rule is applied on the way out, by
``strip_wrapper_line_echo`` at the bottom of this module. Both tool surfaces
call it before handing a failure to the model.

What each layer actually protects. ``read_only=True`` protects *integrity*; it
stops writes. It does **not** protect *confidentiality*: a read that reaches
outside the ledger is still a read, and reading is what an LLM-composed query
does. Confidentiality is held by this filter plus ``enable_external_access``
being off on the connection. For two releases this module claimed "defense in
depth" while the confidentiality half was a single layer.
"""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from typing import Any

import duckdb

# Relations the agent is allowed to read: the dimensions, the facts, and the
# analytics views built by data/generate.py. Keep in sync with the schema.
ALLOWED_RELATIONS: frozenset[str] = frozenset(
    {
        # dimensions
        "payment_profiles",
        "customers",
        "meta",
        # facts
        "invoices",
        "payments",
        "communications",
        # analytics views (prefer these)
        "v_invoices",
        "v_customer_ar",
        "v_dso",
    }
)

# Read-safe functions the agent may call, in the CANONICAL names DuckDB's parser
# reports — which are not always what was typed: `count(*)` arrives as
# `count_star`, `extract(year FROM d)` as `date_part`, `a + b` as `+`, and
# `INTERVAL 30 DAY` expands to `to_days` and `trunc` (measured on DuckDB 1.5.3 —
# which is why names nobody types are on this list). Checking canonical names is
# the point: an attacker cannot dodge the list by choosing a different spelling
# of the same function.
#
# This is an ALLOW-list on purpose. A deny-list of dangerous functions is blind
# to everything nobody remembered to add, and DuckDB ships new table functions
# every release. Anything absent is rejected with a named error, so a gap costs
# a query, not the ledger. (ADR-022 — grow this list when a legitimate query is
# refused; that is the designed failure direction.)
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # aggregates
        "count", "count_star", "sum", "avg", "mean", "min", "max", "median",
        "mode", "product", "stddev", "stddev_pop", "stddev_samp", "var_pop",
        "var_samp", "variance", "quantile", "quantile_cont", "quantile_disc",
        "approx_count_distinct", "string_agg", "group_concat", "array_agg",
        "list", "first", "last", "any_value", "bool_and", "bool_or", "corr",
        "covar_pop", "covar_samp", "count_if", "histogram", "arg_min", "arg_max",
        "sumkahan", "favg", "fsum",
        # window
        "row_number", "rank", "dense_rank", "percent_rank", "cume_dist",
        "ntile", "lag", "lead", "first_value", "last_value", "nth_value",
        # math
        "abs", "round", "ceil", "ceiling", "floor", "sqrt", "pow", "power",
        "exp", "ln", "log", "log10", "log2", "sign", "greatest", "least",
        "trunc", "mod", "nextafter", "cbrt", "gcd", "lcm", "add", "subtract",
        "multiply", "divide", "//",
        # string
        "lower", "upper", "length", "len", "trim", "ltrim", "rtrim", "substr",
        "substring", "array_slice", "concat", "concat_ws", "replace",
        "split_part", "string_split", "str_split", "starts_with", "ends_with",
        "contains", "position", "instr", "left", "right", "lpad", "rpad",
        "repeat", "reverse", "regexp_matches", "regexp_replace",
        "regexp_extract", "regexp_full_match", "format", "printf", "initcap",
        "nfc_normalize", "prefix", "suffix", "like_escape", "ilike_escape",
        # date / time
        "date_trunc", "datetrunc", "date_part", "datepart", "date_diff",
        "datediff", "date_add", "age", "epoch", "epoch_ms", "strftime",
        "strptime", "make_date", "make_time", "make_timestamp", "last_day",
        "monthname", "dayname", "year", "month", "day", "hour", "minute",
        "second", "millisecond", "microsecond", "dayofweek", "dayofmonth",
        "dayofyear", "weekofyear", "week", "isodow", "quarter", "century",
        "decade", "era", "time_bucket", "to_timestamp", "julian",
        "current_date", "current_timestamp", "get_current_timestamp", "today",
        "current_localtimestamp", "date_sub", "now",
        # conditional / null handling / casts
        "coalesce", "ifnull", "nullif", "nvl", "if", "typeof", "try_cast",
        "try_strptime", "case",
        # comparison / boolean operators the parser reports as functions
        "and", "or", "not", "=", "!=", "<>", "<", "<=", ">", ">=", "+", "-",
        "*", "/", "%", "~~", "!~~", "~~*", "!~~*",
        # `||` is string concatenation — the single most common idiom in a
        # collections report ("name || ' — ' || status") and it was
        # refused while `concat()`, the same operation spelled as a call, was
        # allowed. `^` is the operator spelling of `pow` / `power`, both of
        # which are listed a few lines up. Neither adds capability: the whole
        # operator-named family in DuckDB's catalog is `function_type =
        # 'scalar'` (28 names, measured on 1.5.3), so none of them can read a
        # file or reach the catalog. The rest of that family — bit shifts,
        # array distance, JSON arrows — stays off the list because no
        # receivables query needs it, and refusal is the cheap direction.
        "||", "^",
        # list / struct helpers used by analytical SQL
        "to_days", "to_months", "to_years", "to_hours", "to_minutes",
        "to_seconds", "to_weeks", "to_milliseconds", "to_microseconds",
        "char_length", "character_length", "bit_length", "octet_length",
        "unnest", "range", "generate_series", "list_value", "struct_pack",
        "list_contains", "list_sort", "array_length", "row",
    }
)

# The ledger's own schemas. A two-part name qualified with anything else —
# `information_schema.tables`, `pg_catalog.pg_tables` — is reaching for the
# catalog, not for the data, and is refused by name.
#
# This list only ever sees the SECOND part of a name. In a three-part name the
# database goes to `catalog_name` and `main` goes to `schema_name`, so such a
# name clears this list while saying nothing about which database it reads.
# That half is not this list's job: `_collect` refuses catalog qualification
# outright.
ALLOWED_SCHEMAS: frozenset[str] = frozenset({"main", "memory", "temp"})

DEFAULT_MAX_ROWS = 200

# Upper bound on the row cap a caller may ask for. `Settings.max_rows` already
# carries `le=10_000`, but that only bounds the *environment variable*: every
# other caller of `guard_query` — `plan_replay`, the MCP server, a test — passes
# the number straight through, and the guard used to format whatever arrived
# into the LIMIT. `max_rows=10**30` produced `LIMIT 1000…000`, i.e. no cap at
# all, which is exactly what the outer LIMIT exists to prevent.
#
# The number is duplicated in `core/config.py` on purpose: this module is the
# security surface and imports nothing from the app, so it cannot read the
# settings. The duplication is held by a test that compares the two bounds
# rather than by a comment asking the next person to remember.
MAX_ROWS_CEILING = 10_000

# How deep `_collect` may recurse before refusing the query. The walk descends
# one frame per dict and one per list, so a tree level costs about two frames.
#
# Without this, a deeply nested expression did not get refused — it *crashed*.
# `SELECT 1+1+1+…` with 494 terms (996 characters, well inside anything a model
# emits) raised `RecursionError` out of `guard_query`, past callers that catch
# `GuardrailError` and `duckdb.Error` and nothing else. DuckDB's own parser has
# a depth limit of its own ("Max expression depth"), but it only trips at 1000,
# so the whole 494–999 band was a Python crash instead of a decision. 494 is
# also not a fixed number: it is whatever is left of the interpreter's stack
# when `guard_query` is called, so the same query crashed at different lengths
# depending on how deep the caller already was.
#
# 250 frames against a measured worst case of **18** across every payload the
# two guard suites accept, plus a five-CTE aging report with window functions
# and a CASE ladder written to be worse than anything in them (also 18 — tree
# depth follows nesting, not size). Thirteen times the deepest real query, and
# still ~750 frames below the interpreter's default limit of 1000.
MAX_WALK_DEPTH = 250

# Parsing needs a connection, but never a *data* connection: this one is
# in-memory, empty, and hardened the same way the ledger is. It only ever sees
# `json_serialize_sql`, which parses text and returns a tree — it does not plan,
# bind or execute the statement under test.
_PARSER_CONFIG: dict[str, str] = {
    "enable_external_access": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "allow_community_extensions": "false",
}
_parser_lock = threading.Lock()
_parser_con: duckdb.DuckDBPyConnection | None = None


class GuardrailError(ValueError):
    """Raised when a query violates the read-only / allow-list policy."""


def _parser() -> duckdb.DuckDBPyConnection:
    global _parser_con
    if _parser_con is None:
        _parser_con = duckdb.connect(":memory:", config=dict(_PARSER_CONFIG))
    return _parser_con


def _single_statement(sql: str) -> None:
    """Reject anything that is not exactly one statement, per the parser.

    Counting semicolons in text is what the previous implementation did, and it
    is defeated by any quoting construct the counter does not model. The parser
    cannot be lied to about its own statement boundaries.
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.ParserException as exc:
        raise GuardrailError(f"Query does not parse: {exc}") from exc
    if len(statements) != 1:
        raise GuardrailError(
            f"Only a single statement is allowed (parsed {len(statements)})."
        )


def _syntax_tree(sql: str) -> dict[str, Any]:
    """Parse `sql` to a syntax tree, or reject it.

    ``json_serialize_sql`` only serializes SELECT statements. Every other
    statement kind — INSERT, CREATE, PRAGMA, ATTACH, COPY, SET, PREPARE,
    DELETE — comes back as an error, which is exactly the read-only check, made
    by the parser instead of by a keyword list that has to guess.
    """
    with _parser_lock:
        try:
            raw = _parser().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()
        except duckdb.Error as exc:
            raise GuardrailError(f"Query does not parse: {exc}") from exc
    if not raw or not raw[0]:
        raise GuardrailError("Query could not be parsed.")
    tree = json.loads(raw[0])
    if tree.get("error"):
        raise GuardrailError(
            "Only read-only SELECT / WITH queries are allowed "
            f"({tree.get('error_type') or 'not a select statement'})."
        )
    return tree


def _statement_text(sql: str) -> str:
    """The exact text handed to the parser — the one owner of that decision.

    It exists because there are entry points that must agree on it
    (`statement_identity`, and `_statement_and_tree` for everything else), and a
    second copy of `.strip()` is a second thing to keep in step. Until 2026-08-05
    this sentence named `guard_query` as a caller and `guard_query` kept its own
    inline `.strip()`; both spellings did the same thing, which is why nothing
    caught it. Measured 2026-08-01, before this was shared: with
    `statement_identity` reading the raw text, a leading `\\x0b` made it return "no
    opinion" while the guard stripped the same character and ran the statement — so
    a query that *executed* fell back to the text key, restoring the literal
    collision the tree key exists to prevent.
    """
    return sql.strip()


def _statement_and_tree(sql: str) -> tuple[str, dict[str, Any]]:
    """The text handed to the parser and the tree it produced — one statement.

    Three steps that only mean anything in this order: normalise the text once
    (`_statement_text`), refuse anything that is not exactly one statement, then
    parse. Written once because there are now three entry points that need it,
    and the two added on 2026-08-05 (`relations_read`, `time_literals`) are
    *facts about a statement* — asking them about text that holds none has to be
    an error, not the answer "no relations, no dates". Measured while writing
    their tests: `json_serialize_sql('')` returns ``{"statements": []}`` with no
    error at all, so without the count check the empty string reports itself as
    a clean statement that reads nothing. A vacuous yes is precisely the family
    of defect this session exists to close.

    `guard_query` had its own inline copy of the first two steps until today,
    while `_statement_text` below claimed in prose to be "the one owner" and
    named `guard_query` as one of its callers. It was not one. Identical
    behaviour, false sentence — the ADR-014 defect in its documentation-only
    form (R1-C14: a docstring saying "single owner" is not a single owner).
    """
    text = _statement_text(sql)
    _single_statement(text)
    return text, _syntax_tree(text)


def statement_identity(sql: str) -> str | None:
    """A canonical identity for `sql`, or `None` when it is not a parseable SELECT.

    Two texts get the same identity **iff DuckDB parses them into the same
    statement**: indentation, keyword case, comments and a trailing separator are
    lexical noise that never reaches the tree, while anything inside a string
    literal does reach it and therefore separates. `query_location` (a byte offset
    into the text the tree came from) is dropped for the same reason it is dropped
    in `_canonical_statement` — it moves with whitespace even though the statement
    did not.

    This exists because callers outside the guard need to ask *"is this the same
    query I already ran?"*, and the only honest answer comes from the parser. A
    text comparison, however normalized, either separates statements that are the
    same or — worse, measured 2026-07-31 — merges statements that differ only
    inside a literal. `None` means "no opinion": the caller falls back to text,
    which is safe precisely because a statement that does not parse never runs.

    It errs on the side of *separating*: the tree keeps identifiers as they were
    typed, so `FROM customers` and `FROM CUSTOMERS` — the same relation to the
    binder — get different identities (measured 2026-08-01). For a dedup caller
    that costs one repeated call; the opposite error costs a caller the wrong
    rows, so the asymmetry is deliberate. It is not a normalizer for anything that
    needs semantic equality.
    """
    try:
        tree = _syntax_tree(_statement_text(sql))
    except GuardrailError:
        return None
    return json.dumps(_without_locations(tree), sort_keys=True, default=str)


def _without_locations(node: Any) -> Any:
    """Copy of `node` with every `query_location` dropped.

    Those fields are byte offsets into the text the tree came from, so they
    shift whenever the text is reprinted even though nothing about the
    statement changed. They are the only field that differs across a print /
    re-parse round trip (measured over the whole accepted corpus of both guard
    suites: 27 of 27 trees identical once these are removed), which is why the
    fixed-point check can afford to be an exact comparison of everything else.
    """
    if isinstance(node, dict):
        return {
            key: _without_locations(value)
            for key, value in node.items()
            if key != "query_location"
        }
    if isinstance(node, list):
        return [_without_locations(value) for value in node]
    return node


def _canonical_statement(sql: str, tree: dict[str, Any]) -> str:
    """Return the statement as DuckDB prints it back from `tree`.

    The round trip is what removes comments and trailing separators: they are
    lexical noise that never reached the tree, so they cannot come out of a
    print. `sql` is re-serialized here rather than the already-parsed `tree`
    being sent back, so that the printed text is derived by DuckDB from its own
    serialization in one call — one extra parse per query, on a connection that
    holds no data.

    The result is parsed again and compared with the tree that passed the
    allow-lists. That comparison is the entire safety argument for executing
    generated text instead of the caller's: if DuckDB's printer and parser ever
    disagree, the query is refused rather than run, so the statement that
    executes is always the statement that was checked.
    """
    with _parser_lock:
        try:
            raw = (
                _parser()
                .execute("SELECT json_deserialize_sql(json_serialize_sql(?))", [sql])
                .fetchone()
            )
        except duckdb.Error as exc:
            raise GuardrailError(f"Query could not be normalized: {exc}") from exc
    if not raw or not raw[0]:
        raise GuardrailError("Query could not be normalized.")
    canonical = str(raw[0])
    if _without_locations(_syntax_tree(canonical)) != _without_locations(tree):
        raise GuardrailError(
            "Query could not be normalized: the parser did not reproduce the "
            "statement that was validated."
        )
    return canonical


def _collect(
    node: Any,
    tables: set[str],
    functions: set[str],
    pseudo: set[str],
    scope: frozenset[str] = frozenset(),
    depth: int = 0,
) -> None:
    """Walk the tree, resolving every table and function name *in its own scope*.

    Three properties this has to hold, each learned from a break:

    **Any node carrying a ``table_name`` is a relation reference.** Not just
    ``BASE_TABLE``. ``SHOW``/``DESCRIBE`` parse as select statements whose source
    is a ``SHOW_REF`` — same field, different node type — so keying the check on
    the type let ``SHOW ALL TABLES`` enumerate the whole catalog past two empty
    allow-lists. Read the field wherever it appears; classify afterwards.

    **CTE names are scoped, not global.** ``scope`` extends only into the subtree
    of the node that declares the CTE. Collecting every CTE name into one flat
    set let a CTE declared inside a nested subquery exempt that table name in a
    *sibling* branch, where DuckDB resolves it to the real table.

    **A schema-qualified name is never satisfied by a CTE.** CTEs cannot be
    schema-qualified, so ``main.internal_notes`` must clear the allow-list on its
    own. Comparing a synthesised ``"schema.table"`` string against CTE names was
    defeatable by *quoting a dot into a CTE name* — ``WITH "main.internal_notes"
    AS (…)``. Qualified references now skip the CTE set entirely instead of
    being compared against it.

    **A qualified name is read in all of its parts.** The tree carries three
    fields — ``catalog_name``, ``schema_name``, ``table_name`` — and this walk
    read two of them. In a three-part name the database lands in
    ``catalog_name`` and ``main`` lands in ``schema_name``, so the schema branch
    saw an allow-listed schema and only the bare name was ever checked:
    ``evildb.main.customers`` and ``"/tmp/other.duckdb".main.customers`` were
    accepted. Reading two thirds of a name is the same class of bug as keying
    the check on the node type — the field is there and the check did not look
    at it.

    **The walk is bounded.** Recursion depth is a property of the *input*, and
    the input is attacker-controlled text, so the natural stopping point is the
    interpreter's stack limit — a `RecursionError` that no caller catches. The
    limit is stated here instead, and a query that exceeds it is refused with a
    reason like any other violation.
    """
    if depth > MAX_WALK_DEPTH:
        raise GuardrailError(
            f"Query is nested too deeply (walk limit {MAX_WALK_DEPTH})."
        )
    if isinstance(node, dict):
        # CTEs declared here are in scope for this subtree only — and NOT inside
        # their own definitions. Walking the definitions with the full set in
        # scope was a hole in both directions:
        #
        #   WITH leak AS (SELECT max(secret) FROM internal_notes),
        #        internal_notes AS (SELECT 1) SELECT * FROM leak
        #
        # exempted a *forward-declared* name that SQL has not bound yet, and
        #
        #   WITH internal_notes AS (SELECT * FROM internal_notes) SELECT …
        #
        # exempted a CTE's own name inside its own body. Both read the real
        # table. So each definition is validated against only the CTEs declared
        # BEFORE it, which is what SQL actually binds.
        #
        # A CTE's own name is never in scope in its own body, `RECURSIVE` or
        # not. That is not conservatism: measured on DuckDB 1.5.3, the anchor
        # term of `WITH RECURSIVE internal_notes AS (SELECT secret FROM
        # internal_notes UNION ALL …)` binds to the base table and returns its
        # rows. The cost is that genuinely recursive CTEs are refused (the
        # recursive term references the CTE name); the refusal is visible and
        # a leak would not be. See ADR-022.
        cte_map = node.get("cte_map")
        declared_here: list[tuple[str, Any]] = []
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map") or []:
                if isinstance(entry, dict) and entry.get("key"):
                    declared_here.append((str(entry["key"]).lower(), entry))

        if declared_here:
            visible = set(scope)
            for cte_name, entry in declared_here:
                # Validated against the CTEs declared before it — not itself.
                _collect(
                    entry, tables, functions, pseudo, frozenset(visible), depth + 1
                )
                visible.add(cte_name)
            scope = frozenset(visible)

        kind = str(node.get("type") or "")
        if kind == "TABLE_FUNCTION":
            fn = (node.get("function") or {}).get("function_name")
            if fn:
                functions.add(str(fn).lower())
        if node.get("function_name"):
            functions.add(str(node["function_name"]).lower())

        # Presence of the field, not truthiness of its value: `SHOW TABLES FROM
        # main` is a SHOW_REF whose table_name is the empty string, and a
        # truthiness test skips it into the clear.
        if "table_name" in node:
            bare = str(node.get("table_name") or "").lower()
            schema = str(node.get("schema_name") or "").lower()
            catalog = str(node.get("catalog_name") or "").lower()
            if kind != "BASE_TABLE":
                # SHOW_REF covers two different things. A catalog listing
                # (`SHOW ALL TABLES`, `SHOW DATABASES`) has no query sub-tree
                # and enumerates the catalog — never legitimate. `DESCRIBE x` /
                # `SUMMARIZE x` carry their target as a real sub-tree, so the
                # relation allow-list checks it like any other reference and
                # `DESCRIBE internal_notes` is refused on its own merits.
                if node.get("query") is None:
                    pseudo.add(kind.lower() or "unknown")
            elif not bare:
                pseudo.add(kind.lower() or "unknown")
            elif catalog:
                # Three-part name. There is exactly one database open on the
                # ledger connection and `ATTACH` is refused upstream, so no
                # legitimate query needs to name a catalog at all — the whole
                # form is refused rather than matched against a list of catalog
                # names, which would only track the ledger's file name. The full
                # path goes into the error so the refusal says which database
                # was asked for. As with the schema branch, `scope` is not
                # consulted: a CTE cannot be catalog-qualified.
                tables.add(".".join(part for part in (catalog, schema, bare) if part))
            elif schema and schema not in ALLOWED_SCHEMAS:
                tables.add(f"{schema}.{bare}")
            elif schema:
                # Qualified against the ledger's own schema: check the bare name,
                # and deliberately do NOT consult `scope`.
                tables.add(bare)
            elif bare not in scope:
                tables.add(bare)

        for key, value in node.items():
            # `cte_map` was already walked above, each definition under its own
            # narrower scope. Re-walking it here with the full scope would put
            # every CTE name back in view inside its own body and undo that.
            if key == "cte_map" and declared_here:
                continue
            _collect(value, tables, functions, pseudo, scope, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect(value, tables, functions, pseudo, scope, depth + 1)


def relations_read(sql: str) -> frozenset[str]:
    """Every relation the statement resolves against the catalog.

    The same walk `guard_query` validates with, asked for its intermediate
    result instead of its verdict — CTE names already resolved away, qualified
    names already assembled. It is a *fact* about the statement, not a policy:
    the guard uses it to decide what is allowed, and the plan-cache uses it to
    decide what a replay can honestly claim (an empty set means the statement
    reads no ledger data at all, so re-running it proves nothing about the
    ledger — see `plan_cache.freshness_violation`).

    Deliberately not a second walk: a copy of `_collect` would drift from the
    guard's scoping rules exactly where those rules are subtle, which is how
    the CTE holes in ADR-022 happened in the first place. Parsing goes through
    `_statement_and_tree` for the same reason — one owner for text, count and
    parse, so this can never disagree with what `guard_query` validated.

    Raises `GuardrailError` if `sql` is not exactly one read-only SELECT, so a
    caller that has not run it through `guard_query` still cannot get a silent
    answer — and neither can a caller that hands it no statement at all.
    """
    _text, tree = _statement_and_tree(sql)
    tables: set[str] = set()
    functions: set[str] = set()
    pseudo: set[str] = set()
    _collect(tree, tables, functions, pseudo)
    return frozenset(tables)


@lru_cache(maxsize=1)
def _expression_frame() -> str:
    """A serialized ``SELECT 1``, used as a frame to print one expression back.

    DuckDB will only print a *statement*, so an expression is spliced into this
    skeleton's select list and deserialized. Round-tripping through the engine's
    own printer is the only way to get text that means what the node means; any
    reconstruction written here would be a second SQL printer, and a printer
    that disagrees with the parser is the class of defect ADR-022 exists for.
    """
    with _parser_lock:
        raw = _parser().execute("SELECT json_serialize_sql('SELECT 1')").fetchone()
    if not raw or not raw[0]:  # pragma: no cover - a constant query that parsed
        raise GuardrailError("Parser could not serialize its own skeleton.")
    return str(raw[0])


@lru_cache(maxsize=1)
def _moving_functions() -> frozenset[str]:
    """Functions whose value is not fixed at write time — read from the catalog.

    `now()`, `current_date`, `today()` are ``CONSISTENT_WITHIN_QUERY``;
    `random()` is ``VOLATILE``; `make_date`, `strptime`, `date_trunc` are
    ``CONSISTENT``. That column is the engine's own statement about which
    expressions move, so the set is derived rather than listed — a hand-written
    list of clock functions is the enumerated guard this module keeps being bitten
    by, and it would go stale the first time DuckDB adds one.

    Unknown stability (``NULL``) is deliberately **not** treated as moving: an
    expression it appears in stays a frozen-date candidate, which costs a cache
    hit rather than a false freshness claim. Measured 2026-08-05: no scalar
    function has a NULL stability, so the branch is insurance.
    """
    with _parser_lock:
        rows = (
            _parser()
            .execute(
                "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
                "WHERE stability IS NOT NULL AND stability <> 'CONSISTENT'"
            )
            .fetchall()
        )
    return frozenset(str(row[0]) for row in rows)


@lru_cache(maxsize=1)
def _aggregate_functions() -> frozenset[str]:
    """Aggregate names, from the catalog — see `constant_projections`."""
    with _parser_lock:
        rows = (
            _parser()
            .execute(
                "SELECT DISTINCT lower(function_name) FROM duckdb_functions() "
                "WHERE function_type = 'aggregate'"
            )
            .fetchall()
        )
    return frozenset(str(row[0]) for row in rows)


@lru_cache(maxsize=1)
def _temporal_types() -> frozenset[str]:
    """Every type name in the catalog's ``DATETIME`` category."""
    with _parser_lock:
        rows = (
            _parser()
            .execute(
                "SELECT DISTINCT lower(type_name) FROM duckdb_types() "
                "WHERE type_category = 'DATETIME'"
            )
            .fetchall()
        )
    return frozenset(str(row[0]) for row in rows)


@lru_cache(maxsize=1)
def _point_in_time_types() -> frozenset[str]:
    """The temporal types that denote a *point*, not a duration.

    The whole ``DATETIME`` category minus ``interval`` — the one member that is
    relative. `now() - INTERVAL 30 DAY` has to stay cacheable, so the exception
    is named here with its reason rather than the set being written out by hand
    (measured 2026-08-05: 14 type names in the category, `interval` the only
    relative one). The full set is still needed by `_moves_with_clock`, where an
    interval derived from the clock does carry the clock forward.
    """
    return _temporal_types() - {"interval"}


def _expression_text(node: Any) -> str | None:
    """`SELECT <node>` as DuckDB prints it, or `None` if it is not an expression."""
    frame = json.loads(_expression_frame())
    frame["statements"][0]["node"]["select_list"] = [node]
    try:
        with _parser_lock:
            raw = (
                _parser()
                .execute("SELECT json_deserialize_sql(?)", [json.dumps(frame)])
                .fetchone()
            )
    except duckdb.Error:
        return None  # not an expression node — the caller walks into its children
    return str(raw[0]) if raw and raw[0] else None


def _bound_type(select_text: str) -> str | None:
    """The type `select_text` binds to, or `None` when it does not bind alone.

    ``DESCRIBE`` runs the **binder** and not the executor: it answers the type
    without evaluating, so probing `range(1000000000)` costs nothing and a
    hostile expression cannot turn this into work. Failure to bind is the
    signal, not an error to report — an expression that needs a column is by
    definition not fixed at write time, which is exactly what the callers want
    to know.
    """
    try:
        with _parser_lock:
            rows = _parser().execute(f"DESCRIBE {select_text}").fetchall()
    except duckdb.Error:
        return None
    return str(rows[0][1]).lower() if rows else None


def _walk_expressions(tree: Any, visit: Any, depth: int = 0) -> None:
    """Depth-first walk over dict/list nodes, calling `visit(node)` on each dict.

    `visit` returns `True` to stop the walk descending into that node. Bounded
    like `_collect`, and for the same reason: recursion depth is a property of
    attacker-controlled input, so the alternative stopping point is a
    `RecursionError` no caller catches.
    """
    if depth > MAX_WALK_DEPTH:
        raise GuardrailError(f"Query is nested too deeply (walk limit {MAX_WALK_DEPTH}).")
    if isinstance(tree, dict):
        if visit(tree):
            return
        for child in tree.values():
            _walk_expressions(child, visit, depth + 1)
    elif isinstance(tree, list):
        for child in tree:
            _walk_expressions(child, visit, depth + 1)


def _function_names(node: Any) -> set[str]:
    names: set[str] = set()

    def visit(candidate: dict[str, Any]) -> bool:
        name = candidate.get("function_name")
        if name:
            names.add(str(name).lower())
        return False

    _walk_expressions(node, visit)
    return names


def _child_nodes(node: dict[str, Any], skip: frozenset[str] = frozenset()) -> list[Any]:
    """The dict children of `node`, through lists, minus the named keys."""
    children: list[Any] = []
    for key, value in node.items():
        if key in skip:
            continue
        if isinstance(value, dict):
            children.append(value)
        elif isinstance(value, list):
            children.extend(item for item in value if isinstance(item, dict))
    return children


def _is_clock(node: dict[str, Any]) -> bool:
    """Whether this node *is* the clock — under either spelling.

    `now()` and `today()` are functions the catalog marks as moving.
    `current_date` and `current_timestamp` are **not**: DuckDB parses them as a
    column reference and the binder resolves the name later, and
    `current_timestamp` is not even in ``duckdb_functions()`` (measured
    2026-08-05). So a bare identifier that still binds with no table in scope is
    taken as a built-in, which is what it can only be.
    """
    name = node.get("function_name")
    if name and str(name).lower() in _moving_functions():
        return True
    if "column_names" in node:
        text = _expression_text(node)
        return text is not None and _bound_type(text) is not None
    return False


def _moves_with_clock(node: dict[str, Any], depth: int = 0) -> bool:
    """Whether this expression's value follows the clock.

    Not *"does the clock appear anywhere inside"* — that was the first version,
    and a blind pass broke it in one line: `make_date(2026, 8, 1 + (current_date
    - current_date))` mentions the clock, cancels it out, and pins 2026-08-01
    forever. Mentioning is not depending. Fixing a false refusal by adding a
    blanket veto is how a guard against noise becomes a channel for silence.

    So the clock has to *carry its value out*: it counts only through steps that
    are themselves temporal. `now() - INTERVAL 30 DAY` stays a moving timestamp
    at every step; `current_date - current_date` collapses to an INTEGER, and an
    integer is not a time the clock is still deciding.

    The stated limit: a clock reading that leaves the temporal types and comes
    back — `make_date(year(current_date), 8, 1)` — is read as fixed and its plan
    is refused. That costs a cache hit for a real question, which is the
    direction this whole rule fails in on purpose.
    """
    if depth > MAX_WALK_DEPTH:
        raise GuardrailError(f"Query is nested too deeply (walk limit {MAX_WALK_DEPTH}).")
    if _is_clock(node):
        return True
    for child in _child_nodes(node):
        if not _moves_with_clock(child, depth + 1):
            continue
        text = _expression_text(child)
        if text is not None and (_bound_type(text) or "") in _temporal_types():
            return True
    return False


def _is_fixed_point_in_time(node: dict[str, Any]) -> str | None:
    """The printed expression if it is a *fixed* date/time, else `None`."""
    text = _expression_text(node)
    if text is None:
        return None
    bound = _bound_type(text)
    if bound is None:
        return None  # needs a column ⇒ not fixed at write time
    if bound not in _point_in_time_types():
        return None
    return None if _moves_with_clock(node) else text


@lru_cache(maxsize=512)
def frozen_time_expressions(sql: str) -> tuple[str, ...]:
    """Every expression whose value is a date/time **fixed when it was written**.

    This is what makes a cached plan answer *the question of the day it was
    written*: the number it returns is fresh, but `due_date < '2026-08-01'` asks
    a different question every day it is not run.

    **The engine decides, twice.** An expression is fixed if (a) it contains no
    function the catalog marks as moving (`_moving_functions`), and (b) DuckDB's
    **binder** gives it a point-in-time type when asked alone — which it can only
    do if the expression needs no column. Both answers come from the same engine
    that will run the query, never from a pattern.

    That matters because a date does not have to be a literal. Written as a
    literal scan over string constants, this rule was falsified by a blind pass
    in four ways within minutes: `make_date(2026, 8, 1)`,
    `to_timestamp(1785110400)::DATE`, `('2026-08' || '-01')::DATE` and
    `strptime('01/08/2026', '%d/%m/%Y')` all pin a date and carry **no**
    date-shaped string — `'01/08/2026'` is not a date to DuckDB, and `'2026-08'`
    is half of one. Checking the *shape of the input* instead of the *meaning of
    the expression* is the same mistake as counting semicolons in text, one
    module up (ADR-022, 2026-08-05).

    A bare string literal is still checked by `TRY_CAST`, because the binder
    cannot help there: `'2026-03-01'` alone binds to VARCHAR, and only the
    comparison against a date column — which needs the real schema — makes it a
    date. Two engine quirks ride along on purpose: `'epoch'` and `'infinity'`
    cast to dates. The first *is* a frozen date; the second is not, and refusing
    to cache the rare query using it costs a slow answer, which is the side
    ADR-009 already chose.
    """
    _text, tree = _statement_and_tree(sql)
    found: list[str] = []
    literals: list[str] = []
    seen: set[str] = set()

    def visit(node: dict[str, Any]) -> bool:
        value = node.get("value")
        # The field it carries, not its node type — see `_collect`: keying a
        # check on the type is what let `SHOW ALL TABLES` past two empty
        # allow-lists.
        if isinstance(value, dict) and "is_null" in value:
            literal = value.get("value")
            if isinstance(literal, str) and literal not in seen:
                seen.add(literal)
                literals.append(literal)
            return False
        fixed = _is_fixed_point_in_time(node)
        if fixed is not None and fixed not in seen:
            seen.add(fixed)
            found.append(fixed)
            return True  # maximal: its children are the pieces of this one date
        return False

    _walk_expressions(tree, visit)

    if literals:
        with _parser_lock:
            con = _parser()
            for literal in literals:
                row = con.execute(
                    "SELECT TRY_CAST(? AS DATE) IS NOT NULL", [literal]
                ).fetchone()
                # A one-row scalar SELECT never returns nothing, so this branch is
                # unreachable through DuckDB and a mutation of it stays green. It
                # is written this way because `fetchone` is typed as optional and
                # the direction this guard fails in is not a detail: "no answer"
                # must read as *pin it* (one lost cache hit), never as "not a
                # date" (a frozen question under the seal).
                if row is None or row[0]:
                    found.append(repr(literal))
    return tuple(found)


# Clauses whose select lists never become an answer. An `EXISTS (SELECT 1 …)`
# is a semi-join, and its `SELECT 1` is a placeholder every SQL dialect writes —
# the first version of `invented_outputs` walked into it and refused the idiom
# outright (found by a blind pass, 2026-08-05). A subquery under a predicate
# contributes a truth value; the answer is decided by the select lists outside.
_PREDICATE_CLAUSES: frozenset[str] = frozenset(
    {"where_clause", "having", "qualify", "condition"}
)


def _reads_data(node: dict[str, Any], depth: int = 0) -> bool:
    """Whether this output expression is decided by the ledger.

    A **positive** test, and that is the whole design. The first version asked
    the opposite question — *does it fail to bind on its own?* — and treated
    failure as proof of reading data. A blind pass turned that around with
    `row_number() OVER ()`: it does not read one byte of any relation, and it
    was enough to certify a select list of invented numbers as ledger-decided.
    Absence of evidence had been wired up as evidence.

    Three things count, each because the ledger decides them:

    * a **column** — a bare identifier that does *not* bind with an empty
      catalog (one that does is a built-in like `current_date`);
    * a **star**, spotted by the field it carries (`exclude_list`) rather than
      by its node type, for the reason in `_collect`;
    * an **aggregate**, from the catalog. `count(*)` binds happily with no
      `FROM` at all, so nothing else here would recognise the demo's very first
      one-click question.
    """
    if depth > MAX_WALK_DEPTH:
        raise GuardrailError(f"Query is nested too deeply (walk limit {MAX_WALK_DEPTH}).")
    if "exclude_list" in node:
        return True
    name = node.get("function_name")
    if name and str(name).lower() in _aggregate_functions():
        return True
    if "column_names" in node:
        text = _expression_text(node)
        if text is None or _bound_type(text) is None:
            return True  # a real column: it does not resolve without a relation
    return any(_reads_data(child, depth + 1) for child in _child_nodes(node))


@lru_cache(maxsize=512)
def invented_outputs(sql: str) -> tuple[str, ...]:
    """Outputs the ledger does not decide — values fixed when the plan was written.

    `relations_read` answers *"is a ledger relation named?"*, which is necessary
    for reading the ledger and nowhere near sufficient. A blind pass built the
    difference in one line:

        SELECT 425000.00 AS total_overdue FROM invoices WHERE 1 = 0
        UNION ALL SELECT 425000.00

    `invoices` is named, the guard is satisfied, the ledger is never read, and
    the replay prints an invented number under the freshness seal. That is the
    *realistic* failure, not an exotic one: a tiny model that hallucinates a
    total writes `FROM v_customer_ar` after it without being asked.

    So **every** output of every answering select list must be ledger-decided.
    Requiring only one was the second version, and it fell to the same pass:
    pad the list with `row_number() OVER ()` and the invented number rides
    along. Requiring all of them costs a real thing, and it is worth naming: a
    query that labels its result (`SELECT 'total' AS label, sum(amount) …`) is
    not cacheable. One slow answer, which is the side ADR-009 always takes.
    """
    _text, tree = _statement_and_tree(sql)
    invented: list[str] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > MAX_WALK_DEPTH:
            raise GuardrailError(
                f"Query is nested too deeply (walk limit {MAX_WALK_DEPTH})."
            )
        if isinstance(node, dict):
            select_list = node.get("select_list")
            if isinstance(select_list, list):
                for entry in select_list:
                    if isinstance(entry, dict) and not _reads_data(entry):
                        text = _expression_text(entry)
                        if text is not None:
                            invented.append(text)
            for child in _child_nodes(node, skip=_PREDICATE_CLAUSES):
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)

    visit(tree)
    return tuple(invented)


def guard_query(sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Validate `sql` and return an executable, row-capped query.

    Returns the statement — as DuckDB prints it back from the validated syntax
    tree, not as it was written — wrapped in ``SELECT * FROM (...) LIMIT n``.
    Raises ``GuardrailError`` (never silently rewrites) on any violation.

    "Any violation" includes the arguments themselves. Both callers of this
    function — ``tools.py`` and ``mcp_server/server.py`` — catch
    ``GuardrailError`` and ``duckdb.Error``, so anything else escapes the tool
    boundary as a crash instead of a refusal the model can act on. The type
    checks below are what makes that catch complete; they were added after an
    audit walked in through ``guard_query(123)`` (``AttributeError``),
    ``max_rows="5"`` (``TypeError``) and ``max_rows=float("inf")``
    (``OverflowError``).
    """
    if not isinstance(sql, str):
        raise GuardrailError(f"Query must be a string (got {type(sql).__name__}).")
    if not _statement_text(sql):
        raise GuardrailError("Empty query.")
    # `bool` is a subclass of `int`, and it is checked first because it is the
    # one that fails *quietly*: `max_rows=True` formatted as `LIMIT 1` and
    # returned a single row from a query the caller thought was uncapped.
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise GuardrailError(
            f"max_rows must be an int (got {type(max_rows).__name__})."
        )
    if max_rows <= 0:
        raise GuardrailError("max_rows must be positive.")
    if max_rows > MAX_ROWS_CEILING:
        raise GuardrailError(
            f"max_rows must not exceed {MAX_ROWS_CEILING} (got {max_rows})."
        )

    # Trailing separators need no handling here: `extract_statements` reports
    # "SELECT 1; ; " as one statement and a bare ";" as none, so both the
    # cosmetic case and the empty case are decided by the parser. A ";" alone
    # is refused by the count below rather than by a length check on text.
    stmt, tree = _statement_and_tree(sql)

    tables: set[str] = set()
    functions: set[str] = set()
    pseudo: set[str] = set()
    _collect(tree, tables, functions, pseudo)

    if pseudo:
        raise GuardrailError(
            "Catalog-listing statements are not allowed (" + ", ".join(sorted(pseudo)) + ")."
        )

    unknown = sorted(t for t in tables if t not in ALLOWED_RELATIONS)
    if unknown:
        raise GuardrailError(
            "Relation(s) not in allow-list: " + ", ".join(unknown) + "."
        )

    # NOTE: CTE names are deliberately NOT consulted here. A CTE named after a
    # table function must not whitelist that function — see the module docstring.
    unknown_fns = sorted(f for f in functions if f not in ALLOWED_FUNCTIONS)
    if unknown_fns:
        raise GuardrailError(
            "Function(s) not in allow-list: " + ", ".join(unknown_fns) + "."
        )

    canonical = _canonical_statement(stmt, tree)

    # No `int()` around `max_rows` here: it is already an int by the checks at
    # the top. The cast used to be the only thing standing between a float and
    # the LIMIT, and it did that by truncating — `max_rows=2.9` became `LIMIT 2`
    # without a word to anyone.
    guarded = f"SELECT * FROM (\n{canonical}\n) AS _guarded LIMIT {max_rows}"
    # The wrapper is the last thing an attacker can aim at: closing it early and
    # appending statements is how the old guard was escaped. Re-parsing the
    # finished text proves that what will actually be executed is still exactly
    # one statement. Nothing the caller wrote is pasted in here any more, so
    # this check is a proof rather than a filter — and its failure message says
    # so in the guard's own words instead of quoting the wrapper's text back.
    # Not for secrecy: this file is public, so the wrapper's shape is not a
    # secret from anyone. The reason is that the caller is a language model
    # trying to self-correct, and a syntax error pointing at a line it did not
    # write is feedback it cannot act on. Measured: no payload in either guard
    # suite can reach the `except` branch, so it is insurance against a
    # refactor rather than a live filter; the test that covers it has to fake a
    # broken normalizer to get there.
    try:
        _single_statement(guarded)
    except GuardrailError as exc:
        raise GuardrailError(
            "The validated query could not be wrapped as a single statement."
        ) from exc
    return guarded


# DuckDB's source echo is two lines and always the last two: the quoted source
# line, then a caret pointing into it. Both are required before anything is cut
# — matching the `LINE n:` head alone is not enough, because that head can be
# forged from inside the query (see the docstring below). Measured on DuckDB
# 1.5.3 across the 14 echoing failures of a 20-query sweep: the caret line is
# spaces-then-caret in every one, with nothing after it.
_SOURCE_ECHO_HEAD = re.compile(r"^LINE \d+: ")
_SOURCE_ECHO_CARET = re.compile(r"^ *\^$")


def strip_wrapper_line_echo(message: str) -> str:
    """Remove the ``LINE n:`` source echo from a DuckDB *execution* error.

    The refusals raised above never quote the wrapper back at the caller, for
    the reason given at the wrap site: the caller is a language model trying to
    self-correct, and a line number into a query it did not compose is feedback
    it cannot act on. Errors raised while *executing* the guarded query used to
    escape that rule — ``guard_query`` returns the statement inside
    ``SELECT * FROM (\\n … \\n) AS _guarded LIMIT n``, so DuckDB reports a
    binder failure at ``LINE 2`` and echoes the line, which is the printed
    canonical text rather than anything the model typed::

        Binder Error: Referenced column "amont" not found in FROM clause!
        Candidate bindings: "amount", "status", "customer_id"

        LINE 2: SELECT amont FROM invoices
                       ^

    Only the echo goes. The diagnosis above it — including *Candidate
    bindings*, the part a model actually corrects itself with — is kept
    verbatim. Rewriting the number to be relative to the caller's text was the
    obvious alternative and is worse: the echoed line is DuckDB's print of the
    validated tree, so ``amount_due::INTEGER`` comes back as
    ``CAST(amount_due AS INTEGER)`` and the caret would point into a string the
    caller never had. A wrong caret is more expensive than no caret.

    Measured on DuckDB 1.5.3 over 20 failing queries: 14 carry the echo and in
    every one of them it is the **last two lines** — echo, then caret. The six
    without it (``GROUP BY 9``, ``amount.foo``, ``date_trunc('bogus', …)``, …)
    are returned unchanged. The line number is not a constant either: a string
    literal containing a real newline pushes the failure to ``LINE 3``, which is
    why the pattern reads ``LINE \\d+`` and not the ``LINE 2`` every example
    shows.

    Both lines of the block are required, and that is not belt-and-braces. A
    quoted identifier can carry a newline, so the caller can put a line that
    reads exactly like an echo *into the diagnosis itself*::

        SELECT "a\\nLINE 9: injected" FROM invoices

    lands as ``Referenced column "a`` / ``LINE 9: injected" not found …`` — an
    echo-shaped line sitting above the genuine one. Cutting at the first
    ``LINE n:`` would delete the Candidate bindings this function exists to
    preserve, which hands the caller a way to blank its own error message. So
    the cut is anchored at the tail and needs the caret line under it; a forged
    head cannot supply one, because whatever the caller injects is followed by
    the rest of DuckDB's sentence rather than by a bare ``^``.

    The failure direction is deliberate: if a future DuckDB emits the echo in
    some other shape, this returns the message untouched and the leak comes
    back rather than the diagnosis being eaten. That is the cheaper of the two
    mistakes, and the ROUND 7 tests fail on the upgrade that causes it — they
    run real queries into the binder instead of asserting against hand-written
    message strings.
    """
    lines = message.splitlines()
    if len(lines) < 2:
        return message
    if not _SOURCE_ECHO_CARET.match(lines[-1]):
        return message
    if not _SOURCE_ECHO_HEAD.match(lines[-2]):
        return message
    return "\n".join(lines[:-2]).rstrip()

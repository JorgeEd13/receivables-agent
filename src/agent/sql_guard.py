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

What each layer actually protects. ``read_only=True`` protects *integrity*; it
stops writes. It does **not** protect *confidentiality*: a read that reaches
outside the ledger is still a read, and reading is what an LLM-composed query
does. Confidentiality is held by this filter plus ``enable_external_access``
being off on the connection. For two releases this module claimed "defense in
depth" while the confidentiality half was a single layer.
"""

from __future__ import annotations

import json
import threading
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
    """
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
                _collect(entry, tables, functions, pseudo, frozenset(visible))
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
            _collect(value, tables, functions, pseudo, scope)
    elif isinstance(node, list):
        for value in node:
            _collect(value, tables, functions, pseudo, scope)


def guard_query(sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Validate `sql` and return an executable, row-capped query.

    Returns the statement — as DuckDB prints it back from the validated syntax
    tree, not as it was written — wrapped in ``SELECT * FROM (...) LIMIT n``.
    Raises ``GuardrailError`` (never silently rewrites) on any violation.
    """
    if not sql or not sql.strip():
        raise GuardrailError("Empty query.")
    if max_rows <= 0:
        raise GuardrailError("max_rows must be positive.")

    # Trailing separators need no handling here: `extract_statements` reports
    # "SELECT 1; ; " as one statement and a bare ";" as none, so both the
    # cosmetic case and the empty case are decided by the parser. A ";" alone
    # is refused by the count below rather than by a length check on text.
    stmt = sql.strip()

    _single_statement(stmt)
    tree = _syntax_tree(stmt)

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

    guarded = f"SELECT * FROM (\n{canonical}\n) AS _guarded LIMIT {int(max_rows)}"
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

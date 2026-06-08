"""Guardrail for the text-to-SQL tool — the priority security surface.

The agent may *only* run read-only analytical queries against a fixed set of
relations. This module is the prompt-layer filter; it is paired with a
**read-only DuckDB connection** (see ``ledger.py``) so the two form defense in
depth: even if a jailbroken prompt slipped a mutation past this filter, the
connection physically cannot write.

Design rules (do not relax these to "make a query pass" — fix the query):

* one statement only (no stacked ``;`` queries);
* it must be a ``SELECT`` / ``WITH`` read query;
* a deny-list of write/DDL/catalog/filesystem keywords is rejected;
* every referenced relation must be in the allow-list (CTE names excepted);
* the result is wrapped in an outer ``LIMIT`` so the model can never dump the
  whole ledger.

The parser is intentionally small and string-literal aware: comments are
stripped and statements are split *outside* of quoted strings, and keyword /
relation matching runs against a copy with string contents masked, so a literal
like ``WHERE name = 'DROP TABLE x'`` is not mistaken for an attack.
"""

from __future__ import annotations

import re

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

# Whole-word tokens that must never appear in a query: writes, DDL, catalog and
# transaction control, plus DuckDB filesystem/table functions that could read or
# exfiltrate data outside the ledger. Matched case-insensitively with \b...\b.
# Note: deliberately *not* listed are read-safe words that double as functions
# (e.g. ``replace`` the string function); CREATE catches "CREATE OR REPLACE".
DENY_KEYWORDS: tuple[str, ...] = (
    # DML / DDL
    "insert", "update", "delete", "drop", "create", "alter", "truncate",
    "merge", "upsert", "grant", "revoke",
    # catalog / session / transaction control
    "attach", "detach", "copy", "export", "import", "install", "load",
    "pragma", "set", "reset", "call", "use", "vacuum", "checkpoint",
    "prepare", "execute", "deallocate", "begin", "start", "commit",
    "rollback", "abort", "transaction",
    # filesystem / table functions
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
    "read_text", "read_blob", "parquet_scan", "glob", "sniff_csv",
    "query_table", "system",
)

DEFAULT_MAX_ROWS = 200

# Keywords that end a FROM/JOIN relation list (used by the relation scanner).
_CLAUSE_KW = frozenset(
    {
        "where", "group", "order", "having", "limit", "offset", "union",
        "intersect", "except", "on", "using", "join", "inner", "left",
        "right", "full", "cross", "natural", "window", "qualify", "fetch",
        "select", "from",
    }
)

# A token is a (possibly dotted / quoted) identifier or a structural char.
_TOKEN_RE = re.compile(r'"[^"]*"(?:\.[\w$"]+)*|[A-Za-z_][\w$]*(?:\.[\w$]+)*|[(),]')

# CTE / WINDOW names introduced with `name AS (` — these are locally defined and
# therefore exempt from the relation allow-list.
_LOCAL_NAME_RE = re.compile(r'(?:\bwith\b|,)\s+("?[A-Za-z_]\w*"?)\s+as\s*\(', re.I)

_DENY_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in DENY_KEYWORDS) + r")\b", re.I
)


class GuardrailError(ValueError):
    """Raised when a query violates the read-only / allow-list policy."""


def guard_query(sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Validate `sql` and return an executable, row-capped query.

    Returns the original statement wrapped in ``SELECT * FROM (...) LIMIT n``.
    Raises ``GuardrailError`` (never silently rewrites) on any violation.
    """
    if not sql or not sql.strip():
        raise GuardrailError("Empty query.")
    if max_rows <= 0:
        raise GuardrailError("max_rows must be positive.")

    statements = _strip_comments_split(sql)
    if not statements:
        raise GuardrailError("Empty query after stripping comments.")
    if len(statements) > 1:
        raise GuardrailError(
            "Only a single statement is allowed (no stacked ';' statements)."
        )

    stmt = statements[0]
    masked = _mask_literals(stmt)
    low = masked.lower()

    if not re.match(r"^\s*(?:select|with)\b", low):
        raise GuardrailError("Only read-only SELECT / WITH queries are allowed.")

    deny = _DENY_RE.search(low)
    if deny:
        raise GuardrailError(f"Disallowed keyword: {deny.group(0).upper()}.")

    local_names = _local_names(masked)
    unknown = sorted(
        r
        for r in _referenced_relations(masked)
        if r not in ALLOWED_RELATIONS and r not in local_names
    )
    if unknown:
        raise GuardrailError(
            "Relation(s) not in allow-list: " + ", ".join(unknown) + "."
        )

    return f"SELECT * FROM (\n{stmt}\n) AS _guarded LIMIT {int(max_rows)}"


# --------------------------------------------------------------------------- #
# Internal: a tiny string-literal-aware SQL scanner.
# --------------------------------------------------------------------------- #


def _strip_comments_split(sql: str) -> list[str]:
    """Remove comments and split on top-level ``;``, ignoring quoted strings.

    Comments become a space (so they cannot glue two tokens together). Quoted
    string and identifier contents are preserved verbatim.
    """
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "'":  # single-quoted string literal
            j = i + 1
            cur.append("'")
            while j < n:
                c = sql[j]
                cur.append(c)
                if c == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' escape
                        cur.append("'")
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            i = j
            continue
        if ch == '"':  # double-quoted identifier
            j = i + 1
            cur.append('"')
            while j < n:
                c = sql[j]
                cur.append(c)
                j += 1
                if c == '"':
                    break
            i = j
            continue
        if ch == "-" and nxt == "-":  # line comment
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            cur.append(" ")
            continue
        if ch == "/" and nxt == "*":  # block comment
            i += 2
            while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                i += 1
            i += 2
            cur.append(" ")
            continue
        if ch == ";":  # statement separator
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _mask_literals(stmt: str) -> str:
    """Return `stmt` with single-quoted string *contents* blanked to ``''``.

    Used only for keyword / relation analysis so that data inside string
    literals (e.g. a customer named "DROP TABLE x") never trips the guard.
    """
    out: list[str] = []
    i, n = 0, len(stmt)
    while i < n:
        ch = stmt[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if stmt[j] == "'":
                    if j + 1 < n and stmt[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append("''")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _local_names(stmt: str) -> set[str]:
    """Names defined locally by CTEs / WINDOW clauses (exempt from allow-list)."""
    return {m.group(1).strip('"').lower() for m in _LOCAL_NAME_RE.finditer(stmt)}


def _referenced_relations(stmt: str) -> set[str]:
    """Best-effort set of relations referenced after FROM / JOIN.

    Handles comma-separated lists, ``schema.table`` qualification, aliases and
    derived-table subqueries (which are skipped, their inner FROMs handled by
    the outer scan). This is the prompt-layer check; the read-only connection is
    the authoritative backstop for anything the parser doesn't catch.
    """
    tokens = _TOKEN_RE.findall(stmt)
    rels: set[str] = set()
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i].lower() in ("from", "join"):
            j = i + 1
            expect_rel = True
            while j < n:
                tok = tokens[j]
                if expect_rel:
                    if tok == "(":  # derived table — skip the balanced group
                        depth, j = 1, j + 1
                        while j < n and depth > 0:
                            if tokens[j] == "(":
                                depth += 1
                            elif tokens[j] == ")":
                                depth -= 1
                            j += 1
                        expect_rel = False
                        continue
                    if tok in (",", ")"):
                        j += 1
                        continue
                    name = tok.strip('"').split(".")[-1].strip('"').lower()
                    rels.add(name)
                    expect_rel = False
                    j += 1
                    continue
                if tok == ",":
                    expect_rel = True
                    j += 1
                    continue
                if tok.lower() in _CLAUSE_KW:
                    break
                j += 1
            i = j
            continue
        i += 1
    return rels

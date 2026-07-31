"""The prompt's map of the ledger, checked against the ledger the generator builds.

`SCHEMA_HINTS` is the third side of a triangle: `data/generate.py` builds the
schema, `sql_guard.ALLOWED_RELATIONS` says what may be read, and this text is
what the *model* reads before it writes any SQL. The first two sides have owned
each other since the relation surface was partitioned in the adversarial suite.
This side had no owner at all: renaming `v_dso` to a view that does not exist
left the suite at 346 green, and the failure it would cause in production is the
expensive kind — syntactically perfect SQL, a `Catalog Error` at execution, and
the agent burning its step budget trying to repair a query that was never the
problem, with nobody suspecting the prompt.

The surface here is a **real ledger**, built by `data/generate.py` through its
own entry point. The adversarial suite's throwaway ledger cannot answer for this
file: it hand-rolls relations that only have to be *shaped* like the real ones
(its `payments` has three columns; the real one has six), so a column list
checked against it would prove nothing about what the model will find.

No `skipif` here on purpose. `tests/test_ledger.py` skips when
`data/ledger.duckdb` is absent — which, since the file is git-ignored, means it
skips in CI, where drift is exactly what nobody is watching for. This module
builds its own ledger instead, so it runs everywhere or fails loudly.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator

import duckdb
import pytest

from src.agent.schema_hints import SCHEMA_HINTS, SCHEMA_HINTS_BRIEF
from src.agent.sql_guard import ALLOWED_RELATIONS

# Relations the full hint describes in prose instead of listing with a column
# tuple: `meta` appears only as `meta.as_of_date`, because the model needs the
# column, not the table. Stated here, in the test, so that the *partition* of
# the schema — what the prompt spells out and what it only mentions — is written
# somewhere other than the text under test. Same reasoning as `LEDGER_OFF_LIMITS`
# in the adversarial suite: a list that is checked against itself checks nothing.
DESCRIBED_IN_PROSE = frozenset({"meta"})

# The (relation, column) pairs the full hint commits to a closed set of values
# for. Pinned outside the text so the loop below cannot go quiet: if an `∈`
# clause is deleted, or reworded past the parser, the count changes and this
# turns red instead of iterating over nothing.
PROMISED_VALUE_SETS = frozenset(
    {
        ("v_invoices", "status"),
        ("v_invoices", "aging_bucket"),
        ("customers", "segment"),
        ("customers", "profile"),
        ("communications", "stage"),
    }
)

# Smallest generated ledger that still realises every documented value set —
# measured, not guessed (25 customers already covers all five dunning stages and
# all five aging buckets; 60 is margin). A ledger too small to contain a promised
# value makes the value-set test RED, never falsely green: the comparison is set
# equality against what the file actually holds.
LEDGER_CUSTOMERS = 60


@pytest.fixture(scope="module")
def ledger(tmp_path_factory) -> Iterator[duckdb.DuckDBPyConnection]:
    """A real ledger, generated the way the product generates it.

    `generate.main()` is driven through `sys.argv` rather than by calling
    `create_dimensions` / `generate_facts` / `create_views` in sequence here.
    Re-assembling the pipeline in the fixture would be a second copy of the
    orchestration, and a relation added to `main()` and not to the copy would be
    invisible to every assertion below.
    """
    # Imported here, not at module scope: `faker` lives in the optional `data`
    # extra, and a missing extra should cost this fixture, not collection of the
    # whole file.
    import data.generate as generate

    path = str(tmp_path_factory.mktemp("hints") / "ledger.duckdb")
    argv = ["generate.py", "--customers", str(LEDGER_CUSTOMERS), "--out", path]
    saved, sys.argv = sys.argv, argv
    try:
        generate.main()
    finally:
        sys.argv = saved

    con = duckdb.connect(path, read_only=True)
    yield con
    con.close()


# --------------------------------------------------------------------------- #
# Reading the two surfaces: the ledger's catalog, and the prompt's text.
# --------------------------------------------------------------------------- #

def _catalog(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    """Every non-internal relation in the ledger, with its columns."""
    surface: dict[str, set[str]] = {}
    for table, column in con.execute(
        "SELECT table_name, column_name FROM duckdb_columns() WHERE NOT internal"
    ).fetchall():
        surface.setdefault(table, set()).add(column)
    return surface


def _views(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT view_name FROM duckdb_views() WHERE NOT internal"
        ).fetchall()
    }


# `- name(col, col,\n    col)` — the shape both hints use to list a relation.
_RELATION_BLOCK = re.compile(r"^- (\w+)\(([^)]*)\)", re.MULTILINE)
# `status ∈ {paid, open, overdue}` — the full hint's way of closing a value set.
_VALUE_SET = re.compile(r"(\w+) ∈ \{([^}]*)\}")
# `-- status: paid|open|overdue; aging_bucket: current|1-30|...` — the brief's way.
_BRIEF_VALUE_SET = re.compile(r"(\w+): ([\w|+\-]+)")


def _documented_columns(hint: str) -> dict[str, set[str]]:
    return {
        match.group(1): {col.strip() for col in match.group(2).split(",")}
        for match in _RELATION_BLOCK.finditer(hint)
    }


def _documented_value_sets(hint: str, pattern: re.Pattern[str], sep: str) -> dict[tuple[str, str], set[str]]:
    """Value sets found in `hint`, each bound to the relation block it sits under."""
    blocks = [(m.start(), m.group(1)) for m in _RELATION_BLOCK.finditer(hint)]
    promises: dict[tuple[str, str], set[str]] = {}
    for match in pattern.finditer(hint):
        enclosing = [name for start, name in blocks if start < match.start()]
        if not enclosing:
            continue
        promises[(enclosing[-1], match.group(1))] = {
            value.strip() for value in match.group(2).split(sep)
        }
    return promises


def _distinct(con: duckdb.DuckDBPyConnection, relation: str, column: str) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(f"SELECT DISTINCT {column} FROM {relation}").fetchall()
    }


# --------------------------------------------------------------------------- #
# The full hint — the prompt every non-tiny model gets.
# --------------------------------------------------------------------------- #

def test_the_hint_documents_every_relation_the_ledger_holds(ledger) -> None:
    """Both directions, against the ledger's own catalog.

    A relation the hint names and the ledger lacks is the `v_dso` defect: the
    model is told to query something that will not resolve. A relation the ledger
    has and the hint omits is the quieter half — capacity the guard allows and
    the model never learns about.
    """
    present = set(_catalog(ledger))
    # Sanity on the probe: a mis-filtered catalog query (dropping `NOT internal`,
    # say) would drag in DuckDB's own metadata and make every set below noise.
    assert {"invoices", "v_dso"} <= present, present
    assert "duckdb_columns" not in present, "the catalog filter let internal tables in"

    documented = set(_documented_columns(SCHEMA_HINTS)) | DESCRIBED_IN_PROSE
    assert documented == present


def test_the_columns_the_hint_lists_are_the_columns_the_relation_has(ledger) -> None:
    """Iterating the *catalog*, not the hint.

    Parametrizing over the text under test is the defect one level up, and it
    was measured: removing a name from a list removed its own test case with it,
    and the mutation stayed green. So the ledger decides what gets checked and
    the hint is checked against it.
    """
    documented = _documented_columns(SCHEMA_HINTS)
    real = _catalog(ledger)
    for relation in sorted(set(real) - DESCRIBED_IN_PROSE):
        assert relation in documented, f"{relation} is in the ledger, not in the prompt"
        assert documented[relation] == real[relation], relation


def test_the_prose_reference_to_meta_names_a_real_column(ledger) -> None:
    """`meta` is the one relation the hint does not list, so it is checked apart.

    The whole "today" story rests on this single reference: every overdue and
    aging answer is computed against `meta.as_of_date` instead of CURRENT_DATE.
    A second column on `meta` would be undocumented, which is why the comparison
    is equality and not membership.
    """
    assert "meta.as_of_date" in SCHEMA_HINTS
    assert _catalog(ledger)["meta"] == {"as_of_date"}


def test_the_hint_and_the_guardrail_describe_the_same_ledger() -> None:
    """Prompt and allow-list, with no ledger in between.

    These two drift in opposite, equally silent ways. A relation in the hint but
    not on the allow-list means the model is invited to write SQL the guard will
    refuse — the user sees a refusal for a query the prompt suggested. A relation
    on the allow-list but not in the hint is dead capacity. `ALLOWED_RELATIONS`
    is itself pinned against a real catalog in the adversarial suite, so equality
    here closes the triangle without needing a third copy of the schema.
    """
    documented = set(_documented_columns(SCHEMA_HINTS)) | DESCRIBED_IN_PROSE
    assert documented == set(ALLOWED_RELATIONS)


def test_the_value_sets_the_hint_promises_are_what_the_ledger_holds(ledger) -> None:
    """`status ∈ {paid, open, overdue}` is a promise about *data*, not schema.

    It is the drift a schema check cannot see: every column still exists, every
    query still parses, and `WHERE stage = 'final_notice'` quietly returns zero
    rows because the generator renamed a stage. The model has no way to discover
    that — it never sees the data, only this sentence.
    """
    promised = _documented_value_sets(SCHEMA_HINTS, _VALUE_SET, ",")
    assert set(promised) == PROMISED_VALUE_SETS
    for relation, column in sorted(PROMISED_VALUE_SETS):
        assert promised[(relation, column)] == _distinct(ledger, relation, column)


# --------------------------------------------------------------------------- #
# The brief hint — the tiny-model prompt (ADR-013). Deliberately a subset.
# --------------------------------------------------------------------------- #

def test_the_brief_hint_covers_exactly_the_views(ledger) -> None:
    """The brief prompt's rule is "views only", so the catalog states its scope.

    Written as equality against the ledger's views rather than against a list of
    three names: a fourth analytics view added to `data/generate.py` and left out
    of the tiny-model prompt is precisely the drift this is here to catch.
    """
    assert set(_documented_columns(SCHEMA_HINTS_BRIEF)) == _views(ledger)


def test_the_brief_hint_invents_nothing_and_contradicts_nothing(ledger) -> None:
    """Two containments, because the brief is a subset by design.

    It drops `currency` and `as_of_date` to save tokens, so equality would be
    wrong. What must hold is that every column it does name exists in the ledger
    *and* says the same thing the full prompt says.
    """
    brief = _documented_columns(SCHEMA_HINTS_BRIEF)
    full = _documented_columns(SCHEMA_HINTS)
    real = _catalog(ledger)
    for relation, columns in sorted(brief.items()):
        assert columns, f"{relation} was parsed with no columns"
        assert columns <= real[relation], relation
        assert columns <= full[relation], f"the two prompts disagree about {relation}"


def test_the_brief_value_sets_are_what_the_ledger_holds(ledger) -> None:
    """The same promise as the full hint, in the compact `a|b|c` notation."""
    promised = _documented_value_sets(SCHEMA_HINTS_BRIEF, _BRIEF_VALUE_SET, "|")
    assert set(promised) == {("v_invoices", "status"), ("v_invoices", "aging_bucket")}
    for (relation, column), values in sorted(promised.items()):
        assert values == _distinct(ledger, relation, column)


def test_the_brief_hint_keeps_the_as_of_date_rule(ledger) -> None:
    """The one prose rule the compact prompt kept — it has to survive the cut."""
    assert "meta.as_of_date" in SCHEMA_HINTS_BRIEF
    assert "as_of_date" in _catalog(ledger)["meta"]

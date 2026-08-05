"""Fixtures shared by more than one suite.

Two things, for one reason: they let a suite check something the repo *says*
against what the product actually builds.

* a **real ledger**, built by `data/generate.py` through its own entry point.
  `test_schema_hints.py` checks the prompt's map of it; `test_plan_cache.py`
  checks that the curated SQL baked into the demo image still runs on it.
* the **real policy document**, indexed offline. `test_plan_cache.py` replays
  the curated policy chips through it; `test_replay_claims.py` checks what the
  reply *claims* about a replay that only touched it.

They live here rather than in one of the suites because a second copy of a
fixture is a second thing to keep in step, and a ledger (or an index) built
slightly differently in one suite would make that suite's assertions quietly
weaker than they read. `policy` moved here on 2026-08-05, when the second
consumer appeared — the same move `ledger` made on 2026-08-01.

No `skipif` on purpose. `tests/test_ledger.py` skips when `data/ledger.duckdb`
is absent — which, since the file is git-ignored, means it skips in CI, where
drift is exactly what nobody is watching for. This builds its own ledger, so the
suites that use it run everywhere or fail loudly.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

# Smallest generated ledger that still realises every documented value set —
# measured, not guessed (25 customers already covers all five dunning stages and
# all five aging buckets; 60 is margin). A ledger too small to contain a promised
# value makes the value-set test RED, never falsely green: the comparison is set
# equality against what the file actually holds. Measured at 3 customers: the
# value-set tests go red and every curated-plan test stays green — so this
# constant is load-bearing for the prompt suite, not for the replay suite.
LEDGER_CUSTOMERS = 60


@pytest.fixture(scope="session")
def ledger(tmp_path_factory) -> Iterator[duckdb.DuckDBPyConnection]:
    """A real ledger, generated the way the product generates it.

    `generate.main()` is driven through `sys.argv` rather than by calling
    `create_dimensions` / `generate_facts` / `create_views` in sequence here.
    Re-assembling the pipeline in the fixture would be a second copy of the
    orchestration, and a relation added to `main()` and not to the copy would be
    invisible to every assertion that uses this.

    Read-only, like the connection the API and the replay path open, and
    session-scoped because the generator is deterministic (fixed seed) — the same
    bytes for every consumer, built once.
    """
    # Imported here, not at module scope: `faker` lives in the optional `data`
    # extra, and a missing extra should cost this fixture, not collection of
    # every file that happens to sit next to it.
    import data.generate as generate

    path = str(tmp_path_factory.mktemp("ledger") / "ledger.duckdb")
    argv = ["generate.py", "--customers", str(LEDGER_CUSTOMERS), "--out", path]
    saved, sys.argv = sys.argv, argv
    try:
        generate.main()
    finally:
        sys.argv = saved

    con = duckdb.connect(path, read_only=True)
    yield con
    con.close()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def shipped_default(field: str):
    """The default that ships, read off `Settings` instead of copied here.

    Not `Settings()`: that would read the developer's git-ignored `.env`, and a
    local `MAX_ROWS=5` must not quietly change what a suite asserts.
    """
    from src.core.config import Settings

    return Settings.model_fields[field].default


@pytest.fixture(scope="module")
def policy():
    """The real policy document, indexed offline into an ephemeral collection.

    The deterministic (hashing) embedding stands in for MiniLM, so what this
    fixture supports is "retrieval finds *a* section and names it", never "this
    query ranks that section first" — the ranking belongs to the real embedding
    and is not measured here.
    """
    import chromadb

    from src.rag.embeddings import DeterministicEmbeddingFunction
    from src.rag.index import build_index

    return build_index(
        chromadb.EphemeralClient(),
        str(repo_root() / shipped_default("policy_path")),
        "shared_policy_index",
        DeterministicEmbeddingFunction(),
    )

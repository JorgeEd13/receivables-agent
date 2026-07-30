"""Hardened, read-only access to the DuckDB ledger.

Two distinct protections, deliberately named apart (ADR-022):

* ``read_only=True`` protects **integrity** — no statement on this connection
  can create, alter or mutate anything.
* ``enable_external_access=False`` plus disabled extension autoloading protects
  **confidentiality** — it is what stops ``sqlite_scan``, ``postgres_scan``,
  ``read_csv``, ``getenv`` and every future table function from reaching data
  outside the ledger. A read-only connection does nothing about those: they are
  all *reads*, and reads are what an LLM-composed query is made of.

The first of these was previously described as "the authoritative half" of the
guardrail. It was authoritative only against mutation; against exfiltration the
prompt-layer filter was standing alone.
"""

from __future__ import annotations

import os
from typing import Any

import duckdb

# Engine-level backstop. Applied at connect time because several of these are
# start-up-only settings — they cannot be tightened later from a SET statement,
# which is precisely the property we want.
HARDENED_CONFIG: dict[str, str] = {
    "enable_external_access": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "allow_community_extensions": "false",
    "lock_configuration": "true",
}


def connect_readonly(path: str) -> duckdb.DuckDBPyConnection:
    """Open the ledger read-only, with external access disabled."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Ledger not found at {path!r}. Build it with "
            "`python data/generate.py` (needs the 'data' extra)."
        )
    return duckdb.connect(path, read_only=True, config=dict(HARDENED_CONFIG))


def run_query(
    con: duckdb.DuckDBPyConnection, guarded_sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute already-guarded SQL and return (column_names, rows)."""
    cur = con.execute(guarded_sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    return columns, cur.fetchall()

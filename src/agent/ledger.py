"""Read-only access to the DuckDB ledger.

The connection is opened ``read_only=True``: this is the authoritative half of
the SQL guardrail (``sql_guard``). No DuckDB statement on this connection can
create, alter, or mutate anything — a hard backstop behind the prompt-layer
filter.
"""

from __future__ import annotations

import os
from typing import Any

import duckdb


def connect_readonly(path: str) -> duckdb.DuckDBPyConnection:
    """Open the ledger read-only. Raises if the file is missing."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Ledger not found at {path!r}. Build it with "
            "`python data/generate.py` (needs the 'data' extra)."
        )
    return duckdb.connect(path, read_only=True)


def run_query(
    con: duckdb.DuckDBPyConnection, guarded_sql: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute already-guarded SQL and return (column_names, rows)."""
    cur = con.execute(guarded_sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    return columns, cur.fetchall()

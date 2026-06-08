# Architecture — receivables-agent

> Evolving document. High-level design; detail fills in as phases land.

## Overview

A ReAct agent answers natural-language questions about accounts receivable by
orchestrating two grounded tools over synthetic data.

## Data flow

1. **Synthetic data** (`data/generate.py`) → `ledger.duckdb`
   - Faker generates dimensions (customers, payment terms).
   - Facts (invoices, payments, communications) are generated set-based in
     DuckDB, with per-customer payment-behavior profiles so the data carries
     signal. Aging buckets and DSO are derived in SQL.
2. **Collections policy** (`data/collections_policy.md`) → ChromaDB index.
3. **Agent** (`src/agent/`): a LangGraph ReAct loop with two tools —
   - `query_ledger`: guarded text-to-SQL (read-only connection + allow/deny
     lists + schema hints).
   - `search_policy`: retrieval over the policy index.
   - LLM via a dual provider (cloud / local) with active fallback.
4. **API** (`src/api/`): FastAPI, async lifespan, API-key auth, Pydantic v2.
5. **Web** (`web/`): a small React chat UI.

## Components

_(to be expanded per phase)_

## Security model

- The SQL tool runs on a **read-only** DuckDB connection and is allow-listed;
  even if prompt-level filtering failed, the connection cannot mutate data.
- No secrets in code; configuration via environment variables.

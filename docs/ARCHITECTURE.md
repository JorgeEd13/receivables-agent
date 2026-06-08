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

## Data model (Phase 1 — implemented)

`data/generate.py` builds `data/ledger.duckdb`. Faker generates the customer
dimension in Python; the fact tables are generated **set-based in DuckDB**
(~1.06M invoices from 13k customers in ~45 s; see ADR-001).

**Dimensions**

- `payment_profiles(profile, default_rate, late_mean_days)` — the five
  behaviour profiles (prompt → defaulter) that drive the fact distributions.
- `customers(customer_id, name, segment, industry, country, profile,
  payment_terms_days, invoice_count, credit_limit, onboarded_at)`.
- `meta(as_of_date)` — single-row reporting date so views are self-contained.

**Facts**

- `invoices(invoice_id, customer_id, issue_date, due_date, amount, currency,
  status)` — `status ∈ {paid, open, overdue}`, derived from the (hidden)
  payment date vs. the as-of date.
- `payments(payment_id, invoice_id, customer_id, payment_date, amount_paid,
  method)` — one full payment per paid invoice.
- `communications(comm_id, customer_id, invoice_id, sent_at, channel, stage)` —
  dunning touch-points for overdue invoices (cadence matches the policy doc).

**Analytics views (derived in SQL)**

- `v_invoices` — invoices enriched with `days_overdue` and `aging_bucket`
  (`current / 1-30 / 31-60 / 61-90 / 90+`) as of `meta.as_of_date`.
- `v_customer_ar` — per-customer outstanding, overdue amount, max days overdue.
- `v_dso` — Days Sales Outstanding over the trailing 90 days.

The data carries signal: overdue rate rises monotonically with the behaviour
profile (prompt ≈0.5% → defaulter ≈50%), so aging/DSO are meaningful, not noise.

The collections policy (`data/collections_policy.md`) is the RAG knowledge
base; its aging buckets and dunning cadence intentionally match this model.

## Components

_(agent / api / web to be expanded per phase)_

## Security model

- The SQL tool runs on a **read-only** DuckDB connection and is allow-listed;
  even if prompt-level filtering failed, the connection cannot mutate data.
- No secrets in code; configuration via environment variables.

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

### RAG over the policy (Phase 3 — implemented)

`src/rag/` indexes `data/collections_policy.md` into ChromaDB and serves
retrieval to the `search_policy` tool.

- **`chunking.py`** — splits the policy on its `##` sections (each a
  self-contained, citable rule) into chunks with deterministic IDs
  (`"{source}::{heading-slug}"`).
- **`embeddings.py`** — local embeddings by default (ChromaDB's bundled ONNX
  `all-MiniLM-L6-v2`; no key, no provider coupling — ADR-005) and an injectable
  `DeterministicEmbeddingFunction` (offline hashing vectorizer) for tests.
- **`index.py`** — `build_index` is idempotent: `upsert` by deterministic ID +
  prune orphaned IDs, so the collection always mirrors the document (ADR-006).
  `ensure_policy_index` builds it once on the agent's first run and reuses it
  after.

### Agent (Phase 2–3 — implemented)

`src/agent/` is a LangGraph ReAct loop (`graph.build_agent`) over two tools,
used together: `query_ledger` (numbers) and `search_policy` (rules).

- **`sql_guard.py`** — the prompt-layer guardrail. `guard_query` strips comments
  and splits statements (string-literal aware), requires `SELECT`/`WITH`, rejects
  a deny-list of write/DDL/catalog/filesystem keywords, checks referenced
  relations against an allow-list, and wraps the query in an outer `LIMIT`. Pure
  and offline-testable (see ADR-003).
- **`ledger.py`** — opens the ledger `read_only=True` (the authoritative half of
  the guardrail) and runs guarded SQL.
- **`tools.py`** — `query_ledger`: guard → read-only execute → JSON rows
  (rejections/errors come back as text so the loop self-corrects); and
  `search_policy`: retrieve top-k policy chunks → JSON with the section heading
  to cite. The system prompt steers the agent to use both together (e.g. fetch
  the credit-hold *rule* with `search_policy`, then the matching *accounts* with
  `query_ledger`).
- **`providers.py` / `graph.py`** — dual provider (Ollama / Gemini) with active
  fallback, wired through a dynamic-model callable so it survives
  `create_react_agent`'s tool-binding (ADR-004). Order is config-driven
  (`PRIMARY_PROVIDER`).
- **`schema_hints.py`** — the schema description injected into the system prompt
  and the tool description.

Configuration lives in `src/core/config.py` (`pydantic-settings`, read from the
environment / `.env`).

### API (Phase 4 — implemented)

`src/api/` is a FastAPI service wrapping `build_agent`.

- **`app.py`** — `create_app(agent_builder)` factory. An async `lifespan`
  builds the agent **once** at startup (opening the read-only ledger and the
  policy index) and stores it on `app.state`; every request reuses it. The
  builder is injectable so tests run the whole HTTP stack against an offline
  stub (no LLM). Endpoints: `GET /api/health` (open) and `POST /api/chat`
  (API-key protected). When a built `web/dist` exists it is mounted at `/`, so
  one container serves UI + API same-origin.
- **`schemas.py`** — Pydantic v2 models at the boundary (`ChatRequest` with
  `message` + prior `history`; `ChatResponse` with `reply` + `tools_used`).
- **Auth** — an `X-API-Key` header is compared to `Settings.app_api_key` with a
  constant-time compare (`secrets.compare_digest`); `/api/health` is open for
  orchestrator probes (ADR-007).

### Web (Phase 4 — implemented)

`web/` is a minimal React (Vite) chat UI: keeps the conversation in state, posts
each turn (+ history) to `/api/chat`, renders replies and a small `tools_used`
badge so you can see the agent actually queried the ledger / read the policy. In
dev Vite proxies `/api` to the API; in the image the built bundle is served by
FastAPI itself, so it is same-origin in both. The API key is baked at build
(`VITE_API_KEY`, matching `APP_API_KEY`).

### Packaging / deploy (Phase 4 — implemented)

A multi-stage `Dockerfile` (Node build → Python runtime) produces a single image
that serves UI + API and generates the synthetic ledger at build time;
`docker compose up` is the one-command run. The single same-origin container is
also the shape a free cloud Space expects (`$PORT` override). See ADR-007.

## Security model

- **Two-layer SQL guardrail (ADR-003).** The tool runs on a **read-only** DuckDB
  connection (the engine refuses every write/DDL) *and* a prompt-layer filter
  (allow/deny lists, single-statement, row cap). Each layer covers the other's
  weakness; neither is relaxed to make a query pass.
- No secrets in code; configuration via environment variables. Provider SDKs are
  imported lazily, so a key for a provider you don't run is never required.

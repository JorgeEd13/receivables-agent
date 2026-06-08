# PLAN — receivables-agent

A public, clean-room portfolio project: a conversational AI agent over synthetic
accounts-receivable data. Goal: a navigable, runnable "shipped link" that
demonstrates the AI / full-stack axis (agents, guarded tool-use, RAG,
full-stack, AI-native tooling) without exposing any private project.

## Principles

- **English everywhere**; clean room (no proprietary code/data); secrets hygiene.
- **Plan first**; record non-obvious choices as ADRs in `docs/DECISIONS.md`.
- Ship a **lean but polished** MVP; a great README counts as much as the code.

## Architecture (target)

```
(synthetic data) ──► DuckDB ledger ;  (policy doc) ──► ChromaDB
        ──► LangGraph ReAct agent [guarded text-to-SQL + policy retrieval,
            dual-provider LLM with fallback] ──► FastAPI ──► React chat UI
```

## Phases

### Phase 0 — Foundations  ✅ (scaffold)
Repo in English; README, CLAUDE.md, PLAN.md, docs skeletons (ARCHITECTURE,
DECISIONS, STATE), LICENSE (MIT), .gitignore, .env.example, pyproject.toml.

### Phase 1 — Synthetic data engine
`data/generate.py`: Faker for dimensions (customers, payment terms); DuckDB
set-based generation of facts (invoices, payments, communications), ~1M+ rows,
with per-customer payment-behavior profiles so the data carries signal. Derive
aging buckets and DSO (days sales outstanding) in SQL. Deterministic seed.
Write `data/collections_policy.md`. → ADR-001: DuckDB over Spark/Pandas.

### Phase 2 — Guarded text-to-SQL + agent core
Read-only DuckDB connection; `query_ledger` tool with allow/deny-list regex +
schema hints. LangGraph ReAct loop. Dual provider (cloud/local) with active
fallback. pytest for the guardrail (injection / escape attempts) — the priority
suite.

### Phase 3 — RAG over the policy
Idempotent ChromaDB indexer (deterministic IDs); `search_policy` tool; agent
uses both tools together.

### Phase 4 — Full-stack + ship
FastAPI (async lifespan, API-key auth, Pydantic v2); React chat UI; Docker +
Compose, one-command run. README with a demo GIF, an architecture diagram, and
the methodology section.

### Phase 5 — AI-native layer (lightweight, in MVP)
An MCP (Model Context Protocol) server exposing the ledger query as a tool; one
Claude Code skill (e.g. an eval runner); fill `docs/DECISIONS.md`; a small eval
suite (golden questions).

## MVP cut

Phases 0–4 plus a lightweight Phase 5. The MCP server, the skill and the
`CLAUDE.md` are the strongest signal for AI-native employers, so they stay in
the MVP.

## Out of scope (for now)

- **PySpark** — deferred to a dedicated data-engineering showcase where large /
  distributed data justifies it. Here DuckDB is the right tool (ADR-001).

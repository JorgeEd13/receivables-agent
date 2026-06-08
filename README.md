# receivables-agent

A conversational AI agent that answers questions about **accounts receivable** —
overdue invoices, aging buckets, who to prioritize — by combining a **guarded
text-to-SQL tool** over a ledger with **retrieval over a collections policy**.

> **Status: work in progress.** This is a public, clean-room portfolio project
> built on 100% synthetic data. See [`PLAN.md`](PLAN.md) for the roadmap and
> [`docs/STATE.md`](docs/STATE.md) for current progress.

## What it does

Ask things like *"Which overdue invoices above $50k should we prioritize this
week, and what does our policy say about offering a payment plan?"* The agent:

1. Plans which tools to call (a ReAct loop on LangGraph).
2. Queries the receivables ledger through a **read-only, allow-listed SQL tool**
   (defense in depth: the model can never mutate or escape the database).
3. Retrieves relevant rules from a **collections-policy knowledge base** (RAG).
4. Answers in natural language, grounded in both sources.

## Architecture (target)

```
(synthetic data) ──► DuckDB ledger
                          │
(collections policy) ─► ChromaDB (RAG)
                          │
                LangGraph ReAct agent
        guarded text-to-SQL  +  policy retrieval
        dual-provider LLM (cloud / local) with fallback
                          │
                   FastAPI service
                          │
                    React chat UI
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for detail.

## Tech stack

- **Agent:** LangGraph (ReAct), dual LLM provider with active fallback
- **Data:** synthetic generator (Faker + DuckDB), ~1M+ invoices
- **Retrieval:** ChromaDB, idempotent indexing
- **API:** FastAPI (async lifespan, API-key auth, Pydantic v2)
- **Web:** React chat UI
- **Tooling:** Docker / Compose, pytest, an MCP server, a Claude Code skill

## Run

> Coming together phase by phase — see [`PLAN.md`](PLAN.md). The target is a
> single `docker compose up`.

## How this was built

I architect and review every line; AI accelerates the implementation. The
[`CLAUDE.md`](CLAUDE.md) conventions, the decision log
([`docs/DECISIONS.md`](docs/DECISIONS.md)) and the volatile-context file
([`docs/STATE.md`](docs/STATE.md)) are the infrastructure I maintain to keep an
AI collaborator productive without losing context across sessions — the same
discipline a fast-moving team needs. The engineering judgment (the data model,
the SQL guardrail, the dual-provider fallback, the test strategy) is mine; the
tooling just makes me faster.

## License

MIT — see [`LICENSE`](LICENSE).

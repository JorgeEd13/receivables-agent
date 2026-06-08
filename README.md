# receivables-agent

**Collections teams burn hours on two questions every day: _who do we chase
first_, and _what does our policy allow?_** Answering them today means writing
SQL against the ledger *and* digging through a policy document — for every
account, every week.

`receivables-agent` answers both in plain language. Ask *"which overdue accounts
above $50k should go on credit hold this week, and what's the rule?"* and it
returns the **prioritized accounts** (from the ledger) **with the governing
policy, cited** — turning an analyst's afternoon of SQL and PDF-hunting into one
question. Under the hood it's a ReAct agent that combines a **guarded
text-to-SQL tool** over the ledger with **retrieval over the collections
policy**.

> **Status: working MVP.** The agent, the guarded SQL + RAG tools, the FastAPI
> service, the React UI, the one-command Docker run and the AI-native layer (MCP
> server + eval suite) are all implemented and tested (Phases 0–5, 76 tests). A
> public, clean-room portfolio project on 100% synthetic data. See
> [`PLAN.md`](PLAN.md) for the roadmap and [`docs/STATE.md`](docs/STATE.md) for
> current progress.

![receivables-agent demo](docs/demo.gif)

*One plain-language question → the agent runs **guarded SQL** over the ledger and
**retrieves the governing policy**, then answers with the prioritized accounts
*and* the cited rule. The `tools_used` badge shows which tools each answer hit.
Recorded against a local Ollama model (`qwen2.5:7b`); a `GEMINI_API_KEY` works
the same way.*

## What it does

Ask things like *"Which overdue invoices above $50k should we prioritize this
week, and what does our policy say about offering a payment plan?"* The agent:

1. Plans which tools to call (a ReAct loop on LangGraph).
2. Queries the receivables ledger through a **read-only, allow-listed SQL tool**
   (defense in depth: the model can never mutate or escape the database).
3. Retrieves relevant rules from a **collections-policy knowledge base** (RAG).
4. Answers in natural language, grounded in both sources.

## Example

> The figures below are **real** — queried live from the synthetic ledger — and
> the policy reference is the actual section in
> [`data/collections_policy.md`](data/collections_policy.md). The agent's exact
> wording is produced by the LLM at run time; this shows the shape of an answer.

**Question**

> Which overdue accounts above $50k should go on credit hold this week, and what
> policy governs that decision?

**Agent answer**

> **2,719** accounts are overdue above $50k and 60+ days past due. Per the
> *Credit holds* policy, an account goes on hold once an invoice is **60+ days
> past due** without a payment commitment (or the balance exceeds the credit
> limit). Working highest-risk first (the *Prioritisation* rule — 90+ bucket
> above $50k), the top accounts are:
>
> 1. **Hensley-Huang** (enterprise) — ~$10.07M overdue, 186 days past due
> 2. **Serrano-Jenkins** (enterprise) — ~$8.74M overdue, 247 days past due
> 3. **Sanders-Banks** (enterprise) — ~$8.36M overdue, 655 days past due
>
> Source: *Credit holds* and *Prioritisation rules* sections of the collections
> policy.

**Tools used:** `query_ledger` (guarded SQL over the ledger) ·
`search_policy` (retrieval over the collections policy)

## The data (and why it's realistic)

100% generated locally (Faker for the customer dimension + **set-based DuckDB**
for the facts), so the project is clean-room and the figures are reproducible
from a fixed seed:

| Metric | Value |
| --- | ---: |
| Invoices | 1,057,402 |
| Payments | 923,200 |
| Dunning communications | 304,773 |
| Customers | 13,000 |
| Aging buckets | 5 — current / 1–30 / 31–60 / 61–90 / 90+ |
| Reporting (as-of) date | 2026-06-30 |

It carries **signal, not noise.** Every customer gets a payment-behaviour
profile that drives a realistic overdue rate, so aging and DSO are meaningful and
the agent can actually find the slow payers:

| Behaviour profile | Overdue rate |
| --- | ---: |
| prompt | 0.5% |
| reliable | 1.7% |
| slow | 7.5% |
| delinquent | 19.1% |
| defaulter | 49.6% |

On this ledger that totals **~$1.47B overdue out of ~$2.98B outstanding**, with a
trailing-90-day **DSO ≈ 78 days** — the kind of numbers the agent computes on
demand from natural-language questions.

## Architecture

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

### One command (Docker)

```bash
cp .env.example .env          # then set GEMINI_API_KEY (and APP_API_KEY) in .env
docker compose up --build
```

Open <http://localhost:8000>. The image builds the React UI, installs the API,
**generates the synthetic ledger at build time**, and serves the UI + API from a
single container. The demo uses the cloud provider (Gemini); set `GEMINI_API_KEY`
in `.env`. `APP_API_KEY` guards the API and is baked into the UI build so the
same-origin browser can authenticate (keep the two equal).

### Local dev (two processes)

```bash
# 1) API — Ollama primary by default (run `ollama serve` + pull a tool model),
#    or set PRIMARY_PROVIDER=gemini + GEMINI_API_KEY in .env.
pip install -e ".[ollama,gemini,data,dev]"
python data/generate.py                       # build data/ledger.duckdb once
uvicorn src.api.app:app --reload              # http://localhost:8000

# 2) Web — Vite dev server, proxies /api to the API above.
cd web && npm install && npm run dev          # http://localhost:5173
```

### API

- `GET /api/health` — liveness + whether the agent is built (open, no key).
- `POST /api/chat` — `{ "message": "...", "history": [...] }` with an
  `X-API-Key` header → `{ "reply": "...", "tools_used": ["query_ledger", ...] }`.
  Interactive docs at `/docs`.

### Tests

```bash
pip install -e ".[dev]"
pytest            # offline: SQL guardrail, RAG indexer, API, MCP, and evals (76 tests)
```

## AI-native layer

Beyond the app, the ledger and the agent are built to be *operated by other AI
tools* — the surface AI-native teams care about:

- **MCP server** ([`mcp_server/`](mcp_server/)) — exposes the ledger to any MCP
  (Model Context Protocol) client (Claude Code, Claude Desktop, other agents) as
  a `query_ledger` tool, running the **same read-only guardrail** as the app, so
  a new surface is never a weaker one. `python -m mcp_server.server`.
- **Eval suite** ([`evals/`](evals/)) — golden questions scored by *properties*
  (used the right tool, cited the policy keyword, stated the right number within
  tolerance) rather than brittle string matching; numeric expectations are
  computed from the ledger. `python -m evals.run` gates a regression with a
  non-zero exit. The pure checks are unit-tested offline.
- **Claude Code skill** ([`.claude/skills/eval-agent/`](.claude/skills/eval-agent/))
  + [`CLAUDE.md`](CLAUDE.md) — the conventions and the eval-runner skill that let
  an AI collaborator work in this repo productively.

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

# CLAUDE.md — conventions for AI-assisted development

Guidance for any AI collaborator (and humans) working in this repo. Read this
first, then [`docs/STATE.md`](docs/STATE.md) for where things currently stand.

## What this project is

`receivables-agent` is a public portfolio project: a conversational AI agent
over **synthetic** accounts-receivable data. Clean room — it reimplements
patterns from scratch and contains no proprietary code or data.

## Golden rules

1. **English everywhere.** File and folder names, variables, functions,
   comments, commit messages, docs — all in English. No exceptions.
2. **Clean room.** Never copy code or data from other (private) projects.
   Reimplement patterns. All data is synthetic and generated locally.
3. **Secrets hygiene.** No keys in code or git. Read from the environment;
   `.env` is git-ignored; `.env.example` documents the variables.
4. **Plan before you code.** Question requirements, propose the approach, then
   implement. Record non-obvious choices in `docs/DECISIONS.md` (ADR style).
5. **Token-economy / context discipline.** Volatile state lives in
   `docs/STATE.md` (short, current); durable design in `docs/ARCHITECTURE.md`;
   decisions in `docs/DECISIONS.md`. Read on demand — don't reload everything.

## Where things live

- `data/`            — synthetic data generator + the policy document for RAG
- `src/agent/`       — LangGraph graph, tools (guarded SQL, policy search), providers,
                       plan-cache (`plan_cache` / `plan_replay` / `cached_agent`, ADR-009),
                       grammar-constrained tool-calls for tiny models (`constrained.py`, ADR-011)
- `src/rag/`         — ChromaDB indexer + embeddings
- `src/api/`         — FastAPI app (lifespan, auth, routes)
- `src/core/`        — config + Pydantic schemas + hardware-aware model
                       selection (`hardware.py`, ADR-010)
- `web/`             — React chat UI
- `mcp_server/`      — MCP server exposing the ledger as a tool (not `mcp/` — that
                       would shadow the MCP SDK package; see ADR-008)
- `evals/`           — golden-question eval suite (pure checks + live runner)
- `.claude/skills/`  — Claude Code skill(s)
- `tests/`           — pytest (the SQL guardrail is the priority suite)
- `docs/`            — ARCHITECTURE, DECISIONS, STATE

## Conventions

- Python >= 3.11, type hints, Pydantic v2 at boundaries.
- The SQL tool is **read-only** and allow-listed by design; never relax that to
  "make a query work" — fix the query or the schema hint instead.
- Unit tests must run offline (no live LLM calls).

## Definition of done (per feature)

- Code + types + a focused test.
- `docs/STATE.md` updated (what changed, what's next).
- A decision recorded in `docs/DECISIONS.md` if a non-obvious choice was made.

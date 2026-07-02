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

### Phase 4 — Full-stack + ship  ✅
FastAPI (async lifespan, API-key auth, Pydantic v2); React chat UI; Docker +
Compose, one-command run. README with an architecture diagram and the
methodology section. (Remaining ship gate: record the demo GIF + optional Space
deploy — see `docs/STATE.md`.)

### Phase 5 — AI-native layer (lightweight, in MVP)  ✅
An MCP (Model Context Protocol) server exposing the ledger query as a tool (same
guardrail as the app); a Claude Code skill that runs the evals; a small eval
suite (golden questions with property checks, ledger-verified expectations).
ADR-008. (The live accuracy *number* is captured on a machine with a provider —
see `docs/DEMO.md` / `docs/STATE.md`.)

### Phase 6 — Public zero-key "click-and-try" demo  ⏳ (next)

**Goal:** anyone opens the hosted URL and gets a working agent **without installing
anything or supplying an API key**, on a **free** CPU tier — while the "point a
strong model at it and it shines" story stays honest and front-and-center. The
**architecture is the product** (governed text-to-SQL + RAG + MCP), not the LLM.

**Design principle — graceful degradation.** Great with a tiny model, *instant* on
repeated questions, provably better with a strong model. This mirrors the repo's
existing dual-provider/fallback philosophy, so it reads as consistent, not bolted-on.

**Load-bearing correctness rule (do NOT violate):** never cache a question→**answer**
over mutable data — a frozen number is fragile and, in a showcase, dishonest (regenerate
the ledger with more/fewer customers, or test a new scenario, and a cached answer lies).
Cache the question→**plan** (which tool + the validated SQL + which policy chunks) and
**always re-execute the read-only SQL live**. The number is always fresh; only the
*reasoning* is skipped. (This is "caching the reasoning / structured intent", not output
caching — see the semantic-cache research; validated by SemanticALLI / Chillara 2026.)

**Layer 1 — Semantic plan-cache  ⬅ START HERE (desktop, no model needed).**
- Example questions pre-populate the cache with **plans** (guard-validated SQL), not answers.
- Every execution is **live + read-only** → number always correct, robust to mutable data.
- Novel user questions: embed → cosine-similarity lookup → **hit** reuses the plan (re-run
  live, ~tens of ms, LLM untouched) / **miss** calls the LLM and **warms** the cache.
- **Reuse the existing ChromaDB + MiniLM ONNX embeddings** (already in the repo for RAG) —
  no new dependency. Honesty guards: cache **read-only plans only**; conservative similarity
  threshold; and **every cached SQL is re-checked by `sql_guard` before execution** (fail →
  fall through to the LLM). Similar-text/different-intent ("check" vs "send") never serves an
  unsafe or wrong plan because the plan is re-validated and read-only.
- Expected hit-rate on a demo: 30–70% (visitors ask similar things: top overdue, DSO, aging).

**Layer 2 — Hardened tiny local LLM.**
- `qwen2.5:0.5b`/`1.5b` quantized (Q4_K_M), tool-capable, fits the HF free tier (16 GB / 2 CPU).
- **Grammar-constrained decoding (GBNF / `response_format`)** forcing valid tool-call JSON —
  kills the #1 failure mode of small-model tool-calling (malformed JSON). This is what makes
  a 0.5–1.5B model reliable; likely the best effort/robustness ratio in the whole phase.
- **KV / prompt caching** on the long, identical system-prompt prefix (schema hints + tool
  instructions) → only the new user turn is re-processed. A llama.cpp server flag, near-free.
- *Not doing:* custom tokenization — we use a pretrained model; token savings come from the
  already-lean schema hint + KV cache, not from touching the tokenizer.

**Layer 3 — Self-contained deploy + honest framing.** Reuse the forge-pdm F6 mechanics
*exactly* (they are already paid-for lessons): a separate `Dockerfile.hf` (distinct from the
Gemini `Dockerfile` — the Gemini image/compose stays intact on `main`), a dedicated
`space-deploy` branch with HF front-matter (`sdk: docker`, `app_port`) + LFS for the model
if needed, `deploy_space.sh`. The three container-only gotchas from forge-pdm F6 apply (HF
ignores `dockerfile_path` → literal `Dockerfile`; package-vs-source data paths resolve from
the script; bake at **startup** not build). Honest banner in UI + README: *"This live demo
runs a tiny local model on free CPU (slow, limited). The architecture — governed text-to-SQL
+ RAG — is the product; point it at Claude / GPT-4 / a Gemini key and it shines."*

**Layer 4 — README + honest numbers.** Live Space link at the top (like forge-pdm's `/health`);
capture the two still-pending real numbers (`evals.run` pass-rate + one latency) on the
notebook — ideally for **both** the tiny model (honest floor) and a strong model (the ceiling),
showing the delta the banner claims.

**Sequencing / effort:** ~2 sessions, zero cost. **Layer 1 first (this repo, on the desktop,
no model)** — it alone resolves the mutable-data concern and is the highest-value piece.
Layers 2–3 need the notebook (model + normal network, same constraint as the original ship
gate). Layer 4 closes it. → ADR-009 (plan-cache = cache reasoning not answers · grammar-
constrained tool-calls · self-contained zero-key image, separate from the Gemini path).

## MVP cut

Phases 0–4 plus a lightweight Phase 5. The MCP server, the skill and the
`CLAUDE.md` are the strongest signal for AI-native employers, so they stay in
the MVP. **Phase 6 is post-MVP polish** that turns the "shipped link" into a
"click-and-try" link (biggest UX gain for reviewers who won't install anything).

## Out of scope (for now)

- **PySpark** — deferred to a dedicated data-engineering showcase where large /
  distributed data justifies it. Here DuckDB is the right tool (ADR-001).

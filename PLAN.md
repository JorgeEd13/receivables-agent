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

**Hardware-awareness is load-bearing to this principle, and must be present from the
start (not bolted on later).** "Works fine as a demo, shines with a better model" is
only honest if the app *detects the box it runs on* and picks the model that fits:
the tiny grammar-constrained model is the **floor for the free HF CPU tier**, but on a
machine with more RAM / a GPU the same code should auto-select a stronger local model
(and a cloud key, when present, is always the ceiling). This is exactly what the
private FIA (`fleet_intelligence_agent`) proved with its `utils/hardware.py`
(RAM/VRAM/CPU detection → Ollama model recommendation, with stdlib-only fallbacks and a
`--select` machine-readable mode a bootstrap can `eval`). receivables is the **real
public showcase**, so the pertinent engineering from FIA belongs here — see Phase 7.

**Load-bearing correctness rule (do NOT violate):** never cache a question→**answer**
over mutable data — a frozen number is fragile and, in a showcase, dishonest (regenerate
the ledger with more/fewer customers, or test a new scenario, and a cached answer lies).
Cache the question→**plan** (which tool + the validated SQL + which policy chunks) and
**always re-execute the read-only SQL live**. The number is always fresh; only the
*reasoning* is skipped. (This is "caching the reasoning / structured intent", not output
caching — see the semantic-cache research; validated by SemanticALLI / Chillara 2026.)

**Layer 1 — Semantic plan-cache  ✅ (desktop, no model needed — ADR-009).**
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

**Layer 2 — Hardened tiny local LLM.  ✅ (ADR-011)**
Done: probed native tool-calling on `qwen2.5:0.5b`/`1.5b` and **refuted the
malformed-JSON premise** — the real failure is the native tool-calling *channel*
(0.5b invents fake args/omits `sql`; 1.5b writes SQL in prose, no tool call). Fix
shipped: `src/agent/constrained.py::ConstrainedToolModel` constrains the reply to
a `{tool, sql|query, answer}` JSON schema (Ollama `format`) + translates to
`tool_calls`; tier-gated in `providers.should_constrain` (auto-wrap tiny only) +
`ollama_keep_alive`/`num_ctx` for KV cache. Measured ≤1/5 → **5/5**; end-to-end on
the real agent+ledger, 1.5b completes the loop. `tests/test_constrained.py` (11).
Original scope below.
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

### Phase 7 — FIA-parity sweep + hardware-awareness  ⏳ (planned — next sessions)

**Why:** receivables is the **real public showcase for the AI-Engineer axis**, yet the
private FIA (`fleet_intelligence_agent`) carries engineering that never made it here.
Bring over everything **pertinent** (FIA is clean-room-safe to draw from: its
`hardware.py` holds **zero confidential data** — public Ollama model names + generic
`psutil`/`nvidia-smi` detection; the clean-room rule bars *confidential* material, not
reusing our own good engineering — copy-and-adapt or rewrite-if-better, keeping it in
this repo's English/style). Do the audit first, then port by pertinence.

**7.1 — Hardware-aware model selection (headline; do first, feeds Phase 6).  ✅ (ADR-010)**
Done: `src/core/hardware.py` (RAM/VRAM/CPU detection, public LLM catalog,
effective-memory heuristic, `--json`/`--select` CLI), wired into
`resolve_ollama_model` in `src/agent/providers.py` via `OLLAMA_MODEL=auto` (new
default). Offline tests in `tests/test_hardware.py`; verified live on the notebook
(RTX 4050 → `auto` picks `qwen2.5:7b`). Original scope below.
- Port/reimplement FIA's `utils/hardware.py`: detect RAM / VRAM (nvidia-smi) / CPU;
  a small **public** Ollama model catalog (name, RAM/VRAM need, quality) → recommend
  the best model that *fits*; "effective memory" heuristic (VRAM if GPU, else ~80% RAM).
- Wire it into `src/agent/providers.py` model selection so the local path **auto-picks
  tiny-vs-strong by detected hardware**: the Q4 tiny model is the HF-CPU floor, a bigger
  local model is chosen automatically where the box allows, cloud key = ceiling. This is
  what makes Phase 6's "shines with a better model" claim *real from the start*, not a
  README promise.
- Keep FIA's nice touches where they earn their place: a diagnostic CLI
  (`python -m ... hardware` → readable table) and a `--select` machine-readable mode a
  container entrypoint / compose can consume. Offline-testable (mock the detectors) so
  it fits the "unit tests run offline" rule.
- → new ADR (hardware-aware selection; catalog is public data; clean-room note that
  reusing FIA engineering ≠ leaking the employer data).

**7.2 — FIA→receivables parity audit (the rest).** A **dedicated full-scan session**:
walk the whole FIA feature set and port what's pertinent to a public AR-agent showcase
(skip anything the employer/GPS-specific). **Accelerator (start here, don't stop here):** mine
`(private career repo)` for the FIA "engenuity" entries — they already flag
the CV/post-worthy techniques, so they're the fastest index of what's worth transferring.
Then sweep the FIA repo itself for anything the ACHADOS didn't capture. Candidates to
check — richer provider fallback ergonomics, the hardware diagnostic UX, any
guardrail/prompt refinements, RAG/indexer niceties, `.env`/config ergonomics. Record
kept / improved / dropped per item (ADR or a short table), so the sweep is auditable and
not a vague "port stuff" task.

**7.3 — Compose upgrade (the pertinent IaC step here).** Today's `docker-compose.yml` is
**single-service and Gemini-only** (it even pins `PRIMARY_PROVIDER: gemini`, overriding
`.env` — a documented gotcha). Upgrade it to a **multi-service** local stack (app +
`ollama` service + a models volume) so `docker compose up` stands up the *whole* local
agent, hardware-aware model and all — turning the file from "containerized" into real
orchestration and killing the "Ollama-in-Docker unsupported" gotcha. (The heavier
IaC/managed-cloud rungs — Cloud Run + managed DB — are already owned and largely shipped
by the sibling `forge-pdm-mlops`; no need to duplicate them here.)

## MVP cut

Phases 0–4 plus a lightweight Phase 5. The MCP server, the skill and the
`CLAUDE.md` are the strongest signal for AI-native employers, so they stay in
the MVP. **Phase 6 is post-MVP polish** that turns the "shipped link" into a
"click-and-try" link (biggest UX gain for reviewers who won't install anything).

## Out of scope (for now)

- **PySpark** — deferred to a dedicated data-engineering showcase where large /
  distributed data justifies it. Here DuckDB is the right tool (ADR-001).

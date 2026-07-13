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

### Phase 6 — Public zero-key "click-and-try" demo  ✅ LIVE (only the strong-model number deferred)

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

**Layer 3 — Self-contained deploy + honest framing.  ✅ (ADR-012, LIVE)**
Done + deployed from the **desktop** (Docker daemon + git-push-to-HF both work past the corp
TLS proxy). `Dockerfile.hf` (multi-stage; installs the Ollama binary + `zstd`; bakes the
ledger; non-root UID 1000; pins `OLLAMA_MODEL=qwen2.5:1.5b` for a deterministic demo),
`requirements-hf.txt` (runtime + `langchain-ollama`, no Gemini SDK), `scripts/hf_entrypoint.sh`
(`ollama serve` → pull → serve uvicorn), `scripts/deploy_space.sh`, `docs/DEPLOY.md`. Deployed
via a `space-deploy` branch (HF front-matter README + the image as the literal `Dockerfile` +
`.gitattributes` LFS-tracking `*.png`/`*.gif`). Honest security + latency banners in DEPLOY.md
and the UI. **LIVE: https://jorgeed-receivables-agent.hf.space.** Build gotchas (each fixed,
Findings worth writing up: corp-CA needed for in-build pip/npm/ONNX (git-ignored, no-op on HF); `zstd`; the ONNX
embedder must be baked into **appuser's** cache (root-download → runtime re-download corrupted
= `INVALID_PROTOBUF` crash).

**Layer 4 — README + honest numbers.  ◑ (tiny side done; strong side deferred to GPU).**
README carries the live "try it" badge + link. The tiny-model live link and its own behaviour
are shipped and verified; the **tiny-vs-strong** number (`evals.run` pass-rate + one latency vs
a strong model, the delta the banner claims) still needs the **notebook GPU** — deferred.

**Layer 5 — Demo hardening (from live browser testing).  ✅ (ADR-012/013, LIVE).**
Real use surfaced demo-quality problems that unit tests couldn't; each fixed + verified live:
- **SSE streaming** (`POST /api/chat/stream`, `CachedAgent.astream`): the UI shows the agent
  *thinking* live — `cached` / `tool` (Querying the ledger / Reading the policy) / `answer`
  events + an elapsed timer — so a slow tiny-CPU answer never looks frozen (turns the free-tier
  slowness into a "watch it think" feature).
- **Baked curated plan-cache** (`data/curated_plans.py`, `seed_plan_cache.py --curated`, baked in
  `Dockerfile.hf`): the 8 showcased questions are **instant (~1.5 s) + correct from the first
  request after every deploy** — no warm window, no tiny-model mis-routing on the showcased path.
- **Tiny-model prompt/routing/recursion (ADR-013):** a 57%-shorter tiny-tier prompt (729→310
  tok) with one firm routing rule (default `query_ledger`; policy only for explicit rule Qs) +
  `agent_recursion_limit=8` and a graceful `GraphRecursionError` message — a novel "top 5" now
  uses **only** the ledger and answers (~80 s) instead of over-calling the policy and thrashing
  ~250 s.
- **Cache hits mid-conversation** (dropped the `len(messages)!=1` guard on lookup) + **no
  auto-scroll-yank** while reading + a persistent **"Instant:" quick-bar**.

**Deferred (next session — pick up cleanly, nothing to reinvent):**
- **Widen the curated seed set** — ◑ STARTED 2026-07-05: added a **top-N-per-aging-bucket** window
  query (`QUALIFY row_number() OVER …`) the tiny model can't write, under 4 phrasings + a UI chip
  (from the live finding that "top 5 of each age group" hit the ceiling). Still the highest-value
  remaining demo win — keep going, fully desktop-doable.
  Add more **natural phrasings** of the showcased questions (e.g. "top 5 / which 5 / the most
  overdue money" → the top-overdue plan; DSO / aging rewordings) and a few more question types,
  so more of what a reviewer *types* is an **instant cache hit** instead of an ~80 s live run.
  Mechanically: append entries to `data/curated_plans.py` (each a guard-valid `Plan`) — the SQL
  is re-run live so numbers stay fresh; add the new UI chips (keep `test_curated_questions_match_
  ui_suggestions` green); rebuild bakes them. Jorge's idea to also cache **computed key facts**
  fits here as curated plans (the plan re-runs the SQL, so it's never a frozen value).
- **Layer 4 strong-model number** — on the notebook GPU: `evals.run` pass-rate + one latency vs a
  strong model → the tiny-vs-strong delta in the README.

**Sequencing / effort:** Layers 1–3 + 5 all shipped (Layer 1 on the desktop; 2–5 built AND
deployed from the desktop once Docker + git-to-HF were proven to work past the proxy — the
notebook was NOT required after all). → ADR-009 (plan-cache), ADR-010 (hardware-aware),
ADR-011 (grammar-constrained), ADR-012 (self-contained tiny-Ollama Space + baked cache/ONNX),
ADR-013 (tiny-model prompt/routing/recursion).

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
  reusing my own prior engineering ≠ leaking any employer's data).

**7.2 — parity audit against my prior private agent (the rest).** A **dedicated full-scan
session**: walk that project's feature set and port what's pertinent to a public AR-agent
showcase (skip anything employer- or GPS-specific). **Accelerator (start here, don't stop
here):** my private engineering-findings log already flags the techniques worth writing up,
so it's the fastest index of what's worth transferring. Then sweep the private repo itself
for anything the log didn't capture. Candidates to
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

### Phase 8 — Agent reliability at the tiny-model budget ceiling  ✅ (SHIPPED 2026-07-05, ADR-014)

**DONE (2026-07-05, ADR-014).** All of Phase 8 shipped in two same-day slices; **suite 134 green**.
New `src/agent/turn_control.py` + wiring in `tools.py` / `graph.py` / `cached_agent.py` / `web/`;
`tests/test_turn_control.py` (16, offline).
- **8.2 — redundant-call dedup.** `ToolCallTracker.wrap` memoizes an identical tool call within a
  turn (whitespace-normalized, case-preserved) → returns the prior result + a firm nudge, no
  re-execution. State lives in a `ContextVar` (concurrency-safe under the process-wide shared agent).
- **8.1/8.3 — forced finalization.** On the ceiling, `finalize_answer` composes a deterministic
  partial answer from the recorded observations (latest *successful* ledger query + policy finding +
  the gap + a narrowed next step) instead of the canned apology. Wired into `invoke`/`ainvoke`
  (previously *raised*) **and** `astream`.
- **8.5 — narration + the step/time split.** `narrate_start`/`narrate_end` stream a human `step`
  line per tool start/end (UI renders it live). The **silent** path keeps the tight cap (8); the
  **narrated** streaming path gets a higher cap (16) **plus** a soft wall-clock budget (45 s) that
  narrates a wrap-up and finalizes — the honest split of ADR-013's one cap into a loop guard + a wait
  guard. Cap raised only *with* narration + dedup + finalization.
- **8.4 — "continue" affordance: satisfied-by-design, no checkpointer.** The API already resends full
  history per turn, so a follow-up carries prior context, and 8.1's next-step invite makes it useful;
  a LangGraph checkpointer would *double-count* history. Recorded as a decision in ADR-014, not built.

**Original scoping below (kept for the record).**

**Read ADR-013 + ADR-009 first — this phase IMPROVES existing seams, it does NOT add them.**
What already exists (do not re-implement): (a) a **graceful recursion ceiling** — `CachedAgent.astream`
catches `GraphRecursionError` and yields a graceful answer (`src/agent/cached_agent.py`, ADR-013);
(b) a **deliberately low cap** `agent_recursion_limit=8` — chosen in ADR-013 *specifically* to fail an
unproductive loop in seconds instead of the **200–250 s thrash** the LangGraph default (25) caused.
**Re-examined 2026-07-04 (Jorge's point):** the cap of 8 conflates *two* concerns — a **loop guard**
(stop a degenerate cycle) and a **wait guard** (don't make the user wait). The 200–250 s thrash was bad
not only because it was *long* but because it was **silent and ended in no answer**. So the honest
refinement is **not** "keep 8 forever" nor "blindly raise it" — it is: *a narrated, progress-visible
wait with a guaranteed final answer is tolerable*, which lets the step cap rise **when paired with 8.5
(progress narration) + 8.2 (dedup, so the extra steps are productive) + 8.3 (forced finalization, so it
always ends in an answer)**. Raising the cap **alone** would just re-create the silent thrash — that is
the part that stays wrong. See 8.5. (c) a **semantic
plan-cache** for *successful* runs that replays them for repeat questions (ADR-009); (d) a compact
tiny-model prompt + one firm routing rule (ADR-013) and grammar-constrained tool-calls (ADR-011).

**Observed behavior (the motive — this is *why* the phase exists, not a guess):** on novel typed
questions the free-CPU tiny model still (i) **over-calls tools** — ADR-013 documents it calling *both*
`query_ledger` and `search_policy` when the policy is irrelevant, then looping — so it **burns the
8-step budget on redundant/repeated calls** and hits the ceiling *without ever answering*; and (ii) when
it hits the ceiling, the graceful message is **generic and dead-ends** — *"couldn't finish on the
free-tier tiny model — try an example or rephrase"* — which surfaces **nothing it actually found** and,
in Jorge's words, "reads as 'I just don't work for no apparent reason.'" The goal: **fewer wasted cycles
(so the same 8-cap goes further) + a far more useful outcome when the cap is still hit.**

**What we will do (each with its reason; NONE of these is "raise the limit" or "add a ceiling"):**

- **8.1 — A richer graceful answer instead of the canned apology.** *Why:* the current message throws
  away the partial work. *Fix:* on hitting the ceiling, spend the reply surfacing **what it did gather**
  (the tools it ran, any partial `query_ledger` result) **+ one specific, narrowed next step** ("I pulled
  the overdue list but didn't rank it — ask me to sort by amount"), not a generic "rephrase." Needs the
  partial run state (see 8.4's checkpointer).
- **8.2 — Redundant-call short-circuit / loop-dedup.** *Why:* the exact ADR-013 failure is *wasted*
  steps (calling both tools, or re-calling the same tool with the same args). *Fix:* detect a repeated
  identical tool call (and the "called `search_policy` on a pure-data question" over-call) and
  short-circuit it — return the prior result and nudge "you already have this; now answer or try a
  *different* action." **This is what actually buys "more possibilities on a tiny model" (Jorge's goal):
  not a bigger budget, but not squandering the budget we have** — fully consistent with ADR-013's
  fail-fast philosophy.
- **8.3 — Budget-aware forced finalization (turn the hard error into a real answer).** *Why:* hitting
  `GraphRecursionError` means *no* answer was produced; catching it after the fact can only apologize.
  *Fix:* track step count in graph state and at `steps == limit − 1` **route to a `finalize` node** that
  forces "answer from what you have, and state the gap honestly" *instead of* another tool call — so the
  last cycle produces a best-effort partial answer rather than an exception. This is the honest form of
  Jorge's "use the last of its resources to conclude."
- **8.4 — A "continue" affordance via LangGraph's checkpointer (the honest form of "cache & resume").**
  *Why:* Jorge's instinct — "cache the run conclusions so it can return and run again without losing
  itself" — is right in goal, and LangGraph **already provides thread-level state persistence** (a
  checkpointer + `thread_id`), so we *use* that rather than hand-rolling a cache. *Fix:* give each
  conversation a `thread_id` + a persistent checkpointer so a follow-up message **continues the same
  thread with all prior context** (distinct from the ADR-009 plan-cache, which replays *finished*
  successful runs). **Honest caveat to preserve for future-me:** *blindly auto-re-running* a tiny model
  that just looped will **re-loop on the same novel question** (ADR-013's evidence) — so "resume" only
  helps when paired with 8.2 (dedup) + 8.3 (finalization); it is a *user-driven continue*, not an
  automatic replay of a failed run.

- **8.5 — Progress narration → a *tolerable* longer wait, so the cap can rise (Jorge's 2026-07-04
  insight, the reframe that ties the phase together).** *Why:* ADR-013 cut the cap to 8 to avoid a
  **silent** 200–250 s wait that ended in nothing — but the wait was intolerable because it was
  *invisible*, not merely because it was *long*. If the agent **narrates its progress** as it works —
  *"Looking up the overdue ledger… found 12 overdue accounts, now checking the late-fee policy…"* —
  then a longer, genuinely-productive run becomes acceptable, and we stop **cutting off questions that
  just needed a few more steps**. *Fix:* stream a short human-readable **intent/finding line per step**,
  not just the tool name. **What already exists:** `CachedAgent.astream` already emits an SSE `tool`
  event per `on_tool_start` (the "watch it think" streaming, ADR-012) — so the pipe is there; 8.5
  *upgrades* it from "using `query_ledger`" to a templated intent line derived from the tool + its args
  (and, where the model's ReAct "thought" is clean enough, a distilled version of it). **Then raise the
  step cap** to a higher, tuned ceiling for this narrated path (e.g. 8 → ~15–20), safe because 8.2 keeps
  the extra steps productive and 8.3 guarantees a final answer. **Add a separate wall-clock budget
  (honest engineering nuance):** on a 2-vCPU box "tolerable wait" is really about *seconds*, and the
  step cap is a clumsy proxy for time. Split the two concerns ADR-013 fused — a **step cap** as the loop
  guard and a **soft wall-clock budget** ("if we've spent > N s, narrate that and finalize now, 8.3") as
  the wait guard. Narration keeps those seconds engaging instead of dead. **Honest caveat to preserve:**
  narration does *not* fix a true degenerate loop — narrating "looking for X… looking for X…" is honest
  but still not an answer; that is exactly why 8.5 is only safe *with* 8.2 (dedup breaks/short-circuits
  the loop) and 8.3 (finalization ends it). 8.5 is the **framing** of the phase; 8.2/8.3 are what make it
  safe.

**DoD.** A question that previously hit the ceiling now: (a) **narrates its progress** step by step
(8.5) so a longer run is visible, not a frozen spinner; (b) is allowed **more productive steps** (raised
step cap) *and* is bounded by a **soft wall-clock budget** that triggers a graceful finalize; (c) ends in
either a finalized partial answer naming what it found + the gap (8.1/8.3) or a faster settle because a
redundant call was short-circuited (8.2); a follow-up continues the thread with context (8.4). The
**silent** thrash must not return — a raised cap is only shipped together with narration + dedup +
finalization; verify the worst case (a true loop) still ends in a narrated finalize within the wall-clock
budget, never a bare error or dead air. Offline tests cover the dedup, the boundary finalization, the
per-step narration events, and the wall-clock finalize. New ADR-014 (references ADR-013/012/009).
Showcase framing: *"I made a tiny model degrade gracefully — it narrates what it's doing, doesn't waste
cycles, and always ends in an answer — instead of freezing then apologizing,"* a strong, honest
AI-reliability story.

### Phase 9 — Demo product-polish: theme + internationalization (i18n) + friendlier UX  ✅ (SHIPPED 2026-07-06, ADR-015)

**DONE (2026-07-06, ADR-015).** 9.1 (light/dark theme — `theme.js`, CSS custom props,
`prefers-color-scheme` default + a persisted `data-theme` override that wins both ways) + 9.2
(EN/PT-BR i18n — `i18n.js`, a string dict + `translator()` closure, no framework; theme +
language toggles in the header) shipped. The **honesty boundary is stated in-UI** (an `i18nNote`
in both languages: the interface is localized, the agent's English-corpus answers are not
machine-translated). The seeded example questions stay **English in every locale** on purpose
(plan-cache keys, ADR-009, + the verbatim text sent to the agent — a PT-BR paraphrase would miss
the 0.90 cache and hit the slow tiny model). `npm run build` green (34 modules); `web/dist`
rebuilt for the container; no regression to streaming/plan-cache. Paired design language with
`forge-pdm-mlops` F9 (ADR-018). 9.3 (friendlier affordances) folded in via the localized
progress states + hints. Details below.

**Why (observed):** the React chat UI (`web/`) is **single-theme and English-only** (one `web/src/styles.css`,
no locale layer) — Jorge's note: "both demos have only one language and one theme; testers looking for
variety and versatility deserve better." This is also a deliberate **front-end/product showcase** play
(a strong-but-underexplored axis). **Paired with the sibling `forge-pdm-mlops` F9** (same theme + i18n
work on its `/demo`) so the two public demos share a design language — do them close together.

**What we will do:**
- **9.1 — Light/dark theme.** A theme toggle that is **theme-aware** (honours `prefers-color-scheme` as
  the default signal, plus a manual toggle that persists). *Why:* first impression + accessibility; cheap,
  high-polish signal.
- **9.2 — Internationalization (i18n), EN + PT-BR at least.** A **lightweight** locale layer (a small
  string dictionary + a context/hook — no heavy i18n framework needed for two locales), a language toggle,
  and translated UI chrome + the seeded example questions. *Why:* versatility for a broader tester pool;
  Jorge works bilingually. **Honesty note to preserve:** the **agent's answers** come from the model and
  the (English) policy corpus — i18n covers the **UI shell + examples**, not on-the-fly translation of
  model output (that would be a separate, larger claim; call it out, don't silently imply it).
- **9.3 — (optional) friendlier chat affordances** — clearer streaming/typing states, a plain-language
  one-liner on what the agent can do, tidier example-question chips.

**DoD.** Theme toggle works in both schemes and persists; the UI + example questions render in EN and
PT-BR via the toggle; the honesty boundary (UI-localized ≠ answer-translated) is stated in the UI/README;
no regression to the streaming/plan-cache paths. ADR-015 if a non-obvious choice is made (e.g., the i18n
approach). Coordinate the visual language with `forge-pdm-mlops` F9.

## MVP cut

Phases 0–4 plus a lightweight Phase 5. The MCP server, the skill and the
`CLAUDE.md` are the strongest signal for AI-native employers, so they stay in
the MVP. **Phase 6 is post-MVP polish** that turns the "shipped link" into a
"click-and-try" link (biggest UX gain for reviewers who won't install anything).

## Out of scope (for now)

- **PySpark** — deferred to a dedicated data-engineering showcase where large /
  distributed data justifies it. Here DuckDB is the right tool (ADR-001).

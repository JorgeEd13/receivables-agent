# STATE — receivables-agent

> Volatile, short, current. Update at the end of each work session.

## Current focus

**ADR-022 — the SQL guardrail was broken, and is now rebuilt on DuckDB's own parser. ✅ SHIPPED
2026-07-30.** An external review found one bypass (the relation allow-list never looked inside a
derived table). Repairing it triggered **three rounds of adversarial review by an instance with
no knowledge of the design**, which found much worse — all verified end-to-end against a real
connection, not argued on paper:

- `_mask_literals` modelled `'…'` but **not** dollar-quoting (`$a$…$a$`), escape strings
  (`e'\''`) or quoted identifiers containing an apostrophe. Any of the three flips quote parity
  and blanks **the entire rest of the statement** out of every check — while the unblanked
  original is what executes. Read `duckdb_settings()`, the ledger's file path, forbidden tables.
- The same desync hid `;`: closing the guard's own `SELECT * FROM (…)` wrapper early made
  **`CREATE TEMP TABLE` / `VIEW` / `MACRO` and `PREPARE` succeed on a `read_only=True`
  connection**, persisting for the session.
- `SHOW ALL TABLES` enumerated the catalog past two empty allow-lists; CTE names exempted tables
  in sibling scopes, forward-declared, and inside their own bodies; a CTE name with a quoted dot
  defeated the schema check.

**The scanner was replaced, not patched:** the statement is parsed by the **same parser that
executes it** (`extract_statements` + `json_serialize_sql`) and every check runs on the syntax
tree, so parser-vs-executor disagreement is impossible by construction rather than merely fixed.
Full reasoning in **ADR-022**.

**Measured, and it changed a decision:** on DuckDB 1.5.3 the **anchor** term of
`WITH RECURSIVE x AS (SELECT c FROM x UNION ALL …)` binds to a real base table named `x` and
returns its rows — so "a CTE's own name is exempt in its own body" cannot be made safe by
restricting it to the recursive form. **Recursive CTEs are now refused on purpose**, recorded in
`tests/test_sql_guard_adversarial.py` with the measurement.

**It also got LESS restrictive where it was wrong:** `EXTRACT(year FROM d)`, `SUBSTRING … FROM …
FOR`, `TRIM(BOTH … FROM …)`, `DECIMAL`/`VARCHAR` casts, `WITH t(a)`, `now()`, **`INTERVAL 30
DAY`** and schema-qualified references to allow-listed tables were all being refused. The old
relation scanner read the `FROM` inside `EXTRACT` as a relation list.

**261 tests** (135 pre-existing + 126 adversarial kept as a regression floor). **First CI
workflow this repo has ever had** — there was none, so nothing ran unless someone remembered —
plus ruff + mypy gates, both clean.

> ⚠️ **OPEN — availability.** The guard bounds what can be READ, not how much WORK a query
> may do. `WITH RECURSIVE invoices(n) AS (… n < 100000000) …`, `repeat('a', 1000000000)` and a
> six-way self-cartesian join are all **accepted** and never return; the outer `LIMIT 200` caps
> rows *returned*, not rows *computed*. Confidentiality held under every vector tried.
> Needs a design call (watchdog interrupt / row budget / killable subprocess) — see ADR-022.

> ⚠️ **OPEN — the live demo still runs the vulnerable guard.** `space-deploy` is **43 commits
> behind `main`** (drifting since Phase 6). `git push origin` does not deploy the HF Space. This
> needs a dedicated session: the cherry-pick could not be validated here because checking out
> `space-deploy` fails on an LFS smudge error (`assets/logo.png`).

> ⚠️ **Verification lesson from this session, worth more than the fix.** The lint gate was
> reported green locally and failed in CI minutes later. The check was
> `ruff check . -q | grep '^Found'` — and `-q` **suppresses that very line**, so the grep could
> never match and "clean" was printed unconditionally. Verify tools by **exit code**, never by
> grepping their output. Also: adding a CI workflow and not polling `gh run list` in the same
> session leaves a new gate indistinguishable from a passing one.

**Phase 9 — demo product-polish (light/dark theme + EN/PT-BR i18n) — ✅ SHIPPED 2026-07-06
(ADR-015).** The React chat UI was single-theme (dark navy) and English-only; Phase 9 adds a
**light/dark theme** (CSS custom props; `prefers-color-scheme` default + a persisted
`data-theme` override that wins both ways — `theme.js`) and a **lightweight EN/PT-BR i18n
layer** (string dict + `translator()` closure, no framework — `i18n.js`), with theme +
language toggles in the header. **Honesty boundary held (ADR-015):** i18n localizes the **UI
shell only** — the agent answers from the model + the **English** policy corpus and is NOT
machine-translated; an `i18nNote` says so in both languages. **The seeded example questions
stay English in every locale on purpose** — they are plan-cache keys (0.90-similarity, ADR-009)
*and* the text sent verbatim to the English-corpus agent, so a PT-BR paraphrase would miss the
cache and hit the slow tiny model. `npm run build` green (34 modules, was 32; `web/dist`
rebuilt for the container). No regression to the streaming/plan-cache paths. **Paired with
`forge-pdm-mlops` F9 (ADR-018)** for one shared design language (hypercube navy+cyan). Front-end
showcase (a strong, under-shown axis). ADR-015 written; PLAN Phase 9 done.

> **Prior:** Phases 0–5 done. Phase 6 L1 (plan-cache) + L2 (grammar-constrained, ADR-011) +
7.1 (hardware-aware, ADR-010) done. Suite **110 green**. **Phase 6 LAYER 3 SHIPPED
+ LIVE (ADR-012): the self-contained tiny-Ollama Space is deployed and answering.**
🚀 **https://jorgeed-receivables-agent.hf.space** — `/api/health` ok, a seeded
question replays in ~3 s via the plan-cache (real `query_ledger`, 13,000 customers).
Built, smoke-tested, AND deployed from the DESKTOP (Docker + git push to HF both work
here). README carries the live link. **NEXT — Layer 4: tiny-vs-STRONG number** on the
notebook GPU (run `evals.run` + one latency vs a strong model for the "shines with a
better model" delta); everything else for the live link is done.

**Phase 8 — agent reliability at the tiny-model budget ceiling — ✅ SHIPPED 2026-07-05 (ADR-014).**
All of it, two same-day slices, **suite 134 green**: dedup (8.2) + forced finalization (8.1/8.3) +
progress narration with a raised narrated cap split from a soft wall-clock budget (8.5); the
"continue" affordance (8.4) is satisfied-by-design (history-resend), no checkpointer added. Details
below + in PLAN.md / ADR-014. Original scoping (IMPROVE the existing ADR-013 graceful ceiling, do
NOT add one): Motive: observed the tiny model burning its 8-step budget on redundant tool calls then
dead-ending on a generic apology that "reads as 'I just don't work'." Fixes: richer partial
answer + redundant-call dedup + budget-aware forced finalization + `thread_id`/checkpointer
"continue" affordance + **progress narration (Jorge's insight)**. Key reframe: the cap of 8 fused
a *loop guard* and a *wait guard* — the 200–250s thrash was bad because it was **silent + answerless**,
not merely long; so **narrating progress lets the cap RISE safely** (paired with dedup so extra steps
are productive + forced finalization so it always answers), plus split off a **soft wall-clock budget**
as the real wait guard. NOT "blindly raise the cap" (that re-creates the silent thrash). **Phase 9
— demo product-polish** (light/dark theme + EN/PT-BR i18n on the React UI; paired with
`forge-pdm-mlops` F9). Both scoped from Jorge's 2026-07-04 review; ADR-014 (Phase 8) / ADR-015
(Phase 9) when built.

> **2026-07-05 — Phase 8 follow-up (from live testing): curated top-N-per-bucket plan + richer
> no-gather finalization.** Live test surfaced a real gap: a natural follow-up — *"give me the top 5
> of each age group"* — hit the ceiling and returned the bare no-progress floor. Two fixes: **(1)** a
> **curated plan** (`data/curated_plans.py`) for top-5-customers-*within-each*-aging-bucket — a
> `QUALIFY row_number() OVER (PARTITION BY aging_bucket …)` window query the tiny model reliably
> can't write — under 4 phrasings + a 5th UI chip, validated against the live ledger (20 rows, 5/bucket);
> now instant + correct instead of a failure. **(2)** richer finalization: when the model *attempted*
> the ledger but every query **errored** (vs gathered nothing at all), `finalize_answer` now names the
> attempt + gives a concrete decomposition (*"ask for one group at a time, e.g. the top 5 in the 90+
> bucket"*) instead of the generic floor (`_NO_USABLE_RESULT` vs `_NO_PROGRESS`). +2 tests, **suite
> 135**, UI rebuilt. ADR-014 (the "widen curated seeds" deferred item, now partly done).
>
> **2026-07-05 — Phase 8 slice 2: progress narration + step/time guard split (8.5), 8.4 closed.**
> The narrated streaming path now streams a human `step` line per tool start/end (`narrate_start`/
> `narrate_end`: "Checking the collections policy on '…'" → "Found 12 rows in the ledger"), rendered
> live by the UI (`web/src/App.jsx`, coalescing repeats). ADR-013's single step cap is **split into
> two guards**: the *silent* `invoke`/`ainvoke` path keeps the tight cap (8, `agent_recursion_limit`)
> because a silent long wait reads as hung; the *narrated* `astream` path gets a higher cap (16,
> `agent_narrated_step_cap`) **plus** a soft wall-clock budget (45 s, `agent_wall_clock_budget_s`)
> that narrates a wrap-up and finalizes — a visible, time-bounded, productive run instead of a silent
> thrash. The cap only rose *because* narration + dedup + finalization ship with it. Clock is
> injectable (tests don't fight asyncio's own `time.monotonic`). **8.4 (checkpointer "continue") is
> closed as satisfied-by-design:** the API already resends full history per turn, so a follow-up
> carries context and 8.1's next-step invite makes it useful — a checkpointer would double-count
> history (decision in ADR-014). +4 tests (narration lines, `step` events on a normal run, wall-clock
> early-finalize). **Suite 134.** UI rebuilt (`npm run build` OK). Phase 8 COMPLETE.
>
> **2026-07-05 — Phase 8 slice 1: graceful ceiling = dedup + forced finalization (ADR-014).**
> Fixes the worst live first-impression: a novel typed question that hit the step cap dead-ended on
> a generic apology that "reads as 'I just don't work'." Now (a) an **identical repeated tool call**
> is served from a per-turn memo + a firm nudge instead of re-executing (8.2 — "don't squander the
> budget we have"), and (b) on a ceiling hit the agent **finalizes from what it gathered** — the
> latest *successful* ledger query and/or a policy finding + the specific gap + a narrowed next step
> (8.1/8.3) — instead of the canned message. New `src/agent/turn_control.py` (`ToolCallTracker`
> dedup + observation log in a **`ContextVar`** so the process-wide shared agent is concurrency-safe;
> `finalize_answer`, deterministic — surfaces only what a tool actually returned, never a fabricated
> number; errored/guard-rejected queries don't count as facts). Tools gained an optional `wrap`;
> wired into `invoke`/`ainvoke` (which previously **raised** on the ceiling) **and** `astream`.
> `tests/test_turn_control.py` (12, offline, incl. a looping-stub end-to-end through both invoke and
> astream, and dedup through a real `StructuredTool`). **Suite 130 green.** Step cap deliberately
> **unchanged at 8** — raising it is only safe with 8.5 (narration) + this dedup + finalization, per
> the phase reframe. **Next:** 8.5 (progress narration → a raised, narrated cap + a soft wall-clock
> budget), then 8.4 (a "continue" affordance via a LangGraph checkpointer). Live-on-Space
> confirmation of the tiny-model path is deferred (the tiny-CPU loop can't run cheaply here), as
> with ADR-013's streaming.
>
> **2026-07-03 — Cached questions now hit MID-CONVERSATION + scroll no longer yanks
> (VERIFIED LIVE).** Live transcript showed a curated question worked only as the FIRST
> message: `_try_cache` bailed on `len(messages) != 1`, so every suggested question clicked
> AFTER an answer carried history → bypassed the cache → slow model → recursion limit
> ("Sorry, need more steps"). Fix: look up the **latest** user question regardless of history
> (a cached plan is self-contained, ADR-009; a real follow-up won't clear 0.90, so it still
> falls to the LLM). Warming stays single-turn. Also: chat auto-scrolled on every tick →
> yanked a reader down; now only scrolls when already near the bottom. **Verified LIVE:** three
> curated Qs in a row, each WITH accumulating history, all `cached=True` in 1.0–1.5 s. Tests:
> known-Q hits after prior turns; novel follow-up still → LLM. **Suite 118.** (`deploy_space.sh`
> now requires explicit commit SHAs — auto-detect is unsafe on the LFS-rewritten space-deploy.)
>
> **2026-07-03 — Tiny-model prompt/routing/recursion (ADR-013) + UI quick-bar — VERIFIED
> LIVE.** From browser testing: a novel "which 5 customers have the most overdue money?"
> over-called `search_policy`, looped, thrashed ~250 s, then "did not return an answer".
> Fixes: a **57 %-shorter** tiny-tier prompt (`SYSTEM_PROMPT_BRIEF`/`SCHEMA_HINTS_BRIEF`,
> 729→310 tok) with ONE firm rule (default `query_ledger`; policy only for explicit rule
> Qs; then stop); `agent_recursion_limit=8` on every call so a loop fails in seconds and
> `astream` catches `GraphRecursionError` → graceful "couldn't finish, try an example"
> answer. UI: a persistent **"Instant:" quick-bar** keeps cached Qs one click away after the
> first answer (was empty-chat only). **Verified on the live Space:** the same question now
> uses **only `query_ledger`**, no policy, and **answers in ~82 s with real customers/numbers**
> (Lang/Velasquez/Johnson $13.37 M…) — correct, single-tool, completes, live progress the whole
> time. Also fixed `deploy_space.sh` (patch-id forwarding via `git cherry` + auto-sync the
> literal Dockerfile). **Suite 117.** Still: tiny-CPU is slow by nature (deferred: widen
> curated seeds; Layer 4 tiny-vs-strong on GPU). ADR-013.
>
> **2026-07-03 — Baked curated plan-cache + ONNX model (instant & correct from first
> request).** Every redeploy wiped the plan-cache (warmed at startup) → a multi-minute slow
> window even on the suggested chips. Now baked at BUILD time: `data/curated_plans.py`
> (hand-authored, `sql_guard`-validated plans for the 8 seed Qs — correct SQL/policy queries,
> re-run LIVE on a hit so ADR-009 honesty holds), `seed_plan_cache.py --curated` (model-free),
> run in `Dockerfile.hf` after the ledger bake → `data/chroma` ships warm (count=8, verified
> in-image). Entrypoint dropped the slow tiny-model warm; a cheap curated re-seed self-heals.
> **Gotcha (caught on the live Space, fixed):** the build downloaded the ONNX MiniLM embedder as
> ROOT (`/root/.cache`), but the container runs as appuser → runtime re-download, which on HF
> produced a TRUNCATED `model.onnx` → `INVALID_PROTOBUF`, Exit 3, startup crash-loop. Fix: COPY
> the known-good 90 MB model into `/home/appuser/.cache` at build; verified with
> `docker run --network none` (embedder loads offline, lookup hits). **Verified LIVE:** Space
> recovers, a seeded Q answers in **1.4 s the moment it starts** (baked cache, no warm window),
> and a NOVEL Q streams a `query_ledger` tool event at ~28 s (correct routing + live progress,
> not a frozen screen). Tests: curated plans guard-valid + UI suggestions ⊆ curated. **Suite 115.**
> Follow-up: `deploy_space.sh` should auto-sync the literal `Dockerfile` from `Dockerfile.hf`
> (manual each deploy today). Still deferred: trim the ~2500-tok tiny-model prompt; firm up
> routing for paraphrases like "top 5 / most overdue money"; Layer 4 tiny-vs-strong number.
>
> **2026-07-03 — SSE streaming (live "watch it think") + demo-UX hardening.** Real
> browser testing showed novel (typed) questions are slow (tiny 1.5B on free CPU) and
> a static "Thinking…" reads as hung. Added a **streaming endpoint** `POST /api/chat/stream`
> (SSE): `CachedAgent.astream` emits `cached`/`tool`/`answer`/`error` events — real
> `on_tool_start` events on a cache MISS (via LangGraph `astream_events`), synthetic tool
> events on a HIT. Shared `src/agent/message_utils.py` (final-text + tools-used, de-duped
> from `app.py`). UI (`web/src/App.jsx`) consumes the stream: live step list ("Querying the
> ledger" → "Reading the collections policy"), an **elapsed-seconds** counter, and an honest
> "tiny model on a free CPU, it's working not stuck" hint after 6 s. 3 new streaming API
> tests. **Suite 113 green.** NOTE: local container verification is impractical here (the
> throttled proxy makes the in-container `ollama pull` take ~1 h); streaming is
> unit-tested + will be verified LIVE on HF (clean fast network). **Still deferred (plan):
> bake the plan-cache at build (every redeploy currently re-warms → slow window), trim the
> tiny-model prompt (~2500 tok/turn → the real speed lever), firm up tool-routing so
> "top N customers" hits `query_ledger` not the policy, and the Layer 4 tiny-vs-strong number.**
>
> **2026-07-03 — Phase 6 LAYER 3 LIVE.** Space deployed via a `space-deploy` branch
> (HF front-matter README + tiny-Ollama image as the literal `Dockerfile` + `.gitattributes`
> LFS-tracking `*.png`/`*.gif` — HF rejects plain-git binaries). HF builds `Dockerfile.hf`
> on its clean runners (the optional-CA build step no-ops there) and it serves. **Demo-UX bugs
> caught by real browser testing (both fixed):** (a) the UI's *first suggested* question was
> NOT in the plan-cache seed set → clicking it fell through to the slow tiny model and hung
> on "Thinking…" — fixed by making `web/src/App.jsx` SUGGESTIONS mirror `data/seed_plan_cache.py`
> exactly (every chip is now a ~3 s cache hit); (b) added an honest latency hint (chips =
> instant/cached, typed = live tiny model, may take up to a minute) so "Thinking…" never
> reads as hung. **Deploy gotchas (portfolio-worthy findings, portable to the other Spaces):** git &
> the Docker daemon can trust a supplied CA (push/pull work) even though `curl`/schannel fails
> revocation; HF binaries need LFS; `git lfs migrate` rewrites shared history → keep it OFF
> `main` (had to hard-reset local `main` to the clean `origin/main` after a leak; origin was
> never polluted).

> **2026-07-03 — Phase 6 LAYER 3 DONE on the DESKTOP (self-contained tiny-Ollama HF
> image, ADR-012).** Decision: the public Space runs a **tiny Ollama model baked into
> one container** (no key, no cloud quota, always up) — not a Gemini-key Space. New:
> `Dockerfile.hf` (multi-stage; installs the Ollama binary + `zstd`; bakes the ledger;
> non-root UID 1000; runtime deps `requirements-hf.txt` = runtime set + `langchain-ollama`,
> no Gemini SDK; pins `OLLAMA_MODEL=qwen2.5:1.5b` for a deterministic demo footprint),
> `scripts/hf_entrypoint.sh` (`ollama serve` → pull the model → serve uvicorn NOW →
> **background** plan-cache warm), `scripts/deploy_space.sh` (mirrors forge-pdm F6),
> `docs/DEPLOY.md`, ADR-012. **Built + ran end-to-end here on Docker Desktop.**
>
> **Findings from actually building it (each fixed):** (1) `docker build` pip/npm hit a
> a **network that terminates TLS** — the daemon pulls base images
> fine, but installs *inside* the build don't trust the terminating CA. Fix: OPTIONAL
> `ca.cr[t]` (glob) COPY’d in + `update-ca-certificates`; **git-ignored, never
> shipped**; a NO-OP on HF's clean runners.
> (2) Ollama installer needs **`zstd`** (slim image lacks it). (3) `--select` prints TWO
> stdout lines (`OLLAMA_MODEL=` **and** `HAS_GPU=`) → the entrypoint must grep just the
> model line. (4) The runtime **ONNX embedding download** (ChromaDB MiniLM) also hit the
> terminating CA → set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE` to the system bundle
> (AFTER pip, so it doesn't bust the cached dep layer); verified live: `EMBEDDING_OK
> dim=384`. (5) **UX fix:** warming 8 seed Qs on the tiny CPU model is slow (~2 tok/s,
> ~25 s/answer) — doing it *before* serving left `/api/health`+UI dead for minutes → moved
> the warm to the **background** so uvicorn is live immediately. Tiny-CPU inference is slow
> by nature; the plan-cache hides it on headline Qs. **These CA/zstd findings are portable
> to the other public Spaces (forge-pdm already deployed) — worth writing up.**

> **2026-07-02 — Phase 6 LAYER 2 DONE (grammar-constrained tool-calls, ADR-011).**
> **Tested first (per the "measure, don't assume" call) and it refuted the plan's
> premise:** native tool-calling on tiny models fails, but NOT from malformed JSON.
> Measured on the real tools/prompt (5 golden Qs, temp 0): `qwen2.5:0.5b` emits
> valid JSON but **invents fake arg fields + omits `sql`** (~1/5); `qwen2.5:1.5b`
> writes **correct SQL in prose, no tool call at all** (0/5). So a valid-JSON grammar
> would fix nothing. Fix: constrain the WHOLE reply to a `{tool, sql|query, answer}`
> schema via Ollama `format` (GBNF underneath) + translate to `tool_calls`. New
> `src/agent/constrained.py::ConstrainedToolModel` (BaseChatModel wrapper; `final_answer`
> option lets the ReAct loop end); tier-gated in `providers.should_constrain`
> (`constrained_tool_calls=auto` wraps only tiny catalog models, reusing ADR-010's
> `quality`); strong models keep native tool-calling (constraining them would hurt).
> Added `ollama_keep_alive`/`num_ctx` (KV cache). **Measured: ≤1/5 → 5/5 valid+routed+
> filled.** End-to-end on the real agent+ledger, `qwen2.5:1.5b` completes the full loop
> (tool call → guarded SQL → final answer with a real number; top-3 customers w/ balances,
> ~2–4 s on GPU). Caveat (honest, in ADR): `format` fixes STRUCTURE not SQL correctness —
> a 0.5B still writes weak SQL (may not filter "90 days" precisely); plan-cache serves
> curated-correct SQL for headline Qs, guard makes bad SQL safe on misses. This IS the
> "shines with a better model" story, now with numbers. `tests/test_constrained.py` (11).
> **Demo decision: ship BOTH tiny models, default `qwen2.5:1.5b`** (better SQL; 0.5b =
> extreme floor a tester can flip to via `OLLAMA_MODEL`). ADR-011 + PLAN L2 done.
>
> **NEXT — Phase 6 Layer 3 + 4 (this notebook).** L3: self-contained `Dockerfile.hf`
> (tiny model baked/pulled at startup, distinct from the Gemini `Dockerfile`) +
> `space-deploy` branch with HF front-matter, reusing forge-pdm F6 mechanics + its 3
> container gotchas. L4: README live Space link + capture the two pending numbers
> (`python -m evals.run` pass-rate + one latency), ideally tiny-vs-strong to show the delta.

> **2026-07-02 — Phase 7.1 DONE (hardware-aware selection, ADR-010).** New
> `src/core/hardware.py`: detects RAM/VRAM(nvidia-smi)/CPU → *effective memory*
> heuristic (VRAM if real GPU else ~80% RAM) → picks the best-fitting **downloaded**
> model from a public LLM catalog (falls back to best-that-would-fit). Wired into
> `resolve_ollama_model` (`src/agent/providers.py`): `OLLAMA_MODEL=auto` (now the
> **default**) resolves the local model at construction; a concrete tag overrides.
> LLM-only catalog (embeddings stay ChromaDB ONNX MiniLM, ADR-005) → no Ollama
> embedder to pick. **No new dependency** (psutil optional; stdlib `/proc`/`wmic`/
> `nvidia-smi` fallbacks so the diagnostic runs with the system Python). CLI:
> `python -m src.core.hardware` (table) · `--json` · `--select` (emits
> `OLLAMA_MODEL=…` for a container entrypoint). `tests/test_hardware.py` — 11
> offline tests (effective-memory GPU-vs-CPU, tiny-iGPU-ignored, best-downloaded-
> that-fits, skip-too-big-downloaded, fallback-to-best-fits, tag-tolerant match,
> `auto` resolves + never-empty). **Full suite: 99 green** (was 88 pre-7.1).
> **Verified live on the notebook** (RTX 4050 / 6 GB VRAM / `qwen2.5:7b` pulled) →
> `--select` emits `OLLAMA_MODEL=qwen2.5:7b`; a 16 GB CPU tier would pick a tiny
> model instead. This makes Phase 6's "shines with a better model" real from run one.
>
> **Remaining Phase 7 (later sessions):**
> - **7.2 (dedicated full-scan session):** audit the *whole* FIA feature set. Start
>   by mining my private engineering-findings log, then
>   sweep the FIA repo. Record kept/improved/dropped per item.
> - **7.3:** upgrade `docker-compose.yml` single-service/Gemini-only →
>   **multi-service** (app + `ollama` + models volume); a compose entrypoint can
>   call `python -m src.core.hardware --select` to choose the model to pull.

> **NEXT — Phase 6 Layer 2 (needs this notebook: model + normal network).**
> Hardened tiny local LLM (`qwen2.5:0.5b`/`1.5b` Q4_K_M) with **GBNF
> grammar-constrained tool-calls** (kills malformed-JSON, the #1 small-model
> failure) + KV/prompt caching on the long system-prompt prefix. Then Layer 3
> (self-contained `Dockerfile.hf` + `space-deploy` branch reusing forge-pdm F6
> mechanics) and Layer 4 (README live link + the two pending numbers: `evals.run`
> pass-rate + one latency, ideally tiny-vs-strong to show the delta). Also here:
> `python -m data.seed_plan_cache` to pre-warm the demo cache, and `python -m evals.run`.

> **2026-07-02 — Phase 6 LAYER 1 DONE (semantic plan-cache, ADR-009).** Built entirely
> here on the CPU-only desktop, no model needed. Cache the question→**PLAN** (the agent's
> guard-validated tool calls), **never** the answer; on a hit the plan is **re-executed
> live** (SQL re-validated through `sql_guard` + read-only, policy re-retrieved) and the
> answer composed deterministically from **fresh** results → the LLM is skipped but the
> number is always current. Proven offline by mutating an in-memory ledger and re-running
> the same cached plan (freshness test). New: `src/agent/plan_cache.py` · `plan_replay.py`
> · `cached_agent.py` (drop-in `CachedAgent` wrapping `build_agent`, config-gated by
> `plan_cache_enabled`) · `data/seed_plan_cache.py` (optional warm-up) · `tests/test_plan_cache.py`
> (12 tests). Reuses the existing ChromaDB + MiniLM embeddings — no new dependency.
> **Gotcha:** ChromaDB's in-process store can share state across ephemeral clients, so the
> test fixture uses a unique collection name per test (a fixed name bled between tests).
>
> **NEXT (needs the notebook — model + normal network):** Layers 2–3 — hardened tiny local
> LLM (`qwen2.5:0.5b/1.5b` Q4_K_M) with **GBNF grammar-constrained tool-calls** + KV cache;
> self-contained `Dockerfile.hf` + `space-deploy` branch reusing forge-pdm F6 mechanics.
> Layer 4 — README live link + the two still-pending numbers (`evals.run` pass-rate + one
> latency), ideally tiny-vs-strong model to show the delta. Also on the notebook: run
> `python -m data.seed_plan_cache` (needs a provider) to pre-warm the demo cache, and
> `python -m evals.run`. Demo provider so far was local Ollama `qwen2.5:7b`.

> **2026-06-10 — deferred again, on purpose (handoff).** Confirmed these two numbers
> (eval pass-rate + one latency) are recorded **nowhere** yet — they need a live run.
> NOT done on the production desktop (i3 / 8 GB / no GPU; Ollama server was down) —
> per the compute split, inference belongs on the **personal Linux notebook** (GPU,
> normal network, no TLS termination). Dedicated session to do, with full context already
> here + in `docs/DEMO.md` §6:
> 1. On the notebook: `ollama serve` + pull `qwen2.5:7b` (or `llama3.1`); `pip install -e ".[ollama,gemini,data,dev]"`; `python data/generate.py` (build the ledger).
> 2. `python -m evals.run` → record the **pass-rate** (e.g. "7/7 golden questions pass").
> 3. Hit `POST /api/chat` once (or watch the demo run) → note **one response latency**.
> 4. Add a small "Evals" + "Latency" line to the README — all real, none guessed.

## Done
- 2026-07-02: **Phase 6 Layer 2 — grammar-constrained tool-calls (ADR-011).**
  - Probed native tool-calling on `qwen2.5:0.5b`/`1.5b` (real tools+prompt, 5
    golden Qs) → refuted the malformed-JSON premise: 0.5b invents fake arg fields
    + omits `sql` (~1/5); 1.5b writes SQL in prose, 0 tool calls. Structured
    output (`format`=schema) → both **5/5** valid+routed+filled.
  - `src/agent/constrained.py`: `ConstrainedToolModel` (BaseChatModel wrapper) —
    `bind_tools` builds a `{tool, sql|query, answer}` schema (+ `final_answer` to
    end the loop); `_generate`/`_agenerate` bind `format=schema`, parse, return an
    `AIMessage` with one `tool_calls` entry or final content; malformed/off-menu
    degrades to plain content. Presents as a chat model → create_react_agent,
    plan-cache, guard, evals all unchanged.
  - `src/agent/providers.py`: `should_constrain()` tier-gates via ADR-010 catalog
    `quality` (`auto` wraps tiny only; `on`/`off` force); `build_chat_model` wraps
    the tiny Ollama path + passes `num_ctx`/`keep_alive`. New `hardware.catalog_quality()`.
  - Config: `constrained_tool_calls` / `constrained_quality_max` / `ollama_num_ctx`
    / `ollama_keep_alive` (+ `.env.example`).
  - `tests/test_constrained.py`: 11 offline tests (schema, parse tool-call/final/
    malformed/foreign-field/unknown-tool, format passed through, tier gating).
    Full suite: **110 passing**. Verified end-to-end live on the notebook.
  - ADR-011 written; PLAN Layer 2 done; CLAUDE.md file-map updated.
- 2026-07-02: **Phase 7.1 — hardware-aware local model selection (ADR-010).**
  - `src/core/hardware.py`: `detect_hardware()` (RAM via psutil/`/proc`, CPU,
    NVIDIA VRAM via `nvidia-smi`); `HardwareProfile.effective_memory_gb` (VRAM if
    real GPU, else ~80% RAM); `recommend_model()` picks the best-quality model
    that fits **and** is downloaded from a public LLM catalog (`_CATALOG`,
    tiny→strong), falling back to best-that-would-fit. `list_downloaded_models()`
    hits Ollama `/api/tags` (stdlib urllib, fails soft). CLI `python -m
    src.core.hardware` + `--json` + `--select`.
  - `src/agent/providers.py`: `resolve_ollama_model()` — `OLLAMA_MODEL=auto`
    (new default in `config.py` + `.env.example`) resolves via hardware at model
    construction; concrete tag passes through; always returns a real tag.
  - Clean-room reuse of FIA's `utils/hardware.py` engineering (public data only),
    rewritten in English; dropped the embed catalog (embeddings are ChromaDB ONNX,
    ADR-005). No new dependency (psutil optional; stdlib fallbacks).
  - `tests/test_hardware.py`: 11 offline tests (mock detectors). Full suite: **99
    passing**. Verified live on the notebook.
  - ADR-010 written; PLAN 7.1 marked done; CLAUDE.md file-map updated.
- 2026-07-02: **Phase 6 — Layer 1: semantic plan-cache (ADR-009).**
  - `src/agent/plan_cache.py`: a *plan* = the agent's tool calls (validated
    `query_ledger` SQL + `search_policy` query), stored in a ChromaDB collection
    keyed by the question embedding (same MiniLM embeddings as RAG, no new dep).
    `plan_from_messages` only caches read-only, guard-valid turns (re-checks SQL
    via `sql_guard`); `PlanCache.lookup` = cosine similarity, conservative
    threshold (default 0.90).
  - `src/agent/plan_replay.py`: on a hit, re-validate + read-only re-execute the
    SQL (and re-run policy retrieval), compose the answer **deterministically
    from fresh results** — LLM untouched. `ReplayError` → caller falls back to
    the LLM.
  - `src/agent/cached_agent.py`: `CachedAgent`, a drop-in `invoke`/`ainvoke`
    wrapper; `build_agent` wraps the compiled agent when `plan_cache_enabled`.
    Multi-turn requests bypass the cache.
  - `data/seed_plan_cache.py`: optional one-time warm-up via the live agent.
  - Config: `plan_cache_enabled` / `plan_cache_collection` / `plan_cache_threshold`.
  - `tests/test_plan_cache.py`: 12 offline tests — plan extraction (grounded /
    ungrounded / unsafe-SQL / dedupe), semantic hit-vs-miss, **live-replay
    freshness** (mutate the ledger → the replayed number moves), guard
    re-validation on replay, and `CachedAgent` (miss→LLM+warm, hit→replay
    without the LLM, multi-turn bypass). Full suite: **88 passing**.
  - ADR-009 written; ARCHITECTURE + PLAN (Phase 6) + CLAUDE.md updated.
- 2026-06-08: **Ship gate closed — live demo GIF.**
  - First end-to-end live run, on the personal Linux notebook (normal network):
    agent answered real questions using `query_ledger` (and `search_policy`),
    HTTP 200. Provider: **local Ollama `qwen2.5:7b`** (tool-capable).
  - Ran **outside Docker** (the repo's intended Ollama path): host `uvicorn`
    reads `.env` (`PRIMARY_PROVIDER=ollama`, `OLLAMA_BASE_URL=http://localhost:11434`),
    `web/` built with `VITE_API_KEY` matching `APP_API_KEY`. The one-time ONNX
    MiniLM embedding download succeeded (no TLS termination on this network).
  - **Gotcha found & documented:** `docker-compose.yml` pins `PRIMARY_PROVIDER:
    gemini` in its `environment:` block, which **overrides `.env`** — Compose only
    uses the root `.env` for `${VAR}` *substitution*, it is not injected into the
    container (no `env_file:`). So a `.env` set to Ollama is silently ignored by
    the container, and the placeholder Gemini key then 500s. The Docker image is
    also Gemini-only by design (`requirements.txt` omits `langchain-ollama`), so
    Ollama-in-Docker is not a supported path — the host run is the clean one. Left
    a git-ignored `docker-compose.override.yml` locally for reference only.
  - `docs/demo.gif` (≈628 KB, inline-friendly) recorded and embedded in the README
    (replaced the placeholder comment).
- 2026-06-08: **Phase 5 — AI-native layer.**
  - `mcp_server/server.py`: MCP server exposing `query_ledger` (tool) +
    `schema://ledger` (resource) via FastMCP. Reuses the SAME guardrail as the
    app through `run_guarded_query` (connect_readonly + guard_query). Named
    `mcp_server` (not `mcp`) — a top-level `mcp/` shadowed the MCP SDK package
    and broke its import; caught by the import test. `mcp_server/README.md` has
    Claude Code registration. Smoke-verified: builds, registers tool + resource.
  - `evals/`: `checks.py` (pure property checks — used_tool / mentions_all /
    contains_number, tolerant + format-agnostic), `golden.py` (7 cases with
    ledger-verified expectations spanning ledger-only / policy-only / both),
    `run.py` (`python -m evals.run`, live harness, non-zero exit on failure).
  - `.claude/skills/eval-agent/SKILL.md`: Claude Code skill that runs the evals.
  - `tests/test_mcp.py` (guardrail parity: legal SELECT, row cap, write/unknown
    rejected with nothing executed) + `tests/test_evals.py` (check primitives +
    golden well-formedness). Full suite: **76 passing**.
  - ADR-008 written (shared-guardrail MCP · `mcp_server` naming · property-based
    evals). README "AI-native layer" section; ARCHITECTURE + CLAUDE.md + PLAN +
    pyproject (`mcp` extra) updated.
  - Offline by design: `evals/run.py` (live LLM) not run here — the accuracy
    number is captured on the live-network machine, same trip as the GIF.

### Earlier
- 2026-06-08: **Phase 4 — full-stack + ship.**
  - `src/api/app.py`: FastAPI via `create_app(agent_builder)` factory. Async
    `lifespan` builds the agent **once** (read-only ledger + policy index) onto
    `app.state`; `GET /api/health` (open) + `POST /api/chat` (API-key via
    `X-API-Key`, constant-time compare). Mounts a built `web/dist` at `/` so one
    container serves UI + API same-origin. Builder is injectable → offline tests.
  - `src/api/schemas.py`: Pydantic v2 boundary (`ChatRequest` message+history,
    `ChatResponse` reply+`tools_used`, role-restricted `Message`).
  - `web/`: minimal React (Vite) chat UI — history in state, posts to `/api/chat`,
    renders replies + a `tools_used` badge; Vite proxies `/api` in dev. `npm run
    build` verified (32 modules, ~145 kB). API key baked via `VITE_API_KEY`.
  - `Dockerfile` (multi-stage Node→Python) + `docker-compose.yml` + `.dockerignore`
    + `requirements.txt`: one-command `docker compose up`; ledger generated at
    image build; `$PORT` override for a Space.
  - `tests/test_api.py`: 7 offline tests (health open, auth required/wrong-key,
    happy path reply+tools, history forwarding, empty-message + bad-role 422),
    injected stub agent. Full suite: **65 passing**.
  - ADR-007 written (single same-origin container · build agent once · baked UI
    key · build-time ledger). README Run section + ARCHITECTURE api/web/deploy
    sections + `.env.example` note updated. pyproject deps: fastapi, uvicorn, httpx.
  - Verified here: static mount serves the SPA at `/` and `/api/health` → ok
    against a stub agent (LLM/ONNX/`docker build`/`npm install` registry are
    blocked by the sandbox SSL cert — code paths verified, not exercised live).

### Earlier
- 2026-06-08: **Phase 3 — RAG over the collections policy.**
  - `src/rag/chunking.py`: split the policy on `##` sections (one citable rule
    each) into chunks with deterministic IDs (`"{source}::{heading-slug}"`).
  - `src/rag/embeddings.py`: local default (ChromaDB ONNX `all-MiniLM-L6-v2`,
    no key — ADR-005) + injectable `DeterministicEmbeddingFunction` (offline
    hashing vectorizer) for tests.
  - `src/rag/index.py`: idempotent `build_index` (upsert by ID + prune orphans →
    collection mirrors the doc; ADR-006); `ensure_policy_index` builds once,
    reuses after.
  - `src/agent/tools.py`: `search_policy` tool — top-k chunks as JSON with the
    section heading to cite.
  - `src/agent/graph.py`: registered `search_policy` alongside `query_ledger`;
    rewrote the system prompt to use BOTH tools together (number from the ledger
    + governing rule from the policy, cited).
  - `tests/test_rag.py`: 5 offline tests — idempotency (same input → same
    IDs/count), pruning of removed sections, and retrieval finds the expected
    rule (deterministic embeddings, ephemeral client). Full suite: **58 passing**.
  - ADR-005 (local embeddings) + ADR-006 (idempotent indexer) written.

### Earlier
- 2026-06-08: **Phase 2 — guarded text-to-SQL + agent core.**
  - `src/agent/sql_guard.py`: defense-in-depth guardrail. String-literal-aware
    scanner (strip comments, split top-level `;`), `SELECT`/`WITH` only,
    deny-list (writes/DDL/catalog/filesystem fns), relation allow-list (CTE-aware),
    outer `LIMIT` wrap. Never relaxes — rejects with a reason. ADR-003.
  - `src/agent/ledger.py`: read-only DuckDB connection (authoritative backstop)
    + `run_query`.
  - `src/agent/tools.py`: `query_ledger` tool (guard → read-only exec → JSON;
    rejections/errors returned as text so the loop self-corrects).
  - `src/agent/providers.py` + `graph.py`: dual provider (Ollama/Gemini) with
    active fallback via a dynamic-model callable (ADR-004); config-driven order.
  - `src/core/config.py`: `pydantic-settings`. `schema_hints.py`: prompt schema.
  - `tests/`: 53 passing. `test_sql_guard.py` is the priority offline suite
    (injection/escape + over-blocking); `test_ledger.py` proves the read-only
    connection blocks writes and the tool caps rows (skips if ledger absent).
  - ADR-002 (project name), ADR-003 (guardrail), ADR-004 (fallback) written.
  - Verified end-to-end against the real ledger (top overdue customers, DSO).

### Earlier
- 2026-06-08: Repo scaffolded. README, CLAUDE.md, PLAN.md, docs skeletons,
  LICENSE (MIT), .gitignore, .env.example, pyproject.toml.
- 2026-06-08: **Phase 1 — synthetic data engine.**
  - `data/generate.py`: Faker customer dimension + DuckDB set-based facts.
    ~1.06M invoices / 923k payments / 305k communications from 13k customers in
    ~45 s → `data/ledger.duckdb` (21 MB, git-ignored). Deterministic
    (seed + single thread). CLI: `--customers --as-of --seed --out`.
  - Signal verified: overdue rate rises with behaviour profile
    (prompt ≈0.5% → defaulter ≈51%). Views: `v_invoices`, `v_customer_ar`,
    `v_dso`.
  - `data/collections_policy.md`: RAG knowledge base (aging buckets + dunning
    cadence match the ledger).
  - ADR-001 written (DuckDB over Spark/Pandas). pyproject deps + ARCHITECTURE
    data-model section updated.

## Next
- **Ship gate (do first):** record `docs/demo.gif` — full step-by-step in
  [`docs/DEMO.md`](DEMO.md). **Must run on a network that does not terminate
  TLS:** termination blocks both the Gemini API and the
  one-time ONNX embedding download (verified — same cert failure as npm). The
  personal Linux notebook is the right host (no TLS termination; GPU for local Ollama if
  preferred). That GIF + the repo is the "shipped link". Optionally deploy to a
  Hugging Face Space (Docker SDK; secrets `GEMINI_API_KEY`, `APP_API_KEY`,
  build-arg `VITE_API_KEY`; `PRIMARY_PROVIDER=gemini`).
- **Same live-network session, capture the README numbers:** run
  `python -m evals.run` (with a provider) for the accuracy/pass-rate figure, and
  note one response latency. Then add the GIF + a small "evals" and "latency"
  line to the README — all real, none guessed.
- After that: optional polish only. The MVP (Phases 0–5) is functionally done.

## Open decisions / notes
- RAG default embeddings (ONNX MiniLM) **download the model once on first use**;
  this sandbox blocks the fetch (SSL cert), so the live ONNX path is verified by
  code but not exercised here. Tests run offline on the deterministic hashing
  embedding by design. On the demo Space the one-time download is expected.
- `data/chroma/` (the persisted index) is built on demand and git-ignored like
  the ledger; rebuild is automatic (idempotent) on first agent run.
- Agent **exercised live on 2026-06-08** (host run, Ollama `qwen2.5:7b`): real
  tool-using answers, HTTP 200. Tests stay offline by design. A live run needs
  `ollama serve` + a tool-capable model (note: the default `llama3.1` works, and
  `qwen2.5:7b` is confirmed) or a `GEMINI_API_KEY`.
- Install providers per env: `pip install -e ".[ollama]"` and/or `".[gemini]"`
  (lazy imports — only the one you run is required).
- Regenerate the ledger with `python data/generate.py` (needs `faker` extra:
  `pip install -e ".[data]"`). The `.duckdb` file is not committed.
- Provider order: **Ollama primary** in dev (zero cost / no network), **Gemini
  fallback**; flip via `PRIMARY_PROVIDER=gemini` for the cloud demo (ADR-004).

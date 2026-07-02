# Decision log (ADRs)

Architecture Decision Records — one entry per non-obvious choice: context,
decision, consequences. Kept short.

---

## ADR-010 — Hardware-aware local model selection (`OLLAMA_MODEL=auto`)

**Status:** Accepted · 2026-07-02

### Context

Phase 6 sells the demo as *"runs a tiny model on free CPU, shines with a better
model."* Pinning a single `OLLAMA_MODEL` in config makes that a **README
promise**: on a 16 GB / 2-CPU free tier a 7B tag stalls or OOMs, while on a dev
box with a GPU a pinned 0.5B needlessly throws away capability. The local path
should scale to the box it runs on — from the first request, with no manual
retune per machine. (The private `fleet_intelligence_agent` already does this;
its `utils/hardware.py` holds **zero confidential data** — public Ollama model
names + generic `psutil`/`nvidia-smi` detection — so the clean-room rule permits
reusing our own engineering, adapted to this repo's English/style.)

### Decision

Add `src/core/hardware.py`: detect RAM / NVIDIA-VRAM / CPU, compute an
**effective memory** heuristic (VRAM if a real GPU is present, else ~80 % of RAM),
and pick from a **public catalog** the highest-quality model that both *fits* and
is *already downloaded* (falling back to the best that *would* fit if none is
pulled yet). `OLLAMA_MODEL=auto` (now the default) triggers this in
`resolve_ollama_model` (`src/agent/providers.py`) at model construction; a
concrete tag still overrides — the HF CPU demo pins the tiny floor explicitly.

- **LLM-only catalog.** Embeddings are ChromaDB's bundled ONNX MiniLM (ADR-005),
  so there is no Ollama embedder to select — dropped FIA's embed catalog.
- **No new dependency.** `psutil` is used *if present*; every detector has a
  stdlib fallback (`/proc`, `os`, `nvidia-smi`, `wmic`), so the diagnostic runs
  with the system Python before any `pip install`.
- **Diagnostic + machine-readable modes.** `python -m src.core.hardware` prints a
  readable table; `--json` and `--select` (emits `OLLAMA_MODEL=…`) let a
  container entrypoint / compose consume the choice.
- **Offline-testable.** Detectors are separable; `tests/test_hardware.py`
  constructs `HardwareProfile` directly and monkeypatches detection, honouring
  the "unit tests run offline" rule (no `nvidia-smi`, no Ollama).

### Consequences

- Phase 6's tiny-vs-strong claim is **real from run one**: verified live on the
  notebook (RTX 4050, 6 GB VRAM, `qwen2.5:7b` pulled) → `auto` resolves to
  `qwen2.5:7b`; a 16 GB CPU tier would resolve to a tiny model instead.
- `resolve_ollama_model` **always returns a concrete tag** (tiny floor if
  detection yields nothing), so `ChatOllama` construction never fails; a
  genuinely unusable local box is still covered by the provider fallback (ADR-004)
  at call time.
- Feeds Phase 6 Layer 2 (the hardened tiny local LLM) and Phase 7.3 (a
  multi-service compose can call `--select` to choose the model to pull).

---

## ADR-009 — Semantic plan-cache: cache the *plan*, never the answer

**Status:** Accepted · 2026-07-02

### Context

Phase 6 turns the "shipped link" into a **zero-key click-and-try** demo on a free
CPU tier: no install, no API key for the visitor. On that tier the LLM is the
slow, expensive part of every turn, and demo visitors ask overlapping questions
(top overdue, DSO, aging). A cache is the obvious win — but the naive cache is a
**correctness trap**: caching a question → *answer* freezes a number over mutable
data. Regenerate the ledger with more customers, or test a new scenario, and a
cached answer silently lies. In a portfolio piece, a confidently-wrong number is
worse than a slow one.

### Decision

Cache the question → **plan**, not the question → **answer**, and always
**re-execute the plan live**.

1. **A plan is the agent's tool calls** — the guard-validated `query_ledger` SQL
   and the `search_policy` query text (*intent*, `src/agent/plan_cache.py`). It
   carries **no answer text and no numbers**. Only a **read-only, guard-valid**
   turn is cacheable: `plan_from_messages` re-checks every `query_ledger` SQL
   through `sql_guard` before storing, and refuses to cache a turn with no
   groundable tool call.
2. **A hit is replayed live** (`src/agent/plan_replay.py`): each cached step is
   **re-validated through `sql_guard` and executed read-only** (SQL) / re-run
   against the live index (policy), then the answer is **composed
   deterministically from the fresh results** — the LLM is never called. So the
   number always reflects the *current* data; only the *reasoning /
   tool-selection* is skipped. If a cached query no longer validates or runs
   (`ReplayError`), the caller falls through to the LLM.
3. **Semantic lookup reuses the existing RAG stack** — the same ChromaDB client
   and local MiniLM embeddings as the policy index (ADR-005), so this adds **no
   new dependency**. Cosine similarity with a **conservative threshold** (default
   0.90); a miss simply calls the LLM and warms the cache. Precision over recall:
   "check this account" never matches "send this account" — and even a wrong
   match is harmless, because every replayed plan is re-validated and read-only.
4. **Drop-in wrapper.** `CachedAgent` presents the same `invoke`/`ainvoke`
   interface as the compiled agent, so the API, evals and tests are unchanged;
   `build_agent` wraps the agent when `plan_cache_enabled` (config-gated).
   Multi-turn requests (a follow-up needs prior context) are not cache-eligible.

This is caching the **reasoning / structured intent**, not output caching (cf.
the semantic-cache literature).

### Consequences

- **Fast *and* honest:** a hit is ~tens of ms (no LLM), yet the number is always
  live — proven offline by mutating the ledger and re-running the same cached
  plan (`tests/test_plan_cache.py`), no model required.
- The whole mechanism is unit-testable **offline** (deterministic embeddings +
  an in-memory DuckDB), which is why Layer 1 was built entirely on the CPU-only
  desktop; only seeding real example plans and the strong-vs-tiny model numbers
  need a live provider (the notebook).
- Reuses the guardrail as the safety backstop on the replay path too — one
  audited security surface, no second model.
- Layers 2–3 of Phase 6 (a hardened tiny local LLM with grammar-constrained
  tool-calls + a self-contained `Dockerfile.hf` / `space-deploy` branch) build on
  this and are deferred to a live-network session.

---

## ADR-008 — AI-native layer: shared-guardrail MCP server + property-based evals

**Status:** Accepted · 2026-06-08

### Context

Phase 5 adds the layer aimed at AI-native employers: an MCP (Model Context
Protocol) server, a Claude Code skill, and an eval suite. Three choices weren't
obvious: how the MCP surface relates to the in-app tool, how to name the package,
and how to judge a non-deterministic agent's answers.

### Decision

1. **The MCP server reuses the same guardrail — no second security model.**
   `mcp_server/server.py` exposes `query_ledger` over MCP by calling
   `run_guarded_query`, which uses the *same* `connect_readonly` +
   `guard_query` (SELECT/WITH only, allow/deny lists, single statement, row cap)
   as the in-app tool. A new surface must not be a weaker surface. The core is a
   plain function so it's unit-tested offline (`tests/test_mcp.py`) without an
   MCP client; the schema is exposed as a `schema://ledger` resource.
2. **Package named `mcp_server`, not `mcp`.** A top-level `mcp/` package (which
   `pythonpath=["."]` puts on `sys.path`) *shadows the MCP SDK's own `mcp`
   package*, breaking `from mcp.server.fastmcp import FastMCP`. Renamed to
   `mcp_server` to avoid the collision — caught immediately by the import test.
3. **Evals assert properties, not exact strings.** An LLM phrases the same fact
   many ways, so golden cases (`evals/golden.py`) check that the answer (a) used
   the right tool(s), (b) cites the governing policy keywords, and (c) states the
   right number within a tolerance — primitives in `evals/checks.py`. Numeric
   expectations are computed from the ledger (reproducible via the fixed seed)
   and policy keywords come from the policy doc, so a pass means *grounded in
   both sources*. The checks are pure and offline-tested; only the runner
   (`evals/run.py`, `python -m evals.run`) needs a live LLM.

### Consequences

- One guardrail, audited once, protects both the app and the MCP surface; a
  guardrail regression fails tests for both.
- The eval suite is a real regression gate (non-zero exit on failure) and the
  source of a defensible accuracy number for the README — captured on a machine
  with a live provider, never guessed.
- The `mcp` extra is optional (`pip install -e ".[mcp]"`); the web/API container
  doesn't carry it.

---

## ADR-007 — Single same-origin container; agent built once; key baked into the UI

**Status:** Accepted · 2026-06-08

### Context

Phase 4 turns the agent into a runnable "shipped link": a FastAPI service, a
React UI, and a one-command run that also has to work on a free cloud Space.
Three choices weren't obvious: (a) how to host the UI and API, (b) when to build
the agent, and (c) how the browser authenticates to a key-protected API on a
public demo.

### Decision

1. **One same-origin container.** A multi-stage `Dockerfile` builds the React
   bundle (Node stage) and serves it as static files from the same FastAPI app
   that exposes `/api` (Python stage). No separate web server, no CORS, no
   second service to orchestrate — and it matches what a free Space runs (one
   container, `$PORT`). In dev the two run apart (Vite proxies `/api`), so the
   UI is same-origin in both modes.
2. **Build the agent once, in an async `lifespan`.** Constructing the agent
   opens the read-only ledger and builds the policy index — startup work, not
   per-request. It lives on `app.state`; requests reuse it. `create_app` takes
   an injectable `agent_builder` so the HTTP stack is testable offline against a
   stub (no LLM), which is how `tests/test_api.py` runs.
3. **API-key auth, baked into the UI build.** `/api/chat` requires an
   `X-API-Key` matching `Settings.app_api_key` (constant-time compare);
   `/api/health` stays open for probes. The same key is baked into the bundle at
   build time (`VITE_API_KEY`) so the same-origin browser can call the API. It
   demonstrates the auth boundary without a login system; the key isn't a
   user-secret here (the data is synthetic), it gates the public demo endpoint.
4. **Generate the ledger at image-build time.** The synthetic ledger is
   deterministic and ~45 s to build, so `RUN python data/generate.py` bakes it
   into the image (the `.dockerignore` excludes any local `*.duckdb`) — the
   container is self-contained and needs no data volume.

### Consequences

- One artifact to ship and one command to run; the same image runs locally and
  on a Space by flipping `PRIMARY_PROVIDER=gemini` and setting secrets.
- The baked UI key is visible to anyone who inspects the bundle — acceptable for
  a synthetic-data demo, and rotating it is a rebuild. A real multi-tenant app
  would use per-user auth instead; out of scope here.
- The policy index's one-time ONNX embedding download still happens at first
  run (ADR-005), not build time — the first request after a cold start pays it.

---

## ADR-006 — Idempotent policy indexer (deterministic IDs + prune)

**Status:** Accepted · 2026-06-08

### Context

The policy index is rebuilt on demand — on the agent's first run, in tests, and
whenever the policy doc changes. A naive "embed all chunks and `add`" duplicates
every chunk on each run (ChromaDB's `add` appends), so retrieval would return
stale copies and counts would drift. The index must instead converge to exactly
mirror the current document, however many times it runs.

### Decision

Make `build_index` idempotent on two mechanisms:

1. **Deterministic chunk IDs.** Chunk on the policy's `##` sections (each is a
   self-contained, citable rule by design) and ID each as
   `"{source}::{heading-slug}"`. `upsert` by that ID overwrites a section in
   place — same input → same IDs → no duplicates, and an *edited* section
   updates rather than appends.
2. **Prune orphans.** After upserting the current chunks, delete any ID in the
   collection that the current document no longer produces, so a removed or
   renamed section can't leave a stale chunk behind.

The net guarantee: after any run, the collection equals the document — same IDs
and count regardless of history. This is the property the offline tests assert
(`tests/test_rag.py`: re-index → identical IDs/count; drop a section → it's
pruned).

### Consequences

- Safe to call on every agent build (`ensure_policy_index` reuses a populated
  collection; first run builds it once).
- IDs are heading-derived, so two sections must not share a heading; the policy
  doc uses unique `##` headings, and a collision would surface as a lost chunk
  in the count assertions.

---

## ADR-005 — Local embeddings for RAG, with an injectable function

**Status:** Accepted · 2026-06-08

### Context

`search_policy` needs to embed the policy chunks and the query. Two routes: a
**provider embedding API** (e.g. Gemini embeddings) or a **local model**. The
project ships as a self-contained "shipped link" that runs on a free cloud
Space and must keep unit tests offline — and the LLM provider is already
swappable (Ollama/Gemini, ADR-004), so coupling retrieval to one provider's
embedding API would re-introduce a key/quota/network dependency the rest of the
design avoids.

### Decision

Use **local embeddings** by default: ChromaDB's bundled ONNX `all-MiniLM-L6-v2`
(`DefaultEmbeddingFunction`) — no API key, no per-call cost, runs in-process via
`onnxruntime`. The model file downloads once on first use and is cached.

Make the embedding function **injectable** (`build_index`/`get_collection` take
it as a parameter). That keeps retrieval decoupled from the LLM provider and
lets the tests pass a `DeterministicEmbeddingFunction` — a dependency-free
hashing bag-of-words vectorizer that needs no download, so the suite indexes and
retrieves fully offline and reproducibly.

### Consequences

- The demo Space embeds locally; no embedding key or quota, and retrieval is
  independent of which LLM provider is active.
- One-time MiniLM download on first run (cached after). In a network-restricted
  environment the injectable hashing function is a no-download fallback.
- The deterministic function is lexical, not semantic — fine for the tests'
  shared-vocabulary assertions, but it is a test/fallback aid, not the shipping
  retriever.

---

## ADR-004 — Dual-provider fallback via a dynamic-model callable

**Status:** Accepted · 2026-06-08

### Context

The agent must run on two interchangeable LLMs: a local one (Ollama) in dev and
a cloud one (Gemini) on the demo Space, with **active fallback** if the primary
fails at call time. LangChain expresses fallback as
`primary.with_fallbacks([secondary])` — but LangGraph's `create_react_agent`
auto-binds tools to a *single* `BaseChatModel` and rejects a `RunnableWithFallbacks`
passed as `model` (it is neither a chat model nor a `RunnableBinding`).

### Decision

Bind the tools to **each** provider, combine them with `with_fallbacks`, and
hand the result to `create_react_agent` as a **dynamic model callable** — a plain
`(state, runtime) -> model` function. LangGraph treats a callable as a pre-built
model and skips its own tool-binding, so the fallback runnable flows through
every model turn. The primary/fallback order is config-driven; setting
`PRIMARY_PROVIDER=gemini` inverts dev↔deploy with no code change. The fallback is
wired only when it has credentials, so an Ollama-only box still builds.

### Consequences

- One agent definition serves both environments; the demo Space just flips an
  env var. Provider SDKs are imported lazily, so neither is a hard dependency.
- The callable signature is coupled to a LangGraph internal contract; pinned via
  `langgraph>=1.0` and covered by a build smoke check.

---

## ADR-003 — Defense-in-depth guardrail for the text-to-SQL tool

**Status:** Accepted · 2026-06-08

### Context

The agent writes SQL from natural language and runs it against the ledger. That
is the project's main attack surface: prompt injection could try to mutate data,
read the filesystem (DuckDB's `read_csv`/`COPY`), or exfiltrate via stacked
statements. A single prompt-level filter is not trustworthy on its own — LLM
output is adversarial-by-default.

### Decision

Two independent layers, neither of which may be relaxed "to make a query pass":

1. **Read-only connection** (authoritative). The ledger is opened
   `duckdb.connect(path, read_only=True)`; the engine itself refuses every
   write/DDL regardless of what the prompt produced.
2. **Prompt-layer filter** (`sql_guard.guard_query`, fast feedback for the
   ReAct loop): a string-literal-aware scanner that strips comments and splits
   on top-level `;` (rejecting stacked statements), requires `SELECT`/`WITH`,
   applies a deny-list of write/DDL/catalog/filesystem keywords, checks every
   referenced relation against an **allow-list** (CTE names excepted), and wraps
   the query in an outer `LIMIT` so the model can never dump the ledger. Keyword
   and relation matching run on a copy with string contents masked, so data like
   a customer named `'DROP TABLE x'` is not mistaken for an attack.

Rejections and SQL errors are returned to the model as text (not raised) so the
loop can read the reason and self-correct.

### Why not just one layer

- Prompt-only is bypassable (novel encodings, parser gaps). The read-only
  connection is the hard stop.
- Connection-only gives the model opaque engine errors and still lets it read
  *any* table; the allow-list + row cap scope it to the intended relations and
  keep tool output small. Each layer covers the other's weakness.

### Consequences

- **Positive:** mutations are impossible by construction; the filter is unit-
  testable offline (the priority `tests/test_sql_guard.py` suite — injection and
  over-blocking cases) with no LLM or DB.
- **Limits:** the relation extractor is a regex/token scanner, not a full SQL
  parser; it is best-effort at the prompt layer and leans on the read-only
  connection as the backstop for anything it doesn't model.

---

## ADR-002 — Project name avoids third-party trademarks

**Status:** Accepted · 2026-06-08

### Context

The project was first sketched as "receivables-copilot". "Copilot" is a Microsoft
product brand; using it in a public portfolio repo invites trademark confusion
and dates the project to a single vendor's framing.

### Decision

Name it **receivables-agent**. It is accurate ("agent" describes the ReAct
architecture), vendor-neutral, and carries no third-party mark.

### Consequences

- No brand collision; the name describes the architecture, not a product.
- One-time rename of the repo/package early, before any external links exist.

---

## ADR-001 — DuckDB over Spark/Pandas for data generation and queries

**Status:** Accepted · 2026-06-08

### Context

The project needs (a) to *generate* ~1M+ synthetic invoices with realistic,
signal-carrying distributions, and (b) to *serve* read-only analytical queries
to the agent's text-to-SQL tool (aging, DSO, per-customer AR). The deliverable
is a runnable "shipped link": it must start with a single command and run on a
laptop or a free cloud Space — no cluster, no external database service.

The candidate engines:

- **Pandas** — generate row-by-row in Python, hold everything in memory.
- **Spark / PySpark** — a distributed engine built for data that does not fit on
  one machine.
- **DuckDB** — an embedded, columnar, vectorised SQL engine (the "SQLite for
  analytics"): a single file, an in-process library, full analytical SQL.

### Decision

Use **DuckDB** as both the generation engine and the query engine.

- **Generation is set-based in SQL.** Faker produces only the small customer
  dimension (13k rows) in Python; the ~1M-row fact tables are produced by one
  `CREATE TABLE ... AS SELECT` that cross-joins each customer with a correlated
  `range()` and draws every random attribute (lognormal amount via Box-Muller,
  exponential days-late, Bernoulli default) in SQL. The whole ledger builds in
  ~45 s into a 21 MB file.
- **Serving is the same file.** The agent opens `ledger.duckdb` on a *read-only*
  connection — which also reinforces the SQL guardrail (Phase 2): even a
  jailbroken prompt cannot mutate the database.

### Why not the alternatives

- **Pandas:** a Python loop over a million rows is slow and memory-hungry, and it
  separates "generation" from "query" into two mental models. DuckDB lets the
  same engine that serves the data also generate it, set-based.
- **Spark:** built to scale *across machines*. At ~1M rows / 21 MB on a single
  node, the JVM, the cluster/session overhead, and the deployment weight are all
  cost with no benefit — it would actively work against the one-command,
  free-Space constraint. Spark earns its keep when data outgrows one machine;
  this data does not, by design.

### Consequences

- **Positive:** one dependency, one file, one-command run; trivial to ship to a
  free Space; the read-only connection is a security primitive, not just a perf
  choice; set-based generation is fast and fully reproducible (fixed seed +
  single thread → deterministic `random()`).
- **Negative / limits:** single-node only; if a future version needed
  genuinely large or streaming data this decision would be revisited.
- **Deferred:** PySpark is intentionally out of scope here and parked for a
  dedicated data-engineering showcase where distributed data justifies it.
  Reaching for Spark on this dataset would be over-engineering.

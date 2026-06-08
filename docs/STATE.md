# STATE — receivables-agent

> Volatile, short, current. Update at the end of each work session.

## Current focus
Phase 2 (guarded text-to-SQL + agent core) — done. Next up: Phase 3 (RAG over
the collections policy).

## Done
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
- Phase 3 — RAG over the collections policy: idempotent ChromaDB indexer
  (deterministic IDs over `data/collections_policy.md`); `search_policy` tool;
  register it alongside `query_ledger` so the agent uses both tools together.

## Open decisions / notes
- Agent not yet exercised against a live LLM (tests are offline by design): no
  Ollama daemon / Gemini key in this env. Build + tool + guard all verified;
  first live run needs `ollama serve` + a tool-capable model (default
  `llama3.1`) or a `GEMINI_API_KEY`.
- Install providers per env: `pip install -e ".[ollama]"` and/or `".[gemini]"`
  (lazy imports — only the one you run is required).
- Regenerate the ledger with `python data/generate.py` (needs `faker` extra:
  `pip install -e ".[data]"`). The `.duckdb` file is not committed.
- Provider order: **Ollama primary** in dev (zero cost / no network), **Gemini
  fallback**; flip via `PRIMARY_PROVIDER=gemini` for the cloud demo (ADR-004).

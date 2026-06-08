# STATE — receivables-agent

> Volatile, short, current. Update at the end of each work session.

## Current focus
Phase 1 (synthetic data engine) — done. Next up: Phase 2 (guarded text-to-SQL).

## Done
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
- Phase 2 — guarded text-to-SQL + agent core: read-only DuckDB connection;
  `query_ledger` tool with allow/deny-list regex + schema hints; LangGraph
  ReAct loop; dual provider with fallback. pytest guardrail suite is the
  priority (injection / escape attempts).

## Open decisions / notes
- ADR-002 (to write): project name avoids third-party trademarks
  (receivables-copilot → receivables-agent).
- Regenerate the ledger with `python data/generate.py` (needs `faker` extra:
  `pip install -e ".[data]"`). The `.duckdb` file is not committed.
- Phase 2 provider order (decided 2026-06-08): **Ollama primary** in dev
  (zero cost / no network), **Gemini fallback**; the order is flipped for the
  cloud demo via env config (Gemini primary on the Space).

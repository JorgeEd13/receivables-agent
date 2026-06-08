# STATE — receivables-agent

> Volatile, short, current. Update at the end of each work session.

## Current focus
Phase 0 (foundations) — repository scaffold.

## Done
- 2026-06-08: Repo scaffolded. README, CLAUDE.md, PLAN.md, docs skeletons,
  LICENSE (MIT), .gitignore, .env.example, pyproject.toml.

## Next
- Phase 1 — synthetic data engine (`data/generate.py`): Faker dimensions +
  DuckDB set-based facts (~1M+ invoices) with payment-behavior profiles;
  collections-policy document for RAG.

## Open decisions / notes
- ADR-001 (to write): DuckDB over Spark/Pandas for data generation & queries.
- ADR-002 (to write): project name avoids third-party trademarks
  (receivables-copilot → receivables-agent).

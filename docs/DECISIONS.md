# Decision log (ADRs)

Architecture Decision Records — one entry per non-obvious choice: context,
decision, consequences. Kept short.

> Pending (to be written as we reach each point):
>
> - **ADR-002 — Project name avoids third-party trademarks.** Why
>   "receivables-copilot" was dropped in favor of "receivables-agent".

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

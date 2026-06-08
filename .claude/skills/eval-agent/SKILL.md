---
name: eval-agent
description: Run the receivables-agent golden-question eval suite and report the pass rate. Use when asked to evaluate, benchmark, or check the agent's answer quality, or after changing the agent/tools/prompt.
---

# Evaluate the receivables agent

Run the golden-question suite against the live agent and summarize the result.

## Prerequisites

- A built ledger: `data/ledger.duckdb` (else `python data/generate.py`, needs the
  `data` extra).
- A reachable LLM provider: either a running Ollama (`ollama serve` + a
  tool-capable model such as `llama3.1`) **or** `GEMINI_API_KEY` set with
  `PRIMARY_PROVIDER=gemini`.

## Steps

1. Run the suite:
   ```bash
   python -m evals.run
   ```
   For a single case: `python -m evals.run --case <id>` (ids in `evals/golden.py`).
2. Read the per-check output (✓/✗) and the final `N/M cases passed` line.
3. Report the pass rate and list any failing case with the specific check that
   failed (wrong tool, missing policy keyword, or wrong number).
4. If a case fails, inspect whether it's the agent (prompt/tool wiring) or a
   stale expectation in `evals/golden.py` (the ledger was regenerated) — don't
   "fix" a test by loosening a real regression.

## Notes

- The property checks (`evals/checks.py`) are unit-tested offline; this skill
  exercises the *live* agent end-to-end.
- The MCP server (`python -m mcp_server.server`) exposes the same guarded
  `query_ledger` to any MCP client; the eval and the MCP surface share the
  guardrail.
- Record the pass rate for the README once it runs green on a real provider.

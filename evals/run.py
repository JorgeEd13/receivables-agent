"""Run the golden-question eval suite against the live agent.

This is the one eval piece that needs a real LLM (a running Ollama or a
`GEMINI_API_KEY`) — it builds the agent and asks each golden question, then
scores the answer with the offline property checks in `evals/checks.py`.

    python -m evals.run                 # all cases
    python -m evals.run --case dso      # one case by id

Exit code is non-zero if any case fails, so it can gate CI. The checks
themselves are unit-tested offline (`tests/test_evals.py`); this runner is the
live harness — capture the pass rate here for the README.
"""

from __future__ import annotations

import argparse
from typing import Any

from evals.golden import GOLDEN, Case


def _final_text(messages: list[Any]) -> str:
    if not messages:
        return ""
    content = getattr(messages[-1], "content", messages[-1])
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _tools_used(messages: list[Any]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and name not in names:
                names.append(name)
    return names


def run_case(agent: Any, case: Case) -> tuple[bool, list[tuple[str, bool]]]:
    """Ask one question and score it. Returns (passed, per-check results)."""
    result = agent.invoke({"messages": [{"role": "user", "content": case.question}]})
    messages = result["messages"]
    checks = case.evaluate(_final_text(messages), _tools_used(messages))
    return all(passed for _, passed in checks), checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the receivables agent.")
    parser.add_argument("--case", help="Run a single case by id.")
    args = parser.parse_args()

    cases = [c for c in GOLDEN if not args.case or c.id == args.case]
    if not cases:
        print(f"No case with id {args.case!r}. Known: {[c.id for c in GOLDEN]}")
        return 2

    # Imported lazily so the module is importable without the agent's deps.
    from src.agent.graph import build_agent

    agent = build_agent()
    passed_count = 0
    for case in cases:
        ok, checks = run_case(agent, case)
        passed_count += ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {case.id}: {case.question}")
        for label, passed in checks:
            print(f"    {'✓' if passed else '✗'} {label}")

    total = len(cases)
    print(f"\n{passed_count}/{total} cases passed ({100 * passed_count // total}%).")
    return 0 if passed_count == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

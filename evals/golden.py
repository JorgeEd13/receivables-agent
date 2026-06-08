"""Golden questions for the receivables agent.

Each case pairs a question with the *properties* a correct answer must have. The
numeric expectations were computed directly from the synthetic ledger (a fixed
seed makes them reproducible), and the policy keywords come from
`data/collections_policy.md` — so the suite checks the agent is **grounded in
both sources**, not just fluent. Cases deliberately span the three modes: a pure
ledger number, a pure policy rule, and the flagship that needs both at once.

Update the expectations if `data/generate.py` or the policy doc changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.checks import contains_number, mentions_all, used_tool


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    expect_tools: list[str] = field(default_factory=list)
    must_mention: list[str] = field(default_factory=list)
    # (expected_value, absolute_tolerance)
    numbers: list[tuple[float, float]] = field(default_factory=list)

    def evaluate(self, reply: str, tools_used: list[str]) -> list[tuple[str, bool]]:
        """Run every property of this case → list of (label, passed)."""
        results: list[tuple[str, bool]] = []
        for tool in self.expect_tools:
            results.append((f"used {tool}", used_tool(tools_used, tool)))
        if self.must_mention:
            results.append(
                (f"mentions {self.must_mention}", mentions_all(reply, self.must_mention))
            )
        for value, tol in self.numbers:
            results.append((f"states ~{value}", contains_number(reply, value, tol)))
        return results


# Expected values verified against data/ledger.duckdb on 2026-06-08.
GOLDEN: list[Case] = [
    Case(
        id="customer_count",
        question="How many customers are in the ledger?",
        expect_tools=["query_ledger"],
        numbers=[(13000, 0)],
    ),
    Case(
        id="dso",
        question="What is our current DSO (days sales outstanding)?",
        expect_tools=["query_ledger"],
        numbers=[(77.7, 1.0)],
    ),
    Case(
        id="top_overdue_customer",
        question="Which single customer has the largest overdue balance?",
        expect_tools=["query_ledger"],
        must_mention=["Hensley-Huang"],
    ),
    Case(
        id="credit_hold_rule",
        question="What does our policy say about when a customer is placed on credit hold?",
        expect_tools=["search_policy"],
        must_mention=["60", "credit limit"],
    ),
    Case(
        id="writeoff_threshold",
        question="At how many days past due does an invoice become a write-off candidate?",
        expect_tools=["search_policy"],
        numbers=[(180, 0)],
    ),
    Case(
        id="payment_plan_eligibility",
        question="When is a customer eligible for a payment plan?",
        expect_tools=["search_policy"],
        numbers=[(5000, 0), (90, 0)],
    ),
    Case(
        id="credit_hold_accounts_and_rule",
        question=(
            "Which overdue accounts should go on credit hold this week, and what "
            "is the policy rule behind it?"
        ),
        expect_tools=["query_ledger", "search_policy"],
        must_mention=["60"],
    ),
]

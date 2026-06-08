"""Eval-suite tests — the property checks and golden cases, offline.

The live runner (`evals/run.py`) needs an LLM; these tests cover everything that
doesn't: the check primitives parse/compare correctly, and the golden cases are
well-formed (every case asserts at least one property).
"""

from __future__ import annotations

from evals.checks import contains_number, extract_numbers, mentions_all, used_tool
from evals.golden import GOLDEN


def test_used_tool():
    assert used_tool(["query_ledger", "search_policy"], "search_policy")
    assert not used_tool(["query_ledger"], "search_policy")


def test_mentions_all_case_insensitive():
    text = "Place the account on CREDIT HOLD at 60 days."
    assert mentions_all(text, ["credit hold", "60"])
    assert not mentions_all(text, ["write-off"])


def test_extract_numbers_handles_separators_and_decimals():
    nums = extract_numbers("We have 13,000 customers and a DSO of 77.7 days.")
    assert 13000 in nums
    assert 77.7 in nums


def test_contains_number_tolerance():
    assert contains_number("DSO is about 78 days", 77.7, tol=1.0)
    assert not contains_number("DSO is about 90 days", 77.7, tol=1.0)
    # Thousands-separated value matches an exact expectation.
    assert contains_number("There are 13,000 customers", 13000, tol=0)


def test_evaluate_reports_each_property():
    case = next(c for c in GOLDEN if c.id == "credit_hold_accounts_and_rule")
    checks = case.evaluate(
        reply="Accounts 60+ days overdue go on hold.",
        tools_used=["query_ledger", "search_policy"],
    )
    labels = dict(checks)
    assert labels["used query_ledger"] is True
    assert labels["used search_policy"] is True
    assert labels["mentions ['60']"] is True


def test_golden_cases_are_well_formed():
    ids = [c.id for c in GOLDEN]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in GOLDEN:
        assert case.question.strip()
        # Every case must assert something checkable.
        assert case.expect_tools or case.must_mention or case.numbers
        # An empty reply with no tools should never fully pass a real case.
        passed = [ok for _, ok in case.evaluate("", [])]
        assert not all(passed)

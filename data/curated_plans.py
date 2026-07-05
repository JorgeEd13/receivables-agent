"""Curated plan-cache seeds: hand-authored, correct plans for the demo's
one-click questions (ADR-009 / ADR-012).

Why curated instead of model-authored? The demo runs a tiny model on a free CPU.
Warming the cache by *asking that model* at build time is slow and risks baking a
**wrong** plan (a 1.5B mis-routes "top N customers" to the policy instead of the
ledger). So for the showcased questions we author the plan directly — known-correct
SQL / policy queries — and bake them at image-build time so they are instant from
the first request and survive every restart.

This does **not** weaken the honesty contract. A curated plan is still just a
*plan* (tool calls), re-validated through ``sql_guard`` here and **re-executed live**
on every hit (``plan_replay``) — never a frozen answer/number. The only difference
from a model-warmed plan is who wrote the SQL: a human who checked it against the
schema, not a tiny model. Fresh numbers, guaranteed-correct routing.

Each entry maps the exact seed question (which the UI's SUGGESTIONS mirror) to an
ordered list of steps. SQL targets the analytics views in ``schema_hints`` and uses
``meta.as_of_date`` semantics already encoded in ``v_invoices``/``v_customer_ar``.
"""

from __future__ import annotations

from src.agent.plan_cache import Plan, ToolStep

# A window-function query — top 5 customers by overdue balance *within each* aging
# bucket. This is exactly the kind of question a tiny model struggles to write in one
# go (it needs `QUALIFY row_number() OVER (PARTITION BY …)`), so we curate it: several
# natural phrasings all map to this one guard-valid, live-replayed plan. That turns a
# reliable tiny-model failure into an instant, correct answer.
_TOP5_PER_BUCKET = [
    ToolStep(
        "query_ledger",
        {
            "sql": "SELECT i.aging_bucket, c.name, sum(i.amount) AS overdue_amount "
            "FROM v_invoices i JOIN v_customer_ar c ON c.customer_id = i.customer_id "
            "WHERE i.status = 'overdue' "
            "GROUP BY i.aging_bucket, c.name "
            "QUALIFY row_number() OVER "
            "(PARTITION BY i.aging_bucket ORDER BY sum(i.amount) DESC) <= 5 "
            "ORDER BY i.aging_bucket, overdue_amount DESC"
        },
    ),
]

# question -> ordered tool steps. query_ledger takes {"sql": ...};
# search_policy takes {"query": ...}. Kept close to how the agent would phrase them.
CURATED_PLANS: dict[str, list[ToolStep]] = {
    "How many customers are in the ledger?": [
        ToolStep("query_ledger", {"sql": "SELECT count(*) AS customers FROM v_customer_ar"}),
    ],
    "What is our current DSO?": [
        ToolStep("query_ledger", {"sql": "SELECT dso_days FROM v_dso"}),
    ],
    "Which single customer has the largest overdue balance?": [
        ToolStep(
            "query_ledger",
            {"sql": "SELECT name, overdue_amount FROM v_customer_ar "
                    "ORDER BY overdue_amount DESC LIMIT 1"},
        ),
    ],
    "Show me the total overdue amount by aging bucket.": [
        ToolStep(
            "query_ledger",
            {"sql": "SELECT aging_bucket, sum(amount) AS overdue_amount FROM v_invoices "
                    "WHERE status = 'overdue' GROUP BY aging_bucket ORDER BY aging_bucket"},
        ),
    ],
    "Who are the top 10 customers by overdue balance?": [
        ToolStep(
            "query_ledger",
            {"sql": "SELECT name, overdue_amount FROM v_customer_ar "
                    "ORDER BY overdue_amount DESC LIMIT 10"},
        ),
    ],
    "What does our policy say about credit holds?": [
        ToolStep("search_policy", {"query": "credit hold rule threshold"}),
    ],
    "At how many days past due does an invoice become a write-off candidate?": [
        ToolStep("search_policy", {"query": "write-off candidate days past due"}),
    ],
    "When is a customer eligible for a payment plan?": [
        ToolStep("search_policy", {"query": "payment plan eligibility"}),
    ],
    # Top-N-within-each-group (window function). Several phrasings → one plan, so a
    # natural follow-up like "top 5 of each age group" hits the cache instead of the
    # tiny model, which reliably fails to write this SQL (the 2026-07-05 live finding).
    "Top 5 customers by overdue balance in each aging bucket": _TOP5_PER_BUCKET,
    "Show me the top 5 overdue customers in each age group.": _TOP5_PER_BUCKET,
    "Give me the top 5 of each age group.": _TOP5_PER_BUCKET,
    "Which are the top 5 customers by overdue amount per aging bucket?": _TOP5_PER_BUCKET,
}


def curated_plans() -> dict[str, Plan]:
    """The curated question -> Plan map (frozen ``Plan`` objects)."""
    return {q: Plan(steps=tuple(steps)) for q, steps in CURATED_PLANS.items()}

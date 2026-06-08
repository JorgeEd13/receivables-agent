"""Human/LLM-readable description of the ledger schema.

Injected into the system prompt so the model writes correct DuckDB SQL against
the allow-listed relations (see ``sql_guard.ALLOWED_RELATIONS``). Keep this in
sync with ``data/generate.py``.
"""

from __future__ import annotations

SCHEMA_HINTS = """\
DuckDB read-only ledger. Reporting "today" is `meta.as_of_date` — use it for any
"now"/"overdue" logic instead of CURRENT_DATE. Prefer the analytics views.

VIEWS (prefer these):
- v_invoices(invoice_id, customer_id, issue_date, due_date, amount, currency,
    status, as_of_date, days_overdue, aging_bucket)
    status ∈ {paid, open, overdue}; aging_bucket ∈ {current, 1-30, 31-60, 61-90, 90+}.
- v_customer_ar(customer_id, name, segment, profile, credit_limit,
    open_invoices, outstanding_amount, overdue_amount, max_days_overdue)
    one row per customer — the go-to for "who owes / who to prioritize".
- v_dso(period_days, receivables, credit_sales, dso_days)
    single-row Days Sales Outstanding over the trailing 90 days.

BASE TABLES:
- customers(customer_id, name, segment, industry, country, profile,
    payment_terms_days, invoice_count, credit_limit, onboarded_at)
    segment ∈ {enterprise, mid_market, smb}; profile ∈ {prompt, reliable, slow,
    delinquent, defaulter}.
- invoices(invoice_id, customer_id, issue_date, due_date, amount, currency, status)
- payments(payment_id, invoice_id, customer_id, payment_date, amount_paid, method)
- communications(comm_id, customer_id, invoice_id, sent_at, channel, stage)
    dunning touch-points; stage ∈ {first_reminder, second_reminder, phone_call,
    escalation_notice, final_notice}.
- payment_profiles(profile, default_rate, late_mean_days)

Amounts are USD. You may only SELECT; the tool rejects writes, multiple
statements, unknown tables, and caps the row count automatically.
"""

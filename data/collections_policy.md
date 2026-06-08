# Accounts Receivable & Collections Policy

> Synthetic policy document for a fictional company ("Northwind Receivables").
> It is the knowledge base the agent retrieves from (RAG). It is written to be
> *chunkable*: each `##` section is a self-contained rule an agent can cite.
> Aging buckets and the dunning cadence here intentionally match the synthetic
> ledger, so policy answers and ledger facts line up.

## Purpose and scope

This policy governs how outstanding customer invoices are monitored, when and
how customers are contacted, when an account is escalated, and the conditions
under which payment plans, discounts, credit holds, and write-offs are applied.
It applies to all open accounts-receivable balances denominated in USD.

## Payment terms

Standard payment terms are **Net 30** from the invoice issue date. Negotiated
terms by segment are:

- **Enterprise** customers: Net 45.
- **Mid-market** customers: Net 30.
- **SMB** customers: Net 15.

An invoice is **current** until the day after its due date. The due date is the
issue date plus the customer's payment-terms days.

## Aging buckets

Outstanding invoices are classified by how many days past due they are, measured
as of the reporting date:

- **Current** — not yet past due.
- **1–30 days** past due.
- **31–60 days** past due.
- **61–90 days** past due.
- **90+ days** past due.

Aging is the primary lens for prioritisation: the older the bucket and the
larger the balance, the higher the collection priority.

## Dunning cadence

Reminders for an overdue invoice follow a fixed cadence, counted in days after
the **due date**:

- **Day 7** — first reminder (email). Friendly, assumes oversight.
- **Day 15** — second reminder (email). Restates the amount and due date.
- **Day 30** — phone call. Confirm receipt of the invoice and ask for a payment
  commitment date.
- **Day 45** — escalation notice (email). Warns that the account may be placed
  on credit hold and referred for escalation.
- **Day 60** — final notice (phone). Last contact before the account is handed
  to the escalation / recovery process.

Reminders are suppressed automatically once an invoice is paid in full or a
payment plan is in place.

## Prioritisation rules

When deciding which accounts to work first, apply this order:

1. **High-value, deeply aged.** Invoices in the **90+** bucket above **$50,000**
   are worked first; they carry the most risk and the most cash.
2. **Balance over the customer's credit limit.** Any customer whose total
   outstanding exceeds their credit limit is prioritised regardless of bucket.
3. **Concentration risk.** Customers whose overdue balance is a large share of
   total overdue AR are prioritised, even if individual invoices are smaller.
4. **Behaviour trend.** Accounts sliding from on-time to chronically late are
   prioritised earlier than steady-state late payers.

Lower priority: small balances under **$1,000** in the 1–30 bucket, which are
handled by automated reminders only.

## Payment plans

A payment plan may be offered when a customer requests one in good faith and the
account is not already in default:

- Eligible when the **overdue balance is at least $5,000** and **no more than
  90 days** past due.
- Maximum plan length is **6 monthly instalments**.
- A plan requires a **first instalment within 7 days** of agreement.
- While a plan is honoured, dunning reminders for the covered invoices are
  paused and the account is **not** referred for escalation.
- Missing two consecutive instalments voids the plan and resumes escalation from
  the current aging bucket.

## Early-payment discounts

To accelerate cash collection, an early-payment discount of **2% (terms 2/10
Net 30)** may be offered to customers who pay within **10 days** of the invoice
date. Discounts are not offered on invoices already past due, and never stack
with a payment plan.

## Credit holds

A customer is placed on **credit hold** — no new orders shipped on credit —
when any of the following is true:

- An invoice reaches **60+ days** past due without a payment commitment.
- Total outstanding balance **exceeds the credit limit**.
- A payment plan has been voided for missed instalments.

A hold is released once the balance returns below the credit limit **and** no
invoice is more than 30 days past due.

## Escalation and recovery

Accounts that reach the **90+** bucket after the final notice, or that breach a
voided payment plan, are referred to the escalation / recovery process:

- The account is placed on credit hold (if not already).
- A formal demand letter is issued.
- Balances above **$25,000** may be referred to a third-party collections
  agency or to legal review.

## Write-off thresholds

An invoice becomes a candidate for **write-off** (recognised as bad debt) when:

- It is **180+ days** past due, **and**
- All dunning and escalation steps have been exhausted with no payment or
  payment commitment.

Write-offs above **$10,000** require finance-manager approval. Writing off a
balance does not by itself close the customer; future business may resume on a
prepaid basis only.

## Disputes

If a customer disputes an invoice, dunning for that invoice is **paused** while
the dispute is investigated, but the clock for aging continues to run for
reporting purposes. Disputes must be resolved or rejected within 15 business
days. A rejected dispute resumes the dunning cadence from the current bucket.

## Communication standards

- Always identify the company, the invoice number, the amount, and the due date.
- Early reminders assume good faith; tone escalates only with the aging bucket.
- Never disclose account details to anyone other than an authorised contact.
- Every outbound contact (email or phone) is logged against the invoice.

## Key metrics

- **DSO (Days Sales Outstanding)** = (accounts receivable / credit sales in the
  period) × number of days in the period. It estimates the average time to
  collect. A rising DSO signals slowing collections.
- **Overdue rate** = overdue balance ÷ total receivables. Tracked overall and by
  customer segment and behaviour profile.
- **Collection effectiveness** is reviewed monthly against these metrics.

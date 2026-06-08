"""Synthetic accounts-receivable data generator.

Builds a single-file DuckDB ledger (`ledger.duckdb`) with a realistic
receivables data model:

  dimensions  : payment_profiles, customers
  facts       : invoices, payments, communications
  analytics   : v_invoices, v_customer_ar, v_dso  (derived in SQL)

Design (see docs/DECISIONS.md, ADR-001):

  * **Faker** generates the small dimension tables (customers) in Python.
  * **DuckDB** generates the large fact tables *set-based* — no Python row
    loops. ~1M+ invoices are produced by a single CTAS that cross-joins each
    customer with a correlated ``range()`` and draws every random attribute in
    SQL. This is the whole point: the engine does the heavy lifting.

The data carries *signal*: every customer is assigned a payment-behaviour
profile (prompt / reliable / slow / delinquent / defaulter) that drives default
rate and days-late distribution, so aging, DSO and collections activity are not
uniform noise — an agent can actually find the slow payers.

Determinism: a fixed seed feeds both Faker and DuckDB's ``setseed``; generation
runs single-threaded so ``random()`` is reproducible. Same seed + same args =>
byte-comparable distributions.

Usage:
    python data/generate.py                       # defaults: ~1.1M invoices
    python data/generate.py --customers 2000      # smaller, fast dev set
    python data/generate.py --as-of 2026-06-30 --seed 42 --out data/ledger.duckdb
"""

from __future__ import annotations

import argparse
import os
import random
from dataclasses import dataclass
from datetime import date, datetime

import duckdb
from faker import Faker

# --------------------------------------------------------------------------- #
# Reference data: payment-behaviour profiles and customer segments.
# These are the knobs that put signal into the generated facts.
# --------------------------------------------------------------------------- #

# profile -> (weight, default_rate, late_mean_days)
#   default_rate    : P(invoice is never paid) -> ages into the overdue book
#   late_mean_days  : mean of an exponential draw added to the due date
PAYMENT_PROFILES: dict[str, tuple[float, float, float]] = {
    "prompt": (0.25, 0.005, 1.0),  # pays on or before terms
    "reliable": (0.35, 0.010, 5.0),  # pays a few days late
    "slow": (0.22, 0.040, 22.0),  # routinely weeks late
    "delinquent": (0.13, 0.120, 50.0),  # very late, sometimes defaults
    "defaulter": (0.05, 0.450, 75.0),  # frequently never pays
}

# segment -> (weight, terms_days, invoices_lo, invoices_hi, ln_mu, ln_sigma)
#   invoices_*  : per-customer invoice count is uniform in [lo, hi]
#   ln_mu/sigma : invoice amount ~ lognormal(mu, sigma)  (USD)
SEGMENTS: dict[str, tuple[float, int, int, int, float, float]] = {
    "enterprise": (0.08, 45, 150, 320, 10.5, 0.9),  # ~$36k median, big books
    "mid_market": (0.30, 30, 60, 150, 9.2, 0.8),  # ~$10k median
    "smb": (0.62, 15, 20, 80, 7.8, 0.7),  # ~$2.4k median, long tail
}

INDUSTRIES = [
    "Manufacturing", "Logistics", "Retail", "Construction", "Healthcare",
    "Technology", "Energy", "Agriculture", "Hospitality", "Professional Services",
]

PAYMENT_METHODS = ["wire", "ach", "credit_card", "check"]

# Dunning cadence: days after due date, channel, stage. Communications are
# generated for overdue invoices whose due date + offset has already passed.
DUNNING_SCHEDULE = [
    (7, "email", "first_reminder"),
    (15, "email", "second_reminder"),
    (30, "phone", "phone_call"),
    (45, "email", "escalation_notice"),
    (60, "phone", "final_notice"),
]


@dataclass
class Args:
    customers: int
    as_of: date
    seed: int
    out: str


# --------------------------------------------------------------------------- #
# Dimension generation (Python + Faker)
# --------------------------------------------------------------------------- #

def _weighted_keys(spec: dict[str, tuple]) -> tuple[list[str], list[float]]:
    keys = list(spec.keys())
    weights = [v[0] for v in spec.values()]
    return keys, weights


def build_customers(args: Args) -> list[tuple]:
    """Generate the customer dimension with Faker. Small enough for Python."""
    fake = Faker()
    Faker.seed(args.seed)
    rng = random.Random(args.seed)

    seg_keys, seg_weights = _weighted_keys(SEGMENTS)
    prof_keys, prof_weights = _weighted_keys(PAYMENT_PROFILES)

    # Customers must have been onboarded before they can be invoiced; spread
    # onboarding across the ~4 years preceding the as-of date.
    earliest = date(args.as_of.year - 4, 1, 1)
    span_days = (args.as_of - earliest).days

    rows: list[tuple] = []
    for cid in range(1, args.customers + 1):
        segment = rng.choices(seg_keys, weights=seg_weights, k=1)[0]
        profile = rng.choices(prof_keys, weights=prof_weights, k=1)[0]
        _, terms, inv_lo, inv_hi, _, _ = SEGMENTS[segment]

        onboarded = earliest.toordinal() + rng.randint(0, span_days - 90)
        onboarded_at = date.fromordinal(onboarded)
        invoice_count = rng.randint(inv_lo, inv_hi)
        # Credit limit loosely tracks segment scale.
        credit_limit = {"enterprise": 500_000, "mid_market": 120_000, "smb": 30_000}[segment]
        credit_limit = round(credit_limit * rng.uniform(0.5, 1.5), -2)

        rows.append(
            (
                cid,
                fake.company(),
                segment,
                rng.choice(INDUSTRIES),
                fake.country_code(),
                profile,
                terms,
                invoice_count,
                float(credit_limit),
                onboarded_at,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Schema + set-based fact generation (DuckDB)
# --------------------------------------------------------------------------- #

def create_dimensions(con: duckdb.DuckDBPyConnection, customers: list[tuple]) -> None:
    con.execute(
        """
        CREATE TABLE payment_profiles (
            profile        VARCHAR PRIMARY KEY,
            default_rate   DOUBLE,   -- P(invoice never paid)
            late_mean_days DOUBLE    -- mean of exponential days-late draw
        );
        """
    )
    con.executemany(
        "INSERT INTO payment_profiles VALUES (?, ?, ?)",
        [(name, dr, lm) for name, (_, dr, lm) in PAYMENT_PROFILES.items()],
    )

    con.execute(
        """
        CREATE TABLE customers (
            customer_id        INTEGER PRIMARY KEY,
            name               VARCHAR,
            segment            VARCHAR,
            industry           VARCHAR,
            country            VARCHAR,
            profile            VARCHAR REFERENCES payment_profiles(profile),
            payment_terms_days INTEGER,
            invoice_count      INTEGER,
            credit_limit       DOUBLE,
            onboarded_at       DATE
        );
        """
    )
    con.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        customers,
    )

    # Single-row meta table holds the as-of date so views are self-contained
    # ("today" is stable and queryable, not hardcoded in every view).
    con.execute("CREATE TABLE meta (as_of_date DATE);")
    con.execute("INSERT INTO meta VALUES (?)", [_args_as_of])


def generate_facts(con: duckdb.DuckDBPyConnection, as_of: date) -> None:
    """Generate invoices, payments and communications set-based in SQL."""
    as_of_lit = as_of.isoformat()

    # 1) Raw invoice generation. Every random attribute is drawn in SQL.
    #    Box-Muller turns two uniforms into a standard normal for the lognormal
    #    amount; an exponential draw models days-late; a Bernoulli draw models
    #    default. One correlated range() per customer fans out the rows.
    con.execute(
        f"""
        CREATE TEMP TABLE _invoice_gen AS
        WITH amount_params AS (
            SELECT * FROM (VALUES
                ('enterprise', 10.5, 0.9),
                ('mid_market',  9.2, 0.8),
                ('smb',         7.8, 0.7)
            ) AS t(segment, ln_mu, ln_sigma)
        ),
        gen AS (
            SELECT
                c.customer_id,
                c.payment_terms_days,
                c.onboarded_at,
                ap.ln_mu,
                ap.ln_sigma,
                p.default_rate,
                p.late_mean_days,
                -- issue date: uniform between onboarding and as-of
                (c.onboarded_at
                    + CAST(floor(random()
                        * date_diff('day', c.onboarded_at, DATE '{as_of_lit}'))
                      AS INTEGER))                              AS issue_date,
                -- lognormal amount via Box-Muller(u1, u2)
                round(exp(ap.ln_mu + ap.ln_sigma
                    * sqrt(-2 * ln(random())) * cos(2 * pi() * random())), 2) AS amount,
                random()                                        AS u_default,
                random()                                        AS u_late
            FROM customers c
            JOIN payment_profiles p ON c.profile = p.profile
            JOIN amount_params ap   ON c.segment = ap.segment,
                 range(1, c.invoice_count + 1) AS g(n)
        ),
        derived AS (
            SELECT
                customer_id,
                issue_date,
                issue_date + payment_terms_days                 AS due_date,
                greatest(amount, 1.0)                           AS amount,
                payment_terms_days,
                (u_default < default_rate)                      AS will_default,
                -- exponential days-late, floored at 0
                CAST(round(-late_mean_days * ln(greatest(u_late, 1e-9))) AS INTEGER) AS days_late
            FROM gen
        )
        SELECT
            row_number() OVER ()                                AS invoice_id,
            customer_id,
            issue_date,
            due_date,
            amount,
            'USD'                                               AS currency,
            days_late,
            -- payment_date is NULL for defaults; otherwise due + days_late
            CASE WHEN will_default THEN NULL
                 ELSE due_date + days_late END                  AS payment_date
        FROM derived;
        """
    )

    # 2) Invoices fact: status derived from payment_date vs the as-of date.
    #    paid    : payment happened on or before as-of
    #    open    : not yet paid and not yet due
    #    overdue : not yet paid and past due (includes future-dated payments
    #              that haven't landed and outright defaults)
    con.execute(
        f"""
        CREATE TABLE invoices AS
        SELECT
            invoice_id,
            customer_id,
            issue_date,
            due_date,
            amount,
            currency,
            CASE
                WHEN payment_date IS NOT NULL AND payment_date <= DATE '{as_of_lit}' THEN 'paid'
                WHEN due_date >= DATE '{as_of_lit}'                                   THEN 'open'
                ELSE 'overdue'
            END                                                 AS status
        FROM _invoice_gen;
        """
    )

    # 3) Payments fact: one full payment per actually-paid invoice.
    con.execute(
        f"""
        CREATE TABLE payments AS
        SELECT
            row_number() OVER ()                                AS payment_id,
            g.invoice_id,
            g.customer_id,
            g.payment_date,
            g.amount                                            AS amount_paid,
            (['wire','ach','credit_card','check'])[1 + CAST(floor(random()*4) AS INTEGER)] AS method
        FROM _invoice_gen g
        WHERE g.payment_date IS NOT NULL AND g.payment_date <= DATE '{as_of_lit}';
        """
    )

    # 4) Communications fact: dunning touch-points for overdue invoices whose
    #    scheduled reminder date has already passed.
    schedule_values = ",\n            ".join(
        f"({off}, '{ch}', '{stage}')" for off, ch, stage in DUNNING_SCHEDULE
    )
    con.execute(
        f"""
        CREATE TABLE communications AS
        WITH schedule(offset_days, channel, stage) AS (
            VALUES
            {schedule_values}
        )
        SELECT
            row_number() OVER ()                                AS comm_id,
            i.customer_id,
            i.invoice_id,
            i.due_date + s.offset_days                          AS sent_at,
            s.channel,
            s.stage
        FROM invoices i
        CROSS JOIN schedule s
        WHERE i.status = 'overdue'
          AND i.due_date + s.offset_days <= DATE '{as_of_lit}';
        """
    )

    con.execute("DROP TABLE _invoice_gen;")


def create_views(con: duckdb.DuckDBPyConnection) -> None:
    """Analytics derived in SQL: enriched invoices, per-customer AR, DSO."""
    # Enriched invoice view: days overdue and aging bucket as of meta.as_of_date.
    con.execute(
        """
        CREATE VIEW v_invoices AS
        SELECT
            i.*,
            m.as_of_date,
            CASE WHEN i.status = 'overdue'
                 THEN date_diff('day', i.due_date, m.as_of_date) ELSE 0 END AS days_overdue,
            CASE
                WHEN i.status != 'overdue'                              THEN 'current'
                WHEN date_diff('day', i.due_date, m.as_of_date) <= 30   THEN '1-30'
                WHEN date_diff('day', i.due_date, m.as_of_date) <= 60   THEN '31-60'
                WHEN date_diff('day', i.due_date, m.as_of_date) <= 90   THEN '61-90'
                ELSE '90+'
            END                                                          AS aging_bucket
        FROM invoices i CROSS JOIN meta m;
        """
    )

    # Per-customer accounts-receivable summary.
    con.execute(
        """
        CREATE VIEW v_customer_ar AS
        SELECT
            c.customer_id,
            c.name,
            c.segment,
            c.profile,
            c.credit_limit,
            count(*) FILTER (WHERE i.status != 'paid')                       AS open_invoices,
            coalesce(sum(i.amount) FILTER (WHERE i.status != 'paid'), 0)     AS outstanding_amount,
            coalesce(sum(i.amount) FILTER (WHERE i.status = 'overdue'), 0)   AS overdue_amount,
            max(i.days_overdue)                                             AS max_days_overdue
        FROM customers c
        LEFT JOIN v_invoices i ON c.customer_id = i.customer_id
        GROUP BY 1, 2, 3, 4, 5;
        """
    )

    # Days Sales Outstanding over the trailing 90 days:
    #   DSO = (accounts receivable / credit sales in period) * period_days
    con.execute(
        """
        CREATE VIEW v_dso AS
        WITH ar AS (
            SELECT sum(amount) AS receivables
            FROM v_invoices WHERE status != 'paid'
        ),
        sales AS (
            SELECT sum(amount) AS credit_sales
            FROM v_invoices
            WHERE issue_date > as_of_date - 90
        )
        SELECT
            90                                                       AS period_days,
            ar.receivables,
            sales.credit_sales,
            round(ar.receivables / nullif(sales.credit_sales, 0) * 90, 1) AS dso_days
        FROM ar, sales;
        """
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

_args_as_of: date  # set in main(), read by create_dimensions


def parse_args() -> Args:
    p = argparse.ArgumentParser(description="Generate synthetic receivables data into DuckDB.")
    p.add_argument("--customers", type=int, default=13_000, help="number of customers (default 13000 -> ~1.1M invoices)")
    p.add_argument("--as-of", type=str, default="2026-06-30", help="reference 'today' date, YYYY-MM-DD")
    p.add_argument("--seed", type=int, default=42, help="deterministic seed")
    p.add_argument("--out", type=str, default=os.path.join("data", "ledger.duckdb"), help="output DuckDB path")
    ns = p.parse_args()
    return Args(
        customers=ns.customers,
        as_of=datetime.strptime(ns.as_of, "%Y-%m-%d").date(),
        seed=ns.seed,
        out=ns.out,
    )


def main() -> None:
    global _args_as_of
    args = parse_args()
    _args_as_of = args.as_of

    if os.path.exists(args.out):
        os.remove(args.out)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    print(f"Generating synthetic ledger -> {args.out}")
    print(f"  customers={args.customers}  as_of={args.as_of}  seed={args.seed}")

    customers = build_customers(args)
    print(f"  built {len(customers):,} customers (Faker)")

    con = duckdb.connect(args.out)
    # Determinism: single-threaded so seeded random() is reproducible.
    con.execute("SET threads TO 1;")
    con.execute(f"SELECT setseed({(args.seed % 1000) / 1000.0});")

    create_dimensions(con, customers)
    generate_facts(con, args.as_of)
    create_views(con)

    # Summary so a run is self-verifying.
    n_inv, n_pay, n_comm = con.execute(
        "SELECT (SELECT count(*) FROM invoices),"
        "       (SELECT count(*) FROM payments),"
        "       (SELECT count(*) FROM communications)"
    ).fetchone()
    status_mix = con.execute(
        "SELECT status, count(*), round(100.0*count(*)/sum(count(*)) OVER (), 1) "
        "FROM invoices GROUP BY status ORDER BY 2 DESC"
    ).fetchall()
    dso = con.execute("SELECT dso_days, receivables FROM v_dso").fetchone()

    con.close()

    print(f"  invoices={n_inv:,}  payments={n_pay:,}  communications={n_comm:,}")
    print("  status mix:")
    for status, cnt, pct in status_mix:
        print(f"    {status:<8} {cnt:>10,}  ({pct}%)")
    print(f"  DSO={dso[0]} days   outstanding=${dso[1]:,.0f}")
    print("Done.")


if __name__ == "__main__":
    main()

"""What a cache hit is allowed to **claim** — the sentence, not the code.

Every test here exists because of one blind adversarial pass (2026-08-01) that
was handed the three plan-cache modules and a single sentence to break:

    "A cache hit is replayed live: every number in the reply comes from a query
    executed now, against the current ledger. Nothing is served from a frozen
    snapshot."

It came back with three reproductions, and none of them is a bug a mutation
finds, because none of them has a line to delete — the claim was simply wider
than the work:

* **the seal over zero queries.** A policy-only plan is a perfectly good cache
  hit that executes no SQL at all, and it was printing *"the query was re-run
  live against the ledger, so the numbers are current"* anyway. Three of the
  twelve one-click questions the demo ships are policy-only, so this was live.
* **the query that reads nothing.** ``SELECT 425000.00 AS total_overdue``
  satisfies the relation allow-list *vacuously* and re-runs forever under the
  seal — a number the model invented, re-printed as a fresh measurement.
* **the query that pins a date.** ``WHERE due_date < '2026-08-01'`` re-runs
  perfectly and answers the question of the day it was written. The number is
  fresh; the *meaning* is frozen.

The split of ownership these tests pin: `plan_replay` owns what the reply says,
`plan_cache.freshness_violation` owns which SQL may be replayed at all, and it
is enforced on **both** sides — writing (`plan_from_messages`) and serving
(`_replay_query`) — because the collection is persistent, ships pre-seeded in
the demo image, and outlives the code that filled it.
"""

from __future__ import annotations

import duckdb
import pytest

from src.agent.plan_cache import (
    REPLAYABLE_TOOLS,
    Plan,
    ToolStep,
    freshness_violation,
    plan_from_messages,
)
from src.agent.plan_replay import _FRESHNESS_CLAIMS, ReplayError, replay_plan
from src.agent.sql_guard import GuardrailError, guard_query, relations_read
from tests.conftest import shipped_default

_LEDGER_CLAIM = _FRESHNESS_CLAIMS["query_ledger"]
_POLICY_CLAIM = _FRESHNESS_CLAIMS["search_policy"]


def _prefix(reply: str) -> str:
    """The claim line — everything before the rendered body."""
    return reply.split("\n\n")[0]


def _tool_turn(name: str, args: dict):
    from types import SimpleNamespace

    return SimpleNamespace(content="", tool_calls=[{"name": name, "args": args, "id": "x"}])


def _final(text: str):
    from types import SimpleNamespace

    return SimpleNamespace(content=text, tool_calls=[])


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """A tiny ledger under allow-listed relation names."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE customers (customer_id INTEGER, name TEXT)")
    c.execute("INSERT INTO customers VALUES (1, 'ACME'), (2, 'Globex')")
    return c


def _replay(plan, con, policy):
    return replay_plan(
        plan,
        con,
        policy,
        max_rows=shipped_default("max_rows"),
        search_k=shipped_default("search_k"),
    )


# --- the claim must match the work ---------------------------------------- #


def test_every_replayable_tool_has_a_claim_of_its_own() -> None:
    """Compared against the **surface**, not against examples.

    The defect this file closes was a single sentence written about one tool and
    printed over every hit. A map keyed by tool only fixes that while the map
    covers the tools: add a third entry to `REPLAYABLE_TOOLS`, forget it here,
    and the new tool either inherits someone else's sentence or the reply says
    nothing about it. Set equality is what makes forgetting loud (the same
    reason `ALLOWED_RELATIONS` is compared against the catalog, R1-C3).
    """
    assert set(_FRESHNESS_CLAIMS) == REPLAYABLE_TOOLS
    # Distinct sentences: two tools sharing one string would satisfy the line
    # above and re-create the defect exactly.
    assert len(set(_FRESHNESS_CLAIMS.values())) == len(_FRESHNESS_CLAIMS)


def test_a_ledger_hit_still_carries_the_freshness_seal(con) -> None:
    """The shipped sentence, byte for byte — this half was never wrong."""
    plan = Plan((ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),))
    reply = _replay(plan, con, policy=None).reply

    assert _prefix(reply) == (
        "_(Answered from a cached plan — the query was re-run live against the "
        "ledger, so the numbers are current.)_"
    )


def test_a_policy_only_hit_does_not_claim_a_ledger_query(policy) -> None:
    """The finding, at its worst: three shipped chips, zero SQL, full seal.

    Asserted as **equality of the whole claim line** rather than as "the word
    ledger is absent". A `not in` check passes for a reply that says nothing at
    all, and would also pass for a reply that re-worded the ledger promise
    without the word — the failure mode here is a sentence, so the assertion has
    to be about the whole sentence.
    """
    plan = Plan((ToolStep("search_policy", {"query": "credit hold rule threshold"}),))
    result = _replay(plan, con=None, policy=policy)

    assert result.tools_used == ["search_policy"]
    assert _prefix(result.reply) == f"_(Answered from a cached plan — {_POLICY_CLAIM}.)_"
    # And the body is a real retrieval, so this is not passing on an empty reply.
    assert "policy" in result.reply.lower() and len(result.reply) > len(_prefix(result.reply))


def test_a_mixed_plan_claims_both_in_the_order_they_ran(con, policy) -> None:
    """Two tools, two claims, joined in run order — not in dict order."""
    ledger_step = ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"})
    policy_step = ToolStep("search_policy", {"query": "credit hold rule threshold"})

    ledger_first = _replay(Plan((ledger_step, policy_step)), con, policy)
    policy_first = _replay(Plan((policy_step, ledger_step)), con, policy)

    assert _prefix(ledger_first.reply) == (
        f"_(Answered from a cached plan — {_LEDGER_CLAIM}; {_POLICY_CLAIM}.)_"
    )
    assert _prefix(policy_first.reply) == (
        f"_(Answered from a cached plan — {_POLICY_CLAIM}; {_LEDGER_CLAIM}.)_"
    )


def test_no_curated_chip_claims_a_tool_it_did_not_run(ledger, policy) -> None:
    """The instrument aimed at the shipped corpus, not at a hand-written case.

    Iterates `curated_plans()` — the twelve one-click questions the image bakes —
    and asserts each claim appears **if and only if** the plan holds a step for
    that tool. This is the test that would have caught the original defect, on
    three chips at once, without anyone suspecting it.

    The two `any(...)` anchors below are not decoration: an "if and only if" over
    a corpus that happens to hold only ledger plans is satisfied vacuously on the
    policy side, and a curated corpus is exactly the kind of list someone edits.
    """
    from data.curated_plans import curated_plans

    plans = curated_plans()
    assert any(
        all(step.tool == "search_policy" for step in plan.steps) for plan in plans.values()
    ), "no policy-only curated plan left — the iff below is vacuous on that side"
    assert any(
        all(step.tool == "query_ledger" for step in plan.steps) for plan in plans.values()
    ), "no ledger-only curated plan left — the iff below is vacuous on that side"

    for question, plan in plans.items():
        reply = _replay(plan, ledger, policy).reply
        tools = {step.tool for step in plan.steps}
        for tool, claim in _FRESHNESS_CLAIMS.items():
            assert (claim in reply) is (tool in tools), (
                f"{question!r}: claim for {tool!r} present={claim in reply}, "
                f"plan runs it={tool in tools}"
            )


def test_a_replayed_tool_without_a_claim_falls_back_to_the_llm(con, monkeypatch) -> None:
    """The branch that keeps the map honest when someone edits only the loop.

    A `ReplayError` costs a slow turn; the alternative — printing whatever
    sentence happens to be nearby — is the defect this file is named after.
    """
    monkeypatch.setitem(_FRESHNESS_CLAIMS, "query_ledger", None)
    plan = Plan((ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),))
    with pytest.raises(ReplayError, match="freshness claim"):
        _replay(plan, con, policy=None)


# --- a query that reads nothing ------------------------------------------- #

# Read-only, allow-list-clean, and evidence about nothing. The middle two are
# the guard's own allow-listed generators; the last one hides it behind a CTE,
# which is where a relation check that walked the text instead of the tree would
# stop looking.
READS_NOTHING = (
    "SELECT 425000.00 AS total_overdue",
    "SELECT * FROM range(3)",
    "SELECT n FROM generate_series(1, 5) AS t(n)",
    "WITH answer AS (SELECT 425000.00 AS total_overdue) SELECT * FROM answer",
)


def test_the_guard_accepts_every_query_that_reads_nothing() -> None:
    """Says out loud *why* the refusals below count.

    If the guard rejected these, the tests underneath would be green for a
    reason that has nothing to do with the code they are testing. It does not:
    the relation allow-list is satisfied **vacuously** when there is no relation
    to allow, which is correct — a security guard is not a correctness guard
    (R1-C11) — and is precisely why `freshness_violation` has to exist.
    """
    assert len(READS_NOTHING) == 4  # count anchor
    for sql in READS_NOTHING:
        guard_query(sql)  # raises if the guard is what refuses these


def test_a_turn_whose_sql_reads_nothing_is_not_cached() -> None:
    for sql in READS_NOTHING:
        messages = [_tool_turn("query_ledger", {"sql": sql}), _final("$425,000 is overdue.")]
        assert plan_from_messages(messages) is None, sql

    # Discriminating control: the same shape, reading a real relation, IS cached
    # — so the refusals above are the new rule, not a broken extractor.
    grounded = [
        _tool_turn("query_ledger", {"sql": "SELECT sum(amount) AS total FROM invoices"}),
        _final("ok"),
    ]
    assert plan_from_messages(grounded) is not None


def test_a_stored_plan_that_reads_nothing_is_not_replayed(con) -> None:
    """The serving half, and its own owner.

    Not redundant with the test above: `plan_from_messages` is the *write* path,
    and the cache is a persistent collection that ships pre-seeded in the demo
    image and is warmed by a long-running Space. Plans written before this rule
    existed are being served right now, so the write-side check alone would fix
    nothing that is already stored.
    """
    for sql in READS_NOTHING:
        plan = Plan((ToolStep("query_ledger", {"sql": sql}),))
        with pytest.raises(ReplayError, match="reads no ledger relation"):
            _replay(plan, con, policy=None)


# --- a query that pins a date --------------------------------------------- #

# The four ways to write the same frozen date. All four reach the tree as a
# VARCHAR constant (measured on DuckDB 1.5.3), which is why one rule covers them
# and no cast syntax has to be enumerated.
PINNED_DATE_SPELLINGS = (
    "SELECT count(*) AS n FROM invoices WHERE due_date < '2026-03-01'",
    "SELECT count(*) AS n FROM invoices WHERE due_date < DATE '2026-03-01'",
    "SELECT count(*) AS n FROM invoices WHERE due_date < '2026-03-01'::DATE",
    "SELECT count(*) AS n FROM invoices WHERE due_date < CAST('2026-03-01' AS DATE)",
)

# What the engine's DATE cast accepts and its TIMESTAMP cast does not. The rule
# used to ask both, and a mutation showed the TIMESTAMP arm had no case of its
# own — these are the strings that prove the *asymmetry* runs the other way, so
# the surviving arm is the broader one.
DATE_ONLY_LITERALS = ("2026-08-01 25:00:00", "5877642-01-01", "2026-08-01 +00")

# The honest ways to ask a time-relative question, and the literals that are not
# dates at all. Every one of these must stay cacheable: a rule that refuses
# these would "fix" the finding by turning the cache off.
STAYS_CACHEABLE = (
    "SELECT count(*) AS n FROM invoices WHERE due_date < (SELECT as_of_date FROM meta)",
    "SELECT count(*) AS n FROM invoices WHERE due_date < now() - INTERVAL 30 DAY",
    "SELECT count(*) AS n FROM invoices WHERE status = 'overdue'",
    "SELECT count(*) AS n FROM invoices WHERE customer_id = 2026",
    "SELECT strftime(due_date, '%Y-%m') AS month FROM invoices",
    "SELECT * FROM v_dso",
    "SELECT name, (SELECT max(amount) FROM invoices) AS biggest FROM customers",
    "SELECT date_trunc('month', due_date) AS m, sum(amount) AS t FROM v_invoices GROUP BY 1",
    # `EXISTS (SELECT 1 …)` is the semi-join every dialect writes, and the first
    # version of the output rule walked into the subquery and refused it: a
    # placeholder inside a predicate is not an answer (blind pass, 2026-08-05).
    "SELECT count(*) AS n FROM customers c WHERE EXISTS ("
    "SELECT 1 FROM invoices i WHERE i.customer_id = c.customer_id "
    "AND i.due_date < current_date)",
    "SELECT count(*) AS n FROM customers c WHERE NOT EXISTS ("
    "SELECT 1 FROM payments p WHERE p.customer_id = c.customer_id)",
)


def test_every_spelling_of_a_pinned_date_is_refused() -> None:
    assert len(PINNED_DATE_SPELLINGS) == 4  # count anchor
    for sql in PINNED_DATE_SPELLINGS:
        guard_query(sql)  # the guard accepts all four — the refusal is ours
        reason = freshness_violation(sql)
        assert reason is not None and "pins a date" in reason, sql


def test_the_date_cast_is_the_broader_arm_on_the_installed_engine() -> None:
    """A claim about somebody else's library, pinned against the installed one.

    `time_literals` asks DuckDB a single question — *does this string cast to a
    DATE?* — and that is only sufficient because on DuckDB 1.5.3 nothing casts
    to TIMESTAMP without also casting to DATE. Nothing in this repo makes that
    true, and a library claim rots without anyone touching the code, so it is
    asserted here rather than trusted in a comment.

    Both directions are checked: no timestamp-only string exists (which is what
    lets the TIMESTAMP arm stay deleted), and date-only strings do (which is
    what makes the surviving arm the right one to keep).
    """
    con = duckdb.connect(":memory:")

    def casts(literal: str) -> tuple[bool, bool]:
        row = con.execute(
            "SELECT TRY_CAST(? AS DATE) IS NOT NULL, TRY_CAST(? AS TIMESTAMP) IS NOT NULL",
            [literal, literal],
        ).fetchone()
        assert row is not None
        return bool(row[0]), bool(row[1])

    probes = (
        *DATE_ONLY_LITERALS,
        "2026-03-01",
        "2026-08-01 10:00:00",
        "2026-08-01T10:00:00Z",
        "epoch",
        "infinity",
        "-infinity",
        "290309-12-22 00:00:00",
        "9999-12-31 23:59:59.999999",
        "overdue",
        "2026",
        "1 day",
    )
    assert len(probes) == 14  # count anchor

    timestamp_only = [p for p in probes if casts(p) == (False, True)]
    assert not timestamp_only, f"the TIMESTAMP arm now has cases of its own: {timestamp_only}"

    for literal in DATE_ONLY_LITERALS:
        assert casts(literal) == (True, False), literal

    # And the discriminating control: the probe set is not trivially one-sided.
    assert any(casts(p) == (True, True) for p in probes)
    assert any(casts(p) == (False, False) for p in probes)


# Dates that are pinned without a single date-shaped string in the SQL. Every
# one of these was found by a blind pass against the first version of the rule,
# which scanned string constants: `'01/08/2026'` is not a date to DuckDB and
# `'2026-08'` is half of one, so all four walked straight through it.
PINNED_WITHOUT_A_DATE_STRING = (
    "SELECT count(*) AS n FROM invoices WHERE due_date < make_date(2026, 8, 1)",
    "SELECT count(*) AS n FROM invoices WHERE due_date < to_timestamp(1785110400)::DATE",
    "SELECT count(*) AS n FROM invoices WHERE due_date < ('2026-08' || '-01')::DATE",
    "SELECT count(*) AS n FROM invoices WHERE due_date < strptime('01/08/2026', '%d/%m/%Y')",
    # And the two that beat the *repair*: mentioning the clock is not depending
    # on it. Both cancel `current_date` out and pin 2026-08-01 forever, and both
    # walked through the version of the check that vetoed on any clock mention.
    "SELECT count(*) AS n FROM invoices "
    "WHERE due_date < make_date(2026, 8, 1 + (current_date - current_date))",
    "SELECT count(*) AS n FROM invoices "
    "WHERE due_date < make_date(2026 + (current_date - current_date), 8, 1)",
)

# Answers the ledger does not decide, dressed up as queries that read it. The
# first is the shape a tiny model actually produces when it invents a total; the
# second hides the constant behind a branch that returns no rows.
CONSTANT_ANSWERS = (
    "SELECT 425000.00 AS total_overdue FROM v_customer_ar",
    "SELECT 425000.00 AS total_overdue FROM invoices WHERE 1 = 0 UNION ALL SELECT 425000.00",
    # The one that beat the *repair*: `row_number() OVER ()` reads no relation,
    # and a rule that asked "does any output fail to bind alone?" accepted it as
    # proof the list was ledger-decided. Padding is not evidence.
    "SELECT 425000.00 AS total_overdue, row_number() OVER () AS r FROM invoices",
)


def test_a_pinned_date_does_not_have_to_look_like_a_date() -> None:
    """The rule asks the binder what an expression *means*, not what it reads like.

    Each of these names a relation, passes the guard, contains no date-shaped
    literal, and freezes the question anyway. A scan over string constants — the
    first version of this check — accepted all four.
    """
    assert len(PINNED_WITHOUT_A_DATE_STRING) == 6  # count anchor
    for sql in PINNED_WITHOUT_A_DATE_STRING:
        guard_query(sql)  # the guard is not what refuses these
        assert relations_read(sql), f"{sql}: not a relation-less query either"
        reason = freshness_violation(sql)
        assert reason is not None and "pins a date" in reason, sql


def test_the_clock_is_not_a_pinned_date_however_it_is_spelled() -> None:
    """The other wall, and the one this rule broke first.

    `current_date` and `current_timestamp` are not function calls — DuckDB parses
    them as bare identifiers and resolves them in the binder — so a check that
    only knew about function names called the honest spelling a frozen date and
    refused it.
    """
    clocks = ("now()", "today()", "current_date", "current_timestamp", "get_current_timestamp()")
    assert len(clocks) == 5  # count anchor
    for clock in clocks:
        sql = f"SELECT count(*) AS n FROM invoices WHERE due_date < {clock}"
        assert freshness_violation(sql) is None, f"the clock was read as pinned: {clock}"


def test_naming_a_relation_is_not_reading_it() -> None:
    """`relations_read` is a necessary condition, never a sufficient one.

    Both payloads name an allow-listed relation, so the relation rule passes them
    — the second one was built by a blind pass precisely to walk through it. What
    catches them is the select list: an answer every branch of which is fixed at
    write time is not a measurement of anything.
    """
    assert len(CONSTANT_ANSWERS) == 3  # count anchor
    for sql in CONSTANT_ANSWERS:
        guard_query(sql)
        assert relations_read(sql), f"{sql}: the relation rule is not what refuses it"
        reason = freshness_violation(sql)
        assert reason is not None and "the ledger does not decide" in reason, sql

    # Discriminating control: `count(*)` also binds with no table at all, and it
    # is the demo's first one-click question — the aggregate exception is what
    # keeps this rule from turning the cache off.
    assert freshness_violation("SELECT count(*) AS customers FROM v_customer_ar") is None

    # The stated cost of requiring *every* output to be ledger-decided: a query
    # that labels its own result is not cacheable. Asserted so the trade-off is
    # a decision on the record and not an accident.
    labelled = "SELECT 'total' AS label, sum(amount) AS amount FROM v_invoices"
    assert freshness_violation(labelled) is not None


def test_a_date_only_literal_is_still_a_pinned_date() -> None:
    """The rule follows the engine, not the subset of dates a person types."""
    for literal in DATE_ONLY_LITERALS:
        sql = f"SELECT count(*) AS n FROM invoices WHERE due_date < '{literal}'"
        guard_query(sql)
        reason = freshness_violation(sql)
        assert reason is not None and "pins a date" in reason, literal


def test_time_relative_and_non_date_literals_stay_cacheable() -> None:
    """The other wall. Refusing everything is not a fix."""
    assert len(STAYS_CACHEABLE) == 10  # count anchor
    for sql in STAYS_CACHEABLE:
        assert freshness_violation(sql) is None, sql
        messages = [_tool_turn("query_ledger", {"sql": sql}), _final("ok")]
        assert plan_from_messages(messages) is not None, sql


def test_a_pinned_date_answers_the_question_of_the_day_it_was_written() -> None:
    """The measurement, not the rule — why the rule is worth a lost cache hit.

    Both queries below pass the guard, both re-run live, and both are honest on
    the day they are written. Then the ledger moves on, which is the whole
    premise of the plan-cache: only one of them is still asking the question the
    visitor asked.
    """
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE invoices (invoice_id INTEGER, due_date DATE)")
    con.execute("CREATE TABLE meta (as_of_date DATE)")
    con.execute("INSERT INTO meta VALUES (DATE '2026-03-01')")
    con.execute("INSERT INTO invoices VALUES (1, DATE '2026-01-15')")

    pinned = PINNED_DATE_SPELLINGS[0]
    relative = STAYS_CACHEABLE[0]
    count = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731

    assert count(pinned) == count(relative) == 1, "the two must agree on day one"

    # The ledger is regenerated with a later as-of date and three more overdue
    # invoices — exactly what the cache is designed to survive.
    con.execute("UPDATE meta SET as_of_date = DATE '2026-08-01'")
    con.executemany(
        "INSERT INTO invoices VALUES (?, ?)",
        [(2, "2026-04-01"), (3, "2026-05-01"), (4, "2026-06-01")],
    )

    assert count(relative) == 4, "the live question now has a different answer"
    assert count(pinned) == 1, "the pinned plan still answers the old one"

    # So the pinned plan is refused on both sides, and the relative one is not.
    assert freshness_violation(pinned) is not None
    assert freshness_violation(relative) is None
    with pytest.raises(ReplayError, match="pins a date"):
        _replay(Plan((ToolStep("query_ledger", {"sql": pinned}),)), con, policy=None)
    assert _replay(Plan((ToolStep("query_ledger", {"sql": relative}),)), con, policy=None)


# --- the shipped corpus survives the rules -------------------------------- #


def test_every_curated_query_earns_the_freshness_claim() -> None:
    """The demo, checked against my own new rule.

    A correctness rule that quietly un-caches the twelve one-click chips would
    be a worse regression than the defect it closes — the chips are the
    difference between a demo that answers in ~3s and one that runs a tiny model
    on a free CPU. This is the line that turns red if a curated plan is ever
    written with a hardcoded date.
    """
    from data.curated_plans import curated_plans

    checked = 0
    for question, plan in curated_plans().items():
        for step in plan.steps:
            if step.tool != "query_ledger":
                continue
            checked += 1
            assert freshness_violation(step.args["sql"]) is None, question
    assert checked >= 6, "expected the curated corpus to still hold ledger plans"


def test_the_image_build_refuses_a_curated_plan_it_could_not_replay() -> None:
    """The third place this rule is applied, and the one with no visitor.

    `data/seed_plan_cache.py` bakes the curated corpus into the demo image. It
    used to hold a closure whose comment said it *mirrors* the warm path's
    guarantee — a copy, in the file furthest from anyone's attention. Calling
    the same owner is only half the fix; the other half is a seam a test can
    reach, which is why `rejection_reason` is module level now.
    """
    from data.curated_plans import curated_plans
    from data.seed_plan_cache import rejection_reason

    for question, plan in curated_plans().items():
        assert rejection_reason(plan) is None, question

    pinned = Plan((ToolStep("query_ledger", {"sql": PINNED_DATE_SPELLINGS[0]}),))
    reason = rejection_reason(pinned)
    assert reason is not None and reason.startswith("freshness:")

    unsafe = Plan((ToolStep("query_ledger", {"sql": "DROP TABLE customers"}),))
    assert (rejection_reason(unsafe) or "").startswith("guard:")


def test_guard_failure_is_reported_as_a_freshness_violation_not_raised() -> None:
    """`freshness_violation` is called on the replay path *after* the guard, but
    it must not depend on that ordering: a caller that asks it first gets a
    reason, never a `GuardrailError` escaping into a turn.
    """
    for sql in ("DROP TABLE customers", "not sql at all", ""):
        reason = freshness_violation(sql)
        assert reason is not None and "not a parseable read-only statement" in reason
    with pytest.raises(GuardrailError):
        guard_query("DROP TABLE customers")

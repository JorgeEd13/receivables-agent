"""Routing geometry of the shipped plan-cache — measured with the **production**
embedding (ADR-009 Amendment 2026-08-01).

Why a suite of its own. The rest of ``test_plan_cache.py`` runs on the
deterministic hashing stand-in, which is the right tool for the *rule* (does an
ambiguous neighbourhood miss?) and says nothing at all about the *question that
matters*: where MiniLM actually places two real questions relative to the shipped
threshold. A stand-in cannot answer that, so until this file existed the claim
"a wrong match cannot do harm" had no owner and was, as written, false — a typed
question is 0.9767 from a curated plan that answers a narrower question.

So these tests embed the real corpus with the real model. That costs a one-time
ONNX model download (cached afterwards, and cached in CI) and about a second per
run. They deliberately do **not** skip when the model is missing: a routing suite
that quietly skips is exactly the absent owner it was written to replace.

Two of the four are surface tests rather than examples (the ``R1-C3`` pattern):
every curated pair is examined, and every curated question is routed. The typed
paraphrases can only be examples — but each one carries the measurement that
makes it discriminating, so a probe that would have been rejected anyway cannot
pass for a probe the margin rejected.
"""

from __future__ import annotations

import inspect
from itertools import combinations

import chromadb
import pytest
from data.curated_plans import curated_plans

from src.agent.plan_cache import AMBIGUITY_MARGIN, PlanCache, get_plan_cache
from src.core.config import Settings
from src.rag.embeddings import default_embedding_function

# Questions a visitor might type that sit *between* two curated plans. Each row
# carries the two plans it falls between; the assertions re-measure the geometry
# rather than trusting these numbers, so a drift in the model shows up as a
# failure here instead of as a wrong answer in the demo.
BETWEEN_TWO_PLANS = [
    # wants a list, is closest to the plan that returns one row
    "Which customers have the largest overdue balances?",
    # wants one customer, is closest to the plan that returns ten rows
    "Who is the top customer by overdue balance?",
]

# Paraphrases that are unambiguously about one curated plan. These guard the
# other direction: a margin wide enough to reject everything would "fix" the
# suite above while turning the cache off.
PARAPHRASE_HITS = [
    ("How many customers do we have in the ledger?", "How many customers are in the ledger?"),
    ("total overdue by aging bucket", "Show me the total overdue amount by aging bucket."),
    ("what does the policy say about credit holds", "What does our policy say about credit holds?"),
    ("top 5 of each age group", "Give me the top 5 of each age group."),
]


@pytest.fixture(scope="module")
def curated() -> dict:
    return curated_plans()


@pytest.fixture(scope="module")
def cache(curated) -> PlanCache:
    """The curated corpus, seeded exactly as ``data/seed_plan_cache.py`` seeds it,
    with the production embedding and the shipped threshold and margin."""
    client = chromadb.EphemeralClient()
    plan_cache = get_plan_cache(
        client,
        "plan_routing",
        default_embedding_function(),
        similarity_threshold=Settings().plan_cache_threshold,
    )
    for question, plan in curated.items():
        plan_cache.warm(question, plan)
    return plan_cache


@pytest.fixture(scope="module")
def neighbours(cache):
    """(question, cosine similarity) for the ``k`` nearest stored questions, read
    from the same collection the cache reads — no second copy of the metric.

    Memoised because every call embeds its text with MiniLM (~0.4 s): the tests
    below ask about the same handful of questions more than once, and the corpus
    census would otherwise pay for all 66 pairs instead of the 12 rows they come from.
    """
    memo: dict[tuple[str, int], list[tuple[str, float]]] = {}

    def _neighbours(question: str, k: int = 2) -> list[tuple[str, float]]:
        if (question, k) not in memo:
            result = cache._collection.query(query_texts=[question], n_results=k)
            memo[(question, k)] = [
                (i, 1.0 - d)
                for i, d in zip(result["ids"][0], result["distances"][0], strict=True)
            ]
        return memo[(question, k)]

    return _neighbours


def test_the_shipped_threshold_is_the_one_these_tests_measure() -> None:
    """Three copies of 0.90 exist: the setting production reads, and the defaults of
    ``get_plan_cache`` and ``PlanCache.__init__``. If they disagree, every
    measurement in this file describes a cache nobody runs. Measured before this
    covered all three: relaxing the ``PlanCache.__init__`` copy alone stayed green."""
    shipped = Settings().plan_cache_threshold
    for owner in (get_plan_cache, PlanCache.__init__):
        assert inspect.signature(owner).parameters["similarity_threshold"].default == shipped


def test_curated_corpus_inseparable_pairs_are_exactly_the_known_one(neighbours, curated) -> None:
    """Surface, not examples: every pair of curated questions, not a chosen few.

    Two questions closer than the threshold but carrying **different** plans cannot
    be told apart by the threshold at all — that is what the ambiguity margin is
    for. One such pair ships today. A second one appearing (someone adds a curated
    question near an existing one) is a routing decision, not a detail, so it fails
    here and gets made on purpose.
    """
    questions = list(curated)
    threshold = Settings().plan_cache_threshold

    examined = 0
    inseparable = set()
    for a, b in combinations(questions, 2):
        examined += 1
        similarity = dict(neighbours(a, k=len(questions)))[b]
        if similarity >= threshold and curated[a] != curated[b]:
            inseparable.add(frozenset((a, b)))

    # Count anchor. The first version of this line compared ``examined`` against a
    # formula over ``len(questions)`` — which the loop satisfies by construction, so
    # it held for *any* corpus and anchored nothing. What can really shrink is the
    # corpus, so both numbers are literal: twelve questions, sixty-six pairs, and
    # nine distinct plans (four phrasings deliberately share the top-5 plan). Delete
    # a curated question, or quietly point two of them at one plan, and this fails
    # instead of the census silently examining less.
    assert (len(questions), examined) == (12, 66)
    assert len({plan.to_json() for plan in curated.values()}) == 9
    assert inseparable == {
        frozenset(
            (
                "Which single customer has the largest overdue balance?",
                "Who are the top 10 customers by overdue balance?",
            )
        )
    }


def test_every_curated_question_routes_to_its_own_plan(cache, curated) -> None:
    """The one-click chips, all of them. This is what the exact-question shortcut
    protects: two of these are 0.9156 apart — inside the margin — so without it the
    demo's own suggestions would fall through to the slow model."""
    for question, plan in curated.items():
        hit = cache.lookup(question)
        assert hit is not None, f"curated question missed its own plan: {question}"
        assert hit.steps == plan.steps, f"curated question routed elsewhere: {question}"


def test_a_question_between_two_plans_falls_through_to_the_llm(cache, neighbours, curated) -> None:
    """The defect this session closed: these used to be answered by a plan that
    answers a *neighbouring* question, with the same "the numbers are current" seal."""
    threshold = Settings().plan_cache_threshold
    # Count anchor: the loop below proves nothing about probes it never sees, and
    # a probe table can be shortened one row at a time. Measured: deleting a single
    # row here left the whole repo green.
    assert len(BETWEEN_TWO_PLANS) == 2

    for question in BETWEEN_TWO_PLANS:
        (first, sim_1), (second, sim_2) = neighbours(question)

        # Discriminating, in three parts: the winner clears the threshold (so the
        # threshold is not what rejects this), the runner-up is a *different* plan,
        # and the gap is inside the margin. Without all three the assertion below
        # could pass for the wrong reason.
        assert sim_1 >= threshold, f"{question!r} is a threshold miss, not an ambiguous one"
        assert curated[first] != curated[second]
        assert sim_1 - sim_2 < AMBIGUITY_MARGIN

        assert cache.lookup(question) is None, f"still answered by a neighbour: {question}"


def test_an_unambiguous_paraphrase_still_hits_the_right_plan(cache, neighbours, curated) -> None:
    """The other wall. Rejecting ambiguity is only a fix if the cache still works:
    each of these clears the threshold *and* wins by more than the margin."""
    threshold = Settings().plan_cache_threshold
    assert len(PARAPHRASE_HITS) == 4  # count anchor, same reason as above

    for typed, expected in PARAPHRASE_HITS:
        (first, sim_1), _runner_up = neighbours(typed)
        assert first == expected, f"{typed!r} is closest to {first!r}, not {expected!r}"
        assert sim_1 >= threshold

        hit = cache.lookup(typed)
        assert hit is not None, f"paraphrase lost its plan: {typed}"
        assert hit.steps == curated[expected].steps


def test_a_rival_behind_the_shared_plan_cluster_is_not_missed(cache, curated) -> None:
    """The corpus reason the scan cannot stop at the runner-up.

    Four curated phrasings share the top-5-per-bucket plan, so a question in that
    cluster has *the same plan* at ranks 0 and 1 and the rival — the top-10 plan —
    at rank 2, 0.074 behind. Comparing only the runner-up compares a plan with
    itself and serves the hit. Found by a blind adversarial pass on 2026-08-01;
    these three questions are its reproducing cases.
    """
    for question in (
        "Which are the top 5 customers by overdue balance per bucket",
        "Who are the top 5 customers by overdue balance per bucket",
        "What are the top 5 customers by overdue balance per bucket",
    ):
        assert cache.lookup(question) is None, f"a rival at rank 2 went unexamined: {question}"


def test_byte_variants_of_every_chip_are_still_the_same_question(cache, curated) -> None:
    """Surface, not examples: every curated question, in the forms a real visitor
    produces. These embed at distance 0.0000 from the original, so refusing them is
    refusing the demo's own suggestions — which byte equality did, for the two chips
    that sit 0.9156 apart (blind pass, 2026-08-01)."""
    for question, plan in curated.items():
        for variant in (question + " ", " " + question, question.lower(), question.upper()):
            hit = cache.lookup(variant)
            assert hit is not None, f"variant of a curated question missed: {variant!r}"
            assert hit.steps == plan.steps, f"variant routed elsewhere: {variant!r}"


def test_the_margin_sits_between_the_two_measured_walls(neighbours) -> None:
    """The number itself has an owner. 0.10 is not a taste: it is above every
    ambiguous gap the corpus produces and below every legitimate one, both
    re-measured here. Move it far in either direction and this goes red — which is
    the whole point, because the two walls are what makes the value defensible."""
    widest_ambiguous = max(
        sim_1 - sim_2
        for question in BETWEEN_TWO_PLANS
        for (_, sim_1), (_, sim_2) in [neighbours(question)]
    )
    tightest_legitimate = min(
        sim_1 - sim_2
        for typed, _ in PARAPHRASE_HITS
        for (_, sim_1), (_, sim_2) in [neighbours(typed)]
    )

    assert widest_ambiguous < AMBIGUITY_MARGIN < tightest_legitimate

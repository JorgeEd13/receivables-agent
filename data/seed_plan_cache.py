"""Pre-warm the semantic plan-cache with example questions (ADR-009).

The plan-cache warms itself as visitors ask questions, but a demo feels better
if the *first* click is already instant. This script asks the agent a handful of
representative questions **once** — using a real provider — and lets the normal
warm path store each guard-validated plan. Nothing here is hand-written SQL: the
seeded plans are authored by the live agent, then re-executed live on every hit,
so they can never go stale.

Run it once, on a machine with a provider (a running Ollama or ``GEMINI_API_KEY``)
and after the ledger exists, before shipping the Space image:

    python data/generate.py            # build the ledger (needs the 'data' extra)
    python -m data.seed_plan_cache     # warm the cache (needs a provider)

The cache persists under ``chroma_path`` alongside the policy index, so a baked
image ships pre-warmed. Requires ``plan_cache_enabled`` (the default).
"""

from __future__ import annotations

from typing import Any

# Representative of what a reviewer actually types at the demo: the aging / DSO /
# top-overdue / policy questions people ask of a receivables agent. Kept close to
# the golden set so the seeded plans are the ones most likely to be hit.
#
# The UI's one-click SUGGESTIONS (web/src/App.jsx) MUST be a subset of these exact
# strings, so every suggested question is a cache hit that replays in ~3s on the
# free CPU Space. Keep the two lists in lockstep (a paraphrase misses the 0.90
# similarity threshold and falls through to the slow tiny model).
SEED_QUESTIONS: list[str] = [
    "How many customers are in the ledger?",
    "What is our current DSO?",
    "Which single customer has the largest overdue balance?",
    "Show me the total overdue amount by aging bucket.",
    "Who are the top 10 customers by overdue balance?",
    "What does our policy say about credit holds?",
    "At how many days past due does an invoice become a write-off candidate?",
    "When is a customer eligible for a payment plan?",
]


def rejection_reason(plan: Any) -> str | None:
    """Why this curated plan must not be baked into the image, or ``None``.

    The same two conditions ``plan_from_messages`` applies on the live warm path:
    guard-valid **and** honest to replay. Both are *called*, not restated — this
    file used to carry a ``_validated`` closure whose comment said it "mirrors"
    the warm path, and a mirror is a copy that agrees until it doesn't (ADR-014,
    R1-C12). A curated plan that pins a date would otherwise seed cleanly and
    then be refused at replay: a one-click chip that silently falls through to
    the slow model, which is the exact failure the curated corpus exists to
    prevent.

    Module level, not a closure inside ``seed_curated``, because the version
    inside the closure had no seam a test could reach — and an untested third
    copy of a rule is how the rule drifts.
    """
    from src.agent.plan_cache import freshness_violation
    from src.agent.sql_guard import GuardrailError, guard_query

    for step in plan.steps:
        if step.tool == "query_ledger":
            sql = step.args.get("sql")
            if not isinstance(sql, str):
                return "query_ledger step has no SQL"
            try:
                guard_query(sql)
            except GuardrailError as exc:
                return f"guard: {exc}"
            unfit = freshness_violation(sql)
            if unfit is not None:
                return f"freshness: {unfit}"
    return None


def seed_curated() -> int:
    """Write the hand-authored plans (``data/curated_plans.py``) into the cache.

    No model, no ledger call: each curated plan is re-validated through the same
    ``sql_guard`` the live warm path uses (never store an unsafe plan), then
    upserted. This is what the image build runs so the demo's one-click questions
    are instant + correct from the first request and survive every restart (ADR-012).
    """
    import chromadb
    from data.curated_plans import curated_plans

    from src.agent.plan_cache import get_plan_cache
    from src.core.config import get_settings
    from src.rag.embeddings import default_embedding_function

    settings = get_settings()
    if not settings.plan_cache_enabled:
        print("plan_cache_enabled is False — nothing to seed.")
        return 1

    client = chromadb.PersistentClient(path=settings.chroma_path)
    cache = get_plan_cache(
        client,
        settings.plan_cache_collection,
        default_embedding_function(),
        similarity_threshold=settings.plan_cache_threshold,
    )

    written = 0
    for question, plan in curated_plans().items():
        rejection = rejection_reason(plan)
        if rejection is not None:
            print(f"  ✗ REJECTED ({rejection}) — not cached: {question}")
            continue
        cache.warm(question, plan)
        written += 1
        print(f"  ✓ curated plan cached: {question}")

    print(f"\nSeeded {written} curated plans into '{settings.plan_cache_collection}'.")
    return 0 if written else 1


def seed_via_model() -> int:
    """Warm the cache by asking the live agent each seed question (model-authored).

    Slower and hardware-dependent; kept for local/dev use. The image build uses the
    curated path instead (see ``seed_curated``).
    """
    from src.agent.cached_agent import CachedAgent
    from src.agent.graph import build_agent
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.plan_cache_enabled:
        print("plan_cache_enabled is False — nothing to seed.")
        return 1

    agent = build_agent(settings)
    if not isinstance(agent, CachedAgent):
        print("Agent is not cache-wrapped; check plan_cache_enabled.")
        return 1

    warmed = 0
    for question in SEED_QUESTIONS:
        # A single-message turn is the cache-eligible shape; the wrapper warms
        # the plan on a miss automatically.
        agent.invoke({"messages": [{"role": "user", "content": question}]})
        # Confirm it landed (a lookup now hits).
        if agent._cache.lookup(question) is not None:
            warmed += 1
            print(f"  ✓ cached plan for: {question}")
        else:
            print(f"  · not cached (no groundable plan): {question}")

    print(f"\nWarmed {warmed}/{len(SEED_QUESTIONS)} example plans.")
    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Seed the semantic plan-cache.")
    p.add_argument(
        "--curated",
        action="store_true",
        help="write hand-authored, guard-validated plans (no model; used by the image build)",
    )
    args = p.parse_args()
    return seed_curated() if args.curated else seed_via_model()


if __name__ == "__main__":
    raise SystemExit(main())

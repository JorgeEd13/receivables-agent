"""Semantic plan-cache (Phase 6 / ADR-009) — fully offline.

These prove the properties the plan-cache is *for*, without an LLM or a network:

* a plan is the agent's **tool calls**, extracted from a finished turn — and only
  a **read-only, guard-valid** turn is cacheable;
* lookup is **semantic** (a paraphrase hits; an unrelated question misses) under
  a conservative threshold;
* a hit is **replayed live** — the number always reflects the *current* data, so
  mutating the ledger changes the replayed answer (the load-bearing honesty
  claim: we cache the plan, never the answer);
* replay **re-validates through the guard** and abandons (``ReplayError``) if a
  cached query is no longer safe, so the caller falls back to the LLM.

The embeddings are the deterministic hashing function (shared vocabulary ⇒
similarity), so "similar" here means lexical overlap — enough to exercise the
threshold logic offline. The shipping cache uses the same MiniLM embeddings as
RAG (ADR-005).
"""

from __future__ import annotations

from types import SimpleNamespace

import chromadb
import duckdb
import pytest

from src.agent.cached_agent import CachedAgent
from src.agent.plan_cache import (
    Plan,
    PlanCache,
    ToolStep,
    get_plan_cache,
    plan_from_messages,
)
from src.agent.plan_replay import ReplayError, replay_plan
from src.rag.embeddings import DeterministicEmbeddingFunction


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def embedding_function() -> DeterministicEmbeddingFunction:
    return DeterministicEmbeddingFunction()


@pytest.fixture
def cache(embedding_function, request) -> PlanCache:
    client = chromadb.EphemeralClient()
    # A unique collection name per test — ChromaDB's in-process store can share
    # state across ephemeral clients, so a fixed name would bleed between tests.
    name = f"plan_cache_{abs(hash(request.node.nodeid)) % 10**8}"
    # A permissive threshold so the deterministic (lexical) embedding produces
    # hits for paraphrases in these tests; production defaults higher.
    return get_plan_cache(client, name, embedding_function, similarity_threshold=0.5)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """An in-memory ledger with allow-listed relation names the guard accepts."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE customers (customer_id INTEGER, name TEXT)")
    c.execute("INSERT INTO customers VALUES (1, 'ACME'), (2, 'Globex')")
    return c


def _tool_turn(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(content="", tool_calls=[{"name": name, "args": args, "id": "x"}])


def _final(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=text, tool_calls=[])


# --- plan extraction --------------------------------------------------------


def test_plan_extracted_from_a_grounded_turn() -> None:
    messages = [
        _tool_turn("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),
        _final("There are 2 customers."),
    ]
    plan = plan_from_messages(messages)
    assert plan is not None
    assert plan.steps == (
        ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),
    )


def test_turn_with_no_groundable_tool_is_not_cached() -> None:
    # A turn that answered from the model with no tool call is not reproducible.
    assert plan_from_messages([_final("I think it's fine.")]) is None


def test_plan_with_unsafe_sql_is_not_cached() -> None:
    # If the SQL wouldn't pass the guard, we must never store it as a plan.
    messages = [
        _tool_turn("query_ledger", {"sql": "DROP TABLE customers"}),
        _final("done"),
    ]
    assert plan_from_messages(messages) is None


def test_plan_dedupes_repeated_identical_calls() -> None:
    call = {"sql": "SELECT 1 AS n FROM customers"}
    messages = [_tool_turn("query_ledger", call), _tool_turn("query_ledger", call), _final("ok")]
    plan = plan_from_messages(messages)
    assert plan is not None and len(plan.steps) == 1


# --- semantic lookup --------------------------------------------------------


def test_paraphrase_hits_and_unrelated_misses(cache) -> None:
    plan = Plan((ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),))
    cache.warm("how many customers are in the ledger", plan)

    # A lexical paraphrase (shared words) is a hit.
    hit = cache.lookup("how many customers do we have in the ledger")
    assert hit is not None and hit.steps == plan.steps

    # A totally unrelated question is a miss → caller falls through to the LLM.
    assert cache.lookup("zebra giraffe elephant") is None


def test_empty_cache_misses(cache) -> None:
    assert cache.lookup("anything at all") is None


# --- live replay: freshness + guard re-validation ---------------------------


def test_replay_reflects_current_data_not_a_frozen_answer(con) -> None:
    """The load-bearing claim: a cached plan re-runs live, so the number moves."""
    plan = Plan((ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),))

    first = replay_plan(plan, con, policy=None, max_rows=100, search_k=1)
    assert "2" in first.reply
    assert first.tools_used == ["query_ledger"]

    # Mutate the ledger, then replay the SAME plan — the answer must update.
    con.execute("INSERT INTO customers VALUES (3, 'Initech')")
    second = replay_plan(plan, con, policy=None, max_rows=100, search_k=1)
    assert "3" in second.reply  # fresh, not the cached "2"


def test_replay_revalidates_the_guard_and_raises_on_unsafe(con) -> None:
    # A plan that somehow carries unsafe SQL is rejected at replay, not executed.
    bad = Plan((ToolStep("query_ledger", {"sql": "DELETE FROM customers"}),))
    with pytest.raises(ReplayError):
        replay_plan(bad, con, policy=None, max_rows=100, search_k=1)
    # And nothing was mutated.
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 2


def test_replay_raises_when_cached_sql_no_longer_runs(con) -> None:
    # The SQL passes the guard but references a dropped column → fall back to LLM.
    plan = Plan((ToolStep("query_ledger", {"sql": "SELECT gone FROM customers"}),))
    with pytest.raises(ReplayError):
        replay_plan(plan, con, policy=None, max_rows=100, search_k=1)


# --- CachedAgent integration ------------------------------------------------


class _RecordingAgent:
    """A stub compiled agent: returns a canned grounded turn and counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, payload: dict) -> dict:
        self.calls += 1
        messages = payload["messages"] + [
            _tool_turn("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),
            _final("There are 2 customers."),
        ]
        return {"messages": messages}


def _cached_agent(agent, cache, con) -> CachedAgent:
    return CachedAgent(agent, cache, con, policy=None, max_rows=100, search_k=1)


def test_miss_calls_llm_then_hit_replays_without_it(cache, con) -> None:
    stub = _RecordingAgent()
    agent = _cached_agent(stub, cache, con)
    q = {"messages": [{"role": "user", "content": "how many customers are in the ledger"}]}

    # First ask: a miss → the wrapped agent runs and the plan is warmed.
    agent.invoke(q)
    assert stub.calls == 1

    # Second ask (same question): a hit → replayed live, the LLM is NOT called.
    result = agent.invoke(q)
    assert stub.calls == 1  # unchanged
    reply = result["messages"][-1].content
    assert "cached plan" in reply and "2" in reply


def test_hit_stays_fresh_after_data_changes(cache, con) -> None:
    stub = _RecordingAgent()
    agent = _cached_agent(stub, cache, con)
    q = {"messages": [{"role": "user", "content": "how many customers are in the ledger"}]}

    agent.invoke(q)  # warm
    con.execute("INSERT INTO customers VALUES (3, 'Initech')")
    result = agent.invoke(q)  # hit → replayed live
    assert stub.calls == 1
    assert "3" in result["messages"][-1].content  # current count, not a frozen 2


def test_multi_turn_requests_are_not_cached(cache, con) -> None:
    # A follow-up depends on prior context, so it always goes to the LLM.
    stub = _RecordingAgent()
    agent = _cached_agent(stub, cache, con)
    payload = {
        "messages": [
            {"role": "user", "content": "how many customers are in the ledger"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "and how many are overdue"},
        ]
    }
    agent.invoke(payload)
    agent.invoke(payload)
    assert stub.calls == 2  # never served from cache


# --- curated seed plans (baked into the demo image) --------------------- #


def test_curated_plans_are_all_guard_valid_and_replayable() -> None:
    """Every curated plan (data/curated_plans.py) must be safe to bake: only
    replayable tools, and every query_ledger SQL passes sql_guard read-only."""
    from data.curated_plans import curated_plans
    from src.agent.plan_cache import REPLAYABLE_TOOLS
    from src.agent.sql_guard import guard_query

    plans = curated_plans()
    assert plans, "expected curated seed plans"
    for question, plan in plans.items():
        assert plan.steps, f"empty plan for: {question}"
        for step in plan.steps:
            assert step.tool in REPLAYABLE_TOOLS, f"{question}: non-replayable {step.tool}"
            if step.tool == "query_ledger":
                sql = step.args.get("sql")
                assert isinstance(sql, str) and sql.strip()
                guard_query(sql)  # raises if not read-only / allow-listed
            elif step.tool == "search_policy":
                assert isinstance(step.args.get("query"), str) and step.args["query"].strip()


def test_curated_questions_match_ui_suggestions() -> None:
    """The UI's one-click SUGGESTIONS must all be curated (so every chip is an
    instant, correct cache hit). Guards against the two lists drifting apart."""
    import re
    from pathlib import Path

    from data.curated_plans import CURATED_PLANS

    app_jsx = Path(__file__).resolve().parents[1] / "web" / "src" / "App.jsx"
    text = app_jsx.read_text(encoding="utf-8")
    block = re.search(r"const SUGGESTIONS = \[(.*?)\];", text, re.S).group(1)
    suggestions = re.findall(r'"([^"]+)"', block)
    assert suggestions, "no SUGGESTIONS found in App.jsx"
    missing = [s for s in suggestions if s not in CURATED_PLANS]
    assert not missing, f"UI suggestions not in curated cache: {missing}"

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

    def invoke(self, payload: dict, config: dict | None = None) -> dict:
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


def test_known_question_hits_cache_even_after_prior_turns(cache, con) -> None:
    """The fix for the demo bug: a self-contained question the cache knows must be
    served from the cache even when it's asked *after* an earlier Q&A (history is
    present) — clicking a suggested question mid-chat stays instant, not slow LLM."""
    stub = _RecordingAgent()
    agent = _cached_agent(stub, cache, con)
    q = "how many customers are in the ledger"

    agent.invoke({"messages": [{"role": "user", "content": q}]})  # warm (single-turn)
    assert stub.calls == 1

    calls_before = stub.calls
    result = agent.invoke(
        {
            "messages": [
                {"role": "user", "content": "what is our DSO"},
                {"role": "assistant", "content": "78 days."},
                {"role": "user", "content": q},  # a known, self-contained question
            ]
        }
    )
    assert stub.calls == calls_before  # served from cache — the LLM was NOT called
    assert "2" in result["messages"][-1].content  # replayed live


def test_novel_followup_mid_conversation_still_goes_to_llm(cache, con) -> None:
    # A genuine context-dependent follow-up won't match any cached plan (below the
    # similarity threshold), so it falls through to the LLM even mid-conversation.
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


# --- curated plans, replayed against a real ledger ---------------------- #
#
# The guard validates *relation names* and never looks at a column, so the two
# tests above accept SQL that cannot run: `guard_query("SELECT no_such_column
# FROM v_customer_ar")` returns a wrapped query, happily. Measured: renaming
# `v_customer_ar.overdue_amount` in the generator left this whole file green
# while three curated plans — three one-click chips on the live demo — became
# `Binder Error` at replay, and the visitor gets the slow tiny model instead of
# the instant answer the chip promises.
#
# So these replay every curated plan through `replay_plan`, the same function
# the demo runs on a cache hit, against the ledger `data/generate.py` builds.


@pytest.fixture(scope="module")
def policy():
    """The real policy document, indexed offline into an ephemeral collection.

    The deterministic (hashing) embedding stands in for MiniLM, so what this
    fixture supports is "retrieval finds *a* section and names it", never "this
    query ranks that section first" — the ranking belongs to the real embedding
    and is not measured here.
    """
    from src.rag.index import build_index

    return build_index(
        chromadb.EphemeralClient(),
        str(_repo_root() / _shipped("policy_path")),
        "curated_policy_replay",
        DeterministicEmbeddingFunction(),
    )


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def _shipped(field: str):
    """The default that ships, read off `Settings` instead of copied here.

    Not `Settings()`: that would read the developer's git-ignored `.env`, and a
    local `MAX_ROWS=5` must not quietly change what these assert.
    """
    from src.core.config import Settings

    return Settings.model_fields[field].default


def _replay(plan, ledger, policy):
    from src.agent.plan_replay import replay_plan

    return replay_plan(
        plan,
        ledger,
        policy,
        max_rows=_shipped("max_rows"),
        search_k=_shipped("search_k"),
    )


def test_every_curated_plan_replays_against_the_real_ledger(ledger, policy) -> None:
    """The column-level owner the guard cannot be.

    A `ReplayError` here is exactly what a visitor would hit: the chip falls
    through to the LLM, on a free CPU, for a question that was supposed to be
    instant. Tool usage is asserted too, so a plan that silently degrades to
    fewer steps than it declares is not read as a pass.
    """
    from data.curated_plans import curated_plans

    for question, plan in curated_plans().items():
        result = _replay(plan, ledger, policy)
        expected = list(dict.fromkeys(step.tool for step in plan.steps))
        assert result.tools_used == expected, question
        assert result.reply.strip(), question


def test_every_curated_query_still_returns_rows(ledger, policy) -> None:
    """Empty is the failure a schema check cannot see.

    Every column still exists, the SQL still parses, and `WHERE status =
    'overdue'` returns nothing because the generator renamed a status. The chip
    stays instant and answers "The query returned no rows." — worse than the
    slow path, because it reads like a fact about the business.

    The empty-render sentinel is taken *from the renderer* rather than copied as
    a string literal: a reworded message would otherwise make this test pass
    while stating nothing.
    """
    from data.curated_plans import curated_plans

    from src.agent.plan_replay import _render_rows

    empty = _render_rows(["any_column"], [])
    assert empty.strip() and empty != _render_rows(["any_column"], [(1,)])

    for question, plan in curated_plans().items():
        if not any(step.tool == "query_ledger" for step in plan.steps):
            continue
        assert empty not in _replay(plan, ledger, policy).reply, question


# The section each curated policy query exists to reach. Written here, outside
# both the plans and the document, because neither can be checked against
# itself: the query is free text with no schema, and retrieval always returns
# *something*, so deleting the section a chip asks about is silent — the visitor
# gets the nearest surviving chunk, presented as the policy's answer.
CURATED_POLICY_SECTIONS = {
    "credit hold rule threshold": "Credit holds",
    "write-off candidate days past due": "Write-off thresholds",
    "payment plan eligibility": "Payment plans",
}


def test_every_curated_policy_step_reaches_a_section_that_exists(policy) -> None:
    """The policy half of the same question.

    Two assertions with different jobs: the section a curated query depends on
    is still in the document (renaming `## Credit holds` turns this red), and
    the replay path still cites *a* section rather than dumping an unattributed
    chunk. What it deliberately does not claim is ranking — the deterministic
    embedding is a stand-in for MiniLM, so "this query retrieves that section
    first" is not measurable here (see the `policy` fixture).
    """
    from data.curated_plans import curated_plans

    from src.agent.plan_replay import _replay_search
    from src.rag.chunking import chunk_policy

    path = _repo_root() / _shipped("policy_path")
    headings = {c.heading for c in chunk_policy(path.read_text(encoding="utf-8"), path.name)}
    assert headings, "the policy document parsed into no sections"

    queries = {
        step.args["query"]
        for plan in curated_plans().values()
        for step in plan.steps
        if step.tool == "search_policy"
    }
    # Equality, not containment: a curated query added without naming the
    # section it targets would otherwise skip every check below.
    assert queries == set(CURATED_POLICY_SECTIONS)

    for query, section in sorted(CURATED_POLICY_SECTIONS.items()):
        assert section in headings, f"{query!r} targets a section the policy no longer has"
        rendered = _replay_search({"query": query}, policy, _shipped("search_k"))
        assert any(f"**{heading}**" in rendered for heading in headings), query

# --- tiny-model prompt + graceful recursion (ADR-013) ------------------- #


def test_select_prompt_uses_brief_for_constrained_tiny_model(monkeypatch) -> None:
    """A constrained tiny model gets the compact routing prompt; a strong/cloud
    model gets the full one."""
    from src.agent import graph
    from src.core.config import Settings

    monkeypatch.setattr(graph, "resolve_ollama_model", lambda s: "qwen2.5:1.5b", raising=False)
    # Patch the providers.should_constrain that select_prompt imports.
    import src.agent.providers as providers
    monkeypatch.setattr(providers, "should_constrain", lambda name, s: True)
    monkeypatch.setattr(providers, "resolve_ollama_model", lambda s: "qwen2.5:1.5b")

    tiny = Settings(primary_provider="ollama")
    assert graph.select_prompt(tiny) == graph.SYSTEM_PROMPT_BRIEF

    monkeypatch.setattr(providers, "should_constrain", lambda name, s: False)
    strong = Settings(primary_provider="ollama")
    assert graph.select_prompt(strong) == graph.SYSTEM_PROMPT

    cloud = Settings(primary_provider="gemini")
    assert graph.select_prompt(cloud) == graph.SYSTEM_PROMPT


def test_astream_handles_recursion_limit_gracefully(cache, con) -> None:
    """A looping agent (GraphRecursionError) yields a friendly answer event, not a
    broken stream or a bare error."""
    import asyncio

    from langgraph.errors import GraphRecursionError

    class _LoopingAgent:
        async def astream_events(self, payload, version, config=None):
            yield {"event": "on_tool_start", "name": "query_ledger"}
            raise GraphRecursionError("recursion limit reached")

    agent = CachedAgent(
        _LoopingAgent(), cache, con, policy=None, max_rows=100, search_k=1, recursion_limit=8
    )

    async def _collect():
        return [ev async for ev in agent.astream({"messages": [{"role": "user", "content": "loop me"}]})]

    events = asyncio.run(_collect())
    assert {"type": "tool", "name": "query_ledger"} in events
    answer = events[-1]
    assert answer["type"] == "answer"
    assert "couldn't finish" in answer["reply"].lower()
    assert answer["tools_used"] == ["query_ledger"]

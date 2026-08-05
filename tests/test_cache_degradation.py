"""The plan-cache is an optimisation, and an optimisation must never cost a turn.

ADR-009 Decision-2 promises this in one sentence — *"If a cached query no longer
validates or runs (`ReplayError`), the caller falls through to the LLM"* — and
until 2026-08-01 nothing in the repo exercised it: replacing the whole
fall-through with `raise` left the suite at **396 passed**. Tests existed for
`ReplayError` being *raised* (`replay_plan` called directly) and none for what
`CachedAgent` *does* with it. That is the difference between testing the part and
testing the seam.

The sibling hole was worse, because it needed no mutation at all to be real: a
stored plan that does not deserialize raised `JSONDecodeError` / `KeyError` /
`TypeError` — and, one layer down, `AttributeError` from a non-dict `args` — out
of `invoke`, `ainvoke` **and** `astream`. `astream`'s own `except Exception` did
not help: `_try_cache` runs before that `try` block. The cache is persistent and
outlives the code that wrote it, so "a stored document this version cannot read"
is a normal event, not a corruption scare.

So the claim these tests own is deliberately wider than the ADR sentence:

    no failure of any cache operation — lookup, replay or warm — can turn a turn
    into an error the visitor sees.

Three properties, kept separate on purpose:

* `Plan.from_json` is **total**: a `Plan` or a `PlanFormatError`, never a third
  type. That is what lets the caller say "unreadable" in one `except` instead of
  a list that grows every time someone finds a new way to corrupt a document;
* every **seam** the wrapper reaches into the cache machinery for degrades to the
  LLM, checked against the seam *surface* rather than a list of the ones that
  came to mind;
* the two `except` clauses in `_try_cache` are **not** interchangeable — the
  designed `ReplayError` path and the catch-all admit different things, and the
  log distinguishes them, so neither can be deleted while the other covers for it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from types import SimpleNamespace

import chromadb
import duckdb
import pytest

import src.agent.cached_agent as cached_agent_mod
from src.agent.cached_agent import CachedAgent
from src.agent.message_utils import final_text as _final_of
from src.agent.plan_cache import (
    Plan,
    PlanCache,
    PlanFormatError,
    ToolStep,
    get_plan_cache,
)
from src.agent.plan_replay import ReplayError, replay_plan
from src.rag.embeddings import DeterministicEmbeddingFunction

QUESTION = "how many customers are in the ledger"
CACHED_SQL = "SELECT count(*) AS n FROM customers"
LLM_ANSWER = "the model answered this one"


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    """An in-memory ledger with an allow-listed relation name."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE customers (customer_id INTEGER, name TEXT)")
    c.execute("INSERT INTO customers VALUES (1, 'ACME'), (2, 'Globex')")
    return c


@pytest.fixture
def make_cache(request):
    """A fresh, empty plan-cache — unique collection per call.

    ChromaDB's in-process store shares state across ephemeral clients, so a fixed
    name would bleed between tests.
    """
    counter = iter(range(100))

    def _make() -> PlanCache:
        name = f"degr_{abs(hash(request.node.nodeid)) % 10**8}_{next(counter)}"
        return get_plan_cache(
            chromadb.EphemeralClient(),
            name,
            DeterministicEmbeddingFunction(),
            similarity_threshold=0.5,
        )

    return _make


class _StubModel:
    """The wrapped agent: answers with a recognisable string, counts its calls.

    Supports all three shapes `CachedAgent` drives — sync, async and the event
    stream — so a degradation test can assert the *same* property on each entry
    point instead of proving it once and assuming the others.
    """

    def __init__(self) -> None:
        self.calls = 0

    def _turn(self, payload: dict) -> dict:
        self.calls += 1
        return {
            "messages": payload["messages"]
            + [
                SimpleNamespace(
                    content="",
                    tool_calls=[{"name": "query_ledger", "args": {"sql": CACHED_SQL}, "id": "1"}],
                ),
                SimpleNamespace(content=LLM_ANSWER, tool_calls=[]),
            ]
        }

    def invoke(self, payload: dict, config: dict | None = None) -> dict:
        return self._turn(payload)

    async def ainvoke(self, payload: dict, config: dict | None = None) -> dict:
        return self._turn(payload)

    async def astream_events(self, payload, version, config=None):
        yield {"event": "on_chain_end", "data": {"output": self._turn(payload)}}


ENTRYPOINTS = ("invoke", "ainvoke", "astream")


def _reply(agent: CachedAgent, entrypoint: str, question: str = QUESTION) -> str:
    """Drive one turn through one entry point and return the visitor-visible reply.

    Any exception escaping here *is* the failure under test, so nothing is caught:
    a raise fails the test with the real traceback. On the streaming path the
    equivalent failure is an `error` event, which is why the event type is
    asserted rather than the reply read blindly off the last event.
    """
    payload = {"messages": [{"role": "user", "content": question}]}
    if entrypoint == "invoke":
        return _final_of(agent.invoke(payload)["messages"])
    if entrypoint == "ainvoke":
        return _final_of(asyncio.run(agent.ainvoke(payload))["messages"])

    async def _collect():
        return [event async for event in agent.astream(payload)]

    events = asyncio.run(_collect())
    assert events[-1]["type"] == "answer", f"stream ended on {events[-1]!r}"
    return events[-1]["reply"]


def _agent(model, cache, con) -> CachedAgent:
    return CachedAgent(model, cache, con, policy=None, max_rows=100, search_k=1)


def _replayed_seal(con) -> str:
    """The line `plan_replay` stamps on a replayed answer, taken **from the
    renderer** by replaying a trivial plan — not copied here as a literal.

    A copied seal rots into a string nothing produces, and "the reply does not
    contain a string nothing produces" is a check that always passes.
    """
    # Trivial, but not *constant*: a select list the ledger does not decide is
    # refused before the seal is ever printed (R1-C15).
    trivial = Plan((ToolStep("query_ledger", {"sql": "SELECT count(*) AS n FROM customers"}),))
    return replay_plan(trivial, con, policy=None, max_rows=100, search_k=1).reply.split("\n")[0]


# --- the ADR-009 Decision-2 sentence, finally owned ------------------------ #


def test_replay_error_falls_through_to_the_llm(make_cache, con) -> None:
    """A cached plan that no longer runs must cost a slow answer, not an error.

    The payload is the story the ADR describes — the schema moved under a plan
    warmed against the old one — and the test pins the *reason* rather than the
    absence of an exception: `replay_plan` is called directly first, so a payload
    that failed for some other cause (refused by the guard, say, or never cached
    at all) cannot make this pass while the fall-through is broken.
    """
    warm_plan = Plan((ToolStep("query_ledger", {"sql": "SELECT name FROM customers"}),))
    con.execute("ALTER TABLE customers DROP COLUMN name")

    # The motive, asserted: this plan fails replay, and fails it as a ReplayError.
    with pytest.raises(ReplayError):
        replay_plan(warm_plan, con, policy=None, max_rows=100, search_k=1)

    # A fresh cache per entry point, and the reason is a property worth naming:
    # falling through to the LLM *re-warms* the entry with a plan that works, so
    # the cache heals itself after one slow turn. Reusing one cache here would
    # have made entry points two and three assert against a healthy hit —
    # measured, that is exactly what the first version of this test did.
    for entrypoint in ENTRYPOINTS:
        cache, model = make_cache(), _StubModel()
        cache.warm(QUESTION, warm_plan)
        reply = _reply(_agent(model, cache, con), entrypoint)
        assert model.calls == 1, f"{entrypoint}: the LLM was not asked"
        assert reply == LLM_ANSWER, f"{entrypoint}: the visitor did not get the model's answer"


def test_the_two_except_clauses_are_not_interchangeable(make_cache, con, caplog) -> None:
    """`ReplayError` is the contract; the catch-all is the admission.

    Both end in the same `return None`, so without this the pair reads as one
    redundant defence and either could be deleted while the other kept every test
    green. They are distinguished by what they *say*: an abandoned replay is
    expected (INFO), anything else is a defect that happens to be survivable
    (WARNING, with the traceback).
    """
    cache, model = make_cache(), _StubModel()
    agent = _agent(model, cache, con)
    cache.warm(QUESTION, Plan((ToolStep("query_ledger", {"sql": "SELECT name FROM customers"}),)))
    con.execute("ALTER TABLE customers DROP COLUMN name")

    with caplog.at_level(logging.INFO, logger=cached_agent_mod.__name__):
        _reply(agent, "invoke")
    designed = [r for r in caplog.records if r.name == cached_agent_mod.__name__]
    assert designed and all(r.levelno == logging.INFO for r in designed), [
        (r.levelname, r.message) for r in designed
    ]
    assert any("falling through" in r.getMessage() for r in designed)

    # The same turn, failing for a reason no `except` clause could have named.
    caplog.clear()

    class _Novel(Exception):
        pass

    def _boom(*args, **kwargs):
        raise _Novel("something no version of this file anticipated")

    original, cached_agent_mod.replay_plan = cached_agent_mod.replay_plan, _boom
    try:
        with caplog.at_level(logging.INFO, logger=cached_agent_mod.__name__):
            assert _reply(agent, "invoke") == LLM_ANSWER
    finally:
        cached_agent_mod.replay_plan = original
    unexpected = [r for r in caplog.records if r.name == cached_agent_mod.__name__]
    assert unexpected and all(r.levelno == logging.WARNING for r in unexpected)
    assert any(r.exc_info for r in unexpected), "a survivable defect must still leave a traceback"


# --- the seam surface, not a list of seams -------------------------------- #


def _cache_surface() -> set[str]:
    """Every callable `cached_agent` reaches into the plan-cache machinery for.

    Derived from the module's own namespace instead of written down, so a new
    helper imported from `plan_cache` / `plan_replay` — or a new public method on
    `PlanCache` — turns the anchor below red *before* the degradation test can
    quietly stop covering the whole surface. Exceptions and dataclasses are
    dropped: they are vocabulary, not seams that can fail.
    """
    from src.agent import plan_cache, plan_replay

    modules = {plan_cache.__name__, plan_replay.__name__}
    seams: set[str] = set()
    for name, obj in vars(cached_agent_mod).items():
        if name.startswith("_") or getattr(obj, "__module__", None) not in modules:
            continue
        if isinstance(obj, type):
            if issubclass(obj, BaseException) or dataclasses.is_dataclass(obj):
                continue
            seams |= {
                f"{name}.{attr}"
                for attr in dir(obj)
                if not attr.startswith("_") and callable(getattr(obj, attr))
            }
        elif callable(obj):
            seams.add(name)
    return seams


SEAMS = {"PlanCache.lookup", "PlanCache.warm", "plan_from_messages", "replay_plan"}


def test_the_injected_seams_are_the_whole_cache_surface() -> None:
    """The count anchor for the test below — it is only as good as this set."""
    assert _cache_surface() == SEAMS


def _inject(seam: str, raiser):
    """Replace one seam, returning the undo. Patches where the *caller* looks it
    up (the module namespace for functions, the class for methods)."""
    if "." in seam:
        holder = getattr(cached_agent_mod, seam.split(".")[0])
        attr = seam.split(".")[1]
    else:
        holder, attr = cached_agent_mod, seam
    original = getattr(holder, attr)
    setattr(holder, attr, raiser)
    return lambda: setattr(holder, attr, original)


@pytest.mark.parametrize("seam", sorted(SEAMS))
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_every_cache_seam_degrades_to_the_llm(seam, entrypoint, make_cache, con) -> None:
    """Whatever breaks in the cache, the visitor gets a well-formed answer.

    Each seam is exercised twice — against a cold cache (the miss path, which is
    where warming happens) and a warm one (the hit path, which is where replay
    happens) — because a seam that the chosen path never reaches would make this
    pass while proving nothing. `reached` asserts that never happened.

    The assertion is *"one of the two legitimate answers"*, not *"the model's
    answer"*: a broken `warm` on a cache that is already warm is still a genuine
    hit, and demanding the slow path there would be asserting a regression. What
    is never legitimate is a raise (which `_reply` lets through) or an `error`
    event (which `_reply` rejects).
    """
    reached = []
    legitimate = _replayed_seal(con)

    class _SeamFailure(Exception):
        """Deliberately not in any `except` list — that is the property."""

    def _raiser(*args, **kwargs):
        reached.append(seam)
        raise _SeamFailure(seam)

    for warmed in (False, True):
        cache, model = make_cache(), _StubModel()
        if warmed:
            cache.warm(QUESTION, Plan((ToolStep("query_ledger", {"sql": CACHED_SQL}),)))
        agent = _agent(model, cache, con)
        undo = _inject(seam, _raiser)
        try:
            reply = _reply(agent, entrypoint)
        finally:
            undo()
        where = f"{seam} on a {'warm' if warmed else 'cold'} cache"
        assert reply == LLM_ANSWER or legitimate in reply, f"{where}: got {reply!r}"
        assert (model.calls == 1) == (reply == LLM_ANSWER), f"{where}: answer/caller mismatch"

    assert reached, f"{seam} was never called — this test proved nothing"


def test_warming_failure_does_not_cost_an_answer_already_produced(make_cache, con) -> None:
    """The discriminating payload for the warm clause: a real tool call the plan
    extractor cannot serialize, rather than an injected exception.

    Warming runs *after* the model answered, so this is the one cache failure that
    destroys work already paid for — the visitor waited out the tiny model and
    would have received a traceback instead of the reply it produced.
    """
    cache = make_cache()

    class _UnserializableModel(_StubModel):
        def _turn(self, payload: dict) -> dict:
            turn = super()._turn(payload)
            turn["messages"][-2].tool_calls[0]["args"] = {"sql": CACHED_SQL, "opts": {1, 2}}
            return turn

    model = _UnserializableModel()
    with pytest.raises(TypeError):  # the motive: it is json.dumps that fails
        json.dumps({"opts": {1, 2}})

    assert _reply(_agent(model, cache, con), "invoke") == LLM_ANSWER
    assert model.calls == 1


# --- Plan.from_json is total ---------------------------------------------- #


def _payload_grid() -> list[str]:
    """Stored documents that are not `to_json` output, generated rather than listed.

    A hand-written list is the shape of this defect, not its fix: the original
    code was written against `JSONDecodeError` and `KeyError` and was broken by
    `TypeError` and `AttributeError`, both of which a list of "the corruptions I
    thought of" would also have missed. The grid crosses every JSON type with
    every position it can appear in.
    """
    good_step = {"tool": "query_ledger", "args": {"sql": CACHED_SQL}}
    not_a_list = ["null", "true", "3", '"a string"', "{}"]
    not_an_object = ["null", "true", "3", '"a string"', "[]"]
    # Per key, the values of the *wrong type*. `"x"` is absent from the `tool`
    # row on purpose: an unknown tool name is a perfectly readable plan. Whether
    # a tool can be replayed is `REPLAYABLE_TOOLS`' question, one layer up —
    # pinned by `test_an_unknown_tool_name_is_readable_not_malformed` below.
    wrong_type = {
        "tool": ["null", "3", "[]", "{}", "true"],
        "args": ["null", "3", '"x"', "[]", "true"],
    }

    payloads = ["", "{not json", "[", "[{", '{"tool": "query_ledger"}']
    payloads += not_a_list  # a document that is not a list of steps at all
    payloads += [f"[{s}]" for s in not_an_object]  # a step that is not an object
    payloads.append("[{}]")  # an object with neither key
    for key, wrongs in wrong_type.items():
        broken = {k: v for k, v in good_step.items() if k != key}
        payloads.append(json.dumps([broken]))  # a missing key
        payloads += [json.dumps([{**good_step, key: json.loads(w)}]) for w in wrongs]
        # ...and the same corruption in second position, after a valid step — a
        # scan that stopped at the first step would go green on these.
        payloads += [
            json.dumps([good_step, {**good_step, key: json.loads(w)}]) for w in wrongs
        ]
    return payloads


def test_from_json_returns_a_plan_or_a_plan_format_error_and_nothing_else() -> None:
    """Totality is the property, not "these N corruptions are handled"."""
    for raw in _payload_grid():
        try:
            Plan.from_json(raw)
        except PlanFormatError:
            continue
        except Exception as exc:  # a bare `except` is the point: what leaks is the test
            pytest.fail(f"{raw!r} leaked {type(exc).__name__}: {exc}")


def test_the_grid_would_notice_if_from_json_stopped_rejecting() -> None:
    """The control: the grid is only worth something if it is mostly rejections.

    Without this an empty (or accidentally all-valid) grid would make the totality
    test above pass by testing nothing.
    """
    rejected = 0
    for raw in _payload_grid():
        try:
            Plan.from_json(raw)
        except PlanFormatError:
            rejected += 1
    assert rejected == len(_payload_grid()) >= 35, f"{rejected} of {len(_payload_grid())}"


def test_from_json_survives_the_whole_failure_surface_of_json_loads() -> None:
    """The grid above is a grid of *documents*; this is the parser's own surface.

    Found by a blind adversarial pass on 2026-08-01, against a version whose first
    clause was `except json.JSONDecodeError`. Measured on the installed CPython:
    `json.loads` answers a syntax error with `JSONDecodeError`, a number past the
    4300-digit conversion limit with a **bare `ValueError`**, and deep nesting with
    a **`RecursionError`** — which is a `RuntimeError`, not a `ValueError` at all.
    Neither reached `_plan_at`'s `except PlanFormatError`, because that class
    *subclasses* `ValueError` instead of the other way round, so a real
    `ValueError` sails straight past it and out of `lookup`.

    The payloads are built from the limits themselves rather than pasted, so a
    CPython that moves either limit still produces a document that trips it.
    """
    import sys

    beyond_digits = "9" * (sys.get_int_max_str_digits() + 1)
    beyond_stack = "[" * (sys.getrecursionlimit() * 100)
    surface = {
        "syntax": "{not json",
        "digit limit": f'[{{"tool": "q", "args": {{"n": {beyond_digits}}}}}]',
        "nesting": beyond_stack + "]" * (sys.getrecursionlimit() * 100),
    }
    for label, raw in surface.items():
        # The motive: each payload must break `json.loads` itself, and the three
        # must break it in *different* ways — otherwise this is one test wearing
        # three hats.
        # Broad on purpose — the *type* is what this records, so naming it here
        # would presuppose the answer the assertion below checks.
        with pytest.raises(Exception) as raw_failure:
            json.loads(raw)
        surface[label] = type(raw_failure.value)

        with pytest.raises(PlanFormatError):
            Plan.from_json(raw)

    assert len(set(surface.values())) == 3, f"payloads collapsed onto {surface}"


def test_an_unknown_tool_name_is_readable_not_malformed() -> None:
    """Where the layer ends, stated rather than assumed.

    `from_json` answers "is this a plan"; it does not answer "can we run it".
    Making it reject unknown tools would move a policy decision into the parser
    and hide it from `REPLAYABLE_TOOLS` — and an empty plan is likewise readable,
    refused later by `replay_plan` with the `ReplayError` the first test owns.
    """
    assert Plan.from_json('[{"tool": "shell", "args": {}}]').steps[0].tool == "shell"
    assert Plan.from_json("[]") == Plan(steps=())


def test_a_real_plan_still_round_trips() -> None:
    """The other side of totality — the check must not reject what we write."""
    plan = Plan(
        (
            ToolStep("query_ledger", {"sql": CACHED_SQL}),
            ToolStep("search_policy", {"query": "credit hold rule threshold"}),
        )
    )
    assert Plan.from_json(plan.to_json()) == plan


# --- an unreadable stored plan, end to end -------------------------------- #


def _store_raw(cache: PlanCache, question: str, raw_plan: str) -> None:
    """Write a plan document the current code cannot read — what a persistent
    cache written by an older version of this file looks like."""
    cache._collection.upsert(
        ids=[question], documents=[question], metadatas=[{"plan": raw_plan}]
    )


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_an_unreadable_stored_plan_is_a_miss_on_every_entry_point(
    entrypoint, make_cache, con
) -> None:
    """Measured before the fix (2026-08-01): each of these raised out of all three
    entry points. `astream` included — its own `except Exception` never covered
    this, because the cache lookup happens before that `try`."""
    for raw in ('{"legacy": "shape"}', '[{"tool": "query_ledger"}]', '"query_ledger"'):
        cache, model = make_cache(), _StubModel()
        _store_raw(cache, QUESTION, raw)
        assert _reply(_agent(model, cache, con), entrypoint) == LLM_ANSWER, raw
        assert model.calls == 1, raw


def test_lookup_reports_an_unreadable_plan_as_a_miss(make_cache, caplog) -> None:
    """The lower layer owns its own half.

    `CachedAgent`'s catch-all would hide a `lookup` that raised, so the two are
    tested where they live: this asserts `lookup` *returns* a miss, which the
    backstop cannot fake.

    The log is asserted, not decoration. Degrading quietly is the right thing to
    do *for the turn* and the wrong thing to do forever: an entry that no version
    of this code can read stays unreadable, so the demo is permanently slower for
    that question with nothing anywhere saying why.
    """
    import src.agent.plan_cache as plan_cache_mod

    cache = make_cache()
    _store_raw(cache, QUESTION, '[{"tool": "query_ledger"}]')
    with caplog.at_level(logging.WARNING, logger=plan_cache_mod.__name__):
        assert cache.lookup(QUESTION) is None
    assert [r for r in caplog.records if r.name == plan_cache_mod.__name__], (
        "an unreadable stored plan degraded silently"
    )


def test_lookup_is_a_miss_for_every_shape_a_metadata_row_can_take() -> None:
    """`_plan_at` reads a structure it did not build, so "unreadable" has to cover
    the *shape* too, not just the plan string inside it.

    Found by a claim audit on 2026-08-01. Nine of ten shapes already missed; a
    truthy **non-mapping** row entry (a list) slipped past the falsy check and
    died on `.get`. It is outside ChromaDB's declared contract — which is the
    point: this method exists so that the turn does not depend on that contract
    holding. `CachedAgent`'s backstop would have swallowed it, which is exactly
    how a hole like this stays invisible.
    """

    class _Collection:
        def __init__(self, row):
            self._row = row

        def count(self):
            return 1

        def query(self, query_texts, n_results):
            return {"ids": [["q"]], "distances": [[0.0]], "metadatas": [self._row]}

    shapes = [
        None,                       # the whole field absent
        [None],                     # a row of nothing
        [{}],                       # no `plan` key
        [{"plan": None}],           # a `plan` that is not a string
        [{"plan": ""}],             # an empty one
        [{"plan": "{not json"}],    # unparseable
        [{"plan": "[{}]"}],         # parseable, wrong shape
        [["notadict"]],             # a truthy non-mapping — the one that raised
        [42],                       # a scalar row entry
        [],                         # a row shorter than the id list
    ]
    for shape in shapes:
        cache = PlanCache(_Collection(shape), similarity_threshold=0.5)
        assert cache.lookup("anything") is None, f"shape {shape!r} did not miss"

    # The control: the same harness with a *readable* plan is a hit, so the ten
    # misses above are the shapes talking and not a broken stub.
    good = Plan((ToolStep("query_ledger", {"sql": CACHED_SQL}),))
    hit = PlanCache(_Collection([{"plan": good.to_json()}]), similarity_threshold=0.5)
    assert hit.lookup("anything") == good


def test_an_unreadable_neighbour_counts_as_a_rival(make_cache) -> None:
    """Failing open is the failure mode of a guard nobody watches.

    The ambiguity check added on 2026-08-01 asks whether a neighbour inside the
    margin holds a *different* plan. Spelled `rival is not None and rival != plan`,
    a neighbour whose plan cannot be read answered "not different" — so a single
    unreadable entry switched the check off for its whole neighbourhood, in the
    one situation where the cache is least trustworthy. The rule is now positive:
    every neighbour inside the margin must be demonstrably the *same* plan.
    """
    cache = get_plan_cache(
        chromadb.EphemeralClient(),
        "degr_rival",
        DeterministicEmbeddingFunction(),
        similarity_threshold=0.5,
        ambiguity_margin=0.20,
    )
    plan = Plan((ToolStep("query_ledger", {"sql": CACHED_SQL}),))
    cache.warm("how many customers are in the ledger", plan)
    _store_raw(cache, "how many invoices are in the ledger", '{"legacy": "shape"}')

    # Control: the same neighbourhood with a *readable* copy of the same plan is
    # a hit — so the miss below is the unreadable entry talking, not the geometry.
    twin = get_plan_cache(
        chromadb.EphemeralClient(),
        "degr_rival_twin",
        DeterministicEmbeddingFunction(),
        similarity_threshold=0.5,
        ambiguity_margin=0.20,
    )
    twin.warm("how many customers are in the ledger", plan)
    twin.warm("how many invoices are in the ledger", plan)
    assert twin.lookup("how many are in the ledger") is not None

    assert cache.lookup("how many are in the ledger") is None


def test_a_scan_that_ran_out_of_neighbours_is_a_miss(monkeypatch) -> None:
    """A stopped scan is not a finished one.

    `_NEIGHBOURS_SCANNED` is a **cost** ceiling, and until 2026-08-01 it was
    quietly answering a **correctness** question: when every neighbour fetched was
    inside the margin, the loop simply ran out of rows and the winner was served —
    so a genuine rival one row past the ceiling was never looked at. Found by a
    blind adversarial pass, which built 32 near-duplicates of one plan and hid a
    different plan at rank 32.

    The cap is patched down rather than reproduced at 32: what is under test is
    the bookkeeping (`did the scan leave the window, or just stop?`), and a test
    that needs 33 stored questions to say so is measuring the constant instead.
    """
    import src.agent.plan_cache as plan_cache_mod

    def _cache(name: str) -> PlanCache:
        cache = get_plan_cache(
            chromadb.EphemeralClient(),
            name,
            DeterministicEmbeddingFunction(),
            similarity_threshold=0.5,
            ambiguity_margin=1.0,  # everything stored is inside the window
        )
        plan = Plan((ToolStep("query_ledger", {"sql": CACHED_SQL}),))
        for suffix in ("alpha", "beta", "gamma"):
            cache.warm(f"how many customers are in the ledger {suffix}", plan)
        return cache

    question = "how many customers are in the ledger"

    # Control first: with the whole collection fetched, every neighbour *is*
    # demonstrably the same plan, so this is a hit. Without this the assertion
    # below would also pass if the margin alone were rejecting everything.
    assert _cache("scan_full").lookup(question) is not None

    monkeypatch.setattr(plan_cache_mod, "_NEIGHBOURS_SCANNED", 2)
    assert _cache("scan_capped").lookup(question) is None


# --- the two holes a blind pass found in this very fix --------------------- #


def test_one_question_is_one_row_however_it_is_spelled(make_cache) -> None:
    """`warm` keys rows by the question **key**, the same owner the shortcut uses.

    Found by a blind pass on 2026-08-01, and it is the ADR-014 defect one layer
    up: the normalisation lived in the comparison while storage used the raw
    text, so two spellings of one question were two rows for `warm` and one
    question for `_same_question`. Two rows a trailing space apart sit at
    distance 0.0000 — no embedder can separate them — so the shortcut fired on
    whichever ChromaDB ranked first and the ambiguity check never ran. The
    consequence is not a slow answer: it is a *different plan*, served under the
    "the numbers are current" seal.
    """
    cache = make_cache()
    first = Plan((ToolStep("query_ledger", {"sql": "SELECT 1 AS a FROM customers"}),))
    second = Plan((ToolStep("query_ledger", {"sql": "SELECT 2 AS b FROM customers"}),))

    cache.warm("What is total outstanding?", first)
    cache.warm("What is total outstanding? ", second)   # trailing space
    cache.warm("what is TOTAL outstanding?", second)    # and case

    assert cache._collection.count() == 1, "spellings of one question became several rows"
    # The surviving row is the last write, and every spelling reaches it.
    for spelling in (
        "What is total outstanding?",
        "What is total outstanding? ",
        "what is TOTAL outstanding?",
        "  What is   total outstanding?  ",
    ):
        hit = cache.lookup(spelling)
        assert hit == second, f"{spelling!r} did not reach the one stored plan"


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
def test_a_message_that_is_not_a_dict_degrades_instead_of_raising(
    entrypoint, make_cache, con
) -> None:
    """The first statement of the cache path was outside its own `try`.

    Found by a blind pass on 2026-08-01 attacking the guarantee this file exists
    to own — which is the sharpest version of the lesson: the claim said *no*
    failure of the cache path reaches the caller, and the very first line of that
    path was unprotected. A LangGraph-native message object has no `.get`, the
    unwrapped agent handles it fine, and the wrapper turned the turn into an
    `AttributeError` — on `astream`, into a stream that yields **nothing at all**,
    not even an `error` event.
    """

    class _NativeMessage:
        """No `.get` — the shape `_last_user_message` assumes it will never see."""

        def __init__(self, content: str) -> None:
            self.content = content
            self.type = "human"

    model = _StubModel()
    agent = _agent(model, make_cache(), con)
    payload = {"messages": [_NativeMessage(QUESTION)]}

    if entrypoint == "invoke":
        reply = _final_of(agent.invoke(payload)["messages"])
    elif entrypoint == "ainvoke":
        reply = _final_of(asyncio.run(agent.ainvoke(payload))["messages"])
    else:

        async def _collect():
            return [event async for event in agent.astream(payload)]

        events = asyncio.run(_collect())
        assert events, "the stream yielded nothing — not even an error event"
        assert events[-1]["type"] == "answer", f"stream ended on {events[-1]!r}"
        reply = events[-1]["reply"]

    assert reply == LLM_ANSWER
    assert model.calls == 1


def test_a_legacy_duplicate_row_is_ambiguity_not_a_coin_toss() -> None:
    """The writer invariant is enforced; the reader stops assuming it.

    `warm` now keys by `_question_key`, so one question is one row — but the
    collection is persisted and the demo image ships a seeded one, so a
    collection written before that change can hold two rows for one question at
    distance 0.0000 apart. The blind pass built exactly that state and watched
    the shortcut resolve it by rank. Two rows, two different plans, and the
    answer decided by whichever ChromaDB returned first.
    """

    class _Legacy:
        """A collection as an older version of `warm` would have left it."""

        def count(self):
            return 2

        def query(self, query_texts, n_results):
            return {
                "ids": [["What is total outstanding? ", "What is total outstanding?"]],
                "distances": [[0.0, 0.0]],
                "metadatas": [
                    [
                        {"plan": Plan((ToolStep("query_ledger", {"sql": "SELECT 1 AS a"}),)).to_json()},
                        {"plan": Plan((ToolStep("query_ledger", {"sql": "SELECT 2 AS b"}),)).to_json()},
                    ]
                ],
            }

    assert PlanCache(_Legacy(), similarity_threshold=0.5).lookup("What is total outstanding?") is None

    class _LegacyAgreeing(_Legacy):
        """The control: the same duplicate rows holding the *same* plan are not a
        conflict, so this must still be a hit — otherwise the check above would
        just be the shortcut switched off."""

        def query(self, query_texts, n_results):
            result = super().query(query_texts, n_results)
            result["metadatas"][0][1] = dict(result["metadatas"][0][0])
            return result

    hit = PlanCache(_LegacyAgreeing(), similarity_threshold=0.5).lookup("What is total outstanding?")
    assert hit is not None and hit.steps[0].args["sql"] == "SELECT 1 AS a"

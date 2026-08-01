"""Offline tests for the tiny-model turn control (Phase 8, ADR-014).

Covers the three seams that make a ceiling-hitting tiny model degrade gracefully:
dedup / redundant-call short-circuit (8.2), forced finalization (8.1 + 8.3) and
progress narration + the wall-clock budget (8.5, exercised end-to-end through
``CachedAgent``). No LLM, no network — pure functions over a tracker and JSON tool
results.

Both gaps measured on 2026-07-31 are closed here (2026-08-01): turn isolation is
now exercised *concurrently* (a module global fails it), and the dedup key of the
SQL tool is exercised through the tool `tools.py` actually builds — the wiring,
not the helper — so a `query_ledger` that went back to a text key would be red.
"""

from __future__ import annotations

import asyncio
import json

from src.agent.sql_guard import statement_identity
from src.agent.turn_control import (
    Observation,
    ToolCallTracker,
    finalize_answer,
    narrate_end,
    narrate_start,
)

# --- Dedup / redundant-call short-circuit (8.2) -------------------------- #


def test_wrap_is_passthrough_outside_a_turn() -> None:
    """Without ``begin`` the wrapper doesn't touch the tool (keeps tools testable)."""
    tracker = ToolCallTracker()
    calls: list[str] = []

    def tool(sql: str) -> str:
        calls.append(sql)
        return json.dumps({"row_count": 1})

    wrapped = tracker.wrap("query_ledger", tool)
    assert json.loads(wrapped(sql="SELECT 1")) == {"row_count": 1}
    assert calls == ["SELECT 1"]  # ran once, no recording
    assert tracker.observations() == []


def test_dedup_short_circuits_identical_call() -> None:
    """A repeated identical call is served from the memo (not re-run) with a nudge."""
    tracker = ToolCallTracker()
    tracker.begin()
    runs: list[str] = []

    def tool(sql: str) -> str:
        runs.append(sql)
        return json.dumps({"columns": ["n"], "row_count": 1, "rows": [{"n": 5}]})

    wrapped = tracker.wrap("query_ledger", tool)

    first = wrapped(sql="SELECT count(*) FROM invoices")
    # Cosmetically different (whitespace) but the SAME call → must dedup.
    second = wrapped(sql="SELECT   count(*)   FROM invoices")

    assert len(runs) == 1  # the second call was NOT executed
    assert "note" not in json.loads(first)
    payload = json.loads(second)
    assert "already ran this exact call" in payload["note"].lower()
    assert payload["rows"] == [{"n": 5}]  # prior result preserved
    # Only one observation recorded (the dedup hit isn't a new fact).
    assert len(tracker.observations()) == 1


def test_distinct_calls_are_not_de_duped() -> None:
    """Genuinely different args each run and each get recorded (precision, not recall)."""
    tracker = ToolCallTracker()
    tracker.begin()
    runs: list[str] = []

    def tool(sql: str) -> str:
        runs.append(sql)
        return json.dumps({"row_count": 0, "rows": []})

    wrapped = tracker.wrap("query_ledger", tool)
    wrapped(sql="SELECT 1")
    wrapped(sql="SELECT 2")
    assert len(runs) == 2
    assert len(tracker.observations()) == 2


def test_begin_isolates_turns() -> None:
    """A fresh ``begin`` clears the prior turn's memo and observations."""
    tracker = ToolCallTracker()

    def tool(sql: str) -> str:
        return json.dumps({"rows": []})

    wrapped = tracker.wrap("query_ledger", tool)

    tracker.begin()
    wrapped(sql="SELECT 1")
    assert len(tracker.observations()) == 1

    tracker.begin()  # new turn
    assert tracker.observations() == []


def test_dedup_works_through_a_structured_tool() -> None:
    """The wrapped callable is invoked correctly by LangChain's StructuredTool.

    Guards the integration seam: ``make_*_tool`` builds the StructuredTool from the
    *wrapped* function, so dedup must survive langchain's arg parsing (kwargs).
    """
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    from src.agent.tools import make_query_ledger_tool  # noqa: F401 (import smoke)

    tracker = ToolCallTracker()
    tracker.begin()
    runs: list[str] = []

    def query_ledger(sql: str) -> str:
        runs.append(sql)
        return json.dumps({"columns": ["n"], "row_count": 1, "rows": [{"n": 1}]})

    class _In(BaseModel):
        sql: str = Field(description="SQL")

    tool = StructuredTool.from_function(
        func=tracker.wrap("query_ledger", query_ledger),
        name="query_ledger",
        description="run sql",
        args_schema=_In,
    )
    tool.invoke({"sql": "SELECT 1"})
    tool.invoke({"sql": "SELECT 1"})  # identical → deduped
    assert len(runs) == 1
    assert len(tracker.observations()) == 1


def _built_query_ledger(ledger, tracker: ToolCallTracker):
    """The `query_ledger` tool exactly as `tools.py` builds it, tracker-wrapped.

    These cases go through the real construction site on purpose: the dedup key
    is chosen *there* (`key=_sql_dedup_key`), so a tool rebuilt without it — the
    regression that matters — must turn this red. Calling the key function
    directly would keep passing while the product went back to text.
    """
    from src.agent.tools import make_query_ledger_tool

    return make_query_ledger_tool(ledger, max_rows=100, wrap=tracker.wrap)


def test_sql_dedup_matches_rewrites_of_the_same_statement(ledger) -> None:
    """Indentation, keyword case, a comment and a trailing `;` are not a new query."""
    tracker = ToolCallTracker()
    tracker.begin()
    tool = _built_query_ledger(ledger, tracker)

    first = tool.invoke({"sql": "SELECT name FROM customers WHERE name = 'John  Doe'"})
    second = tool.invoke(
        {"sql": "select   name\n  from customers\n  where name = 'John  Doe' ; -- retry"}
    )

    # `columns` (not an `error`) is what proves the fixture is a real ledger and the
    # statement really ran: an absent relation would come back as `sql_error` and
    # every count below would still line up, measuring nothing.
    assert json.loads(first)["columns"] == ["name"]
    assert "note" not in json.loads(first)
    assert "already ran this exact call" in json.loads(second)["note"].lower()
    assert len(tracker.observations()) == 1  # the rewrite did not re-execute


def test_sql_dedup_separates_queries_that_differ_inside_a_literal(ledger) -> None:
    """Two different literals are two different questions — the 2026-07-31 defect.

    Under the old text key both calls below collapsed to one: the second never ran
    and was handed the first's rows with a "you already ran this exact call" note.
    """
    tracker = ToolCallTracker()
    tracker.begin()
    tool = _built_query_ledger(ledger, tracker)

    tool.invoke({"sql": "SELECT name FROM customers WHERE name = 'John  Doe'"})
    spaces = tool.invoke({"sql": "SELECT name FROM customers WHERE name = 'John Doe'"})
    case = tool.invoke({"sql": "SELECT name FROM customers WHERE name = 'JOHN  DOE'"})

    for reply in (spaces, case):
        payload = json.loads(reply)
        assert payload["columns"] == ["name"]  # it reached the ledger, it did not error
        assert "note" not in payload  # each ran on its own
    assert len(tracker.observations()) == 3


def test_padding_the_guard_strips_cannot_reopen_the_literal_collision(ledger) -> None:
    """A statement that *runs* is never keyed by text — whatever it is padded with.

    The dedup key and the guard have to strip alike, or the fallback branch gets
    taken for a query that executes and the text key's literal collision comes
    back through the crack. Found by attack, not by reading: `\x0b` is whitespace
    to Python and a parse error to DuckDB, so the guard stripped it and ran the
    query while the identity said "no opinion".
    """
    tracker = ToolCallTracker()
    tracker.begin()
    tool = _built_query_ledger(ledger, tracker)

    tool.invoke({"sql": "\x0bSELECT name FROM customers WHERE name = 'John  Doe'"})
    second = json.loads(
        tool.invoke({"sql": "\x0bSELECT name FROM customers WHERE name = 'John Doe'"})
    )

    assert second["columns"] == ["name"]  # it ran
    assert "note" not in second  # it was not answered with the first one's rows
    assert len(tracker.observations()) == 2


def test_sql_dedup_falls_back_to_text_when_the_statement_does_not_parse(ledger) -> None:
    """Unparseable SQL has no tree, so the text key still short-circuits a repeat.

    The fallback is safe because such a statement is refused by the guard and never
    reaches the ledger: merging two of them only spares the model a wasted step.
    """
    tracker = ToolCallTracker()
    tracker.begin()
    tool = _built_query_ledger(ledger, tracker)

    broken = "SELECT name FROM WHERE"
    first = json.loads(tool.invoke({"sql": broken}))
    second = json.loads(tool.invoke({"sql": broken}))

    assert first["error"] == "rejected_by_guardrail"  # refused, not executed
    assert "note" not in first
    assert "already ran this exact call" in second["note"].lower()
    assert len(tracker.observations()) == 1

    # …but "has no tree" is not an identity: a *different* broken statement is a
    # different call, and must get its own rejection rather than the first one's.
    other = json.loads(tool.invoke({"sql": "SELECT FROM nowhere WHERE"}))
    assert "note" not in other
    assert len(tracker.observations()) == 2


def test_a_text_key_cannot_impersonate_a_tree_key(ledger) -> None:
    """The two key namespaces are kept apart, and it is not theoretical.

    A tree key is the parser's JSON for the statement; that JSON, handed back as
    the `sql` argument, does not parse — so it takes the text branch, where it
    would serialize to the exact same bytes. Without the `tree:` / `text:` prefix
    the second call below is deduped and answered with the first's rows.
    """
    tracker = ToolCallTracker()
    tracker.begin()
    tool = _built_query_ledger(ledger, tracker)

    tool.invoke({"sql": "SELECT 1 AS n"})
    impostor = json.loads(tool.invoke({"sql": statement_identity("SELECT 1 AS n")}))

    assert "note" not in impostor
    assert impostor["error"] == "rejected_by_guardrail"  # it is not SQL, and is refused
    assert len(tracker.observations()) == 2


def test_tool_name_is_part_of_the_dedup_key() -> None:
    """Two tools sharing an argument name must not share a memo entry.

    Today the two real tools take differently-named arguments (`sql`, `query`), so
    dropping the name from the key stays green by coincidence of naming. A third
    tool taking `query` would silently be served `search_policy`'s result.
    """
    tracker = ToolCallTracker()
    tracker.begin()
    runs: list[str] = []

    def make(tag: str):
        def tool(query: str) -> str:
            runs.append(tag)
            return json.dumps({"from": tag})

        return tool

    first = tracker.wrap("search_policy", make("policy"))
    second = tracker.wrap("search_notes", make("notes"))

    assert json.loads(first(query="credit holds")) == {"from": "policy"}
    assert json.loads(second(query="credit holds")) == {"from": "notes"}
    assert runs == ["policy", "notes"]  # neither was served the other's memo
    assert len(tracker.observations()) == 2


def test_interleaved_turns_do_not_cross_contaminate() -> None:
    """Two turns running *concurrently* never read each other's state.

    The owner of the module docstring's concurrency guarantee. `begin` installs the
    state in the calling context, and asyncio gives every task a copy of the
    context, so the two turns are isolated even though they share one tracker and
    one wrapped tool. Sequential turns (``test_begin_isolates_turns``) do not prove
    this: a module-global implementation passes that one and fails this one, which
    is exactly how it went unnoticed until 2026-08-01.
    """
    tracker = ToolCallTracker()
    executions = iter(range(100))

    def tool(sql: str) -> str:
        # Each *execution* is stamped, so a turn served from another turn's memo is
        # visible in the result itself and not only in the log.
        return json.dumps({"execution": next(executions), "rows": [{"n": 1}]})

    wrapped = tracker.wrap("query_ledger", tool)

    async def turn(delay: float) -> tuple[dict, list[str]]:
        tracker.begin()
        # Both turns open before either calls a tool — the real shape of a request
        # (the model is awaited between `begin` and the first tool call). This
        # ordering is what exposes a *memo* leak; the await further down exposes an
        # *observation* leak. Under a module global, one implementation detail
        # breaks both.
        await asyncio.sleep(0)
        # Two users asking the *same* question at the same moment: the memo is
        # per-turn, so this must run twice — one turn's memo may never answer the
        # other, and neither may be told it "already ran this call".
        result = wrapped(sql="SELECT count(*) FROM invoices")
        await asyncio.sleep(delay)  # the await is where the other turn interleaves
        return json.loads(result), [obs.result for obs in tracker.observations()]

    async def _both():
        return await asyncio.gather(turn(0.02), turn(0.01))

    (a_result, a_log), (b_result, b_log) = asyncio.run(_both())

    # Both ran (distinct executions) and neither was handed the other's memo.
    assert {a_result["execution"], b_result["execution"]} == {0, 1}
    assert "note" not in a_result and "note" not in b_result
    # Each turn's observation log holds its own call, and only its own.
    assert a_log == [json.dumps(a_result)] and b_log == [json.dumps(b_result)]


# --- Forced finalization (8.1 + 8.3) ------------------------------------- #


def _ledger_obs(rows: list[dict], columns: list[str]) -> Observation:
    result = json.dumps(
        {"columns": columns, "row_count": len(rows), "truncated": False, "rows": rows}
    )
    return Observation(tool="query_ledger", args={"sql": "SELECT ..."}, result=result)


def test_finalize_with_no_observations_is_honest_no_progress() -> None:
    """Nothing gathered → the honest 'couldn't finish' message with a hint."""
    reply = finalize_answer([])
    assert "couldn't finish" in reply.lower()
    assert "example questions" in reply.lower()


def test_finalize_surfaces_partial_ledger_result_and_next_step() -> None:
    """A ceiling hit after a good ledger query surfaces the rows + a narrowed step."""
    obs = [
        _ledger_obs(
            rows=[
                {"customer": "Lang", "overdue": 13.37},
                {"customer": "Velasquez", "overdue": 9.1},
            ],
            columns=["customer", "overdue"],
        )
    ]
    reply = finalize_answer(obs, question="who owes the most?")
    assert "Lang" in reply and "Velasquez" in reply  # the gathered facts surface
    assert "from the ledger" in reply.lower()
    assert "sort" in reply.lower() or "narrow" in reply.lower()  # actionable gap
    # Honest framing: it says it didn't fully finish, not a confident final answer.
    assert "couldn't fully finish" in reply.lower()


def test_finalize_errored_ledger_gets_decomposition_hint() -> None:
    """An errored query is not 'gathered facts', but the attempt is named honestly
    with a concrete decomposition — richer than the bare no-progress floor."""
    bad = Observation(
        tool="query_ledger",
        args={"sql": "SELECT ... QUALIFY row_number() ..."},
        result=json.dumps({"error": "sql_error", "detail": "parse error"}),
    )
    reply = finalize_answer([bad])
    assert "didn't run cleanly" in reply.lower()  # names the attempt
    assert "one group at a time" in reply.lower()  # concrete decomposition
    assert "couldn't finish" not in reply.lower()  # NOT the empty-turn floor


def test_finalize_truly_empty_is_no_progress_floor() -> None:
    """No tool calls at all → the honest no-progress floor (distinct from an attempt)."""
    reply = finalize_answer([])
    assert "couldn't finish" in reply.lower()
    assert "didn't run cleanly" not in reply.lower()


def test_finalize_prefers_the_latest_successful_ledger_query() -> None:
    """When several queries ran, the most recent successful one is surfaced."""
    obs = [
        _ledger_obs(rows=[{"n": 1}], columns=["n"]),
        _ledger_obs(rows=[{"n": 99}], columns=["n"]),
    ]
    reply = finalize_answer(obs)
    assert "99" in reply


def test_finalize_includes_policy_finding() -> None:
    """A policy lookup surfaces with its heading in the finalized answer."""
    policy = Observation(
        tool="search_policy",
        args={"query": "credit hold rule"},
        result=json.dumps(
            {
                "results": [
                    {"heading": "Credit Holds", "text": "Hold at 90 days past due."}
                ]
            }
        ),
    )
    reply = finalize_answer([policy])
    assert "Credit Holds" in reply
    assert "90 days" in reply


# --- Progress narration (8.5) -------------------------------------------- #


def test_narrate_start_lines() -> None:
    assert "ledger" in narrate_start("query_ledger", {"sql": "SELECT 1"}).lower()
    line = narrate_start("search_policy", {"query": "credit holds"})
    assert "policy" in line.lower() and "credit holds" in line
    assert "policy" in narrate_start("search_policy", {}).lower()  # no query → generic
    assert "mystery" in narrate_start("mystery", {}).lower()


def test_narrate_end_lines() -> None:
    ok = json.dumps({"row_count": 3, "rows": [{"n": 1}]})
    assert "3 rows" in narrate_end("query_ledger", ok)
    one = json.dumps({"row_count": 1, "rows": [{"n": 1}]})
    assert "1 row" in narrate_end("query_ledger", one) and "rows" not in narrate_end("query_ledger", one)
    empty = json.dumps({"row_count": 0, "rows": []})
    assert "no matching" in narrate_end("query_ledger", empty).lower()
    err = json.dumps({"error": "rejected_by_guardrail"})
    assert "didn't run" in narrate_end("query_ledger", err).lower()
    pol = json.dumps({"results": [{"heading": "Credit Holds", "text": "…"}]})
    assert "Credit Holds" in narrate_end("search_policy", pol)
    assert narrate_end("query_ledger", "not json")  # degrades, no raise


# --- End-to-end through CachedAgent (invoke + astream ceiling paths) ------ #


def test_cached_agent_invoke_finalizes_on_ceiling() -> None:
    """A looping agent under invoke returns a finalized partial answer, not a raise."""
    from langgraph.errors import GraphRecursionError

    from src.agent.cached_agent import CachedAgent
    from src.agent.message_utils import final_text, tools_used

    tracker = ToolCallTracker()

    def tool(sql: str) -> str:
        return json.dumps(
            {"columns": ["c"], "row_count": 1, "rows": [{"c": "ACME"}]}
        )

    wrapped = tracker.wrap("query_ledger", tool)

    class _LoopingAgent:
        def invoke(self, payload, config=None):
            wrapped(sql="SELECT c FROM customers")  # gathers a fact, then loops
            raise GraphRecursionError("recursion limit reached")

    class _NoCache:  # a cache that never hits (isolate the ceiling path)
        def lookup(self, question):
            return None

    agent = CachedAgent(
        _LoopingAgent(),
        _NoCache(),
        con=None,
        policy=None,
        max_rows=100,
        search_k=1,
        tracker=tracker,
    )
    out = agent.invoke({"messages": [{"role": "user", "content": "who?"}]})
    reply = final_text(out["messages"])
    assert "ACME" in reply  # the partial work is surfaced, not thrown away
    assert tools_used(out["messages"]) == ["query_ledger"]


def test_cached_agent_astream_finalizes_on_ceiling() -> None:
    """The streaming ceiling path yields a finalized answer built from observations."""
    from langgraph.errors import GraphRecursionError

    from src.agent.cached_agent import CachedAgent

    tracker = ToolCallTracker()

    def tool(sql: str) -> str:
        return json.dumps({"columns": ["c"], "row_count": 1, "rows": [{"c": "ACME"}]})

    wrapped = tracker.wrap("query_ledger", tool)

    class _LoopingAgent:
        async def astream_events(self, payload, version, config=None):
            yield {"event": "on_tool_start", "name": "query_ledger"}
            wrapped(sql="SELECT c FROM customers")
            raise GraphRecursionError("recursion limit reached")

    class _NoCache:
        def lookup(self, question):
            return None

    agent = CachedAgent(
        _LoopingAgent(),
        _NoCache(),
        con=None,
        policy=None,
        max_rows=100,
        search_k=1,
        tracker=tracker,
    )

    async def _collect():
        return [
            ev
            async for ev in agent.astream(
                {"messages": [{"role": "user", "content": "who?"}]}
            )
        ]

    events = asyncio.run(_collect())
    answer = events[-1]
    assert answer["type"] == "answer"
    assert "ACME" in answer["reply"]
    assert answer["tools_used"] == ["query_ledger"]


class _NoCache:
    def lookup(self, question):
        return None

    def warm(self, question, plan):  # pragma: no cover - not reached in these tests
        pass


def test_astream_emits_narration_steps_on_a_normal_run() -> None:
    """A completing run streams human intent + finding lines (8.5), then the answer."""
    from types import SimpleNamespace

    from src.agent.cached_agent import CachedAgent

    class _Agent:
        async def astream_events(self, payload, version, config=None):
            yield {
                "event": "on_tool_start",
                "name": "query_ledger",
                "data": {"input": {"sql": "SELECT 1"}},
            }
            yield {
                "event": "on_tool_end",
                "name": "query_ledger",
                "data": {"output": json.dumps({"row_count": 3, "rows": [{"n": 1}]})},
            }
            yield {
                "event": "on_chain_end",
                "data": {
                    "output": {
                        "messages": [SimpleNamespace(content="the answer", tool_calls=[])]
                    }
                },
            }

    agent = CachedAgent(
        _Agent(), _NoCache(), con=None, policy=None, max_rows=100, search_k=1
    )

    async def _collect():
        return [
            ev
            async for ev in agent.astream(
                {"messages": [{"role": "user", "content": "how many?"}]}
            )
        ]

    events = asyncio.run(_collect())
    steps = [e["text"] for e in events if e["type"] == "step"]
    assert any("ledger" in s.lower() for s in steps)  # intent line
    assert any("3 row" in s for s in steps)  # finding line
    assert events[-1] == {
        "type": "answer",
        "reply": "the answer",
        "tools_used": [],
    }


def test_astream_wall_clock_budget_finalizes_early() -> None:
    """Past the soft wall-clock budget the run narrates a wrap-up + finalizes (8.5)."""
    from src.agent.cached_agent import CachedAgent

    tracker = ToolCallTracker()

    def tool(sql: str) -> str:
        return json.dumps({"columns": ["c"], "row_count": 1, "rows": [{"c": "ACME"}]})

    wrapped = tracker.wrap("query_ledger", tool)

    class _SlowAgent:
        async def astream_events(self, payload, version, config=None):
            wrapped(sql="SELECT c FROM customers")  # gather a fact
            yield {
                "event": "on_tool_start",
                "name": "query_ledger",
                "data": {"input": {"sql": "SELECT c FROM customers"}},
            }
            # A second event would come, but the budget check fires first.
            yield {"event": "on_tool_start", "name": "query_ledger", "data": {}}

    # Injected clock: deadline computed at t=0, then every check is far past it.
    clock = iter([0.0] + [100.0] * 20)

    agent = CachedAgent(
        _SlowAgent(),
        _NoCache(),
        con=None,
        policy=None,
        max_rows=100,
        search_k=1,
        wall_clock_budget_s=45.0,
        tracker=tracker,
        clock=lambda: next(clock),
    )

    async def _collect():
        return [
            ev
            async for ev in agent.astream(
                {"messages": [{"role": "user", "content": "who?"}]}
            )
        ]

    events = asyncio.run(_collect())
    assert any(
        e["type"] == "step" and "wrapping up" in e["text"].lower() for e in events
    )
    answer = events[-1]
    assert answer["type"] == "answer"
    assert "ACME" in answer["reply"]  # finalized from the gathered fact

"""Offline tests for grammar-constrained tool-calling (ADR-011).

A fake base model returns canned JSON and records what it was called with, so we
exercise schema-building, the JSON protocol appended to the prompt, the
JSON→tool_calls translation, the final-answer / malformed fallbacks, the async
twin of the generate path, and the tier gating — with no Ollama and no network.

The grammar guarantees the *shape* of the reply; the system nudge is what carries
the *meaning* (which tool takes which field, when to stop, don't invent numbers).
Both are asserted here against a spelled-out expectation — never against the code
that produced them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from src.agent.constrained import (
    FINAL_ANSWER,
    ConstrainedToolModel,
    _parse,
    build_schema,
)
from src.agent.providers import should_constrain
from src.core.config import Settings
from src.core.hardware import catalog_quality

# --- fixtures ---------------------------------------------------------------


def _ledger(sql: str) -> str:
    return "ledger"


def _policy(query: str) -> str:
    return "policy"


TOOLS = [
    StructuredTool.from_function(_ledger, name="query_ledger", description="run SQL"),
    StructuredTool.from_function(_policy, name="search_policy", description="find policy"),
]

# Every tool the model may pick, with the field it takes — written out, not derived
# from `_tool_fields`. A test that builds its expectation with the code under test
# agrees with any change to that code (see EXPECTED_SCHEMA below).
TOOL_FIELDS = {"query_ledger": "sql", "search_policy": "query"}

# The grammar the tiny model is constrained to, spelled out. Comparing against
# `build_schema(TOOLS)` is a circular oracle: the same function sits on both sides
# of `==`, so it stays true through any edit to the schema.
EXPECTED_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "enum": ["query_ledger", "search_policy", "final_answer"]},
        "sql": {"type": "string"},
        "query": {"type": "string"},
        "answer": {"type": "string"},
    },
    "required": ["tool"],
}


class FakeBase(BaseChatModel):
    """Returns a fixed content string; records the `format` and the messages it saw."""

    response: str = "{}"
    _seen_format: Any = PrivateAttr(default=None)
    _seen_messages: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._seen_format = kwargs.get("format")
        self._seen_messages = list(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


def _system_nudge(base: FakeBase) -> str:
    """The trailing system message the wrapper appends — the JSON protocol."""
    seen = base._seen_messages
    assert seen, "the base model was never called"
    last = seen[-1]
    assert isinstance(last, SystemMessage), (
        f"the protocol must be the LAST message the model reads, got {type(last).__name__}"
    )
    return last.content


# --- schema -----------------------------------------------------------------


def test_schema_lists_tools_plus_final_answer_and_fields():
    # Whole-surface equality, not four `in` checks: it also pins the field *types*,
    # the enum order (`final_answer` last, after the real tools) and that nothing
    # extra leaks into the grammar.
    assert build_schema(TOOLS) == EXPECTED_SCHEMA
    assert EXPECTED_SCHEMA["properties"]["tool"]["enum"][-1] == FINAL_ANSWER


# --- parsing ----------------------------------------------------------------


def test_parse_tool_call_becomes_tool_calls():
    msg = _parse('{"tool": "query_ledger", "sql": "SELECT 1"}', TOOLS)
    assert msg.tool_calls and msg.tool_calls[0]["name"] == "query_ledger"
    assert msg.tool_calls[0]["args"] == {"sql": "SELECT 1"}
    assert msg.content == ""


def test_parse_drops_empty_and_foreign_fields():
    # 'query' belongs to the other tool and 'sql' is empty → no args survive.
    msg = _parse('{"tool": "query_ledger", "sql": "", "query": "x"}', TOOLS)
    assert msg.tool_calls[0]["args"] == {}


def test_parse_final_answer_ends_the_loop():
    msg = _parse('{"tool": "final_answer", "answer": "42 invoices"}', TOOLS)
    assert msg.tool_calls == []
    assert msg.content == "42 invoices"


def test_parse_unknown_tool_falls_back_to_text():
    msg = _parse('{"tool": "rm_rf", "answer": "nope"}', TOOLS)
    assert msg.tool_calls == []
    assert msg.content == "nope"


def test_parse_malformed_json_degrades_to_content():
    msg = _parse("not json at all", TOOLS)
    assert msg.tool_calls == []
    assert msg.content == "not json at all"


# --- the model wrapper ------------------------------------------------------


def test_bind_tools_then_invoke_emits_tool_call_and_uses_format():
    base = FakeBase(response='{"tool": "search_policy", "query": "credit holds"}')
    model = ConstrainedToolModel(base=base).bind_tools(TOOLS)
    out = model.invoke([("human", "what is the credit hold rule?")])
    assert out.tool_calls[0]["name"] == "search_policy"
    assert out.tool_calls[0]["args"] == {"query": "credit holds"}
    # the schema reached the base as `format`, and it is the *right* schema
    assert base._seen_format == EXPECTED_SCHEMA


def test_invoke_appends_the_json_protocol_after_the_caller_messages():
    """The grammar fixes the shape; this system nudge is what fixes the meaning.

    Without it the model still emits valid JSON and picks badly — so nothing here
    can be left to the schema assertions.
    """
    base = FakeBase(response='{"tool": "final_answer", "answer": "42 invoices"}')
    model = ConstrainedToolModel(base=base).bind_tools(TOOLS)
    model.invoke([("human", "how many invoices are overdue?")])

    # the caller's messages survive, in order, and the nudge trails them
    assert len(base._seen_messages) == 2  # the human turn + the appended nudge
    assert base._seen_messages[0].content == "how many invoices are overdue?"
    hint = _system_nudge(base)

    # every tool is named with the field it takes — the whole menu, not a sample
    for name, field in TOOL_FIELDS.items():
        assert f'tool="{name}"' in hint, f"{name} is missing from the protocol"
        assert f'"{field}"' in hint, f"the {field!r} field of {name} is missing"
    # ...and no phantom option: one line per tool, plus final_answer
    assert hint.count('tool="') == len(TOOL_FIELDS) + 1

    # the half a grammar cannot encode: how to stop, and not to invent numbers
    assert f'tool="{FINAL_ANSWER}"' in hint and '"answer"' in hint
    assert "once the tool results already answer the question" in hint
    assert "Ground every number in a tool result" in hint


def test_ainvoke_sends_the_same_protocol_and_schema_as_invoke():
    """`_agenerate` is a hand-copied twin of `_generate`; pin that it stays a twin.

    Neither side is the oracle here — they check each other, and EXPECTED_SCHEMA
    anchors the pair so they cannot drift together.
    """
    prompt = [("human", "how many invoices are overdue?")]
    sync_base = FakeBase(response='{"tool": "query_ledger", "sql": "SELECT 1"}')
    async_base = FakeBase(response='{"tool": "query_ledger", "sql": "SELECT 1"}')

    sync_out = ConstrainedToolModel(base=sync_base).bind_tools(TOOLS).invoke(prompt)
    async_out = asyncio.run(ConstrainedToolModel(base=async_base).bind_tools(TOOLS).ainvoke(prompt))

    # tool-call ids are uuid4, so compare everything else
    assert [(c["name"], c["args"]) for c in async_out.tool_calls] == [
        (c["name"], c["args"]) for c in sync_out.tool_calls
    ]
    assert async_out.tool_calls[0]["args"] == {"sql": "SELECT 1"}
    assert async_base._seen_format == sync_base._seen_format == EXPECTED_SCHEMA
    assert _system_nudge(async_base) == _system_nudge(sync_base)


def test_invoke_without_bind_tools_raises():
    model = ConstrainedToolModel(base=FakeBase())
    try:
        model.invoke([("human", "hi")])
    except ValueError as e:
        assert "bind_tools" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- tier gating ------------------------------------------------------------


def test_should_constrain_auto_wraps_tiny_not_strong():
    # anchored, not commented: if the catalog re-ranks these tags the test would
    # otherwise keep passing for the wrong reason
    assert (catalog_quality("qwen2.5:1.5b"), catalog_quality("qwen2.5:7b")) == (2, 7)
    s = Settings(constrained_tool_calls="auto", constrained_quality_max=3)
    assert should_constrain("qwen2.5:1.5b", s) is True
    assert should_constrain("qwen2.5:7b", s) is False


def test_should_constrain_is_inclusive_at_the_threshold():
    """`<=` vs `<` is invisible at the default ceiling: the catalog jumps 2 -> 4.

    A numeric threshold only has an owner if some real value lands exactly on it,
    so this test moves the ceiling onto a rank that exists.
    """
    assert catalog_quality("qwen2.5:1.5b") == 2  # sits *on* the ceiling below
    assert catalog_quality("llama3.2:3b") == 4  # the next rank up; 3 does not exist
    s = Settings(constrained_tool_calls="auto", constrained_quality_max=2)
    assert should_constrain("qwen2.5:1.5b", s) is True  # inclusive: `<=`, not `<`
    assert should_constrain("llama3.2:3b", s) is False  # and it really is a ceiling


def test_should_constrain_auto_leaves_unknown_model_native():
    s = Settings(constrained_tool_calls="auto")
    assert should_constrain("some-custom-model", s) is False


def test_should_constrain_explicit_on_off():
    assert should_constrain("qwen2.5:7b", Settings(constrained_tool_calls="on")) is True
    assert should_constrain("qwen2.5:0.5b", Settings(constrained_tool_calls="off")) is False

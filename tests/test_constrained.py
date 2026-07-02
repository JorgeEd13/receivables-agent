"""Offline tests for grammar-constrained tool-calling (ADR-011).

A fake base model returns canned JSON, so we exercise schema-building, the
JSON→tool_calls translation, the final-answer / malformed fallbacks, and the
tier gating — with no Ollama and no network.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import PrivateAttr

from src.agent.constrained import (
    FINAL_ANSWER,
    ConstrainedToolModel,
    build_schema,
    _parse,
)
from src.agent.providers import should_constrain
from src.core.config import Settings


# --- fixtures ---------------------------------------------------------------


def _ledger(sql: str) -> str:
    return "ledger"


def _policy(query: str) -> str:
    return "policy"


TOOLS = [
    StructuredTool.from_function(_ledger, name="query_ledger", description="run SQL"),
    StructuredTool.from_function(_policy, name="search_policy", description="find policy"),
]


class FakeBase(BaseChatModel):
    """Returns a fixed content string; records the `format` it was bound with."""

    response: str = "{}"
    _seen_format: Any = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._seen_format = kwargs.get("format")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


# --- schema -----------------------------------------------------------------


def test_schema_lists_tools_plus_final_answer_and_fields():
    schema = build_schema(TOOLS)
    enum = schema["properties"]["tool"]["enum"]
    assert enum == ["query_ledger", "search_policy", FINAL_ANSWER]
    assert "sql" in schema["properties"] and "query" in schema["properties"]
    assert "answer" in schema["properties"]
    assert schema["required"] == ["tool"]


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
    # the schema was actually passed through to the base as `format`
    assert base._seen_format == build_schema(TOOLS)


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
    s = Settings(constrained_tool_calls="auto", constrained_quality_max=3)
    assert should_constrain("qwen2.5:1.5b", s) is True  # quality 2
    assert should_constrain("qwen2.5:7b", s) is False  # quality 7


def test_should_constrain_auto_leaves_unknown_model_native():
    s = Settings(constrained_tool_calls="auto")
    assert should_constrain("some-custom-model", s) is False


def test_should_constrain_explicit_on_off():
    assert should_constrain("qwen2.5:7b", Settings(constrained_tool_calls="on")) is True
    assert should_constrain("qwen2.5:0.5b", Settings(constrained_tool_calls="off")) is False

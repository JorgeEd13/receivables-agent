"""Grammar-constrained tool-calling for tiny local models (ADR-011).

Small models (`qwen2.5:0.5b`/`1.5b`) are unreliable at **native** tool-calling —
measured here: 0.5b invents fictional argument fields and omits the real one;
1.5b writes correct SQL but as prose and emits *no* tool call at all. Ollama's
native tool-calling channel is the weak link, not JSON syntax.

The fix is to constrain the model's *entire* reply to a JSON schema (Ollama's
``format`` option, a GBNF grammar under the hood) that encodes the agent's whole
decision — pick a tool and fill its argument, **or** emit the final answer — then
translate that JSON back into the ``AIMessage.tool_calls`` the ReAct loop expects.
So ``create_react_agent``, the plan-cache, the SQL guard and the evals all keep
working unchanged; only the local model's *channel* changes.

Measured effect (5 golden questions, structured vs native): both tiny models go
from ≤1/5 to **5/5** valid + routed + filled tool calls. `format` fixes the
*structure*; SQL *correctness* still scales with model size — which is exactly the
"shines with a better model" story, and why this is gated to the tiny tier only
(strong models keep native tool-calling; see ``providers.should_constrain``).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import ConfigDict, PrivateAttr

# The sentinel "tool" the model uses to end the loop with a natural-language reply.
FINAL_ANSWER = "final_answer"
_ANSWER_FIELD = "answer"


def _tool_fields(tool: BaseTool) -> list[str]:
    """The argument field names a tool accepts (e.g. ``["sql"]``)."""
    try:
        return list(tool.args.keys())
    except Exception:  # pragma: no cover - defensive; StructuredTool always has .args
        return []


def build_schema(tools: list[BaseTool]) -> dict:
    """A JSON schema encoding the ReAct decision: one tool + its field, or a final answer.

    ``{"tool": <name|final_answer>, <each tool field>: str, "answer": str}`` with
    only ``tool`` required — the model fills the field matching its chosen tool.
    """
    properties: dict[str, Any] = {
        "tool": {"type": "string", "enum": [t.name for t in tools] + [FINAL_ANSWER]},
    }
    for tool in tools:
        for field in _tool_fields(tool):
            properties[field] = {"type": "string"}
    properties[_ANSWER_FIELD] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": ["tool"]}


def _render_hint(tools: list[BaseTool]) -> str:
    lines = [
        "Reply as JSON only, choosing exactly one `tool`:",
    ]
    for tool in tools:
        fields = _tool_fields(tool)
        field = fields[0] if fields else "(no argument)"
        lines.append(f'- tool="{tool.name}": set "{field}" — {tool.description}')
    lines.append(
        f'- tool="{FINAL_ANSWER}": set "{_ANSWER_FIELD}" to your reply once the tool '
        "results already answer the question. Ground every number in a tool result."
    )
    return "\n".join(lines)


def _parse(content: str, tools: list[BaseTool]) -> AIMessage:
    """Translate the constrained JSON reply into an ``AIMessage``.

    A tool choice → an ``AIMessage`` with a single ``tool_calls`` entry (so the
    ReAct loop runs it); ``final_answer`` (or unparseable output) → a plain
    content message that ends the loop. Unparseable output is returned as-is so
    the loop degrades to a normal answer rather than crashing.
    """
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return AIMessage(content=content or "")
    if not isinstance(obj, dict):
        return AIMessage(content=str(content))

    tool = obj.get("tool")
    if tool == FINAL_ANSWER or tool is None:
        return AIMessage(content=obj.get(_ANSWER_FIELD) or content or "")

    by_name = {t.name: t for t in tools}
    chosen = by_name.get(tool)
    if chosen is None:  # off-menu tool name — treat the JSON as the answer text
        return AIMessage(content=obj.get(_ANSWER_FIELD) or content or "")

    args = {f: obj[f] for f in _tool_fields(chosen) if obj.get(f) not in (None, "")}
    return AIMessage(
        content="",
        tool_calls=[{"name": tool, "args": args, "id": str(uuid.uuid4()), "type": "tool_call"}],
    )


class ConstrainedToolModel(BaseChatModel):
    """Wrap a chat model so its replies are grammar-constrained to the tool schema.

    Behaves like a normal chat model: ``bind_tools`` returns a configured copy,
    and ``invoke``/``ainvoke`` return an ``AIMessage`` with ``tool_calls`` (or a
    final answer). Only the local (tiny) tier is wrapped — see
    ``providers.build_chat_model``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base: BaseChatModel

    _tools: list[BaseTool] = PrivateAttr(default_factory=list)
    _schema: dict | None = PrivateAttr(default=None)
    _hint: str = PrivateAttr(default="")

    @property
    def _llm_type(self) -> str:
        return "constrained-tool-model"

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> ConstrainedToolModel:  # type: ignore[override]
        clone = ConstrainedToolModel(base=self.base)
        clone._tools = list(tools)
        clone._schema = build_schema(clone._tools)
        clone._hint = _render_hint(clone._tools)
        return clone

    def _prepare(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        # Append the JSON protocol as a trailing system nudge (kept out of the
        # shared ReAct prompt so the strong-model path is unaffected).
        return [*messages, SystemMessage(content=self._hint)]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._schema is None:
            raise ValueError("ConstrainedToolModel used without bind_tools()")
        raw = self.base.bind(format=self._schema).invoke(self._prepare(messages))
        message = _parse(raw.content if isinstance(raw.content, str) else str(raw.content), self._tools)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._schema is None:
            raise ValueError("ConstrainedToolModel used without bind_tools()")
        raw = await self.base.bind(format=self._schema).ainvoke(self._prepare(messages))
        message = _parse(raw.content if isinstance(raw.content, str) else str(raw.content), self._tools)
        return ChatResult(generations=[ChatGeneration(message=message)])

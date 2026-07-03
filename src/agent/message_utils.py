"""Read a finished LangGraph turn into the API's answer shape.

Both the HTTP boundary (`src/api/app.py`) and the streaming plan-cache wrapper
(`src/agent/cached_agent.py`) need the same two extractions — the final answer
text and the distinct tool names — so they live here once.
"""

from __future__ import annotations

from typing import Any


def final_text(messages: list[Any]) -> str:
    """The agent's final natural-language answer (last message's content)."""
    if not messages:
        return ""
    content = getattr(messages[-1], "content", messages[-1])
    if isinstance(content, list):  # some providers return content blocks
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def tools_used(messages: list[Any]) -> list[str]:
    """Distinct tool names the agent called this turn, in first-seen order."""
    names: list[str] = []
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and name not in names:
                names.append(name)
    return names

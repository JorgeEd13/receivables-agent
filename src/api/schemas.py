"""Pydantic v2 models for the API boundary.

Every request and response crossing the HTTP edge is a typed model, so FastAPI
validates inputs and documents the contract in the generated OpenAPI schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# A turn in the conversation. Only the two roles the chat UI produces are
# accepted; anything else is rejected at the boundary.
Role = Literal["user", "assistant"]


class Message(BaseModel):
    role: Role
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    """One chat turn. `message` is the new user turn; `history` is the prior
    conversation (oldest first), so the agent has multi-turn context."""

    message: str = Field(min_length=1, max_length=4000)
    history: list[Message] = Field(default_factory=list, max_length=50)


class ChatResponse(BaseModel):
    reply: str
    # Which tools the agent called this turn — a small transparency signal for
    # the UI ("it actually queried the ledger / read the policy").
    tools_used: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    agent_ready: bool

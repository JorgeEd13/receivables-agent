"""The FastAPI application.

Design notes:
- **Build the agent once.** Constructing the agent opens the read-only ledger
  connection and builds the policy index — work we do a single time in an async
  ``lifespan`` and stash on ``app.state``, not per request.
- **API-key auth.** Every data endpoint requires an ``X-API-Key`` header that
  matches ``Settings.app_api_key`` (constant-time compare). ``/api/health`` is
  open so a container orchestrator can probe it.
- **Pydantic v2 at the boundary.** Requests/responses are typed models
  (``src/api/schemas.py``); FastAPI validates and documents them.
- **Injectable agent builder.** ``create_app`` takes an ``agent_builder`` so
  tests can run the whole HTTP stack offline against a stub agent (no LLM).
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from src.agent.graph import build_agent
from src.api.schemas import ChatRequest, ChatResponse, HealthResponse
from src.core.config import Settings, get_settings

# Builds the compiled agent. Swapped in tests for an offline stub.
AgentBuilder = Callable[[Settings], Any]

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
# The bundled React build, served at "/" when present (Docker image / Space).
_WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def _default_agent_builder(settings: Settings) -> Any:
    return build_agent(settings)


def create_app(agent_builder: AgentBuilder = _default_agent_builder) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Build the agent once at startup; reused by every request.
        app.state.agent = agent_builder(settings)
        yield
        app.state.agent = None

    app = FastAPI(
        title="receivables-agent",
        version="0.1.0",
        summary="Conversational AI over synthetic accounts-receivable data.",
        lifespan=lifespan,
    )

    def require_api_key(key: str | None = Security(_API_KEY_HEADER)) -> None:
        # Constant-time compare so a wrong key can't be timed character by char.
        if not key or not secrets.compare_digest(key, settings.app_api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API key.",
            )

    def get_agent(request: Request) -> Any:
        agent = getattr(request.app.state, "agent", None)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Agent is not ready.",
            )
        return agent

    @app.get("/api/health", response_model=HealthResponse, tags=["meta"])
    async def health(request: Request) -> HealthResponse:
        return HealthResponse(agent_ready=getattr(request.app.state, "agent", None) is not None)

    @app.post(
        "/api/chat",
        response_model=ChatResponse,
        tags=["chat"],
        dependencies=[Depends(require_api_key)],
    )
    async def chat(req: ChatRequest, agent: Any = Depends(get_agent)) -> ChatResponse:
        messages = [m.model_dump() for m in req.history]
        messages.append({"role": "user", "content": req.message})

        result = await agent.ainvoke({"messages": messages})
        out = result["messages"]
        return ChatResponse(reply=_final_text(out), tools_used=_tools_used(out))

    # Serve the built React app at the root when it exists (production image).
    # In dev the UI runs on the Vite server and calls /api directly.
    if _WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")

    return app


def _final_text(messages: list[Any]) -> str:
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


def _tools_used(messages: list[Any]) -> list[str]:
    """Distinct tool names the agent called this turn, in first-seen order."""
    names: list[str] = []
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name and name not in names:
                names.append(name)
    return names


# The ASGI entrypoint (uvicorn src.api.app:app).
app = create_app()

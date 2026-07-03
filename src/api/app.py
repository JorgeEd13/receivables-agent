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

import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from src.agent.graph import build_agent
from src.agent.message_utils import final_text as _final_text
from src.agent.message_utils import tools_used as _tools_used
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

    @app.post(
        "/api/chat/stream",
        tags=["chat"],
        dependencies=[Depends(require_api_key)],
    )
    async def chat_stream(req: ChatRequest, agent: Any = Depends(get_agent)) -> StreamingResponse:
        """Server-Sent Events variant of ``/api/chat``.

        Streams the agent's progress so the UI can show it *thinking* — vital on
        the free-CPU Space where a tiny model can take a while. Each event is a
        one-line JSON object (see ``CachedAgent.astream``): ``cached`` /
        ``tool`` / ``answer`` / ``error``. Agents without ``astream`` (e.g. a
        test stub or an un-cached build) degrade to a single ``answer`` event.
        """
        messages = [m.model_dump() for m in req.history]
        messages.append({"role": "user", "content": req.message})

        async def event_source() -> AsyncIterator[str]:
            def sse(obj: dict[str, Any]) -> str:
                return f"data: {json.dumps(obj)}\n\n"

            try:
                if hasattr(agent, "astream"):
                    async for ev in agent.astream({"messages": messages}):
                        yield sse(ev)
                else:  # no streaming support → one shot
                    result = await agent.ainvoke({"messages": messages})
                    out = result["messages"]
                    yield sse({"type": "answer", "reply": _final_text(out), "tools_used": _tools_used(out)})
            except Exception as exc:  # never leave the stream half-open
                yield sse({"type": "error", "message": str(exc)})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Serve the built React app at the root when it exists (production image).
    # In dev the UI runs on the Vite server and calls /api directly.
    if _WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="web")

    return app


# The ASGI entrypoint (uvicorn src.api.app:app).
app = create_app()

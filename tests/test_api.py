"""API tests — the full HTTP stack, offline.

We inject a stub agent into ``create_app`` so the lifespan builds something with
an ``ainvoke`` that returns canned LangGraph-shaped messages. No LLM, no ledger,
no network: this exercises routing, auth, Pydantic validation and the
reply/tools extraction — not the model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.config import get_settings

API_KEY = "test-key"


class _StubAgent:
    """Mimics a compiled LangGraph agent: ``ainvoke`` returns a messages dict.

    Records the last input so tests can assert history is forwarded.
    """

    def __init__(self) -> None:
        self.last_input: dict | None = None

    async def ainvoke(self, payload: dict) -> dict:
        self.last_input = payload
        # An AIMessage that "called" a tool, then a final AIMessage answer.
        tool_turn = SimpleNamespace(
            content="", tool_calls=[{"name": "query_ledger", "args": {}, "id": "1"}]
        )
        final = SimpleNamespace(content="Customer ACME owes $50k.", tool_calls=[])
        return {"messages": payload["messages"] + [tool_turn, final]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_API_KEY", API_KEY)
    get_settings.cache_clear()  # pick up the patched key
    stub = _StubAgent()
    app = create_app(agent_builder=lambda settings: stub)
    with TestClient(app) as c:
        c.stub = stub  # type: ignore[attr-defined]
        yield c
    get_settings.cache_clear()


def test_health_is_open_and_reports_ready(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "agent_ready": True}


def test_chat_requires_api_key(client):
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 401


def test_chat_rejects_wrong_api_key(client):
    resp = client.post(
        "/api/chat", json={"message": "hi"}, headers={"X-API-Key": "nope"}
    )
    assert resp.status_code == 401


def test_chat_happy_path_returns_reply_and_tools(client):
    resp = client.post(
        "/api/chat",
        json={"message": "who owes the most?"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Customer ACME owes $50k."
    assert body["tools_used"] == ["query_ledger"]


def test_chat_forwards_history_then_new_message(client):
    client.post(
        "/api/chat",
        json={
            "message": "and ACME specifically?",
            "history": [
                {"role": "user", "content": "who owes the most?"},
                {"role": "assistant", "content": "ACME."},
            ],
        },
        headers={"X-API-Key": API_KEY},
    )
    sent = client.stub.last_input["messages"]  # type: ignore[attr-defined]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "and ACME specifically?"


def test_chat_validates_empty_message(client):
    resp = client.post(
        "/api/chat", json={"message": ""}, headers={"X-API-Key": API_KEY}
    )
    assert resp.status_code == 422


def test_chat_rejects_unknown_history_role(client):
    resp = client.post(
        "/api/chat",
        json={"message": "hi", "history": [{"role": "system", "content": "x"}]},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422

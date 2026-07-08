"""Tests for the agent API layer (agent_api.py, auth_api.py).

Uses a fake agent that replays canned astream_events output, so no
Azure OpenAI credentials or MCP server are needed.

Run:
    cd agent-client
    uv run --with pytest --with fastapi --with uvicorn --with httpx \
        pytest tests_api/ -q
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_client.agent_api import AgentEvent, AgentService, create_app
from agent_client.auth_api import (
    AuthError,
    NoAuthAuthenticator,
    StaticTokenAuthenticator,
    build_authenticator,
)


# -- Fakes --

def _chunk_event(text):
    """A chat model stream event carrying one answer token."""
    return {
        "event": "on_chat_model_stream",
        "metadata": {},
        "data": {"chunk": SimpleNamespace(content=text, tool_call_chunks=None)},
    }


def _node_start_event(node):
    """A chain start event for a graph node."""
    return {"event": "on_chain_start", "metadata": {"langgraph_node": node}, "data": {}}


class FakeAgent:
    """Replays canned events; mimics the compiled graph's async surface."""

    def __init__(self, events, final_messages=None):
        self.events = events
        self.final_messages = final_messages or []

    async def astream_events(self, inputs, config, version):
        for event in self.events:
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self.final_messages})


def make_service(events, final_messages=None, tool_count=12):
    return AgentService(FakeAgent(events, final_messages), tool_count)


STREAMING_EVENTS = [
    _node_start_event("router"),
    _node_start_event("knowledgebase_agent"),
    _chunk_event("Hello"),
    _chunk_event(" world"),
]


# -- AgentService --

def test_stream_yields_phase_token_answer():
    service = make_service(STREAMING_EVENTS)

    async def collect():
        return [e async for e in service.stream("s1", "hi")]

    events = asyncio.run(collect())
    assert [e.type for e in events] == ["phase", "phase", "token", "token", "answer"]
    assert events[0].data == "Routing"
    assert events[1].data == "Generating answer"
    assert events[-1].data == "Hello world"


def test_stream_ignores_unknown_nodes_and_tool_chunks():
    events = [
        _node_start_event("not_a_real_node"),
        {
            "event": "on_chat_model_stream",
            "metadata": {},
            "data": {"chunk": SimpleNamespace(content="x", tool_call_chunks=[{"name": "t"}])},
        },
        _chunk_event("ok"),
    ]
    service = make_service(events)

    async def collect():
        return [e async for e in service.stream("s1", "hi")]

    collected = asyncio.run(collect())
    assert [e.type for e in collected] == ["token", "answer"]
    assert collected[-1].data == "ok"


def test_stream_falls_back_to_state_when_nothing_streamed():
    final = [SimpleNamespace(content="direct answer")]
    service = make_service([_node_start_event("router")], final_messages=final)

    async def collect():
        return [e async for e in service.stream("s1", "hi")]

    events = asyncio.run(collect())
    assert events[-1].type == "answer"
    assert events[-1].data == "direct answer"


def test_ask_returns_full_answer():
    service = make_service(STREAMING_EVENTS)
    assert asyncio.run(service.ask("s1", "hi")) == "Hello world"


def test_create_session_ids_are_unique():
    service = make_service([])
    assert service.create_session() != service.create_session()


# -- Authenticators --

def test_static_token_accepts_correct_token():
    auth = StaticTokenAuthenticator("secret")
    assert auth.authenticate("secret").subject == "static-client"


@pytest.mark.parametrize("bad", ["wrong", "", "secret2"])
def test_static_token_rejects_bad_tokens(bad):
    auth = StaticTokenAuthenticator("secret")
    with pytest.raises(AuthError):
        auth.authenticate(bad)


def test_no_auth_accepts_anything():
    assert NoAuthAuthenticator().authenticate("").subject == "anonymous"


def test_build_authenticator_static_requires_token(monkeypatch):
    monkeypatch.setenv("AGENT_API_AUTH", "static")
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        build_authenticator()


def test_build_authenticator_modes(monkeypatch):
    monkeypatch.setenv("AGENT_API_AUTH", "static")
    monkeypatch.setenv("AGENT_API_TOKEN", "t")
    assert isinstance(build_authenticator(), StaticTokenAuthenticator)

    monkeypatch.setenv("AGENT_API_AUTH", "none")
    assert isinstance(build_authenticator(), NoAuthAuthenticator)

    monkeypatch.setenv("AGENT_API_AUTH", "entra")
    with pytest.raises(RuntimeError):
        build_authenticator()


# -- API endpoints --

AUTH = {"Authorization": "Bearer secret"}


def make_client(service=None, authenticator=None):
    if service is None:
        service = make_service(STREAMING_EVENTS)
    if authenticator is None:
        authenticator = StaticTokenAuthenticator("secret")
    return TestClient(create_app(service=service, authenticator=authenticator))


def test_health_is_open():
    client = make_client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "tools": 12}


def test_endpoints_require_auth():
    client = make_client()
    assert client.post("/sessions").status_code == 401
    assert client.post(
        "/sessions", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401
    response = client.post("/sessions/s1/messages", json={"message": "hi"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_create_session():
    client = make_client()
    response = client.post("/sessions", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["session_id"]


def test_send_message_buffered():
    client = make_client()
    response = client.post("/sessions/s1/messages", json={"message": "hi"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"answer": "Hello world"}


def test_stream_message_sse():
    client = make_client()
    with client.stream(
        "POST", "/sessions/s1/messages/stream", json={"message": "hi"}, headers=AUTH
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    # events arrive in order and the stream ends with the full answer
    blocks = [b for b in body.strip().split("\n\n") if b]
    types = [b.split("\n")[0].removeprefix("event: ") for b in blocks]
    assert types == ["phase", "phase", "token", "token", "answer"]
    assert '"Hello world"' in blocks[-1]


def test_no_auth_mode_accepts_missing_header():
    client = make_client(authenticator=NoAuthAuthenticator())
    assert client.post("/sessions").status_code == 200

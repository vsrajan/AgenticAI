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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_client.agent_api import (
    AgentEvent,
    AgentService,
    UnknownSessionError,
    create_app,
)
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
    """Replays canned events; mimics the compiled graph's async surface.

    AgentService only calls two methods on the real compiled graph
    (astream_events and aget_state), so a stand-in with those two
    methods is enough to test all the service and API code without an
    LLM or MCP server. This is called duck typing: the object just has
    to quack like a graph.
    """

    def __init__(self, events, final_messages=None):
        self.events = events
        self.final_messages = final_messages or []

    async def astream_events(self, inputs, config, version):
        for event in self.events:
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self.final_messages})


class FakeCheckpointer:
    """Records delete_thread calls so eviction can be asserted."""

    def __init__(self):
        self.deleted = []

    def delete_thread(self, thread_id):
        self.deleted.append(thread_id)


def make_service(events, final_messages=None, tool_count=12, **kwargs):
    return AgentService(FakeAgent(events, final_messages), tool_count, **kwargs)


STREAMING_EVENTS = [
    _node_start_event("router"),
    _node_start_event("knowledgebase_agent"),
    _chunk_event("Hello"),
    _chunk_event(" world"),
]


# -- AgentService --

def test_stream_yields_phase_token_answer():
    service = make_service(STREAMING_EVENTS)
    sid = service.create_session()

    async def collect():
        return [e async for e in service.stream(sid, "hi")]

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
    sid = service.create_session()

    async def collect():
        return [e async for e in service.stream(sid, "hi")]

    collected = asyncio.run(collect())
    assert [e.type for e in collected] == ["token", "answer"]
    assert collected[-1].data == "ok"


def test_stream_falls_back_to_state_when_nothing_streamed():
    final = [SimpleNamespace(content="direct answer")]
    service = make_service([_node_start_event("router")], final_messages=final)
    sid = service.create_session()

    async def collect():
        return [e async for e in service.stream(sid, "hi")]

    events = asyncio.run(collect())
    assert events[-1].type == "answer"
    assert events[-1].data == "direct answer"


def test_ask_returns_full_answer():
    service = make_service(STREAMING_EVENTS)
    sid = service.create_session()
    assert asyncio.run(service.ask(sid, "hi")) == "Hello world"


def test_create_session_ids_are_unique():
    service = make_service([])
    assert service.create_session() != service.create_session()


# -- Session lifecycle --

def test_stream_rejects_unknown_session():
    service = make_service(STREAMING_EVENTS)

    async def run():
        async for _ in service.stream("never-created", "hi"):
            pass

    with pytest.raises(UnknownSessionError):
        asyncio.run(run())


def test_idle_sessions_are_swept():
    checkpointer = FakeCheckpointer()
    service = make_service(
        STREAMING_EVENTS, checkpointer=checkpointer, session_ttl_seconds=100,
    )
    sid = service.create_session()
    # simulate idleness: push the last-used clock past the TTL
    service._sessions[sid].last_used -= 101
    assert service.sweep_expired_sessions() == 1
    assert not service.has_session(sid)
    # the checkpointer thread was deleted too -- that is what frees memory
    assert checkpointer.deleted == [sid]


def test_active_sessions_survive_the_sweep():
    service = make_service(STREAMING_EVENTS, session_ttl_seconds=100)
    sid = service.create_session()
    assert service.sweep_expired_sessions() == 0
    assert service.has_session(sid)


def test_max_sessions_evicts_least_recently_used():
    checkpointer = FakeCheckpointer()
    service = make_service(STREAMING_EVENTS, checkpointer=checkpointer, max_sessions=2)
    s1 = service.create_session()
    s2 = service.create_session()
    service._sessions[s1].last_used -= 10  # s1 is the oldest
    s3 = service.create_session()  # cap reached -> evicts s1
    assert not service.has_session(s1)
    assert service.has_session(s2) and service.has_session(s3)
    assert checkpointer.deleted == [s1]


class SlowFakeAgent(FakeAgent):
    """Tracks how many turns run at once, to prove the per-session lock."""

    def __init__(self, events):
        super().__init__(events)
        self.active = 0
        self.max_active = 0

    async def astream_events(self, inputs, config, version):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)  # give the other turn a chance to overlap
        for event in self.events:
            yield event
        self.active -= 1


def test_concurrent_turns_on_one_session_are_serialised():
    agent = SlowFakeAgent(STREAMING_EVENTS)
    service = AgentService(agent, 12)
    sid = service.create_session()

    async def run():
        await asyncio.gather(service.ask(sid, "a"), service.ask(sid, "b"))

    asyncio.run(run())
    assert agent.max_active == 1  # the lock kept the turns sequential


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


# monkeypatch is a pytest fixture for temporarily changing env vars --
# every change is automatically undone when the test ends, so tests
# cannot leak configuration into each other
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
    # TestClient calls the FastAPI app directly in-process -- no real
    # network socket, but the same request parsing, auth dependency,
    # and response serialization as a live server
    if service is None:
        service = make_service(STREAMING_EVENTS)
    if authenticator is None:
        authenticator = StaticTokenAuthenticator("secret")
    return TestClient(create_app(service=service, authenticator=authenticator))


def new_session(client) -> str:
    """Create a session through the API, as a real client would."""
    return client.post("/sessions", headers=AUTH).json()["session_id"]


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
    sid = new_session(client)
    response = client.post(f"/sessions/{sid}/messages", json={"message": "hi"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"answer": "Hello world"}


def test_unknown_session_returns_404_everywhere():
    client = make_client()
    body = {"message": "hi"}
    assert client.post("/sessions/nope/messages", json=body, headers=AUTH).status_code == 404
    assert client.post("/sessions/nope/messages/stream", json=body, headers=AUTH).status_code == 404
    assert client.get("/sessions/nope/messages", headers=AUTH).status_code == 404


def test_stream_message_sse():
    client = make_client()
    sid = new_session(client)
    with client.stream(
        "POST", f"/sessions/{sid}/messages/stream", json={"message": "hi"}, headers=AUTH
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


# a realistic slice of graph history: the user asks, the specialist
# calls a tool (AIMessage with tool_calls, no visible content), the
# tool answers (ToolMessage), then the specialist gives the final answer
CHAT_HISTORY = [
    HumanMessage(content="hi"),
    AIMessage(content="", tool_calls=[
        {"name": "search_docs", "args": {"query": "hi"}, "id": "t1"},
    ]),
    ToolMessage(content="doc snippet", tool_call_id="t1"),
    AIMessage(content="final answer"),
]


def test_get_messages_returns_chat_view():
    # the chat view hides tool calls and tool results -- only what a
    # chat window would have displayed
    client = make_client(service=make_service([], final_messages=CHAT_HISTORY))
    sid = new_session(client)
    response = client.get(f"/sessions/{sid}/messages", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["messages"] == [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "final answer"},
    ]


def test_get_messages_raw_view_for_debugging():
    client = make_client(service=make_service([], final_messages=CHAT_HISTORY))
    sid = new_session(client)
    response = client.get(f"/sessions/{sid}/messages?raw=true", headers=AUTH)
    messages = response.json()["messages"]
    assert [m["type"] for m in messages] == [
        "HumanMessage", "AIMessage", "ToolMessage", "AIMessage",
    ]
    assert messages[1]["tool_calls"] == ["search_docs"]


def test_get_messages_requires_auth():
    client = make_client(service=make_service([], final_messages=CHAT_HISTORY))
    assert client.get("/sessions/s1/messages").status_code == 401


def test_cors_preflight_allows_browser_clients():
    # before a cross-origin POST with an Authorization header, browsers
    # send an OPTIONS "preflight" request; the CORS middleware must
    # answer it or the browser never sends the real request
    client = make_client()
    response = client.options(
        "/sessions",
        headers={
            "Origin": "null",  # what a page opened from file:// sends
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"

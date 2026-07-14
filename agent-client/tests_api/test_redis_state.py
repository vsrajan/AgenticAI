"""Tests for Redis-backed session state (redis_state.py, P3.1).

Runs against fakeredis -- an in-process fake Redis -- so no server is
needed in CI. The manual two-instance rig against a real redis-server
is described in docs/P3.1.md section 9.

Run:
    cd agent-client
    uv run --with pytest --with httpx --with 'fakeredis[lua]' pytest tests_api/ -q

(the [lua] extra is required: redis-py releases its distributed lock
through a Lua script, which fakeredis executes only with lupa installed)
"""

import asyncio
from types import SimpleNamespace

import pytest
from fakeredis import FakeAsyncRedis
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from ease_clients.agent_api import AgentService, UnknownSessionError, create_app
from ease_clients.redis_state import (
    RedisSaver,
    RedisSessionStore,
    build_redis_state,
)

TTL = 60


# -- helpers --

def make_state(client, ttl=TTL, max_sessions=500, lock_timeout=5):
    saver = RedisSaver(client, ttl)
    store = RedisSessionStore(client, saver, ttl, max_sessions, lock_timeout)
    return store, saver


def make_echo_graph(saver):
    """A minimal real LangGraph whose one node answers 'echo: <input>'.

    Small as it is, running it exercises the checkpointer exactly the
    way the real agent does: reads on turn start, puts per superstep.
    """
    def echo(state):
        last = state["messages"][-1].content
        return {"messages": [AIMessage(content=f"echo: {last}")]}

    graph = StateGraph(MessagesState)
    graph.add_node("echo", echo)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph.compile(checkpointer=saver)


def turn(agent, thread_id, text):
    config = {"configurable": {"thread_id": thread_id}}
    return agent.ainvoke({"messages": [HumanMessage(content=text)]}, config)


# -- RedisSaver: checkpointing through a real graph --

def test_two_turns_accumulate_history():
    async def run():
        client = FakeAsyncRedis()
        _, saver = make_state(client)
        agent = make_echo_graph(saver)
        await turn(agent, "t1", "hello")
        result = await turn(agent, "t1", "again")
        contents = [m.content for m in result["messages"]]
        # turn 2 saw turn 1's messages -- state persisted between turns
        assert contents == ["hello", "echo: hello", "again", "echo: again"]

    asyncio.run(run())


def test_history_survives_a_restart():
    async def run():
        client = FakeAsyncRedis()
        _, saver = make_state(client)
        await turn(make_echo_graph(saver), "t1", "hello")

        # "restart": brand-new saver and graph objects, same Redis
        _, saver2 = make_state(client)
        agent2 = make_echo_graph(saver2)
        state = await agent2.aget_state({"configurable": {"thread_id": "t1"}})
        contents = [m.content for m in state.values["messages"]]
        assert contents == ["hello", "echo: hello"]

        # and the conversation continues where it left off
        result = await turn(agent2, "t1", "again")
        assert len(result["messages"]) == 4

    asyncio.run(run())


def test_threads_are_isolated():
    async def run():
        client = FakeAsyncRedis()
        _, saver = make_state(client)
        agent = make_echo_graph(saver)
        await turn(agent, "t1", "one")
        await turn(agent, "t2", "two")
        state = await agent.aget_state({"configurable": {"thread_id": "t2"}})
        assert [m.content for m in state.values["messages"]] == ["two", "echo: two"]

    asyncio.run(run())


def test_delete_thread_removes_every_key():
    async def run():
        client = FakeAsyncRedis()
        _, saver = make_state(client)
        agent = make_echo_graph(saver)
        await turn(agent, "t1", "hello")
        assert [k async for k in client.scan_iter(match="agnes:*")]

        await saver.adelete_thread("t1")
        assert [k async for k in client.scan_iter(match="agnes:*")] == []
        state = await agent.aget_state({"configurable": {"thread_id": "t1"}})
        assert state.values == {}

    asyncio.run(run())


def test_alist_returns_newest_first_and_respects_limit():
    async def run():
        client = FakeAsyncRedis()
        _, saver = make_state(client)
        agent = make_echo_graph(saver)
        await turn(agent, "t1", "one")
        await turn(agent, "t1", "two")
        config = {"configurable": {"thread_id": "t1"}}
        tuples = [t async for t in saver.alist(config)]
        assert len(tuples) >= 2
        ids = [t.config["configurable"]["checkpoint_id"] for t in tuples]
        assert ids == sorted(ids, reverse=True)
        limited = [t async for t in saver.alist(config, limit=1)]
        assert len(limited) == 1

    asyncio.run(run())


def test_put_writes_round_trip():
    async def run():
        from langgraph.checkpoint.base import empty_checkpoint

        client = FakeAsyncRedis()
        _, saver = make_state(client)
        config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
        ckpt = empty_checkpoint()
        saved = await saver.aput(config, ckpt, {"source": "test"}, {})
        await saver.aput_writes(saved, [("messages", "pending!")], task_id="task-1")
        tup = await saver.aget_tuple(saved)
        assert tup is not None
        assert ("task-1", "messages", "pending!") in tup.pending_writes

    asyncio.run(run())


def test_sync_surface_is_explicitly_unsupported():
    saver = RedisSaver(FakeAsyncRedis(), TTL)
    with pytest.raises(NotImplementedError):
        saver.get_tuple({"configurable": {"thread_id": "t"}})
    with pytest.raises(NotImplementedError):
        saver.delete_thread("t")


# -- RedisSessionStore: registry, TTL, LRU cap --

def test_create_require_touch():
    async def run():
        store, _ = make_state(FakeAsyncRedis())
        sid = await store.create_session()
        assert await store.has_session(sid)
        await store.require(sid)  # refreshes, no error

    asyncio.run(run())


def test_require_unknown_session_raises_keyerror():
    async def run():
        store, _ = make_state(FakeAsyncRedis())
        with pytest.raises(KeyError):
            await store.require("never-created")

    asyncio.run(run())


def test_idle_sessions_expire_via_ttl():
    async def run():
        client = FakeAsyncRedis()
        store, _ = make_state(client, ttl=1)  # 1 second idle TTL
        sid = await store.create_session()
        await asyncio.sleep(1.1)
        assert not await store.has_session(sid)
        with pytest.raises(KeyError):
            await store.require(sid)
        # the stale LRU index entry was lazily cleaned by require()
        assert await client.zscore("agnes:sessions:index", sid) is None

    asyncio.run(run())


def test_lru_cap_evicts_oldest():
    async def run():
        client = FakeAsyncRedis()
        store, _ = make_state(client, max_sessions=2)
        s1 = await store.create_session()
        s2 = await store.create_session()
        await store.touch(s2)  # s1 is now clearly the LRU
        s3 = await store.create_session()
        assert not await store.has_session(s1)
        assert await store.has_session(s2) and await store.has_session(s3)

    asyncio.run(run())


def test_cap_eviction_skips_sessions_mid_turn():
    async def run():
        client = FakeAsyncRedis()
        store, _ = make_state(client, max_sessions=2)
        s1 = await store.create_session()
        s2 = await store.create_session()
        await store.touch(s2)  # s1 = LRU candidate...

        lock = store.lock(s1)
        await lock.__aenter__()  # ...but its turn is running
        try:
            s3 = await store.create_session()
            # the locked LRU candidate survived; the idle one was evicted
            assert await store.has_session(s1) and await store.has_session(s3)
            assert not await store.has_session(s2)
        finally:
            await lock.__aexit__(None, None, None)

    asyncio.run(run())


def test_eviction_deletes_checkpoints_too():
    async def run():
        client = FakeAsyncRedis()
        store, saver = make_state(client)
        sid = await store.create_session()
        await turn(make_echo_graph(saver), sid, "hello")
        await store.evict(sid, reason="test")
        assert [k async for k in client.scan_iter(match=f"agnes:ckpt:{sid}*")] == []

    asyncio.run(run())


# -- the distributed turn lock --

def test_lock_serializes_turns_across_store_instances():
    async def run():
        client = FakeAsyncRedis()
        # two stores over one Redis = two agent-api instances
        store_a, _ = make_state(client)
        store_b, _ = make_state(client)
        order = []

        async def hold_first():
            async with store_a.lock("s1"):
                order.append("a-in")
                await asyncio.sleep(0.2)
                order.append("a-out")

        async def then_second():
            await asyncio.sleep(0.05)  # ensure A grabs the lock first
            async with store_b.lock("s1"):
                order.append("b-in")

        await asyncio.gather(hold_first(), then_second())
        # B entered only after A released -- never interleaved
        assert order == ["a-in", "a-out", "b-in"]

    asyncio.run(run())


def test_lock_release_after_expiry_warns_instead_of_raising():
    async def run():
        client = FakeAsyncRedis()
        store, _ = make_state(client, lock_timeout=1)
        lock = store.lock("s1")
        await lock.__aenter__()
        await asyncio.sleep(1.1)  # lock TTL elapsed mid-turn
        await lock.__aexit__(None, None, None)  # must not raise

    asyncio.run(run())


# -- AgentService in Redis mode (fake agent, real store) --

class FakeAgent:
    """Replays canned events; enough graph surface for AgentService."""

    def __init__(self, events):
        self.events = events

    async def astream_events(self, inputs, config, version):
        for event in self.events:
            yield event

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})


CHUNKS = [
    {"event": "on_chat_model_stream", "metadata": {},
     "data": {"chunk": SimpleNamespace(content="hi", tool_call_chunks=None)}},
]


def make_redis_service(client, events=CHUNKS):
    store, saver = make_state(client)
    return AgentService(FakeAgent(events), 12, saver, store=store)


def test_service_streams_in_redis_mode():
    async def run():
        client = FakeAsyncRedis()
        service = make_redis_service(client)
        sid = await service.acreate_session()
        events = [e async for e in service.stream(sid, "hello")]
        assert events[-1].type == "answer" and events[-1].data == "hi"
        # the turn lock was released -- no lock key left behind
        assert not await client.exists(f"agnes:lock:{sid}")

    asyncio.run(run())


def test_service_rejects_unknown_session_in_redis_mode():
    async def run():
        service = make_redis_service(FakeAsyncRedis())

        with pytest.raises(UnknownSessionError):
            async for _ in service.stream("never-created", "hi"):
                pass

    asyncio.run(run())


def test_session_created_on_one_instance_works_on_another():
    async def run():
        client = FakeAsyncRedis()
        # two services over one Redis = two pods behind a load balancer
        service_a = make_redis_service(client)
        service_b = make_redis_service(client)
        sid = await service_a.acreate_session()
        assert await service_b.ahas_session(sid)
        events = [e async for e in service_b.stream(sid, "hello")]
        assert events[-1].data == "hi"

    asyncio.run(run())


# -- app-level: /health reflects the store --

def test_health_reports_redis_and_503_when_down():
    from fastapi.testclient import TestClient

    from ease_clients.auth_api import NoAuthAuthenticator

    client = FakeAsyncRedis()
    service = make_redis_service(client)
    app = create_app(service=service, authenticator=NoAuthAuthenticator())
    with TestClient(app) as http:
        body = http.get("/health").json()
        assert body["status"] == "ok" and body["session_store"] == "redis"

        async def broken_ping():
            raise ConnectionError("down")

        service._store.ping = broken_ping
        assert http.get("/health").status_code == 503


# -- wiring: fail-fast on unreachable Redis --

def test_build_redis_state_fails_fast_when_unreachable():
    async def run():
        # port 1 on localhost: connection refused immediately
        with pytest.raises(RuntimeError, match="REDIS_URL"):
            await build_redis_state("redis://127.0.0.1:1/0", TTL, 500, 5)

    asyncio.run(run())


def test_build_redis_state_accepts_injected_client():
    async def run():
        store, saver = await build_redis_state(
            "redis://ignored:6379/0", TTL, 500, 5, client=FakeAsyncRedis(),
        )
        sid = await store.create_session()
        assert await store.has_session(sid)
        assert isinstance(saver, RedisSaver)

    asyncio.run(run())

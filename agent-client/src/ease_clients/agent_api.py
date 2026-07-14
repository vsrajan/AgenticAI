"""Agent service core and FastAPI app for the agent API.

Wraps the existing LangGraph agent behind a transport-agnostic
AgentService, then exposes it over HTTP so any external client (web UI,
Teams bot, Slack, another agent) can use it. Everything is imported
from utils/agnes_agent_graph.py -- that module is not modified.

Endpoints:
  GET  /health                         -> liveness + tool count (no auth)
  POST /sessions                       -> create a session id
  POST /sessions/{id}/messages         -> full answer as JSON (for bots)
  POST /sessions/{id}/messages/stream  -> SSE stream of phase/token/answer
  GET  /sessions/{id}/messages         -> conversation history (raw=true for debug)

Auth: Authorization: Bearer <token> on everything except /health.
See auth_api.py for the authenticator model.

Started by cli_api.py (uv run agent-api). For a full walkthrough of
the design, the concepts used here (FastAPI, SSE, bearer tokens), and
worked client examples, see docs/agent_api.md.
"""

import asyncio
import datetime
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from ease_clients.utils.agnes_agent_graph import (
    _NODE_PHASES,
    _get_mcp_server_config,
    _write_stream_entry,
    build_graph,
)
from ease_clients.auth_api import AuthError, Authenticator, Principal, build_authenticator
from ease_clients.utils.llm import get_llm

logger = logging.getLogger("ease_clients.agent_api")


# -- Events --

@dataclass
class AgentEvent:
    """One unit of agent output.

    type is one of:
      phase  -- the graph moved to a new node (data: human-readable label)
      token  -- one streamed token of the answer (data: token text)
      answer -- the complete answer text, always emitted last
      error  -- something went wrong (data: safe message)
    """
    type: str
    data: str


# -- Service core --

class UnknownSessionError(Exception):
    """Raised when a session id was never created or has been evicted."""


class _Session:
    """Bookkeeping for one live session.

    last_used drives TTL eviction (time.monotonic is a steady clock
    that never jumps backwards, unlike wall-clock time). The lock
    serialises turns: two concurrent messages on the same session
    would otherwise interleave their writes into one LangGraph thread
    and corrupt the conversation history.
    """
    __slots__ = ("last_used", "lock")

    def __init__(self):
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()


class AgentService:
    """Transport-agnostic wrapper around the compiled agent graph.

    Frontends consume stream() (token-level events) or ask() (buffered
    answer). Session ids map 1:1 onto LangGraph thread ids, so the
    checkpointer keeps per-session conversation history.

    Sessions are explicit: only ids minted by create_session() are
    accepted, and idle sessions are evicted after session_ttl_seconds
    (their checkpointer state deleted) so memory cannot grow without
    bound in a long-running server. max_sessions is a hard cap -- when
    full, the least recently used session is evicted to make room.
    """

    def __init__(self, agent, tool_count: int, checkpointer=None,
                 session_ttl_seconds: float = 3600, max_sessions: int = 500,
                 stream_file: str = "", store=None):
        self._agent = agent
        self.tool_count = tool_count
        self._checkpointer = checkpointer
        self._session_ttl = session_ttl_seconds
        self._max_sessions = max_sessions
        # store is the Redis session registry (redis_state.py) when
        # REDIS_URL is set; None keeps every in-process code path below
        # exactly as it was (the CLI, scanner, and tests never see Redis)
        self._store = store
        self._sessions: dict[str, _Session] = {}
        # stream_file mirrors the CLI's agent_stream.txt: every node's
        # output is appended as it completes, so tail -f gives live
        # visibility into graph execution. Empty string disables it.
        self._stream_file = stream_file
        if stream_file:
            # reset per server run (same as the CLI does per session)
            # so the file does not grow across restarts
            try:
                with open(stream_file, "w", encoding="utf-8") as fh:
                    fh.write(f"Agent API stream log -- {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
                    fh.write("=" * 60 + "\n")
            except OSError:
                logger.exception("Stream file %s is not writable; disabling stream log", stream_file)
                self._stream_file = ""

    # an async classmethod factory instead of doing this work in
    # __init__: connecting to the MCP server requires await, and
    # python does not allow __init__ to be async. So construction is
    # two steps -- create() does the slow async setup, then calls the
    # plain __init__ with the finished pieces.
    @classmethod
    async def create(cls, checkpointer=None) -> "AgentService":
        """Connect to the MCP server, load tools, build and compile the graph.

        Session state (P3.1): with REDIS_URL set, sessions, locks, and
        conversation checkpoints live in Redis -- they survive restarts
        and are shared by every agent-api instance pointing at the same
        Redis (required to run more than one instance). Without it,
        everything lives in process memory (MemorySaver) exactly as
        before: sessions are lost on restart and the server must stay a
        single instance. An explicitly passed checkpointer wins either
        way (tests use this).

        Session hygiene config from env:
            AGENT_API_SESSION_TTL_MINUTES -- evict sessions idle this long (default 60)
            AGENT_API_MAX_SESSIONS        -- hard cap; LRU-evict when full (default 500)
            REDIS_URL                     -- e.g. redis://localhost:6379/0; see docs/P3.1.md
            AGENT_API_LOCK_TIMEOUT_SECONDS -- Redis turn-lock TTL (default 300)
        Graph visibility:
            AGENT_API_STREAM_FILE -- per-node execution log for tail -f
                (default agent_api_stream.txt; empty string disables)
        """
        llm = get_llm()
        mcp_config = _get_mcp_server_config()

        logger.info("Connecting to MCP server ...")
        client = MultiServerMCPClient(mcp_config)
        tools = await client.get_tools()
        logger.info("Loaded %d MCP tools", len(tools))

        ttl_minutes = float(os.environ.get("AGENT_API_SESSION_TTL_MINUTES", "60"))
        max_sessions = int(os.environ.get("AGENT_API_MAX_SESSIONS", "500"))
        stream_file = os.environ.get("AGENT_API_STREAM_FILE", "agent_api_stream.txt")

        store = None
        redis_url = os.environ.get("REDIS_URL", "")
        if redis_url and checkpointer is None:
            # fails fast when Redis is unreachable -- see build_redis_state
            from ease_clients.redis_state import build_redis_state

            lock_timeout = float(os.environ.get("AGENT_API_LOCK_TIMEOUT_SECONDS", "300"))
            store, checkpointer = await build_redis_state(
                redis_url, ttl_minutes * 60, max_sessions, lock_timeout,
            )
        checkpointer = checkpointer or MemorySaver()
        graph = build_graph(llm, tools)
        agent = graph.compile(checkpointer=checkpointer)
        return cls(agent, len(tools), checkpointer,
                   session_ttl_seconds=ttl_minutes * 60,
                   max_sessions=max_sessions,
                   stream_file=stream_file,
                   store=store)

    # -- session lifecycle --

    def create_session(self) -> str:
        """Mint and register a new session id (the LangGraph thread id).

        When the registry is at max_sessions, the least recently used
        IDLE session is evicted first so the cap holds. A session whose
        turn is currently running (lock held) is never evicted -- if
        every session is mid-turn, the cap briefly overshoots instead
        of deleting history out from under a running turn.
        """
        while len(self._sessions) >= self._max_sessions:
            idle = [
                sid for sid, session in self._sessions.items()
                if not session.lock.locked()
            ]
            if not idle:
                logger.warning(
                    "max_sessions cap reached with every session mid-turn; "
                    "allowing temporary overshoot"
                )
                break
            lru_id = min(idle, key=lambda sid: self._sessions[sid].last_used)
            self._evict(lru_id, reason="max_sessions cap")
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = _Session()
        return session_id

    def has_session(self, session_id: str) -> bool:
        """True if the session exists (created and not yet evicted)."""
        return session_id in self._sessions

    # -- async variants used by the endpoints --
    # In Redis mode every registry operation is a network call and must
    # be awaited; in-memory mode they just call the sync methods above,
    # which stay exactly as they were (and keep working for tests and
    # any embedder that constructs AgentService directly).

    async def acreate_session(self) -> str:
        if self._store is not None:
            return await self._store.create_session()
        return self.create_session()

    async def ahas_session(self, session_id: str) -> bool:
        if self._store is not None:
            return await self._store.has_session(session_id)
        return self.has_session(session_id)

    async def _arequire_session(self, session_id: str) -> None:
        """Validate the session and refresh its TTL clock (both modes)."""
        if self._store is not None:
            try:
                await self._store.require(session_id)
            except KeyError:
                raise UnknownSessionError(session_id)
        else:
            self._require_session(session_id)

    def _require_session(self, session_id: str) -> _Session:
        """Return the session's bookkeeping entry, refreshing its TTL clock."""
        session = self._sessions.get(session_id)
        if session is None:
            raise UnknownSessionError(session_id)
        session.last_used = time.monotonic()
        return session

    def _evict(self, session_id: str, reason: str) -> None:
        """Drop one session: registry entry plus its checkpointer state.

        Deleting the checkpointer thread is what actually frees the
        conversation memory -- the registry entry is only bookkeeping.
        """
        self._sessions.pop(session_id, None)
        if self._checkpointer is not None and hasattr(self._checkpointer, "delete_thread"):
            self._checkpointer.delete_thread(session_id)
        logger.info("Evicted session %s (%s)", session_id, reason)

    def sweep_expired_sessions(self) -> int:
        """Evict every idle-beyond-TTL session. Returns the eviction count.

        Sessions whose turn is currently running (lock held) are never
        swept -- last_used only refreshes when a turn completes, so
        without this check a turn outlasting the TTL would have its
        history deleted mid-run.
        """
        now = time.monotonic()
        expired = [
            sid for sid, session in self._sessions.items()
            if now - session.last_used > self._session_ttl
            and not session.lock.locked()
        ]
        for sid in expired:
            self._evict(sid, reason="idle TTL")
        return len(expired)

    async def sweep_loop(self, interval_seconds: float = 60) -> None:
        """Background task: sweep expired sessions periodically.

        Started by the app's lifespan handler; runs until cancelled at
        shutdown. In Redis mode there is nothing to do -- every key
        carries an EXPIRE, so the idle TTL enforces itself server-side.
        """
        if self._store is not None:
            return
        while True:
            await asyncio.sleep(interval_seconds)
            self.sweep_expired_sessions()

    def _config(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 50}

    def _open_stream_file(self, session_id: str):
        """Open the stream file for one turn and write the turn header.

        Returns None when stream logging is disabled or the file cannot
        be opened -- a logging failure must never break the turn itself.
        The session id is part of the header (and of every entry) so
        one conversation can be reconstructed with grep even when turns
        from different sessions interleave in the file.
        """
        if not self._stream_file:
            return None
        try:
            fh = open(self._stream_file, "a", encoding="utf-8")
        except OSError:
            logger.exception("Could not open stream file %s", self._stream_file)
            return None
        fh.write(f"\n--- turn {datetime.datetime.now():%H:%M:%S}  session {session_id} ---\n\n")
        fh.flush()
        return fh

    # async def + yield makes this an async generator: callers loop
    # over it with "async for event in service.stream(...)" and receive
    # each event the moment it is produced, instead of waiting for the
    # whole answer. This is what lets the API stream tokens live.
    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[AgentEvent]:
        """Run one turn and yield AgentEvents as they happen.

        Same astream_events v2 handling as the CLI loop in agnes_agent_graph.py, but
        yields events instead of writing to stdout:
          on_chain_start on a known node -> phase event
          on_chat_model_stream content chunk without tool calls -> token event
          on_chain_end -> node output appended to the stream file (when enabled)
          end of run -> one answer event with the full text

        Also measures durations (the agent-side half of the P0
        instrumentation): each node's elapsed time is written into its
        stream-file entry, and a per-turn summary line splits the turn
        into agent (LLM) time vs tool time.

        Raises UnknownSessionError for ids that were never created or
        have been evicted.
        """
        if self._store is not None:
            # Redis mode: validate + TTL-refresh in Redis, and take the
            # DISTRIBUTED turn lock -- one turn at a time per session
            # even when the two turns land on different instances
            await self._arequire_session(session_id)
            session = None
            turn_lock = self._store.lock(session_id)
        else:
            session = self._require_session(session_id)
            turn_lock = session.lock
        config = self._config(session_id)
        answer_parts: list[str] = []
        stream_fh = self._open_stream_file(session_id)

        # per-node timing: a node's own start/end events carry the node
        # name in event["name"] (child runnables inherit the metadata
        # but not the name, so this filter times the node itself)
        turn_start = time.perf_counter()
        node_started: dict[str, float] = {}
        node_totals: dict[str, float] = {}

        try:
            # one turn at a time per session: a second message on the same
            # session waits here until the first finishes, instead of both
            # writing into the same thread's history concurrently
            async with turn_lock:
                if stream_fh:
                    _write_stream_entry(
                        stream_fh,
                        HumanMessage(content=user_input),
                        f"input, session: {session_id}",
                    )

                async for event in self._agent.astream_events(
                    {"messages": [HumanMessage(content=user_input)]},
                    config,
                    version="v2",
                ):
                    kind = event["event"]

                    if kind == "on_chain_start":
                        node = event.get("metadata", {}).get("langgraph_node", "")
                        if node and event.get("name") == node and node not in node_started:
                            node_started[node] = time.perf_counter()
                        phase = _NODE_PHASES.get(node)
                        if phase:
                            yield AgentEvent("phase", phase)

                    if kind == "on_chat_model_stream":
                        chunk = event["data"]["chunk"]
                        # content chunks without tool_call_chunks are answer tokens
                        if chunk.content and not getattr(chunk, "tool_call_chunks", None):
                            answer_parts.append(chunk.content)
                            yield AgentEvent("token", chunk.content)

                    # log each completed node's output for tail -f, exactly
                    # like the CLI loop does -- now with the node's duration
                    if kind == "on_chain_end":
                        node = event.get("metadata", {}).get("langgraph_node", "")
                        took = None
                        if node and event.get("name") == node and node in node_started:
                            took = time.perf_counter() - node_started.pop(node)
                            node_totals[node] = node_totals.get(node, 0.0) + took
                        if stream_fh and node:
                            tag = f"{node}, session: {session_id}"
                            if took is not None:
                                tag += f", took={took:.2f}s"
                            output = event.get("data", {}).get("output", {})
                            msgs = output.get("messages", []) if isinstance(output, dict) else []
                            for msg in msgs:
                                _write_stream_entry(stream_fh, msg, tag)

                # turn summary: agent (LLM) nodes vs tool nodes -- the
                # split that tells you where a slow turn actually went
                total = time.perf_counter() - turn_start
                tool_time = sum(v for k, v in node_totals.items() if k.endswith("_tools"))
                agent_time = sum(v for k, v in node_totals.items() if not k.endswith("_tools"))
                logger.info(
                    "session %s turn done in %.1fs (agent nodes %.1fs, tool nodes %.1fs)",
                    session_id, total, agent_time, tool_time,
                )
                if stream_fh:
                    stream_fh.write(
                        f"--- turn done in {total:.1f}s "
                        f"(agent nodes {agent_time:.1f}s, tool nodes {tool_time:.1f}s) ---\n\n"
                    )
                    stream_fh.flush()

                # a long turn should not count against the idle TTL
                if self._store is not None:
                    await self._store.touch(session_id)
                else:
                    session.last_used = time.monotonic()

                if answer_parts:
                    yield AgentEvent("answer", "".join(answer_parts))
                    return

                # nothing streamed -- fall back to reading the final state
                state = await self._agent.aget_state(config)
                messages = state.values.get("messages", [])
                yield AgentEvent("answer", messages[-1].content if messages else "")
        finally:
            if stream_fh:
                stream_fh.close()

    async def ask(self, session_id: str, user_input: str) -> str:
        """Run one turn and return the complete answer text (no streaming).

        Consumes stream() internally and keeps only the final answer
        event, so both entry points share one code path.
        """
        answer = ""
        async for event in self.stream(session_id, user_input):
            if event.type == "answer":
                answer = event.data
        return answer

    async def get_history(self, session_id: str) -> list:
        """Return the full raw message history for a session.

        Raises UnknownSessionError for ids that were never created or
        have been evicted.
        """
        await self._arequire_session(session_id)
        state = await self._agent.aget_state(self._config(session_id))
        return state.values.get("messages", [])


# -- History shaping --

def _history_to_chat(messages: list) -> list[dict]:
    """Reduce raw graph history to what a chat window displays.

    The stored history contains everything the graph produced: tool
    calls, tool results, and routing chatter. A chat client shows only
    the human messages and the AI messages that were actual answers
    (content present, no tool calls) -- the same filter the streaming
    path applies when deciding which tokens to display.
    """
    chat = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            chat.append({"role": "user", "text": msg.content})
        elif isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            chat.append({"role": "assistant", "text": msg.content})
    return chat


def _history_to_debug(messages: list) -> list[dict]:
    """Dump every stored message for debugging: its class name, its
    content, and the names of any tool calls it made."""
    dump = []
    for msg in messages:
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        entry = {"type": msg.__class__.__name__, "text": content}
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [tc["name"] for tc in tool_calls]
        dump.append(entry)
    return dump


# -- FastAPI app --

# a pydantic model describes the expected JSON request body. FastAPI
# parses and validates incoming JSON against it automatically: a POST
# body of {"message": "hi"} becomes MessageRequest(message="hi"), and
# a body without "message" is rejected with a 422 error before our
# endpoint code ever runs.
class MessageRequest(BaseModel):
    message: str


# HTTPBearer is a FastAPI helper that extracts the
# "Authorization: Bearer <token>" header from a request.
# auto_error=False means a MISSING header is passed to us as None
# instead of being rejected immediately -- we want the configured
# authenticator to make that call (none mode accepts missing headers,
# static mode rejects them).
_bearer = HTTPBearer(auto_error=False)


def create_app(
    service: AgentService | None = None,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    service and authenticator can be injected (used by tests). When
    omitted, they are created once at startup by the lifespan handler --
    the authenticator from env config, the service by connecting to the
    MCP server.
    """

    # lifespan is FastAPI's startup/shutdown hook: everything before
    # the yield runs once when the server starts (before any request is
    # accepted), everything after the yield would run at shutdown. The
    # slow work -- reading auth config, connecting to the MCP server,
    # building the graph -- happens here exactly once, not per request.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.authenticator is None:
            app.state.authenticator = build_authenticator()
        if app.state.service is None:
            app.state.service = await AgentService.create()
        # background sweeper frees idle sessions; cancelled at shutdown
        sweeper = asyncio.create_task(app.state.service.sweep_loop())
        logger.info("Agent API ready (%d tools)", app.state.service.tool_count)
        yield
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="Access Governance Agent API", lifespan=lifespan)

    # -- CORS --
    # browsers refuse to let javascript on one origin (for example the
    # POC page webclient_api.html opened as a local file) read responses
    # from an api on another origin (http://127.0.0.1:8080) unless the
    # api opts in by sending CORS headers. This middleware adds them.
    # AGENT_API_CORS_ORIGINS is a comma-separated allowlist; the default
    # * accepts any origin, which is fine for local development --
    # tighten it to your real frontend's origin for deployments.
    # Note CORS is not authentication: every request still needs the
    # bearer token. CORS only controls which web pages a browser will
    # let read our responses.
    cors_origins = os.environ.get("AGENT_API_CORS_ORIGINS", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins.split(",")],
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # app.state is a scratch area FastAPI provides for objects that
    # should live as long as the server; endpoints read them back from
    # there. None here means "build at startup" (see lifespan above);
    # tests pass ready-made fakes instead.
    app.state.service = service
    app.state.authenticator = authenticator

    # This function is a FastAPI "dependency". Any endpoint that
    # declares an argument like
    #     principal: Principal = Depends(current_principal)
    # gets this function run BEFORE its own body. If this function
    # returns a value, the request proceeds and the endpoint receives
    # that value; if it raises HTTPException, the client gets that
    # error response and the endpoint body never runs. This is how one
    # auth check protects every endpoint without repeating code.
    # Depends(_bearer) chains the same mechanism one level down: FastAPI
    # runs the HTTPBearer helper first and hands us its result here.
    async def current_principal(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        token = credentials.credentials if credentials else ""
        try:
            return request.app.state.authenticator.authenticate(token)
        except AuthError:
            # 401 = "who are you?" -- the standard status for missing or
            # bad credentials. The WWW-Authenticate header tells clients
            # which auth scheme this API expects.
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # @app.get / @app.post decorators register a function as the
    # handler for one URL and HTTP method. Returning a dict makes
    # FastAPI serialize it to JSON automatically.
    # /health takes no Depends(current_principal), so it is the one
    # endpoint that works without a token -- monitoring probes need it.
    @app.get("/health")
    async def health():
        service = app.state.service
        # in Redis mode an instance that cannot reach Redis cannot serve
        # any session -- report 503 so an orchestrator's readiness probe
        # takes it out of rotation (see docs/P3.1.md section 12)
        if service is not None and service._store is not None:
            try:
                await service._store.ping()
            except Exception:
                logger.exception("Health check: session store unreachable")
                raise HTTPException(
                    status_code=503, detail="Session store unreachable.",
                )
            return {"status": "ok", "tools": service.tool_count,
                    "session_store": "redis"}
        return {"status": "ok", "tools": service.tool_count if service else 0}

    @app.post("/sessions")
    async def create_session(principal: Principal = Depends(current_principal)):
        session_id = await app.state.service.acreate_session()
        logger.info("Created session %s for %s", session_id, principal.subject)
        return {"session_id": session_id}

    # {session_id} in the path is a variable: FastAPI matches the URL
    # and passes the value as the session_id argument. The request
    # body is parsed into MessageRequest (see above).
    @app.post("/sessions/{session_id}/messages")
    async def send_message(
        session_id: str,
        request: MessageRequest,
        principal: Principal = Depends(current_principal),
    ):
        """Buffered request/response -- for clients that can't stream (bots)."""
        try:
            answer = await app.state.service.ask(session_id, request.message)
        except UnknownSessionError:
            raise HTTPException(
                status_code=404,
                detail="Unknown or expired session. Create a new one with POST /sessions.",
            )
        except Exception:
            # log the full traceback server-side but send the client a
            # generic message -- internals never leak into responses
            logger.exception("Error processing message for session %s", session_id)
            raise HTTPException(status_code=500, detail="Error processing the message.")
        return {"answer": answer}

    # a query parameter with a default (raw: bool = False) is optional:
    # GET .../messages returns the chat view, GET .../messages?raw=true
    # switches to the full debug dump
    @app.get("/sessions/{session_id}/messages")
    async def get_messages(
        session_id: str,
        raw: bool = False,
        principal: Principal = Depends(current_principal),
    ):
        """Conversation history for a session.

        Default: only what a chat window shows (user and assistant
        messages) -- lets a reloaded web client repopulate its view.
        raw=true: every stored message with its type and tool calls --
        for debugging what the agent actually did.
        """
        try:
            messages = await app.state.service.get_history(session_id)
        except UnknownSessionError:
            raise HTTPException(
                status_code=404,
                detail="Unknown or expired session. Create a new one with POST /sessions.",
            )
        shaped = _history_to_debug(messages) if raw else _history_to_chat(messages)
        return {"session_id": session_id, "messages": shaped}

    @app.post("/sessions/{session_id}/messages/stream")
    async def stream_message(
        session_id: str,
        request: MessageRequest,
        principal: Principal = Depends(current_principal),
    ):
        """SSE stream of phase/token/answer events -- for live UIs."""
        # validate the session BEFORE streaming starts: once the
        # response body is open the status line is already sent, so a
        # 404 can only be delivered here
        if not await app.state.service.ahas_session(session_id):
            raise HTTPException(
                status_code=404,
                detail="Unknown or expired session. Create a new one with POST /sessions.",
            )

        # sse() is an async generator producing Server-Sent Events, the
        # standard format for pushing updates over one open HTTP
        # response (see docs/agent_api.md section 4). Each event is two
        # text lines plus a blank line as separator:
        #     event: token
        #     data: {"data": "Hello"}
        #
        # StreamingResponse sends each yielded string to the client
        # immediately instead of collecting everything first -- that is
        # the whole point: the client sees tokens as they are generated.
        async def sse() -> AsyncIterator[str]:
            try:
                async for event in app.state.service.stream(session_id, request.message):
                    payload = json.dumps({"data": event.data})
                    yield f"event: {event.type}\ndata: {payload}\n\n"
            except Exception:
                # too late for an HTTP error status -- the 200 header
                # went out when streaming began. Signal failure in-band
                # as an error event instead.
                logger.exception("Error streaming message for session %s", session_id)
                payload = json.dumps({"data": "Error processing the message."})
                yield f"event: error\ndata: {payload}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return app

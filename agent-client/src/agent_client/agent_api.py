"""Agent service core and FastAPI app for the agent API.

Wraps the existing LangGraph agent behind a transport-agnostic
AgentService, then exposes it over HTTP so any external client (web UI,
Teams bot, Slack, another agent) can use it. Everything is imported
from agent.py -- that module is not modified.

Endpoints:
  GET  /health                         -> liveness + tool count (no auth)
  POST /sessions                       -> create a session id
  POST /sessions/{id}/messages         -> full answer as JSON (for bots)
  POST /sessions/{id}/messages/stream  -> SSE stream of phase/token/answer

Auth: Authorization: Bearer <token> on everything except /health.
See auth_api.py for the authenticator model.

Run via cli_api.py:
    cd agent-client && uv run src/agent_client/cli_api.py
"""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from agent_client.agent import _NODE_PHASES, _get_mcp_server_config, build_graph
from agent_client.auth_api import AuthError, Authenticator, Principal, build_authenticator
from agent_client.llm import get_llm

logger = logging.getLogger("agent_client.agent_api")


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

class AgentService:
    """Transport-agnostic wrapper around the compiled agent graph.

    Frontends consume stream() (token-level events) or ask() (buffered
    answer). Session ids map 1:1 onto LangGraph thread ids, so the
    checkpointer keeps per-session conversation history.
    """

    def __init__(self, agent, tool_count: int):
        self._agent = agent
        self.tool_count = tool_count

    @classmethod
    async def create(cls, checkpointer=None) -> "AgentService":
        """Connect to the MCP server, load tools, build and compile the graph.

        checkpointer defaults to MemorySaver -- sessions live in process
        memory and are lost on restart. Pass a persistent saver to change that.
        """
        llm = get_llm()
        mcp_config = _get_mcp_server_config()

        logger.info("Connecting to MCP server ...")
        client = MultiServerMCPClient(mcp_config)
        tools = await client.get_tools()
        logger.info("Loaded %d MCP tools", len(tools))

        graph = build_graph(llm, tools)
        agent = graph.compile(checkpointer=checkpointer or MemorySaver())
        return cls(agent, len(tools))

    def create_session(self) -> str:
        """Return a new session id (used as the LangGraph thread id)."""
        return uuid.uuid4().hex

    def _config(self, session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 50}

    async def stream(self, session_id: str, user_input: str) -> AsyncIterator[AgentEvent]:
        """Run one turn and yield AgentEvents as they happen.

        Same astream_events v2 handling as the CLI loop in agent.py, but
        yields events instead of writing to stdout:
          on_chain_start on a known node -> phase event
          on_chat_model_stream content chunk without tool calls -> token event
          end of run -> one answer event with the full text
        """
        config = self._config(session_id)
        answer_parts: list[str] = []

        async for event in self._agent.astream_events(
            {"messages": [HumanMessage(content=user_input)]},
            config,
            version="v2",
        ):
            kind = event["event"]

            if kind == "on_chain_start":
                node = event.get("metadata", {}).get("langgraph_node", "")
                phase = _NODE_PHASES.get(node)
                if phase:
                    yield AgentEvent("phase", phase)

            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                # content chunks without tool_call_chunks are answer tokens
                if chunk.content and not getattr(chunk, "tool_call_chunks", None):
                    answer_parts.append(chunk.content)
                    yield AgentEvent("token", chunk.content)

        if answer_parts:
            yield AgentEvent("answer", "".join(answer_parts))
            return

        # nothing streamed -- fall back to reading the final state
        state = await self._agent.aget_state(config)
        messages = state.values.get("messages", [])
        yield AgentEvent("answer", messages[-1].content if messages else "")

    async def ask(self, session_id: str, user_input: str) -> str:
        """Run one turn and return the complete answer text (no streaming)."""
        answer = ""
        async for event in self.stream(session_id, user_input):
            if event.type == "answer":
                answer = event.data
        return answer

    async def get_history(self, session_id: str) -> list:
        """Return the full message history for a session."""
        state = await self._agent.aget_state(self._config(session_id))
        return state.values.get("messages", [])


# -- FastAPI app --

class MessageRequest(BaseModel):
    message: str


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.authenticator is None:
            app.state.authenticator = build_authenticator()
        if app.state.service is None:
            app.state.service = await AgentService.create()
        logger.info("Agent API ready (%d tools)", app.state.service.tool_count)
        yield

    app = FastAPI(title="Access Governance Agent API", lifespan=lifespan)
    app.state.service = service
    app.state.authenticator = authenticator

    async def current_principal(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        token = credentials.credentials if credentials else ""
        try:
            return request.app.state.authenticator.authenticate(token)
        except AuthError:
            raise HTTPException(
                status_code=401,
                detail="Invalid or missing bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    async def health():
        service = app.state.service
        return {"status": "ok", "tools": service.tool_count if service else 0}

    @app.post("/sessions")
    async def create_session(principal: Principal = Depends(current_principal)):
        session_id = app.state.service.create_session()
        logger.info("Created session %s for %s", session_id, principal.subject)
        return {"session_id": session_id}

    @app.post("/sessions/{session_id}/messages")
    async def send_message(
        session_id: str,
        request: MessageRequest,
        principal: Principal = Depends(current_principal),
    ):
        """Buffered request/response -- for clients that can't stream (bots)."""
        try:
            answer = await app.state.service.ask(session_id, request.message)
        except Exception:
            logger.exception("Error processing message for session %s", session_id)
            raise HTTPException(status_code=500, detail="Error processing the message.")
        return {"answer": answer}

    @app.post("/sessions/{session_id}/messages/stream")
    async def stream_message(
        session_id: str,
        request: MessageRequest,
        principal: Principal = Depends(current_principal),
    ):
        """SSE stream of phase/token/answer events -- for live UIs."""

        async def sse() -> AsyncIterator[str]:
            try:
                async for event in app.state.service.stream(session_id, request.message):
                    payload = json.dumps({"data": event.data})
                    yield f"event: {event.type}\ndata: {payload}\n\n"
            except Exception:
                # the response has already started, so signal errors in-band
                logger.exception("Error streaming message for session %s", session_id)
                payload = json.dumps({"data": "Error processing the message."})
                yield f"event: error\ndata: {payload}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")

    return app

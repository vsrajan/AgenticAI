"""LangGraph ReAct agent that connects to a remote MCP docs server.

The agent connects to an already-running MCP server (stdio or SSE),
loads the documentation tools, and wraps them in a ReAct loop powered
by AzureOpenAI.

The MCP server must be started separately — this client does NOT
manage the server lifecycle.
"""

import asyncio
import datetime
import itertools
import logging
import os
import sys
import uuid
from typing import Annotated

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import AgentState, create_agent
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import add_messages

from dotenv import load_dotenv, find_dotenv
from ease_clients.utils.llm import get_llm

logger = logging.getLogger("agent_client.agent")


class Spinner:
    """Animated terminal spinner shown while the agent is thinking."""

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking") -> None:
        self._message = message
        self._task: asyncio.Task | None = None

    async def _spin(self) -> None:
        write = sys.stderr.write
        flush = sys.stderr.flush
        try:
            for frame in itertools.cycle(self._FRAMES):
                write(f"\r{frame} {self._message}…")
                flush()
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            # Clear the spinner line
            write("\r" + " " * (len(self._message) + 4) + "\r")
            flush()

    async def __aenter__(self) -> "Spinner":
        self._task = asyncio.create_task(self._spin())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# Maximum number of messages to keep in the agent's working state.
# Each ReAct turn can produce 4-6 messages (human, tool calls, tool
# results, assistant answer), so 20 ≈ 3-4 full turns of context.
# Set to 0 to disable trimming (unlimited history).
KEEP_LAST_N = int(os.environ.get("KEEP_LAST_N_MSGS", "20"))


def _add_and_trim_messages(left: list, right: list) -> list:
    """Standard add_messages reducer followed by a trim to KEEP_LAST_N."""
    result = add_messages(left, right)
    if KEEP_LAST_N > 0 and len(result) > KEEP_LAST_N:
        result = result[-KEEP_LAST_N:]
    return result


class TrimmedAgentState(AgentState):
    """AgentState whose messages reducer caps history at KEEP_LAST_N."""

    messages: Annotated[list[AnyMessage], _add_and_trim_messages]  # type: ignore[assignment]


SYSTEM_PROMPT = (
    "You are an Access Governance assistant. You help users understand the "
    "Access Governance application, find the right access rights, and discover "
    "what their peers already have.\n\n"

    "Your PRIMARY knowledge source is the PDF documentation. CSV datasets "
    "supplement the documentation with structured data (access rights "
    "catalogues, entitlement records, etc.).\n\n"

    "=== PDF DOCUMENTATION TOOLS ===\n\n"

    "These are your main tools for answering questions about how Access "
    "Governance works — processes, procedures, FAQs, how-to guides, and "
    "policies.\n\n"

    "- list_topics  — call this first to see every available documentation "
    "topic and its document paths.\n"
    "- search_docs(query) — full-text search across all documents. Returns "
    "ranked results with snippets and page_path values.\n"
    "- read_page(page_path) — retrieves the complete text of a document. "
    "The text contains [Page N] markers so you can identify exactly which "
    "PDF page each piece of information comes from.\n\n"

    "PDF search strategy:\n"
    "1. Call search_docs with the user's question (try different phrasings "
    "if the first search returns few results).\n"
    "2. For every relevant result, call read_page to get the full content — "
    "snippets from search_docs are too short for a thorough answer.\n"
    "3. Read the [Page N] markers in the returned text to identify the exact "
    "pages that contain the answer.\n"
    "4. Synthesise a clear answer and cite every fact with its document and "
    "page number (see CITATIONS below).\n"
    "5. If the answer spans multiple documents, read each one and combine "
    "the information.\n\n"

    "=== CSV DATA TOOLS ===\n\n"

    "These tools provide structured data — access rights catalogues, user "
    "entitlement records, organisational data, etc.\n\n"

    "- list_datasets — shows available datasets, their column names, and "
    "row counts.\n"
    "- search_dataset(dataset, query) — free-text search across all columns "
    "of a dataset.\n"
    "- filter_dataset(dataset, filters) — filter rows by exact column values "
    "(case-insensitive).\n"
    "- get_column_values(dataset, column) — list distinct values in a column "
    "(useful before filtering).\n\n"

    "CSV search strategies:\n"
    "- Access rights by name/description: search_dataset on the access "
    "rights dataset.\n"
    "- Peer recommendations: filter_dataset on entitlements by OU/location, "
    "count the most common access rights, then look up descriptions.\n"
    "- Exploration: get_column_values to show distinct OUs, locations, or "
    "categories, then let the user narrow down with filter_dataset.\n\n"

    "=== STARTUP ===\n\n"

    "At the start of a conversation:\n"
    "1. Call list_datasets to learn the available datasets and their column "
    "names (columns are not known in advance — discover them dynamically).\n"
    "2. If no datasets are loaded, that is normal — the server may only have "
    "PDF documentation. Rely on the PDF tools.\n\n"

    "=== CITATIONS (MANDATORY) ===\n\n"

    "Every claim sourced from PDF documentation MUST include an inline "
    "citation with the document path and the specific page number. The "
    "content returned by search_docs and read_page contains [Page N] "
    "markers — use these to identify the exact page.\n\n"

    "Format: (Source: <page_path>, Page <N>)\n"
    "Examples:\n"
    "- (Source: entitlements/ordering_faq.pdf, Page 2)\n"
    "- (Source: delegations/setup_guide.pdf, Pages 3-4)\n\n"

    "Rules:\n"
    "- Cite immediately after each fact or paragraph, not just once at the "
    "end of your answer.\n"
    "- If information spans multiple pages, cite the range.\n"
    "- If multiple documents are used, cite each one where it is referenced.\n"
    "- After calling search_docs, ALWAYS call read_page to get the full "
    "content — search snippets alone are not sufficient for accurate "
    "page-level citations.\n"
    "- Never omit citations for documentation-sourced information.\n\n"

    "=== GUIDELINES ===\n\n"

    "1. Always use the tools before answering — do not guess.\n"
    "2. Default to PDF documentation for how-to, process, and policy "
    "questions. Use CSV data for lookups, recommendations, and "
    "data-driven queries.\n"
    "3. When presenting data results, format them clearly (tables or "
    "lists).\n"
    "4. If the data or documentation does not cover the user's question, "
    "say so clearly.\n"
    "5. Be concise but thorough.\n"
)


def _get_mcp_server_config() -> dict:
    """Build the MCP server connection config from environment variables.

    Supported transports:
        sse   — connects to a running MCP server over HTTP/SSE
                Requires MCP_SERVER_URL (e.g. http://host:8000/sse)
        stdio — connects to a running MCP server via stdin/stdout pipe
                Requires MCP_SERVER_COMMAND (e.g. "uv") and
                MCP_SERVER_ARGS (e.g. "run --directory ../mcp-server mcp-docs-server")
    """
    server_name = os.environ.get("MCP_SERVER_NAME", "access-governance-docs")
    transport = os.environ.get("MCP_TRANSPORT", "sse").lower()

    if transport == "sse":
        url = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/sse")
        logger.info("MCP server=%s transport=sse url=%s", server_name, url)
        return {
            server_name: {
                "url": url,
                "transport": "sse",
            }
        }

    if transport == "stdio":
        command = os.environ.get("MCP_SERVER_COMMAND")
        args_str = os.environ.get("MCP_SERVER_ARGS", "")
        if not command:
            raise ValueError(
                "MCP_TRANSPORT=stdio requires MCP_SERVER_COMMAND to be set "
                "(e.g. MCP_SERVER_COMMAND=uv)"
            )
        args = args_str.split() if args_str else []
        logger.info("MCP server=%s transport=stdio command=%s args=%s", server_name, command, args)
        return {
            server_name: {
                "command": command,
                "args": args,
                "transport": "stdio",
            }
        }

    raise ValueError(
        f"Unsupported MCP_TRANSPORT={transport!r}. Use 'sse' or 'stdio'."
    )


LOG_FILE = "agent_log.txt"


def _dump_history(all_messages: list) -> None:
    """Write the full conversation history to *LOG_FILE*."""
    try:
        if not all_messages:
            return
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"Agent session log — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"Total messages: {len(all_messages)}\n")
            fh.write("=" * 60 + "\n\n")
            for msg in all_messages:
                role = msg.__class__.__name__
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                fh.write(f"[{role}]\n{content}\n\n")
        logger.info("Session history written to %s (%d messages)", LOG_FILE, len(all_messages))
    except Exception:
        logger.exception("Failed to write session history to %s", LOG_FILE)


async def run_agent_loop(on_response=None):
    """Run the interactive agent loop.

    Args:
        on_response: Optional callback ``(str) -> None`` called with each
            assistant response.  Defaults to ``print``.
    """
    if on_response is None:
        on_response = print

    llm = get_llm()
    mcp_config = _get_mcp_server_config()

    logger.info("Connecting to MCP server …")

    client = MultiServerMCPClient(mcp_config)
    tools = await client.get_tools()
    logger.info("Loaded %d MCP tools", len(tools))

    checkpointer = MemorySaver()
    agent = create_agent(
        llm, tools,
        system_prompt=SYSTEM_PROMPT,
        state_schema=TrimmedAgentState,
        checkpointer=checkpointer,
    )

    # Each CLI session gets a unique thread so the checkpointer can
    # track the conversation history across turns.
    thread_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

    # Full (untrimmed) message history for the session log.
    # The agent state is trimmed to KEEP_LAST_N, so we track separately.
    all_messages: list = []

    print("\nAccess Governance Assistant")
    print("=" * 40)
    print('Type your question below. Type "exit" to quit.\n')

    while True:
        try:
            user_input = input("🧑 You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("Goodbye!")
            break

        logger.info("User query: %s", user_input)

        try:
            human_msg = HumanMessage(content=user_input)
            all_messages.append(human_msg)
            async with Spinner("Thinking"):
                response = await agent.ainvoke(
                    {"messages": [human_msg]},
                    config,
                )
            # The agent state is trimmed, but new messages from this
            # turn are always present.  Collect any we haven't seen.
            seen_ids = {m.id for m in all_messages}
            for msg in response["messages"]:
                if msg.id not in seen_ids:
                    all_messages.append(msg)
            # The last message is the assistant's final answer.
            answer = response["messages"][-1].content
            logger.debug("Agent response: %s", answer)
            on_response(f"\n🤖 Assistant: {answer}\n")
        except Exception:
            logger.exception("Error processing query")
            on_response(
                "\nAssistant: Sorry, an error occurred while "
                "processing your question. Please try again.\n"
            )

    # Dump full (untrimmed) conversation history on exit.
    _dump_history(all_messages)

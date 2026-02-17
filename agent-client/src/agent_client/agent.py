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

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

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


# Maximum number of messages to keep in conversation history.
# Each ReAct turn can produce 4-6 messages (human, tool calls, tool
# results, assistant answer), so 20 ≈ 3-4 full turns of context.
# Set to 0 to disable trimming (unlimited history).
KEEP_LAST_N = int(os.environ.get("KEEP_LAST_N_MSGS", "20"))


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

    "Two CSV datasets provide structured data for access-rights discovery:\n\n"

    "1. Entitlements — each row is a person-to-resource assignment.\n"
    "   Key columns:\n"
    "   - ResourceID: the access right identifier (join key to Resources)\n"
    "   - JOBTITLE: the person's job title (e.g. Software Engineer, "
    "Product Manager, Analyst). This is a PRIMARY search criterion — "
    "people with the same job title typically need the same access rights.\n"
    "   - OU: the user's organisational unit\n"
    "   - ParentOU: the parent of the user's OU\n"
    "   - CITY / COUNTRY_VALUE: location\n"
    "   - C_EMPLOYEECLASS: employee class (e.g. External Staff)\n"
    "   - Business hierarchy (top-down): AREANAME > SECTORNAME > "
    "SEGMENTNAME > FUNCTIONNAME (from broadest to most specific)\n\n"

    "2. Resources — the access-rights catalogue.\n"
    "   Key columns:\n"
    "   - ResourceID: unique identifier (join key to Entitlements)\n"
    "   - name: human-readable name of the access right\n"
    "   - DESCRIPTION: what the access right grants\n"
    "   - ResourceType / RequestingSystem: classification and owning system\n\n"

    "Available CSV tools:\n"
    "- list_datasets — shows datasets, their column names, and row counts.\n"
    "- search_dataset(dataset, query) — free-text BM25 search across all "
    "columns of a dataset.\n"
    "- filter_dataset(dataset, filters) — filter rows by exact column values "
    "(case-insensitive).\n"
    "- get_column_values(dataset, column) — list distinct values in a column "
    "(useful before filtering).\n\n"

    "=== CSV SEARCH STRATEGIES ===\n\n"

    "Strategy 1 — Peer-based recommendations (most common):\n"
    "When a user wants to know which access rights they should have, find "
    "what their peers already hold:\n"
    "  a. Identify the user's attributes — ask for JOBTITLE, OU, and "
    "their business hierarchy (AREANAME, SECTORNAME, SEGMENTNAME, or "
    "FUNCTIONNAME) if not already provided.\n"
    "  b. Use count_by_column on Entitlements, grouping by ResourceID, "
    "with filters for the user's attributes. This returns ResourceIDs "
    "ranked by how many peers hold each one:\n"
    "     count_by_column(\"Entitlements\", \"ResourceID\", "
    "{\"JOBTITLE\": \"Software Engineer\", \"OU\": \"...\"})\n"
    "  c. If too few results, broaden progressively:\n"
    "     - Drop OU, keep JOBTITLE + business hierarchy column\n"
    "     - Try ParentOU instead of OU\n"
    "     - Use JOBTITLE alone across the whole organisation\n"
    "  d. For the top ResourceIDs, call search_dataset on Resources "
    "to retrieve names and descriptions.\n"
    "  e. Present results as a table with columns: Resource Name, "
    "Description, Requesting System (from Resources), and Peer Count "
    "(the count from count_by_column). Sort by Peer Count descending.\n\n"

    "Strategy 2 — Search by description:\n"
    "When a user describes what they need (e.g. \"SAP finance reporting\"):\n"
    "  a. search_dataset on Resources with the description.\n"
    "  b. Present matches with name, description, and RequestingSystem.\n\n"

    "Strategy 3 — Explore the organisation:\n"
    "When a user is unsure of exact values, use get_column_values on "
    "Entitlements for the relevant column, let them pick, then proceed "
    "with Strategy 1 or 2.\n\n"

    "CSV rules:\n"
    "- Values must match exactly (case-insensitive). Use get_column_values "
    "if unsure of spelling.\n"
    "- If filter_dataset returns nothing, broaden by removing the most "
    "specific criterion first.\n"
    "- ResourceID joins the two datasets. Always look up Resources for "
    "names/descriptions — never show raw ResourceIDs.\n\n"

    "=== STARTUP ===\n\n"

    "At the start of a conversation:\n"
    "1. Call list_datasets to confirm the available datasets and their "
    "column names.\n"
    "2. If no datasets are loaded, that is normal — the server may only "
    "have PDF documentation. Rely on the PDF tools.\n"
    "3. When a user asks about access rights, recommendations, or what "
    "they should request, ask them for:\n"
    "   - Job Title (e.g. Software Engineer, Product Manager)\n"
    "   - OU or Parent OU (organisational unit)\n"
    "   - Business hierarchy — tell the user the organisation is split "
    "into Areas, Sectors, Segments, and Functions, and ask which one "
    "they belong to and its name (e.g. SEGMENTNAME = \"Cloud Platform\")\n"
    "   - Location (city / country) — optional but helpful\n"
    "   If unsure of exact values, offer to look them up with "
    "get_column_values.\n\n"

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


def _prompt(state: dict) -> list:
    """Prepend system prompt and trim conversation history.

    The checkpointer stores the full history, but the LLM only sees the
    system prompt plus the most recent ``KEEP_LAST_N`` messages.  This
    prevents context-window overflow and attention dilution in long
    sessions while preserving the complete history for debugging.
    """
    messages = state.get("messages", [])
    if KEEP_LAST_N > 0 and len(messages) > KEEP_LAST_N:
        messages = messages[-KEEP_LAST_N:]
        # Drop orphaned ToolMessages at the start of the window — their
        # preceding AIMessage (with tool_calls) was trimmed away, and the
        # OpenAI API rejects tool-role messages without a prior tool_calls.
        while messages and isinstance(messages[0], ToolMessage):
            messages = messages[1:]
    return [SystemMessage(content=SYSTEM_PROMPT)] + messages


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


async def _dump_history(agent, config) -> None:
    """Write the full conversation history from the checkpointer to *LOG_FILE*."""
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return
        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"Agent session log — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"Total messages: {len(messages)}\n")
            fh.write("=" * 60 + "\n\n")
            for msg in messages:
                role = msg.__class__.__name__
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                fh.write(f"[{role}]\n{content}\n\n")
        logger.info("Session history written to %s (%d messages)", LOG_FILE, len(messages))
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
    agent = create_react_agent(llm, tools, prompt=_prompt, checkpointer=checkpointer)

    # Each CLI session gets a unique thread so the checkpointer can
    # track the conversation history across turns.
    thread_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}}

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
            async with Spinner("Thinking"):
                response = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user_input)]},
                    config,
                )
            # The last message is the assistant's final answer
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
    await _dump_history(agent, config)

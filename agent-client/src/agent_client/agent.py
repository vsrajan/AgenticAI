"""LangGraph custom StateGraph agent with Router, PDF, and CSV nodes.

The agent connects to an already-running MCP server (stdio or SSE),
loads the documentation and data tools, and routes user queries through
specialised nodes:

    START ──▶ (active_agent?) ──▶ specialist ↔ tools ──▶ END
                    │                  │
                    ▼                  ▼ (handoff)
                 router            handoff ──▶ router
                    │
                    └──▶ END (greeting / chat)

Once the router assigns a specialist, that specialist owns the
conversation until the user switches context (e.g. from data
questions to process questions).  The specialist hands back to the
router via hand_off_to_router only on a context switch.

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

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

from dotenv import load_dotenv, find_dotenv
from ease_clients.utils.llm import get_llm

logger = logging.getLogger("agent_client.agent")


# ── Spinner ──────────────────────────────────────────────────────────

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


# ── Configuration ────────────────────────────────────────────────────

# Maximum number of messages to keep in conversation history.
# Each turn can produce several messages (human, tool calls, tool
# results, assistant answer), so 20 ≈ 3-4 full turns of context.
# Set to 0 to disable trimming (unlimited history).
KEEP_LAST_N = int(os.environ.get("KEEP_LAST_N_MSGS", "20"))

# Tool name sets used for splitting MCP tools into groups.
PDF_TOOL_NAMES = {"list_topics", "search_docs", "read_page"}
CSV_TOOL_NAMES = {
    "list_datasets",
    "search_dataset",
    "filter_dataset",
    "filter_dataset_fuzzy",
    "count_by_column",
    "get_column_values",
}


# ── State ────────────────────────────────────────────────────────────

class AgentState(MessagesState):
    """Extended state that tracks which specialist owns the conversation."""
    active_agent: str  # "pdf_agent", "csv_agent", or "" (use router)


# ── Routing tools (used by the router) ───────────────────────────────

@tool
def route_to_pdf(reason: str) -> str:
    """Route the query to the PDF Documentation specialist agent."""
    return reason


@tool
def route_to_csv(reason: str) -> str:
    """Route the query to the CSV Data specialist agent."""
    return reason


ROUTING_TOOLS = [route_to_pdf, route_to_csv]


# ── Handoff tool (used by specialists) ───────────────────────────────

@tool
def hand_off_to_router(reason: str) -> str:
    """Hand the conversation back to the router because the user's
    question is outside your area of expertise."""
    return reason


# ── Prompts ──────────────────────────────────────────────────────────

ROUTER_PROMPT = (
    "You are the routing agent for an Access Governance assistant. "
    "Analyse the user's query and the conversation history, then "
    "decide the next step.\n\n"

    "You have two specialist agents:\n"
    "1. PDF Documentation Agent — answers questions about how Access "
    "Governance works: processes, procedures, FAQs, how-to guides, "
    "and policies.\n"
    "2. CSV Data Agent — handles structured data lookups: access "
    "rights catalogues, entitlement records, peer-based "
    "recommendations, and organisational data.\n\n"

    "=== DECISION RULES ===\n\n"
    "- Questions about how something works, processes, policies, "
    "procedures, FAQs → route to PDF.\n"
    "- Questions about specific access rights, peer recommendations, "
    "data lookups, entitlements, organisational data → route to CSV.\n"
    "- If the user's question is a greeting or general chat that "
    "does not require tool lookups → answer directly.\n\n"

    "=== RESPONSE FORMAT ===\n\n"
    "You have two routing tools available:\n"
    "- route_to_pdf(reason) — delegate to the PDF specialist.\n"
    "- route_to_csv(reason) — delegate to the CSV specialist.\n\n"
    "Call the appropriate routing tool when you need a specialist. "
    "When you can answer the user directly (greetings, general chat), "
    "respond with plain text (do NOT call a tool).\n"
)

PDF_PROMPT = (
    "You are the PDF Documentation specialist for an Access "
    "Governance assistant. You answer the user directly.\n\n"

    "=== AVAILABLE TOOLS ===\n\n"
    "- list_topics — lists every available documentation topic and "
    "its document paths. Call this first if you have not seen the "
    "topic structure yet in this conversation.\n"
    "- search_docs(query) — full-text search across all documents. "
    "Returns ranked results with snippets and page_path values.\n"
    "- read_page(page_path) — retrieves the complete text of a "
    "document. The text contains [Page N] markers so you can "
    "identify exactly which PDF page each piece of information "
    "comes from.\n"
    "- hand_off_to_router(reason) — hand the conversation back to "
    "the router if the user's question is outside your expertise.\n\n"

    "=== WHEN TO HAND OFF ===\n\n"
    "If the user asks ONLY about specific access rights, entitlements, "
    "peer recommendations, data lookups, or organisational data "
    "(and nothing documentation-related), call hand_off_to_router "
    "with the reason. These questions belong to the CSV Data "
    "specialist.\n\n"

    "=== MIXED QUESTIONS (CRITICAL) ===\n\n"
    "If the user's message contains BOTH a documentation question AND "
    "a data question (e.g. 'How do I set up delegations? Also, what "
    "access do I need?'), you MUST:\n"
    "1. Answer the documentation part FIRST — call search_docs, "
    "read_page, etc. as normal and provide a full cited answer.\n"
    "2. In your final answer, tell the user: 'For the data part of "
    "your question (e.g. specific access rights), please ask me "
    "separately so I can route it to the right specialist.'\n"
    "3. Do NOT call hand_off_to_router for mixed questions. If you "
    "call hand_off_to_router alongside your search tools, all your "
    "tool calls will be cancelled and the user will get no answer.\n\n"

    "=== SEARCH STRATEGY ===\n\n"
    "1. Call search_docs with the user's question (try different "
    "phrasings if the first search returns few results).\n"
    "2. For every relevant result, call read_page to get the full "
    "content — snippets from search_docs are too short for a "
    "thorough answer.\n"
    "3. Read the [Page N] markers in the returned text to identify "
    "the exact pages that contain the answer.\n"
    "4. Synthesise a clear answer and cite every fact with its "
    "document and page number.\n"
    "5. If the answer spans multiple documents, read each one and "
    "combine the information.\n\n"

    "=== CITATIONS (MANDATORY) ===\n\n"
    "Every claim sourced from documentation MUST include an inline "
    "citation: (Source: <page_path>, Page <N>)\n\n"
    "Examples:\n"
    "- (Source: entitlements/ordering_faq.pdf, Page 2)\n"
    "- (Source: delegations/setup_guide.pdf, Pages 3-4)\n\n"
    "Rules:\n"
    "- Cite immediately after each fact or paragraph.\n"
    "- If information spans multiple pages, cite the range.\n"
    "- If multiple documents are used, cite each one where "
    "referenced.\n"
    "- ALWAYS call read_page to get full content — search snippets "
    "alone are not sufficient for accurate page-level citations.\n"
    "- Never omit citations for documentation-sourced information.\n\n"

    "=== OUTPUT ===\n\n"
    "Provide your answer directly to the user with full citations. "
    "Be concise but thorough. If the documentation does not cover "
    "the user's question, say so clearly.\n"
)

CSV_PROMPT = (
    "You are the CSV Data specialist for an Access Governance "
    "assistant. You answer the user directly.\n\n"

    "=== AVAILABLE DATASETS ===\n\n"
    "Two CSV datasets provide structured data for access-rights "
    "discovery:\n\n"

    "1. Entitlements — each row is a person-to-resource assignment.\n"
    "   Key columns:\n"
    "   - ResourceID: the access right identifier (join key to "
    "Resources)\n"
    "   - JOBTITLE: the person's job title (PRIMARY search "
    "criterion — people with the same job title typically need the "
    "same access rights)\n"
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
    "   - ResourceType / RequestingSystem: classification and "
    "owning system\n\n"

    "=== AVAILABLE TOOLS ===\n\n"
    "- list_datasets — shows datasets, column names, and row "
    "counts. Call this first if you have not seen the dataset "
    "structure yet in this conversation.\n"
    "- search_dataset(dataset, query) — free-text BM25 search "
    "across all columns. Best for Resources (descriptions).\n"
    "- filter_dataset_fuzzy(dataset, filters) — regex pattern "
    "matching on specific columns (case-insensitive). Use for "
    "broad discovery with partial or approximate terms. Supports "
    "regex: \"finance|accounting\" matches either term.\n"
    "- filter_dataset(dataset, filters) — filter by exact column "
    "values (case-insensitive). Use when you know the precise "
    "value.\n"
    "- count_by_column(dataset, column, filters) — filter rows by "
    "exact values, then count occurrences of each distinct value "
    "in column. Returns [{value, count}] sorted descending. Use "
    "for peer recommendations after identifying exact filter "
    "values.\n"
    "- get_column_values(dataset, column) — list all distinct "
    "values in a column. Useful for small-cardinality columns.\n"
    "- hand_off_to_router(reason) — hand the conversation back to "
    "the router if the user's question is outside your expertise.\n\n"

    "=== WHEN TO HAND OFF ===\n\n"
    "If the user asks ONLY about how something works, processes, "
    "policies, procedures, FAQs, or how-to guides (and nothing "
    "data-related), call hand_off_to_router with the reason. "
    "These questions belong to the PDF Documentation specialist.\n\n"

    "=== MIXED QUESTIONS (CRITICAL) ===\n\n"
    "If the user's message contains BOTH a data question AND a "
    "documentation question (e.g. 'What access do my peers have? "
    "Also, how does the approval process work?'), you MUST:\n"
    "1. Answer the data part FIRST — call your data tools as "
    "normal and provide a full answer.\n"
    "2. In your final answer, tell the user: 'For the documentation "
    "part of your question (e.g. processes, how-to), please ask me "
    "separately so I can route it to the right specialist.'\n"
    "3. Do NOT call hand_off_to_router for mixed questions. If you "
    "call hand_off_to_router alongside your data tools, all your "
    "tool calls will be cancelled and the user will get no answer.\n\n"

    "=== MANDATORY MINIMUM CRITERIA FOR PEER RECOMMENDATIONS ===\n\n"
    "Before running peer-based entitlement searches (Strategy 1), "
    "you MUST have ALL of the following from the conversation:\n"
    "  1. JOBTITLE — always required, no exceptions.\n"
    "  2. At least ONE of:\n"
    "     - OU (organisational unit)\n"
    "     - ParentOU (parent organisational unit)\n"
    "     - One business hierarchy value: AREANAME, SECTORNAME, "
    "SEGMENTNAME, or FUNCTIONNAME\n\n"
    "If these criteria are missing, ask the user directly for "
    "the missing information. Do NOT proceed with peer "
    "recommendations without these criteria.\n\n"
    "This restriction does NOT apply to exploratory queries "
    "(Strategy 3) such as listing column values, browsing "
    "datasets, or helping the user discover their own attributes.\n\n"

    "=== SEARCH STRATEGIES ===\n\n"

    "Strategy 1 — Peer-based recommendations (most common):\n"
    "  a. Ensure mandatory criteria are present (JOBTITLE + at "
    "least one of OU/ParentOU/hierarchy).\n"
    "  b. Discovery — use filter_dataset_fuzzy on Entitlements for "
    "broad terms. Review distinct JOBTITLE values to identify the "
    "exact peer group. Confirm with user if multiple titles match.\n"
    "  c. Counting — use count_by_column on Entitlements, grouping "
    "by ResourceID with exact filters. Returns ResourceIDs ranked "
    "by peer count.\n"
    "  d. If too few results, broaden progressively: ParentOU "
    "instead of OU, drop OU and keep hierarchy, try broader "
    "hierarchy level. Never drop JOBTITLE.\n"
    "  e. For top ResourceIDs, call search_dataset on Resources "
    "for names and descriptions.\n"
    "  f. Present as table: ResourceID, Resource Name, Resource "
    "Description, Peer Count. Sort by Peer Count descending.\n\n"

    "Strategy 2 — Search by description:\n"
    "  a. search_dataset on Resources with the description.\n"
    "  b. Present matches with name, description, "
    "RequestingSystem.\n\n"

    "Strategy 3 — Explore the organisation:\n"
    "  - Low-cardinality columns (OU, AREANAME, etc.): use "
    "get_column_values to list options.\n"
    "  - High-cardinality columns (JOBTITLE, CITY, etc.): use "
    "filter_dataset_fuzzy with a partial term.\n"
    "  Then proceed with Strategy 1 or 2.\n\n"

    "=== RULES ===\n\n"
    "- Use filter_dataset_fuzzy for broad discovery; filter_dataset "
    "and count_by_column for precise queries.\n"
    "- ResourceID joins the two datasets. Always look up Resources "
    "for names/descriptions — never show raw ResourceIDs.\n"
    "- If a fuzzy filter returns too many results, add more filter "
    "columns or use a more specific pattern.\n"
    "- If a fuzzy filter returns nothing, try a broader pattern or "
    "fewer filter columns.\n\n"

    "=== OUTPUT ===\n\n"
    "Provide your answer directly to the user. Be concise but "
    "thorough. If the data does not cover the user's question, "
    "say so clearly.\n\n"
    "For peer-recommendation results, present a table with these "
    "columns: ResourceID, Resource Name, Resource Description, "
    "Peer Count. Sort by Peer Count descending.\n"
)


# ── Helpers ──────────────────────────────────────────────────────────

def _trim_messages(messages: list) -> list:
    """Trim conversation history to the most recent messages.

    The checkpointer stores the full history, but the LLM only sees
    the most recent ``KEEP_LAST_N`` messages.  This prevents
    context-window overflow and attention dilution in long sessions.
    """
    if KEEP_LAST_N > 0 and len(messages) > KEEP_LAST_N:
        messages = messages[-KEEP_LAST_N:]
        # Drop orphaned ToolMessages at the start of the window —
        # their preceding AIMessage (with tool_calls) was trimmed
        # away, and the OpenAI API rejects tool-role messages without
        # a prior tool_calls.
        while messages and isinstance(messages[0], ToolMessage):
            messages = messages[1:]
    return messages


# ── Node factories ───────────────────────────────────────────────────

def _make_router_node(llm, routing_tools):
    """Create the router node — decides PDF, CSV, or direct answer.

    When routing to a specialist, sets ``active_agent`` so subsequent
    turns skip the router and go directly to that specialist.
    """
    llm_with_tools = llm.bind_tools(routing_tools)

    def router_node(state: AgentState) -> dict:
        messages = _trim_messages(state["messages"])
        response = llm_with_tools.invoke(
            [SystemMessage(content=ROUTER_PROMPT)] + messages
        )
        result = [response]
        active_agent = ""
        for tc in response.tool_calls:
            result.append(
                ToolMessage(
                    content=tc["args"].get("reason", ""),
                    tool_call_id=tc["id"],
                )
            )
            if tc["name"] == "route_to_pdf":
                active_agent = "pdf_agent"
            elif tc["name"] == "route_to_csv":
                active_agent = "csv_agent"
        return {"messages": result, "active_agent": active_agent}

    return router_node


def _make_agent_node(llm, tools, prompt):
    """Create a specialist agent node (PDF or CSV).

    The returned node binds *tools* to the LLM so it can produce
    ``tool_calls``.  Actual tool execution happens in a separate
    ``ToolNode`` — this node only calls the LLM.
    """
    llm_with_tools = llm.bind_tools(tools)

    def agent_node(state: AgentState) -> dict:
        messages = _trim_messages(state["messages"])
        response = llm_with_tools.invoke(
            [SystemMessage(content=prompt)] + messages
        )
        return {"messages": [response]}

    return agent_node


# ── Handoff node ─────────────────────────────────────────────────────

def _handoff_node(state: AgentState) -> dict:
    """Process a hand_off_to_router tool call and clear the active agent.

    Produces a ToolMessage for every pending tool call so the message
    history stays valid, then resets ``active_agent`` to ``""`` so the
    router takes over on the next step.
    """
    last_msg = state["messages"][-1]
    result = []
    for tc in last_msg.tool_calls:
        if tc["name"] == "hand_off_to_router":
            content = tc["args"].get("reason", "Routing to another specialist.")
        else:
            content = "Tool call cancelled due to handoff."
        result.append(ToolMessage(content=content, tool_call_id=tc["id"]))
    return {"messages": result, "active_agent": ""}


# ── Custom tool node ─────────────────────────────────────────────────

def _make_tool_node(domain_tools):
    """Create a tool node that gracefully handles mixed domain + handoff calls.

    When the specialist calls both domain tools and ``hand_off_to_router``
    in a single response, this node:
      - executes the domain tools normally via ``ToolNode``
      - stubs ``hand_off_to_router`` with a directive telling the
        specialist to answer its part and inform the user about the rest

    This prevents the ToolNode from failing on the unknown handoff tool.
    """
    base_node = ToolNode(domain_tools)
    domain_names = {t.name for t in domain_tools}

    def node(state: AgentState) -> dict:
        last_msg = state["messages"][-1]
        handoff_calls = [
            tc for tc in last_msg.tool_calls
            if tc["name"] == "hand_off_to_router"
        ]
        domain_calls = [
            tc for tc in last_msg.tool_calls
            if tc["name"] in domain_names
        ]

        if not handoff_calls:
            # No mixed calls — run all tools normally.
            return base_node.invoke(state)

        # Mixed calls: execute domain tools only, stub the handoff.
        # Build a modified AIMessage containing only domain tool_calls
        # so the base ToolNode can process them without errors.
        modified_msg = AIMessage(
            content=last_msg.content,
            tool_calls=domain_calls,
            id=last_msg.id,
        )
        modified_state = {**state, "messages": list(state["messages"])[:-1] + [modified_msg]}
        result = base_node.invoke(modified_state)

        # Add stub responses for the handoff calls.
        for tc in handoff_calls:
            result["messages"].append(
                ToolMessage(
                    content=(
                        "Handoff was deferred because you also called "
                        "domain tools. Answer the part within your "
                        "expertise using the tool results above, then "
                        "tell the user to ask separately about the part "
                        "outside your expertise."
                    ),
                    tool_call_id=tc["id"],
                )
            )

        return result

    return node


# ── Routing / edge functions ─────────────────────────────────────────

def route_entry(state: AgentState) -> str:
    """Route from START: skip the router if a specialist already owns
    the conversation."""
    active = state.get("active_agent", "")
    if active in ("pdf_agent", "csv_agent"):
        return active
    return "router"


def route_from_router(state: AgentState) -> str:
    """Determine where to go after the router node.

    The router node appends a ToolMessage after any routing tool call,
    so the last message is either:
      - a ToolMessage (routing decision) → check the preceding AIMessage
      - an AIMessage with no tool_calls (direct answer) → END
    """
    messages = state["messages"]
    ai_msg = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    if ai_msg is not None and ai_msg.tool_calls:
        tool_name = ai_msg.tool_calls[0]["name"]
        if tool_name == "route_to_pdf":
            return "pdf_agent"
        if tool_name == "route_to_csv":
            return "csv_agent"
    # No tool call → direct answer; end the graph turn.
    return END


def _make_specialist_edge(tool_node_name: str):
    """Return an edge function for a specialist node.

    Routing logic:
      - hand_off_to_router as the *only* tool call → ``"handoff"`` node
      - MCP tool calls (possibly mixed with hand_off_to_router) →
        *tool_node_name* — domain tools take priority so the specialist
        can answer its part of a mixed question
      - no tool calls (final answer) → ``END``
    """

    def check(state: AgentState) -> str:
        last_msg = state["messages"][-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            has_handoff = any(
                tc["name"] == "hand_off_to_router"
                for tc in last_msg.tool_calls
            )
            has_domain = any(
                tc["name"] != "hand_off_to_router"
                for tc in last_msg.tool_calls
            )
            if has_handoff and not has_domain:
                # Pure handoff — no domain work to do.
                return "handoff"
            # Domain tools present (possibly alongside a stray handoff
            # call).  Route to the tool node so the specialist can
            # answer the part within its expertise.
            return tool_node_name
        return END

    return check


# ── Graph builder ────────────────────────────────────────────────────

def build_graph(llm, all_tools):
    """Build the StateGraph with persistent specialist ownership.

    Graph structure::

        START ──(active_agent?)──▶ pdf_agent ↔ pdf_tools ──▶ END
                       │               │
                       │               └──▶ handoff ──▶ router
                       │                                  │
                       ├──▶ router ──(route)──▶ ...       │
                       │       │                          │
                       │       └──▶ END                   │
                       │                                  │
                       └──────────▶ csv_agent ↔ csv_tools ──▶ END
                                       │
                                       └──▶ handoff ──▶ router

    Once the router assigns a specialist, ``active_agent`` is set in
    the state.  On subsequent user turns, START routes directly to
    that specialist, bypassing the router entirely.  The specialist
    only hands back to the router via ``hand_off_to_router`` when the
    user switches context.
    """
    # Split MCP tools into PDF and CSV groups.
    pdf_tools = [t for t in all_tools if t.name in PDF_TOOL_NAMES]
    csv_tools = [t for t in all_tools if t.name in CSV_TOOL_NAMES]

    # Each specialist gets its MCP tools + the handoff tool.
    pdf_all_tools = pdf_tools + [hand_off_to_router]
    csv_all_tools = csv_tools + [hand_off_to_router]

    graph = StateGraph(AgentState)

    # ── Nodes ──
    graph.add_node("router", _make_router_node(llm, ROUTING_TOOLS))

    graph.add_node("pdf_agent", _make_agent_node(llm, pdf_all_tools, PDF_PROMPT))
    graph.add_node("pdf_tools", _make_tool_node(pdf_tools))

    graph.add_node("csv_agent", _make_agent_node(llm, csv_all_tools, CSV_PROMPT))
    graph.add_node("csv_tools", _make_tool_node(csv_tools))

    graph.add_node("handoff", _handoff_node)

    # ── Edges ──

    # Entry: check if a specialist already owns the conversation.
    graph.add_conditional_edges(
        START,
        route_entry,
        {"router": "router", "pdf_agent": "pdf_agent", "csv_agent": "csv_agent"},
    )

    # Router decides which specialist to activate.
    graph.add_conditional_edges(
        "router",
        route_from_router,
        {"pdf_agent": "pdf_agent", "csv_agent": "csv_agent", END: END},
    )

    # PDF sub-loop: agent → tools → agent → … → END or handoff.
    graph.add_conditional_edges(
        "pdf_agent",
        _make_specialist_edge("pdf_tools"),
        {"pdf_tools": "pdf_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("pdf_tools", "pdf_agent")

    # CSV sub-loop: agent → tools → agent → … → END or handoff.
    graph.add_conditional_edges(
        "csv_agent",
        _make_specialist_edge("csv_tools"),
        {"csv_tools": "csv_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("csv_tools", "csv_agent")

    # Handoff returns to router for re-routing.
    graph.add_edge("handoff", "router")

    return graph


# ── MCP config ───────────────────────────────────────────────────────

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


# ── History dump ─────────────────────────────────────────────────────

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


# ── Interactive loop ─────────────────────────────────────────────────

async def run_agent_loop(on_response=None):
    """Run the interactive agent loop.

    The loop runs until the user types ``exit`` or presses Ctrl-C.

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
    graph = build_graph(llm, tools)
    agent = graph.compile(checkpointer=checkpointer)

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
            # The last message is the specialist's or router's answer.
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

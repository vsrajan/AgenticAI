"""LangGraph custom StateGraph agent with Router, Knowledgebase, and Resource nodes.

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
from agent_client.llm import get_llm

logger = logging.getLogger("agent_client.agent")


# ── Spinner ──────────────────────────────────────────────────────────

class Spinner:
    """Animated terminal spinner with a mutable status message.

    The spinner runs as a background asyncio task and can be updated
    in-flight to reflect which phase the agent is in (routing, calling
    tools, generating the answer, etc.).

    Usage::

        spinner = Spinner("Thinking")
        await spinner.start()
        # ... later ...
        spinner.update("Calling tools")
        # ... later ...
        await spinner.stop()
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking") -> None:
        self._message = message
        self._task: asyncio.Task | None = None
        self._max_len = len(message)  # track widest message for clean overwrite

    def update(self, message: str) -> None:
        """Change the spinner label while it is running."""
        self._message = message
        if len(message) > self._max_len:
            self._max_len = len(message)

    async def _spin(self) -> None:
        write = sys.stderr.write
        flush = sys.stderr.flush
        try:
            for frame in itertools.cycle(self._FRAMES):
                text = f"\r{frame} {self._message}…"
                # Pad to max width so shorter messages fully overwrite longer ones.
                write(text.ljust(self._max_len + 4))
                flush()
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            write("\r" + " " * (self._max_len + 4) + "\r")
            flush()

    async def start(self) -> None:
        """Start the spinner background task."""
        self._task = asyncio.create_task(self._spin())

    async def stop(self) -> None:
        """Stop the spinner and clear its line."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def __aenter__(self) -> "Spinner":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()


# ── Configuration ────────────────────────────────────────────────────

# Maximum number of messages to keep in conversation history.
# Each turn can produce several messages (human, tool calls, tool
# results, assistant answer), so 20 ≈ 3-4 full turns of context.
# Set to 0 to disable trimming (unlimited history).
KEEP_LAST_N = int(os.environ.get("KEEP_LAST_N_MSGS", "20"))

# Tool name sets used for splitting MCP tools into groups.
KNOWLEDGEBASE_TOOL_NAMES = {"list_topics", "search_docs", "read_page"}
RESOURCE_TOOL_NAMES = {
    "list_datasets",
    "search_dataset",
    "filter_dataset",
    "filter_dataset_fuzzy",
    "count_by_column",
    "get_column_values",
    "get_request_attributes",
    "raise_entitlement_request",
}


# ── State ────────────────────────────────────────────────────────────

class AgentState(MessagesState):
    """Extended state that tracks which specialist owns the conversation."""
    active_agent: str  # "knowledgebase_agent", "resource_agent", or "" (use router)


# ── Routing tools (used by the router) ───────────────────────────────

@tool
def route_to_knowledgebase(reason: str) -> str:
    """Route the query to the Knowledgebase specialist agent."""
    return reason


@tool
def route_to_resource(reason: str) -> str:
    """Route the query to the Resource specialist agent."""
    return reason


ROUTING_TOOLS = [route_to_knowledgebase, route_to_resource]


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
    "1. Knowledgebase Agent — answers questions about how Access "
    "Governance works: processes, procedures, FAQs, how-to guides, "
    "and policies.\n"
    "2. Resource Agent — handles structured data lookups: access "
    "rights catalogues, entitlement records, peer-based "
    "recommendations, and organisational data.\n\n"

    "=== DECISION RULES ===\n\n"
    "- Questions about how something works, processes, policies, "
    "procedures, FAQs → route to Knowledgebase.\n"
    "- Questions about specific access rights, peer recommendations, "
    "data lookups, entitlements, organisational data → route to Resource.\n"
    "- If the user's question is a greeting or general chat that "
    "does not require tool lookups → answer directly.\n\n"

    "=== RESPONSE FORMAT ===\n\n"
    "You have two routing tools available:\n"
    "- route_to_knowledgebase(reason) — delegate to the Knowledgebase specialist.\n"
    "- route_to_resource(reason) — delegate to the Resource specialist.\n\n"
    "Call the appropriate routing tool when you need a specialist. "
    "When you can answer the user directly (greetings, general chat), "
    "respond with plain text (do NOT call a tool).\n"
)

KNOWLEDGEBASE_PROMPT = (
    "You are the Knowledgebase specialist for an Access "
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
    "with the reason. These questions belong to the Resource "
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

RESOURCE_PROMPT = (
    "You are the Resource specialist for an Access Governance "
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
    "- count_by_column(dataset, column, filters, fuzzy) — PREFERRED "
    "for discovery queries. Filters rows then counts occurrences "
    "of each distinct value in column. Returns compact [{value, "
    "count}] sorted descending. Set fuzzy=True for regex pattern "
    "matching on filter values (e.g. 'what segments match TISO?'). "
    "Use this instead of filter tools whenever you need counts or "
    "lists of matching values.\n"
    "- get_column_values(dataset, column) — list all distinct "
    "values in a column. Useful for small-cardinality columns.\n"
    "- filter_dataset_fuzzy(dataset, filters, max_results) — regex "
    "pattern matching on specific columns (case-insensitive). "
    "Returns up to max_results full rows (default 100). Use ONLY "
    "when you need actual row-level data. For discovery/counting, "
    "prefer count_by_column with fuzzy=True instead.\n"
    "- filter_dataset(dataset, filters, max_results) — filter by "
    "exact column values (case-insensitive). Returns up to "
    "max_results full rows (default 100). Use when you need "
    "precise row data and know exact filter values.\n"
    "- get_request_attributes — returns the schema of attributes "
    "needed to raise an entitlement request. Call this first when "
    "the user wants to request access.\n"
    "- raise_entitlement_request(resource_id, justification, "
    "start_date, end_date) — submit an entitlement access request. "
    "Requires resource_id and justification; start_date and "
    "end_date are optional.\n"
    "- hand_off_to_router(reason) — hand the conversation back to "
    "the router if the user's question is outside your expertise.\n\n"

    "=== WHEN TO HAND OFF ===\n\n"
    "If the user asks ONLY about how something works, processes, "
    "policies, procedures, FAQs, or how-to guides (and nothing "
    "data-related), call hand_off_to_router with the reason. "
    "These questions belong to the Knowledgebase specialist.\n\n"

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
    "  b. Discovery — use count_by_column with fuzzy=True on "
    "Entitlements to discover matching values. For example, to find "
    "job titles matching 'analyst', call count_by_column(dataset="
    "'Entitlements', column='JOBTITLE', filters={'JOBTITLE': "
    "'analyst'}, fuzzy=True). This returns a compact list of "
    "matching titles with counts. Confirm with user if multiple "
    "titles match.\n"
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
    "count_by_column with fuzzy=True to discover matching values "
    "with counts (e.g. count_by_column(dataset='Entitlements', "
    "column='SEGMENTNAME', filters={'SEGMENTNAME': 'tiso'}, "
    "fuzzy=True)).\n"
    "  Then proceed with Strategy 1 or 2.\n\n"

    "Strategy 4 — Request access:\n"
    "  a. Call get_request_attributes to learn what fields are "
    "needed.\n"
    "  b. Collect the required information from the user "
    "(resource_id, justification, and any optional fields).\n"
    "  c. If the user doesn't know the ResourceID, help them find "
    "it first using Strategies 1-3.\n"
    "  d. Once all required fields are gathered, call "
    "raise_entitlement_request to submit.\n"
    "  e. Report the result (request ID, status) to the user.\n\n"

    "=== RULES ===\n\n"
    "- Use count_by_column (with fuzzy=True) for broad discovery "
    "and counting; use filter_dataset or filter_dataset_fuzzy ONLY "
    "when you need actual row data.\n"
    "- If a filter tool response includes _truncated=True, switch to "
    "count_by_column for a compact summary instead of increasing "
    "max_results.\n"
    "- ResourceID joins the two datasets. Always look up Resources "
    "for names/descriptions — never show raw ResourceIDs.\n"
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
    """Create the router node — decides Knowledgebase, Resource, or direct answer.

    When routing to a specialist, sets ``active_agent`` so subsequent
    turns skip the router and go directly to that specialist.
    """
    llm_with_tools = llm.bind_tools(routing_tools)

    async def router_node(state: AgentState) -> dict:
        messages = _trim_messages(state["messages"])
        response = await llm_with_tools.ainvoke(
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
            if tc["name"] == "route_to_knowledgebase":
                active_agent = "knowledgebase_agent"
            elif tc["name"] == "route_to_resource":
                active_agent = "resource_agent"
        return {"messages": result, "active_agent": active_agent}

    return router_node


def _make_agent_node(llm, tools, prompt):
    """Create a specialist agent node (Knowledgebase or Resource).

    The returned node binds *tools* to the LLM so it can produce
    ``tool_calls``.  Actual tool execution happens in a separate
    ``ToolNode`` — this node only calls the LLM.
    """
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict:
        messages = _trim_messages(state["messages"])
        response = await llm_with_tools.ainvoke(
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
    # Standalone tool executor — does NOT mutate the graph state.
    # ToolNode.invoke() takes a state dict, runs the tool calls found in it,
    # and returns an update dict (e.g. {"messages": [ToolMessage, ...]}).
    # The actual graph state is only updated later, when the graph runtime
    # merges this node's return value via reducers (e.g. add_messages).
    base_node = ToolNode(domain_tools)
    domain_names = {t.name for t in domain_tools}

    async def node(state: AgentState) -> dict:
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
            return await base_node.ainvoke(state)

        # Mixed calls: execute domain tools only, stub the handoff.
        # Build a modified AIMessage containing only domain tool_calls
        # so the base ToolNode can process them without errors.
        modified_msg = AIMessage(
            content=last_msg.content,
            tool_calls=domain_calls,
            id=last_msg.id,
        )
        # Unpack state and replace "messages" with a new list into modified_state,
        # so we don't mutate the real graph state (AgentState is a dict, passed by reference).
        modified_state = {
            **state,
            "messages": list(state["messages"])[:-1] + [modified_msg]
        }
        # Returns {"messages": [ToolMessage, ...]} — one per domain tool call.
        # This does not touch the graph state; it's just a local result dict.
        result = await base_node.ainvoke(modified_state)

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

        # The graph runtime will merge this return value into the real
        # state via reducers (add_messages appends to state["messages"]).
        return result

    return node


# ── Routing / edge functions ─────────────────────────────────────────

def route_entry(state: AgentState) -> str:
    """Route from START: skip the router if a specialist already owns
    the conversation."""
    active = state.get("active_agent", "")
    if active in ("knowledgebase_agent", "resource_agent"):
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
        if tool_name == "route_to_knowledgebase":
            return "knowledgebase_agent"
        if tool_name == "route_to_resource":
            return "resource_agent"
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

        START ──(active_agent?)──▶ knowledgebase_agent ↔ knowledgebase_tools ──▶ END
                       │                    │
                       │                    └──▶ handoff ──▶ router
                       │                                       │
                       ├──▶ router ──(route)──▶ ...            │
                       │       │                               │
                       │       └──▶ END                        │
                       │                                       │
                       └──────────▶ resource_agent ↔ resource_tools ──▶ END
                                          │
                                          └──▶ handoff ──▶ router

    Once the router assigns a specialist, ``active_agent`` is set in
    the state.  On subsequent user turns, START routes directly to
    that specialist, bypassing the router entirely.  The specialist
    only hands back to the router via ``hand_off_to_router`` when the
    user switches context.
    """
    # Split MCP tools into Knowledgebase and Resource groups.
    knowledgebase_tools = [t for t in all_tools if t.name in KNOWLEDGEBASE_TOOL_NAMES]
    resource_tools = [t for t in all_tools if t.name in RESOURCE_TOOL_NAMES]

    # Each specialist gets its MCP tools + the handoff tool.
    knowledgebase_all_tools = knowledgebase_tools + [hand_off_to_router]
    resource_all_tools = resource_tools + [hand_off_to_router]

    graph = StateGraph(AgentState)

    # ── Nodes ──
    graph.add_node("router", _make_router_node(llm, ROUTING_TOOLS))

    graph.add_node("knowledgebase_agent", _make_agent_node(llm, knowledgebase_all_tools, KNOWLEDGEBASE_PROMPT))
    graph.add_node("knowledgebase_tools", _make_tool_node(knowledgebase_tools))

    graph.add_node("resource_agent", _make_agent_node(llm, resource_all_tools, RESOURCE_PROMPT))
    graph.add_node("resource_tools", _make_tool_node(resource_tools))

    graph.add_node("handoff", _handoff_node)

    # ── Edges ──

    # Entry: check if a specialist already owns the conversation.
    graph.add_conditional_edges(
        START,
        route_entry,
        {"router": "router", "knowledgebase_agent": "knowledgebase_agent", "resource_agent": "resource_agent"},
    )

    # Router decides which specialist to activate.
    graph.add_conditional_edges(
        "router",
        route_from_router,
        {"knowledgebase_agent": "knowledgebase_agent", "resource_agent": "resource_agent", END: END},
    )

    # Knowledgebase sub-loop: agent → tools → agent → … → END or handoff.
    graph.add_conditional_edges(
        "knowledgebase_agent",
        _make_specialist_edge("knowledgebase_tools"),
        {"knowledgebase_tools": "knowledgebase_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("knowledgebase_tools", "knowledgebase_agent")

    # Resource sub-loop: agent → tools → agent → … → END or handoff.
    graph.add_conditional_edges(
        "resource_agent",
        _make_specialist_edge("resource_tools"),
        {"resource_tools": "resource_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("resource_tools", "resource_agent")

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
    """Write the full conversation history from the checkpointer to *LOG_FILE*.

    Each message is annotated with the graph node that produced it so
    that readers can trace the exact invocation flow.
    """
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return

        # Walk the checkpoint history (newest-first) and build a mapping
        # from message id → source node that produced it.
        snapshots = [s async for s in agent.aget_state_history(config)]
        snapshots.reverse()  # chronological order

        # Each snapshot's messages list is *cumulative* — it contains every
        # message that existed at that checkpoint, not just the new ones.
        # However, the snapshot metadata only records a single "source" node
        # (the node that produced that checkpoint).  To figure out which node
        # produced each *individual* message, we walk the snapshots in
        # chronological order and use a sliding-window diff:
        #
        #   prev_count tracks where the previous snapshot's messages ended,
        #   so snap_msgs[prev_count:] gives only the messages that were
        #   *added* by the current snapshot's source node.
        #
        #   Iteration | prev_count (start) | len(snap_msgs) | slice examined
        #   ----------|--------------------|-----------------|--------------
        #   Snap 0    | 0                  | 3               | msgs 0–2
        #   Snap 1    | 3                  | 5               | msgs 3–4
        #   Snap 2    | 5                  | 7               | msgs 5–6
        #
        # After the loop, msg_id_to_node maps every message id to the graph
        # node that produced it.
        msg_id_to_node: dict[str, str] = {}
        prev_count = 0
        for snap in snapshots:
            # metadata["source"] is always "input" or "loop" — not the
            # actual graph node.  The node name lives in the "writes"
            # dict, which maps node_name → state_update.
            writes = snap.metadata.get("writes") or {}
            if writes:
                node = next(iter(writes))          # first (usually only) key
            else:
                node = snap.metadata.get("source", "unknown")
            snap_msgs = snap.values.get("messages", [])
            for msg in snap_msgs[prev_count:]:
                msg_id_to_node[msg.id] = node

            prev_count = len(snap_msgs)

        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"Agent session log — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"Total messages: {len(messages)}\n")
            fh.write("=" * 60 + "\n\n")
            for msg in messages:
                role = msg.__class__.__name__
                node = msg_id_to_node.get(msg.id, "unknown")
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                fh.write(f"[{role}]  (node: {node})\n{content}\n")
                # Show tool calls so AIMessages with empty content are understandable.
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        fh.write(f"  ↳ tool_call: {tc['name']}({tc.get('args', {})})\n")
                fh.write("\n")
        logger.info("Session history written to %s (%d messages)", LOG_FILE, len(messages))
    except Exception:
        logger.exception("Failed to write session history to %s", LOG_FILE)


# ── Interactive loop ─────────────────────────────────────────────────

# Phase labels shown in the spinner for each graph node.
_NODE_PHASES = {
    "router": "Routing",
    "knowledgebase_agent": "Generating answer",
    "resource_agent": "Generating answer",
    "knowledgebase_tools": "Calling tools",
    "resource_tools": "Calling tools",
    "handoff": "Switching specialist",
}


async def run_agent_loop(on_response=None):
    """Run the interactive agent loop.

    The loop runs until the user types ``exit`` or presses Ctrl-C.

    Uses ``astream_events`` to stream the assistant's final answer
    token-by-token, so the user sees output progressively instead of
    waiting for the entire graph to complete.

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
            answer, streamed = await _stream_response(agent, user_input, config)
            logger.debug("Agent response: %s", answer)
            # Only call on_response if the answer was NOT already
            # streamed to stdout token-by-token.
            if not streamed:
                on_response(f"\n🤖 Assistant: {answer}\n")
        except Exception:
            logger.exception("Error processing query")
            on_response(
                "\nAssistant: Sorry, an error occurred while "
                "processing your question. Please try again.\n"
            )

    # Dump full (untrimmed) conversation history on exit.
    await _dump_history(agent, config)


async def _stream_response(agent, user_input: str, config: dict) -> tuple[str, bool]:
    """Run the agent graph with token-level streaming.

    Shows a phase-aware spinner while routing / executing tools,
    then streams the final answer token-by-token.

    Returns:
        (answer_text, was_streamed) — *was_streamed* is True when the
        answer was already written to stdout token-by-token.
    """
    spinner = Spinner("Thinking")
    await spinner.start()

    streaming = False   # True once the first answer token arrives
    answer_parts: list[str] = []

    try:
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=user_input)]},
            config,
            version="v2",
        ):
            kind = event["event"]

            # Update spinner label when a new graph node starts.
            if kind == "on_chain_start":
                node = event.get("metadata", {}).get("langgraph_node", "")
                phase = _NODE_PHASES.get(node)
                if phase and not streaming:
                    spinner.update(phase)

            # Stream text tokens from the LLM.  GPT-4o produces either
            # text content (final answer) or tool_call_chunks (tool
            # invocations), never both in the same chunk.  Filtering on
            # "has content, no tool_call_chunks" naturally selects only
            # the final-answer tokens.
            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                has_content = chunk.content
                has_tool_calls = getattr(chunk, "tool_call_chunks", None)

                if has_content and not has_tool_calls:
                    if not streaming:
                        streaming = True
                        await spinner.stop()
                        sys.stdout.write("\n🤖 Assistant: ")
                    sys.stdout.write(chunk.content)
                    sys.stdout.flush()
                    answer_parts.append(chunk.content)
    finally:
        await spinner.stop()

    if streaming:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # If streaming produced tokens, return the accumulated answer.
    # Otherwise fall back to reading the final state (e.g. if the LLM
    # didn't stream for some reason).
    if answer_parts:
        return "".join(answer_parts), True

    state = await agent.aget_state(config)
    messages = state.values.get("messages", [])
    return (messages[-1].content if messages else ""), False

"""LangGraph StateGraph agent with Router, Knowledgebase, Resource, and Quality nodes.

Connects to an already-running MCP server (stdio or SSE), loads tools,
and routes user queries through specialised nodes:

  START -> (active_agent?) -> specialist <-> tools -> END
               |                   |
               v                   v (handoff)
            router              handoff -> router
               |
               +-> END (greeting / chat)

Once the router assigns a specialist, that specialist owns the
conversation until the user switches context. The specialist hands
back to the router via hand_off_to_router only on a context switch.

The MCP server must be started separately.
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

logger = logging.getLogger("ease_clients.agent")


# -- Spinner --

class Spinner:
    """Animated terminal spinner shown while the agent is working.

    Runs as a background asyncio task. Update the label in-flight
    to reflect the current phase (routing, calling tools, etc.).
    """

    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Thinking") -> None:
        self._message = message
        self._task: asyncio.Task | None = None
        self._max_len = len(message)  # widest message seen, for clean overwrite

    def update(self, message: str) -> None:
        """Change the spinner label while running."""
        self._message = message
        if len(message) > self._max_len:
            self._max_len = len(message)

    async def _spin(self) -> None:
        write = sys.stderr.write
        flush = sys.stderr.flush
        try:
            for frame in itertools.cycle(self._FRAMES):
                text = f"\r{frame} {self._message}…"
                # pad to max width so shorter messages overwrite longer ones
                write(text.ljust(self._max_len + 4))
                flush()
                # yield control to the event loop so the agent graph can
                # run between animation frames -- without this await the
                # spinner would block the entire loop
                await asyncio.sleep(0.08)
        except asyncio.CancelledError:
            # cancel() in stop() schedules a CancelledError that lands
            # at the await asyncio.sleep above. Clear the spinner line
            # before exiting so subsequent output starts on a clean line.
            # \r moves cursor to column 0, spaces overwrite the widest
            # label ever shown (+4 covers the frame char, spacing, and
            # ellipsis), then a second \r resets the cursor to column 0
            # so the next print starts on a clean line.
            write("\r" + " " * (self._max_len + 4) + "\r")
            flush()

    async def start(self) -> None:
        """Start the spinner background task."""
        self._task = asyncio.create_task(self._spin())

    async def stop(self) -> None:
        """Stop the spinner and clear its line."""
        if self._task:
            # cancel() just sets a flag and returns immediately -- the
            # CancelledError hasn't been raised inside _spin yet
            self._task.cancel()
            # await the task so _spin's CancelledError handler (line
            # clearing) runs before we return. Awaiting a cancelled task
            # re-raises CancelledError to the caller, so we suppress it
            # here because the cancellation is intentional.
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


# -- Configuration --

# Max messages kept in context. Each turn produces several messages
# (human, tool calls, results, answer), so 20 ~ 3-4 full turns.
# Set to 0 to disable trimming.
KEEP_LAST_N = int(os.environ.get("KEEP_LAST_N_MSGS", "20"))

# Tool name sets for splitting MCP tools into groups.
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
QUALITY_TOOL_NAMES = {
    "get_quality_criteria",
    "filter_dataset",
    "filter_dataset_fuzzy",
    "search_dataset",
    "list_datasets",
    "get_column_values",
}


# -- State --

class AgentState(MessagesState):
    """Extended state that tracks which specialist owns the conversation."""
    active_agent: str  # "knowledgebase_agent", "resource_agent", "quality_agent", or "" (use router)


# -- Routing tools (used by the router) --

@tool
def route_to_knowledgebase(reason: str) -> str:
    """Route the query to the Knowledgebase specialist agent."""
    return reason


@tool
def route_to_resource(reason: str) -> str:
    """Route the query to the Resource specialist agent."""
    return reason


@tool
def route_to_quality_checker(reason: str) -> str:
    """Route the query to the Data Quality Checker specialist."""
    return reason


ROUTING_TOOLS = [route_to_knowledgebase, route_to_resource, route_to_quality_checker]


# -- Handoff tool (used by specialists) --

@tool
def hand_off_to_router(reason: str) -> str:
    """Hand the conversation back to the router because the user's
    question is outside your area of expertise."""
    return reason


# -- Prompts --

ROUTER_PROMPT = """\
You are the routing agent for an Access Governance assistant. Analyse the user's query and the conversation history, then decide the next step.

You have three specialist agents:
1. Knowledgebase Agent -- answers questions about how Access Governance works: processes, procedures, FAQs, how-to guides, and policies.
2. Resource Agent -- handles structured data lookups: access rights catalogues, entitlement records, peer-based recommendations, and organisational data.
3. Data Quality Checker -- evaluates the quality of resource metadata against a criteria checklist and assesses whether resources grant privileged access. Route here when the user asks to check, audit, or evaluate quality of specific resources, or asks whether a resource grants privileged or administrative access.

=== DECISION RULES ===

- Questions about how something works, processes, policies, procedures, FAQs -> route to Knowledgebase.
- Questions about specific access rights, peer recommendations, data lookups, entitlements, organisational data -> route to Resource.
- Questions about data quality, auditing resource metadata, checking completeness of resource records, or determining whether resources grant privileged/admin access -> route to Quality Checker.
- If the user's question is a greeting or general chat that does not require tool lookups -> answer directly.
- If the question belongs to a specialist but is missing details (role, team, location, resource names, ids), STILL route to that specialist -- specialists ask their own follow-up questions. Never gather requirements yourself.
- Example: "I'm a new joiner, what access do I need?" -> route_to_resource (an access/entitlement question, even though it is phrased with joiner vocabulary and gives no role details -- the Resource specialist will ask for the details it needs). Questions about the joining PROCESS itself ("what happens when I join", "how does onboarding work") -> route_to_knowledgebase.

=== RESPONSE FORMAT ===

You have three routing tools available:
- route_to_knowledgebase(reason) -- delegate to the Knowledgebase specialist.
- route_to_resource(reason) -- delegate to the Resource specialist.
- route_to_quality_checker(reason) -- delegate to the Data Quality Checker.

Call the appropriate routing tool when you need a specialist. When you can answer the user directly (greetings, general chat), respond with plain text (do NOT call a tool).

Never tell the user you are routing, forwarding, or escalating their request. Routing happens ONLY by calling a routing tool and is invisible to the user. If you respond with plain text instead, that text must be a complete direct answer that claims no actions.
"""

KNOWLEDGEBASE_PROMPT = """\
You are the Knowledgebase specialist for an Access Governance assistant. You answer the user directly.

=== AVAILABLE TOOLS ===

- list_topics — lists every available documentation topic and its document paths. Call this first if you have not seen the topic structure yet in this conversation.
- search_docs(query) — full-text search across all documents. Returns ranked results with snippets and page_path values.
- read_page(page_path) — retrieves the complete text of a document. The text contains [Page N] markers so you can identify exactly which PDF page each piece of information comes from.
- hand_off_to_router(reason) — hand the conversation back to the router if the user's question is outside your expertise.

=== WHEN TO HAND OFF ===

If the user asks ONLY about specific access rights, entitlements, peer recommendations, data lookups, or organisational data (and nothing documentation-related), call hand_off_to_router with the reason. These questions belong to the Resource specialist.

=== MIXED QUESTIONS (CRITICAL) ===

If the user's message contains BOTH a documentation question AND a data question (e.g. 'How do I set up delegations? Also, what access do I need?'), you MUST:
1. Answer the documentation part FIRST — call search_docs, read_page, etc. as normal and provide a full cited answer.
2. In your final answer, tell the user: 'For the data part of your question (e.g. specific access rights), please ask me separately so I can route it to the right specialist.'
3. Do NOT call hand_off_to_router for mixed questions. If you call hand_off_to_router alongside your search tools, all your tool calls will be cancelled and the user will get no answer.

=== SEARCH STRATEGY ===

1. Call search_docs with the user's question (try different phrasings if the first search returns few results).
2. For every relevant result, call read_page to get the full content — snippets from search_docs are too short for a thorough answer.
3. Read the [Page N] markers in the returned text to identify the exact pages that contain the answer.
4. Synthesise a clear answer and cite every fact with its document and page number.
5. If the answer spans multiple documents, read each one and combine the information.

=== CITATIONS (MANDATORY) ===

Every claim sourced from documentation MUST include an inline citation: (Source: <page_path>, Page <N>)

Examples:
- (Source: entitlements/ordering_faq.pdf, Page 2)
- (Source: delegations/setup_guide.pdf, Pages 3-4)

Rules:
- Cite immediately after each fact or paragraph.
- If information spans multiple pages, cite the range.
- If multiple documents are used, cite each one where referenced.
- ALWAYS call read_page to get full content — search snippets alone are not sufficient for accurate page-level citations.
- Never omit citations for documentation-sourced information.

=== OUTPUT ===

Provide your answer directly to the user with full citations. Be concise but thorough. If the documentation does not cover the user's question, say so clearly.
"""

RESOURCE_PROMPT = """\
You are the Resource specialist for an Access Governance assistant. You answer the user directly.

=== AVAILABLE DATASETS ===

Two datasets provide structured data for access-rights discovery:

1. Entitlements — each row is a person-to-resource assignment.
   Key columns:
   - EmployeeID: the person's GPN, the firm-wide employee identifier.
     Exact-match key for a single person (see Strategy 0).
   - ResourceID: the access right identifier (join key to Resources)
   - JOBTITLE: the person's job title (PRIMARY search criterion — people with the same job title typically need the same access rights)
   - Business hierarchy — PREFERRED for scoping, broadest to most specific:
     AREANAME > SECTORNAME > SEGMENTNAME > FUNCTIONNAME
   - Financial hierarchy — a SEPARATE dimension: OU is a FINANCIAL unit
     (NOT a business unit), ParentOU is its parent. Use only when the user
     prefers to search that way, or only knows their OU.
   - CITY / COUNTRY_VALUE: location
   - C_EMPLOYEECLASS: employee class (e.g. External Staff)

   The business and financial hierarchies are DIFFERENT things. Never describe an OU as a business unit, and never broaden from one hierarchy into the other — pick one dimension and stay in it.

   There are NO person names in this data. If a user names a colleague but does not know their GPN, say that the GPN is required — do not guess at a person.

2. Resources — the access-rights catalogue.
   Key columns:
   - ResourceID: unique identifier (join key to Entitlements)
   - name: human-readable name of the access right
   - DESCRIPTION: what the access right grants
   - ResourceType / RequestingSystem: classification and owning system

=== AVAILABLE TOOLS ===

- list_datasets — shows datasets, column names, and row counts. Call this first if you have not seen the dataset structure yet in this conversation.
- search_dataset(dataset, query) — free-text BM25 search across all columns. Best for Resources (descriptions).
- count_by_column(dataset, column, filters, fuzzy) — PREFERRED for discovery queries. Filters rows then counts occurrences of each distinct value in column. Returns compact [{value, count}] sorted descending. Set fuzzy=True for regex pattern matching on filter values (e.g. 'what segments match TISO?'). Use this instead of filter tools whenever you need counts or lists of matching values.
- get_column_values(dataset, column) — list all distinct values in a column. Useful for small-cardinality columns.
- filter_dataset_fuzzy(dataset, filters, max_results) — regex pattern matching on specific columns (case-insensitive). Returns up to max_results full rows (default 100). Use ONLY when you need actual row-level data. For discovery/counting, prefer count_by_column with fuzzy=True instead.
- filter_dataset(dataset, filters, max_results) — filter by exact column values (case-insensitive). Returns up to max_results full rows (default 100). Use when you need precise row data and know exact filter values.
- get_request_attributes — returns the schema of attributes needed to raise an entitlement request. Call this first when the user wants to request access.
- raise_entitlement_request(resource_id, justification, start_date, end_date) — submit an entitlement access request. Requires resource_id and justification; start_date and end_date are optional.
- hand_off_to_router(reason) — hand the conversation back to the router if the user's question is outside your expertise.

=== WHEN TO HAND OFF ===

If the user asks ONLY about how something works, processes, policies, procedures, FAQs, or how-to guides (and nothing data-related), call hand_off_to_router with the reason. These questions belong to the Knowledgebase specialist.

=== MIXED QUESTIONS (CRITICAL) ===

If the user's message contains BOTH a data question AND a documentation question (e.g. 'What access do my peers have? Also, how does the approval process work?'), you MUST:
1. Answer the data part FIRST — call your data tools as normal and provide a full answer.
2. In your final answer, tell the user: 'For the documentation part of your question (e.g. processes, how-to), please ask me separately so I can route it to the right specialist.'
3. Do NOT call hand_off_to_router for mixed questions. If you call hand_off_to_router alongside your data tools, all your tool calls will be cancelled and the user will get no answer.

=== MINIMUM CRITERIA FOR PEER RECOMMENDATIONS ===

Peer searches need TWO dimensions, and a PARTIAL or APPROXIMATE term is enough to start — resolving it to exact values is YOUR job (Step R below):

  1. A job title — required, but 'engineer', 'analyst', 'risk manager' all qualify.
  2. At least ONE scope term, in this order of preference:
     a. A business hierarchy term — AREANAME, SECTORNAME, SEGMENTNAME or FUNCTIONNAME. PREFER this. The user does NOT need to know which level their term belongs to.
     b. A financial term — OU or ParentOU — when the user prefers that view, or only knows their OU.
     Use one dimension or the other, never both at once.

A colleague's GPN (EmployeeID) satisfies BOTH dimensions on its own — see Strategy 0.

Ask the user only when a dimension is entirely ABSENT (no job title at all, or no scope at all). Never ask the user to supply an exact value from the data — that is your job: run Step R and offer them the matching values to choose from.

Do NOT run the final peer count on an unresolved term. Resolve first, then count on confirmed values — a fuzzy peer count silently mixes different populations.

This section does NOT apply to exploratory queries (Strategy 3) such as listing column values, browsing datasets, or helping the user discover their own attributes.

=== STEP R — RESOLVING A PARTIAL TERM TO EXACT VALUES ===

Job title:
  count_by_column(dataset='Entitlements', column='JOBTITLE',
                  filters={'JOBTITLE': '<user term>'}, fuzzy=True)
Matching is case-insensitive and unanchored, so 'engineer' matches 'Senior Software Engineer'. You get back real titles with peer counts.

Scope term when the user does not know the level: the same call shape against each BUSINESS hierarchy column in turn — SEGMENTNAME, FUNCTIONNAME, SECTORNAME, AREANAME. Filters on different columns are ANDed, so use ONE column per call; never combine them hoping for an OR. Try OU / ParentOU only if the user asked to search by financial unit, or nothing in the business hierarchy matched and they confirm the term is an OU.

Then, by outcome:
- exactly 1 match — proceed, stating the assumption ("Using 'Senior Software Engineer' — 142 people").
- 2-10 matches — list them with counts and ask which apply; the user may pick several.
- more than 10 — show the top 10 by count and ask the user to narrow.
- 0 matches — try a shorter term, a different spelling, or another column before reporting nothing found; suggest what DOES exist (get_column_values on a low-cardinality column such as AREANAME).

Multi-select: when the user picks several values, filter with fuzzy=True and a regex alternation of the EXACT chosen values, e.g. {'JOBTITLE': 'Senior Software Engineer|Software Engineer'} — precise, but covers every chosen title.

=== HELPING USERS WHO DO NOT KNOW THEIR DETAILS (e.g. new joiners) ===

New joiners rarely know their business area, hierarchy level, or exact job title. Never present the criteria as a form to fill in. Offer to find them, in this order, asking at most one or two questions at a time:

1. "Do you know the GPN of a colleague already doing the job you are joining — someone on your new team?" If yes, Strategy 0 turns that one number into both the answer and the peer criteria, and no further questions are needed.
   Do NOT ask a new joiner for their OWN GPN: they have no assignments yet, so it returns nothing useful. The colleague's GPN is what carries the answer.
2. If not, ask what they will be doing in plain words and run Step R on JOBTITLE.
3. For scope, ask which business area or function they are joining, in their own words, and run Step R across the business hierarchy columns. If they cannot name one, offer recognisable choices with get_column_values on AREANAME (the broadest business level). Mention OU only if they raise it or prefer the financial view.
4. Briefly say why you are asking ("peers with the same role in the same area usually need the same access").

Only when every route fails, tell the user plainly that the data cannot identify their peer group yet, and say what would unblock it (a colleague's GPN, or a job title plus a business area).

=== SEARCH STRATEGIES ===

Strategy 0 — Start from a colleague's GPN / EmployeeID (fastest path):
  ONE colleague's GPN answers 'what access do I need?' directly: their assignments are the model, and their attributes (JOBTITLE, business hierarchy) satisfy BOTH minimum criteria at once — no Step R needed.
  a. EXACT match only, never fuzzy: IDs are substrings of one another ('123' would match '1234' and '91230' under regex matching).
     filter_dataset(dataset='Entitlements', filters={'EmployeeID': '<gpn>'})
  b. Entitlements has ONE ROW PER ASSIGNMENT, so a GPN returns many rows: the person's attributes repeat identically on every row (read them from the first), and the ResourceIDs across the rows ARE that person's current access.
  c. VERIFY the returned rows carry the GPN you asked for. If they do not, the filter was ignored (usually a wrong column name — re-check with list_datasets). Never present rows you have not verified belong to the requested person.
  d. If the response includes _truncated=True, switch to count_by_column(dataset='Entitlements', column='ResourceID', filters={'EmployeeID': '<gpn>'}) with fuzzy=False.
  e. Look up the ResourceIDs in Resources for names and descriptions.
  Uses:
  - A COLLEAGUE's GPN (the common case — a new joiner naming someone on their team, or 'give me the same access as GPN 12345'): that person's ResourceIDs are the direct answer. Their attributes also let you widen to the whole peer group with Strategy 1 — offer this, since one colleague may hold unusual extras that should not be copied blindly.
  - The user's OWN GPN: existing employees asking 'what do I have today?' only. If a lookup meant to describe the user comes back empty, say so plainly and pivot to a colleague's GPN or Step R.

Strategy 1 — Peer-based recommendations (most common):
  a. Check both dimensions are present as terms (a job title + one scope term). Ask only for a dimension that is entirely missing.
  b. Resolve every partial term to confirmed exact values — Step R above.
  c. Counting — use count_by_column on Entitlements, grouping by ResourceID, filtering on the RESOLVED values: exact filters, or fuzzy=True with an alternation of the exact chosen values when the user picked several. Returns ResourceIDs ranked by peer count.
  d. If too few results, broaden WITHIN the dimension you are using, never across into the other one:
     - business hierarchy: FUNCTIONNAME -> SEGMENTNAME -> SECTORNAME -> AREANAME
     - financial: OU -> ParentOU
     Never drop JOBTITLE.
  e. For the top ResourceIDs, call search_dataset on Resources for names and descriptions.
  f. Present as a table: ResourceID, Resource Name, Resource Description, Peer Count. Sort by Peer Count descending.

Strategy 2 — Search by description:
  a. search_dataset on Resources with the description.
  b. Present matches with name, description, RequestingSystem.

Strategy 3 — Explore the organisation:
  - Business hierarchy first: get_column_values on AREANAME (broadest, small list) to offer recognisable choices; count_by_column with fuzzy=True on SEGMENTNAME or FUNCTIONNAME to discover matches with counts (e.g. count_by_column(dataset='Entitlements', column='SEGMENTNAME', filters={'SEGMENTNAME': 'tiso'}, fuzzy=True)).
  - OU / ParentOU only when the user wants the financial view.
  - High-cardinality columns (JOBTITLE, CITY): count_by_column with fuzzy=True to discover matching values with counts.
  Then proceed with Strategy 0, 1 or 2.

Strategy 4 — Request access:
  a. Call get_request_attributes to learn what fields are needed.
  b. Collect the required information from the user (resource_id, justification, and any optional fields).
  c. If the user doesn't know the ResourceID, help them find it first using Strategies 0-3.
  d. Once all required fields are gathered, call raise_entitlement_request to submit.
  e. Report the result (request ID, status) to the user.

=== RULES ===

- Use count_by_column (with fuzzy=True) for broad discovery and counting; use filter_dataset or filter_dataset_fuzzy ONLY when you need actual row data.
- If a filter tool response includes _truncated=True, switch to count_by_column for a compact summary instead of increasing max_results.
- ResourceID joins the two datasets. Always look up Resources for names/descriptions — never show raw ResourceIDs.
- If a fuzzy filter returns nothing, try a broader pattern or fewer filter columns.
- Column names must come from list_datasets. A filter naming a column that does not exist is SILENTLY IGNORED — you get unfiltered rows, not an error. For identity lookups, always verify the returned rows carry the value you filtered on.
- Never fuzzy-match an identifier (EmployeeID, ResourceID) when you mean one specific record.
- Never mix business hierarchy and financial (OU) terms in one filter set, and never broaden from one into the other.

=== OUTPUT ===

Provide your answer directly to the user. Be concise but thorough. If the data does not cover the user's question, say so clearly.

For peer-recommendation results, present a table with these columns: ResourceID, Resource Name, Resource Description, Peer Count. Sort by Peer Count descending.
"""
QUALITY_PROMPT = """\
You are the Data Quality Checker specialist for an Access Governance assistant. You evaluate resource metadata against a quality criteria checklist and produce structured reports.

=== WORKFLOW ===

1. Parse the comma-separated ResourceIds from the user's message.
2. Call list_datasets to discover the current column names on the Resources dataset.
3. Call get_quality_criteria to load the quality checklist.
4. Fetch resource data using filter_dataset_fuzzy on the Resources dataset with a ResourceID regex pattern joining all IDs with | (e.g. {"ResourceID": "id1|id2|id3"}). This returns full rows with all columns -- no column mapping is needed.
5. For each resource, evaluate each criterion against the entire row data (all columns). Use the criterion description to judge whether any column satisfies it:
   - Does the resource data contain information that meets the criterion?
   - Is that information meaningful and complete?
   - Verdict: Pass, Fail, or Partial (with explanation).
6. For each resource, perform a Privileged Access assessment using the definition and signals in the PRIVILEGED ACCESS ASSESSMENT section below.
7. Produce a structured quality report.

=== EVALUATION RULES ===

- Criteria are NOT mapped to specific columns. Evaluate each criterion against all available columns in the resource row.
- A criterion passes if any column (or combination of columns) provides data that satisfies the criterion description.
- A criterion fails if no column contains relevant data, or the data is empty, meaningless, or clearly insufficient.
- Mark as Partial when some relevant data exists but does not fully satisfy the criterion description.
- Pay attention to importance levels: Very Important, Important, and Nice to Have. Flag Very Important failures prominently.
- If a criterion description says 'only fill out if applicable' and the field is empty, that is acceptable (not a failure).

=== AVAILABLE TOOLS ===

- list_datasets -- shows datasets, column names, and row counts. Call this first to discover the Resources dataset schema.
- get_quality_criteria -- returns the quality criteria checklist with name, description, importance, and range for each criterion.
- filter_dataset_fuzzy(dataset, filters) -- regex pattern matching on columns. Use to batch-fetch resources by ResourceID.
- filter_dataset(dataset, filters) -- exact match filtering.
- search_dataset(dataset, query) -- free-text BM25 search.
- get_column_values(dataset, column) -- list distinct values.
- hand_off_to_router(reason) -- hand the conversation back if the question is outside your expertise.

=== WHEN TO HAND OFF ===

If the user asks about processes, policies, peer recommendations, or anything not related to data quality evaluation, call hand_off_to_router with the reason.

=== OUTPUT FORMAT ===

For each resource, produce a table:

| Criterion | Importance | Status | Notes |
|-----------|------------|--------|-------|

Then produce a summary table:

| ResourceId | Name | Score | Very Important Gaps | Status | PU Classification |
|------------|------|-------|---------------------|--------|-------------------|

Status values: Good (all Very Important pass), At Risk (any Very Important fail), Needs Review (only Important/Nice to Have failures).

Be thorough but concise. List the column(s) you matched each criterion against in the Notes column so the user can verify your assessment.

=== PRIVILEGED ACCESS ASSESSMENT ===

After the quality criteria evaluation, assess whether each resource grants Privileged User (PU) access. Privileged access enables a user to:
- Alter a system's configuration
- Administrative access for system/application control override
- Alter stored data by overriding system/application controls
- Interrupt or interfere with normal operation

Typical PU activities: installing/upgrading systems, troubleshooting, creating user accounts, overriding application controls.

Signals to look for across ALL columns in the resource row:
- Access Mode = Manage or Admin (strong signal)
- Access Mode = Write combined with high criticality (moderate signal)
- Name or description containing: admin, administrator, root, superuser, manage, configuration, override, install, system control, full access, unrestricted, elevated, privileged, troubleshoot
- Access Type = Function (system-level control vs data access)
- High Access Right Criticality combined with write/manage access
- Description referencing account creation, system configuration, or control override

Classification:
- Privileged -- strong indicators present
- Potentially Privileged -- some indicators but not conclusive
- Not Privileged -- no indicators found

For each resource, add a row to the Privileged Access Assessment table placed AFTER the summary table:

| ResourceId | Name | PU Classification | Key Signals | Reasoning |
|------------|------|-------------------|-------------|-----------|
"""


# -- Helpers --

# Max character length for a single ToolMessage's content.
# MCP adapters can return content as a list of thousands of blocks,
# which exceeds OpenAI's 16,384 array-element limit. _compact_content
# consolidates and truncates to stay within bounds.
MAX_TOOL_CONTENT_LEN = int(os.environ.get("MAX_TOOL_CONTENT_LEN", "80000"))


def _compact_content(msg):
    """Consolidate list-type ToolMessage content into a single text string.

    MCP adapters store tool results as a list of content blocks. Large results
    can exceed OpenAI's 16,384 array-element limit. This joins all text blocks
    into one string and truncates if it exceeds MAX_TOOL_CONTENT_LEN.

    Returns a new ToolMessage (never mutates the original) so the checkpointer's
    saved state is not corrupted.
    """
    if not isinstance(msg, ToolMessage):
        return msg
    content = msg.content
    if isinstance(content, list):
        # consolidate list of content blocks into a single string
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        text = "\n".join(parts)
    else:
        text = content

    if len(text) <= MAX_TOOL_CONTENT_LEN and isinstance(content, str):
        return msg  # nothing to change

    if len(text) > MAX_TOOL_CONTENT_LEN:
        text = text[:MAX_TOOL_CONTENT_LEN] + "\n...[truncated]"

    return ToolMessage(
        content=text,
        tool_call_id=msg.tool_call_id,
        name=msg.name,
        id=msg.id,
    )


def _trim_messages(messages: list) -> list:
    """Keep only the most recent KEEP_LAST_N messages for the LLM.

    The checkpointer stores the full history, but the LLM only sees
    a sliding window to prevent context overflow. Also compacts any
    ToolMessage with oversized content (list or long string).
    """
    if KEEP_LAST_N > 0 and len(messages) > KEEP_LAST_N:
        messages = messages[-KEEP_LAST_N:]
        # drop orphaned ToolMessages at the start -- their parent
        # AIMessage was trimmed and OpenAI rejects dangling tool msgs
        while messages and isinstance(messages[0], ToolMessage):
            messages = messages[1:]
    # consolidate oversized tool results so they don't exceed
    # OpenAI's content array limit (16,384 elements)
    messages = [_compact_content(m) for m in messages]
    return messages


# -- Node factories --

def _make_router_node(llm, routing_tools):
    """Create the router node -- picks Knowledgebase, Resource, or answers directly.

    Sets active_agent when routing so subsequent turns skip the router.
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
            elif tc["name"] == "route_to_quality_checker":
                active_agent = "quality_agent"
        return {"messages": result, "active_agent": active_agent}

    return router_node


def _make_agent_node(llm, tools, prompt):
    """Create a specialist agent node (Knowledgebase or Resource).

    Binds tools to the LLM so it can produce tool_calls. Actual tool
    execution happens in a separate ToolNode -- this node only calls the LLM.
    """
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict:
        messages = _trim_messages(state["messages"])
        response = await llm_with_tools.ainvoke(
            [SystemMessage(content=prompt)] + messages
        )
        return {"messages": [response]}

    return agent_node


# -- Handoff node --

def _handoff_node(state: AgentState) -> dict:
    """Process hand_off_to_router and clear active_agent.

    Produces a ToolMessage for every pending tool call to keep the
    message history valid, then resets active_agent so the router
    takes over on the next step.
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


# -- Custom tool node --

def _make_tool_node(domain_tools):
    """Create a tool node that handles mixed domain + handoff calls.

    When the specialist calls both domain tools and hand_off_to_router
    in a single response, this node executes the domain tools normally
    and stubs the handoff with a directive to answer its part first.
    """
    # base_node runs tool calls and returns {"messages": [ToolMessage, ...]}
    # without mutating graph state -- the runtime merges it via reducers.
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
            return await base_node.ainvoke(state)

        # mixed calls -- run domain tools only, stub the handoff
        modified_msg = AIMessage(
            content=last_msg.content,
            tool_calls=domain_calls,
            id=last_msg.id,
        )
        # copy state with only domain tool_calls so base_node doesn't choke
        modified_state = {
            **state,
            "messages": list(state["messages"])[:-1] + [modified_msg]
        }
        result = await base_node.ainvoke(modified_state)

        # stub responses for the handoff calls
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


# -- Routing / edge functions --

def route_entry(state: AgentState) -> str:
    """Entry edge: skip the router if a specialist already owns the conversation."""
    active = state.get("active_agent", "")
    if active in ("knowledgebase_agent", "resource_agent", "quality_agent"):
        return active
    return "router"


def route_from_router(state: AgentState) -> str:
    """After the router: follow the routing tool call to a specialist, or END."""
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
        if tool_name == "route_to_quality_checker":
            return "quality_agent"
    # no tool call -> direct answer, end the turn
    return END


def _make_specialist_edge(tool_node_name: str):
    """Return an edge function for a specialist node.

    hand_off_to_router only -> "handoff"
    domain tool calls (possibly mixed with handoff) -> tool_node_name
    no tool calls (final answer) -> END
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
                return "handoff"
            # domain tools present -- route to tool node even if
            # there's a stray handoff alongside
            return tool_node_name
        return END

    return check


# -- Graph builder --

def build_graph(llm, all_tools):
    """Build the StateGraph with persistent specialist ownership.

    START -> (active_agent?) -> knowledgebase_agent <-> knowledgebase_tools -> END
                |                     |
                |                     +-> handoff -> router
                |                                      |
                +-> router -> (route) -> ...           |
                |     |                                |
                |     +-> END                          |
                |                                      |
                +-> resource_agent <-> resource_tools -> END
                |         |
                |         +-> handoff -> router
                |
                +-> quality_agent <-> quality_tools -> END
                          |
                          +-> handoff -> router

    Once the router assigns a specialist, active_agent is set. On
    subsequent turns, START routes directly to that specialist. The
    specialist only hands back via hand_off_to_router on context switch.
    """
    knowledgebase_tools = [t for t in all_tools if t.name in KNOWLEDGEBASE_TOOL_NAMES]
    resource_tools = [t for t in all_tools if t.name in RESOURCE_TOOL_NAMES]
    quality_tools = [t for t in all_tools if t.name in QUALITY_TOOL_NAMES]

    # each specialist gets its MCP tools + the handoff tool
    knowledgebase_all_tools = knowledgebase_tools + [hand_off_to_router]
    resource_all_tools = resource_tools + [hand_off_to_router]
    quality_all_tools = quality_tools + [hand_off_to_router]

    graph = StateGraph(AgentState)

    # -- nodes --
    graph.add_node("router", _make_router_node(llm, ROUTING_TOOLS))

    graph.add_node("knowledgebase_agent", _make_agent_node(llm, knowledgebase_all_tools, KNOWLEDGEBASE_PROMPT))
    graph.add_node("knowledgebase_tools", _make_tool_node(knowledgebase_tools))

    graph.add_node("resource_agent", _make_agent_node(llm, resource_all_tools, RESOURCE_PROMPT))
    graph.add_node("resource_tools", _make_tool_node(resource_tools))

    graph.add_node("quality_agent", _make_agent_node(llm, quality_all_tools, QUALITY_PROMPT))
    graph.add_node("quality_tools", _make_tool_node(quality_tools))

    graph.add_node("handoff", _handoff_node)

    # -- edges --

    # entry: skip router if a specialist already owns the conversation
    graph.add_conditional_edges(
        START,
        route_entry,
        {
            "router": "router",
            "knowledgebase_agent": "knowledgebase_agent",
            "resource_agent": "resource_agent",
            "quality_agent": "quality_agent",
        },
    )

    # router picks a specialist or answers directly
    graph.add_conditional_edges(
        "router",
        route_from_router,
        {
            "knowledgebase_agent": "knowledgebase_agent",
            "resource_agent": "resource_agent",
            "quality_agent": "quality_agent",
            END: END,
        },
    )

    # knowledgebase sub-loop: agent -> tools -> agent -> ... -> END or handoff
    graph.add_conditional_edges(
        "knowledgebase_agent",
        _make_specialist_edge("knowledgebase_tools"),
        {"knowledgebase_tools": "knowledgebase_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("knowledgebase_tools", "knowledgebase_agent")

    # resource sub-loop: agent -> tools -> agent -> ... -> END or handoff
    graph.add_conditional_edges(
        "resource_agent",
        _make_specialist_edge("resource_tools"),
        {"resource_tools": "resource_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("resource_tools", "resource_agent")

    # quality sub-loop: agent -> tools -> agent -> ... -> END or handoff
    graph.add_conditional_edges(
        "quality_agent",
        _make_specialist_edge("quality_tools"),
        {"quality_tools": "quality_tools", "handoff": "handoff", END: END},
    )
    graph.add_edge("quality_tools", "quality_agent")

    # handoff returns to router for re-routing
    graph.add_edge("handoff", "router")

    return graph


# -- MCP config --

def _get_mcp_server_config() -> dict:
    """Build MCP server connection config from env vars.

    streamable-http -- the default HTTP transport. Needs MCP_SERVER_URL
             (e.g. http://host:8000/mcp); when MCP_SERVER_TOKEN is set
             it is sent as a bearer token so the server can
             authenticate this agent deployment
    sse   -- legacy HTTP transport, kept for rollback. Same env vars
             (URL path is /sse instead of /mcp)
    stdio -- needs MCP_SERVER_COMMAND + MCP_SERVER_ARGS (no auth: the
             server runs as a local child process)

    MCP_TRANSPORT accepts hyphen or underscore spellings
    (streamable-http / streamable_http); the adapter library itself
    expects the underscore form in the connection dict.
    """
    server_name = os.environ.get("MCP_SERVER_NAME", "access-governance-docs")
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").lower().replace("_", "-")

    if transport in ("streamable-http", "sse"):
        default_path = "/mcp" if transport == "streamable-http" else "/sse"
        url = os.environ.get("MCP_SERVER_URL", f"http://127.0.0.1:8000{default_path}")
        connection = {
            "url": url,
            # the adapter's literal is underscore-spelled: streamable_http
            "transport": "streamable_http" if transport == "streamable-http" else "sse",
        }
        # this deployment's MCP credential -- the server identifies the
        # agent by which token it presents, so no name is sent here.
        # Never log the token value.
        token = os.environ.get("MCP_SERVER_TOKEN", "")
        if token:
            connection["headers"] = {"Authorization": f"Bearer {token}"}
        logger.info(
            "MCP server=%s transport=%s url=%s auth=%s",
            server_name, transport, url, "bearer" if token else "off",
        )
        return {server_name: connection}

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
        f"Unsupported MCP_TRANSPORT={transport!r}. "
        "Use 'streamable-http', 'sse', or 'stdio'."
    )


# -- History dump --

LOG_FILE = "agent_log.txt"
STREAM_FILE = "agent_stream.txt"


def _write_stream_entry(fh, msg, node: str) -> None:
    """Append a single message entry to the stream file.

    Format matches _dump_history: [ClassName]  (node: xxx) followed by
    content and any tool_calls. Flushes after each entry so tail -f
    sees it immediately.
    """
    role = msg.__class__.__name__
    content = msg.content if isinstance(msg.content, str) else str(msg.content)
    fh.write(f"[{role}]  (node: {node})\n{content}\n")
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        for tc in msg.tool_calls:
            fh.write(f"  -> tool_call: {tc['name']}({tc.get('args', {})})\n")
    fh.write("\n")
    fh.flush()


async def _dump_history(agent, config) -> None:
    """Write full conversation history to LOG_FILE with node annotations."""
    try:
        state = await agent.aget_state(config)
        messages = state.values.get("messages", [])
        if not messages:
            return

        # build mapping: message id -> graph node that produced it
        snapshots = [s async for s in agent.aget_state_history(config)]
        snapshots.reverse()  # oldest first

        # snapshots are cumulative -- each contains all messages up to
        # that point. A checkpoint is saved after each node executes, so
        # snapshots[i-1].next[0] is the node that produced snapshots[i].
        # We diff adjacent snapshots to find newly added messages.
        msg_id_to_node: dict[str, str] = {}
        prev_count = 0
        for i, snap in enumerate(snapshots):
            if i == 0:
                node = snap.metadata.get("source", "input")
            else:
                prev_next = snapshots[i - 1].next
                node = prev_next[0] if prev_next else "unknown"
            snap_msgs = snap.values.get("messages", [])
            for msg in snap_msgs[prev_count:]:
                msg_id_to_node[msg.id] = node
            prev_count = len(snap_msgs)

        with open(LOG_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"Agent session log -- {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(f"Total messages: {len(messages)}\n")
            fh.write("=" * 60 + "\n\n")
            for msg in messages:
                role = msg.__class__.__name__
                node = msg_id_to_node.get(msg.id, "unknown")
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                fh.write(f"[{role}]  (node: {node})\n{content}\n")
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        fh.write(f"  -> tool_call: {tc['name']}({tc.get('args', {})})\n")
                fh.write("\n")
        logger.info("Session history written to %s (%d messages)", LOG_FILE, len(messages))
    except Exception:
        logger.exception("Failed to write session history to %s", LOG_FILE)


# -- Interactive loop --

# spinner labels per graph node
_NODE_PHASES = {
    "router": "Routing",
    "knowledgebase_agent": "Generating answer",
    "resource_agent": "Generating answer",
    "quality_agent": "Evaluating quality",
    "knowledgebase_tools": "Calling tools",
    "resource_tools": "Calling tools",
    "quality_tools": "Calling tools",
    "handoff": "Switching specialist",
}


async def run_agent_loop(on_response=None):
    """Interactive agent loop. Runs until the user types 'exit' or Ctrl-C.

    Streams the final answer token-by-token via astream_events.
    on_response(str) is called for non-streamed answers (defaults to print).
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

    # unique thread per CLI session for conversation tracking
    thread_id = uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 50}

    # reset the stream file for this session so it doesn't grow unboundedly
    with open(STREAM_FILE, "w", encoding="utf-8") as fh:
        fh.write(f"Agent stream log -- {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        fh.write("=" * 60 + "\n")

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
            # only print if the answer wasn't already streamed
            if not streamed:
                on_response(f"\n🤖 Assistant: {answer}\n")
        except Exception:
            logger.exception("Error processing query")
            on_response(
                "\nAssistant: Sorry, an error occurred while "
                "processing your question. Please try again.\n"
            )

    # dump full (untrimmed) history on exit
    await _dump_history(agent, config)


async def _stream_response(agent, user_input: str, config: dict) -> tuple[str, bool]:
    """Run the graph with streaming. Shows a spinner during routing/tools,
    then streams the final answer token-by-token.

    Returns (answer_text, was_streamed).
    """
    spinner = Spinner("Thinking")
    await spinner.start()

    # streaming is a one-shot latch: False while the LLM is routing or calling
    # tools, flips to True on the first displayable answer token. It serves two
    # purposes: (1) gates spinner updates -- once True the spinner is stopped and
    # never updated again, so it can't overwrite answer text already on screen;
    # (2) prints the "Assistant:" header exactly once on the False -> True edge.
    streaming = False
    answer_parts: list[str] = []

    # stream messages to file in real time so `tail -f agent_stream.txt` works
    stream_fh = open(STREAM_FILE, "a", encoding="utf-8")
    stream_fh.write(f"\n--- turn {datetime.datetime.now():%H:%M:%S} ---\n\n")
    _write_stream_entry(stream_fh, HumanMessage(content=user_input), "input")

    try:
        # astream_events is an async generator -- it runs the full graph and
        # yields a dict for every internal event (node start, LLM token, tool
        # finish, etc.) as they happen. Contrast with the other execution methods:
        #   ainvoke        -> runs graph, returns final state (no intermediate visibility)
        #   astream        -> runs graph, yields state deltas per node (node-level granularity)
        #   astream_events -> runs graph, yields per-runnable events (token-level granularity)
        # We use astream_events because we need token-level streaming for the
        # live typing effect plus node-start events for spinner phase updates.
        async for event in agent.astream_events(
            {"messages": [HumanMessage(content=user_input)]},
            config,
            version="v2",
        ):
            kind = event["event"]

            # -- astream_events (v2) event kinds --
            # LangGraph/LangChain emits events named on_[type]_(start|stream|end).
            # Each runnable type produces a triplet:
            #
            #   chain       -> on_chain_start, on_chain_stream, on_chain_end
            #   chat_model  -> on_chat_model_start, on_chat_model_stream, on_chat_model_end
            #   llm         -> on_llm_start, on_llm_stream, on_llm_end
            #   tool        -> on_tool_start, on_tool_stream, on_tool_end
            #   retriever   -> on_retriever_start, on_retriever_stream, on_retriever_end
            #   prompt      -> on_prompt_start, on_prompt_end
            #   custom      -> on_custom_event
            #
            # _start  -- runnable invoked; data.input has the input
            # _stream -- incremental chunk; data.chunk has the partial result
            # _end    -- runnable finished; data.output has the final result
            #
            # In LangGraph, on_chain_start fires per graph node; metadata.langgraph_node
            # identifies which node. on_chat_model_stream yields AIMessageChunk with
            # .content (text tokens) and optionally .tool_call_chunks (partial tool JSON).
            #
            # We only use on_chain_start (spinner updates) and on_chat_model_stream
            # (token display) -- everything else is handled internally by the graph.
            #
            # Ref: https://api.python.langchain.com/en/latest/runnables/langchain_core.runnables.schema.StreamEvent.html
            # Ref: https://docs.langchain.com/oss/python/langchain/streaming

            # update spinner when a new graph node starts
            if kind == "on_chain_start":
                node = event.get("metadata", {}).get("langgraph_node", "")
                phase = _NODE_PHASES.get(node)
                if phase and not streaming:
                    spinner.update(phase)

            # stream text tokens -- content chunks without tool_call_chunks
            # are final-answer tokens
            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                has_content = chunk.content
                has_tool_calls = getattr(chunk, "tool_call_chunks", None)

                if has_content and not has_tool_calls:
                    if not streaming:
                        streaming = True
                        await spinner.stop()
                        sys.stdout.write("\n🤖 Assistant: ")
                    # sys.stdout.write + flush instead of print() so each token
                    # appears immediately with no added newline -- gives the user
                    # a live typing effect. sys is Python's standard library module
                    # (imported at top of file); sys.stdout is the process stdout.
                    sys.stdout.write(chunk.content)
                    sys.stdout.flush()
                    answer_parts.append(chunk.content)

            # log completed node output to stream file for tail -f
            if kind == "on_chain_end":
                node = event.get("metadata", {}).get("langgraph_node", "")
                if node:
                    output = event.get("data", {}).get("output", {})
                    msgs = []
                    if isinstance(output, dict):
                        msgs = output.get("messages", [])
                    for msg in msgs:
                        _write_stream_entry(stream_fh, msg, node)
    finally:
        await spinner.stop()
        stream_fh.close()

    if streaming:
        sys.stdout.write("\n")
        sys.stdout.flush()

    # fall back to reading final state if nothing was streamed
    if answer_parts:
        return "".join(answer_parts), True

    state = await agent.aget_state(config)
    messages = state.values.get("messages", [])
    return (messages[-1].content if messages else ""), False

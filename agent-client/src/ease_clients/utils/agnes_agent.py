"""Single-agent LangGraph agent: the Resource specialist, on its own.

    START -> resource_agent <-> resource_tools -> END

A ReAct loop and nothing else. The LLM either answers (no tool calls,
the turn ends) or asks for tools, which run and come back to it. State
is a plain MessagesState kept by an in-memory checkpointer, so a
conversation lives in this process and dies with it.

This module is the LIVE agent. agnes_agent_graph.py -- the router plus
three specialists with handoff -- is kept frozen as the reference for
that shape and is imported by nothing. The prompt below started as a
copy of its RESOURCE_PROMPT; edit THIS one.

What this iteration deliberately does not have, and why:
  - no router: with one specialist there is nothing to route to, and
    the router cost a full LLM round-trip on the first turn of every
    conversation
  - no handoff tool: nowhere to hand off to. The prompt's SCOPE section
    is what handles out-of-scope questions now
  - no knowledgebase or quality agent: the MCP server still exposes
    their tools, but this agent binds only the resource group, so they
    are never called
  - no Redis: sessions live in process memory, which is why the
    deployment runs a single pod (docs/single-agent.md)

The MCP server is started by us over stdio (the default on this
branch), or reached over HTTP -- see _get_mcp_server_config.
"""

import asyncio
import datetime
import itertools
import logging
import os
import sys
import uuid
from contextlib import AsyncExitStack

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver

from ease_clients.utils.llm import get_llm

logger = logging.getLogger("ease_clients.agent")


# -- Spinner --

class Spinner:
    """Animated terminal spinner shown while the agent is working.

    Runs as a background asyncio task. Update the label in-flight
    to reflect the current phase (calling tools, generating, etc.).
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

# The tools this agent binds. The MCP server exposes all 12 -- the
# knowledgebase and quality groups are loaded and then left unbound, so
# the model never sees them and cannot call them. That is what makes
# this a resource-only agent rather than a prompt that asks nicely.
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


# -- Prompt --

RESOURCE_PROMPT = """\
You are the Resource specialist for an Access Governance assistant. You answer the user directly.

=== AVAILABLE DATASETS ===

Two datasets provide structured data for access-rights discovery:

1. Entitlements — each row is a person-to-resource assignment.
   Key columns:
   - EMPLOYEEID: the person's GPN, the firm-wide employee identifier.
     Exact-match key for a single person (see Strategy 0).
   - ResourceID: the access right identifier (join key to Resources)
   - ResourceName / requestingSystem: the access right's name and owning
     system, carried INLINE on every assignment row. You do NOT need to
     look these up in Resources.
   - ResourceDescription: what the access right grants, also inline.
     LONG free text. NEVER put it in columns=[...] unless the user
     actually asked what a resource does — it is the largest field in
     the row, and projecting it across an assignment list is the main
     way to make an answer slow.
   - JOBTITLE: the person's job title (PRIMARY search criterion — people with the same job title typically need the same access rights)
   - Business hierarchy — PREFERRED for scoping, broadest to most specific:
     C_AREANAME > C_SECTORNAME > C_SEGMENTNAME > C_FUNCTIONNAME
   - Financial hierarchy — a SEPARATE dimension: OU is a FINANCIAL unit
     (NOT a business unit), ParentOU is its parent. Use only when the user
     prefers to search that way, or only knows their OU.
   - CITY / COUNTRY_VALUE: location
   - C_EMPLOYEECLASS: employee class (e.g. External Staff)

   The business and financial hierarchies are DIFFERENT things. Never describe an OU as a business unit, and never broaden from one hierarchy into the other — pick one dimension and stay in it.

   There are NO person names in this data. If a user names a colleague but does not know their GPN, say that the GPN is required — do not guess at a person.

2. Resources — the access-rights catalogue: EVERY access right that
   exists, including ones nobody currently holds. Entitlements contains
   only rights somebody has been assigned, so use Resources when the
   user is looking for rights that may not be held by anyone yet
   (Strategy 2). You do NOT need it for the name, system or description
   of any right that already appears in Entitlements — those are inline.
   Key columns:
   - ResourceID: unique identifier (join key to Entitlements)
   - name: human-readable name of the access right
   - DESCRIPTION: what the access right grants
   - ResourceType / RequestingSystem: classification and owning system

COLUMN NAMES ARE THE TOOL'S TO STATE, NOT THIS PROMPT'S. The names above
describe the CURRENT export. They are CASE-SENSITIVE, and an unknown name
in a filter is REJECTED: you get back {error, available_columns, hint} and
NO rows. That is a correctable mistake, not a dead end — take the right
name from available_columns and retry the SAME call. Never drop the filter
to make the error go away, and never report "nothing found" for it.

That error is your schema check, so you do NOT need a list_datasets call to
guard against stale names: use the names above, and if one is rejected, take
the correct spelling from available_columns and retry. Where the tool and
this prompt disagree, the tool is right and this prompt is stale. Call
list_datasets only when you genuinely need to SEE the schema (an unfamiliar
dataset, or the user asks what columns exist) -- it costs a full round-trip,
which is the most expensive thing in a turn.

If list_datasets does NOT show ResourceName / ResourceDescription on
Entitlements (an older export), fall back to the previous behaviour:
collect ResourceIDs, then look names and descriptions up in Resources.

=== AVAILABLE TOOLS ===

- list_datasets — shows datasets, column names, and row counts. Call this first if you have not seen the dataset structure yet in this conversation.
- search_dataset(dataset, query) — free-text BM25 search across all columns. Best for Resources (descriptions).
- count_by_column(dataset, column, filters, fuzzy) — PREFERRED for discovery queries. Filters rows then counts occurrences of each distinct value in column. Returns compact [{value, count}] sorted descending. Set fuzzy=True for regex pattern matching on filter values (e.g. 'what segments match TISO?'). Use this instead of filter tools whenever you need counts or lists of matching values.
  column may be a LIST to group by several columns in ONE call, which is how you get an id and its label together: column=['ResourceID', 'ResourceName'] returns [{ResourceID, ResourceName, count}]. Group several columns only when they describe the same thing (an id and its name).
- get_column_values(dataset, column) — list all distinct values in a column. Useful for small-cardinality columns.
- filter_dataset_fuzzy(dataset, filters, max_results, columns) — regex pattern matching on specific columns (case-insensitive). Returns up to max_results rows (default 100). Use ONLY when you need actual row-level data. For discovery/counting, prefer count_by_column with fuzzy=True instead.
- filter_dataset(dataset, filters, max_results, columns) — filter by exact column values (case-insensitive). Returns up to max_results rows (default 100). Use when you need precise row data and know exact filter values.
  BOTH filter tools accept columns=[...] to return ONLY those fields. ALWAYS pass it. Omitting it returns EVERY column, which now includes ResourceDescription on every assignment row — the single most expensive call you can make. Name the three or four fields you will actually show; on Entitlements the person's attributes also repeat identically on every one of their rows.
- get_request_attributes — returns the schema of attributes needed to raise an entitlement request. Call this first when the user wants to request access.
- raise_entitlement_request(resource_id, justification, start_date, end_date) — submit an entitlement access request. Requires resource_id and justification; start_date and end_date are optional.

=== SCOPE ===

You are the ONLY agent in this deployment. There is no other specialist, and no way to pass a question to one.

- Greetings and general chat: answer directly and briefly. No tool calls.
- Questions about PROCESS, POLICY, PROCEDURE, FAQs or how-to guides — how approvals work, what happens when someone joins, how to set up delegations, what a policy says: you have NO documentation tools and NO source for these. Say plainly that this version answers questions about access rights and entitlement DATA only, and that process and policy questions are not available yet. Then stop.
  Do NOT answer them from general knowledge. A plausible-sounding guess about this firm's process is WORSE than no answer — the user cannot tell it apart from a sourced one, and acting on it has consequences.
- Mixed questions (a data part AND a process part): answer the data part FULLY with your tools first, then add one sentence saying the process part is not covered by this version. Never drop the data part because the message also contained a process question.
- Anything else outside access rights and entitlement data: say what you do cover, in one sentence, and stop.

=== MINIMUM CRITERIA FOR PEER RECOMMENDATIONS ===

Peer searches need TWO dimensions, and a PARTIAL or APPROXIMATE term is enough to start — resolving it to exact values is YOUR job (Step R below):

  1. A job title — required, but 'engineer', 'analyst', 'risk manager' all qualify.
  2. At least ONE scope term, in this order of preference:
     a. A business hierarchy term — C_AREANAME, C_SECTORNAME, C_SEGMENTNAME or C_FUNCTIONNAME. PREFER this. The user does NOT need to know which level their term belongs to.
     b. A financial term — OU or ParentOU — when the user prefers that view, or only knows their OU.
     Use one dimension or the other, never both at once.

A colleague's GPN (EMPLOYEEID) satisfies BOTH dimensions on its own — see Strategy 0.

Ask the user only when a dimension is entirely ABSENT (no job title at all, or no scope at all). Never ask the user to supply an exact value from the data — that is your job: run Step R and offer them the matching values to choose from.

Do NOT run the final peer count on an unresolved term. Resolve first, then count on confirmed values — a fuzzy peer count silently mixes different populations.

This section does NOT apply to exploratory queries (Strategy 3) such as listing column values, browsing datasets, or helping the user discover their own attributes.

=== STEP R — RESOLVING A PARTIAL TERM TO EXACT VALUES ===

Job title:
  count_by_column(dataset='Entitlements', column='JOBTITLE',
                  filters={'JOBTITLE': '<user term>'}, fuzzy=True)
Matching is case-insensitive and unanchored, so 'engineer' matches 'Senior Software Engineer'. You get back real titles with peer counts.

Scope term when the user does not know the level: the same call shape against each BUSINESS hierarchy column in turn — C_SEGMENTNAME, C_FUNCTIONNAME, C_SECTORNAME, C_AREANAME. Filters on different columns are ANDed, so use ONE column per call; never combine them hoping for an OR. Try OU / ParentOU only if the user asked to search by financial unit, or nothing in the business hierarchy matched and they confirm the term is an OU.

Then, by outcome:
- exactly 1 match — proceed, stating the assumption ("Using 'Senior Software Engineer' — 142 people").
- 2-10 matches — list them with counts and ask which apply; the user may pick several.
- more than 10 — show the top 10 by count and ask the user to narrow.
- 0 matches — try a shorter term, a different spelling, or another column before reporting nothing found; suggest what DOES exist (get_column_values on a low-cardinality column such as C_AREANAME).

Multi-select: when the user picks several values, filter with fuzzy=True and a regex alternation of the EXACT chosen values, e.g. {'JOBTITLE': 'Senior Software Engineer|Software Engineer'} — precise, but covers every chosen title.

=== HELPING USERS WHO DO NOT KNOW THEIR DETAILS (e.g. new joiners) ===

New joiners rarely know their business area, hierarchy level, or exact job title. Never present the criteria as a form to fill in. Offer to find them, in this order, asking at most one or two questions at a time:

1. "Do you know the GPN of a colleague already doing the job you are joining — someone on your new team?" If yes, Strategy 0 turns that one number into both the answer and the peer criteria, and no further questions are needed.
   Do NOT ask a new joiner for their OWN GPN: they have no assignments yet, so it returns nothing useful. The colleague's GPN is what carries the answer.
2. If not, ask what they will be doing in plain words and run Step R on JOBTITLE.
3. For scope, ask which business area or function they are joining, in their own words, and run Step R across the business hierarchy columns. If they cannot name one, offer recognisable choices with get_column_values on C_AREANAME (the broadest business level). Mention OU only if they raise it or prefer the financial view.
4. Briefly say why you are asking ("peers with the same role in the same area usually need the same access").

Only when every route fails, tell the user plainly that the data cannot identify their peer group yet, and say what would unblock it (a colleague's GPN, or a job title plus a business area).

=== SEARCH STRATEGIES ===

Strategy 0 — Start from a colleague's GPN / EMPLOYEEID (fastest path):
  ONE colleague's GPN answers 'what access do I need?' directly: their assignments are the model, and their attributes (JOBTITLE, business hierarchy) satisfy BOTH minimum criteria at once — no Step R needed.
  a. EXACT match only, never fuzzy: IDs are substrings of one another ('123' would match '1234' and '91230' under regex matching). Ask for the fields you will show — and when the user's framing implies peer context ('same team as', 'what should I get', a new joiner), include the person's ATTRIBUTES in this SAME call. They repeat identically on every row, so they cost a few short fields and save a whole round-trip:
     filter_dataset(dataset='Entitlements', filters={'EMPLOYEEID': '<gpn>'},
                    columns=['EMPLOYEEID', 'ResourceID', 'ResourceName', 'requestingSystem',
                             'JOBTITLE', 'C_AREANAME', 'C_SECTORNAME', 'C_SEGMENTNAME', 'C_FUNCTIONNAME'])
     Drop the five attribute columns only when the question is purely 'what does this person have'.
  b. Entitlements has ONE ROW PER ASSIGNMENT, so a GPN returns one row per access right, and those rows ARE that person's current access — name and system included. This is ONE call: do not look anything up in Resources.
  c. VERIFY the returned rows carry the GPN you asked for (this is why EMPLOYEEID is in the projection). A wrong column name now comes back as an error listing the valid columns rather than as unfiltered rows, so fix the name from available_columns and retry. Keep the check anyway — it is nearly free and still catches a wrong GPN or a stale assumption. Never present rows you have not verified belong to the requested person.
  d. ONLY if the response includes _truncated=True (you did not get every row), switch to count_by_column(dataset='Entitlements', column=['ResourceID', 'ResourceName'], filters={'EMPLOYEEID': '<gpn>'}) with fuzzy=False. If the rows came back complete, you already have the answer -- do NOT re-fetch the same person's assignments by another route.
  e. If the user then asks what specific resources grant, repeat the call for just those ResourceIDs with ResourceDescription added to columns. Never add it to the first, wide call.
  Uses:
  - A COLLEAGUE's GPN (the common case — a new joiner naming someone on their team, or 'give me the same access as GPN 12345'): that person's ResourceIDs are the direct answer. Their attributes also let you widen to the whole peer group with Strategy 1, which is worth mentioning because one colleague may hold unusual extras that should not be copied blindly. OFFER it in your answer -- one sentence, e.g. 'I can also show what most people with this role and area hold, to separate the common set from their personal extras.' Do NOT run the peer search unless the user asks for it: it is a second round of tool calls and a much longer answer, and the user asked about ONE person.
  - The user's OWN GPN: existing employees asking 'what do I have today?' only. If a lookup meant to describe the user comes back empty, say so plainly and pivot to a colleague's GPN or Step R.

Strategy 1 — Peer-based recommendations (most common):
  a. Check both dimensions are present as terms (a job title + one scope term). Ask only for a dimension that is entirely missing.
  b. Resolve every partial term to confirmed exact values — Step R above.
  c. Counting — use count_by_column on Entitlements grouping by BOTH id and name, column=['ResourceID', 'ResourceName'], filtering on the RESOLVED values: exact filters, or fuzzy=True with an alternation of the exact chosen values when the user picked several. This returns id, name and peer count together — ONE call, no follow-up lookup.
  d. If too few results, broaden WITHIN the dimension you are using, never across into the other one:
     - business hierarchy: C_FUNCTIONNAME -> C_SEGMENTNAME -> C_SECTORNAME -> C_AREANAME
     - financial: OU -> ParentOU
     Never drop JOBTITLE.
  e. Present as a table: ResourceID, Resource Name, Peer Count. Sort by Peer Count descending. Descriptions are not in this table: when the user asks about specific rows, fetch them with filter_dataset on those ResourceIDs and columns=['ResourceID', 'ResourceDescription'].

Strategy 2 — Search by description (finding rights, not people):
  a. search_dataset on Resources with the description. Use Resources, NOT Entitlements: the catalogue includes rights nobody holds yet, and Entitlements is far past the free-text size gate anyway.
  b. Present matches with name, description, RequestingSystem.

Strategy 3 — Explore the organisation:
  - Business hierarchy first: get_column_values on C_AREANAME (broadest, small list) to offer recognisable choices; count_by_column with fuzzy=True on C_SEGMENTNAME or C_FUNCTIONNAME to discover matches with counts (e.g. count_by_column(dataset='Entitlements', column='C_SEGMENTNAME', filters={'C_SEGMENTNAME': 'tiso'}, fuzzy=True)).
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
- Never show a raw ResourceID without its name. ResourceName is inline on Entitlements, so this costs you nothing — project it or group by it. Go to Resources only for DESCRIPTION.
- Pass columns=[...] on every filter call, and group by [id, name] rather than counting ids and looking names up afterwards. Each avoided tool call removes a full model round-trip, which is the dominant cost of a turn — the lookups themselves are milliseconds.
- NEVER re-query data you already have. Before every tool call, check the conversation for a result that already answers it: the same rows under a different tool, the same person's assignments fetched again, a column you already projected. Re-fetching costs a full round-trip and returns what you are already holding. Answer from what you have.
- Do the work the user asked for and nothing more. Extra searches they did not request are not thoroughness -- each one adds a round-trip and lengthens the answer. Offer the next step in one sentence and let them choose.
- NEVER project or group by ResourceDescription unless the user asked what a right grants. It is long free text: projecting it across an assignment list, or grouping by it, is the main cause of a slow answer.
- Do not call search_dataset on Entitlements — it is far past the free-text size gate and will only return guidance. Free-text search belongs on Resources.
- If a fuzzy filter returns nothing, try a broader pattern or fewer filter columns.
- Column names must come from list_datasets. A filter naming a column that does not exist is an ERROR: you get {error, available_columns, hint} and NO rows. Correct the name from available_columns and retry the same call — do not drop the filter, and do not report "nothing found". This applies to count_by_column's filters too. For identity lookups, still verify the returned rows carry the value you filtered on.
- Never fuzzy-match an identifier (EMPLOYEEID, ResourceID) when you mean one specific record.
- Never mix business hierarchy and financial (OU) terms in one filter set, and never broaden from one into the other.

=== OUTPUT ===

Provide your answer directly to the user. If the data does not cover the user's question, say so plainly.

Never shorten, sample, truncate or summarise the DATA. Every row you retrieved is listed in full, however many there are. The budget below governs PROSE ONLY.

Before the list or table — at most ONE sentence, and it must carry the provenance:
- identity lookup: whose access this is, and that the rows you are showing carry that GPN, e.g. "The 45 rights below are held by GPN 40123456."
- resolved term: the population you used, e.g. "Using 'Senior Software Engineer' in Technology — 142 people."
Do not restate the question and do not narrate which tools you called.

After the list or table — at most TWO short lines, drawn ONLY from these, never invented:
- the description offer ("ask me about any of these and I will explain what it grants");
- the Strategy 0 peer-widening offer (one sentence, offer only — never run it unasked);
- the out-of-scope sentence for a mixed question's process part — this one is NEVER optional when the user asked one, and it does not count against the budget;
- a gap in what the data could answer.
Nothing else: no "Key observations", no "Summary", no section heading around a single sentence, no restatement of what the table already shows.

For peer-recommendation results, present a table with these columns: ResourceID, Resource Name, Peer Count. Sort by Peer Count descending. Add Requesting System when you have it. Descriptions are NOT in this table — offer them and fetch them only for the ones the user picks, with ResourceDescription projected for just those ResourceIDs.
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


# -- Graph builder --

def build_graph(llm, all_tools):
    """Build the single-agent ReAct graph.

        START -> resource_agent <-> resource_tools -> END

    all_tools is everything the MCP server offered; only the resource
    group is bound (see RESOURCE_TOOL_NAMES). The rest stay loaded but
    invisible to the model.

    Two nodes, three edges, no routing state: the conditional edge
    reads the last message and sends tool calls to the tool node,
    anything else to END. Compile with a checkpointer to get sessions.
    """
    tools = [t for t in all_tools if t.name in RESOURCE_TOOL_NAMES]
    dropped = sorted({t.name for t in all_tools} - RESOURCE_TOOL_NAMES)
    missing = sorted(RESOURCE_TOOL_NAMES - {t.name for t in all_tools})
    logger.info("Binding %d resource tools (not bound: %s)", len(tools), ", ".join(dropped) or "none")
    if missing:
        # not fatal -- the prompt degrades rather than breaking -- but it
        # always means the server and this list have drifted apart
        logger.warning("Expected resource tools missing from the MCP server: %s", ", ".join(missing))

    llm_with_tools = llm.bind_tools(tools)

    async def resource_agent(state: MessagesState) -> dict:
        """Call the LLM with the system prompt plus the trimmed history."""
        messages = _trim_messages(state["messages"])
        response = await llm_with_tools.ainvoke(
            [SystemMessage(content=RESOURCE_PROMPT)] + messages
        )
        return {"messages": [response]}

    def should_continue(state: MessagesState) -> str:
        """Tool calls -> run them; a plain answer -> the turn is done."""
        last_msg = state["messages"][-1]
        if getattr(last_msg, "tool_calls", None):
            return "resource_tools"
        return END

    graph = StateGraph(MessagesState)
    graph.add_node("resource_agent", resource_agent)
    graph.add_node("resource_tools", ToolNode(tools))
    graph.add_edge(START, "resource_agent")
    graph.add_conditional_edges(
        "resource_agent",
        should_continue,
        {"resource_tools": "resource_tools", END: END},
    )
    # tool results always go back to the model, which either answers or
    # asks for more tools -- this edge is the loop in "tool loop"
    graph.add_edge("resource_tools", "resource_agent")

    return graph


# -- MCP config --

def _get_mcp_server_config() -> dict:
    """Build MCP server connection config from env vars.

    stdio -- the default on this branch: we spawn the server ourselves
             as a child process and talk over pipes. Needs
             MCP_SERVER_COMMAND + MCP_SERVER_ARGS. No auth, because
             there is no listener to authenticate
    streamable-http -- the split topology. Needs MCP_SERVER_URL
             (e.g. http://host:8000/mcp); when MCP_SERVER_TOKEN is set
             it is sent as a bearer token so the server can
             authenticate this agent deployment
    sse   -- legacy HTTP transport, kept for rollback. Same env vars
             (URL path is /sse instead of /mcp)

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
        # the child runs in the MCP SERVER's project environment, not
        # ours. VIRTUAL_ENV names the agent's venv, so forwarding it
        # tells the child a virtualenv is active that it is not using:
        # uv prints "VIRTUAL_ENV=... does not match the project
        # environment path ... and will be ignored" on every spawn.
        # Harmless (uv does ignore it and --project wins) but it is
        # noise in exactly the logs you read when a spawn goes wrong.
        child_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        logger.info("MCP server=%s transport=stdio command=%s args=%s", server_name, command, args)
        return {
            server_name: {
                "command": command,
                "args": args,
                "transport": "stdio",
                # pass our environment explicitly. The MCP SDK does NOT
                # inherit it: when this key is absent it forwards only
                # get_default_environment() -- HOME, LOGNAME, PATH,
                # SHELL, TERM, USER -- and every MCP_* setting is
                # dropped, so the server silently starts on its
                # built-in defaults (csv mode, db file beside the docs
                # dir). Wrong data, no error. The child is our own
                # process in our own trust domain, so the SDK's
                # allowlist buys us nothing here.
                "env": child_env,
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

# spinner labels per graph node. The API reads this same map to turn
# node starts into SSE phase events, so the keys must stay in step with
# the node names in build_graph.
_NODE_PHASES = {
    "resource_agent": "Generating answer",
    "resource_tools": "Calling tools",
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
    # one held session, not one per tool call -- over stdio the adapter
    # would otherwise spawn a fresh server subprocess for every call
    # (docs/single-container-stdio.md section 4.2)
    stack = AsyncExitStack()
    session = await stack.enter_async_context(
        client.session(next(iter(mcp_config))))
    tools = await load_mcp_tools(session)
    logger.info("Loaded %d MCP tools", len(tools))

    try:
        # in-memory checkpointer: this conversation lives in this
        # process and is gone when it exits
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
    finally:
        await stack.aclose()


async def _stream_response(agent, user_input: str, config: dict) -> tuple[str, bool]:
    """Run the graph with streaming. Shows a spinner while tools run,
    then streams the final answer token-by-token.

    Returns (answer_text, was_streamed).
    """
    spinner = Spinner("Thinking")
    await spinner.start()

    # streaming is a one-shot latch: False while the LLM is calling tools,
    # flips to True on the first displayable answer token. It serves two
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

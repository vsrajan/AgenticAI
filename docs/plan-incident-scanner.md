# Incident Scanner -- Implementation Plan

## Context

Users want to correlate access governance incidents with knowledgebase documentation to surface knowledge gaps. The hybrid approach puts incident data on the MCP server (as a CSV, easily swapped for ServiceNow later) and adds a 4th specialist agent ("scanner") that uses both incident tools and knowledgebase tools directly.

**Key design decisions (already confirmed):**
- Scanner calls KB MCP tools directly (search_docs, read_page) -- no changes to KB agent prompt
- When KB has no answer for an incident, scanner flags it as "no documentation coverage"
- Only 2 incident MCP tools (list + search) -- thin wrappers around CsvStore, easy to swap for ServiceNow

---

## Changes

### 1. Create `mcp-server/docs/Incidents.csv`

ServiceNow-style sample data with 10-15 incidents. Columns:

```
IncidentID, ShortDescription, Description, Priority, State, Category, Subcategory, AssignmentGroup, AssignedTo, OpenedDate, ResolvedDate, ResolutionNotes
```

Cover common access governance scenarios: password resets, delegation failures, provisioning gaps, deprovisioning issues, certification campaigns, SoD violations. CsvStore auto-discovers this -- no loader code needed.

### 2. Add 2 incident tools to `mcp-server/src/mcp_docs_server/server.py`

New `# -- Incident tools --` section after Quality tools:

- **`list_incidents(max_results=50)`** -- returns all incidents via `csv_store.filter_rows("Incidents", ...)`
- **`search_incidents(query, max_results=10)`** -- BM25 keyword search via `csv_store.search("Incidents", ...)`

Both are thin CsvStore wrappers. Update module docstring to include the new group.

### 3. Modify `agent-client/src/agent_client/agent.py`

**3a. Add tool name set** (after `QUALITY_TOOL_NAMES`, ~line 146):
```python
SCANNER_TOOL_NAMES = {
    "list_incidents", "search_incidents",
    "list_topics", "search_docs", "read_page",
}
```

**3b. Update `AgentState` docstring** (~line 153): add `"scanner_agent"` to the active_agent comment.

**3c. Add routing tool** (~line 173):
```python
@tool
def route_to_scanner(reason: str) -> str:
    """Route the query to the Incident Scanner specialist."""
    return reason
```
Add to `ROUTING_TOOLS` list (~line 176).

**3d. Add `SCANNER_PROMPT`** (after `QUALITY_PROMPT`):
- Workflow: retrieve incidents -> for each, search_docs for relevant KB pages -> read_page to confirm coverage -> produce correlation report
- Knowledge gap detection: no search results, or results don't address the incident scenario
- Output format: Incident-Documentation Correlation Table + Knowledge Gap Summary
- Handoff: if question isn't about incident scanning, hand off to router

**3e. Update `ROUTER_PROMPT`**: add scanner as 4th specialist with routing criteria (scan/analyse/review incidents, incident-documentation coverage gaps).

**3f. Update `route_entry`** (~line 656): add `"scanner_agent"` to the valid active agents tuple.

**3g. Update `route_from_router`** (~line 669): add `route_to_scanner` -> `"scanner_agent"` mapping.

**3h. Update `build_graph`** (~line 711):
- Split out scanner_tools from all_tools using SCANNER_TOOL_NAMES
- Add scanner_agent and scanner_tools nodes (same pattern as quality)
- Add scanner_agent to both conditional edge maps (START and router)
- Add scanner sub-loop edges (agent <-> tools, handoff, END)
- Update docstring ASCII diagram

**3i. Update `_NODE_PHASES`**: add `"scanner_agent": "Scanning incidents"` and `"scanner_tools": "Calling tools"`.

### 4. Update documentation

**`CLAUDE.md`**: Update architecture section (10 nodes), MCP tools table (14 tools, add Incident group), agent graph diagram, recent work.

**`docs/architecture.md`**: Add scanner to Mermaid diagrams, conditional edge tables, tool bindings table.

---

## Verification

1. Start MCP server (`cd mcp-server && uv run mcp-docs-server`) -- confirm `list_incidents` and `search_incidents` appear in tool list
2. Start agent client (`cd agent-client && uv run agent-client`)
3. Test routing: ask "scan my incidents" -- should route to scanner
4. Test workflow: scanner retrieves incidents, searches docs, produces gap report
5. Test handoff: ask scanner a non-incident question -- should hand off to router
6. Test existing agents still work (KB, resource, quality queries)

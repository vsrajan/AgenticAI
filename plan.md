# DataQualityChecker -- High-Level Design

## Overview

A new **quality_agent** specialist node that checks resource data
against a configurable quality criteria checklist. Given a set of
ResourceIds, it fetches each resource's data from the Resources
dataset and evaluates every field against the criteria, producing a
structured quality report with pass/fail per criterion and importance
flags.

---

## 1. Quality criteria config

**File**: `mcp-server/docs/quality_criteria.json`

Follows the same pattern as `request_config.json` -- a static JSON
file on the MCP server, exposed via a new MCP tool.

```json
{
  "criteria": [
    {
      "name": "Name",
      "field": "name",
      "description": "Resource must have a non-empty, human-readable name",
      "importance": "Very Important"
    },
    {
      "name": "Description",
      "field": "DESCRIPTION",
      "description": "Resource must have a meaningful description that explains what access is granted",
      "importance": "Very Important"
    },
    {
      "name": "Access Type",
      "field": "AccessType",
      "description": "Resource must specify the type of access granted (Read, Write, Delete, etc.)",
      "importance": "Important"
    },
    {
      "name": "HPU Level Access",
      "field": "HPUAccess",
      "description": "Resource must indicate whether it grants Highly Privileged User level access (Yes/No)",
      "importance": "Important"
    }
  ]
}
```

The user will provide the full criteria list later. The schema is:
- **name**: criterion display name
- **field**: column name in the Resources dataset to inspect (optional -- some criteria may span multiple fields or require semantic evaluation)
- **description**: what the criterion checks and what constitutes a pass
- **importance**: "Very Important", "Important", or "Nice to Have"

---

## 2. New MCP tool: `get_quality_criteria`

**Server**: `mcp-server/src/mcp_docs_server/server.py`

Reads `quality_criteria.json` and returns the full criteria list.
Mirrors the existing `get_request_attributes` pattern:

```python
@mcp.tool()
def get_quality_criteria() -> dict:
    """Return the data quality criteria checklist for resource evaluation."""
    path = DOCS_DIR / "quality_criteria.json"
    return json.loads(path.read_text())
```

This keeps criteria decoupled from the agent prompt -- the agent
fetches them at runtime, so changes to the JSON don't require agent
redeployment.

---

## 3. New graph nodes

### quality_agent (specialist)

LLM node with its own system prompt. Has access to:

- **Resource MCP tools** (to fetch data): `filter_dataset`, `search_dataset`, `list_datasets`, `get_column_values`
- **Quality MCP tool**: `get_quality_criteria`
- **Handoff tool**: `hand_off_to_router`

### quality_tools (tool executor)

Standard `_make_tool_node()` wrapping the quality agent's MCP tools.
Reuses the existing mixed-call handling for handoff.

---

## 4. Agent prompt: `QUALITY_PROMPT`

Instructs the LLM to follow this workflow:

1. **Parse input** -- extract comma-separated ResourceIds from the user message
2. **Load criteria** -- call `get_quality_criteria` to get the checklist
3. **Fetch resources** -- call `filter_dataset` on the Resources dataset for each ResourceId (or batch via `filter_dataset_fuzzy` with a regex pattern like `id1|id2|id3`)
4. **Evaluate** -- for each resource, check each criterion:
   - Is the relevant field present and non-empty?
   - Does the value meet the criterion's description? (semantic evaluation by LLM)
   - Mark as Pass / Fail / Partial
5. **Report** -- produce a structured quality report

### Report format (example)

```
## Data Quality Report

### Resource: ABC123 -- "Finance Read Access"

| Criterion        | Importance     | Status  | Notes                                    |
|------------------|----------------|---------|------------------------------------------|
| Name             | Very Important | Pass    | Has descriptive name                     |
| Description      | Very Important | Fail    | Description is empty                     |
| Access Type      | Important      | Pass    | "Read" specified                         |
| HPU Level Access | Important      | Fail    | Field missing -- cannot determine        |

Quality Score: 2/4 (50%)

### Summary

| ResourceId | Name                 | Score | Very Important Gaps | Status  |
|------------|----------------------|-------|---------------------|---------|
| ABC123     | Finance Read Access  | 2/4   | Description missing | At Risk |
| DEF456     | Admin Write Access   | 4/4   | None                | Good    |
```

---

## 5. Routing changes

### New routing tool

```python
@tool
def route_to_quality_checker(reason: str) -> str:
    """Route the query to the Data Quality Checker specialist."""
    return reason
```

Added to `ROUTING_TOOLS` list.

### Router prompt update

Add a third specialist to `ROUTER_PROMPT`:

```
3. Data Quality Checker -- evaluates the quality of resource data
   against a criteria checklist. Route here when the user asks to
   check, audit, or evaluate the quality of specific resources.
```

Decision rule:
```
- Questions about data quality, auditing resource metadata,
  checking completeness of resource records -> route to Quality Checker.
```

### State

`active_agent` gains a new valid value: `"quality_agent"`.

---

## 6. Graph wiring

Inside `build_graph()`:

```
# quality tool set: subset of resource tools + quality criteria tool
quality_tools = [t for t in all_tools if t.name in QUALITY_TOOL_NAMES]
quality_all_tools = quality_tools + [hand_off_to_router]

# nodes
graph.add_node("quality_agent", _make_agent_node(llm, quality_all_tools, QUALITY_PROMPT))
graph.add_node("quality_tools", _make_tool_node(quality_tools))

# edges -- same sub-loop pattern as other specialists
graph.add_conditional_edges(
    "quality_agent",
    _make_specialist_edge("quality_tools"),
    {"quality_tools": "quality_tools", "handoff": "handoff", END: END},
)
graph.add_edge("quality_tools", "quality_agent")
```

Update `route_entry`, `route_from_router`, and the START conditional
edges to include `"quality_agent"`.

---

## 7. Tool name set

```python
QUALITY_TOOL_NAMES = {
    "get_quality_criteria",
    "filter_dataset",
    "search_dataset",
    "list_datasets",
    "get_column_values",
}
```

The quality agent reuses existing resource tools for data fetching but
does NOT get `raise_entitlement_request`, `count_by_column`, or
`filter_dataset_fuzzy` -- it doesn't need them.

---

## 8. Updated graph topology

```
START -> route_entry()
           |
           +-> quality_agent (if active_agent == "quality_agent")
           +-> knowledgebase_agent (if active_agent == "knowledgebase_agent")
           +-> resource_agent (if active_agent == "resource_agent")
           +-> router (otherwise)
                 |
                 +-> route_to_knowledgebase -> knowledgebase_agent
                 +-> route_to_resource -> resource_agent
                 +-> route_to_quality_checker -> quality_agent
                 +-> (direct answer) -> END

quality_agent <-> quality_tools (sub-loop)
       |
       +-> handoff -> router (context switch)
       +-> END (final answer)
```

---

## 9. File changes summary

| File | Change |
|------|--------|
| `mcp-server/docs/quality_criteria.json` | **New** -- criteria config (placeholder, user will populate) |
| `mcp-server/src/mcp_docs_server/server.py` | Add `get_quality_criteria` tool |
| `agent-client/src/agent_client/agent.py` | Add `QUALITY_TOOL_NAMES`, `QUALITY_PROMPT`, `route_to_quality_checker`, wire new nodes/edges in `build_graph()`, update router prompt and routing logic |
| `CLAUDE.md` | Update architecture docs to reflect new node |
| `docs/architecture.md` | Update Mermaid diagrams |

---

## 10. Key design decisions

1. **LLM-based evaluation** -- the quality agent uses the LLM to
   semantically evaluate criteria (e.g., "is the description
   meaningful?") rather than doing purely mechanical field-presence
   checks. This allows nuanced quality assessment.

2. **Criteria on the server** -- stored as JSON in `mcp-server/docs/`,
   fetched at runtime via MCP tool. Same pattern as
   `request_config.json`. Criteria can be updated without redeploying
   the agent.

3. **Reuse existing resource tools** -- the quality agent fetches data
   through the same MCP tools the resource agent uses
   (`filter_dataset`, etc.). No new data-fetching infrastructure.

4. **Same specialist pattern** -- follows the existing agent/tools
   sub-loop with handoff support. No new architectural patterns
   introduced.

5. **Batch via regex** -- for multiple ResourceIds, use
   `filter_dataset_fuzzy` with `ResourceID: "id1|id2|id3"` to fetch
   all resources in one call rather than N individual calls.
   (Reconsidering: include `filter_dataset_fuzzy` in the tool set for
   this purpose.)

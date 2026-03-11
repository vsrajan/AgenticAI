# Access Governance AI

LangGraph multi-agent system with an MCP tool server for documentation search,
resource discovery, and entitlement management. Powered by Azure OpenAI (GPT-4o).

## Repository structure

```
agent-client/src/agent_client/
  agent.py            — LangGraph StateGraph (router + 3 specialists + handoff)
  llm.py              — Azure OpenAI config
  cli.py              — CLI entry point (interactive agent)
  incident_sources.py — Incident dataclass + CSV/ServiceNow source classes
  scanner.py          — Batch scan engine (reuses agent graph)
  scanner_cli.py      — CLI entry point (batch scanner)

agent-client/data/
  Incidents.csv       — Sample ServiceNow-style incident data (12 incidents)

mcp-server/src/mcp_docs_server/
  server.py      — FastMCP server (12 tools over SSE/stdio)
  pdf_indexer.py — PDF → per-page BM25 index (DocIndex)
  csv_store.py   — CSV → in-memory DataFrame (CsvStore)

docs/
  architecture.md — Mermaid architecture diagrams (high-level, agent graph, MCP tools)
```

## Architecture

The agent is a LangGraph `StateGraph` with 8 nodes:

```
START -> route_entry() -> Router -> knowledgebase_agent / resource_agent / quality_agent
                                      <-> tool loop        <-> tool loop     <-> tool loop
                                    knowledgebase_tools   resource_tools    quality_tools
                                       \          handoff          /
                                             Router (re-route)
                                               -> END
```

- **State**: `AgentState(messages: list, active_agent: str)`
- `active_agent` provides persistent specialist ownership across turns
- Specialists call `hand_off_to_router()` to defer to another specialist
- Mixed questions: specialist answers its part, defers the rest

### MCP tools (12 total)

| Group | Tools |
|-------|-------|
| Knowledgebase (3) | `list_topics`, `search_docs`, `read_page` |
| Resource (6) | `list_datasets`, `search_dataset`, `filter_dataset`, `filter_dataset_fuzzy`, `count_by_column`, `get_column_values` |
| Request (2) | `get_request_attributes`, `raise_entitlement_request` |
| Quality (1) | `get_quality_criteria` |

For full diagrams with conditional edges and data flow, see `docs/architecture.md`.

## Naming conventions

- **Agent-side** uses domain names: `knowledgebase` (not pdf), `resource` (not csv), and `quality`
- **MCP server internals** keep implementation names (`pdf_indexer`, `csv_store`, `CsvStore`, `DocIndex`) — these describe how data is stored

## How to run

```bash
# MCP server
cd mcp-server && uv run mcp-docs-server

# Agent client -- interactive (in a separate terminal)
cd agent-client && uv run agent-client

# Incident scanner -- batch mode (in a separate terminal)
cd agent-client && uv run scan-cli data/Incidents.csv -o scan_results.csv
```

Required env vars: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`

MCP transport: SSE (default, set `MCP_SERVER_URL`) or stdio (set `MCP_SERVER_COMMAND` + `MCP_SERVER_ARGS`)

## Development

Branch: `claude/mcp-html-docs-server-S9jg9`

### Recent work

- Refactored agent from flat ReAct loop to custom StateGraph with Router, specialists, and handoff
- Renamed pdf/csv identifiers to knowledgebase/resource in agent code
- Added `get_request_attributes` and `raise_entitlement_request` MCP tools (dynamic params from `request_config.json`)
- Added mixed-question handling (specialists answer their part, defer the rest)
- Created Mermaid architecture diagrams in `docs/architecture.md`
- Added token-level streaming via astream_events with phase-aware spinner
- Fixed `_dump_history` node attribution -- uses snapshot.next instead of missing metadata["writes"] key (not persisted in LangGraph 1.0.8)
- Normalized comment style: plain characters, `->` arrows, concise docstrings
- Added Data Quality Checker specialist (quality_agent) with 21 criteria from `quality_criteria.json`; evaluates resource metadata dynamically against all CSV columns
- Added standalone incident scanner CLI (scan-cli) -- batch mode that reuses the agent graph without modifying agent.py
- Extracted incident sources to dedicated module (incident_sources.py) with CsvIncidentSource and ServiceNow stub

## General instructions

- Default to **plan mode** -- always present a plan and wait for approval before making changes. Do not use auto-accept unless explicitly told to switch.

## Code style

- **Comments**: Write in plain, human-like English. Use simple alphanumeric characters only.
  - Use `->` for arrows, not `-->`, unicode arrows, or em dashes
  - Use `--` for dashes, not em dashes or unicode
  - Use `+->` for branching, not `└──▶` or other box-drawing characters
  - Keep comments lowercase unless starting a sentence
  - Minimal indentation inside comments -- avoid deeply nested comment formatting
  - Section headers: `# -- Section name --` (not `# ── Section ──────`)
- **Docstrings**: Concise and direct. No RST backtick markup (`` ``var`` ``). Refer to identifiers by name plainly.

## To do

- Refactor `on_response` callback so streamed token output is also pluggable instead of hardcoded to `sys.stdout` -- needed before the agent loop can be embedded in a non-CLI frontend
- Add a web UI frontend to the agent (replace or supplement the CLI interface)

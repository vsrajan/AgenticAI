# Access Governance AI

LangGraph multi-agent system with an MCP tool server for documentation search,
resource discovery, and entitlement management. Powered by Azure OpenAI (GPT-4o).

## Repository structure

```
agent-client/src/ease_clients/
  cli.py              — CLI entry point (interactive agent)
  scanner_cli.py      — CLI entry point (batch scanner)
  agent_api.py        — AgentService (event-stream wrapper) + FastAPI app (HTTP API)
  auth_api.py         — pluggable API auth (static bearer token now, Entra OAuth2 later)
  cli_api.py          — API server entry point (runs uvicorn)

agent-client/src/ease_clients/utils/
  agnes_agent_graph.py — LangGraph StateGraph (router + 3 specialists + handoff)
  llm.py               — Azure OpenAI config
  incident_sources.py  — Incident dataclass + CSV/ServiceNow source classes
  scanner.py           — Batch scan engine (reuses agent graph)

agent-client/
  webclient_api.html  — POC single-page web client for the API (SSE streaming)
  tests_api/          — API test suite (fake agent; no Azure/MCP needed)
  README_api.md       — quick-reference for running the API

agent-client/data/
  Incidents.csv       — Sample ServiceNow-style incident data (12 incidents)

mcp-server/src/mcp_docs_server/
  server.py      — FastMCP server (12 tools over SSE/stdio)
  auth.py        — per-agent bearer-token auth for HTTP transports (TokenVerifier)
  pdf_indexer.py — PDF → per-page BM25 index (DocIndex)
  csv_store.py   — CSV → in-memory DataFrame (CsvStore)

docs/
  architecture.md — Mermaid architecture diagrams (high-level, agent graph, MCP tools)
  agent_api.md    — beginner-oriented guide to the HTTP API layer
  entra_auth_guide.md — Entra ID implementation guide (client->agent + agent->MCP)
  PerformanceRecommendations.md — prod-scale performance analysis + prioritized plan
  architecture_excalidraw.md + 0*.excalidraw — Excalidraw diagrams (05 = API flows)
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

# HTTP API server (in a separate terminal; requires AGENT_API_TOKEN or AGENT_API_AUTH=none)
cd agent-client && uv run agent-api

# API tests (fake agent -- no Azure/MCP needed)
cd agent-client && uv run --with pytest --with httpx pytest tests_api/ -q
```

Required env vars: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`.
All config is read from the single gitignored `agent-client/.env`; `.env.example`
is the complete committed template (agent + API settings).

MCP transport: SSE (default, set `MCP_SERVER_URL`) or stdio (set `MCP_SERVER_COMMAND` + `MCP_SERVER_ARGS`)

MCP auth: the server requires a bearer token per agent on HTTP transports
(`MCP_AUTH=static` fail-closed, `MCP_AUTH_TOKENS=name:token,...`; `none` to
disable). The agent sends `MCP_SERVER_TOKEN` from its .env; the name is a
server-side label used in tool-call audit logs (`caller=agnes`). stdio needs
no auth. See mcp-server/README.md Authentication.

API: bearer-token auth on all endpoints except /health (`AGENT_API_AUTH=static`
with `AGENT_API_TOKEN`, fail-closed; `none` to disable for local dev). Endpoints:
POST /sessions, POST /sessions/{id}/messages (buffered JSON), POST
/sessions/{id}/messages/stream (SSE: phase/token/answer events), GET
/sessions/{id}/messages (history; raw=true for debug dump). Sessions are
explicit (404 for unknown/expired ids), TTL-evicted, LRU-capped, and locked
to one message at a time. See `docs/agent_api.md` for the full guide.

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
- Added HTTP API layer in `_api` files: AgentService event-stream core + FastAPI app (agent_api.py), pluggable bearer-token auth with fail-closed startup (auth_api.py), uvicorn launcher (cli_api.py), CORS support for browser clients
- Added POC single-page web client (webclient_api.html) -- vanilla JS, fetch-based SSE parsing, session reuse
- Added API test suite (tests_api/, 22 tests) using a fake agent -- runs without Azure or MCP
- Added GET /sessions/{id}/messages history endpoint -- chat view by default, raw=true debug dump (types + tool calls)
- Added session hygiene to the API: explicit sessions only (404 for unknown/expired ids), idle-TTL eviction via background sweeper + LRU cap (AGENT_API_SESSION_TTL_MINUTES / AGENT_API_MAX_SESSIONS), per-session lock serialising concurrent messages; web client auto-recreates expired sessions. Eviction is lock-aware: in-flight sessions are never evicted (cap overshoots if everything is mid-turn)
- Added graph execution log to the API (AGENT_API_STREAM_FILE, default agent_api_stream.txt): per-node output appended per turn, session-tagged, same format as the CLI's agent_stream.txt, reset on server start
- Added MCP server auth: per-agent static bearer tokens on HTTP transports via the MCP SDK's TokenVerifier hook (mcp-server auth.py; MCP_AUTH fail-closed, MCP_AUTH_TOKENS name:token pairs), caller identity in all 12 tool log lines, client sends MCP_SERVER_TOKEN from _get_mcp_server_config. mcp-server tests (13); tests_api now 36
- Added beginner-oriented API guide (docs/agent_api.md) and Excalidraw API-flow diagram (docs/05_agent_api_flow.excalidraw)
- Merged the API layer into the main manifests: fastapi/uvicorn deps + agent-api script in pyproject.toml, API settings in .env.example (the temporary pyproject_api.toml / .env_api supersets were removed)
- Restructured the package to match the server deployment: agent_client -> ease_clients, shared internals moved to ease_clients/utils (llm.py, scanner.py, incident_sources.py, and agent.py renamed to agnes_agent_graph.py); entry points and loggers renamed accordingly

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

- Port the interactive CLI onto AgentService events (agnes_agent_graph.py still has its own streaming loop; agent_api.py has the event-based one -- converge them)
- Azure Entra OAuth2 authenticator (`entra` mode in auth_api.py -- JWT/JWKS validation; interface already reserved). See docs/entra_auth_guide.md
- Entra-based auth for the MCP server: replace StaticTokenVerifier with a JWT/JWKS validator in the same TokenVerifier slot (mcp-server auth.py); agent identity from token claims, scopes mapped to tool groups for per-agent authorization. See docs/entra_auth_guide.md
- Replace the POC web client with a real web UI (HTTPS, login flow instead of token-in-url, tightened CORS)

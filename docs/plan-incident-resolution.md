# Incident Scanner -- Implementation Plan (Revised)

## Context

Users want to correlate access governance incidents with knowledgebase documentation to surface knowledge gaps. The scanner is a standalone batch CLI that reuses the existing agent graph without modifying it.

**Key design decisions:**
- agent.py stays completely unchanged -- no new specialist, no new routing
- Scanner is a separate CLI (`scan-cli`) that reads incidents from CSV and runs each through the knowledgebase agent
- Incidents.csv lives on the MCP server (auto-discovered by CsvStore) for interactive queries, but the scanner reads it directly for batch processing
- Each incident gets a fresh graph invocation (no checkpointer, no shared state)
- No new MCP tools -- existing resource tools (`list_datasets`, `search_dataset`, etc.) already work with the auto-discovered Incidents dataset for interactive use

---

## Architecture

```
scan_cli.main()
  +-> CsvIncidentSource.fetch_open_incidents()
  |     reads incidents.csv -> list[Incident]
  |
  +-> scanner.run_scan(incidents, output_path)
        +-> get_llm(), _get_mcp_server_config(), build_graph()
        |
        +-> for each incident:
        |     +-> graph.compile()  (fresh, no checkpointer)
        |     +-> ainvoke(messages=[question], active_agent="knowledgebase_agent")
        |     +-> knowledgebase_agent <-> knowledgebase_tools -> answer
        |     +-> collect ScanResult
        |
        +-> _write_results_csv(results)
```

The scanner skips the router entirely by setting `active_agent="knowledgebase_agent"` in the initial state. This sends each incident straight to the knowledgebase specialist which uses `search_docs` and `read_page` to find relevant documentation.

---

## Files

### New files

| File | Purpose |
|------|---------|
| `agent-client/src/agent_client/scanner.py` | Batch scan engine (see module details below) |
| `agent-client/src/agent_client/scanner_cli.py` | CLI entry point: argparse, .env loading, summary output |
| `mcp-server/docs/Incidents.csv` | Sample ServiceNow-style incident data (12 open incidents) |

### Modified files

| File | Change |
|------|--------|
| `agent-client/pyproject.toml` | Added `scan-cli = "agent_client.scanner_cli:main"` script entry point |
| `CLAUDE.md` | Documented scanner_cli.py and scanner.py in repo structure, added scan-cli to "How to run", added to recent work |
| `docs/architecture.md` | Added section 4 "Incident Scanner -- Batch Flow" with Mermaid diagram |

### Unchanged files

| File | Why unchanged |
|------|---------------|
| `agent-client/src/agent_client/agent.py` | Scanner imports `build_graph` and `_get_mcp_server_config` directly -- no modifications needed |
| `agent-client/src/agent_client/llm.py` | Scanner imports `get_llm` directly |
| `agent-client/src/agent_client/cli.py` | Interactive CLI is unrelated to batch scanning |
| `mcp-server/src/mcp_docs_server/server.py` | CsvStore auto-discovers Incidents.csv -- no new tools needed |

---

## Module details

### scanner.py

**Imports from agent.py (unchanged):**
- `build_graph(llm, all_tools)` -- builds the StateGraph with all specialist nodes
- `_get_mcp_server_config()` -- reads MCP_SERVER_URL / MCP_TRANSPORT env vars

**Imports from llm.py (unchanged):**
- `get_llm()` -- returns configured AzureChatOpenAI instance

**Data classes:**

`Incident` -- one record from the source CSV:
- id, short_description, description, priority, state, category, subcategory, assignment_group, assigned_to, opened_date, resolved_date, resolution_notes

`ScanResult` -- output of scanning one incident:
- incident_id, short_description, category, subcategory, question, answer, has_coverage (bool), matched_topics (list[str])

**CsvIncidentSource:**
- `__init__(csv_path)` -- stores path
- `fetch_open_incidents()` -- reads CSV via DictReader, maps column headers to Incident fields via `_FIELD_MAP`, filters to state in ("open", "in progress", "new")
- `_FIELD_MAP` -- maps CSV headers (IncidentID, ShortDescription, ...) to Incident field names (id, short_description, ...)

**_build_question(incident):**
- Converts an incident into a structured prompt for the knowledgebase agent
- Asks for: relevant documents with citations, whether docs adequately cover the scenario, what specific documentation is missing if there's a gap

**_parse_coverage(answer):**
- Heuristic parser that checks the agent's response for coverage signals
- Negative signals: phrases like "no documentation", "not covered", "documentation gap", "could not find", etc.
- Positive signals: citation patterns matching `(Source: <path>` -- extracts topic paths
- Returns `(has_coverage, topics)` where `has_coverage = bool(topics) and not has_gap`

**run_scan(incidents, output_path):**
- Connects to MCP server via `MultiServerMCPClient` (async context manager)
- Loads MCP tools, builds graph once via `build_graph(llm, tools)`
- For each incident:
  - Compiles graph fresh (`graph.compile()` with no checkpointer)
  - Invokes with `active_agent="knowledgebase_agent"` to bypass router
  - Extracts last AIMessage from final state as the answer
  - Parses coverage via `_parse_coverage`
  - On exception: logs error, appends ScanResult with `has_coverage=False` and error message
- Calls `_write_results_csv` at the end

**_write_results_csv(results, path):**
- Writes CSV with columns: IncidentID, ShortDescription, Category, Subcategory, HasCoverage, MatchedTopics, Answer
- MatchedTopics is semicolon-separated

### scanner_cli.py

- Loads `.env` from `agent-client/.env` (same as cli.py)
- Configures logging to stderr with `AGENT_LOG_LEVEL` env var (default INFO)
- `main()`:
  - argparse with positional `incidents_csv` and optional `-o`/`--output` (default: `scan_results.csv`)
  - Validates input file exists
  - Creates CsvIncidentSource, calls `fetch_open_incidents()`
  - Exits early if no open incidents
  - Calls `asyncio.run(run_scan(incidents, output_path))`
  - Prints summary: N covered, N gap(s) out of N incident(s)

---

## Incidents.csv

12 sample incidents covering common access governance scenarios:

| ID | Category | Subcategory | Scenario |
|----|----------|-------------|----------|
| INC0001 | Access Management | Password Reset | SAP password reset link error |
| INC0002 | Access Management | Delegation | Manager delegation setup fails |
| INC0003 | Provisioning | Joiner | New hire missing standard entitlements |
| INC0004 | Deprovisioning | Leaver | Departed user still has active access |
| INC0005 | Compliance | SoD Violation | Conflicting roles not flagged |
| INC0006 | Compliance | Certification | Campaign stuck in progress |
| INC0007 | Role Management | Role Mining | Inconsistent role mining results |
| INC0008 | Access Management | Access Request | Request pending for 10 days |
| INC0009 | Provisioning | Mover | Bulk transfer failed midway |
| INC0010 | Compliance | Reconciliation | Orphan accounts in Azure AD |
| INC0011 | Access Management | Emergency Access | Break-glass access not revoked |
| INC0012 | Compliance | Data Quality | 200+ entitlements missing descriptions |

All 12 are State=Open with Priority 1-3. CSV columns: IncidentID, ShortDescription, Description, Priority, State, Category, Subcategory, AssignmentGroup, AssignedTo, OpenedDate, ResolvedDate, ResolutionNotes.

---

## How to run

```bash
# start the MCP server (in one terminal)
cd mcp-server && uv run mcp-docs-server

# run the scanner (in another terminal)
cd agent-client && uv run scan-cli ../mcp-server/docs/Incidents.csv -o scan_results.csv
```

The scanner reads the CSV directly, connects to the MCP server for knowledgebase tool calls, and writes results to the output file.

---

## Output format

The results CSV contains:

| Column | Description |
|--------|-------------|
| IncidentID | ID from the source CSV |
| ShortDescription | Brief incident summary |
| Category | Incident category |
| Subcategory | Incident subcategory |
| HasCoverage | True/False -- whether knowledgebase has relevant documentation |
| MatchedTopics | Semicolon-separated list of matched document paths (from citations) |
| Answer | Full knowledgebase agent response with citations |

---

## Verification

1. Start MCP server (`cd mcp-server && uv run mcp-docs-server`)
2. Run scanner (`cd agent-client && uv run scan-cli ../mcp-server/docs/Incidents.csv`)
3. Check that scan_results.csv is produced with one row per open incident (12 rows)
4. Verify HasCoverage and MatchedTopics columns are populated
5. Confirm agent.py was not modified (`git diff agent-client/src/agent_client/agent.py` shows no changes)
6. Existing interactive agent still works (`uv run agent-client`)

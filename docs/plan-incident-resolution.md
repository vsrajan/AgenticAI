# Incident Scanner -- Implementation Plan (Revised)

## Context

Users want to correlate access governance incidents with knowledgebase documentation to surface knowledge gaps. The scanner is a standalone batch CLI that reuses the existing agent graph without modifying it.

**Key design decisions:**
- agent.py stays completely unchanged -- no new specialist, no new routing
- Scanner is a separate CLI (`scan-cli`) that reads incidents from CSV and runs each through the knowledgebase agent
- Incidents.csv lives on the MCP server (auto-discovered by CsvStore) for interactive queries, but the scanner reads it directly for batch processing
- Each incident gets a fresh graph invocation (no checkpointer, no shared state)

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
| `agent-client/src/agent_client/scanner.py` | Batch scan engine: Incident/ScanResult dataclasses, CsvIncidentSource, run_scan(), _write_results_csv() |
| `agent-client/src/agent_client/scanner_cli.py` | CLI entry point: argparse, loads .env, calls run_scan |
| `mcp-server/docs/Incidents.csv` | Sample ServiceNow-style incident data (12 incidents) |

### Modified files

| File | Change |
|------|--------|
| `agent-client/pyproject.toml` | Added `scan-cli` script entry point |
| `CLAUDE.md` | Document scanner CLI |
| `docs/architecture.md` | Add scanner batch flow diagram |

### Unchanged files

| File | Why unchanged |
|------|---------------|
| `agent-client/src/agent_client/agent.py` | Scanner imports build_graph and _get_mcp_server_config -- no modifications needed |
| `mcp-server/src/mcp_docs_server/server.py` | CsvStore auto-discovers Incidents.csv -- no new tools needed |

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
| HasCoverage | True if knowledgebase has relevant documentation |
| MatchedTopics | Semicolon-separated list of matched document paths |
| Answer | Full knowledgebase agent response with citations |

---

## Verification

1. Start MCP server (`cd mcp-server && uv run mcp-docs-server`)
2. Run scanner (`cd agent-client && uv run scan-cli ../mcp-server/docs/Incidents.csv`)
3. Check that scan_results.csv is produced with one row per open incident
4. Verify HasCoverage and MatchedTopics columns are populated
5. Confirm agent.py was not modified (git diff should show no changes)
6. Existing interactive agent still works (`uv run agent-client`)

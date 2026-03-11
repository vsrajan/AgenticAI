# Access Governance AI — Architecture

## 1. High-Level Overview

The system consists of two main components communicating over the
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/):
an **Agent Client** (LangGraph) that orchestrates multi-turn conversations,
and an **MCP Server** (FastMCP) that exposes documentation, data, and
request tools.

```mermaid
%%{init: {'theme': 'default', 'themeVariables': {'fontSize': '12px', 'background': '#ffffff'}, 'flowchart': {'useMaxWidth': false}}}%%
graph LR
    User["User<br/>(CLI / Chat)"]
    User -->|"prompt"| Router

    subgraph AgentClient["Agent Client (LangGraph)"]
        direction TB
        Router["Router Node"]
        KB["Knowledgebase<br/>Agent"]
        Res["Resource<br/>Agent"]
        Qual["Quality<br/>Agent"]
        Handoff["Handoff Node"]
        Router --> KB
        Router --> Res
        Router --> Qual
        KB --> Handoff
        Res --> Handoff
        Qual --> Handoff
        Handoff --> Router
    end

    KB -->|"tool calls<br/>MCP protocol<br/>(SSE / stdio)"| MCP
    Res --> MCP
    Qual --> MCP

    subgraph MCP["MCP Server (FastMCP)"]
        direction TB
        KBTools["Knowledgebase Tools"]
        ResTools["Resource Tools"]
        ReqTools["Request Tools"]
    end

    KB <-->|"LLM calls"| LLM["Azure OpenAI<br/>(GPT-4o)"]
    Res <--> LLM
    Qual <--> LLM

    MCP --> Data["Data Sources<br/>PDF docs · CSV files<br/>request_config.json"]

    style AgentClient fill:#d6e4f0,stroke:#2b579a,stroke-width:2px,color:#000
    style MCP fill:#e0f2f1,stroke:#00696b,stroke-width:2px,color:#000
    style LLM fill:#e3f2fd,stroke:#2b579a,stroke-width:2px,color:#000
    style Data fill:#f5f5f5,stroke:#666,stroke-width:1px,color:#000
    style User fill:#e0e0e0,stroke:#666,stroke-width:1px,color:#000
    style Router fill:#bbdefb,stroke:#2b579a,stroke-width:2px,color:#000
    style KB fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#000
    style Res fill:#fff3e0,stroke:#e65c00,stroke-width:2px,color:#000
    style Qual fill:#e1f5fe,stroke:#0277bd,stroke-width:2px,color:#000
    style Handoff fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px,color:#000
    style KBTools fill:#e8f5e9,stroke:#2e7d32,stroke-width:1px,color:#000
    style ResTools fill:#fff3e0,stroke:#e65c00,stroke-width:1px,color:#000
    style ReqTools fill:#f3e5f5,stroke:#6a1b9a,stroke-width:1px,color:#000
```

---

## 2. LangGraph Agent Graph

The agent is built as a **LangGraph StateGraph** with 8 nodes and
conditional edges that route based on LLM tool-call decisions.
State tracks `messages[]` and `active_agent` (persistent specialist
ownership across turns).

```mermaid
%%{init: {'theme': 'default', 'themeVariables': {'fontSize': '12px', 'background': '#ffffff'}, 'flowchart': {'useMaxWidth': false}}}%%
graph TD
    START(["START"])

    START -->|"active_agent set<br/>-> resume specialist"| KB_AGENT
    START -->|"active_agent set<br/>-> resume specialist"| RES_AGENT
    START -->|"active_agent set<br/>-> resume specialist"| QUAL_AGENT
    START -->|"no active_agent<br/>-> go to router"| ROUTER

    ROUTER{"Router<br/><small>LLM decides routing<br/>using route_to_knowledgebase(),<br/>route_to_resource(), or<br/>route_to_quality_checker()</small>"}

    ROUTER -->|"called route_to_knowledgebase"| KB_AGENT
    ROUTER -->|"called route_to_resource"| RES_AGENT
    ROUTER -->|"called route_to_quality_checker"| QUAL_AGENT
    ROUTER -->|"direct answer<br/>(greeting / chat)"| END_R(["END"])

    subgraph kb_loop ["Knowledgebase Specialist Loop"]
        KB_AGENT["knowledgebase_agent<br/><small>Doc search specialist</small>"]
        KB_TOOLS["knowledgebase_tools<br/><small>list_topics · search_docs<br/>read_page</small>"]
        KB_AGENT -->|"has domain<br/>tool calls"| KB_TOOLS
        KB_TOOLS -->|"return results"| KB_AGENT
    end

    subgraph res_loop ["Resource Specialist Loop"]
        RES_AGENT["resource_agent<br/><small>Data lookup specialist</small>"]
        RES_TOOLS["resource_tools<br/><small>filter_dataset · search_dataset<br/>count_by_column · +5 more</small>"]
        RES_AGENT -->|"has domain<br/>tool calls"| RES_TOOLS
        RES_TOOLS -->|"return results"| RES_AGENT
    end

    subgraph qual_loop ["Quality Specialist Loop"]
        QUAL_AGENT["quality_agent<br/><small>Data quality checker</small>"]
        QUAL_TOOLS["quality_tools<br/><small>get_quality_criteria ·<br/>filter_dataset_fuzzy · +4 more</small>"]
        QUAL_AGENT -->|"has domain<br/>tool calls"| QUAL_TOOLS
        QUAL_TOOLS -->|"return results"| QUAL_AGENT
    end

    KB_AGENT -->|"hand_off_to_router<br/>only (no domain tools)"| HANDOFF
    RES_AGENT -->|"hand_off_to_router<br/>only (no domain tools)"| HANDOFF
    QUAL_AGENT -->|"hand_off_to_router<br/>only (no domain tools)"| HANDOFF

    HANDOFF["handoff<br/><small>Clears active_agent</small>"]
    HANDOFF -->|"re-route"| ROUTER

    KB_AGENT -->|"final answer<br/>(no tool calls)"| END_KB(["END"])
    RES_AGENT -->|"final answer<br/>(no tool calls)"| END_RES(["END"])
    QUAL_AGENT -->|"final answer<br/>(no tool calls)"| END_QUAL(["END"])

    style START fill:#90caf9,stroke:#2b579a,stroke-width:2px,color:#000
    style ROUTER fill:#bbdefb,stroke:#2b579a,stroke-width:2px,color:#000
    style KB_AGENT fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#000
    style KB_TOOLS fill:#c8e6c9,stroke:#2e7d32,stroke-width:1px,color:#000
    style RES_AGENT fill:#fff3e0,stroke:#e65c00,stroke-width:2px,color:#000
    style RES_TOOLS fill:#ffe0b2,stroke:#e65c00,stroke-width:1px,color:#000
    style QUAL_AGENT fill:#e1f5fe,stroke:#0277bd,stroke-width:2px,color:#000
    style QUAL_TOOLS fill:#b3e5fc,stroke:#0277bd,stroke-width:1px,color:#000
    style HANDOFF fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px,color:#000
    style END_R fill:#ef9a9a,stroke:#c62828,stroke-width:2px,color:#000
    style END_KB fill:#ef9a9a,stroke:#c62828,stroke-width:2px,color:#000
    style END_RES fill:#ef9a9a,stroke:#c62828,stroke-width:2px,color:#000
    style END_QUAL fill:#ef9a9a,stroke:#c62828,stroke-width:2px,color:#000
    style kb_loop fill:#f1f8e9,stroke:#2e7d32,stroke-width:1px,stroke-dasharray:5,color:#000
    style res_loop fill:#fff8e1,stroke:#e65c00,stroke-width:1px,stroke-dasharray:5,color:#000
    style qual_loop fill:#e1f5fe,stroke:#0277bd,stroke-width:1px,stroke-dasharray:5,color:#000
```

### Conditional Edge Summary

| Edge function | From | Condition | Routes to |
|---|---|---|---|
| `route_entry()` | START | `active_agent` is set | Resume that specialist directly |
| | | `active_agent` is empty | Router |
| `route_from_router()` | Router | Called `route_to_knowledgebase` | knowledgebase_agent |
| | | Called `route_to_resource` | resource_agent |
| | | Called `route_to_quality_checker` | quality_agent |
| | | No tool call (direct answer) | END |
| `specialist_edge()` | Specialist | Has domain tool calls | Specialist's tool node |
| | | `hand_off_to_router` only | Handoff |
| | | No tool calls (final answer) | END |

---

## 3. MCP Server — Tool Architecture

The MCP server exposes **12 tools** organized into four groups.
Tools are discovered dynamically by the agent client at startup via the
MCP protocol.

```mermaid
%%{init: {'theme': 'default', 'themeVariables': {'fontSize': '12px', 'background': '#ffffff'}, 'flowchart': {'useMaxWidth': false}}}%%
graph TD
    subgraph MCP_SERVER ["MCP Server (FastMCP)"]
        direction TB

        subgraph KB_GROUP ["Knowledgebase Tools"]
            T1["list_topics()<br/><small>Return all indexed topic names</small>"]
            T2["search_docs(query, topic?)<br/><small>BM25 search across PDF pages;<br/>returns ranked snippets</small>"]
            T3["read_page(topic, page)<br/><small>Read full text of a specific<br/>PDF page by number</small>"]
        end

        subgraph RES_GROUP ["Resource Tools"]
            T4["list_datasets()<br/><small>List available CSV dataset names</small>"]
            T5["search_dataset(dataset, query)<br/><small>Keyword search across all columns</small>"]
            T6["filter_dataset(dataset, col, value)<br/><small>Exact-match filter on a column</small>"]
            T7["filter_dataset_fuzzy(dataset, col, value, thresh?)<br/><small>Fuzzy filter via Levenshtein distance</small>"]
            T8["count_by_column(dataset, col)<br/><small>Value frequency counts</small>"]
            T9["get_column_values(dataset, col, n?)<br/><small>Unique values in a column</small>"]
        end

        subgraph REQ_GROUP ["Request Tools"]
            T10["get_request_attributes()<br/><small>Return required/optional fields<br/>for an entitlement request</small>"]
            T11["raise_entitlement_request(**kwargs)<br/><small>Submit an entitlement access<br/>request (placeholder)</small>"]
        end

        subgraph QUAL_GROUP ["Quality Tools"]
            T12["get_quality_criteria()<br/><small>Return data quality criteria<br/>checklist for resource evaluation</small>"]
        end
    end

    KB_GROUP -->|"reads"| DOC_IDX["DocIndex<br/><small>PDF -> per-page BM25 index</small>"]
    RES_GROUP -->|"queries"| CSV_STORE["CsvStore<br/><small>CSV -> in-memory DataFrame</small>"]
    REQ_GROUP -->|"reads schema"| REQ_CFG["request_config.json<br/><small>Dynamic parameter schema</small>"]
    QUAL_GROUP -->|"reads criteria"| QUAL_CFG["quality_criteria.json<br/><small>Quality criteria checklist</small>"]

    style MCP_SERVER fill:#e0f2f1,stroke:#00696b,stroke-width:2px,color:#000
    style KB_GROUP fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#000
    style RES_GROUP fill:#fff3e0,stroke:#e65c00,stroke-width:2px,color:#000
    style REQ_GROUP fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px,color:#000
    style DOC_IDX fill:#c8e6c9,stroke:#2e7d32,stroke-width:1px,color:#000
    style CSV_STORE fill:#ffe0b2,stroke:#e65c00,stroke-width:1px,color:#000
    style REQ_CFG fill:#e1bee7,stroke:#6a1b9a,stroke-width:1px,color:#000
    style T1 fill:#fff,stroke:#2e7d32,color:#000
    style T2 fill:#fff,stroke:#2e7d32,color:#000
    style T3 fill:#fff,stroke:#2e7d32,color:#000
    style T4 fill:#fff,stroke:#e65c00,color:#000
    style T5 fill:#fff,stroke:#e65c00,color:#000
    style T6 fill:#fff,stroke:#e65c00,color:#000
    style T7 fill:#fff,stroke:#e65c00,color:#000
    style T8 fill:#fff,stroke:#e65c00,color:#000
    style T9 fill:#fff,stroke:#e65c00,color:#000
    style T10 fill:#fff,stroke:#6a1b9a,color:#000
    style T11 fill:#fff,stroke:#6a1b9a,color:#000
    style QUAL_GROUP fill:#e1f5fe,stroke:#0277bd,stroke-width:2px,color:#000
    style QUAL_CFG fill:#b3e5fc,stroke:#0277bd,stroke-width:1px,color:#000
    style T12 fill:#fff,stroke:#0277bd,color:#000
```

### Tool Bindings per Agent Node

| Agent Node | Bound Tools | Notes |
|---|---|---|
| **Router** | `route_to_knowledgebase(reason)`, `route_to_resource(reason)`, `route_to_quality_checker(reason)` | Internal routing tools (not MCP). Can also answer directly. |
| **knowledgebase_agent** | `list_topics`, `search_docs`, `read_page`, `hand_off_to_router` | MCP tools + handoff |
| **resource_agent** | `list_datasets`, `search_dataset`, `filter_dataset`, `filter_dataset_fuzzy`, `count_by_column`, `get_column_values`, `get_request_attributes`, `raise_entitlement_request`, `hand_off_to_router` | MCP tools + handoff |
| **quality_agent** | `get_quality_criteria`, `filter_dataset`, `filter_dataset_fuzzy`, `search_dataset`, `list_datasets`, `get_column_values`, `hand_off_to_router` | MCP tools (shared resource tools + quality tool) + handoff |
| **handoff** | *(none -- processes pending `hand_off_to_router` calls)* | Clears `active_agent` and returns to Router |

---

## 4. Incident Scanner -- Batch Flow

The scanner CLI (`scan-cli`) is a standalone batch tool that reuses the
existing agent graph without modification. It reads incidents from a CSV,
runs each through the knowledgebase agent, and writes a coverage report.

```mermaid
%%{init: {'theme': 'default', 'themeVariables': {'fontSize': '12px', 'background': '#ffffff'}, 'flowchart': {'useMaxWidth': false}}}%%
graph TD
    CLI["scan-cli<br/><small>scanner_cli.main()</small>"]
    CSV_IN["Incidents CSV<br/><small>CsvIncidentSource</small>"]
    SCAN["scanner.run_scan()<br/><small>batch engine</small>"]

    CLI -->|"read"| CSV_IN
    CSV_IN -->|"list[Incident]"| SCAN

    SCAN -->|"for each incident"| COMPILE["graph.compile()<br/><small>fresh, no checkpointer</small>"]
    COMPILE -->|"ainvoke<br/>active_agent=knowledgebase_agent"| KB["knowledgebase_agent<br/><small>reused from agent.py</small>"]

    KB <-->|"search_docs, read_page<br/>list_topics"| MCP["MCP Server<br/><small>knowledgebase tools</small>"]
    KB <-->|"LLM calls"| LLM["Azure OpenAI"]

    KB -->|"answer"| RESULT["ScanResult<br/><small>has_coverage, matched_topics</small>"]
    RESULT -->|"collect all"| CSV_OUT["scan_results.csv<br/><small>_write_results_csv()</small>"]

    style CLI fill:#e0e0e0,stroke:#666,stroke-width:2px,color:#000
    style CSV_IN fill:#f5f5f5,stroke:#666,stroke-width:1px,color:#000
    style SCAN fill:#d6e4f0,stroke:#2b579a,stroke-width:2px,color:#000
    style COMPILE fill:#bbdefb,stroke:#2b579a,stroke-width:1px,color:#000
    style KB fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px,color:#000
    style MCP fill:#e0f2f1,stroke:#00696b,stroke-width:2px,color:#000
    style LLM fill:#e3f2fd,stroke:#2b579a,stroke-width:2px,color:#000
    style RESULT fill:#fff3e0,stroke:#e65c00,stroke-width:1px,color:#000
    style CSV_OUT fill:#f5f5f5,stroke:#666,stroke-width:1px,color:#000
```

Key points:
- **No router involved** -- `active_agent="knowledgebase_agent"` bypasses routing
- **Fresh graph per incident** -- no shared conversation state between incidents
- **Reuses agent.py** -- imports `build_graph` and `_get_mcp_server_config` directly
- **Coverage heuristic** -- parses citations from the agent response to detect gaps

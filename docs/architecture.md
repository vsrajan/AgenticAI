# Access Governance AI — Architecture

## 1. High-Level Overview

The system consists of two main components communicating over the
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/):
an **Agent Client** (LangGraph) that orchestrates multi-turn conversations,
and an **MCP Server** (FastMCP) that exposes documentation, data, and
request tools.

```mermaid
graph LR
    User["👤 User<br/>(CLI / Chat)"]
    User -->|"prompt"| Agent

    subgraph AgentClient["Agent Client (LangGraph)"]
        direction TB
        Router["Router Node"]
        KB["Knowledgebase<br/>Agent"]
        Res["Resource<br/>Agent"]
        Handoff["Handoff Node"]
        Router --> KB
        Router --> Res
        KB --> Handoff
        Res --> Handoff
        Handoff --> Router
    end

    Agent -->|"tool calls<br/>MCP protocol<br/>(SSE / stdio)"| MCP

    subgraph MCP["MCP Server (FastMCP)"]
        direction TB
        KBTools["Knowledgebase Tools"]
        ResTools["Resource Tools"]
        ReqTools["Request Tools"]
    end

    Agent <-->|"LLM calls"| LLM["Azure OpenAI<br/>(GPT-4o)"]

    MCP --> Data["Data Sources<br/>PDF docs · CSV files<br/>request_config.json"]

    style AgentClient fill:#d6e4f0,stroke:#2b579a,stroke-width:2px
    style MCP fill:#e0f2f1,stroke:#00696b,stroke-width:2px
    style LLM fill:#e3f2fd,stroke:#2b579a,stroke-width:2px
    style Data fill:#f5f5f5,stroke:#666,stroke-width:1px
    style User fill:#e0e0e0,stroke:#666,stroke-width:1px
    style Router fill:#bbdefb,stroke:#2b579a,stroke-width:2px
    style KB fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style Res fill:#fff3e0,stroke:#e65c00,stroke-width:2px
    style Handoff fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px
    style KBTools fill:#e8f5e9,stroke:#2e7d32,stroke-width:1px
    style ResTools fill:#fff3e0,stroke:#e65c00,stroke-width:1px
    style ReqTools fill:#f3e5f5,stroke:#6a1b9a,stroke-width:1px
```

---

## 2. LangGraph Agent Graph

The agent is built as a **LangGraph StateGraph** with 6 nodes and
conditional edges that route based on LLM tool-call decisions.
State tracks `messages[]` and `active_agent` (persistent specialist
ownership across turns).

```mermaid
graph TD
    START(["START"])

    START -->|"active_agent set<br/>→ resume specialist"| KB_AGENT
    START -->|"active_agent set<br/>→ resume specialist"| RES_AGENT
    START -->|"no active_agent<br/>→ go to router"| ROUTER

    ROUTER{"Router<br/><small>LLM decides routing<br/>using route_to_knowledgebase()<br/>or route_to_resource()</small>"}

    ROUTER -->|"called route_to_knowledgebase"| KB_AGENT
    ROUTER -->|"called route_to_resource"| RES_AGENT
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

    KB_AGENT -->|"hand_off_to_router<br/>only (no domain tools)"| HANDOFF
    RES_AGENT -->|"hand_off_to_router<br/>only (no domain tools)"| HANDOFF

    HANDOFF["handoff<br/><small>Clears active_agent</small>"]
    HANDOFF -->|"re-route"| ROUTER

    KB_AGENT -->|"final answer<br/>(no tool calls)"| END_KB(["END"])
    RES_AGENT -->|"final answer<br/>(no tool calls)"| END_RES(["END"])

    style START fill:#1a365d,stroke:#1a365d,color:#fff
    style ROUTER fill:#bbdefb,stroke:#2b579a,stroke-width:2px
    style KB_AGENT fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style KB_TOOLS fill:#c8e6c9,stroke:#2e7d32,stroke-width:1px
    style RES_AGENT fill:#fff3e0,stroke:#e65c00,stroke-width:2px
    style RES_TOOLS fill:#ffe0b2,stroke:#e65c00,stroke-width:1px
    style HANDOFF fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px
    style END_R fill:#c62828,stroke:#c62828,color:#fff
    style END_KB fill:#c62828,stroke:#c62828,color:#fff
    style END_RES fill:#c62828,stroke:#c62828,color:#fff
    style kb_loop fill:#f1f8e9,stroke:#2e7d32,stroke-width:1px,stroke-dasharray:5
    style res_loop fill:#fff8e1,stroke:#e65c00,stroke-width:1px,stroke-dasharray:5
```

### Conditional Edge Summary

| Edge function | From | Condition | Routes to |
|---|---|---|---|
| `route_entry()` | START | `active_agent` is set | Resume that specialist directly |
| | | `active_agent` is empty | Router |
| `route_from_router()` | Router | Called `route_to_knowledgebase` | knowledgebase_agent |
| | | Called `route_to_resource` | resource_agent |
| | | No tool call (direct answer) | END |
| `specialist_edge()` | Specialist | Has domain tool calls | Specialist's tool node |
| | | `hand_off_to_router` only | Handoff |
| | | No tool calls (final answer) | END |

---

## 3. MCP Server — Tool Architecture

The MCP server exposes **11 tools** organized into three groups.
Tools are discovered dynamically by the agent client at startup via the
MCP protocol.

```mermaid
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
    end

    KB_GROUP -->|"reads"| DOC_IDX["DocIndex<br/><small>PDF → per-page BM25 index</small>"]
    RES_GROUP -->|"queries"| CSV_STORE["CsvStore<br/><small>CSV → in-memory DataFrame</small>"]
    REQ_GROUP -->|"reads schema"| REQ_CFG["request_config.json<br/><small>Dynamic parameter schema</small>"]

    style MCP_SERVER fill:#e0f2f1,stroke:#00696b,stroke-width:2px
    style KB_GROUP fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style RES_GROUP fill:#fff3e0,stroke:#e65c00,stroke-width:2px
    style REQ_GROUP fill:#f3e5f5,stroke:#6a1b9a,stroke-width:2px
    style DOC_IDX fill:#c8e6c9,stroke:#2e7d32,stroke-width:1px
    style CSV_STORE fill:#ffe0b2,stroke:#e65c00,stroke-width:1px
    style REQ_CFG fill:#e1bee7,stroke:#6a1b9a,stroke-width:1px
    style T1 fill:#fff,stroke:#2e7d32
    style T2 fill:#fff,stroke:#2e7d32
    style T3 fill:#fff,stroke:#2e7d32
    style T4 fill:#fff,stroke:#e65c00
    style T5 fill:#fff,stroke:#e65c00
    style T6 fill:#fff,stroke:#e65c00
    style T7 fill:#fff,stroke:#e65c00
    style T8 fill:#fff,stroke:#e65c00
    style T9 fill:#fff,stroke:#e65c00
    style T10 fill:#fff,stroke:#6a1b9a
    style T11 fill:#fff,stroke:#6a1b9a
```

### Tool Bindings per Agent Node

| Agent Node | Bound Tools | Notes |
|---|---|---|
| **Router** | `route_to_knowledgebase(reason)`, `route_to_resource(reason)` | Internal routing tools (not MCP). Can also answer directly. |
| **knowledgebase_agent** | `list_topics`, `search_docs`, `read_page`, `hand_off_to_router` | MCP tools + handoff |
| **resource_agent** | `list_datasets`, `search_dataset`, `filter_dataset`, `filter_dataset_fuzzy`, `count_by_column`, `get_column_values`, `get_request_attributes`, `raise_entitlement_request`, `hand_off_to_router` | MCP tools + handoff |
| **handoff** | *(none — processes pending `hand_off_to_router` calls)* | Clears `active_agent` and returns to Router |

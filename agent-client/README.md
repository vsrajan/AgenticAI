# Agent Client — Access Governance Assistant

LangGraph ReAct agent that helps users find access rights, discover peer
entitlements, and understand the Access Governance application by consulting
PDF documentation and CSV data served via an
[MCP server](../mcp-server/).

## Architecture

```
                          agent-client                    mcp-server
                     ┌─────────────────────┐        ┌──────────────────┐
User  ──▶  CLI  ──▶  │  LangGraph ReAct    │──SSE──▶│  MCP server      │
                     │  agent              │  or    │  (PDFs + CSVs)   │
                     │      │              │ stdio  │                  │
                     │  AzureOpenAI LLM    │        │  host:port       │
                     └─────────────────────┘        └──────────────────┘
```

The agent connects to an **already-running** MCP server over **SSE** (default)
or **stdio**, loads all available tools, and uses them in a ReAct loop to answer
questions with cited sources and structured data.

The client is completely decoupled from the server — it does **not** start or
manage the server. The server may be running on a different host.

## Features

- **Documentation search** — Search and read PDF documentation with page-level
  citations.
- **Access rights lookup** — Search for access rights by name, description, or
  category.
- **Peer-based recommendations** — Find what access rights peers in the same OU
  and/or location already have, with counts (e.g. "8 of 12 people in Finance /
  London have this").
- **Data exploration** — Discover available datasets, columns, and distinct
  values to help users narrow their search.
- **Conversation history** — Full multi-turn context via in-memory checkpointing.

## Prerequisites

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager

Install uv if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

```bash
# 1. Clone the repository
git clone <repo-url> agent-client
cd agent-client

# 2. Create and activate a virtual environment
uv venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate          # Windows

# 3. Install dependencies
uv sync
```

`uv sync` reads `pyproject.toml`, installs all dependencies into the virtual
environment, and downloads the pinned Python version (from `.python-version`)
automatically if needed.

## Configuration

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description | Default |
|---|---|---|
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key | *(required)* |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint URL | *(required)* |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (e.g. `gpt-4o`) | *(required)* |
| `AZURE_OPENAI_API_VERSION` | API version | `2024-12-01-preview` |
| `AGENT_LOG_LEVEL` | Logging level | `INFO` |
| `MCP_SERVER_NAME` | Server name (must match the MCP server) | `access-governance-docs` |
| `MCP_TRANSPORT` | `sse` or `stdio` | `sse` |
| `MCP_SERVER_URL` | Server URL (SSE only) | `http://127.0.0.1:8000/sse` |
| `MCP_SERVER_COMMAND` | Command to pipe (stdio only) | — |
| `MCP_SERVER_ARGS` | Command args (stdio only) | — |

Set `AGENT_LOG_LEVEL=WARNING` to suppress verbose INFO logs on the client.

## Usage

**1. Start the MCP server** (in a separate terminal):

```bash
cd ../mcp-server
MCP_TRANSPORT=sse uv run mcp-docs-server
```

**2. Start the agent client**:

```bash
uv run agent-client
```

Type your questions at the prompt. Type `exit` to quit.

```
Access Governance Assistant
========================================
Type your question below. Type "exit" to quit.

🧑 You: How do I order a new entitlement?

🤖 Assistant: To order a new entitlement, navigate to the Access Governance portal...
(Source: entitlements/ordering_faq.pdf, Page 1)

🧑 You: What access rights do people in Finance / London have?

🤖 Assistant: Based on the entitlements data, 12 people in Finance / London have
the following access rights:
- SAP Financial Reporting (12/12)
- Budget Approval Portal (9/12)
- Expense Management System (8/12)
...

🧑 You: Tell me more about SAP Financial Reporting

🤖 Assistant: SAP Financial Reporting provides read-only access to...

🧑 You: exit
Goodbye!
```

## Project Structure

```
agent-client/
├── pyproject.toml
├── .env.example                  # sample configuration
├── .env                          # your credentials (git-ignored)
├── src/
│   └── ease_clients/
│       ├── __init__.py
│       ├── cli.py                # CLI entry point & logging setup
│       ├── cli_api.py            # API server entry point
│       ├── agent_api.py          # AgentService + FastAPI app
│       ├── auth_api.py           # API bearer-token auth
│       ├── scanner_cli.py        # batch scanner entry point
│       └── utils/
│           ├── llm.py                 # get_llm() → AzureChatOpenAI
│           ├── agnes_agent_graph.py   # LangGraph agent graph + MCP client
│           ├── scanner.py             # batch scan engine
│           └── incident_sources.py    # incident data sources
└── README.md
```

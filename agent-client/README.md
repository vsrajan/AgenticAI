# Agent Client — Access Governance Assistant

LangGraph ReAct agent that answers user questions about the Access Governance
application by consulting PDF documentation served via an
[MCP docs server](../mcp-server/).

## Architecture

```
                          agent-client                    mcp-server
                     ┌─────────────────────┐        ┌──────────────────┐
User  ──▶  CLI  ──▶  │  LangGraph ReAct    │──SSE──▶│  MCP docs server │
                     │  agent              │  or    │  (PDF index)     │
                     │      │              │ stdio  │                  │
                     │  AzureOpenAI LLM    │        │  host:port       │
                     └─────────────────────┘        └──────────────────┘
```

The agent connects to an **already-running** MCP server over **SSE** (default)
or **stdio**, loads three tools (`list_topics`, `search_docs`, `read_page`),
and uses them in a ReAct loop to answer questions with cited sources.

The client is completely decoupled from the server — it does **not** start or
manage the server. The server may be running on a different host.

## Setup

```bash
cd agent-client
uv venv && source .venv/bin/activate
uv pip install -e .
```

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
| `MCP_TRANSPORT` | `sse` or `stdio` | `sse` |
| `MCP_SERVER_URL` | Server URL (SSE only) | `http://127.0.0.1:8000/sse` |
| `MCP_SERVER_COMMAND` | Command to pipe (stdio only) | — |
| `MCP_SERVER_ARGS` | Command args (stdio only) | — |

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

You: How do I order a new entitlement?
Assistant: To order a new entitlement, navigate to the Access Governance portal...
(Source: entitlements/ordering_faq.pdf, Page 1)

You: exit
Goodbye!
```

## Project Structure

```
agent-client/
├── pyproject.toml
├── .env.example                  # sample configuration
├── .env                          # your credentials (git-ignored)
├── src/
│   └── agent_client/
│       ├── __init__.py
│       ├── cli.py                # CLI entry point & logging setup
│       ├── llm.py                # get_llm() → AzureChatOpenAI
│       └── agent.py              # LangGraph ReAct agent + MCP client
└── README.md
```

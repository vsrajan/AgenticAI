# Agent Client — Access Governance Assistant

LangGraph ReAct agent that answers user questions about the Access Governance
application by consulting PDF documentation served via the
[MCP docs server](../mcp-server/).

## Architecture

```
User  ──▶  CLI (agent-client)  ──▶  LangGraph ReAct agent
                                         │
                                    AzureOpenAI LLM
                                         │
                                    MCP tools (stdio)
                                         │
                                  mcp-docs-server
                                    (PDF index)
```

The agent connects to `mcp-docs-server` over **stdio**, loads three tools
(`list_topics`, `search_docs`, `read_page`), and uses them in a ReAct loop
to answer questions with cited sources.

## Setup

```bash
cd agent-client
uv venv && source .venv/bin/activate
uv pip install -e .
```

## Configuration

Create a `.env` file in the `agent-client/` directory:

```env
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-12-01-preview

# Optional
AGENT_LOG_LEVEL=INFO          # DEBUG, INFO, WARNING, ERROR
MCP_SERVER_COMMAND=uv         # override MCP server command
MCP_SERVER_ARGS=run --directory ../mcp-server mcp-docs-server
```

## Usage

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
├── .env                          # your Azure credentials (git-ignored)
├── src/
│   └── agent_client/
│       ├── __init__.py
│       ├── cli.py                # CLI entry point & logging setup
│       ├── llm.py                # get_llm() → AzureChatOpenAI
│       └── agent.py              # LangGraph ReAct agent + MCP client
└── README.md
```

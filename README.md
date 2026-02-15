# AgenticAI

A monorepo for an AI-powered documentation assistant. An LLM agent answers user questions by searching and reading enterprise PDF documentation exposed through an MCP server.

## Architecture

```
┌─────────────────┐       stdio / SSE        ┌──────────────────────┐
│      agent/      │ ◄──────────────────────► │    mcp-server/       │
│  (coming soon)   │     (MCP protocol)       │                      │
│                  │                          │  ┌────────────────┐  │
│  - Receives user │                          │  │   server.py    │  │
│    questions      │                          │  │  (FastMCP app) │  │
│  - Calls MCP     │                          │  └───────┬────────┘  │
│    tools          │                          │          │           │
│  - Synthesizes   │                          │  ┌───────▼────────┐  │
│    answers        │                          │  │  indexer.py    │  │
│                  │                          │  │  (DocIndex)    │  │
│                  │                          │  │                │  │
│                  │                          │  │  - PDF parsing │  │
│                  │                          │  │  - BM25 index  │  │
│                  │                          │  │  - Retrieval   │  │
│                  │                          │  └───────┬────────┘  │
│                  │                          │          │           │
│                  │                          │  ┌───────▼────────┐  │
│                  │                          │  │    docs/       │  │
│                  │                          │  │  (PDF files)   │  │
│                  │                          │  └────────────────┘  │
└─────────────────┘                          └──────────────────────┘
```

### How it works

1. **Startup** — The MCP server scans `mcp-server/docs/` for PDF files, extracts text using PyMuPDF, and builds an in-memory BM25 search index.
2. **Agent queries** — The agent connects over MCP (stdio transport) and calls tools to browse, search, and read documentation.
3. **Response flow** — The agent calls `list_topics` or `search_docs` to find relevant documents, `read_page` to retrieve full content, then synthesizes an answer for the user.

## Repository Layout

```
.
├── mcp-server/               # MCP documentation server
│   ├── server.py             # Server entry point — defines the three MCP tools
│   ├── indexer.py            # PDF extraction, BM25 indexing, and retrieval logic
│   ├── pyproject.toml        # Project metadata and dependencies
│   └── docs/                 # PDF files, organized by topic subdirectories
│       ├── entitlements/
│       ├── delegations/
│       ├── jml/
│       └── ...
├── agent/                    # LangGraph agent (coming soon)
└── README.md
```

## Projects

### mcp-server

An MCP server that makes PDF documentation searchable and retrievable for AI agents. Built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk).

**Tools exposed:**

| Tool | Description |
|---|---|
| `list_topics()` | Returns a tree of all available topics and their document paths. |
| `search_docs(query, max_results=5)` | Full-text BM25 search across all indexed documents. Returns ranked results with snippets. |
| `read_page(page_path)` | Retrieves the full extracted text of a specific PDF document. |

**Key modules:**

- **`server.py`** — FastMCP application that registers the three tools and delegates to `DocIndex`.
- **`indexer.py`** — `DocIndex` class that extracts text from PDFs (PyMuPDF), builds a BM25Okapi index, and provides topic browsing, search, and page retrieval. Includes a sliding-window snippet extractor for search results.
- **`docs/`** — Drop PDF files here. Subdirectory names become topic labels in `list_topics()` (root-level files go under `"general"`).

**Dependencies:**

| Package | Version | Purpose |
|---|---|---|
| [mcp\[cli\]](https://github.com/modelcontextprotocol/python-sdk) | >= 1.0.0 | MCP server framework and CLI tooling |
| [pymupdf](https://pymupdf.readthedocs.io/) | >= 1.25.0 | PDF text extraction |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | >= 0.2.2 | BM25Okapi full-text search ranking |

Python >= 3.10 is required.

### agent (planned)

A LangGraph-based chat agent that connects to the MCP server, receives user questions, and synthesizes answers from the documentation.

## Installation

### Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Running the MCP server

```bash
cd mcp-server
uv run server.py
```

uv will automatically create a virtual environment, install all dependencies, and start the server.

### Adding documentation

Place PDF files in `mcp-server/docs/`, optionally organized by topic:

```bash
mkdir -p mcp-server/docs/entitlements mcp-server/docs/delegations
cp ordering_faq.pdf mcp-server/docs/entitlements/
cp delegations_faq.pdf mcp-server/docs/delegations/
```

To use a docs directory at a different path, set the `MCP_DOCS_DIR` environment variable:

```bash
MCP_DOCS_DIR=/path/to/your/docs uv run server.py
```

### Connecting an agent to the MCP server

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient({
    "docs": {
        "command": "uv",
        "args": ["run", "server.py"],
        "cwd": "mcp-server",
        "transport": "stdio",
    }
}) as client:
    tools = client.get_tools()
    # Pass tools to your LangGraph agent
```

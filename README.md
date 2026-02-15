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
├── mcp-server/                          # MCP documentation server
│   ├── src/
│   │   └── mcp_docs_server/             # Python package
│   │       ├── __init__.py
│   │       ├── server.py                # MCP server — defines the three tools
│   │       └── indexer.py               # PDF extraction, BM25 indexing, retrieval
│   ├── docs/                            # PDF files, organized by topic subdirectories
│   │   ├── entitlements/
│   │   ├── delegations/
│   │   ├── jml/
│   │   └── ...
│   ├── pyproject.toml                   # Project metadata, dependencies, entry point
│   ├── .python-version                  # Python version pin for uv
│   ├── .env                             # Local environment variables (git-ignored)
│   └── .env.example                     # Template for .env
├── agent/                               # LangGraph agent (coming soon)
├── .gitignore
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

- **`server.py`** — FastMCP application that registers the three tools and delegates to `DocIndex`. Loads environment variables from `.env` at startup.
- **`indexer.py`** — `DocIndex` class that extracts text from PDFs (PyMuPDF), builds a BM25Okapi index, and provides topic browsing, search, and page retrieval. Includes a sliding-window snippet extractor for search results.
- **`docs/`** — Drop PDF files here. Subdirectory names become topic labels in `list_topics()` (root-level files go under `"general"`).

**Dependencies:**

| Package | Version | Purpose |
|---|---|---|
| [mcp\[cli\]](https://github.com/modelcontextprotocol/python-sdk) | >= 1.0.0 | MCP server framework and CLI tooling |
| [pymupdf](https://pymupdf.readthedocs.io/) | >= 1.25.0 | PDF text extraction |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | >= 0.2.2 | BM25Okapi full-text search ranking |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | >= 1.0.0 | Load `.env` files into environment |

Python >= 3.10 is required.

### agent (planned)

A LangGraph-based chat agent that connects to the MCP server, receives user questions, and synthesizes answers from the documentation.

## Getting Started

### Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python package and project manager):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Setting up the mcp-server project

1. **Clone the repository and navigate to the project:**

   ```bash
   git clone <repository-url>
   cd AgenticAI/mcp-server
   ```

2. **Create the virtual environment and install dependencies:**

   ```bash
   uv sync
   ```

   This reads `pyproject.toml`, creates a `.venv/` virtual environment in the project directory, and installs all dependencies into it. The Python version is pinned by `.python-version`; uv will download it automatically if needed.

3. **Configure environment variables:**

   Copy the example `.env` file and edit it:

   ```bash
   cp .env.example .env
   ```

   Open `.env` and set any overrides you need (e.g. `MCP_DOCS_DIR`). The defaults work out of the box — the server will read PDFs from the `docs/` directory.

4. **Add PDF documentation:**

   Place your PDF files in the `docs/` directory, optionally organized by topic:

   ```bash
   mkdir -p docs/entitlements docs/delegations docs/jml
   cp /path/to/ordering_faq.pdf docs/entitlements/
   cp /path/to/delegations_faq.pdf docs/delegations/
   ```

5. **Run the server:**

   ```bash
   uv run mcp-docs-server
   ```

   This uses the `[project.scripts]` entry point defined in `pyproject.toml`. Alternatively you can run the module directly:

   ```bash
   uv run python -m mcp_docs_server.server
   ```

### Connecting an agent to the MCP server

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient({
    "docs": {
        "command": "uv",
        "args": ["run", "mcp-docs-server"],
        "cwd": "mcp-server",
        "transport": "stdio",
    }
}) as client:
    tools = client.get_tools()
    # Pass tools to your LangGraph agent
```

### Useful uv commands

| Command | Description |
|---|---|
| `uv sync` | Create/update `.venv` and install all dependencies |
| `uv add <package>` | Add a new dependency to `pyproject.toml` and install it |
| `uv remove <package>` | Remove a dependency |
| `uv run <command>` | Run a command inside the project's virtual environment |
| `uv lock` | Regenerate the lockfile without installing |
| `uv python list` | List available Python versions |

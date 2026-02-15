# MCP Docs Server — Access Governance Documentation

An MCP server that makes PDF documentation searchable and retrievable for AI
agents. Built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk).

## Architecture

```
┌──────────────────────────────────────┐
│            mcp-server                │
│                                      │
│  ┌────────────────┐                  │
│  │   server.py    │  MCP tools       │
│  │  (FastMCP app) │  exposed over    │
│  └───────┬────────┘  stdio / SSE     │
│          │                           │
│  ┌───────▼────────┐                  │
│  │  indexer.py    │                  │
│  │  (DocIndex)   │                  │
│  │               │                  │
│  │  - PDF parsing│                  │
│  │  - BM25 index │                  │
│  │  - Retrieval  │                  │
│  └───────┬────────┘                  │
│          │                           │
│  ┌───────▼────────┐                  │
│  │    docs/       │                  │
│  │  (PDF files)   │                  │
│  └────────────────┘                  │
└──────────────────────────────────────┘
```

### How it works

1. **Startup** — The server scans `docs/` for PDF files, extracts text using
   PyMuPDF, and builds an in-memory BM25 search index.
2. **Agent queries** — An agent connects over MCP (stdio or SSE transport) and
   calls tools to browse, search, and read documentation.
3. **Response flow** — The agent calls `list_topics` or `search_docs` to find
   relevant documents, `read_page` to retrieve full content, then synthesizes
   an answer for the user.

## Tools

| Tool | Description |
|---|---|
| `list_topics()` | Returns a tree of all available topics and their document paths. |
| `search_docs(query, max_results=5)` | Full-text BM25 search across all indexed documents. Returns ranked results with snippets. |
| `read_page(page_path)` | Retrieves the full extracted text of a specific PDF document. |

## Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Setup

```bash
cd mcp-server
uv sync
```

This reads `pyproject.toml`, creates a `.venv/` virtual environment, and
installs all dependencies. The Python version is pinned by `.python-version`;
uv will download it automatically if needed.

## Configuration

Copy the example and edit:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `MCP_SERVER_NAME` | Server name (identifier for clients) | `access-governance-docs` |
| `MCP_DOCS_DIR` | Path to PDF files directory | `docs/` (relative to project) |
| `MCP_TRANSPORT` | `stdio`, `sse`, or `streamable-http` | `stdio` |
| `MCP_HOST` | Bind address (SSE/HTTP only) | `127.0.0.1` |
| `MCP_PORT` | Bind port (SSE/HTTP only) | `8000` |
| `MCP_LOG_LEVEL` | Logging level | `INFO` |

## Adding Documentation

Place PDF files in the `docs/` directory, optionally organized by topic:

```bash
mkdir -p docs/entitlements docs/delegations docs/jml
cp /path/to/ordering_faq.pdf docs/entitlements/
cp /path/to/delegations_faq.pdf docs/delegations/
```

Subdirectory names become topic labels in `list_topics()`. Root-level files go
under `"general"`.

## Running

```bash
# stdio transport (default — for local agent connections)
uv run mcp-docs-server

# SSE transport (for remote agent connections)
MCP_TRANSPORT=sse uv run mcp-docs-server
```

Alternatively, run the module directly:

```bash
uv run python -m mcp_docs_server.server
```

## Testing

A `tester.py` script creates sample PDFs in a temp directory and validates
indexing, search ranking, page reading, and corrupt PDF handling:

```bash
uv run python tester.py              # automated tests (15 checks)
uv run python tester.py --interactive # interactive search loop
```

## Key Modules

- **`server.py`** — FastMCP application that registers the three tools and
  delegates to `DocIndex`. Loads environment variables from `.env` at startup.
- **`indexer.py`** — `DocIndex` class that extracts text from PDFs (PyMuPDF),
  builds a BM25Okapi index, and provides topic browsing, search, and page
  retrieval. Includes a sliding-window snippet extractor for search results.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| [mcp\[cli\]](https://github.com/modelcontextprotocol/python-sdk) | >= 1.0.0 | MCP server framework and CLI tooling |
| [pymupdf](https://pymupdf.readthedocs.io/) | >= 1.25.0 | PDF text extraction |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | >= 0.2.2 | BM25Okapi full-text search ranking |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | >= 1.0.0 | Load `.env` files into environment |

Python >= 3.10 is required.

## Project Structure

```
mcp-server/
├── pyproject.toml
├── .python-version
├── .env.example                     # template for .env
├── .env                             # local env vars (git-ignored)
├── tester.py                        # automated test suite
├── docs/                            # PDF files, organized by topic
│   ├── entitlements/
│   ├── delegations/
│   ├── jml/
│   └── ...
└── src/
    └── mcp_docs_server/
        ├── __init__.py
        ├── server.py                # MCP server — defines the three tools
        └── indexer.py               # PDF extraction, BM25 indexing, retrieval
```

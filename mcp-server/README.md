# MCP Server — Access Governance Documentation & Data

An MCP server that makes PDF documentation and CSV data searchable and
retrievable for AI agents. Built with
[FastMCP](https://github.com/modelcontextprotocol/python-sdk).

## Architecture

```
┌──────────────────────────────────────┐
│            mcp-server                │
│                                      │
│  ┌────────────────┐                  │
│  │   server.py    │  MCP tools       │
│  │  (FastMCP app) │  exposed over    │
│  └──┬─────────┬───┘  stdio / SSE    │
│     │         │                      │
│  ┌──▼──────┐ ┌▼───────────┐         │
│  │pdf_     │ │csv_store.py│         │
│  │indexer  │ │(CsvStore)  │         │
│  │.py      │ │            │         │
│  │- PDF    │ │- CSV parse │         │
│  │  parse  │ │- BM25 index│         │
│  │- BM25   │ │- Column    │         │
│  │  index  │ │  filter    │         │
│  └──┬──────┘ └┬───────────┘         │
│     │         │                      │
│  ┌──▼─────────▼──┐                  │
│  │    docs/       │                  │
│  │ (PDFs & CSVs)  │                  │
│  └────────────────┘                  │
└──────────────────────────────────────┘
```

### How it works

1. **Startup** — The server scans `docs/` for PDF files and CSV files. PDFs are
   text-extracted using PyMuPDF and indexed with BM25. CSVs are loaded with
   dynamic column discovery and each gets its own BM25 index.
2. **Agent queries** — An agent connects over MCP (stdio or SSE transport) and
   calls tools to browse, search, and read documentation or query structured data.
3. **Response flow** — The agent uses PDF tools to find and read documentation,
   and CSV tools to search, filter, and explore structured datasets like access
   rights metadata and user entitlements.

## Tools

### PDF documentation tools

| Tool | Description |
|---|---|
| `list_topics()` | Returns a tree of all available topics and their document paths. |
| `search_docs(query, max_results=5)` | Full-text BM25 search across all indexed documents. Returns ranked results with snippets. |
| `read_page(page_path)` | Retrieves the full extracted text of a specific PDF document. |

### CSV data tools

| Tool | Description |
|---|---|
| `list_datasets()` | Lists all loaded CSV datasets with their column names and row counts. |
| `search_dataset(dataset, query, max_results=10)` | Full-text BM25 search across all columns of a named dataset. Returns matching rows ranked by relevance. |
| `filter_dataset(dataset, filters)` | Filters rows by exact column values (case-insensitive). Accepts a dict of column-value pairs. |
| `get_column_values(dataset, column)` | Lists all distinct values in a column. Useful for discovering available OUs, locations, categories, etc. |

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
git clone <repo-url> mcp-server
cd mcp-server

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

Copy the example and edit:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `MCP_SERVER_NAME` | Server name (identifier for clients) | `access-governance-docs` |
| `MCP_DOCS_DIR` | Path to docs directory (PDFs and CSVs) | `docs/` (relative to project) |
| `MCP_TRANSPORT` | `stdio`, `sse`, or `streamable-http` | `stdio` |
| `MCP_HOST` | Bind address (SSE/HTTP only) | `127.0.0.1` |
| `MCP_PORT` | Bind port (SSE/HTTP only) | `8000` |
| `MCP_LOG_LEVEL` | Logging level | `INFO` |
| `MCP_AUTH` | `static` (bearer tokens, fail-closed) or `none` (open; local dev only) | `static` |
| `MCP_AUTH_TOKENS` | Comma-separated `name:token` pairs, one per agent deployment | unset (required in static mode) |

## Authentication

Agents connecting over HTTP transports (`sse` / `streamable-http`) must
present a bearer token (`Authorization: Bearer <token>`). Tokens are
configured as one `name:token` pair per agent deployment:

```
MCP_AUTH_TOKENS=agnes:tok_abc123,hr-bot:tok_xyz789
```

The name never travels over the wire -- clients send only the token,
and the server derives the agent's identity by lookup (possession of
the secret is the proof). The name appears in tool-call audit logs
(`search_docs caller=agnes query=...`) and makes tokens revocable per
agent: remove one pair without rotating the others.

Fail-closed: with `MCP_AUTH=static` (the default) and no
`MCP_AUTH_TOKENS`, the server refuses to start on an HTTP transport.
Running open requires an explicit `MCP_AUTH=none`. The `stdio`
transport has no HTTP layer, so auth does not apply there (the server
runs as a child process of a caller who already has local access).

On the agent side, set the deployment's token once in
`agent-client/.env` as `MCP_SERVER_TOKEN` -- the CLI, scanner, and API
all send it automatically.

Tokens travel in cleartext over plain HTTP; for anything beyond a
trusted network, terminate TLS in front of the server. The verifier
plugs into the MCP SDK's `TokenVerifier` hook, which is the same slot
a future OAuth2 / Entra JWT validator uses.

Tests: `uv run --with pytest pytest tests/ -q`

## Adding Data

### PDF documentation

Place PDF files in the `docs/` directory, optionally organized by topic:

```bash
mkdir -p docs/entitlements docs/delegations docs/jml
cp /path/to/ordering_faq.pdf docs/entitlements/
cp /path/to/delegations_faq.pdf docs/delegations/
```

Subdirectory names become topic labels in `list_topics()`. Root-level files go
under `"general"`.

### CSV data files

Place CSV files in the `docs/` directory (any level — they are found
recursively). Each CSV becomes a named dataset using its filename (without
extension) as the name.

```bash
cp /path/to/access_rights.csv docs/
cp /path/to/entitlements.csv docs/
```

Column names are discovered automatically from the CSV header row. No schema
configuration is needed — just drop the file in and restart the server.

**Example:** `access_rights.csv` becomes the `"access_rights"` dataset.
`entitlements.csv` becomes the `"entitlements"` dataset.

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

- **`server.py`** — FastMCP application that registers all tools (PDF and CSV)
  and delegates to `DocIndex` and `CsvStore`. Loads environment variables from
  `.env` at startup.
- **`pdf_indexer.py`** — `DocIndex` class that extracts text from PDFs (PyMuPDF),
  builds a BM25Okapi index, and provides topic browsing, search, and page
  retrieval. Includes a sliding-window snippet extractor for search results.
- **`csv_store.py`** — `CsvStore` class that reads CSV files, discovers columns
  dynamically, builds per-dataset BM25 indexes, and supports full-text search,
  column-based filtering, and distinct value listing.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| [mcp\[cli\]](https://github.com/modelcontextprotocol/python-sdk) | >= 1.0.0 | MCP server framework and CLI tooling |
| [pymupdf](https://pymupdf.readthedocs.io/) | >= 1.25.0 | PDF text extraction |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | >= 0.2.2 | BM25Okapi full-text search ranking |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | >= 1.0.0 | Load `.env` files into environment |

## Project Structure

```
mcp-server/
├── pyproject.toml
├── .python-version
├── .env.example                     # template for .env
├── .env                             # local env vars (git-ignored)
├── tester.py                        # automated test suite
├── docs/                            # PDF and CSV files
│   ├── access_rights.csv            # access rights metadata
│   ├── entitlements.csv             # user entitlements data
│   ├── entitlements/                # PDF docs by topic
│   ├── delegations/
│   ├── jml/
│   └── ...
└── src/
    └── mcp_docs_server/
        ├── __init__.py
        ├── server.py                # MCP server — registers all tools
        ├── pdf_indexer.py           # PDF extraction, BM25 indexing, retrieval
        └── csv_store.py             # CSV loading, BM25 search, column filtering
```

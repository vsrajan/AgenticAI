# MCP Docs Server

An MCP (Model Context Protocol) server that makes PDF documentation searchable and retrievable for AI agents. Built to serve Access Governance application FAQs and how-to guides to a LangGraph chat-assist agent.

## Architecture

```
┌─────────────────┐       stdio / SSE        ┌──────────────────────┐
│  LangGraph Agent │ ◄──────────────────────► │   MCP Docs Server    │
│                  │     (MCP protocol)       │                      │
│  - Receives user │                          │  ┌────────────────┐  │
│    questions      │                          │  │   server.py    │  │
│  - Calls MCP     │                          │  │  (FastMCP app) │  │
│    tools          │                          │  └───────┬────────┘  │
│  - Synthesizes   │                          │          │           │
│    answers        │                          │  ┌───────▼────────┐  │
│                  │                          │  │  indexer.py    │  │
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

1. **Startup** — The server scans the `docs/` directory for PDF files, extracts text from each using PyMuPDF, and builds an in-memory BM25 search index.
2. **Agent queries** — The LangGraph agent connects over MCP (stdio transport) and calls tools to browse, search, and read documentation.
3. **Response flow** — The agent typically calls `list_topics` or `search_docs` first to find the right document, then `read_page` to get the full content, and finally synthesizes an answer for the user.

## Project Layout

```
.
├── server.py          # MCP server entry point — defines and exposes the three tools
├── indexer.py         # PDF text extraction, BM25 indexing, and search/retrieval logic
├── pyproject.toml     # Project metadata and dependencies
├── docs/              # PDF documentation files (organized by topic subdirectories)
│   ├── entitlements/
│   │   └── ordering_faq.pdf
│   ├── delegations/
│   │   └── delegations_faq.pdf
│   ├── jml/
│   │   └── jml_process.pdf
│   └── ...
└── README.md
```

## Components

### server.py

The MCP server application built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk). Exposes three tools:

| Tool | Description |
|---|---|
| `list_topics()` | Returns a tree of all available topics and their document paths. Use this to browse what documentation exists. |
| `search_docs(query, max_results=5)` | Full-text BM25 search across all indexed documents. Returns ranked results with relevance scores and text snippets. |
| `read_page(page_path)` | Retrieves the full extracted text of a specific PDF document. Supports fuzzy path matching (partial filenames, omitted extensions). |

### indexer.py

The document indexing and retrieval engine. Contains:

- **`DocIndex`** — Main class that manages the document corpus. On initialization it:
  - Recursively scans the `docs/` directory for `.pdf` files
  - Extracts text from each PDF using PyMuPDF
  - Organizes documents into topics based on subdirectory structure (files at the root go under `"general"`)
  - Builds a BM25Okapi index over the tokenized document corpus

- **`_extract_text(pdf_path)`** — Extracts and concatenates text from all pages of a PDF using PyMuPDF.

- **`_tokenize(text)`** — Simple lowercase word tokenizer used for both indexing and query processing.

- **`_make_snippet(text, query_tokens)`** — Sliding-window snippet extractor that finds the region of a document with the highest density of query term matches.

### docs/

The documentation directory. Place your PDF files here, optionally organized into subdirectories by topic. The subdirectory names become topic labels in `list_topics()`.

Examples:
- `docs/entitlements/ordering_faq.pdf` — grouped under topic `"entitlements"`
- `docs/jml/leaver_process.pdf` — grouped under topic `"jml"`
- `docs/general_overview.pdf` — grouped under topic `"general"` (root-level files)

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| [mcp\[cli\]](https://github.com/modelcontextprotocol/python-sdk) | >= 1.0.0 | MCP server framework (FastMCP) and CLI tooling |
| [pymupdf](https://pymupdf.readthedocs.io/) | >= 1.25.0 | PDF text extraction — fast, no system-level dependencies |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | >= 0.2.2 | BM25Okapi implementation for full-text search ranking |

Python >= 3.10 is required.

## Installation

### Prerequisites

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Setup

1. Clone the repository:

   ```bash
   git clone <repository-url>
   cd AgenticAI
   ```

2. Add your PDF files to the `docs/` directory:

   ```bash
   cp /path/to/your/pdfs/*.pdf docs/
   # or organize by topic:
   mkdir -p docs/entitlements docs/delegations docs/jml
   cp ordering_faq.pdf docs/entitlements/
   cp delegations_faq.pdf docs/delegations/
   cp jml_process.pdf docs/jml/
   ```

3. Run the server:

   ```bash
   uv run server.py
   ```

   uv will automatically create a virtual environment, install all dependencies, and start the server. No separate install step is needed.

### Custom docs directory

To use a docs directory at a different path, set the `MCP_DOCS_DIR` environment variable:

```bash
MCP_DOCS_DIR=/path/to/your/docs uv run server.py
```

## Connecting from LangGraph

Configure your LangGraph agent to connect to this server as an MCP tool provider:

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient({
    "docs": {
        "command": "uv",
        "args": ["run", "server.py"],
        "transport": "stdio",
    }
}) as client:
    tools = client.get_tools()
    # Pass tools to your LangGraph agent
```

The agent's system prompt should instruct it to search first, read relevant pages, then synthesize an answer for the user.

"""MCP server for Access Governance PDF documentation.

Exposes three tools to LangGraph agents:
  - list_topics: browse available documentation topics
  - search_docs: full-text BM25 search across all documents
  - read_page: retrieve the full text of a specific document

Run with:
    uv run mcp-docs-server
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_docs_server.indexer import DocIndex

# Load environment variables from .env file (project root = mcp-server/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Allow overriding the docs directory via environment variable
DOCS_DIR = os.environ.get(
    "MCP_DOCS_DIR",
    str(Path(__file__).resolve().parents[2] / "docs"),
)

mcp = FastMCP(
    "access-governance-docs",
    instructions=(
        "This server provides documentation for an enterprise Access Governance "
        "application. Use list_topics to see what's available, search_docs to find "
        "relevant pages, and read_page to get the full content of a specific page. "
        "Always search before reading to find the most relevant document."
    ),
)

index = DocIndex(DOCS_DIR)


@mcp.tool()
def list_topics() -> dict:
    """List all available documentation topics and their pages.

    Call this first to understand what documentation is available.
    Returns a tree of topics mapped to document paths.
    """
    return index.get_topic_tree()


@mcp.tool()
def search_docs(query: str, max_results: int = 5) -> list[dict]:
    """Search the documentation for a query.

    Uses full-text search to find the most relevant documents.
    Returns matching snippets with page paths that can be passed to read_page.

    Args:
        query: The search query (e.g., "how to order entitlements",
               "delegation setup", "leaver process").
        max_results: Maximum number of results to return (default 5).
    """
    return index.search(query, max_results)


@mcp.tool()
def read_page(page_path: str) -> dict:
    """Read the full text content of a documentation page.

    Use a page_path from list_topics or search_docs results.

    Args:
        page_path: Path to the document (e.g., "entitlements/ordering_faq.pdf").
    """
    return index.read(page_path)


def main():
    """Entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

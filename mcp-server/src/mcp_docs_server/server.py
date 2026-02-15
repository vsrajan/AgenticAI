"""MCP server for Access Governance PDF documentation.

Exposes three tools to LangGraph agents:
  - list_topics: browse available documentation topics
  - search_docs: full-text BM25 search across all documents
  - read_page: retrieve the full text of a specific document

Run with:
    uv run mcp-docs-server
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_docs_server.indexer import DocIndex

# Load environment variables from .env file (project root = mcp-server/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ---------------------------------------------------------------------------
# Logging — stdout handler with configurable level
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("MCP_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mcp_docs_server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Allow overriding the docs directory via environment variable
DOCS_DIR = os.environ.get(
    "MCP_DOCS_DIR",
    str(Path(__file__).resolve().parents[2] / "docs"),
)

# Transport configuration
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

logger.info("Docs directory: %s", DOCS_DIR)
logger.info("Transport: %s (host=%s, port=%d)", MCP_TRANSPORT, MCP_HOST, MCP_PORT)

mcp = FastMCP(
    "access-governance-docs",
    host=MCP_HOST,
    port=MCP_PORT,
    log_level=LOG_LEVEL,
    instructions=(
        "This server provides documentation for an enterprise Access Governance "
        "application. Use list_topics to see what's available, search_docs to find "
        "relevant pages, and read_page to get the full content of a specific page. "
        "Always search before reading to find the most relevant document. "
        "Document text includes [Page N] markers — always cite the source document "
        "and page number when answering (e.g. 'Source: ordering_faq.pdf, Page 3')."
    ),
)

index = DocIndex(DOCS_DIR)
logger.info("Index ready: %d documents", len(index._documents))


@mcp.tool()
def list_topics() -> dict:
    """List all available documentation topics and their pages.

    Call this first to understand what documentation is available.
    Returns a tree of topics mapped to document paths.
    """
    logger.debug("list_topics called")
    return index.get_topic_tree()


@mcp.tool()
def search_docs(query: str, max_results: int = 5) -> list[dict]:
    """Search the documentation for a query.

    Uses full-text search to find the most relevant documents.
    Returns matching snippets with page paths that can be passed to read_page.
    Each result includes the source PDF page_path and total_pages count.
    Snippets contain [Page N] markers indicating the PDF page of the content.

    Args:
        query: The search query (e.g., "how to order entitlements",
               "delegation setup", "leaver process").
        max_results: Maximum number of results to return (default 5).
    """
    logger.info("search_docs query=%r max_results=%d", query, max_results)
    results = index.search(query, max_results)
    logger.info("search_docs returned %d results", len(results))
    return results


@mcp.tool()
def read_page(page_path: str) -> dict:
    """Read the full text content of a documentation page.

    Use a page_path from list_topics or search_docs results.
    Content includes [Page N] markers for each PDF page so you can cite
    the exact page number (e.g. "Source: ordering_faq.pdf, Page 3").

    Args:
        page_path: Path to the document (e.g., "entitlements/ordering_faq.pdf").
    """
    logger.info("read_page page_path=%r", page_path)
    result = index.read(page_path)
    if "error" in result:
        logger.warning("read_page not found: %s", page_path)
    return result


def main():
    """Entry point for the MCP server."""
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()

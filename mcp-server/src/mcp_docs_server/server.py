"""MCP server for Access Governance documentation and data.

Exposes tools for PDF documentation and CSV data to LangGraph agents:

PDF tools:
  - list_topics: browse available documentation topics
  - search_docs: full-text BM25 search across all documents
  - read_page: retrieve the full text of a specific document

CSV tools:
  - list_datasets: list all loaded CSV datasets and their columns
  - search_dataset: full-text search across a CSV dataset
  - filter_dataset: filter rows by column values
  - get_column_values: list distinct values for a column

Run with:
    uv run mcp-docs-server
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from mcp_docs_server.pdf_indexer import DocIndex
from mcp_docs_server.csv_store import CsvStore

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
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "access-governance-docs")

logger.info("Docs directory: %s", DOCS_DIR)
logger.info("Server name: %s", MCP_SERVER_NAME)
logger.info("Transport: %s (host=%s, port=%d)", MCP_TRANSPORT, MCP_HOST, MCP_PORT)

mcp = FastMCP(
    MCP_SERVER_NAME,
    host=MCP_HOST,
    port=MCP_PORT,
    log_level=LOG_LEVEL,
    instructions=(
        "This server provides documentation and data for an enterprise Access "
        "Governance application.\n\n"
        "PDF documentation tools:\n"
        "  Use list_topics to see what's available, search_docs to find relevant "
        "pages, and read_page to get the full content of a specific page. Always "
        "search before reading. Document text includes [Page N] markers — cite "
        "the source document and page number.\n\n"
        "CSV data tools:\n"
        "  Use list_datasets to discover available datasets and their columns. "
        "Use search_dataset for free-text search, filter_dataset for exact "
        "column matching, and get_column_values to discover what values exist "
        "in a column (useful for filtering)."
    ),
)

index = DocIndex(DOCS_DIR)
logger.info("PDF index ready: %d documents", len(index._documents))

csv_store = CsvStore(DOCS_DIR)
logger.info("CSV store ready: %d datasets", len(csv_store._datasets))


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


# ---------------------------------------------------------------------------
# CSV data tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_datasets() -> dict:
    """List all available CSV datasets, their columns, and row counts.

    Call this to discover what structured data is available.
    Use the dataset name and column names with other CSV tools.
    """
    logger.debug("list_datasets called")
    return csv_store.list_datasets()


@mcp.tool()
def search_dataset(dataset: str, query: str, max_results: int = 10) -> list[dict]:
    """Full-text search across all columns of a CSV dataset.

    Returns matching rows ranked by relevance. Each row includes a _score field.

    Args:
        dataset: Name of the dataset (from list_datasets, e.g. "access_rights").
        query: Free-text search query (e.g. "finance reporting read-only").
        max_results: Maximum number of rows to return (default 10).
    """
    logger.info("search_dataset dataset=%r query=%r max_results=%d", dataset, query, max_results)
    results = csv_store.search(dataset, query, max_results)
    logger.info("search_dataset returned %d results", len(results))
    return results


@mcp.tool()
def filter_dataset(dataset: str, filters: dict[str, str]) -> list[dict]:
    """Filter rows in a CSV dataset by exact column values (case-insensitive).

    Use get_column_values first to discover valid filter values.

    Args:
        dataset: Name of the dataset (from list_datasets).
        filters: Column-value pairs to match, e.g. {"ou": "Finance", "location": "London"}.
    """
    logger.info("filter_dataset dataset=%r filters=%r", dataset, filters)
    results = csv_store.filter_rows(dataset, **filters)
    logger.info("filter_dataset returned %d rows", len(results))
    return results


@mcp.tool()
def get_column_values(dataset: str, column: str) -> list[str] | dict:
    """List all distinct values in a column of a CSV dataset.

    Useful for discovering what values exist before filtering
    (e.g. what OUs, locations, or access right names are available).

    Args:
        dataset: Name of the dataset (from list_datasets).
        column: Column name to get values for (from list_datasets).
    """
    logger.info("get_column_values dataset=%r column=%r", dataset, column)
    return csv_store.get_distinct_values(dataset, column)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for the MCP server."""
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()

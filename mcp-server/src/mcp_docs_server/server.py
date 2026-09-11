"""MCP server for Access Governance documentation and data.

Exposes tools for PDF documentation and CSV data to LangGraph agents:

Knowledgebase tools:
  - list_topics: browse available documentation topics
  - search_docs: full-text BM25 search across all documents
  - read_page: retrieve the full text of a specific document

Resource tools:
  - list_datasets: list all loaded CSV datasets and their columns
  - search_dataset: full-text search across a CSV dataset
  - filter_dataset: filter rows by exact column values
  - filter_dataset_fuzzy: filter rows by column regex patterns
  - get_column_values: list distinct values for a column

Request tools:
  - get_request_attributes: get the schema of attributes needed to raise a request
  - raise_entitlement_request: submit an entitlement request (placeholder)

Run with:
    uv run mcp-docs-server
"""

import functools
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from mcp_docs_server.auth import build_token_verifier
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

# Transport configuration. streamable-http is the recommended HTTP
# transport (one /mcp endpoint, works behind load balancers); sse is
# the legacy HTTP transport, kept for rollback; stdio is the local
# child-process default. Hyphen and underscore spellings both accepted.
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower().replace("_", "-")
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "access-governance-docs")

# Stateless streamable-http: every request is self-contained, so any
# server replica can answer it -- required for running more than one
# copy behind a load balancer (e.g. Kubernetes). Costs one extra
# initialize round-trip per tool call; ignored by sse and stdio.
MCP_STATELESS_HTTP = os.environ.get("MCP_STATELESS_HTTP", "true").lower() in ("1", "true", "yes")

logger.info("Docs directory: %s", DOCS_DIR)
logger.info("Server name: %s", MCP_SERVER_NAME)
logger.info(
    "Transport: %s (host=%s, port=%d, stateless=%s)",
    MCP_TRANSPORT, MCP_HOST, MCP_PORT,
    MCP_STATELESS_HTTP if MCP_TRANSPORT == "streamable-http" else "n/a",
)

# -- Authentication --
# HTTP transports require a bearer token per agent (see auth.py).
# stdio has no HTTP layer: the server is a child process of a caller
# who already has local access, so auth does not apply there.
_token_verifier = None
if MCP_TRANSPORT in ("sse", "streamable-http"):
    _token_verifier = build_token_verifier()  # fails closed without tokens
elif os.environ.get("MCP_AUTH") or os.environ.get("MCP_AUTH_TOKENS"):
    logger.info("MCP_AUTH is configured but transport=stdio has no HTTP layer -- auth not applied")

# AuthSettings is OAuth resource-server metadata the SDK advertises to
# clients; for static tokens the URLs are informational only
_SERVER_URL = f"http://{MCP_HOST}:{MCP_PORT}"
_auth_settings = (
    AuthSettings(issuer_url=_SERVER_URL, resource_server_url=_SERVER_URL)
    if _token_verifier else None
)


def _caller() -> str:
    """Name of the authenticated agent, for audit log lines.

    Reads the AccessToken the SDK stored for the current request;
    anonymous when auth is disabled or on stdio.
    """
    access_token = get_access_token()
    return access_token.client_id if access_token else "anonymous"


def _timed(fn):
    """Log every tool call's duration with the calling agent.

    Applied under @mcp.tool() on every tool. functools.wraps keeps the
    original name/docstring, and inspect.signature follows __wrapped__,
    so FastMCP still derives the tool schema from the real function.
    These log lines are the per-tool half of the P0 instrumentation --
    the agent side logs per-node and per-turn durations.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            logger.info(
                "%s caller=%s took=%.1fms",
                fn.__name__, _caller(), (time.perf_counter() - start) * 1000,
            )
    return wrapper


mcp = FastMCP(
    MCP_SERVER_NAME,
    host=MCP_HOST,
    port=MCP_PORT,
    log_level=LOG_LEVEL,
    token_verifier=_token_verifier,
    auth=_auth_settings,
    stateless_http=MCP_STATELESS_HTTP,
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
        "Use search_dataset for free-text search across all columns. "
        "For discovery queries (listing matching values, counting occurrences), "
        "ALWAYS prefer count_by_column — it returns compact {value, count} "
        "summaries instead of full rows. Set fuzzy=True for regex matching. "
        "Only use filter_dataset / filter_dataset_fuzzy when you need "
        "actual row data (e.g. to inspect individual records). "
        "Use get_column_values to discover what values exist in a column. "
        "Column names in filters, projections and grouping are "
        "CASE-SENSITIVE and are validated: an unknown name returns an "
        "error naming it plus available_columns, and no data. Take the "
        "correct name from available_columns and retry the same call -- "
        "do not drop the filter and do not report 'nothing found'."
    ),
)

index = DocIndex(DOCS_DIR)
logger.info("PDF index ready: %d documents", len(index._documents))

csv_store = CsvStore(DOCS_DIR)
logger.info("Data store ready: %d datasets", len(csv_store.dataset_names))


@mcp.tool()
@_timed
def list_topics() -> dict:
    """List all available documentation topics and their pages.

    Call this first to understand what documentation is available.
    Returns a tree of topics mapped to document paths.
    """
    logger.debug("list_topics called by %s", _caller())
    return index.get_topic_tree()


@mcp.tool()
@_timed
def search_docs(query: str, max_results: int = 5) -> list[dict]:
    """Search the documentation for a query.

    Full-text search at PAGE level: each result names the specific
    page that matched (page_path + page number + a snippet from that
    page). Pass page_path and the page number to read_page -- reading
    just the hit page (or a small range around it) is much cheaper
    than reading the whole document.

    Args:
        query: The search query (e.g., "how to order entitlements",
               "delegation setup", "leaver process").
        max_results: Maximum number of results to return (default 5).
    """
    logger.info("search_docs caller=%s query=%r max_results=%d", _caller(), query, max_results)
    results = index.search(query, max_results)
    logger.info("search_docs returned %d results", len(results))
    return results


@mcp.tool()
@_timed
def read_page(page_path: str, pages: str = "") -> dict:
    """Read a documentation document -- whole, or just selected pages.

    Use a page_path from list_topics or search_docs results. PREFER
    passing the page(s) a search hit pointed at (pages="3", or a small
    range like pages="2-4" for surrounding context) -- it returns a
    fraction of the text and answers arrive faster. Omit pages only
    when you genuinely need the entire document.

    Content includes [Page N] markers so you can cite the exact page
    (e.g. "Source: ordering_faq.pdf, Page 3").

    Args:
        page_path: Path to the document (e.g., "entitlements/ordering_faq.pdf").
        pages: Optional selection: "3" for one page, "2-5" for a range,
               empty for the full document.
    """
    logger.info("read_page caller=%s page_path=%r pages=%r", _caller(), page_path, pages)
    result = index.read(page_path, pages)
    if "error" in result:
        logger.warning("read_page not found: %s", page_path)
    return result


# ---------------------------------------------------------------------------
# CSV data tools
# ---------------------------------------------------------------------------


@mcp.tool()
@_timed
def list_datasets() -> dict:
    """List all available CSV datasets, their columns, and row counts.

    Call this to discover what structured data is available.
    Use the dataset name and column names with other CSV tools.
    """
    logger.debug("list_datasets called by %s", _caller())
    return csv_store.list_datasets()


@mcp.tool()
@_timed
def search_dataset(dataset: str, query: str, max_results: int = 10) -> list[dict]:
    """Full-text search across all columns of a CSV dataset.

    Returns matching rows ranked by relevance. Each row includes a _score field.

    Args:
        dataset: Name of the dataset (from list_datasets, e.g. "access_rights").
        query: Free-text search query (e.g. "finance reporting read-only").
        max_results: Maximum number of rows to return (default 10).
    """
    logger.info("search_dataset caller=%s dataset=%r query=%r max_results=%d", _caller(), dataset, query, max_results)
    results = csv_store.search(dataset, query, max_results)
    logger.info("search_dataset returned %d results", len(results))
    return results


@mcp.tool()
@_timed
def filter_dataset(dataset: str, filters: dict[str, str], max_results: int = 100,
                   columns: list[str] | None = None) -> list[dict]:
    """Filter rows in a CSV dataset by exact column values (case-insensitive).

    Returns up to max_results matching rows. When the total matches exceed
    max_results, the last element will be a metadata object with
    _truncated=True and the total count. In that case, prefer
    count_by_column for compact summary counts instead of fetching all rows.

    Use get_column_values first to discover valid filter values.

    PASS columns whenever you only need a few fields. Rows otherwise carry
    every column, and in a per-assignment dataset the person's attributes
    repeat identically on every row — asking for the 3 fields you will
    show is far smaller than the whole row.

    Args:
        dataset: Name of the dataset (from list_datasets).
        filters: Column-value pairs to match, e.g. {"ou": "Finance", "location": "London"}.
                 Column names are CASE-SENSITIVE and must exist in the
                 dataset: an unknown name is an error naming it and
                 listing the valid columns, and NO rows are returned.
                 Correct the name and retry -- never drop the filter.
        max_results: Maximum rows to return (default 100).
        columns: Optional list of columns to return, e.g.
                 ["ResourceID", "ResourceName"]. Omit for every column.
                 An unknown name is an error listing the valid columns.
    """
    logger.info("filter_dataset caller=%s dataset=%r filters=%r max_results=%d columns=%r",
                _caller(), dataset, filters, max_results, columns)
    results = csv_store.filter_rows(dataset, max_results=max_results,
                                    columns=columns, **filters)
    logger.info("filter_dataset returned %d rows", len(results))
    return results


@mcp.tool()
@_timed
def filter_dataset_fuzzy(dataset: str, filters: dict[str, str], max_results: int = 100,
                         columns: list[str] | None = None) -> list[dict]:
    """Filter rows in a CSV dataset using regex pattern matching (case-insensitive).

    Unlike filter_dataset (exact match), this performs regex matching so partial
    terms and patterns work. For example, {"JOBTITLE": "finance"} matches
    "Finance Manager", "Senior Finance Analyst", "VP of Financial Planning", etc.

    Returns up to max_results matching rows. When results are truncated, the
    last element will contain _truncated=True and the total match count.
    For discovery queries (e.g. "what segments match TISO?"), prefer
    count_by_column with fuzzy=True — it returns compact {value, count}
    summaries instead of full rows.

    Supports regex syntax: "finance|accounting" matches either term,
    "senior.*engineer" matches "Senior Software Engineer", etc.

    Args:
        dataset: Name of the dataset (from list_datasets).
        filters: Column-regex pairs to match, e.g. {"JOBTITLE": "finance", "OU": "london"}.
                 Column names are CASE-SENSITIVE and must exist in the
                 dataset: an unknown name is an error naming it and
                 listing the valid columns, and NO rows are returned.
                 Correct the name and retry -- never drop the filter.
                 (The case-insensitivity is in the VALUES matched, not
                 in the column names.)
        max_results: Maximum rows to return (default 100).
        columns: Optional list of columns to return, e.g.
                 ["ResourceID", "ResourceName"]. Omit for every column.
                 An unknown name is an error listing the valid columns.
    """
    logger.info("filter_dataset_fuzzy caller=%s dataset=%r filters=%r max_results=%d columns=%r",
                _caller(), dataset, filters, max_results, columns)
    results = csv_store.filter_rows_fuzzy(dataset, max_results=max_results,
                                          columns=columns, **filters)
    logger.info("filter_dataset_fuzzy returned %d rows", len(results))
    return results


@mcp.tool()
@_timed
def count_by_column(
    dataset: str, column: str | list[str], filters: dict[str, str] | None = None,
    fuzzy: bool = False,
) -> list[dict] | dict:
    """Count occurrences of each distinct value in a column, with optional filtering.

    Returns a compact list of {value, count} objects sorted descending by count.
    This is the PREFERRED tool for discovery queries like "what segments match X?"
    or "how many people have job title Y?" — it returns summary counts instead
    of full rows, keeping responses small.

    column may be a LIST to group by several columns at once, which is how you
    get an identifier and its label in ONE call instead of counting by id and
    then looking the names up separately. For example
    column=["ResourceID", "ResourceName"] returns
    [{"ResourceID": ..., "ResourceName": ..., "count": N}, ...].
    Group by several columns only when they describe the same thing (an id and
    its name); unrelated columns multiply the groups.

    When fuzzy=False (default), filters use exact matching.
    When fuzzy=True, filters use regex pattern matching (same as
    filter_dataset_fuzzy), so partial terms and patterns like
    "finance|accounting" work.

    Args:
        dataset: Name of the dataset (from list_datasets).
        column: Column to group and count by (e.g. "SEGMENTNAME", "ResourceID"),
                or a list of columns, e.g. ["ResourceID", "ResourceName"].
        filters: Optional column-value pairs to filter before counting,
                 e.g. {"JOBTITLE": "Software Engineer", "OU": "Finance"}.
                 Column names are CASE-SENSITIVE and must exist in the
                 dataset: an unknown name is an error naming it and
                 listing the valid columns, and NO counts are returned.
                 Correct the name and retry -- never drop the filter,
                 or the counts would cover everyone instead of the
                 group you asked about.
        fuzzy: If True, apply regex pattern matching on filter values
               instead of exact matching (default False).
    """
    logger.info(
        "count_by_column caller=%s dataset=%r column=%r filters=%r fuzzy=%r",
        _caller(), dataset, column, filters, fuzzy,
    )
    criteria = filters or {}
    results = csv_store.count_by_column(dataset, column, fuzzy=fuzzy, **criteria)
    logger.info("count_by_column returned %d groups", len(results) if isinstance(results, list) else 0)
    return results


@mcp.tool()
@_timed
def get_column_values(dataset: str, column: str) -> list[str] | dict:
    """List all distinct values in a column of a CSV dataset.

    Useful for discovering what values exist before filtering
    (e.g. what OUs, locations, or access right names are available).

    Args:
        dataset: Name of the dataset (from list_datasets).
        column: Column name to get values for (from list_datasets).
    """
    logger.info("get_column_values caller=%s dataset=%r column=%r", _caller(), dataset, column)
    return csv_store.get_distinct_values(dataset, column)


# ---------------------------------------------------------------------------
# Request tools
# ---------------------------------------------------------------------------

_REQUEST_CONFIG_PATH = Path(DOCS_DIR) / "request_config.json"


def _load_request_config() -> dict:
    """Load the request attributes schema from request_config.json."""
    if not _REQUEST_CONFIG_PATH.exists():
        return {"error": f"Request config not found at {_REQUEST_CONFIG_PATH}"}
    with open(_REQUEST_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


@mcp.tool()
@_timed
def get_request_attributes() -> dict:
    """Get the attributes required to raise an entitlement request.

    Returns a schema describing the required and optional fields that
    must be collected before calling raise_entitlement_request.
    Call this first so you know what information to gather from the user.
    """
    logger.debug("get_request_attributes called by %s", _caller())
    return _load_request_config()


@mcp.tool()
@_timed
def raise_entitlement_request(
    resource_id: str,
    justification: str,
    start_date: str = "",
    end_date: str = "",
) -> dict:
    """Submit an entitlement access request (placeholder).

    This is a placeholder that returns a mock confirmation. It will be
    replaced with a real API integration later.

    Args:
        resource_id: The ResourceID of the access right to request.
        justification: Business justification for why this access is needed.
        start_date: Optional requested start date (YYYY-MM-DD). Defaults to today.
        end_date: Optional requested end date (YYYY-MM-DD). Empty for permanent access.
    """
    logger.info(
        "raise_entitlement_request caller=%s resource_id=%r justification=%r start_date=%r end_date=%r",
        _caller(), resource_id, justification, start_date, end_date,
    )

    if not resource_id:
        return {"error": "resource_id is required."}
    if not justification:
        return {"error": "justification is required."}

    request_id = f"REQ-{uuid.uuid4().hex[:8].upper()}"
    return {
        "status": "submitted",
        "request_id": request_id,
        "resource_id": resource_id,
        "justification": justification,
        "start_date": start_date or "today",
        "end_date": end_date or "permanent",
        "message": f"Request {request_id} has been submitted successfully. "
                   f"This is a placeholder — no real request was created.",
    }


# ---------------------------------------------------------------------------
# Quality tools
# ---------------------------------------------------------------------------

_QUALITY_CRITERIA_PATH = Path(DOCS_DIR) / "quality_criteria.json"


def _load_quality_criteria() -> dict:
    """Load the quality criteria checklist from quality_criteria.json."""
    if not _QUALITY_CRITERIA_PATH.exists():
        return {"error": f"Quality criteria config not found at {_QUALITY_CRITERIA_PATH}"}
    with open(_QUALITY_CRITERIA_PATH, encoding="utf-8") as f:
        return json.load(f)


@mcp.tool()
@_timed
def get_quality_criteria() -> dict:
    """Get the data quality criteria checklist for resource evaluation.

    Returns a list of criteria, each with a name, description (what
    constitutes a pass), importance level, and range (Asset or Access Right).
    The quality agent uses these criteria to evaluate resource metadata.
    """
    logger.debug("get_quality_criteria called by %s", _caller())
    return _load_quality_criteria()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for the MCP server."""
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()

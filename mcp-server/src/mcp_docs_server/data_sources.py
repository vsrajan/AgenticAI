"""Ingestion sources for the MCP data store.

The data layer separates INGESTION (where rows come from) from
SERVING (how agent queries run -- csv_store.py, DuckDB). This module
is the ingestion side:

- CsvDataSource: loads *.csv files from the docs directory. Used for
  development, tests, and the shipped sample data.
- DatabaseSource: stub reserving the production path, where rows come
  from an Azure SQL or Postgres database via batch sync. See
  docs/P0.md section 11 for the full implementation runbook.

Both implement the same two-method contract, so csv_store.py never
knows or cares which one feeds it. Loads run inside a transaction the
store opens, so agents querying mid-refresh see the complete old data
until the commit -- never a mixture.
"""

import hashlib
import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("mcp_docs_server.data_sources")


class DataSource(Protocol):
    """Contract every ingestion source implements."""

    def fingerprint(self) -> str:
        """Cheap change detector.

        Returns any string that changes when the source data changes.
        Called every few minutes by the refresh sweeper, so it must be
        cheap: file mtimes for CSVs, a watermark query for a database
        -- never a scan of the data itself.
        """
        ...

    def load(self, con) -> list[str]:
        """(Re)create one DuckDB table per dataset.

        Runs inside a transaction opened by the caller. Uses
        CREATE OR REPLACE TABLE so the swap is atomic per table.
        Returns the dataset (table) names loaded.
        """
        ...


def _quote(identifier: str) -> str:
    """Quote an SQL identifier (table name from a filename)."""
    return '"' + identifier.replace('"', '""') + '"'


class CsvDataSource:
    """Loads every *.csv under docs_dir into DuckDB, one table per file.

    The table name is the filename stem (Entitlements.csv -> table
    Entitlements), matching the dataset names the agent prompts use.
    """

    def __init__(self, docs_dir):
        self.docs_dir = Path(docs_dir)

    def _csv_files(self) -> list[Path]:
        if not self.docs_dir.exists():
            return []
        return sorted(self.docs_dir.rglob("*.csv"))

    def fingerprint(self) -> str:
        """Hash of every CSV's path, mtime, and size -- cheap, and it
        changes whenever a file is added, removed, or rewritten."""
        parts = []
        for path in self._csv_files():
            stat = path.stat()
            parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    @staticmethod
    def _detect_encoding(path: Path) -> str | None:
        """Return an explicit encoding for non-utf8 files.

        Mirrors the old utf-8-sig -> cp1252 fallback: try decoding a
        chunk as utf-8; if that fails, use latin-1, which decodes any
        byte sequence. Detection happens here in python, BEFORE the
        load transaction, so a bad file cannot poison the transaction.
        """
        try:
            with open(path, "rb") as fh:
                fh.read(1 << 16).decode("utf-8")
            return None  # utf-8 (DuckDB's default; it also strips BOMs)
        except UnicodeDecodeError:
            return "latin-1"

    def load(self, con) -> list[str]:
        loaded = []
        for path in self._csv_files():
            name = path.stem
            escaped = str(path).replace("'", "''")
            encoding = self._detect_encoding(path)
            options = "all_varchar=true"  # every column stays a string,
            # matching the previous csv.DictReader semantics exactly
            if encoding:
                options += f", encoding='{encoding}'"
                logger.warning("Reading %s with fallback encoding %s", path.name, encoding)
            con.execute(
                f"CREATE OR REPLACE TABLE {_quote(name)} AS "
                f"SELECT * FROM read_csv_auto('{escaped}', {options})"
            )
            rows = con.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
            if rows == 0:
                # match the old loader: empty datasets are skipped
                con.execute(f"DROP TABLE {_quote(name)}")
                logger.warning("No rows in %s, skipping", path.name)
                continue
            logger.info("Loaded CSV %s: %d rows", name, rows)
            loaded.append(name)
        return loaded


class DatabaseSource:
    """Loads datasets from a production database (Azure SQL / Postgres).

    This is a stub -- the production database is not reachable from
    development. docs/P0.md section 11 is the step-by-step runbook for
    filling it in; the short version:

    Postgres (path A):
      1. In load(): INSTALL/LOAD the duckdb postgres extension, then
         ATTACH the connection string with (TYPE postgres, READ_ONLY).
      2. Per dataset: CREATE OR REPLACE TABLE <name> AS <query>, where
         the query selects from the attached database (src.schema.table).
      3. DETACH. Five million rows copy in seconds, server to file.

    Azure SQL (path B -- prefer B1):
      B1. Have an existing pipeline (ADF/Synapse) export the tables to
          parquet files on a schedule; load() becomes
          CREATE OR REPLACE TABLE <name> AS
          SELECT * FROM read_parquet('<path>'). No database driver or
          credentials on this server at all.
      B2. Direct pull with pyodbc: stream rows with fetchmany (never
          fetchall -- that recreates the memory problem this design
          fixes), insert into an _incoming table, then atomically
          CREATE OR REPLACE the real table from it.

    fingerprint() must stay cheap: a sync-log watermark
    (SELECT max(run_completed_at) FROM etl_log), an indexed
    max(updated_at), or parquet file mtimes. Never hash table contents.

    Config comes from env (see docs/P0.md 11.6): MCP_DATA_SOURCE,
    MCP_DB_CONN (secret, gitignored .env only -- never log it),
    MCP_DB_TABLES (dataset=query pairs), MCP_DB_WATERMARK.
    """

    def __init__(self, conn_string: str, tables: dict[str, str],
                 watermark_query: str | None = None):
        self._conn_string = conn_string
        self._tables = tables
        self._watermark_query = watermark_query

    def fingerprint(self) -> str:
        raise NotImplementedError(
            "DatabaseSource.fingerprint() is not implemented yet. "
            "See docs/P0.md section 11.5 for the watermark strategies."
        )

    def load(self, con) -> list[str]:
        raise NotImplementedError(
            "DatabaseSource.load() is not implemented yet. "
            "See docs/P0.md sections 11.3 (Postgres) and 11.4 (Azure SQL)."
        )

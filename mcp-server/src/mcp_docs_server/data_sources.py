"""Ingestion sources for the MCP data store.

The data layer separates INGESTION (where rows come from) from
SERVING (how agent queries run -- csv_store.py, DuckDB). This module
is the ingestion side:

- CsvDataSource: loads *.csv files from the docs directory. Used for
  development, tests, and the shipped sample data.
- ParquetDataSource: loads Parquet exports (e.g. Spark/Databricks
  jobs writing to Azure Storage, staged locally with az cli /
  azcopy). The PRODUCTION data path -- see docs/P0.md section 11.9.
- DatabaseSource: stub reserving the direct-database path (Azure SQL
  or Postgres batch sync). See docs/P0.md section 11.

All implement the same two-method contract, so csv_store.py never
knows or cares which one feeds it. Loads run inside a transaction the
store opens, so agents querying mid-refresh see the complete old data
until the commit -- never a mixture. build_data_source() picks the
source from MCP_DATA_SOURCE (csv default, parquet for production).
"""

import glob as globlib
import hashlib
import logging
import os
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


class ParquetDataSource:
    """Loads named Parquet exports into DuckDB, one table per dataset.

    Built for the production shape: a Spark/Databricks job writes each
    dataset as a DIRECTORY of part files (Resource.parquet/
    part-00000-*.parquet, plus _SUCCESS/_committed_* markers), which
    is staged to local disk with az cli or azcopy. A dataset therefore
    maps to a GLOB, not a single file -- DuckDB reads all matched
    parts as one table, and the *.parquet suffix naturally excludes
    the marker files.

    sources maps dataset name -> location, where location is a glob
    ("/data/Resource.parquet/*.parquet"), a directory (auto-expanded
    to dir/*.parquet), a single file, or an az:// URL (requires the
    duckdb azure extension to be loadable -- pre-installed in the
    image or sideloaded; remote URLs get no change detection, see
    fingerprint()).

    Every column is CAST TO VARCHAR on ingest. Parquet is typed
    (ints, dates), but the tool contract is string-based -- the
    filter/count SQL does lower(col) = lower(?) and regexp matching,
    which must keep behaving identically regardless of what fed the
    table. This mirrors CsvDataSource's all_varchar=true.
    """

    def __init__(self, sources: dict[str, str]):
        if not sources:
            raise ValueError(
                "ParquetDataSource requires at least one dataset "
                "(MCP_PARQUET_SOURCES=Name=/path/to/parts/*.parquet,...)"
            )
        self._sources = {
            name: self._normalize(location) for name, location in sources.items()
        }

    @staticmethod
    def _normalize(location: str) -> str:
        """Directory -> dir/*.parquet; globs, files, az:// pass through."""
        if location.startswith(("az://", "azure://", "abfss://")):
            return location
        path = Path(location)
        if path.is_dir():
            return str(path / "*.parquet")
        return location

    @staticmethod
    def _is_remote(location: str) -> bool:
        return location.startswith(("az://", "azure://", "abfss://"))

    def fingerprint(self) -> str:
        """Hash of every matched part file's path, mtime, and size.

        Same recipe as CsvDataSource: cheap stat calls, changes when a
        part is added, removed, or rewritten -- so a re-staged dataset
        triggers exactly one atomic re-ingest. A glob that matches
        nothing contributes a marker, so files APPEARING later also
        changes the fingerprint. Remote az:// locations contribute
        only their URL: no change detection (would need blob ETags --
        future work); they reload on restart only.
        """
        parts = []
        for name, location in sorted(self._sources.items()):
            if self._is_remote(location):
                parts.append(f"{name}:{location}")
                continue
            matched = sorted(globlib.glob(location))
            if not matched:
                parts.append(f"{name}:missing")
                continue
            for path in matched:
                stat = os.stat(path)
                parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def load(self, con) -> list[str]:
        if any(self._is_remote(loc) for loc in self._sources.values()):
            try:
                con.execute("LOAD azure")
            except Exception as exc:
                raise RuntimeError(
                    "A parquet source uses an az:// URL, which needs the "
                    "duckdb azure extension. INSTALL it where egress "
                    "allows, sideload it (docs/P0.md 11.9), or stage the "
                    f"files locally instead. Underlying error: {exc}"
                ) from exc

        loaded = []
        for name, location in self._sources.items():
            if not self._is_remote(location) and not globlib.glob(location):
                logger.warning("No parquet files match %s for dataset %s, skipping",
                               location, name)
                continue
            escaped = location.replace("'", "''")
            # COLUMNS(*)::VARCHAR casts every column, keeping names
            con.execute(
                f"CREATE OR REPLACE TABLE {_quote(name)} AS "
                f"SELECT COLUMNS(*)::VARCHAR FROM read_parquet('{escaped}')"
            )
            rows = con.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
            if rows == 0:
                con.execute(f"DROP TABLE {_quote(name)}")
                logger.warning("No rows in %s, skipping", name)
                continue
            logger.info("Loaded parquet %s: %d rows from %s", name, rows, location)
            loaded.append(name)
        return loaded


def parse_parquet_sources(raw: str) -> dict[str, str]:
    """Parse MCP_PARQUET_SOURCES: comma-separated Name=location pairs.

    Example:
      Resources=/data/Resource.parquet/*.parquet,Entitlements=/data/entitlement.parquet

    The dataset name becomes the DuckDB table name (and what the agent
    sees in list_datasets), so it is decoupled from however the export
    job named its output folder.
    """
    sources = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, location = pair.partition("=")
        if not sep or not name.strip() or not location.strip():
            raise ValueError(
                f"Malformed MCP_PARQUET_SOURCES entry {pair!r}: "
                "expected Name=/path/or/glob"
            )
        sources[name.strip()] = location.strip()
    return sources


def build_data_source(docs_dir) -> "DataSource":
    """Pick the ingestion source from MCP_DATA_SOURCE.

    csv (default) -> CsvDataSource over docs_dir, exactly as before.
    parquet       -> ParquetDataSource from MCP_PARQUET_SOURCES
                     (fails fast when unset: a misconfigured
                     production source must never silently fall back
                     to sample CSVs).
    """
    mode = os.environ.get("MCP_DATA_SOURCE", "csv").lower()
    if mode == "csv":
        return CsvDataSource(docs_dir)
    if mode == "parquet":
        raw = os.environ.get("MCP_PARQUET_SOURCES", "")
        if not raw:
            raise ValueError(
                "MCP_DATA_SOURCE=parquet requires MCP_PARQUET_SOURCES "
                "(comma-separated Name=/path/to/parts/*.parquet pairs)"
            )
        source = ParquetDataSource(parse_parquet_sources(raw))
        logger.info("Data source: parquet (%d dataset(s))", len(source._sources))
        return source
    raise ValueError(
        f"Unsupported MCP_DATA_SOURCE={mode!r}. Use 'csv' or 'parquet'."
    )


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

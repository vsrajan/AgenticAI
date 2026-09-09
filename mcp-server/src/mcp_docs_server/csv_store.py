"""Dataset store served by DuckDB behind the CSV tool contracts.

Serving and ingestion are separate concerns:

- SERVING (this module): agent queries -- filters, counts, distinct
  values -- run as SQL against an embedded DuckDB database. Vectorized
  and columnar, they stay in the low milliseconds even at millions of
  rows, where the previous pure-python row scans took seconds and the
  list-of-dicts storage took gigabytes.
- INGESTION (data_sources.py): where rows come from. CsvDataSource
  loads *.csv files (dev, tests, samples); DatabaseSource reserves the
  production path (Azure SQL / Postgres batch sync).

The database persists to a file (MCP_DB_PATH), so a restart with
unchanged source data starts in milliseconds -- ingestion runs only
when the source fingerprint changes. Refresh is atomic: a load runs
in one transaction, so readers see the complete old data until the
commit, then the complete new data. A background sweeper re-checks
the fingerprint every MCP_DATA_REFRESH_MINUTES.

Full-text search is size-gated: BM25 indexes exist only for datasets
with at most MCP_SEARCH_MAX_ROWS rows (the Resources catalogue).
Larger datasets (Entitlements at production volume) get a guidance
error steering the agent to the filter/count tools -- which is how
the resource prompt already uses them.

The class keeps its historical name (repo convention: MCP server
internals keep implementation names) -- the tool contracts hide the
storage engine.
"""

import logging
import os
import re
import threading
import time
from pathlib import Path

import duckdb
from rank_bm25 import BM25Okapi

from mcp_docs_server.data_sources import DataSource, build_data_source

logger = logging.getLogger("mcp_docs_server.csv_store")


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer with lowercasing.

    Input:  "SAP Finance Reporting"
    Output: ["sap", "finance", "reporting"]
    """
    return re.findall(r"\w+", text.lower())


def _quote(identifier: str) -> str:
    """Quote a table/column identifier for SQL.

    Values are always bound as parameters; identifiers cannot be
    bound, so they are validated against the actual schema first
    (only known tables/columns ever reach a query) and quoted here.
    """
    return '"' + identifier.replace('"', '""') + '"'


class _SearchIndex:
    """BM25 sidecar for one SMALL dataset.

    Keeps the rows in memory so search can return them -- acceptable
    only because the size gate caps how many rows this can be.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows
        corpus = [
            _tokenize(" ".join(str(v) for v in row.values() if v is not None))
            for row in rows
        ]
        self.bm25 = BM25Okapi(corpus)


class CsvStore:
    """DuckDB-backed dataset store; public surface unchanged from the
    previous in-memory implementation."""

    def __init__(self, docs_dir, db_path=None, source: DataSource | None = None,
                 search_max_rows: int | None = None,
                 refresh_minutes: float | None = None):
        self.docs_dir = Path(docs_dir)
        if db_path is None:
            db_path = os.environ.get(
                "MCP_DB_PATH", str(self.docs_dir / ".mcp_data.duckdb"))
        if search_max_rows is None:
            search_max_rows = int(os.environ.get("MCP_SEARCH_MAX_ROWS", "50000"))
        if refresh_minutes is None:
            refresh_minutes = float(os.environ.get("MCP_DATA_REFRESH_MINUTES", "15"))
        self._search_max_rows = search_max_rows
        # source selection: an injected source wins (tests); otherwise
        # MCP_DATA_SOURCE picks csv (default) or parquet -- see
        # data_sources.build_data_source
        self._source: DataSource = source or build_data_source(docs_dir)

        # refresh writes are serialised by this lock; reads run on
        # per-call cursors (duckdb allows those concurrently and reads
        # see committed data only)
        self._refresh_lock = threading.RLock()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db_path))
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS _sync_info "
            "(fingerprint VARCHAR, synced_at TIMESTAMP)"
        )

        self._columns: dict[str, list[str]] = {}
        self._search: dict[str, _SearchIndex] = {}

        if not self.refresh_if_stale():
            # warm start: data already current, just read the schema
            self._load_schema()
            self._build_search_indexes()
            logger.info("Warm start: %d dataset(s) already current", len(self._columns))

        if refresh_minutes > 0:
            # daemon thread so it never blocks process exit; works for
            # both stdio and sse transports
            thread = threading.Thread(
                target=self._sweep_loop, args=(refresh_minutes * 60,),
                daemon=True, name="data-refresh-sweeper",
            )
            thread.start()

    # -- refresh --

    def refresh_if_stale(self) -> bool:
        """Reload from the source if its fingerprint changed.

        Returns True when a reload happened. The load runs in one
        transaction: readers see the old tables until the commit.
        """
        new_fingerprint = self._source.fingerprint()
        with self._refresh_lock:
            stored = self._con.execute(
                "SELECT fingerprint FROM _sync_info").fetchone()
            if stored is not None and stored[0] == new_fingerprint:
                return False

            start = time.perf_counter()
            self._con.execute("BEGIN")
            try:
                loaded = self._source.load(self._con)
                # drop datasets that disappeared from the source
                existing = [r[0] for r in self._con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'").fetchall()]
                for table in existing:
                    if not table.startswith("_") and table not in loaded:
                        self._con.execute(f"DROP TABLE {_quote(table)}")
                self._con.execute("DELETE FROM _sync_info")
                self._con.execute(
                    "INSERT INTO _sync_info VALUES (?, current_timestamp)",
                    [new_fingerprint],
                )
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise

            self._load_schema()
            self._build_search_indexes()
            logger.info(
                "Data refresh complete in %.2fs: %s",
                time.perf_counter() - start,
                ", ".join(f"{n} ({self._row_count(n)} rows)"
                          for n in self.dataset_names) or "no datasets",
            )
            return True

    def _sweep_loop(self, interval_seconds: float) -> None:
        """Background staleness check; reloads only on fingerprint change."""
        while True:
            time.sleep(interval_seconds)
            try:
                self.refresh_if_stale()
            except Exception:
                logger.exception("Background data refresh failed")

    # -- schema --

    def _load_schema(self) -> None:
        rows = self._con.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
        ).fetchall()
        columns: dict[str, list[str]] = {}
        for table, column in rows:
            if table.startswith("_"):
                continue  # internal tables (_sync_info)
            columns.setdefault(table, []).append(column)
        self._columns = columns  # atomic swap; readers never see a partial dict

    def _build_search_indexes(self) -> None:
        """Build BM25 sidecars for datasets under the size gate."""
        indexes: dict[str, _SearchIndex] = {}
        for name in self._columns:
            count = self._row_count(name)
            if count > self._search_max_rows:
                logger.info(
                    "No search index for %s: %d rows > MCP_SEARCH_MAX_ROWS=%d",
                    name, count, self._search_max_rows,
                )
                continue
            result = self._con.execute(f"SELECT * FROM {_quote(name)}")
            cols = [d[0] for d in result.description]
            rows = [dict(zip(cols, r)) for r in result.fetchall()]
            indexes[name] = _SearchIndex(rows)
            logger.info("BM25 index built for %s: %d rows", name, count)
        self._search = indexes

    def _row_count(self, name: str) -> int:
        return self._con.execute(
            f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]

    def _synced_at(self) -> str | None:
        row = self._con.execute("SELECT synced_at FROM _sync_info").fetchone()
        return row[0].isoformat() if row and row[0] else None

    @property
    def dataset_names(self) -> list[str]:
        return sorted(self._columns.keys())

    # -- query building --
    # values are ALWAYS bound parameters; identifiers are validated
    # against the schema (unknown filter columns are silently ignored,
    # matching the previous implementation) and quoted

    def _where(self, dataset: str, criteria: dict, fuzzy: bool,
               literal: bool = False) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        for column, value in criteria.items():
            if column not in self._columns[dataset]:
                continue
            q = _quote(column)
            if not fuzzy:
                clauses.append(f"lower({q}) = lower(?)")
            elif literal:
                # fallback when the pattern is not valid regex: plain
                # case-insensitive substring match, mirroring the old
                # escape-and-retry behavior
                clauses.append(f"contains(lower({q}), lower(?))")
            else:
                clauses.append(f"regexp_matches({q}, ?, 'i')")
            params.append(str(value))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def _rows_to_dicts(self, result) -> list[dict]:
        cols = [d[0] for d in result.description]
        return [dict(zip(cols, row)) for row in result.fetchall()]

    def _select_list(self, dataset: str, columns: list[str] | None) -> str | dict:
        """Build the SELECT list for a projection, or an error dict.

        columns=None -> "*" (every column, the historical behavior).
        Unknown names are an ERROR here, deliberately unlike _where,
        which skips unknown filter columns: a filter that quietly does
        nothing returns too many rows, but a projection that quietly
        drops a column would hide data the caller asked for.
        """
        if columns is None:
            return "*"
        known = self._columns[dataset]
        unknown = [c for c in columns if c not in known]
        if unknown:
            return {"error": f"Column(s) not found: {', '.join(unknown)}",
                    "available_columns": known}
        if not columns:
            return "*"
        return ", ".join(_quote(c) for c in columns)

    def _run_filter(self, dataset_name: str, max_results: int, criteria: dict,
                    fuzzy: bool, hint: str,
                    columns: list[str] | None = None) -> list[dict]:
        """Shared body of filter_rows / filter_rows_fuzzy."""
        if dataset_name not in self._columns:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        select = self._select_list(dataset_name, columns)
        if isinstance(select, dict):
            return [select]
        cursor = self._con.cursor()
        try:
            where, params = self._where(dataset_name, criteria, fuzzy)
            table = _quote(dataset_name)
            try:
                # LIMIT max+1: one extra row cheaply detects truncation
                result = cursor.execute(
                    f"SELECT {select} FROM {table}{where} LIMIT {int(max_results) + 1}",
                    params,
                )
                rows = self._rows_to_dicts(result)
            except duckdb.Error:
                if not fuzzy:
                    raise
                # invalid regex for duckdb's RE2 -- retry as literal text
                where, params = self._where(dataset_name, criteria, fuzzy,
                                            literal=True)
                result = cursor.execute(
                    f"SELECT {select} FROM {table}{where} LIMIT {int(max_results) + 1}",
                    params,
                )
                rows = self._rows_to_dicts(result)

            if len(rows) > max_results:
                total = cursor.execute(
                    f"SELECT count(*) FROM {table}{where}", params).fetchone()[0]
                rows = rows[:max_results]
                rows.append({
                    "_truncated": True,
                    "_total_matches": total,
                    "_returned": max_results,
                    "_message": (
                        f"Showing {max_results} of {total} matches. {hint}"
                    ),
                })
            return rows
        finally:
            cursor.close()

    # -- public query surface (contracts unchanged) --

    def list_datasets(self) -> dict:
        """Metadata for all datasets, plus data freshness.

        Output shape matches the previous implementation, with one
        addition: synced_at reports when the data was last ingested.
        """
        if not self._columns:
            return {"message": "No CSV files found in the docs directory."}
        return {
            "datasets": {
                name: {
                    "columns": self._columns[name],
                    "row_count": self._row_count(name),
                }
                for name in self.dataset_names
            },
            "total_datasets": len(self._columns),
            "synced_at": self._synced_at(),
        }

    def search(self, dataset_name: str, query: str, max_results: int = 10) -> list[dict]:
        """BM25 search -- size-gated (see module docstring)."""
        if dataset_name not in self._columns:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        index = self._search.get(dataset_name)
        if index is None:
            return [{
                "error": (
                    f"Dataset {dataset_name} is too large for free-text "
                    f"search ({self._row_count(dataset_name)} rows)."
                ),
                "hint": ("Use filter_dataset, filter_dataset_fuzzy, or "
                         "count_by_column instead."),
            }]
        tokens = _tokenize(query)
        results: list[dict] = []
        if tokens:
            scores = index.bm25.get_scores(tokens)
            scored = sorted(zip(scores, range(len(index.rows))),
                            key=lambda x: x[0], reverse=True)
            for score, i in scored[:max_results]:
                if score <= 0:
                    break
                results.append({**index.rows[i], "_score": round(float(score), 3)})
        if not results:
            return [{"message": "No matching rows found.", "query": query}]
        return results

    def filter_rows(self, dataset_name: str, max_results: int = 100,
                    columns: list[str] | None = None,
                    **criteria: str) -> list[dict]:
        """Exact-match filter (case-insensitive), AND across columns.

        columns projects the result to those fields only; None returns
        every column.
        """
        return self._run_filter(
            dataset_name, max_results, criteria, fuzzy=False,
            hint=("Use count_by_column for compact summaries, "
                  "or add more filter columns to narrow results."),
            columns=columns,
        )

    def filter_rows_fuzzy(self, dataset_name: str, max_results: int = 100,
                          columns: list[str] | None = None,
                          **criteria: str) -> list[dict]:
        """Regex filter (case-insensitive); literal fallback on bad regex.

        columns projects the result to those fields only; None returns
        every column.
        """
        return self._run_filter(
            dataset_name, max_results, criteria, fuzzy=True,
            hint=("Use count_by_column with fuzzy=True for compact "
                  "summaries, or add more filter columns to narrow results."),
            columns=columns,
        )

    def count_by_column(self, dataset_name: str, column: str | list[str],
                        fuzzy: bool = False, **criteria: str) -> list[dict] | dict:
        """Group-count one or more columns after optional filters.

        A single column keeps the historical shape: [{value, count}].
        A list groups by every column and names each field, e.g.
        ["ResourceID", "ResourceName"] -> [{ResourceID, ResourceName,
        count}]. Grouping by several columns is how one call returns an
        identifier together with its label (they are 1:1), instead of
        needing a second lookup to resolve names.
        """
        if dataset_name not in self._columns:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        multi = not isinstance(column, str)
        cols = list(column) if multi else [column]
        if not cols:
            return {"error": "No column given",
                    "available_columns": self._columns[dataset_name]}
        unknown = [c for c in cols if c not in self._columns[dataset_name]]
        if unknown:
            return {"error": f"Column(s) not found: {', '.join(unknown)}",
                    "available_columns": self._columns[dataset_name]}
        cursor = self._con.cursor()
        try:
            quoted = [_quote(c) for c in cols]
            group_by = ", ".join(quoted)
            # the FIRST column carries the non-empty guard: it is the one
            # being counted over, and the historical single-column
            # behavior skipped its empty values
            first = quoted[0]
            if multi:
                select = ", ".join(f"{q} AS {_quote(c)}"
                                   for q, c in zip(quoted, cols))
            else:
                select = f"{first} AS value"

            def run(literal: bool):
                where, params = self._where(dataset_name, criteria, fuzzy, literal)
                # skip empty values, like the previous implementation
                where += (" AND " if where else " WHERE ")
                where += f"({first} IS NOT NULL AND {first} <> '')"
                return cursor.execute(
                    f"SELECT {select}, count(*) AS count "
                    f"FROM {_quote(dataset_name)}{where} "
                    f"GROUP BY {group_by} ORDER BY count DESC, {first}",
                    params,
                ).fetchall()

            try:
                rows = run(literal=False)
            except duckdb.Error:
                if not fuzzy:
                    raise
                rows = run(literal=True)
            if multi:
                return [dict(zip(cols + ["count"], row)) for row in rows]
            return [{"value": value, "count": count} for value, count in rows]
        finally:
            cursor.close()

    def get_distinct_values(self, dataset_name: str, column: str) -> list[str] | dict:
        """Sorted distinct non-empty values of a column."""
        if dataset_name not in self._columns:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in self._columns[dataset_name]:
            return {"error": f"Column not found: {column}",
                    "available_columns": self._columns[dataset_name]}
        cursor = self._con.cursor()
        try:
            q = _quote(column)
            rows = cursor.execute(
                f"SELECT DISTINCT {q} FROM {_quote(dataset_name)} "
                f"WHERE {q} IS NOT NULL AND {q} <> '' ORDER BY {q}"
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            cursor.close()

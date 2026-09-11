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

CONNECTION RULE: a duckdb handle -- the connection OR a cursor --
must never be used by two threads at once. Both failure modes are
silent: two threads sharing one handle get each other's result rows
back, with plausible row counts and no exception. So after __init__,
self._con is a FACTORY only. Every query runs on its own cursor
(cursors are ~6us, leak-free, and release the GIL, so they also let
tool calls genuinely overlap), and the refresh transaction gets a
dedicated cursor of its own for the whole BEGIN..COMMIT span --
transaction control is per-connection state, so the BEGIN, the
source's CREATE OR REPLACE statements and the COMMIT must share one
handle. Cursors do not see the refresh's uncommitted writes, which
is what makes the swap atomic from a reader's point of view.

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
from contextlib import contextmanager
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

        # refresh WRITERS are serialised by this lock (a foreground
        # startup refresh vs the sweeper thread). Readers never take
        # it -- duckdb's MVCC isolation is what protects them, and
        # making them wait would stall every tool call for the length
        # of a multi-second rebuild.
        self._refresh_lock = threading.RLock()

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # after this constructor, self._con is only ever used to make
        # cursors -- see the CONNECTION RULE in the module docstring
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

    # -- cursors --

    @contextmanager
    def _cursor(self):
        """A private duckdb handle for one caller, closed afterwards.

        Every read takes one of these. Never cache or pool the result:
        a cursor shared between threads corrupts results exactly like a
        shared connection does (see the module docstring).
        """
        cursor = self._con.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    # -- refresh --

    def refresh_if_stale(self) -> bool:
        """Reload from the source if its fingerprint changed.

        Returns True when a reload happened. The load runs in one
        transaction on a dedicated cursor: readers, who are on cursors
        of their own, see the old tables until the commit.
        """
        new_fingerprint = self._source.fingerprint()
        with self._refresh_lock, self._cursor() as con:
            stored = con.execute(
                "SELECT fingerprint FROM _sync_info").fetchone()
            if stored is not None and stored[0] == new_fingerprint:
                return False

            start = time.perf_counter()
            con.execute("BEGIN")
            try:
                loaded = self._source.load(con)
                # drop datasets that disappeared from the source. this
                # stays INSIDE the transaction: an uncommitted drop is
                # invisible to readers, so they never hit the window
                # where the table does not exist
                existing = [r[0] for r in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'").fetchall()]
                for table in existing:
                    if not table.startswith("_") and table not in loaded:
                        con.execute(f"DROP TABLE {_quote(table)}")
                con.execute("DELETE FROM _sync_info")
                con.execute(
                    "INSERT INTO _sync_info VALUES (?, current_timestamp)",
                    [new_fingerprint],
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

            # the data is committed, but our in-memory view of it is
            # not rebuilt yet. if that rebuild fails, clear the stored
            # fingerprint so the next sweep retries -- otherwise it
            # would see "already current" and serve a stale schema and
            # search index until the source happens to change again
            try:
                self._load_schema()
                self._build_search_indexes()
            except Exception:
                con.execute("DELETE FROM _sync_info")
                raise

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
        with self._cursor() as cursor:
            rows = cursor.execute(
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
        with self._cursor() as cursor:
            for name in self._columns:
                count = self._row_count(name, cursor)
                if count > self._search_max_rows:
                    logger.info(
                        "No search index for %s: %d rows > MCP_SEARCH_MAX_ROWS=%d",
                        name, count, self._search_max_rows,
                    )
                    continue
                result = cursor.execute(f"SELECT * FROM {_quote(name)}")
                cols = [d[0] for d in result.description]
                rows = [dict(zip(cols, r)) for r in result.fetchall()]
                indexes[name] = _SearchIndex(rows)
                logger.info("BM25 index built for %s: %d rows", name, count)
        self._search = indexes

    def _row_count(self, name: str, cursor=None) -> int:
        """Row count for one dataset, on the caller's cursor or a fresh one."""
        if cursor is not None:
            return cursor.execute(
                f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
        with self._cursor() as own:
            return own.execute(
                f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]

    def _synced_at(self, cursor=None) -> str | None:
        """When the data was last ingested, or None before the first load."""
        if cursor is None:
            with self._cursor() as own:
                return self._synced_at(own)
        row = cursor.execute("SELECT synced_at FROM _sync_info").fetchone()
        return row[0].isoformat() if row and row[0] else None

    @property
    def dataset_names(self) -> list[str]:
        return sorted(self._columns.keys())

    # -- query building --
    # values are ALWAYS bound parameters; identifiers are validated
    # against the schema and quoted. an unknown name -- in a filter, a
    # projection, or a group-by -- is an ERROR, never a silent skip: a
    # filter that quietly does nothing returns EVERY row, which in an
    # access governance answer presents one person's entitlements as
    # another's, indistinguishably from a correct result

    def _unknown_filter_columns(self, dataset: str, criteria: dict) -> list[str]:
        """Filter column names that do not exist on the dataset."""
        known = self._columns[dataset]
        return [column for column in criteria if column not in known]

    def _filter_column_error(self, dataset: str, unknown: list[str]) -> dict:
        """The error payload for unknown filter columns.

        Shaped as an instruction, not just a complaint: naming the bad
        column and listing the real ones is what lets an agent correct
        the call instead of reporting "nothing found" or, worse,
        retrying without the filter.
        """
        return {
            "error": f"Filter column(s) not found: {', '.join(unknown)}",
            "available_columns": self._columns[dataset],
            "hint": ("Column names are case-sensitive. Correct the name "
                     "from available_columns and retry the same call -- "
                     "do not drop the filter."),
        }

    def _where(self, dataset: str, criteria: dict, fuzzy: bool,
               literal: bool = False) -> tuple[str, list]:
        """Build the WHERE clause. Callers validate the column names
        first (_unknown_filter_columns), so every key here is known."""
        clauses: list[str] = []
        params: list = []
        for column, value in criteria.items():
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
        Unknown names are an ERROR here, the same rule filter columns
        follow: a projection that quietly dropped a column would hide
        data the caller asked for, and a filter that quietly did
        nothing would return rows the caller never asked for.
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
        # validate before running anything, so the literal-regex retry
        # below cannot reach _where with an unvalidated column either.
        # single-element LIST, not a bare dict: filter_dataset is typed
        # -> list[dict], and FastMCP validates tool output against that
        # annotation -- a bare dict becomes a protocol-level ToolError
        # that aborts the turn instead of a message the agent can act on
        unknown = self._unknown_filter_columns(dataset_name, criteria)
        if unknown:
            return [self._filter_column_error(dataset_name, unknown)]
        select = self._select_list(dataset_name, columns)
        if isinstance(select, dict):
            return [select]
        with self._cursor() as cursor:
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

    # -- public query surface --

    def list_datasets(self) -> dict:
        """Metadata for all datasets, plus data freshness.

        Output shape matches the previous implementation, with one
        addition: synced_at reports when the data was last ingested.
        """
        # one snapshot of the schema dict, so the names, their columns
        # and their counts all describe the same generation of the data
        # even if a refresh commits while this call is running
        columns = self._columns
        if not columns:
            return {"message": "No CSV files found in the docs directory."}
        with self._cursor() as cursor:
            return {
                "datasets": {
                    name: {
                        "columns": columns[name],
                        "row_count": self._row_count(name, cursor),
                    }
                    for name in sorted(columns)
                },
                "total_datasets": len(columns),
                "synced_at": self._synced_at(cursor),
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
        # the FILTER columns need the same check as the group-by ones
        # above. this is the call the peer-recommendation flow leans on,
        # so a skipped filter here would count the whole company as the
        # user's peer group. bare dict, matching this method's other
        # errors and its -> list[dict] | dict annotation
        unknown_filters = self._unknown_filter_columns(dataset_name, criteria)
        if unknown_filters:
            return self._filter_column_error(dataset_name, unknown_filters)
        with self._cursor() as cursor:
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

    def get_distinct_values(self, dataset_name: str, column: str) -> list[str] | dict:
        """Sorted distinct non-empty values of a column."""
        if dataset_name not in self._columns:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in self._columns[dataset_name]:
            return {"error": f"Column not found: {column}",
                    "available_columns": self._columns[dataset_name]}
        with self._cursor() as cursor:
            q = _quote(column)
            rows = cursor.execute(
                f"SELECT DISTINCT {q} FROM {_quote(dataset_name)} "
                f"WHERE {q} IS NOT NULL AND {q} <> '' ORDER BY {q}"
            ).fetchall()
            return [row[0] for row in rows]

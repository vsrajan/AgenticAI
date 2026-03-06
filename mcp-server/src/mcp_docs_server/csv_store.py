"""CSV data store with BM25 search and column-based filtering.

Recursively reads all CSV files from a directory, discovers columns
automatically from the header row, and provides:
  - Full-text BM25 search across all text columns
  - Column-based filtering and grouping
  - Distinct value listing per column (for agent discovery)
"""

import csv
import logging
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

logger = logging.getLogger("mcp_docs_server.csv_store")


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer with lowercasing.

    Input:  "SAP Finance Reporting"
    Output: ["sap", "finance", "reporting"]
    """
    return re.findall(r"\w+", text.lower())


class CsvDataset:
    """A single CSV file loaded into memory with search support."""

    def __init__(self, name: str, path: Path, rows: list[dict], columns: list[str]):
        """Initialise a dataset from pre-read CSV data.

        Args:
            name:    Dataset identifier, e.g. "Entitlements"
            path:    Absolute path, e.g. Path("/data/docs/Entitlements.csv")
            rows:    List of dicts — one per CSV row, e.g.
                     [{"ResourceID": "R001", "JOBTITLE": "Analyst", "OU": "Finance"},
                      {"ResourceID": "R002", "JOBTITLE": "Manager", "OU": "HR"}]
            columns: Header names, e.g. ["ResourceID", "JOBTITLE", "OU"]
        """
        self.name = name
        self.path = path
        self.rows = rows
        self.columns = columns
        self._bm25: BM25Okapi | None = None
        self._build_index()

    def _build_index(self) -> None:
        """Build a BM25 index over all row text.

        Concatenates every cell in a row into one string, tokenizes it,
        and feeds the resulting corpus to BM25Okapi.

        Given rows:
            [{"ResourceID": "R001", "name": "SAP Finance Reporting"}]

        Produces corpus:
            [["r001", "sap", "finance", "reporting"]]
        """
        if not self.rows:
            return
        corpus = [
            _tokenize(" ".join(str(v) for v in row.values()))
            for row in self.rows
        ]
        self._bm25 = BM25Okapi(corpus)
        logger.info(
            "BM25 index built for %s: %d rows, %d columns",
            self.name, len(self.rows), len(self.columns),
        )

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Full-text BM25 search across all columns.

        Tokenizes *query*, scores every row against the BM25 index, and
        returns the top-*max_results* rows (score > 0) with a ``_score``
        field appended.

        Input:  query="SAP finance", max_results=2
        Output: [
                    {"ResourceID": "R001", "name": "SAP Finance Reporting",
                     "DESCRIPTION": "...", "_score": 4.721},
                    {"ResourceID": "R045", "name": "SAP FI Access",
                     "DESCRIPTION": "...", "_score": 3.108},
                ]
        Returns [] if no rows score above 0.
        """
        if not self._bm25 or not self.rows:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        scored = sorted(zip(scores, self.rows), key=lambda x: x[0], reverse=True)
        results = []
        for score, row in scored[:max_results]:
            if score <= 0:
                break
            results.append({**row, "_score": round(float(score), 3)})
        return results

    # -- Private helpers (uncapped, used internally by count_by) ----------

    def _filter_rows(self, **criteria: str) -> list[dict]:
        """Core exact-match filter — returns ALL matches, no cap."""
        matching = self.rows
        for col, value in criteria.items():
            if col not in self.columns:
                continue
            value_lower = value.lower()
            matching = [
                row for row in matching
                if str(row.get(col, "")).lower() == value_lower
            ]
        return matching

    def _filter_rows_fuzzy(self, **criteria: str) -> list[dict]:
        """Core regex filter — returns ALL matches, no cap."""
        matching = self.rows
        for col, value in criteria.items():
            if col not in self.columns:
                continue
            try:
                pattern = re.compile(value, re.IGNORECASE)
            except re.error:
                pattern = re.compile(re.escape(value), re.IGNORECASE)
            matching = [
                row for row in matching
                if pattern.search(str(row.get(col, "")))
            ]
        return matching

    # -- Public filter methods (capped) ------------------------------------

    def filter(self, max_results: int = 100, **criteria: str) -> tuple[list[dict], int]:
        """Filter rows where columns match the given values (case-insensitive).

        Each keyword argument is a column=value exact-match filter.
        Multiple criteria are ANDed together.  Returns at most
        *max_results* rows plus the total match count.

        Input:  filter(JOBTITLE="Analyst", OU="Finance")
        Output: (
                    [{"ResourceID": "R001", ...}, {"ResourceID": "R017", ...}],
                    2,   # total matches
                )
        Returns ([], 0) if no rows match all criteria.
        """
        matching = self._filter_rows(**criteria)
        total = len(matching)
        return matching[:max_results], total

    def filter_fuzzy(self, max_results: int = 100, **criteria: str) -> tuple[list[dict], int]:
        """Filter rows where columns match the given regex patterns (case-insensitive).

        Like ``filter()``, but each value is treated as a regex pattern
        (falls back to literal match if the regex is invalid).  Returns at
        most *max_results* rows plus the total match count.

        Input:  filter_fuzzy(JOBTITLE="finance|accounting", OU="HR")
        Output: (
                    [{"ResourceID": "R005", ...}, {"ResourceID": "R012", ...}],
                    2,   # total matches
                )
        Returns ([], 0) if no rows match all patterns.
        """
        matching = self._filter_rows_fuzzy(**criteria)
        total = len(matching)
        return matching[:max_results], total

    def distinct(self, column: str) -> list[str]:
        """Return sorted distinct non-empty values for a column.

        Input:  distinct("OU")
        Output: ["Engineering", "Finance", "HR", "Marketing"]
        Returns [] if the column does not exist.
        """
        if column not in self.columns:
            return []
        values = {str(row[column]) for row in self.rows if row.get(column)}
        return sorted(values)

    def count_by(self, column: str, fuzzy: bool = False, **criteria: str) -> list[dict]:
        """Filter rows by *criteria*, then count occurrences of each
        distinct value in *column*.

        When *fuzzy* is ``False`` (default), applies exact-match filters.
        When *fuzzy* is ``True``, applies regex pattern matching (same
        semantics as ``filter_fuzzy``).

        Input:  count_by("ResourceID", JOBTITLE="Analyst", OU="Finance")
        Output: [
                    {"value": "R001", "count": 14},
                    {"value": "R045", "count": 9},
                    {"value": "R102", "count": 3},
                ]
        Results are sorted descending by count.
        Returns [] if *column* does not exist.
        """
        if criteria:
            rows = self._filter_rows_fuzzy(**criteria) if fuzzy else self._filter_rows(**criteria)
        else:
            rows = self.rows
        if column not in self.columns:
            return []
        counts: dict[str, int] = {}
        for row in rows:
            val = str(row.get(column, "")).strip()
            if val:
                counts[val] = counts.get(val, 0) + 1
        return sorted(
            [{"value": v, "count": c} for v, c in counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )


class CsvStore:
    """Manages all CSV files in a directory, each as a named dataset."""

    def __init__(self, docs_dir: str) -> None:
        """Load every CSV file found under *docs_dir* into memory.

        Args:
            docs_dir: Root directory to scan, e.g. "/data/docs".
                      All ``*.csv`` files found recursively become
                      named datasets keyed by filename stem
                      (e.g. ``/data/docs/access/Entitlements.csv``
                      → dataset name ``"Entitlements"``).
        """
        self.docs_dir = Path(docs_dir)
        self._datasets: dict[str, CsvDataset] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Recursively find and load all ``*.csv`` files under ``docs_dir``.

        Populates ``self._datasets`` — e.g. after scanning a directory
        containing ``Entitlements.csv`` and ``Resources.csv``::

            self._datasets == {
                "Entitlements": CsvDataset(..., rows=[{...}, ...]),
                "Resources":    CsvDataset(..., rows=[{...}, ...]),
            }
        """
        if not self.docs_dir.exists():
            logger.warning("Docs directory does not exist: %s", self.docs_dir)
            return

        logger.info("Scanning for CSV files in %s", self.docs_dir)

        for csv_path in sorted(self.docs_dir.rglob("*.csv")):
            rel_path = csv_path.relative_to(self.docs_dir)
            name = csv_path.stem  # filename without extension as the dataset name
            try:
                rows, columns = self._read_csv(csv_path)
            except Exception:
                logger.exception("Failed to read %s, skipping", rel_path)
                continue

            if not rows:
                logger.warning("No rows in %s, skipping", rel_path)
                continue

            self._datasets[name] = CsvDataset(name, csv_path, rows, columns)
            logger.info("Loaded CSV %s: %d rows, columns=%s", name, len(rows), columns)

    @staticmethod
    def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
        """Read a CSV file and return ``(rows_as_dicts, column_names)``.

        Input:  path pointing to a CSV whose contents are::

                    ResourceID,name,DESCRIPTION
                    R001,SAP Finance Reporting,Access to SAP FI module
                    R002,Jira Admin,Full admin on Jira

        Output: (
                    [{"ResourceID": "R001", "name": "SAP Finance Reporting",
                      "DESCRIPTION": "Access to SAP FI module"},
                     {"ResourceID": "R002", "name": "Jira Admin",
                      "DESCRIPTION": "Full admin on Jira"}],
                    ["ResourceID", "name", "DESCRIPTION"],
                )
        """
        for enc in ("utf-8-sig", "cp1252"):
            try:
                with open(path, newline="", encoding=enc) as f:
                    reader = csv.DictReader(f)
                    columns = reader.fieldnames or []
                    rows = list(reader)
                if enc != "utf-8-sig":
                    logger.warning("Read %s with fallback encoding %s", path.name, enc)
                return rows, list(columns)
            except UnicodeDecodeError:
                continue
        # all encodings failed -- raise so _load_all logs and skips
        raise UnicodeDecodeError(
            "utf-8", b"", 0, 1,
            f"Could not decode {path.name} with any supported encoding",
        )

    @property
    def dataset_names(self) -> list[str]:
        return sorted(self._datasets.keys())

    def get_dataset(self, name: str) -> CsvDataset | None:
        return self._datasets.get(name)

    def list_datasets(self) -> dict:
        """Return metadata about all loaded CSV datasets.

        Output (example with two loaded CSVs)::

            {
                "datasets": {
                    "Entitlements": {
                        "columns": ["ResourceID", "JOBTITLE", "OU", ...],
                        "row_count": 54210,
                    },
                    "Resources": {
                        "columns": ["ResourceID", "name", "DESCRIPTION", ...],
                        "row_count": 820,
                    },
                },
                "total_datasets": 2,
            }

        Returns ``{"message": "No CSV files found ..."}`` when empty.
        """
        if not self._datasets:
            return {"message": "No CSV files found in the docs directory."}
        return {
            "datasets": {
                name: {
                    "columns": ds.columns,
                    "row_count": len(ds.rows),
                }
                for name, ds in sorted(self._datasets.items())
            },
            "total_datasets": len(self._datasets),
        }

    def search(self, dataset_name: str, query: str, max_results: int = 10) -> list[dict]:
        """BM25 search on a named dataset.  Delegates to ``CsvDataset.search``.

        Input:  search("Resources", "SAP finance", max_results=2)
        Output: [
                    {"ResourceID": "R001", "name": "SAP Finance Reporting",
                     "DESCRIPTION": "...", "_score": 4.721},
                    {"ResourceID": "R045", "name": "SAP FI Access",
                     "DESCRIPTION": "...", "_score": 3.108},
                ]

        Returns [{"error": "Dataset not found: ...", "available": [...]}]
        if the dataset name is invalid, or
        [{"message": "No matching rows found.", "query": "..."}]
        if nothing scored above 0.
        """
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        results = ds.search(query, max_results)
        if not results:
            return [{"message": "No matching rows found.", "query": query}]
        return results

    def filter_rows(self, dataset_name: str, max_results: int = 100, **criteria: str) -> list[dict]:
        """Exact-match filter on a named dataset.  Delegates to ``CsvDataset.filter``.

        Returns at most *max_results* rows.  When the total number of
        matches exceeds *max_results*, a metadata dict with
        ``_truncated=True`` is appended as the last element.

        Returns an error dict if the dataset name is invalid.
        """
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        rows, total = ds.filter(max_results=max_results, **criteria)
        if total > max_results:
            rows.append({
                "_truncated": True,
                "_total_matches": total,
                "_returned": max_results,
                "_message": (
                    f"Showing {max_results} of {total} matches. "
                    f"Use count_by_column for compact summaries, "
                    f"or add more filter columns to narrow results."
                ),
            })
        return rows

    def filter_rows_fuzzy(self, dataset_name: str, max_results: int = 100, **criteria: str) -> list[dict]:
        """Regex-match filter on a named dataset.  Delegates to ``CsvDataset.filter_fuzzy``.

        Returns at most *max_results* rows.  When the total number of
        matches exceeds *max_results*, a metadata dict with
        ``_truncated=True`` is appended as the last element.

        Returns an error dict if the dataset name is invalid.
        """
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        rows, total = ds.filter_fuzzy(max_results=max_results, **criteria)
        if total > max_results:
            rows.append({
                "_truncated": True,
                "_total_matches": total,
                "_returned": max_results,
                "_message": (
                    f"Showing {max_results} of {total} matches. "
                    f"Use count_by_column with fuzzy=True for compact "
                    f"summaries, or add more filter columns to narrow results."
                ),
            })
        return rows

    def count_by_column(
        self, dataset_name: str, column: str, fuzzy: bool = False, **criteria: str
    ) -> list[dict] | dict:
        """Group-count a column after applying optional filters.

        Delegates to ``CsvDataset.count_by``.  When *fuzzy* is ``True``,
        filter values are treated as regex patterns (case-insensitive);
        when ``False`` (default), exact matching is used.

        Commonly used for peer-based recommendations: filter Entitlements
        by JOBTITLE + OU, then count by ResourceID to rank the most
        popular access rights.

        Returns an error dict if the dataset or column is invalid.
        """
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in ds.columns:
            return {"error": f"Column not found: {column}",
                    "available_columns": ds.columns}
        return ds.count_by(column, fuzzy=fuzzy, **criteria)

    def get_distinct_values(self, dataset_name: str, column: str) -> list[str] | dict:
        """Return sorted distinct values for a column in a dataset.

        Useful for low-cardinality columns (OU, AREANAME, etc.) where
        listing all options is practical.

        Input:  get_distinct_values("Entitlements", "OU")
        Output: ["Engineering", "Finance", "HR", "Marketing"]

        Returns an error dict if the dataset or column is invalid.
        """
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in ds.columns:
            return {"error": f"Column not found: {column}",
                    "available_columns": ds.columns}
        return ds.distinct(column)

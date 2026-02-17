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
    """Simple whitespace + punctuation tokenizer with lowercasing."""
    return re.findall(r"\w+", text.lower())


class CsvDataset:
    """A single CSV file loaded into memory with search support."""

    def __init__(self, name: str, path: Path, rows: list[dict], columns: list[str]):
        self.name = name
        self.path = path
        self.rows = rows
        self.columns = columns
        self._bm25: BM25Okapi | None = None
        self._build_index()

    def _build_index(self) -> None:
        """Build a BM25 index over all row text."""
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
        """Full-text BM25 search across all columns. Returns matching rows."""
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

    def filter(self, **criteria: str) -> list[dict]:
        """Filter rows where columns match the given values (case-insensitive)."""
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

    def distinct(self, column: str) -> list[str]:
        """Return sorted distinct values for a column."""
        if column not in self.columns:
            return []
        values = {str(row[column]) for row in self.rows if row.get(column)}
        return sorted(values)

    def count_by(self, column: str, **criteria: str) -> list[dict]:
        """Filter rows by *criteria*, then count occurrences of each
        distinct value in *column*. Returns [{value, count}] sorted
        descending by count."""
        rows = self.filter(**criteria) if criteria else self.rows
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
        self.docs_dir = Path(docs_dir)
        self._datasets: dict[str, CsvDataset] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Recursively find and load all CSV files."""
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

            if "RequestingSystem" in columns:
                for row in rows:
                    if not row.get("RequestingSystem", "").strip():
                        row["RequestingSystem"] = "Agnes"

            self._datasets[name] = CsvDataset(name, csv_path, rows, columns)
            logger.info("Loaded CSV %s: %d rows, columns=%s", name, len(rows), columns)

    @staticmethod
    def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
        """Read a CSV file and return (rows_as_dicts, column_names)."""
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or []
            rows = list(reader)
        return rows, list(columns)

    @property
    def dataset_names(self) -> list[str]:
        return sorted(self._datasets.keys())

    def get_dataset(self, name: str) -> CsvDataset | None:
        return self._datasets.get(name)

    def list_datasets(self) -> dict:
        """Return metadata about all loaded CSV datasets."""
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
        """Search a named dataset by query."""
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        results = ds.search(query, max_results)
        if not results:
            return [{"message": "No matching rows found.", "query": query}]
        return results

    def filter_rows(self, dataset_name: str, **criteria: str) -> list[dict]:
        """Filter rows in a dataset by column values."""
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return [{"error": f"Dataset not found: {dataset_name}",
                     "available": self.dataset_names}]
        return ds.filter(**criteria)

    def count_by_column(
        self, dataset_name: str, column: str, **criteria: str
    ) -> list[dict] | dict:
        """Group-count a column after applying optional filters."""
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in ds.columns:
            return {"error": f"Column not found: {column}",
                    "available_columns": ds.columns}
        return ds.count_by(column, **criteria)

    def get_distinct_values(self, dataset_name: str, column: str) -> list[str] | dict:
        """Get distinct values for a column in a dataset."""
        ds = self._datasets.get(dataset_name)
        if ds is None:
            return {"error": f"Dataset not found: {dataset_name}",
                    "available": self.dataset_names}
        if column not in ds.columns:
            return {"error": f"Column not found: {column}",
                    "available_columns": ds.columns}
        return ds.distinct(column)

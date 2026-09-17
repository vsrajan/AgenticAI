"""Tests for the DuckDB-backed dataset store (csv_store.py).

Each test builds a store over temporary CSV files -- no network, no
sample-data dependency.

Run:
    cd mcp-server
    uv run --with pytest pytest tests/ -q
"""

import os
import threading

import pytest

from mcp_docs_server.csv_store import CsvStore


ENTITLEMENTS_CSV = """\
ResourceID,JOBTITLE,OU,CITY
R001,Finance Analyst,Finance,London
R001,Finance Analyst,Finance,Paris
R002,Senior Finance Analyst,Finance,London
R003,HR Manager,HR,Berlin
R004,,HR,
"""

RESOURCES_CSV = """\
ResourceID,name,DESCRIPTION
R001,SAP Finance Reporting,Read access to SAP FI reports
R002,SAP FI Admin,Full admin on the SAP FI module
R003,HR Portal,Employee self-service portal
"""


@pytest.fixture()
def store(tmp_path):
    """A store over two small CSVs, sweeper disabled."""
    (tmp_path / "Entitlements.csv").write_text(ENTITLEMENTS_CSV)
    (tmp_path / "Resources.csv").write_text(RESOURCES_CSV)
    return CsvStore(tmp_path, db_path=str(tmp_path / "test.duckdb"),
                    refresh_minutes=0)


# -- list_datasets --

def test_list_datasets_shape(store):
    result = store.list_datasets()
    assert result["total_datasets"] == 2
    assert result["datasets"]["Entitlements"]["row_count"] == 5
    assert result["datasets"]["Entitlements"]["columns"] == [
        "ResourceID", "JOBTITLE", "OU", "CITY"]
    assert result["synced_at"]  # freshness is visible


# -- exact filters --

def test_filter_exact_is_case_insensitive(store):
    rows = store.filter_rows("Entitlements", JOBTITLE="finance analyst")
    assert len(rows) == 2
    assert all(r["JOBTITLE"] == "Finance Analyst" for r in rows)


def test_filter_ands_multiple_columns(store):
    rows = store.filter_rows("Entitlements", JOBTITLE="Finance Analyst", CITY="paris")
    assert len(rows) == 1
    assert rows[0]["CITY"] == "Paris"


def test_filter_ignores_unknown_columns(store):
    # matches the previous implementation: unknown filter columns are skipped
    rows = store.filter_rows("Entitlements", NOT_A_COLUMN="x", OU="HR")
    assert len(rows) == 2


def test_filter_truncation_metadata(store):
    rows = store.filter_rows("Entitlements", max_results=2, OU="Finance")
    assert len(rows) == 3  # 2 rows + metadata object
    meta = rows[-1]
    assert meta["_truncated"] is True
    assert meta["_total_matches"] == 3
    assert meta["_returned"] == 2


def test_filter_unknown_dataset(store):
    rows = store.filter_rows("Nope", OU="HR")
    assert rows[0]["error"].startswith("Dataset not found")
    assert "Entitlements" in rows[0]["available"]


# -- fuzzy filters --

def test_filter_fuzzy_regex(store):
    rows = store.filter_rows_fuzzy("Entitlements", JOBTITLE="senior.*analyst")
    assert len(rows) == 1
    assert rows[0]["ResourceID"] == "R002"


def test_filter_fuzzy_alternation(store):
    rows = store.filter_rows_fuzzy("Entitlements", OU="finance|hr")
    assert len(rows) == 5


def test_filter_fuzzy_invalid_regex_falls_back_to_literal(store):
    # "(" alone is invalid regex; the fallback treats it as literal text
    rows = store.filter_rows_fuzzy("Entitlements", JOBTITLE="(")
    assert rows == []


# -- count_by_column --

def test_count_by_column_orders_descending(store):
    counts = store.count_by_column("Entitlements", "JOBTITLE", OU="Finance")
    assert counts[0] == {"value": "Finance Analyst", "count": 2}
    assert counts[1] == {"value": "Senior Finance Analyst", "count": 1}


def test_count_by_column_fuzzy_filters(store):
    counts = store.count_by_column("Entitlements", "ResourceID",
                                   fuzzy=True, JOBTITLE="analyst")
    assert {c["value"] for c in counts} == {"R001", "R002"}


def test_count_by_column_skips_empty_values(store):
    counts = store.count_by_column("Entitlements", "JOBTITLE")
    assert all(c["value"] for c in counts)  # the empty JOBTITLE row is absent


def test_count_by_column_errors(store):
    assert "error" in store.count_by_column("Nope", "JOBTITLE")
    result = store.count_by_column("Entitlements", "NOPE")
    assert "error" in result and "available_columns" in result


# -- distinct values --

def test_distinct_sorted_and_non_empty(store):
    values = store.get_distinct_values("Entitlements", "OU")
    assert values == ["Finance", "HR"]


# -- search (size-gated BM25) --

def test_search_returns_scored_rows(store):
    results = store.search("Resources", "SAP finance reporting")
    assert results[0]["ResourceID"] == "R001"
    assert results[0]["_score"] > 0


def test_search_no_match_message(store):
    results = store.search("Resources", "zzzzz")
    assert results[0]["message"] == "No matching rows found."


def test_search_gated_on_large_datasets(tmp_path):
    (tmp_path / "Entitlements.csv").write_text(ENTITLEMENTS_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0, search_max_rows=2)  # gate below 5 rows
    results = store.search("Entitlements", "finance")
    assert "too large for free-text search" in results[0]["error"]
    assert "count_by_column" in results[0]["hint"]
    # filters still work on the gated dataset
    assert len(store.filter_rows("Entitlements", OU="HR")) == 2


# -- refresh --

def test_refresh_detects_changed_source(tmp_path):
    csv_path = tmp_path / "Entitlements.csv"
    csv_path.write_text(ENTITLEMENTS_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0)
    assert store.refresh_if_stale() is False  # unchanged source

    csv_path.write_text(ENTITLEMENTS_CSV + "R009,New Role,IT,Oslo\n")
    os.utime(csv_path)  # ensure the mtime moves
    assert store.refresh_if_stale() is True
    rows = store.filter_rows("Entitlements", ResourceID="R009")
    assert len(rows) == 1


def test_warm_start_skips_reingest(tmp_path):
    (tmp_path / "Resources.csv").write_text(RESOURCES_CSV)
    db = str(tmp_path / "t.duckdb")
    first = CsvStore(tmp_path, db_path=db, refresh_minutes=0)
    synced = first.list_datasets()["synced_at"]
    first._con.close()

    second = CsvStore(tmp_path, db_path=db, refresh_minutes=0)
    # same fingerprint -> no re-ingest -> same synced_at timestamp
    assert second.list_datasets()["synced_at"] == synced
    assert second.list_datasets()["total_datasets"] == 1


def test_removed_csv_drops_dataset(tmp_path):
    (tmp_path / "Entitlements.csv").write_text(ENTITLEMENTS_CSV)
    (tmp_path / "Resources.csv").write_text(RESOURCES_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0)
    (tmp_path / "Resources.csv").unlink()
    assert store.refresh_if_stale() is True
    assert store.dataset_names == ["Entitlements"]


def test_empty_csv_is_skipped(tmp_path):
    (tmp_path / "Empty.csv").write_text("a,b,c\n")
    (tmp_path / "Resources.csv").write_text(RESOURCES_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0)
    assert store.dataset_names == ["Resources"]


def test_concurrent_reads_during_refresh(tmp_path):
    """Readers on other threads must never error while a reload runs."""
    csv_path = tmp_path / "Entitlements.csv"
    csv_path.write_text(ENTITLEMENTS_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0)

    errors: list[Exception] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                store.count_by_column("Entitlements", "OU")
                store.filter_rows("Entitlements", OU="Finance")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for i in range(3):
        csv_path.write_text(ENTITLEMENTS_CSV + f"R10{i},Role,IT,Oslo\n")
        os.utime(csv_path)
        store.refresh_if_stale()
    stop.set()
    for t in threads:
        t.join()
    assert errors == []

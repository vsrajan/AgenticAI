"""Tests for the DuckDB-backed dataset store (csv_store.py).

Each test builds a store over temporary CSV files -- no network, no
sample-data dependency.

Run:
    cd mcp-server
    uv run --with pytest pytest tests/ -q
"""

import os
import threading
from pathlib import Path

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


def test_filter_unknown_column_is_an_error(store):
    # fail closed. skipping the unknown column used to drop the filter
    # silently and return EVERY row -- one person's access presented as
    # another's, with no signal that anything went wrong
    rows = store.filter_rows("Entitlements", NOT_A_COLUMN="x", OU="HR")
    assert len(rows) == 1
    assert "NOT_A_COLUMN" in rows[0]["error"]
    assert rows[0]["available_columns"] == ["ResourceID", "JOBTITLE", "OU", "CITY"]
    assert "case-sensitive" in rows[0]["hint"]


def test_filter_unknown_column_error_is_list_wrapped(store):
    # filter_dataset is annotated -> list[dict] and FastMCP validates
    # tool output against that annotation: a bare dict here would be a
    # protocol-level ToolError that kills the turn, instead of a message
    # the agent can read and correct
    rows = store.filter_rows("Entitlements", NOPE="x")
    assert isinstance(rows, list) and isinstance(rows[0], dict)


def test_filter_fuzzy_unknown_column_is_an_error(store):
    rows = store.filter_rows_fuzzy("Entitlements", JobTitle="analyst")
    assert len(rows) == 1
    assert "JobTitle" in rows[0]["error"]  # right name, wrong case


def test_filter_unknown_column_wins_over_the_regex_fallback(store):
    # the invalid-regex retry must not re-enter the query with an
    # unvalidated column name
    rows = store.filter_rows_fuzzy("Entitlements", NOT_A_COLUMN="(")
    assert "NOT_A_COLUMN" in rows[0]["error"]


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


def test_count_by_column_unknown_filter_column_is_an_error(store):
    # count_by_column shares _where, so it had the same silent skip.
    # this is the peer-recommendation call: a dropped filter here
    # counts the whole company as the user's peer group
    result = store.count_by_column("Entitlements", "ResourceID", OU_TYPO="Finance")
    assert "OU_TYPO" in result["error"]
    assert "available_columns" in result


def test_count_by_column_filter_error_is_a_bare_dict(store):
    # count_by_column is annotated -> list[dict] | dict, and its other
    # errors are bare dicts; the filter error must match that shape
    result = store.count_by_column("Entitlements", "ResourceID", NOPE="x")
    assert isinstance(result, dict) and "error" in result


def test_count_by_column_fuzzy_unknown_filter_column_is_an_error(store):
    result = store.count_by_column("Entitlements", "ResourceID",
                                   fuzzy=True, jobtitle="analyst")
    assert "jobtitle" in result["error"]


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


def test_failed_index_rebuild_is_retried_on_the_next_sweep(tmp_path):
    """A rebuild that fails after the commit must not look 'current'.

    The fingerprint is stored inside the load transaction, so a failure
    in the in-memory rebuild that follows would otherwise leave the data
    committed, the fingerprint matching, and the schema and search index
    stale until the source happened to change again.
    """
    csv_path = tmp_path / "Entitlements.csv"
    csv_path.write_text(ENTITLEMENTS_CSV)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "t.duckdb"),
                     refresh_minutes=0)

    csv_path.write_text(ENTITLEMENTS_CSV + "R009,New Role,IT,Oslo\n")
    os.utime(csv_path)

    real = store._build_search_indexes
    calls = {"n": 0}

    def failing_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()

    store._build_search_indexes = failing_once
    with pytest.raises(RuntimeError):
        store.refresh_if_stale()

    # the next sweep must actually redo the work, not skip it
    assert store.refresh_if_stale() is True
    assert len(store.filter_rows("Entitlements", ResourceID="R009")) == 1


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
                # list_datasets is the path that used to run on the
                # SHARED connection -- it is the reason this test exists
                store.list_datasets()
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


# -- thread safety: every handle is private to one caller --
#
# Sharing a duckdb handle across threads fails SILENTLY far more often
# than it raises: threads get each other's result rows back, with
# plausible row counts and no exception. Asserting "no exception" is
# therefore not enough -- these tests assert OWNERSHIP of every row
# returned, which is the property that actually matters here.

OWNERSHIP_CSV_HEADER = "EMPLOYEEID,ResourceID,OU\n"
OWNERSHIP_ROWS = "".join(
    f"E{emp:03d},R{emp:03d}-{n},Finance\n"
    for emp in range(1, 21) for n in range(5)
)


def test_concurrent_filters_never_return_another_callers_rows(tmp_path):
    """Different filters on different threads, with refreshes running.

    The regression this guards: with a shared handle, roughly a quarter
    of these calls came back holding a DIFFERENT employee's rows -- full
    result sets, correct row counts, no exception raised.
    """
    csv_path = tmp_path / "Entitlements.csv"
    csv_path.write_text(OWNERSHIP_CSV_HEADER + OWNERSHIP_ROWS)
    store = CsvStore(tmp_path, db_path=str(tmp_path / "own.duckdb"),
                     refresh_minutes=0)

    failures: list[str] = []
    stop = threading.Event()

    def refresher():
        i = 0
        while not stop.is_set():
            # only ever touches employee 9999, so every assertion below
            # stays stable while the data underneath is rebuilt
            csv_path.write_text(
                OWNERSHIP_CSV_HEADER + OWNERSHIP_ROWS + f"E9999,RX-{i},IT\n")
            os.utime(csv_path)
            try:
                store.refresh_if_stale()
            except Exception as exc:  # pragma: no cover - failure path
                failures.append(f"refresh raised {exc!r}")
                return
            i += 1

    def reader(employee: str):
        for _ in range(150):
            if failures:
                return
            try:
                rows = store.filter_rows(
                    "Entitlements", EMPLOYEEID=employee,
                    columns=["EMPLOYEEID", "ResourceID"])
                owners = {r.get("EMPLOYEEID") for r in rows}
                if owners != {employee}:
                    failures.append(
                        f"{employee} got rows owned by {sorted(owners)}")
                    return
                if len(rows) != 5:
                    failures.append(f"{employee} got {len(rows)} rows, want 5")
                    return

                counts = store.count_by_column(
                    "Entitlements", "ResourceID", EMPLOYEEID=employee)
                if not isinstance(counts, list) or len(counts) != 5:
                    failures.append(f"{employee} count shape {counts!r}")
                    return

                meta = store.list_datasets()
                if "Entitlements" not in meta.get("datasets", {}):
                    failures.append(f"list_datasets lost the dataset: {meta!r}")
                    return
            except Exception as exc:  # pragma: no cover - failure path
                failures.append(f"{employee} raised {exc!r}")
                return

    sweeper = threading.Thread(target=refresher)
    sweeper.start()
    readers = [
        threading.Thread(target=reader, args=(f"E{emp:03d}",))
        for emp in (1, 7, 13, 19)
    ]
    for t in readers:
        t.start()
    for t in readers:
        t.join()
    stop.set()
    sweeper.join()

    assert failures == []


def test_shared_connection_is_only_a_cursor_factory(tmp_path):
    """No query may run on self._con after construction.

    A structural guard: the silent-corruption failure mode is invisible
    in a passing functional test, so this pins the invariant in the
    source itself.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "src" / "mcp_docs_server" / "csv_store.py"
    ).read_text()
    body = source.split("def _cursor", 1)[1]
    assert "self._con.execute" not in body, (
        "queries must run on a per-call cursor, not the shared connection"
    )


# -- projection and multi-column grouping (denormalized Entitlements) --
#
# A separate fixture on purpose: the shared `store` above deliberately
# has no ResourceName, so the tests that rely on it keep covering the
# pre-denormalization shape (and the agent's fallback path).

ENRICHED_CSV = """\
EmployeeID,ResourceID,ResourceName,RequestingSystem,JOBTITLE,AREANAME
1234,R001,Trade Blotter Read,Murex,Senior Software Engineer,Markets
1234,R002,GL Posting,SAP,Senior Software Engineer,Markets
9999,R001,Trade Blotter Read,Murex,Risk Analyst,Risk
8888,R001,Trade Blotter Read,Murex,Senior Software Engineer,Markets
"""


@pytest.fixture()
def enriched(tmp_path):
    """Entitlements carrying ResourceName / RequestingSystem inline."""
    (tmp_path / "Entitlements.csv").write_text(ENRICHED_CSV)
    return CsvStore(tmp_path, db_path=str(tmp_path / "enriched.duckdb"),
                    refresh_minutes=0)


def test_projection_returns_only_requested_columns(enriched):
    rows = enriched.filter_rows("Entitlements", EmployeeID="1234",
                                columns=["ResourceID", "ResourceName"])
    assert len(rows) == 2
    assert all(set(r) == {"ResourceID", "ResourceName"} for r in rows)
    assert {r["ResourceName"] for r in rows} == {"Trade Blotter Read", "GL Posting"}


def test_projection_omitted_returns_every_column(enriched):
    rows = enriched.filter_rows("Entitlements", EmployeeID="1234")
    assert len(rows[0]) == 6  # unchanged SELECT * behavior


def test_projection_unknown_column_is_an_error(enriched):
    # same rule filters follow: an unknown column fails closed rather
    # than silently changing what the caller asked for
    rows = enriched.filter_rows("Entitlements", EmployeeID="1234",
                                columns=["ResourceID", "Nope"])
    assert "Nope" in rows[0]["error"]
    assert "ResourceID" in rows[0]["available_columns"]


def test_projection_works_with_fuzzy_filters(enriched):
    rows = enriched.filter_rows_fuzzy("Entitlements", JOBTITLE="engineer",
                                      columns=["ResourceName"])
    assert len(rows) == 3
    assert all(set(r) == {"ResourceName"} for r in rows)


def test_count_by_multiple_columns_pairs_id_with_name(enriched):
    # the whole point: id + label + count in ONE call
    rows = enriched.count_by_column("Entitlements",
                                    ["ResourceID", "ResourceName"],
                                    JOBTITLE="Senior Software Engineer")
    assert rows == [
        {"ResourceID": "R001", "ResourceName": "Trade Blotter Read", "count": 2},
        {"ResourceID": "R002", "ResourceName": "GL Posting", "count": 1},
    ]


def test_count_by_single_column_shape_unchanged(enriched):
    rows = enriched.count_by_column("Entitlements", "ResourceID")
    assert rows == [{"value": "R001", "count": 3}, {"value": "R002", "count": 1}]


def test_count_by_multiple_columns_unknown_is_an_error(enriched):
    result = enriched.count_by_column("Entitlements", ["ResourceID", "Bad"])
    assert "Bad" in result["error"]


def test_count_by_multiple_columns_honours_fuzzy_filters(enriched):
    rows = enriched.count_by_column("Entitlements",
                                    ["ResourceID", "ResourceName"],
                                    fuzzy=True, JOBTITLE="engineer|analyst")
    counts = {r["ResourceID"]: r["count"] for r in rows}
    assert counts == {"R001": 3, "R002": 1}

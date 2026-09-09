"""Tests for ParquetDataSource (data_sources.py).

Parquet part files are generated with DuckDB itself in tmp dirs --
mirroring the Spark output layout (a folder of part-*.parquet files
plus _SUCCESS / _committed_* markers) with nothing committed to the
repo and no Azure needed.

Run:
    cd mcp-server
    uv run --with pytest pytest tests/test_parquet_source.py -q
"""

import time

import duckdb
import pytest

from mcp_docs_server.csv_store import CsvStore
from mcp_docs_server.data_sources import (
    ParquetDataSource,
    build_data_source,
    parse_parquet_sources,
)


def write_parts(folder, rows, parts=2, start_id=0):
    """Write a Spark-style dataset folder: N part files + markers.

    Columns are deliberately TYPED (BIGINT id, DATE granted) to prove
    the VARCHAR cast on ingest.
    """
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    per_part = rows // parts
    for p in range(parts):
        lo = start_id + p * per_part
        hi = lo + per_part
        con.execute(
            f"COPY (SELECT i::BIGINT AS UserID, 'R-'||(i%7)::VARCHAR AS ResourceID, "
            f"('2026-01-0'||(i%9+1))::DATE AS Granted, "
            f"CASE WHEN i%2=0 THEN 'Finance' ELSE 'HR' END AS OU "
            f"FROM range({lo}, {hi}) t(i)) "
            f"TO '{folder}/part-{p:05d}-tid-abc{p}.parquet' (FORMAT parquet)"
        )
    con.close()
    # spark job markers -- must never be read as data
    (folder / "_SUCCESS").write_text("")
    (folder / "_committed_1234567890").write_text("{}")
    (folder / "_started_1234567890").write_text("")


@pytest.fixture()
def datasets(tmp_path):
    write_parts(tmp_path / "Resource.parquet", rows=20, parts=2)
    write_parts(tmp_path / "entitlement.parquet", rows=40, parts=4)
    return {
        "Resources": str(tmp_path / "Resource.parquet" / "*.parquet"),
        "Entitlements": str(tmp_path / "entitlement.parquet"),  # dir form
    }


def load_into(source):
    con = duckdb.connect()
    names = source.load(con)
    return con, names


# -- parsing --

def test_parse_parquet_sources():
    parsed = parse_parquet_sources(
        "Resources=/d/Resource.parquet/*.parquet, Entitlements=/d/ent"
    )
    assert parsed == {
        "Resources": "/d/Resource.parquet/*.parquet",
        "Entitlements": "/d/ent",
    }


@pytest.mark.parametrize("bad", ["NoEquals", "=nopath", "name="])
def test_parse_rejects_malformed_entries(bad):
    with pytest.raises(ValueError, match="Malformed"):
        parse_parquet_sources(bad)


def test_empty_sources_rejected():
    with pytest.raises(ValueError):
        ParquetDataSource({})


# -- loading --

def test_multipart_folders_load_as_one_table(datasets):
    con, names = load_into(ParquetDataSource(datasets))
    assert sorted(names) == ["Entitlements", "Resources"]
    assert con.execute('SELECT count(*) FROM "Resources"').fetchone()[0] == 20
    assert con.execute('SELECT count(*) FROM "Entitlements"').fetchone()[0] == 40


def test_directory_value_expands_to_parquet_glob(datasets):
    # the Entitlements entry is a bare directory; markers must be skipped
    con, _ = load_into(ParquetDataSource(datasets))
    assert con.execute('SELECT count(*) FROM "Entitlements"').fetchone()[0] == 40


def test_every_column_ingests_as_varchar(datasets):
    con, _ = load_into(ParquetDataSource(datasets))
    types = {row[0]: row[1] for row in con.execute('DESCRIBE "Resources"').fetchall()}
    assert set(types) == {"UserID", "ResourceID", "Granted", "OU"}
    assert all(t == "VARCHAR" for t in types.values()), types


def test_empty_glob_is_skipped_with_no_table(tmp_path, datasets):
    datasets["Ghost"] = str(tmp_path / "nothing" / "*.parquet")
    con, names = load_into(ParquetDataSource(datasets))
    assert "Ghost" not in names
    assert sorted(names) == ["Entitlements", "Resources"]


# -- fingerprints --

def test_fingerprint_stable_when_nothing_changes(datasets):
    source = ParquetDataSource(datasets)
    assert source.fingerprint() == source.fingerprint()


def test_fingerprint_changes_when_a_part_is_rewritten(tmp_path, datasets):
    source = ParquetDataSource(datasets)
    before = source.fingerprint()
    time.sleep(0.01)  # ensure a distinct mtime
    write_parts(tmp_path / "Resource.parquet", rows=20, parts=2, start_id=100)
    assert source.fingerprint() != before


def test_fingerprint_changes_when_files_appear(tmp_path):
    target = tmp_path / "late.parquet"
    source = ParquetDataSource({"Late": str(target / "*.parquet")})
    before = source.fingerprint()  # matches nothing -> "missing" marker
    write_parts(target, rows=10, parts=1)
    assert source.fingerprint() != before


# -- end to end through the store --

def test_store_serves_parquet_datasets(tmp_path, datasets):
    store = CsvStore(tmp_path / "docs", db_path=str(tmp_path / "t.duckdb"),
                     source=ParquetDataSource(datasets), refresh_minutes=0)
    listed = store.list_datasets()
    assert set(listed["datasets"]) == {"Resources", "Entitlements"}
    assert listed["datasets"]["Resources"]["row_count"] == 20
    # filters behave string-wise on originally-typed columns
    rows = store.filter_rows("Resources", OU="finance")  # case-insensitive
    assert rows and all(r["OU"] == "Finance" for r in rows if "_truncated" not in r)
    counts = store.count_by_column("Resources", "OU")
    assert {c["value"] for c in counts} == {"Finance", "HR"}
    # a numeric-typed column is queryable as a string
    assert store.filter_rows("Resources", UserID="3")


def test_store_refresh_cycle_on_restaged_parquet(tmp_path, datasets):
    store = CsvStore(tmp_path / "docs", db_path=str(tmp_path / "t.duckdb"),
                     source=ParquetDataSource(datasets), refresh_minutes=0)
    assert store.list_datasets()["datasets"]["Resources"]["row_count"] == 20
    time.sleep(0.01)
    write_parts(tmp_path / "Resource.parquet", rows=30, parts=3)
    assert store.refresh_if_stale() is True
    assert store.list_datasets()["datasets"]["Resources"]["row_count"] == 30
    # and no change -> no reload
    assert store.refresh_if_stale() is False


# -- env-driven selection --

def test_build_data_source_defaults_to_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_DATA_SOURCE", raising=False)
    source = build_data_source(tmp_path)
    assert type(source).__name__ == "CsvDataSource"


def test_build_data_source_parquet_requires_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DATA_SOURCE", "parquet")
    monkeypatch.delenv("MCP_PARQUET_SOURCES", raising=False)
    with pytest.raises(ValueError, match="MCP_PARQUET_SOURCES"):
        build_data_source(tmp_path)


def test_build_data_source_parquet(tmp_path, monkeypatch, datasets):
    monkeypatch.setenv("MCP_DATA_SOURCE", "parquet")
    monkeypatch.setenv(
        "MCP_PARQUET_SOURCES",
        f"Resources={datasets['Resources']},Entitlements={datasets['Entitlements']}",
    )
    source = build_data_source(tmp_path)
    con, names = load_into(source)
    assert sorted(names) == ["Entitlements", "Resources"]


def test_build_data_source_rejects_unknown_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DATA_SOURCE", "carrier-pigeon")
    with pytest.raises(ValueError, match="csv.*parquet|parquet.*csv"):
        build_data_source(tmp_path)


def test_enriched_columns_survive_the_varchar_cast(tmp_path):
    """Denormalized ResourceName / RequestingSystem load and project.

    The Spark export carries the resource label inline on every
    assignment row so the agent never has to look it up (docs/P0.md
    section 11.10). They are ordinary string columns, but this pins
    that the COLUMNS(*)::VARCHAR ingest keeps them intact and that
    projection + multi-column grouping work over parquet, not just CSV.
    """
    folder = tmp_path / "entitlement.parquet"
    folder.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        "COPY (SELECT (1000+i)::BIGINT AS EmployeeID, "
        "'R-'||(i%3)::VARCHAR AS ResourceID, "
        "'Resource '||(i%3)::VARCHAR AS ResourceName, "
        "CASE WHEN i%3=0 THEN 'Murex' ELSE 'SAP' END AS RequestingSystem "
        f"FROM range(9) t(i)) TO '{folder}/part-00000-tid-x.parquet' (FORMAT parquet)"
    )
    con.close()
    (folder / "_SUCCESS").write_text("")

    source = ParquetDataSource({"Entitlements": str(folder)})
    store = CsvStore(tmp_path, db_path=str(tmp_path / "p.duckdb"),
                     refresh_minutes=0, source=source)

    cols = store.list_datasets()["datasets"]["Entitlements"]["columns"]
    assert "ResourceName" in cols and "RequestingSystem" in cols

    # one call gives the person's access with its label -- no second lookup
    rows = store.filter_rows("Entitlements", EmployeeID="1000",
                             columns=["ResourceID", "ResourceName", "RequestingSystem"])
    assert rows == [{"ResourceID": "R-0", "ResourceName": "Resource 0",
                     "RequestingSystem": "Murex"}]

    grouped = store.count_by_column("Entitlements", ["ResourceID", "ResourceName"])
    assert grouped[0]["count"] == 3
    assert grouped[0]["ResourceName"].startswith("Resource ")

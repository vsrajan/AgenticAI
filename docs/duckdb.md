# Parquet, DuckDB, and MCP tools, from the ground up

The fourth volume of the tutorial set ([async.md](async.md),
[langgraph.md](langgraph.md), [bm25.md](bm25.md)), covering the MCP
server's DATA side: how parquet exports become queryable tables in an
embedded DuckDB database, and how those tables are exposed to the
agent as MCP tools. Part 1 builds the concepts with standalone
samples; part 2 walks `data_sources.py`, `csv_store.py`, and
`server.py`.

Running the samples: save each as a .py file inside `mcp-server/` and
run `uv run python sample_x.py` -- duckdb and the mcp SDK are already
project dependencies. Nothing external is needed; the samples
generate their own data.

## Contents

Part 1 -- the concepts
1. [DuckDB: a database that is just a library](#1-duckdb-a-database-that-is-just-a-library)
2. [Querying files in place: parquet](#2-querying-files-in-place-parquet)
3. [The Spark export layout: folders of part files](#3-the-spark-export-layout-folders-of-part-files)
4. [Refresh: atomic swaps and fingerprints](#4-refresh-atomic-swaps-and-fingerprints)
5. [SQL safety: parameters for values, validation for identifiers](#5-sql-safety-parameters-for-values-validation-for-identifiers)
6. [MCP tools: a function, a schema, and a docstring](#6-mcp-tools-a-function-a-schema-and-a-docstring)

Part 2 -- the concepts in the MCP server
7. [The map](#7-the-map)
8. [Ingestion: data_sources.py](#8-ingestion-data_sourcespy)
9. [Serving: csv_store.py](#9-serving-csv_storepy)
10. [Exposure: server.py and the tool contract](#10-exposure-serverpy-and-the-tool-contract)
11. [The scale story](#11-the-scale-story)

---

## 1. DuckDB: a database that is just a library

Most databases are SERVERS: a separate process you connect to over a
socket (Postgres, Redis). DuckDB -- like SQLite -- is a LIBRARY: the
whole database engine lives inside your Python process, and a
"database" is either process memory or a single ordinary file. No
installation, no daemon, no port, no credentials.

The difference from SQLite is the workload: SQLite is built for
transactional row-at-a-time work; DuckDB is built for ANALYTICS --
scans, filters, aggregations over millions of rows -- and stores data
by COLUMN, so a query touching two columns of a 5M-row table reads
only those two columns, vectorized, not 5M whole rows.

```python
# sample_1_duckdb.py -- in-memory, then persisted to a file
import duckdb

con = duckdb.connect()                     # in-memory database
con.execute("CREATE TABLE users AS "
            "SELECT 'user'||i AS name, i % 3 AS dept FROM range(9) t(i)")
print(con.execute("SELECT dept, count(*) FROM users GROUP BY dept").fetchall())
con.close()

con = duckdb.connect("demo.duckdb")        # file-backed: same API
con.execute("CREATE OR REPLACE TABLE users AS SELECT 42 AS answer")
con.close()

con = duckdb.connect("demo.duckdb")        # reopen -- the table survived
print(con.execute("SELECT * FROM users").fetchall())
con.close()

import os; os.unlink("demo.duckdb")        # cleanup
```

One argument decides memory vs disk. The MCP server uses the
file-backed form (`MCP_DB_PATH`) so a restart with unchanged data
starts warm -- measured at 0.04s vs a 10.2s cold ingest.

## 2. Querying files in place: parquet

DuckDB can treat data FILES as tables directly -- no import step.
Parquet is the format that makes this shine: like DuckDB itself it is
COLUMNAR, it carries its schema (column names AND types), and it is
compressed. Columnar file into columnar engine is the fastest ingest
path there is.

```python
# sample_2_files.py -- write a parquet, query it without loading it
import duckdb

con = duckdb.connect()
con.execute("COPY (SELECT 'R-'||i AS ResourceID, "
            "CASE WHEN i%2=0 THEN 'Finance' ELSE 'HR' END AS OU "
            "FROM range(1000) t(i)) TO 'demo.parquet' (FORMAT parquet)")

# query the FILE -- read_parquet makes it a table expression
print(con.execute(
    "SELECT OU, count(*) FROM read_parquet('demo.parquet') GROUP BY OU"
).fetchall())

# schema travels inside the file
print(con.execute("DESCRIBE SELECT * FROM read_parquet('demo.parquet')").fetchall())
con.close()

import os; os.unlink("demo.parquet")
```

(The same works for CSVs via `read_csv_auto`, which must SNIFF types
from text -- one reason parquet ingest beats CSV ingest: nothing to
guess.) The server's ingestion is essentially one statement:
`CREATE OR REPLACE TABLE x AS SELECT ... FROM read_parquet(...)` --
file to table, one hop.

## 3. The Spark export layout: folders of part files

Production parquet rarely arrives as one tidy file. A Spark or
Databricks job writes each dataset as a DIRECTORY: several
`part-*.parquet` files (one per writer task) plus commit-protocol
markers (`_SUCCESS`, `_committed_*`) that carry no data. Two things
make this painless:

```python
# sample_3_parts.py -- multi-part dataset, globs, and the VARCHAR cast
import duckdb, pathlib

folder = pathlib.Path("Resource.parquet"); folder.mkdir(exist_ok=True)
con = duckdb.connect()
for p in range(2):                                   # two part files
    con.execute(f"COPY (SELECT i::BIGINT AS UserID, 'R-'||(i%5) AS ResourceID "
                f"FROM range({p*50},{p*50+50}) t(i)) "
                f"TO '{folder}/part-{p:05d}.parquet' (FORMAT parquet)")
(folder / "_SUCCESS").write_text("")                 # marker, not data

# a GLOB reads every part as ONE table; *.parquet skips the markers
print("rows:", con.execute(
    f"SELECT count(*) FROM read_parquet('{folder}/*.parquet')").fetchone()[0])

# parquet columns are TYPED (UserID is BIGINT)...
print(con.execute(
    f"DESCRIBE SELECT * FROM read_parquet('{folder}/*.parquet')").fetchall())

# ...but the tool contract wants strings: cast EVERY column in one move
con.execute(f"CREATE TABLE r AS SELECT COLUMNS(*)::VARCHAR "
            f"FROM read_parquet('{folder}/*.parquet')")
print(con.execute("DESCRIBE r").fetchall())          # all VARCHAR now
con.close()

import shutil; shutil.rmtree(folder)
```

The `COLUMNS(*)::VARCHAR` cast deserves a sentence: the MCP tool SQL
does case-insensitive string matching (`lower(col) = lower(?)`) and
regex filtering -- operations that must behave identically whether
the data came from a CSV (all strings by nature) or a typed parquet.
Casting at ingest keeps the tool contract byte-identical regardless
of the source (P0.md section 11.9 calls this "the one correctness
decision").

## 4. Refresh: atomic swaps and fingerprints

Source data changes (a re-staged export). Two problems: how to reload
WITHOUT readers ever seeing half-loaded data, and how to know whether
a reload is needed at all without re-reading everything.

```python
# sample_4_refresh.py -- CREATE OR REPLACE + a fingerprint gate
import duckdb, hashlib, os, time

def write_source(n):
    con = duckdb.connect()
    con.execute(f"COPY (SELECT i AS id FROM range({n}) t(i)) "
                f"TO 'src.parquet' (FORMAT parquet)")
    con.close()

def fingerprint(path):                       # cheap change detector:
    st = os.stat(path)                       # mtime+size, never content
    return hashlib.sha256(f"{path}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()

def refresh_if_stale(con, seen):
    fp = fingerprint("src.parquet")
    if fp == seen:
        print("  fingerprint unchanged -> skip (warm start)")
        return seen
    con.execute("BEGIN")
    # readers see the OLD table until COMMIT -- never a mixture
    con.execute("CREATE OR REPLACE TABLE data AS "
                "SELECT * FROM read_parquet('src.parquet')")
    con.execute("COMMIT")
    print("  reloaded:", con.execute("SELECT count(*) FROM data").fetchone()[0], "rows")
    return fp

con = duckdb.connect()
write_source(100)
seen = refresh_if_stale(con, None)           # first load
seen = refresh_if_stale(con, seen)           # unchanged -> skipped
time.sleep(0.01); write_source(250)          # source re-staged
seen = refresh_if_stale(con, seen)           # detected -> reloaded
con.close(); os.unlink("src.parquet")
```

Both halves matter. `CREATE OR REPLACE TABLE` inside a transaction is
the ATOMIC SWAP: an agent query running mid-refresh reads the
complete old table, then the next query reads the complete new one --
no downtime, no partial state. The FINGERPRINT (a hash of every
source file's path, mtime, and size -- cheap stat calls, never a
content read) is the gate that makes checking for changes nearly
free, so a background sweeper can re-check every few minutes and the
0.04s warm start is possible at all.

## 5. SQL safety: parameters for values, validation for identifiers

Tool arguments come from an LLM, which assembles them from USER text
-- so every tool argument is untrusted input. Splicing it into SQL
with an f-string is the classic injection mistake. The rules:

```python
# sample_5_sql_safety.py
import duckdb

con = duckdb.connect()
con.execute("CREATE TABLE r AS SELECT 'R-'||i AS ResourceID, "
            "CASE WHEN i%2=0 THEN 'Finance' ELSE 'HR' END AS OU FROM range(10) t(i)")

# RULE 1 -- VALUES go in as PARAMETERS (?): the driver keeps them data,
# never SQL, no matter what characters they contain.
value = "finance' OR '1'='1"        # a hostile-looking "OU value"
rows = con.execute("SELECT count(*) FROM r WHERE lower(OU) = lower(?)",
                   [value]).fetchone()[0]
print("parameterized:", rows, "rows  (the injection is just a weird string)")

# RULE 2 -- IDENTIFIERS (table/column names) CANNOT be parameters.
# So: VALIDATE against the real schema first, then quote.
known_columns = {row[0] for row in con.execute("DESCRIBE r").fetchall()}
def safe_column(name):
    if name not in known_columns:                 # allowlist, not escaping
        raise ValueError(f"unknown column {name!r}")
    return '"' + name.replace('"', '""') + '"'   # then quote defensively

print("validated identifier:", safe_column("OU"))
try:
    safe_column("OU; DROP TABLE r")
except ValueError as e:
    print("rejected:", e)
con.close()
```

The two rules together are the server's entire injection posture:
values ride as `?` parameters; table and column names are checked
against the ACTUAL schema (only names that exist can ever reach a
query) and then quoted. Nothing the LLM invents can become SQL.

## 6. MCP tools: a function, a schema, and a docstring

An MCP tool is a Python function that the SDK advertises to clients:
the SIGNATURE (names, types, defaults) becomes a machine-readable
schema the calling LLM fills in, and the DOCSTRING becomes the
tool's instructions -- prose the LLM reads to decide when and how to
call it. The docstring is not decoration; it is the API contract's
human half, and prompt engineering in disguise.

```python
# sample_6_mcp_tool.py -- a queryable table exposed as an MCP tool
import duckdb
from mcp.server.fastmcp import FastMCP

con = duckdb.connect()
con.execute("CREATE TABLE r AS SELECT 'R-'||i AS ResourceID, "
            "CASE WHEN i%2=0 THEN 'Finance' ELSE 'HR' END AS OU FROM range(100) t(i)")

mcp = FastMCP("demo-data-server")

@mcp.tool()
def count_by_ou(ou: str) -> dict:
    """Count resources in one organisational unit (case-insensitive).

    Args:
        ou: The OU name, e.g. "Finance" or "HR".
    """
    n = con.execute("SELECT count(*) FROM r WHERE lower(OU)=lower(?)",
                    [ou]).fetchone()[0]
    if n == 0:
        # an error written FOR THE LLM: name the fix, it will retry
        return {"error": f"No rows for OU {ou!r}. "
                         "Use list_ous to see valid values."}
    return {"ou": ou, "count": n}

@mcp.tool()
def list_ous() -> list[str]:
    """List the organisational units that exist in the data."""
    return [r[0] for r in con.execute("SELECT DISTINCT OU FROM r").fetchall()]

# the functions work as plain Python -- which is also how you test them:
print(count_by_ou("finance"))
print(count_by_ou("Legal"))          # the guidance error
print(list_ous())
print("\nTo serve these over the network: mcp.run(transport='streamable-http')")
```

Note the shape of the failure case: not an exception, a STRUCTURED
message telling the caller what to do instead. The consumer is an
LLM mid-conversation -- it reads `"Use list_ous to see valid
values"`, calls list_ous, and self-corrects within the same turn.
Designing error messages as instructions is the single most
MCP-specific habit in this codebase.

---

## 7. The map

| Concept (sample) | In the server |
|---|---|
| embedded, file-backed DuckDB (1) | `duckdb.connect(db_path)` in CsvStore.__init__ (csv_store.py 86-110), path from `MCP_DB_PATH` |
| read_parquet / read_csv_auto (2) | `CsvDataSource.load` (data_sources.py 100), `ParquetDataSource.load` (data_sources.py ~200) |
| part-file globs + VARCHAR cast (3) | `ParquetDataSource._normalize` (162) and its `COLUMNS(*)::VARCHAR` load |
| atomic swap + fingerprint gate (4) | `refresh_if_stale` (csv_store.py 135), `fingerprint()` in both sources (75, 175), sweeper thread (179) |
| SQL safety rules (5) | `_quote` + schema validation (csv_store.py 56, 237), `?` parameters throughout `_where` |
| tools, docstrings, guidance errors (6) | `@mcp.tool()` definitions (server.py 177+), the size-gate guidance message, `instructions=` (150) |

## 8. Ingestion: data_sources.py

The module's one idea is the SPLIT: ingestion (where rows come from)
is pluggable behind a two-method protocol, and serving never knows
which source fed it.

- `DataSource` (32): `fingerprint() -> str` (sample 4's gate -- must
  be CHEAP, it runs every sweep) and `load(con) -> list[str]` (create
  one table per dataset inside the caller's transaction).
- `CsvDataSource` (60): dev/tests/samples. `read_csv_auto` with
  `all_varchar=true` -- the string-contract decision predates
  parquet.
- `ParquetDataSource` (126): production. Everything from sample 3 in
  hardened form: `_normalize` (162) turns a bare directory into
  `dir/*.parquet` (markers excluded by the suffix); `fingerprint`
  (175) stats every matched part file AND contributes a "missing"
  marker for empty globs, so data APPEARING later still changes the
  hash; `load` casts every column with `COLUMNS(*)::VARCHAR`. It
  also accepts `az://` URLs for a future direct-read mode -- with a
  deliberate error message about the extension if used early.
- `build_data_source`: the env switch (`MCP_DATA_SOURCE=csv|parquet`)
  -- and parquet mode WITHOUT `MCP_PARQUET_SOURCES` fails fast,
  because a misconfigured production source must never silently fall
  back to sample CSVs.

## 9. Serving: csv_store.py

The class keeps its historical name (repo convention: internals keep
implementation names); it is the DuckDB serving layer.

- **Connection model** (86-110): ONE connection to the file, plus
  `con.cursor()` per query (268) -- DuckDB's cheap per-thread handle.
  Why threads matter here: FastMCP runs sync tools in a THREADPOOL,
  so concurrent tool calls are real. Note the contrast with the agent
  side: the agent is asyncio (async.md); this server is threads --
  same concurrency problems, different primitives, which is why
  refresh takes a `threading.RLock` (106) rather than an
  asyncio.Lock, and the sweeper (179) is a daemon THREAD, not a task.
- **`refresh_if_stale`** (135): sample 4 verbatim -- compare the
  source fingerprint against the one stored in the `_sync_info`
  table; on change, run `source.load(con)` inside one transaction;
  rebuild the search indexes after.
- **The query shapes** (`_where`, 237): exact filters are
  `lower(col) = lower(?)`; fuzzy filters are
  `regexp_matches(col, ?, 'i')` with a literal-contains fallback for
  invalid regexes; every column name is schema-validated then quoted
  (sample 5's two rules). One elegant trick in `_run_filter` (262):
  query `LIMIT max_results + 1` -- if that extra row comes back, the
  result was truncated, and only THEN pay for a count(*) to report
  the true total. Truncation detection for the price of one row.
- **`count_by_column`** (376): the same WHERE machinery plus
  `GROUP BY` -- the compact `{value, count}` summaries the resource
  prompt steers the agent toward instead of raw rows.
- **The BM25 sidecar** (`_SearchIndex`, 66): free-text search exists
  only for datasets under `MCP_SEARCH_MAX_ROWS` -- the size gate
  whose full story is P0.md section 12, and whose scoring mechanics
  are [bm25.md](bm25.md). Over the gate, `search` (329) returns a
  guidance message steering to filter/count -- sample 6's
  error-as-instruction pattern at dataset scale.

## 10. Exposure: server.py and the tool contract

The last hop is thin by design -- each `@mcp.tool()` function (177+)
validates nothing itself and simply delegates to the store; what it
ADDS is the contract:

- **Docstrings as prompt engineering**: compare `filter_dataset`'s
  and `count_by_column`'s docstrings -- they do not just describe,
  they STEER ("for discovery queries, ALWAYS prefer count_by_column").
  The LLM reads these every turn; they are load-bearing.
- **`instructions=`** (150): the server-level preamble clients
  receive on connect -- the same steering one level up.
- **`_caller()`** (110): the authenticated agent's name from the
  bearer token, in every log line -- the audit half of the MCP auth
  design.
- **`_timed`** (120): a decorator logging `took=ms` per call -- the
  server half of the P0 instrumentation (the agent side logs per-node
  timings; your "tool nodes: 0.9s" measurement is these numbers,
  summed).

End to end, a resource question now reads as one line: parquet parts
staged from Azure Storage -> fingerprint-gated `read_parquet` into a
DuckDB file (all VARCHAR) -> `lower(col)=lower(?)` with a validated
identifier -> a `{value, count}` JSON the LLM was steered toward by a
docstring -- with `caller=agnes took=25ms` in the log.

## 11. The scale story

The measured numbers that justify the design (P0.md section 8, on a
synthetic 5M-row dataset): filtered group-by 25ms median, fuzzy regex
167ms, warm start 0.04s, cold ingest 10.2s, 108MB resident -- against
the pre-P0 baseline of seconds per query and multi-GB memory. The
remaining boundaries are documented where they bite: per-pod
rebuilds on AKS (the DuckDB file is an emptyDir CACHE -- aks.md
section 4), the BM25 sidecar's memory being the reason for the size
gate (P0.md section 12), and DatabaseSource (data_sources.py's stub)
reserving the direct Azure SQL/Postgres pull if the parquet export
pipeline ever goes away.

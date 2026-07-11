# Vector implementation plan -- hybrid semantic + keyword PDF retrieval

Major-release plan for hybrid retrieval on the knowledgebase (PDF) side
of the MCP server. Branch: `Vector`, based on `P1.1` (which is based on
`Performance-P0`). This is a plan only -- nothing here is implemented
yet.

The change: keep the page-level BM25 search from P1.1, ADD an Azure
OpenAI embedding for every page, store the vectors in an embedded
DuckDB database (the same engine P0 introduced for CSV data), and fuse
the two rankings so `search_docs` returns pages that match either the
words OR the meaning of a query. The `search_docs` / `read_page` tool
contract does not change, so the agent, its prompts, and the scanner
keep working unchanged.

Why this matters: BM25 scores shared tokens. It is excellent for exact
terminology (entitlement names, resource IDs, acronyms) and terrible at
vocabulary mismatch -- a user asking "how do I remove access when
someone quits" scores poorly against a page that says "entitlements are
revoked on the leaver's last day", because they share almost no salient
words. Semantic (vector) search embeds query and page into the same
space and matches on meaning, so it catches paraphrase and synonyms.
Neither is strictly better: vectors are weak exactly where BM25 is
strong (rare tokens, precise IDs). Hybrid keeps both strengths.

## Contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Design decisions](#2-design-decisions)
3. [Architecture and data flow](#3-architecture-and-data-flow)
4. [Data model (DuckDB)](#4-data-model-duckdb)
5. [Retrieval and fusion](#5-retrieval-and-fusion)
6. [Work items, file by file](#6-work-items-file-by-file)
7. [Configuration](#7-configuration)
8. [Behavior changes](#8-behavior-changes)
9. [Testing](#9-testing)
10. [Acceptance criteria](#10-acceptance-criteria)
11. [Rollout and rollback](#11-rollout-and-rollback)
12. [Cost and latency](#12-cost-and-latency)
13. [Provider portability](#13-provider-portability)
14. [Future work](#14-future-work)

---

## 1. Goals and non-goals

**Goals**

- Embed every non-empty PDF page with Azure OpenAI and store the
  vectors in an embedded DuckDB database, persisted to disk.
- Hybrid `search_docs`: run page-level BM25 and vector similarity,
  fuse the two rankings with Reciprocal Rank Fusion (RRF), return the
  fused top-N.
- Keep the exact `search_docs` / `read_page` result shape from P1.1
  (`page_path`, `page`, `total_pages`, `score`, `snippet`). No agent or
  prompt change is required for the feature to work.
- Ingestion is fingerprint-gated (reuse the P0 pattern): a page is
  re-embedded only when its source PDF changed or the embedding model
  changed. Embeddings are the one expensive step, so they must not be
  recomputed on every boot.
- Persist page text alongside the vectors. This gives the PDF side the
  on-disk persistence it lacks today (the P1.2 item), so P1.2 is
  effectively absorbed here.
- One-seam embedding provider (`Embedder`), so a future switch to a
  local model is a config change confined to one module -- the same
  portability principle used for `get_llm()` on the agent side.
- A search-mode switch (`MCP_SEARCH_MODE=hybrid|bm25|vector`) so the
  feature can be enabled, disabled, or run pure-vector without code
  changes. Default `hybrid`.

**Non-goals (explicitly out of this release)**

- Local / on-prem embedding models. The `Embedder` seam is built for
  it, but only the Azure provider is implemented. (Decision on record:
  document text already reaches Azure OpenAI via `read_page` results,
  so Azure embeddings widen no trust boundary.)
- Azure AI Search or any managed vector store. DuckDB is the store for
  this release; managed search is a prod-scale future option (see 14).
- Cross-encoder reranking of the fused top-N (future; noted in 14).
- Sub-page chunking. The page stays the unit -- it is the citation unit
  and P1.1 already produces it.
- Any change to the CSV/resource tools, the quality tools, or the
  request tools.
- HNSW approximate-nearest-neighbour indexing. At the current corpus
  size a brute-force cosine scan is sub-millisecond; HNSW is a
  prod-scale future option (see 14).

## 2. Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Retrieval strategy | hybrid (BM25 + vector, RRF-fused) | keeps BM25's exact-term precision AND adds semantic recall; production RAG norm |
| Chunk / citation unit | page (from P1.1) | already produced, already the citation unit; one vector per page is a clean grain |
| Embedding provider | Azure OpenAI `text-embedding-3-large` (3072-dim) | same Azure resource as GPT-4o, no new vendor/secret, near-SOTA retrieval quality; crosses no new trust boundary |
| Vector store engine | embedded DuckDB + `vss` extension | DuckDB is already in the stack (P0); reuses its persistence, fingerprint, atomic-refresh, and sweeper patterns; single file, no new server |
| Store location | a DEDICATED DuckDB file for the PDF index (`MCP_PDF_DB_PATH`), separate from the CSV file (`MCP_DB_PATH`) | two independent `duckdb.connect(file)` handles in one process cannot both hold a write lock on the same file; a dedicated file keeps `DocIndex` and `CsvStore` fully decoupled, each with its own connection and sweeper, mirroring P0's design |
| ANN vs brute force | brute-force `array_cosine_similarity` scan | sub-millisecond over a few thousand page-vectors; avoids DuckDB HNSW's experimental-persistence flag; revisit at prod scale |
| Fusion | Reciprocal Rank Fusion (RRF), `k=60` | rank-based, so it needs no score normalisation between BM25 and cosine (which live on different scales); simple, robust, the standard hybrid fuser |
| Fingerprinting | per-file mtime+size sha256, model id stamped per row | identical to `data_sources.py` CsvDataSource; a model-id change forces a clean re-embed |
| Missing/misconfigured embeddings | FAIL-SAFE: fall back to BM25-only, log a warning | search must never go down because the embedding deployment is misconfigured. This is deliberately the OPPOSITE of the auth layer's fail-closed posture -- retrieval is availability-first, auth is security-first |
| Tool contract | unchanged | fusion happens server-side behind `search_docs`; the agent never learns retrieval got smarter |

## 3. Architecture and data flow

Two independent flows, ingestion and query, both inside the MCP server
(all embedding happens server-side, where the PDFs and the DuckDB file
live -- the agent-client is untouched).

**Ingestion (at boot, and on the background refresh sweep)**

```
for each docs/**/*.pdf:
  fingerprint = sha256(path, mtime, size)          # data_sources pattern
  if pdf_files has (path, fingerprint, model_id):  # unchanged + same model
      skip                                         # no embedding cost
  else:
      pages = _extract_pages(path)                 # P1.1, per-page text
      vectors = embedder.embed(non_empty_pages)    # Azure, batched
      in one transaction:                          # atomic, like P0
          delete old rows for path
          insert (page_path, page_no, text, embedding, model_id, fingerprint)
          upsert pdf_files bookkeeping row
build in-memory BM25 from the page text now in DuckDB   # cheap, every boot
```

**Query (`search_docs`, mode = hybrid)**

```
tokens   = tokenize(query)
bm25_hits   = bm25.top(MCP_BM25_TOPK)               # (path, page) + rank
q_vec       = embedder.embed([query])[0]            # one Azure call
vector_hits = duckdb cosine top(MCP_VECTOR_TOPK)    # (path, page) + rank
fused       = rrf(bm25_hits, vector_hits, k=MCP_RRF_K)
return top max_results as {page_path, page, total_pages, score, snippet}
       # snippet is page-local (P1.1 _make_snippet); score is the RRF score
```

`read_page` is unchanged -- it already serves page ranges from the
extracted text (now sourced from DuckDB instead of a dict, same
`[Page N]` markers).

## 4. Data model (DuckDB)

A dedicated database file (`MCP_PDF_DB_PATH`, default
`<docs_dir>/.mcp_docs.duckdb`). Two tables:

```sql
INSTALL vss; LOAD vss;   -- run once per connection at startup

CREATE TABLE IF NOT EXISTS pdf_pages (
    page_path   VARCHAR,
    page_no     INTEGER,
    text        VARCHAR,
    embedding   FLOAT[3072],       -- dimension = MCP_EMBEDDING_DIM
    model_id    VARCHAR,           -- e.g. "text-embedding-3-large"
    fingerprint VARCHAR,
    PRIMARY KEY (page_path, page_no)
);

CREATE TABLE IF NOT EXISTS pdf_files (
    page_path   VARCHAR PRIMARY KEY,
    fingerprint VARCHAR,
    model_id    VARCHAR,
    total_pages INTEGER,
    synced_at   TIMESTAMP
);
```

Notes:

- `embedding` is a fixed-size `FLOAT[N]` array; `N` is fixed at table
  creation from `MCP_EMBEDDING_DIM`. Changing the dimension is a
  breaking store change -> drop and re-embed (guarded by the model-id /
  dim stamp).
- The `vss` extension supplies `array_cosine_similarity`. No HNSW index
  in this release; the query is a full scan with `ORDER BY sim DESC
  LIMIT k`.
- `pdf_files` is the bookkeeping table the fingerprint gate reads; blank
  pages are excluded from `pdf_pages` (they hold no text) but counted in
  `total_pages`, preserving P1.1's page-numbering behaviour.

## 5. Retrieval and fusion

**Vector query (DuckDB):**

```sql
SELECT page_path, page_no,
       array_cosine_similarity(embedding, $1::FLOAT[3072]) AS sim
FROM pdf_pages
WHERE model_id = $2
ORDER BY sim DESC
LIMIT $3;             -- MCP_VECTOR_TOPK
```

**RRF fusion (python):** each ranker contributes `1 / (k + rank)` for
every page it returns (rank is 1-based within that ranker's list); a
page's fused score is the sum across rankers. `k` (default 60) damps
the weight of low ranks. RRF is used because BM25 scores and cosine
similarities are on different, non-comparable scales -- fusing by RANK
sidesteps score normalisation entirely.

```
fused[key] = sum over rankers r of  1 / (K + rank_r(key))
```

Pages that both rankers surface rise to the top; a page only one ranker
found still competes. The fused list is truncated to `max_results`, and
each surviving page is dressed with its P1.1 page-local snippet.

**Mode switch:** `bm25` returns the BM25 list unchanged (identical to
P1.1); `vector` returns the cosine list only; `hybrid` fuses. If the
embedder is unavailable, `hybrid` and `vector` both degrade to `bm25`
with a logged warning.

## 6. Work items, file by file

### mcp-server

1. **`pyproject.toml`** -- add `openai>=1.40` (Azure OpenAI embeddings
   client). `duckdb` is already a dependency (P0). The `vss` extension
   is loaded at runtime (`INSTALL vss; LOAD vss;`), not a pip package.
   - [ ] done
2. **`src/mcp_docs_server/embeddings.py`** (new) -- the provider seam.
   - `Embedder` protocol: `model_id: str`, `dim: int`,
     `embed(texts: list[str]) -> list[list[float]]`.
   - `AzureOpenAIEmbedder`: wraps `openai.AzureOpenAI`, batches inputs
     (e.g. 128 per request), retries with backoff on rate limits,
     optional Matryoshka `dimensions` reduction.
   - `build_embedder()`: reads `MCP_EMBEDDING_PROVIDER` (`azure`
     default; `local` reserved -> clear NotImplemented error). Returns
     `None` if Azure config is absent, which triggers the BM25 fallback
     rather than a crash.
   - [ ] done
3. **`src/mcp_docs_server/pdf_indexer.py`** (rework) -- `DocIndex` gains
   a DuckDB-backed page+vector store and hybrid search.
   - Keep `_extract_pages`, the topic tree, `_make_snippet`, `read()`,
     and the in-memory BM25 (built from the persisted page text).
   - Add: DuckDB connection to `MCP_PDF_DB_PATH`; `INSTALL/LOAD vss`;
     the schema in section 4; fingerprint-gated ingestion; the cosine
     query; RRF fusion; the `MCP_SEARCH_MODE` switch; the embedder
     fallback.
   - Add a background refresh sweeper for changed PDFs (mirror the P0
     `CsvStore` sweeper; can share `MCP_DATA_REFRESH_MINUTES`).
   - `search()` returns the same keys as P1.1, with `score` now the RRF
     score in hybrid mode.
   - [ ] done
4. **`src/mcp_docs_server/server.py`** -- build the embedder via
   `build_embedder()` and pass it (plus the search mode) into
   `DocIndex`. Update the `search_docs` docstring to say it matches
   meaning and keywords. Startup log: embedding model, search mode,
   indexed page/vector counts. No tool signature changes.
   - [ ] done
5. **`.env.example`** -- add the section 7 variables with comments.
   - [ ] done
6. **`README.md`** -- new "Semantic search (embeddings)" section: what
   hybrid search does, how to create the Azure embedding deployment,
   the env vars, the search modes, the fail-safe fallback, and the
   one-time embedding cost.
   - [ ] done
7. **`tests/test_pdf_vector.py`** (new) -- uses a deterministic
   `FakeEmbedder` (no Azure, no network), so it runs in CI:
   - ingestion writes one vector row per non-empty page;
   - a second `DocIndex` over the same file does ZERO embed calls
     (fingerprint gate) -- assert via a call-counting fake;
   - changing a PDF re-embeds only that file;
   - a model-id change forces a full re-embed;
   - hybrid fusion surfaces a page that matches semantically but shares
     no query tokens (the case BM25 alone misses);
   - `bm25` and `vector` modes return the expected single-signal lists;
   - embedder `None` -> hybrid degrades to BM25, no error.
   - [ ] done
   - The existing `tests/test_pdf_indexer.py` (17) must stay green.
     With no embedder configured in those tests, `DocIndex` runs in the
     BM25 fallback, so the P1.1 search/read assertions hold unchanged.

### agent-client

8. **`src/ease_clients/utils/agnes_agent_graph.py`** (optional) --
   small KNOWLEDGEBASE_PROMPT note that search now understands
   natural-language phrasing, so the agent can search with the user's
   own wording. Behaviour works without this; it is a quality nudge.
   - [ ] done

### docs

9. **`CLAUDE.md`** -- recent-work entry; update the `pdf_indexer.py`
   line (now hybrid) and the `search_docs` description.
   - [ ] done
10. **`docs/PerformanceRecommendations.md`** -- mark the semantic-search
    idea implemented with a pointer here; note P1.2 is absorbed.
    - [ ] done
11. **`docs/vector.md`** -- this plan; check items off as implemented.
    - [ ] done

## 7. Configuration

All read from the single `mcp-server/.env` (same convention as the rest
of the server). New variables:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_SEARCH_MODE` | `hybrid` | `hybrid` \| `bm25` \| `vector` |
| `MCP_EMBEDDING_PROVIDER` | `azure` | `azure` now; `local` reserved |
| `AZURE_OPENAI_ENDPOINT` | -- | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_API_KEY` | -- | key for that resource |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | embeddings API version |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | -- | name of the embedding deployment (created alongside the GPT-4o one) |
| `MCP_EMBEDDING_MODEL_ID` | `text-embedding-3-large` | stamped on every row; a change forces re-embed |
| `MCP_EMBEDDING_DIM` | `3072` | vector dimension; lower it for Matryoshka reduction |
| `MCP_PDF_DB_PATH` | `<docs_dir>/.mcp_docs.duckdb` | dedicated DuckDB file for the PDF index |
| `MCP_VECTOR_TOPK` | `20` | candidate pool from the vector ranker before fusion |
| `MCP_BM25_TOPK` | `20` | candidate pool from the BM25 ranker before fusion |
| `MCP_RRF_K` | `60` | RRF damping constant |
| `MCP_DATA_REFRESH_MINUTES` | `15` | reused from P0; also drives the PDF refresh sweep |

Prerequisite: an embedding deployment in the Azure OpenAI resource
(same key/endpoint as GPT-4o, new deployment name). The `.mcp_docs.duckdb`
file and its `-wal` sidecar are gitignored (the root `.gitignore`
already ignores `*.duckdb` / `*.duckdb.wal`).

## 8. Behavior changes

1. `search_docs` returns semantically relevant pages even with no
   keyword overlap; exact-term and ID queries keep BM25 precision. The
   result shape is unchanged; `score` is the RRF score in hybrid mode.
2. First boot after adding or changing PDFs spends time and Azure
   tokens embedding the changed pages; subsequent boots with unchanged
   content and model do zero embedding (fingerprint gate) and start in
   milliseconds.
3. The PDF index now persists to disk. Boot no longer re-extracts and
   re-embeds unchanged PDFs (the P1.2 win, folded in here).
4. If the embedding deployment is missing or misconfigured, search
   still works in BM25 mode and logs a warning -- it never fails closed.
5. `read_page` is unchanged (same ranges, same `[Page N]` markers).

## 9. Testing

- Unit tests use a deterministic `FakeEmbedder` (maps text -> a fixed
  vector, counts calls). No Azure, no network -> CI-safe. Coverage in
  work item 7.
- The existing PDF suite (17) runs unchanged in the BM25 fallback.
- Both server suites stay green: mcp-server currently 52 (auth 13,
  csv_store 22, pdf_indexer 17) plus the new vector tests; agent-client
  36.
- A manual, env-gated live check (documented, not in CI) against a real
  Azure embedding deployment: confirm a paraphrase query that BM25
  ranks poorly is retrieved in hybrid mode, and confirm the second boot
  makes no embedding calls.

## 10. Acceptance criteria

Targets only -- measured values are recorded here after implementation
(no numbers are quoted as "measured" before they are measured).

| Metric | Target |
|---|---|
| semantic recall | a curated paraphrase query with ~0 keyword overlap retrieves the correct page in `hybrid`, where `bm25` misses it |
| no keyword regression | exact-term / ID queries return the same top page in `hybrid` as in `bm25` |
| fingerprint gate | second boot, unchanged corpus + model -> 0 embedding calls |
| incremental re-embed | changing one PDF re-embeds only that file's pages |
| model-swap safety | changing `MCP_EMBEDDING_MODEL_ID` forces a clean full re-embed |
| tool contract | existing `test_pdf_indexer.py` (17) green unchanged |
| fail-safe | embedder unavailable -> `hybrid` serves BM25 results, logs a warning, no crash |
| suites green | server 52 + new vector tests; agent-client 36 |

## 11. Rollout and rollback

- Ship with `MCP_SEARCH_MODE=bm25` -> behaviour is identical to P1.1
  (safe default; no embedding deployment needed to start).
- Create the Azure embedding deployment, set the env vars, restart:
  the first boot embeds the corpus once (fingerprint-gated thereafter).
- Flip `MCP_SEARCH_MODE=hybrid` to enable semantic search.
- Rollback is a config change: set `MCP_SEARCH_MODE=bm25`. The DuckDB
  vector file can be left in place (ignored) or deleted; deleting it
  only forces a re-embed on the next hybrid boot.

## 12. Cost and latency

- One-time corpus embedding: cents at the current scale (a few thousand
  pages x a few hundred tokens each). Recomputed only for changed files.
- Per query: one embedding call for the query string (~50-150 ms
  network round-trip) plus a sub-millisecond DuckDB scan. This is the
  main latency added versus BM25's ~0 ms; it is paid once per
  `search_docs` call.
- Storage: `MCP_EMBEDDING_DIM` floats per non-empty page (3072 x 4
  bytes ~ 12 KB/page); trivial at this scale. Matryoshka reduction
  (`MCP_EMBEDDING_DIM=1024`) cuts it to a third with minor quality loss
  if it ever matters.

## 13. Provider portability

The embedding provider lives behind one seam (`embeddings.py`,
`build_embedder()`), the retrieval analogue of the agent-side
`get_llm()`. The vector store, the fusion, the tools, and the prompts
never learn which provider produced the numbers. Each row is stamped
with `model_id` and the store's `dim`, so:

- switching to a local model later (`MCP_EMBEDDING_PROVIDER=local`,
  e.g. `bge-large` via `fastembed`) is a new branch in `build_embedder()`
  plus a one-time re-embed -- no change to `DocIndex`, `server.py`, or
  the agent;
- a model or dimension change is detected automatically (stamp
  mismatch) and triggers a clean re-embed rather than silently mixing
  incompatible vectors.

## 14. Future work

- **HNSW index** (`vss` `CREATE INDEX ... USING HNSW`) when the corpus
  outgrows a brute-force scan; needs DuckDB's experimental-persistence
  flag, so defer until measured need.
- **Azure AI Search** as a managed store at prod scale: native hybrid
  plus a semantic reranker, integrated vectorization that calls this
  same embedding deployment. The `Embedder` seam and the tool contract
  make this a store swap behind `DocIndex`, not an agent change.
- **Cross-encoder reranking** of the fused top-N for another precision
  step, if evaluation shows fusion alone is not enough.
- **Local embedding provider** implementation behind the reserved
  `local` seam, for a fully on-prem retrieval + generation stack.
- **Sub-page chunking** only if measurement shows pages are too coarse.

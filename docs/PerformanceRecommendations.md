# Performance recommendations

An analysis of the end-to-end flow -- client (browser / Teams bot) ->
Agent API -> LLM -> MCP server -> data -- from a performance lens,
with a prioritized list of recommendations. Written against the
current codebase; each finding names the file and mechanism involved.

Context that drives the priorities: the shipped CSVs and PDFs are
SAMPLES. Production entitlement data will contain millions of rows,
and the knowledgebase will hold significantly more PDF content. Some
designs that are perfectly fine today stop working at that scale --
those are P0.

## Contents

1. [Anatomy of one turn](#1-anatomy-of-one-turn)
2. [Findings by component](#2-findings-by-component)
3. [Prioritized recommendations](#3-prioritized-recommendations)
4. [What is already right](#4-what-is-already-right)
5. [Measurement: prove it before and after](#5-measurement-prove-it-before-and-after)

---

## 1. Anatomy of one turn

What actually happens when a user asks "what access do finance
analysts have?":

```
Browser/Bot --1--> Agent API   (HTTP/SSE, auth, session lock)
                   Agent API --2--> Azure OpenAI   router call        ~1-3 s
                   Agent API --3--> Azure OpenAI   specialist call    ~1-5 s
                   Agent API --4--> MCP server     tool call #1       ms..s (data dependent)
                   Agent API --5--> Azure OpenAI   specialist call    ~1-5 s
                   Agent API --6--> MCP server     tool call #2       ms..s
                   ...             (typically 2-5 tool rounds)
                   Agent API --N--> Azure OpenAI   final answer       ~2-10 s (streamed)
```

Two structural facts dominate everything else:

1. **LLM calls are the wall-clock.** Each Azure OpenAI round trip
   costs seconds, and every TOOL ROUND costs an extra LLM call,
   because the model must see the tool result before deciding the
   next step. Anything that shrinks tool results (fewer prompt
   tokens) or eliminates tool rounds saves SECONDS. Anything else
   saves milliseconds.
2. **At production data volume, the MCP data layer flips from
   negligible to broken.** The in-memory design is excellent for the
   sample data. At millions of rows it fails on three axes at once:
   resident memory, startup time, and per-query latency. That is not
   a tuning problem; it is a storage-engine swap (contained inside
   csv_store.py).

Rule of thumb for reading the rest of this document: a
recommendation's impact = (seconds saved per turn) x (how often the
path runs). Token-economy items multiply with EVERY turn; data-layer
items multiply with every RESOURCE-AGENT turn; hop items add small
constants everywhere.

---

## 2. Findings by component

### 2.1 csv_store.py -- the critical finding

**Memory: list-of-dicts storage.** `CsvStore._read_csv` loads every
row as a Python dict (`csv.DictReader` -> `list[dict]`). Python dicts
carry roughly 300-500 bytes of interpreter overhead per row on top of
the actual strings. At 5M entitlement rows that is multi-GB resident
memory before a single query runs.

**Startup + memory again: BM25 over every row.**
`CsvDataset._build_index` concatenates every cell of every row,
tokenizes it, and feeds the corpus to `rank_bm25.BM25Okapi` -- a pure
Python implementation. At millions of rows this costs minutes of
startup CPU and roughly doubles memory (raw rows + tokenized corpus +
BM25 term statistics). And the capability it buys is semantically
weak for this dataset: nobody free-text-searches millions of
person-to-resource assignment rows -- the Resource agent's actual
usage (see RESOURCE_PROMPT strategies) is filter + count + join,
which BM25 does not help with.

**Per-query latency: linear scans everywhere.** All four query paths
are O(N) Python loops over the row list:

| Tool | Internal path | Work per call |
|---|---|---|
| `filter_dataset` | `_filter_rows` | full scan, `str.lower()` per cell compared |
| `filter_dataset_fuzzy` | `_filter_rows_fuzzy` | full scan, `regex.search` per cell |
| `count_by_column` | `count_by` (calls the above) | full scan + dict count |
| `get_column_values` | `distinct` | full scan + set |

At 5M rows each call is seconds of single-threaded Python. The
Resource agent's peer-recommendation flow issues SEVERAL of these per
turn (discovery with fuzzy=True, then exact count, then a Resources
lookup), each followed by an LLM round trip that waits on it. There
are no column indexes: filtering JOBTITLE + OU -- the hot path --
re-scans everything every time.

### 2.2 pdf_indexer.py -- the second finding

**Whole-document granularity.** `DocIndex` builds BM25 over the FULL
TEXT of each PDF as one corpus entry, and `read_page` returns the
ENTIRE document text as one tool result. Consequences at scale:

- ranking gets coarser as documents grow and multiply (a 60-page
  handbook competes as one blob against a 2-page FAQ)
- `read_page` payloads are huge; they routinely hit the
  `MAX_TOOL_CONTENT_LEN` (80k chars) truncation in the agent -- which
  is the worst combination: maximum prompt-token cost for CLIPPED
  information
- prompt tokens are latency AND money: every big tool result makes
  the next LLM call slower and more expensive, every turn

**No persistence.** `_build_index` re-extracts every PDF with PyMuPDF
on every server start. With significantly more documents, boot time
grows linearly and pointlessly -- the PDFs rarely change.

**Pure-Python scoring.** `BM25Okapi.get_scores` walks the whole
corpus per query. Fine at hundreds of documents; noticeable at many
thousands of chunks (mitigated by the chunking recommendation below,
worth watching after it).

### 2.3 Transport: agent -> MCP hops

With the current `MultiServerMCPClient.get_tools()` usage
(agnes_agent_graph._get_mcp_server_config -> AgentService.create),
each TOOL INVOCATION opens a fresh SSE session to the MCP server:
connect, handshake, initialize, call, teardown. That per-call setup
tax is paid 2-5 times per turn, every turn. The adapter supports
persistent sessions (`client.session()` as a context manager, loading
tools inside it); the codebase does not use it yet. Related: the MCP
ecosystem is moving from SSE to `streamable-http` as the preferred
HTTP transport -- worth adopting when touching this code.

### 2.4 Agent API layer

**Single-process ceiling.** Sessions, per-session locks, and the
MemorySaver checkpointer all live in ONE process's memory
(agent_api.py). Running uvicorn with multiple workers, or multiple
instances behind a load balancer, silently breaks sessions: each
process would have its own registry and its own checkpointer, and a
follow-up message routed to another worker gets a 404. Until state is
externalized, single worker is a HARD constraint (document it; do not
add workers as a "quick fix").

**Checkpoint growth per conversation.** LangGraph saves a checkpoint
per graph step and MemorySaver keeps all of them, each holding the
full cumulative message list -- storage per conversation grows
roughly with the square of its length, and stored tool results are
the untrimmed originals. Session TTL + LRU cap bound the POPULATION
(that work is done); a single marathon conversation is still heavy.

**Minor:** the graph execution log (AGENT_API_STREAM_FILE) does small
synchronous writes inside the async loop -- fine for debugging, set
it empty in production. Static-token auth and CORS checks are
microseconds; Entra JWKS keys are cached after first fetch -- auth is
not a performance factor on any path.

### 2.5 LLM usage patterns

- each turn after the first skips the router (active_agent routing)
  -- already saves one LLM call per turn; keep it
- system prompts are large and static (RESOURCE_PROMPT is ~2.5k
  tokens). Azure OpenAI prompt caching discounts and accelerates
  repeated static prefixes on GPT-4o -- PRESERVE prompt stability
  (avoid injecting per-turn dynamic content at the TOP of system
  prompts) so caching keeps applying
- `KEEP_LAST_N_MSGS` (20) caps context growth; `MAX_TOOL_CONTENT_LEN`
  (80k chars) caps single results. Both are blunt instruments -- the
  real fix is smaller tool results at the source (2.1, 2.2)

---

## 3. Prioritized recommendations

### P0 -- do before production data arrives

> **STATUS: IMPLEMENTED** -- see [P0.md](P0.md) for the plan, work-item
> checklist, and measured results (all 5M-row acceptance targets met:
> 25 ms filtered group-by, 167 ms fuzzy filter, 0.04 s warm start,
> 108 MB resident).

**P0.1 Replace CsvStore internals with an embedded analytical store
(DuckDB recommended).**

- What: keep the 8 CSV-backed MCP tool CONTRACTS exactly as they are;
  swap the implementation inside csv_store.py from list-of-dicts +
  Python loops to DuckDB tables + SQL. `filter_dataset` becomes
  `SELECT * FROM t WHERE lower(col)=lower(?) LIMIT ?`;
  `filter_dataset_fuzzy` becomes `regexp_matches`; `count_by_column`
  becomes `GROUP BY ... ORDER BY count DESC`; `get_column_values`
  becomes `SELECT DISTINCT`. The repo convention that "MCP server
  internals keep implementation names" was designed for exactly this
  swap (CsvStore stays CsvStore).
- Why DuckDB: embedded (no server to run), columnar and vectorized
  (5M-row filters and group-bys in milliseconds on one core),
  `read_csv_auto` ingestion, optional FTS extension, persists to a
  single file. SQLite + indexes is the conservative alternative --
  fine for exact filters, weaker for ad-hoc group-bys and regex.
- Persist the database file and ingest CSVs only when they change
  (mtime/hash check) -- startup becomes instant after first load.
- Expected effect at 5M rows: resident memory from multi-GB to tens
  of MB (DuckDB memory-maps its file); per-tool-call latency from
  seconds to low milliseconds; startup from minutes to sub-second.
- Effort: contained in csv_store.py + tests. The tool docstrings
  (truncation metadata, `_truncated` markers) carry over unchanged.

**P0.2 Drop BM25 over the Entitlements dataset.**

- Keep full-text search where it earns its cost: the small Resources
  catalogue (hundreds-thousands of rows, real descriptions) -- via
  rank_bm25 as today or DuckDB FTS after P0.1.
- For Entitlements, `search_dataset` should either return a polite
  "use filters for this dataset" error or route to a filtered query.
  The RESOURCE_PROMPT already steers the agent to count/filter tools
  for Entitlements, so agent behavior barely changes.
- This removes the single worst startup + memory cost in the system.

**P0.3 Add duration instrumentation in the same pass.**

- Log per-tool-call duration on the MCP server (wrap the existing
  caller-tagged log lines: `search_docs caller=agnes ... took=42ms`).
- Log per-node duration in the agent (the stream file's on_chain_end
  handler already sees node boundaries; add elapsed time to the
  entries).
- Purpose: every recommendation below should be accepted or rejected
  with numbers from these logs, not by intuition. Cheap to add while
  the files are open.

### P1 -- token economy (biggest per-turn latency and cost lever)

**P1.1 Page-level PDF indexing and retrieval.**

> **STATUS: IMPLEMENTED** (branch `P1.1`) -- see [P1.1.md](P1.1.md);
> measured 40.4x smaller tool results for single-page reads on a
> 40-page document, 13.5x for a 3-page range.

- Index per PAGE (or ~1-2k-token chunk) instead of per document. The
  extraction already produces per-page text with [Page N] markers --
  the change is to keep pages as separate index entries instead of
  joining them.
- `search_docs` returns page-level hits (path + page number +
  snippet); `read_page` gains a page-range parameter and returns just
  those pages, not the whole document.
- Effect: tool results shrink by 10-50x on large documents, the 80k
  truncation stops firing, prompt tokens (latency AND cost) drop on
  every knowledgebase turn, and ranking precision IMPROVES as the
  corpus grows. Citations get better, not worse -- the agent already
  cites page numbers.
- Migration note: keep `read_page(path)` without a range working
  (return full doc) so prompts and the scanner keep functioning while
  the KNOWLEDGEBASE_PROMPT is updated to prefer ranges.

**P1.2 Persist extracted PDF text / index to disk.**

- Cache extraction output (pickle/parquet keyed by file hash);
  rebuild only for changed files. Boot cost stops scaling with corpus
  size.

**P1.3 Preserve prompt-caching-friendly prompt shape.**

- Azure OpenAI caches long static prompt prefixes on GPT-4o.
  The specialist system prompts are exactly that -- keep them static
  (no timestamps or per-user content at the top of system prompts),
  and the discount/latency benefit applies to every single LLM call.

### P2 -- network hops

**P2.1 Persistent MCP session.**

- Hold one session per AgentService lifetime (adapter's
  `client.session()` context; load tools inside it) instead of a
  fresh SSE connection per tool call. Add reconnect-on-failure.
- Saves connection setup + MCP initialize handshake on every tool
  call -- a small constant, but paid 2-5 times per turn forever.
- While in this code: consider `streamable-http` transport (SSE is
  the legacy option in the MCP spec).

**P2.2 Co-locate agent and MCP server.**

- Same host or same network segment; it is one hop in the middle of
  every tool round. For single-agent deployments stdio removes HTTP
  entirely (at the cost of the multi-agent sharing story -- probably
  not the right trade here, but know it exists).

### P3 -- scale-out ceiling (when concurrent users grow)

**P3.1 Externalize session state to unlock horizontal scaling.**

- Swap MemorySaver for a persistent LangGraph checkpointer (Postgres
  and SQLite savers exist; Redis in the ecosystem). AgentService
  already takes the checkpointer as a constructor parameter -- that
  seam was built for this.
- The session REGISTRY (owner, last_used, lock) must move too: table
  or Redis keys for ownership/TTL; per-session locking becomes a
  distributed concern (advisory lock, or sticky routing by session id
  at the load balancer, which is simpler and usually sufficient).
- Until this is done: run exactly ONE uvicorn worker. Document it as
  a constraint everywhere deployment is described.

**P3.2 Bound per-conversation checkpoint growth.**

- With a persistent checkpointer, prune superseded checkpoints or cap
  turns per session (force a fresh session after N turns). Prevents
  the quadratic-growth marathon-conversation problem from moving to
  the database.

### P4 -- minor / operational

- Disable the graph stream file in production
  (`AGENT_API_STREAM_FILE=` empty) -- sync writes and unbounded
  verbosity are debugging features.
- Tune `KEEP_LAST_N_MSGS` and `MAX_TOOL_CONTENT_LEN` against real
  data once P0/P1 shrink results at the source.
- Uvicorn: `--loop uvloop` is a free few percent on the API hot path.
- Watch BM25 scoring cost on the chunked PDF index (post-P1.1); if
  chunk count reaches many tens of thousands, consider a compiled
  BM25 (e.g. tantivy) or DuckDB FTS for docs too.

### Suggested execution order

P0.1 + P0.2 + P0.3 as one contained change to mcp-server (biggest
risk reduction, measurable, no agent changes) -> P1.1 + P1.2 (second
contained change to mcp-server + one prompt update) -> P2.1 (small
agent-client change) -> P3 when concurrency demands it.

---

## 4. What is already right

Worth listing so nobody "optimizes" these away:

- router bypass via `active_agent` -- saves one LLM call on every
  turn after the first
- token streaming end to end (astream_events -> SSE) -- perceived
  latency is far better than actual latency
- `count_by_column` as a compact-summary tool -- the single best
  token-economy decision in the tool design; P1 extends this
  philosophy, it does not invent it
- `KEEP_LAST_N_MSGS` sliding window + `_compact_content` -- context
  growth is already capped
- session TTL + LRU cap + lock-aware eviction -- the session
  POPULATION cannot grow unboundedly
- prompt structure already steers the agent away from row-dumping
  (filter tools documented as "only when you need actual row data")

## 5. Measurement: prove it before and after

Before implementing anything beyond P0.3, capture a baseline with the
instrumentation it adds:

1. per-tool-call durations on the MCP server (by tool, by caller)
2. per-node durations in the agent stream log (router vs specialist
   vs tool nodes)
3. per-turn totals at the API (time from message receipt to answer
   event -- one log line in AgentService.stream)
4. token counts per LLM call if available from the Azure OpenAI
   response metadata (prompt vs completion tokens per node)

Then rerun the same scripted conversation (the incident scanner is a
ready-made repeatable workload) after each change. Accept or reject
each recommendation with those numbers. The expected story: P0 turns
data-tool seconds into milliseconds at production volume; P1 cuts
prompt tokens per knowledgebase turn by an order of magnitude; P2
shaves a constant few hundred milliseconds per tool round; P3 changes
no single-user number but removes the concurrency ceiling.

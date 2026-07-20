# Access Governance AI

LangGraph multi-agent system with an MCP tool server for documentation search,
resource discovery, and entitlement management. Powered by Azure OpenAI (GPT-4o).

## Repository structure

```
agent-client/src/ease_clients/
  cli.py              — CLI entry point (interactive agent)
  scanner_cli.py      — CLI entry point (batch scanner)
  agent_api.py        — AgentService (event-stream wrapper) + FastAPI app (HTTP API)
  auth_api.py         — pluggable API auth (static bearer token now, Entra OAuth2 later)
  cli_api.py          — API server entry point (runs uvicorn)
  redis_state.py      — Redis session state (P3.1): RedisSaver checkpointer + registry/locks

agent-client/src/ease_clients/utils/
  agnes_agent_graph.py — LangGraph StateGraph (router + 3 specialists + handoff)
  llm.py               — Azure OpenAI config
  incident_sources.py  — Incident dataclass + CSV/ServiceNow source classes
  scanner.py           — Batch scan engine (reuses agent graph)

agent-client/
  webclient_api.html  — POC single-page web client for the API (SSE streaming)
  tests_api/          — API test suite (fake agent; no Azure/MCP needed)
  README_api.md       — quick-reference for running the API

agent-client/data/
  Incidents.csv       — Sample ServiceNow-style incident data (12 incidents)

mcp-server/src/mcp_docs_server/
  server.py      — FastMCP server (12 tools over streamable-http/sse/stdio)
  auth.py        — per-agent bearer-token auth for HTTP transports (TokenVerifier)
  pdf_indexer.py — PDF → per-page BM25 index (DocIndex)
  csv_store.py   — CSV → in-memory DataFrame (CsvStore)

docs/
  architecture.md — Mermaid architecture diagrams (high-level, agent graph, MCP tools)
  agent_api.md    — beginner-oriented guide to the HTTP API layer
  entra_auth_guide.md — Entra ID implementation guide (client->agent + agent->MCP)
  PerformanceRecommendations.md — prod-scale performance analysis + prioritized plan
  aks.md          — AKS deployment analysis (topology, per-tier sizing, E2E latency budget)
  deploy.md       — deployment guide: podman local builds (registry-free), Helm chart, GitLab CI kaniko, dev->test->uat->prod

deploy/ (branch AKS-DEPLOY)
  chart/          — ONE Helm chart for both services (deployments incl. parquet initContainer, services, ingress, networkpolicy, HPA, PDBs, SecretProviderClass, configmaps)
  values-*.yaml   — per-environment overrides (dev/test/uat/prod); no secrets ever

mcp-server/Dockerfile + agent-client/Dockerfile (branch AKS-DEPLOY)
  — python:3.12-slim (mcr mirror), two-step uv sync, non-root; .dockerignore beside each
.gitlab-ci.yml (branch AKS-DEPLOY) — test -> kaniko build -> helm deploy with manual gates
  async.md        — Python async IO tutorial (8 runnable samples) + walkthrough of the agent's async code (spinner, astream_events)
  langgraph.md    — LangGraph tutorial (7 runnable samples, no Azure needed) + walkthrough of build_graph and the event stream
  bm25.md         — PDF extraction + BM25 search tutorial (5 runnable samples) + walkthrough of pdf_indexer.py
  duckdb.md       — parquet/DuckDB/MCP-tools tutorial (6 runnable samples) + walkthrough of the data path
  architecture_excalidraw.md + 0*.excalidraw — Excalidraw diagrams (05 = API flows, 06 = AKS topology)
```

## Architecture

The agent is a LangGraph `StateGraph` with 8 nodes:

```
START -> route_entry() -> Router -> knowledgebase_agent / resource_agent / quality_agent
                                      <-> tool loop        <-> tool loop     <-> tool loop
                                    knowledgebase_tools   resource_tools    quality_tools
                                       \          handoff          /
                                             Router (re-route)
                                               -> END
```

- **State**: `AgentState(messages: list, active_agent: str)`
- `active_agent` provides persistent specialist ownership across turns
- Specialists call `hand_off_to_router()` to defer to another specialist
- Mixed questions: specialist answers its part, defers the rest

### MCP tools (12 total)

| Group | Tools |
|-------|-------|
| Knowledgebase (3) | `list_topics`, `search_docs`, `read_page` |
| Resource (6) | `list_datasets`, `search_dataset`, `filter_dataset`, `filter_dataset_fuzzy`, `count_by_column`, `get_column_values` |
| Request (2) | `get_request_attributes`, `raise_entitlement_request` |
| Quality (1) | `get_quality_criteria` |

For full diagrams with conditional edges and data flow, see `docs/architecture.md`.

## Naming conventions

- **Agent-side** uses domain names: `knowledgebase` (not pdf), `resource` (not csv), and `quality`
- **MCP server internals** keep implementation names (`pdf_indexer`, `csv_store`, `CsvStore`, `DocIndex`) — these describe how data is stored

## How to run

```bash
# MCP server
cd mcp-server && uv run mcp-docs-server

# Agent client -- interactive (in a separate terminal)
cd agent-client && uv run agent-client

# Incident scanner -- batch mode (in a separate terminal)
cd agent-client && uv run scan-cli data/Incidents.csv -o scan_results.csv

# HTTP API server (in a separate terminal; requires AGENT_API_TOKEN or AGENT_API_AUTH=none)
cd agent-client && uv run agent-api

# API tests (fake agent -- no Azure/MCP needed)
cd agent-client && uv run --with pytest --with httpx pytest tests_api/ -q
```

Required env vars: `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`.
All config is read from the single gitignored `agent-client/.env`; `.env.example`
is the complete committed template (agent + API settings).

MCP transport: streamable-http (default, set `MCP_SERVER_URL` to the /mcp
endpoint; stateless mode via `MCP_STATELESS_HTTP=true` so replicas can sit
behind a load balancer), sse (legacy HTTP, kept for rollback), or stdio (set
`MCP_SERVER_COMMAND` + `MCP_SERVER_ARGS`). See docs/streamable-http.md.

MCP auth: the server requires a bearer token per agent on HTTP transports
(`MCP_AUTH=static` fail-closed, `MCP_AUTH_TOKENS=name:token,...`; `none` to
disable). The agent sends `MCP_SERVER_TOKEN` from its .env; the name is a
server-side label used in tool-call audit logs (`caller=agnes`). stdio needs
no auth. See mcp-server/README.md Authentication.

API: bearer-token auth on all endpoints except /health (`AGENT_API_AUTH=static`
with `AGENT_API_TOKEN`, fail-closed; `none` to disable for local dev). Endpoints:
POST /sessions, POST /sessions/{id}/messages (buffered JSON), POST
/sessions/{id}/messages/stream (SSE: phase/token/answer events), GET
/sessions/{id}/messages (history; raw=true for debug dump). Sessions are
explicit (404 for unknown/expired ids), TTL-evicted, LRU-capped, and locked
to one message at a time. See `docs/agent_api.md` for the full guide.

API session state: in-process by default (single instance only, lost on
restart). Set `REDIS_URL` to move sessions, turn locks, and conversation
checkpoints into a shared Redis so multiple API instances can serve one
session pool (plain Redis, no modules; fail-fast when unreachable;
`AGENT_API_LOCK_TIMEOUT_SECONDS` bounds the distributed turn lock). See
`docs/P3.1.md` and docs/agent_api.md section 16.

## Development

Branch: `claude/mcp-html-docs-server-S9jg9`

### Recent work

- Refactored agent from flat ReAct loop to custom StateGraph with Router, specialists, and handoff
- Renamed pdf/csv identifiers to knowledgebase/resource in agent code
- Added `get_request_attributes` and `raise_entitlement_request` MCP tools (dynamic params from `request_config.json`)
- Added mixed-question handling (specialists answer their part, defer the rest)
- Created Mermaid architecture diagrams in `docs/architecture.md`
- Added token-level streaming via astream_events with phase-aware spinner
- Fixed `_dump_history` node attribution -- uses snapshot.next instead of missing metadata["writes"] key (not persisted in LangGraph 1.0.8)
- Normalized comment style: plain characters, `->` arrows, concise docstrings
- Added Data Quality Checker specialist (quality_agent) with 21 criteria from `quality_criteria.json`; evaluates resource metadata dynamically against all CSV columns
- Added standalone incident scanner CLI (scan-cli) -- batch mode that reuses the agent graph without modifying agent.py
- Extracted incident sources to dedicated module (incident_sources.py) with CsvIncidentSource and ServiceNow stub
- Added HTTP API layer in `_api` files: AgentService event-stream core + FastAPI app (agent_api.py), pluggable bearer-token auth with fail-closed startup (auth_api.py), uvicorn launcher (cli_api.py), CORS support for browser clients
- Added POC single-page web client (webclient_api.html) -- vanilla JS, fetch-based SSE parsing, session reuse
- Added API test suite (tests_api/, 22 tests) using a fake agent -- runs without Azure or MCP
- Added GET /sessions/{id}/messages history endpoint -- chat view by default, raw=true debug dump (types + tool calls)
- Added session hygiene to the API: explicit sessions only (404 for unknown/expired ids), idle-TTL eviction via background sweeper + LRU cap (AGENT_API_SESSION_TTL_MINUTES / AGENT_API_MAX_SESSIONS), per-session lock serialising concurrent messages; web client auto-recreates expired sessions. Eviction is lock-aware: in-flight sessions are never evicted (cap overshoots if everything is mid-turn)
- Added graph execution log to the API (AGENT_API_STREAM_FILE, default agent_api_stream.txt): per-node output appended per turn, session-tagged, same format as the CLI's agent_stream.txt, reset on server start
- Added MCP server auth: per-agent static bearer tokens on HTTP transports via the MCP SDK's TokenVerifier hook (mcp-server auth.py; MCP_AUTH fail-closed, MCP_AUTH_TOKENS name:token pairs), caller identity in all 12 tool log lines, client sends MCP_SERVER_TOKEN from _get_mcp_server_config. mcp-server tests (13); tests_api now 36
- Added beginner-oriented API guide (docs/agent_api.md) and Excalidraw API-flow diagram (docs/05_agent_api_flow.excalidraw)
- Merged the API layer into the main manifests: fastapi/uvicorn deps + agent-api script in pyproject.toml, API settings in .env.example (the temporary pyproject_api.toml / .env_api supersets were removed)
- Restructured the package to match the server deployment: agent_client -> ease_clients, shared internals moved to ease_clients/utils (llm.py, scanner.py, incident_sources.py, and agent.py renamed to agnes_agent_graph.py); entry points and loggers renamed accordingly
- Switched the default MCP transport from sse to streamable-http (branch streamable-http): client _get_mcp_server_config gains a streamable_http branch (default URL /mcp, same bearer-token header), server passes MCP_STATELESS_HTTP (default true) to FastMCP so replicas can run behind a load balancer, sse kept as legacy rollback, auth unchanged (the gate already covered both HTTP transports). See docs/streamable-http.md. tests_api now 40
- Implemented P0 performance work (docs/P0.md, branch Performance-P0, rebased onto streamable-http): CsvStore reworked onto embedded DuckDB (persisted MCP_DB_PATH file, warm starts skip ingest, atomic fingerprint-based refresh + background sweeper, size-gated BM25 via MCP_SEARCH_MAX_ROWS), pluggable ingestion in data_sources.py (CsvDataSource + DatabaseSource stub for Azure SQL/Postgres), per-tool took=ms logging on the server, per-node/per-turn timing in AgentService. All 5M-row acceptance targets met (25ms filtered group-by, 0.04s warm start, 108MB RSS). mcp-server tests now 35
- Added ParquetDataSource -- the production data path (docs/P0.md section 11.9): Spark/Databricks parquet exports staged locally from Azure Storage (az cli, service principal) load via MCP_DATA_SOURCE=parquet + MCP_PARQUET_SOURCES=Name=glob pairs. Datasets are folder-of-part-files globs (markers excluded), every column cast to VARCHAR so the string-based tool contract is unchanged, mtime/size fingerprints drive the same atomic refresh cycle; az:// URLs accepted for a future direct-read mode (duckdb azure extension -- blocked in the dev pod, bake into the AKS image later). Verified live over streamable-http + auth. mcp-server tests now 53 (18 new)
- Implemented P1.1 page-level PDF retrieval (docs/P1.1.md, branch P1.1, based on Performance-P0): DocIndex indexes one BM25 entry per page, search_docs hits carry the page number with page-local snippets, read_page gains a pages selection ("3" / "2-5", empty = full document for back compat), KNOWLEDGEBASE_PROMPT steers to page ranges. Measured 40.4x smaller tool results for single-page reads on a 40-page doc. mcp-server tests now 52
- Added AKS deployment analysis (docs/aks.md + Excalidraw diagram 06): two-tier topology on managed AKS against the full P1.1 stack -- agent-api replicas 2-3 (unlocked by P3.1, plain round-robin), mcp-server replicas 2 + CPU HPA (unlocked by stateless streamable-http), Azure Cache for Redis, ingress SSE tuning, readiness-vs-liveness probe guidance, hop-by-hop E2E latency budget (Azure OpenAI quota owns ~95% of turn time; topology buys ms + resilience), sizing starting point, and the follow-up register (P1.2, P1.3, P3.2, /livez)
- Added deployment implementation plan (docs/deploy.md, plan only): RHEL processes stay the dev loop (no docker-compose ever); images built without Docker (podman locally for smoke tests, az acr build / GitLab CI kaniko for real artifacts); ONE Helm chart + four values files (dev/test/uat/prod) with the values-vs-Key-Vault mapping for every env var; GitLab CI pipeline (test -> build -> deploy with manual gates, same chart+SHA promoted through all clusters, environment-scoped credentials); per-environment validation incl. the in-cluster P3.1 rig; helm rollback story
- Added async IO tutorial (docs/async.md): part 1 builds the concepts for a novice (coroutines/await, event loop + gather, tasks, cancellation, async generators, async with, asyncio.Lock, never-block-the-loop) with 8 standalone runnable samples -- all verified; part 2 maps each concept onto agnes_agent_graph.py line by line (cli.py's asyncio.run entry, ainvoke in nodes, the astream_events consumer, the Spinner's create_task / polite sleep / cancel-then-await lifecycle, the single-thread spinner+tokens timeline) plus the API-layer parallels (composed async generators to SSE, the per-session lock, the sweeper task)
- Added LangGraph tutorial (docs/langgraph.md, companion to async.md): part 1 builds StateGraph concepts with 7 standalone runnable samples needing no Azure/MCP (state + partial updates, the messages reducer, conditional edges, the tool-loop cycle, checkpointer/thread_id sessions, astream, astream_events with GenericFakeChatModel producing real token events offline) -- all verified; part 2 walks build_graph line by line (AgentState's two merge behaviors, node factories incl. the never-executed routing/handoff tool trick, the tool split, every edge mapped to its design decision, supersteps/checkpoints, and the event-emission model: metadata.langgraph_node inheritance vs the event name filter the P0 timing keys on)
- Added PDF/BM25 tutorial (docs/bm25.md, third volume after async.md and langgraph.md): part 1 builds the concepts with 5 standalone runnable samples in the mcp-server env (PDF-as-drawing-instructions + blank-page numbering, the regex tokenizer and the no-stemming lexical gap, hand-rolled TF-IDF, BM25's saturation and length-normalization fixes shown empirically, and the whole-doc-vs-page granularity demo that motivates P1.1) -- all verified; part 2 walks pdf_indexer.py (extraction, the parallel corpus/_index_keys lists, the density-window snippet, fuzzy path resolution, page-range reads with LLM-facing structured errors) and closes on the scale story + the lexical boundary that vector.md's hybrid design addresses
- Added parquet/DuckDB/MCP-tools tutorial (docs/duckdb.md, fourth volume): part 1 builds the data-side concepts with 6 standalone runnable samples in the mcp-server env (embedded file-backed DuckDB, querying parquet in place, Spark part-file globs + the COLUMNS(*)::VARCHAR cast, atomic CREATE OR REPLACE swaps gated by mtime/size fingerprints, the two SQL-safety rules for LLM-supplied input, and a minimal FastMCP tool with an error-message-as-instruction) -- all verified; part 2 walks data_sources.py (the DataSource split, ParquetDataSource hardening), csv_store.py (threads-not-asyncio concurrency, cursor-per-call, the LIMIT max+1 truncation trick, the size-gated BM25 sidecar), and server.py (docstrings as prompt engineering, _caller audit identity, the _timed half of the P0 instrumentation), closing on the measured P0 numbers
- Implemented P3.1 Redis session state (docs/P3.1.md, branch P3.1, now stacked on P1.1): with REDIS_URL set, AgentService moves its three in-process state pieces into shared Redis -- conversation checkpoints via a custom plain-Redis RedisSaver (mirrors InMemorySaver's storage model; no RedisJSON/RediSearch modules, so any Azure Cache tier works), the session registry (hash + server-side EXPIRE replaces the sweeper, LRU zset keeps the cap with the lock-aware skip rule), and the per-session turn lock (SET NX PX distributed lock, AGENT_API_LOCK_TIMEOUT_SECONDS). Unset REDIS_URL = previous in-process behavior byte-for-byte; REDIS_URL set but unreachable = fail-fast startup; /health pings Redis (503 when down) for readiness probes. Validated on real plain redis 7.0: restart survival, cross-instance lock exclusion, +2.6ms per-turn overhead. tests_api now 61 (fakeredis[lua], no server in CI)
- Implemented the deploy.md repo-side artifacts (branch AKS-DEPLOY, based on Vector): mcp-server/Dockerfile + agent-client/Dockerfile (python:3.12-slim from the mcr mirror, two-step uv sync -- --no-install-project then src then final sync -- UV_PYTHON_DOWNLOADS=never, non-root app user, uv run --no-sync entry points) with .dockerignore files that keep the duckdb cache / staged parquet / .env out of build contexts; ONE Helm chart (deploy/chart) fixing the aks.md topology in templates (stateless streamable-http, derived in-cluster MCP_SERVER_URL, agent /health readiness vs tcp liveness + 300s grace, MCP tcp startupProbe as the ingest gate, emptyDirs for /data + /duckdb, anti-affinity, checksum/config pod-roll annotation, parquet-staging initContainer authing via workload identity, SecretProviderClass with 5 fixed Key Vault object names, NetworkPolicy agent->mcp:8000 only, conditional HPA/PDBs); four values files (dev runs 2 agent replicas on purpose for the in-cluster P3.1 kill-a-pod check); .gitlab-ci.yml (test -> kaniko builds -> helm deploys with manual gates, REGISTRY_BASE defaults to the GitLab Container Registry pending Phase B). Redis stays OUTSIDE the cluster (Azure Cache; redis-url from Key Vault). Chart verified: helm lint + helm template against all four values files (10/12/13/13 objects). deploy.md rewritten around the committed files incl. the fixed podman smoke test (-v ...:Z parquet mount -- host paths are invisible in containers) and the PDFs-in-CI-images open point; work items 1/4/5/7 checked off

## General instructions

- Default to **plan mode** -- always present a plan and wait for approval before making changes. Do not use auto-accept unless explicitly told to switch.

## Code style

- **Comments**: Write in plain, human-like English. Use simple alphanumeric characters only.
  - Use `->` for arrows, not `-->`, unicode arrows, or em dashes
  - Use `--` for dashes, not em dashes or unicode
  - Use `+->` for branching, not `└──▶` or other box-drawing characters
  - Keep comments lowercase unless starting a sentence
  - Minimal indentation inside comments -- avoid deeply nested comment formatting
  - Section headers: `# -- Section name --` (not `# ── Section ──────`)
- **Docstrings**: Concise and direct. No RST backtick markup (`` ``var`` ``). Refer to identifiers by name plainly.

## To do

- Port the interactive CLI onto AgentService events (agnes_agent_graph.py still has its own streaming loop; agent_api.py has the event-based one -- converge them)
- Azure Entra OAuth2 authenticator (`entra` mode in auth_api.py -- JWT/JWKS validation; interface already reserved). See docs/entra_auth_guide.md
- Entra-based auth for the MCP server: replace StaticTokenVerifier with a JWT/JWKS validator in the same TokenVerifier slot (mcp-server auth.py); agent identity from token claims, scopes mapped to tool groups for per-agent authorization. See docs/entra_auth_guide.md
- Replace the POC web client with a real web UI (HTTPS, login flow instead of token-in-url, tightened CORS)
- Prompt-caching-friendly prompt shape + provider-portable trimming (P1.3 in
  `docs/PerformanceRecommendations.md`). Goal: stop paying full input-token
  cost on the large STATIC prefix (specialist system prompt + the 12 tool
  definitions) that is re-sent on every LLM call. Detailed analysis:
  - The win is provider-agnostic. Every prompt-caching implementation keys off
    a stable, unchanging prompt prefix. Azure OpenAI / GPT-4o / the GPT-5
    family cache such prefixes AUTOMATICALLY (cache reads bill at roughly half
    the input price); Anthropic (Claude) caches the same prefix but only when
    it is marked EXPLICITLY. Either way the prefix must stay byte-stable across
    turns.
  - The one thing that breaks caching today: `_trim_messages` in
    `agnes_agent_graph.py` (line ~499) slides a `messages[-KEEP_LAST_N:]`
    window on EVERY call once a conversation grows past KEEP_LAST_N (env
    `KEEP_LAST_N_MSGS`, default 20). A sliding window shifts the message list
    from the top down, so the cached prefix stops matching and the discount is
    lost on every long conversation. Fix -> chunked trimming with hysteresis:
    let history grow to a high-water mark (~KEEP_LAST_N + 12), cut back to
    KEEP_LAST_N in one step, and stay append-only between trims. The prefix
    then changes only at the rare trim points, not every turn.
  - Three-part change when this is picked up:
    1. Hysteresis trimming in `agnes_agent_graph.py` (replaces the per-call
       slide above).
    2. Cached-token visibility: fold cache hit/write counts into the existing
       per-node timing logs. Read them from LangChain's NORMALIZED
       `usage_metadata.input_token_details` (`cache_read` / `cache_creation`),
       NOT the raw OpenAI-only field `prompt_tokens_details.cached_tokens`. The
       normalized field is populated from whichever provider is active, so the
       instrumentation survives a model swap unchanged.
    3. Prompt-shape guardrail (docs + a code comment): keep static content
       (system prompt, tool defs) first and volatile content (user turns) last.
       This is what makes automatic caching hit AND what makes an explicit
       Anthropic breakpoint worth placing.
  - Model portability (the reason to build it this way now): switching the
    model under the hood stays a ONE-SEAM change. `get_llm()` in
    `ease_clients/utils/llm.py` is the only place that knows the provider:
    - Azure / OpenAI / GPT-5 branch -> return the model as-is; automatic prefix
      caching needs no extra code.
    - Anthropic branch (future) -> return `ChatAnthropic` and attach
      `cache_control={"type": "ephemeral"}` breakpoints to the static prefix
      (system prompt + tools). Anthropic specifics for later: up to 4
      breakpoints, rendered tools -> system -> messages; minimum cacheable
      prefix ~1024-4096 tokens (model dependent); cache writes bill at 1.25x
      (5-min TTL) or 2x (1-hour TTL), reads at ~0.1x; verify via
      `usage.cache_read_input_tokens` / `cache_creation_input_tokens`.
    The graph, specialists, prompts, and trimming never learn which provider is
    active -- breakpoint placement stays confined to `get_llm()`.
  - Net: parts 1 and 3 help every provider and are a precondition for Anthropic
    caching; part 2 is portable if written against `usage_metadata`; only the
    breakpoint placement is provider-specific, and it lives entirely inside
    `get_llm()`.
- Externalize agent session state to Redis (P3.1 in
  `docs/PerformanceRecommendations.md`) -- NEXT UP, planned before migrating
  P0/P1.1. Unlocks agent-api replicas > 1 (multi-pod AKS) by moving the three
  in-process pieces out of AgentService: the LangGraph checkpointer, the
  session registry (Redis per-key EXPIRE replaces the TTL sweeper), and the
  per-session locks (SET NX PX distributed locks with a timeout longer than
  the longest turn). Config seam: REDIS_URL in the single .env; absent ->
  current in-process MemorySaver behavior unchanged (CLI and scanner never
  need Redis). Open design decision for the checkpointer: the official
  langgraph-checkpoint-redis needs the RedisJSON/RediSearch modules
  (redis-stack locally; Azure Cache Enterprise tier in prod), so the plain-
  Redis alternatives are a minimal custom saver on plain strings or the
  official PostgresSaver. Dev environment VALIDATED 2026-07-14: plain redis
  6.2.7 runs locally on the same host as the agent (RHEL 8.10 dev pod with
  CentOS 8 repos; install needed
  --setopt=centos-8-appstream.module_hotfixes=true to bypass missing modular
  metadata; run redis-server --port 6379 -- NOT 8000, which collides with
  MCP_PORT). Test rig for the implementation: two agent-api instances on
  ports 8080/8081 against one local Redis -> restart survival, cross-instance
  session continuity, concurrent-turn lock exclusion.

### Enterprise maturity gaps (E2E review, 2026-07-16)

Gaps identified beyond the feature roadmap above -- these cluster in
trust, quality, and governance of the AI behavior itself. Each lands
on a seam that already exists.

- Evaluation harness -- THE gap to close before P1.3 / Vector / any
  model swap: no way today to tell whether a prompt/model/retrieval
  change degrades answer quality (unit tests prove plumbing, not
  answers). Build a curated golden set per specialist (question ->
  expected facts/citations), run in CI, exact checks where possible +
  LLM-as-judge where not, regression-gated. Head start: the scanner
  CLI already batch-runs questions through the real graph -- an eval
  harness is that plus assertions.
- Prompt-injection defenses, especially INDIRECT injection: the agent
  trusts tool results, so a poisoned PDF page or a crafted entitlement
  field is an instruction channel into the LLM (classic RAG poisoning;
  high-stakes in access governance). Mitigations: content demarcation
  in prompts, tool-result sanitization, output checks before
  consequential actions; Azure AI Content Safety / Prompt Shields fit
  the stack.
- Per-user data authorization (row-level scoping): today every user
  sees everything the AGENT can see -- no requester-based scoping of
  entitlement data. Entra leg 1 brings the user identity to the API
  (Principal.claims is the reserved hook); carry it into tool calls as
  filter constraints. In an access-governance product this is arguably
  the product. Do alongside the Entra work -- same identity plumbing.
- Human-in-the-loop for writes: raise_entitlement_request fires
  whenever the LLM decides (currently a placeholder -- the deadline is
  "before the first real write"). Use LangGraph interrupt_before on
  the tool node; the checkpointer investment (P3.1 RedisSaver) is
  exactly what makes interrupts resumable across pods. API surfaces
  "agent wants to submit X -- confirm?", user approves, graph resumes.
- LLM observability + cost accounting: latency telemetry exists
  (took=ms both sides) but no token/cost tracking per turn / session /
  user / specialist, no cache-hit rates (P1.3 part 2 starts this), no
  trace visualization of a full turn. Candidates: Langfuse
  (self-hostable), LangSmith, or OpenTelemetry GenAI conventions into
  the existing Azure Monitor story. Per-caller cost attribution also
  matters once other agents consume the MCP server. Do with the AKS
  move.
- Durable conversation audit (compliance): sessions are deliberately
  ephemeral (P3.1 non-goal, decision point flagged there). "Show me
  what the agent told this user on March 3rd" is regulated-industry
  table stakes; the seam is a PostgresSaver or an export pipeline off
  the checkpointer. Needs a business decision on retention/PII/DLP
  before code.
- User feedback loop: no thumbs up/down or correction capture. Cheap
  to add at the API/web-UI layer; feeds the eval set, prioritizes
  knowledgebase gaps, demonstrates value.
- LLM-call resilience: get_llm() sets no timeout; retries are SDK
  defaults; no circuit breaker, no fallback deployment/region. An
  Azure OpenAI brownout currently hangs turns toward the P3.1 lock
  timeout. Add explicit timeouts, budgeted retries with jitter, a
  fallback model/region, graceful degradation messaging -- and keep
  lock TTL > (LLM timeout x retries) + tool time (P3.1.md section 14).
- Smaller items: cross-session user memory/personalization (LangGraph
  store patterns); per-user rate limits and token budgets at the API
  (arrives naturally with Entra identities); prompt versioning + A/B
  against the eval set; citations as structured data (source links)
  for the real web UI -- the tool results already carry them.
- Sequencing against the existing roadmap: evals BEFORE P1.3/Vector
  (they are the safety net those changes need); injection defenses +
  row-level authorization ALONGSIDE Entra; HITL when the request tool
  becomes real; observability + resilience WITH the AKS move; audit
  after the retention decision.

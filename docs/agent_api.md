# The Agent API -- a beginner-friendly guide

This document explains the HTTP API layer added to the agent client: what
was built, why each piece exists, and how it works -- written for a reader
who knows some Python but has little experience with FastAPI, web APIs, or
authentication. The API code lives in files suffixed `_api`; its
dependencies and settings are part of the regular `pyproject.toml` and
`.env.example`.

This guide and the source files are designed to be read together: this
document covers the concepts and the why, while the code in
`agent_api.py`, `auth_api.py`, `cli_api.py`, and
`tests_api/test_agent_api.py` carries detailed inline comments
explaining each non-obvious Python and FastAPI construct (dataclasses,
protocols, async generators, dependency injection, lifespan, SSE)
right where it is used.

## Contents

1. [Why an API?](#1-why-an-api)
2. [The big picture](#2-the-big-picture)
3. [What files were added](#3-what-files-were-added)
4. [Concepts you need, explained from scratch](#4-concepts-you-need-explained-from-scratch)
5. [The service core: AgentService and AgentEvent](#5-the-service-core-agentservice-and-agentevent)
6. [Authentication: how and why](#6-authentication-how-and-why)
7. [The FastAPI app and its endpoints](#7-the-fastapi-app-and-its-endpoints)
8. [Defining the server vs starting it](#8-defining-the-server-vs-starting-it)
9. [Session hygiene: bounded memory and one turn at a time](#9-session-hygiene-bounded-memory-and-one-turn-at-a-time)
10. [Configuration -- one single .env file](#10-configuration----one-single-env-file)
11. [Running the API server](#11-running-the-api-server)
12. [Calling the API -- worked examples](#12-calling-the-api----worked-examples)
13. [Testing](#13-testing)
14. [The future: Azure Entra OAuth2](#14-the-future-azure-entra-oauth2)
15. [What was deliberately not changed](#15-what-was-deliberately-not-changed)

---

## 1. Why an API?

Before this change, the only way to talk to the agent was the interactive
CLI (`uv run agent-client`). The agent logic and the terminal were welded
together: the code that runs the LangGraph agent also printed tokens to
the screen with `sys.stdout.write` and drew a spinner.

That is fine for one person at a keyboard, but it cannot serve a web page,
a Microsoft Teams bot, a Slack app, or another program. Those clients all
speak HTTP. So the agent is now wrapped in a small web service:

- any program that can make an HTTP request can use the agent
- each frontend decides its own presentation (a web page might show a
  "thinking..." indicator; a Teams bot just posts the final answer)
- the agent core stays in one place -- new frontends need zero agent code

## 2. The big picture

```mermaid
flowchart LR
    subgraph clients [External clients]
        WEB[Web UI]
        TEAMS[Teams / Slack bot]
        CURL[curl / scripts / other agents]
    end

    subgraph api [API server -- cli_api.py]
        AUTH[auth_api.py<br/>bearer token check]
        APP[agent_api.py<br/>FastAPI endpoints]
        SVC[agent_api.py<br/>AgentService]
    end

    GRAPH[agnes_agent_graph.py<br/>LangGraph graph -- unchanged]
    MCP[MCP server -- unchanged]

    WEB -->|HTTP + SSE| AUTH
    TEAMS -->|HTTP| AUTH
    CURL -->|HTTP| AUTH
    AUTH --> APP
    APP --> SVC
    SVC --> GRAPH
    GRAPH <--> MCP
```

A request flows left to right: a client sends an HTTP request with a
token, the token is checked, the endpoint hands the message to the
AgentService, which runs the existing LangGraph agent, which calls MCP
tools as usual. The answer flows back as JSON or as a live event stream.

## 3. What files were added

All new, all in `agent-client/`:

| File | Role |
|------|------|
| `src/ease_clients/agent_api.py` | The service core (`AgentService`, `AgentEvent`) and the FastAPI app with all endpoints |
| `src/ease_clients/auth_api.py` | Authentication: token checking, pluggable for the future |
| `src/ease_clients/cli_api.py` | The entry point that starts the web server |
| `webclient_api.html` | POC single-page web client (streaming) -- open directly in a browser |
| `README_api.md` | Quick-reference for running the API |
| `tests_api/test_agent_api.py` | 36 tests that run without Azure or the MCP server |

The naming convention: where new behavior parallels an existing file,
the new file takes the same name plus `_api` (`cli.py` -> `cli_api.py`).
The API's dependencies (fastapi, uvicorn), its `agent-api` console
script, and its settings are part of the regular `pyproject.toml` and
`.env.example` alongside everything else.

## 4. Concepts you need, explained from scratch

### What is FastAPI?

FastAPI is a Python library for building web APIs. You write ordinary
Python functions and attach them to URLs with decorators:

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

When an HTTP GET request arrives at `/health`, FastAPI calls this
function and converts the returned dict to JSON automatically. The
`async def` means the function can pause while waiting for slow things
(the LLM, the MCP server) without blocking other requests.

### What is uvicorn?

FastAPI only describes the app; it does not listen on a network port
itself. uvicorn is the web server that does the listening -- it accepts
connections on a host/port and passes each request to the FastAPI app.
`cli_api.py` boils down to one call: `uvicorn.run(create_app(), ...)`.

### What is a "session" here?

The agent keeps conversation history so follow-up questions work
("what about the second one?"). The API models this with sessions: you
create a session once, get back an id, and send every message of that
conversation with the same id. Internally a session id is a LangGraph
"thread id" -- the key under which the checkpointer stores the message
history. Because the checkpointer is MemorySaver (plain process memory),
restarting the server forgets all sessions. That is a known, accepted
tradeoff for now; swapping in a persistent checkpointer later is a
one-argument change in `AgentService.create`.

Sessions have a lifecycle, because a long-running server would
otherwise accumulate conversation state forever (LangGraph saves a
checkpoint after every graph step, so a long conversation holds many
snapshots -- memory per session grows fast):

- only ids minted by `POST /sessions` are accepted; anything else is
  404. Sessions are never created implicitly.
- a session idle longer than `AGENT_API_SESSION_TTL_MINUTES` (default
  60) is evicted by a background sweeper: its registry entry and its
  checkpointer state are deleted. Requests for it get 404 -- the
  client creates a fresh session and continues (the POC web client
  does this automatically).
- `AGENT_API_MAX_SESSIONS` (default 500) is a hard cap; when full, the
  least recently used IDLE session is evicted to make room.
- one message per session runs at a time: a per-session lock makes a
  second concurrent message wait instead of letting two turns
  interleave writes into the same history.
- a session whose turn is currently running is never evicted -- not by
  the TTL sweeper and not by the cap. If every session is mid-turn when
  the cap is hit, the cap briefly overshoots instead of deleting
  history out from under a running turn.

### What is SSE (Server-Sent Events)?

A normal HTTP response arrives all at once. But the agent produces its
answer token by token, and a good UI wants to show words as they are
generated (the "live typing" effect you see in the CLI).

SSE is the simplest standard way to do that: the server keeps the HTTP
response open and writes small text blocks as events happen. Each block
looks like:

```
event: token
data: {"data": "Hello"}

```

(the blank line marks the end of one event). The client reads events as
they arrive instead of waiting for the whole response. Browsers, Python
libraries, and even `curl -N` understand this format.

### What is a bearer token?

The simplest common way to protect an API. The client attaches a secret
string to every request in a standard header:

```
Authorization: Bearer my-secret-token
```

"Bearer" literally means: whoever bears (carries) this token is allowed
in. The server checks the token and rejects the request with status
`401 Unauthorized` if it is wrong or missing. Because possession of the
token IS the authentication, tokens must never be committed to git or
written to logs.

### How do the API dependencies get installed?

The API needs two libraries beyond what the agent already used: fastapi
and uvicorn. They are ordinary project dependencies in
`agent-client/pyproject.toml`, and the `agent-api` command is declared
there as a console script. So the standard

```bash
cd agent-client
uv sync
```

installs everything -- agent, scanner, and API -- into one shared
environment. (Historical note: the API originally shipped with a
separate `pyproject_api.toml` manifest to avoid touching any existing
file; that constraint has since been lifted and the manifest merged.)

## 5. The service core: AgentService and AgentEvent

The heart of the design is separating "what the agent produces" from
"how it is displayed". The CLI displays output by printing; a web UI
displays it by updating a page; a bot posts a message. So the service
core produces neutral events and lets each consumer render them.

`AgentEvent` (in `agent_api.py`) is a tiny dataclass with two fields:

| type | data | meaning |
|------|------|---------|
| `phase` | e.g. `"Routing"`, `"Calling tools"` | the graph moved to a new node -- what the CLI spinner used to show |
| `token` | one text fragment | one streamed piece of the answer |
| `answer` | the full answer text | always emitted last, so buffering clients can just take this one |
| `error` | a safe message | something failed mid-stream |

`AgentService` wraps the compiled LangGraph graph:

- `AgentService.create()` -- an async factory that does the same startup
  as the CLI: build the Azure OpenAI client, connect to the MCP server,
  load the 12 tools, build and compile the graph. Called once when the
  server starts, because connecting and indexing is slow.
- `create_session()` -- returns a fresh uuid to use as a session id.
- `stream(session_id, text)` -- runs one conversation turn and yields
  AgentEvents as they happen. Internally it iterates the graph's
  `astream_events` output exactly like the CLI loop in `agnes_agent_graph.py` does;
  the only difference is that it yields events instead of writing to the
  terminal.
- `ask(session_id, text)` -- convenience for non-streaming clients:
  consumes `stream()` internally and returns just the final answer text.
- `get_history(session_id)` -- returns the session's raw stored message
  list from the checkpointer; the history endpoint shapes it into the
  chat or debug view.

Importantly, `agent_api.py` imports `build_graph` and friends FROM
`utils/agnes_agent_graph.py` -- the graph, the prompts, the specialists,
and the routing logic are all shared, not copied. This mirrors how
`utils/scanner.py` already reuses the graph without modifying it.

## 6. Authentication: how and why

All auth code is in `auth_api.py`, about 100 lines.

### The moving parts

- `Principal` -- a dataclass describing WHO the caller is (`subject`,
  plus a `claims` dict that stays empty for now). Today it is barely
  used; it exists so that when real user identity arrives (see section
  14), the rest of the code does not have to change shape.
- `Authenticator` -- a Protocol (an interface): anything with an
  `authenticate(token) -> Principal` method that raises `AuthError` on
  bad input. The API endpoints depend only on this interface, never on
  a concrete implementation. This is what makes auth "pluggable".
- `StaticTokenAuthenticator` -- the implementation used today. Compares
  the presented token to one shared secret.
- `NoAuthAuthenticator` -- accepts everyone; exists only as an explicit
  opt-out for local development.
- `build_authenticator()` -- a factory that reads the `AGENT_API_AUTH`
  environment variable and returns the right implementation.

### AGENT_API_AUTH values

| Value | Behavior |
|-------|----------|
| `static` (default) | Requires `Authorization: Bearer <token>` matching `AGENT_API_TOKEN`. If `AGENT_API_TOKEN` is not set, the server REFUSES TO START. |
| `none` | No authentication. Local development only. |
| `entra` | Reserved for Azure Entra OAuth2 -- currently rejected at startup. |
| anything else | Startup error listing the valid options (a typo cannot silently disable auth). |

### Three security details worth understanding

**Constant-time comparison.** The token check uses
`secrets.compare_digest(token, expected)` instead of `token == expected`.
A naive `==` returns as soon as the first character differs, so comparing
`"aXXX"` takes measurably less time than comparing `"secrXXX"` -- an
attacker who measures response times can discover the token one character
at a time. `compare_digest` always takes the same time regardless of
where the difference is, closing that hole.

**Fail closed.** If configuration is missing (static mode, no token set),
the server raises an error at startup instead of running without auth.
The safe behavior is the default; the unsafe behavior (`none`) requires
an explicit, grep-able opt-in. This is also why the committed
`.env.example` template leaves `AGENT_API_TOKEN` commented out --
shipping a default token like `change-me` would mean every deployment
that forgot to change it is protected by a publicly known password.

**Never log tokens.** Log lines mention subjects and session ids, never
credential values.

### How auth attaches to endpoints

FastAPI has a feature called dependency injection: an endpoint can
declare `principal: Principal = Depends(current_principal)` in its
signature, and FastAPI runs `current_principal` before the endpoint.
That function extracts the `Authorization: Bearer ...` header, asks the
configured authenticator to validate it, and either returns a Principal
(request proceeds) or raises HTTP `401` with a `WWW-Authenticate: Bearer`
header (request rejected before the endpoint code ever runs). Every
endpoint except `/health` declares this dependency -- `/health` stays
open so monitoring probes work without credentials.

## 7. The FastAPI app and its endpoints

`create_app(service=None, authenticator=None)` builds the app. The two
arguments exist for testing (tests inject fakes); in production both are
`None` and a "lifespan" handler creates them at startup -- the
authenticator from env config, the service by connecting to the MCP
server. Lifespan is FastAPI's hook for run-once-at-startup work.

| Method and path | Auth | Purpose |
|-----------------|------|---------|
| `GET /health` | no | liveness check; returns `{"status": "ok", "tools": 12}` |
| `POST /sessions` | yes | create a conversation; returns `{"session_id": "..."}` |
| `POST /sessions/{id}/messages` | yes | send a message, get `{"answer": "..."}` back in one response -- for bots and scripts |
| `POST /sessions/{id}/messages/stream` | yes | send a message, receive an SSE stream of `phase`/`token`/`answer` events -- for live UIs |
| `GET /sessions/{id}/messages` | yes | conversation history: user/assistant chat view by default, `?raw=true` for the full debug dump (every stored message with types and tool calls) |

Why two message endpoints? A Teams or Slack bot cannot render a stream --
it posts one complete message -- so forcing it to consume SSE would just
push buffering work onto every bot author. Conversely a web UI without
streaming feels frozen for the many seconds a tool-using answer takes.
Offering both lets each client pick.

Error behavior:

- wrong or missing token -> `401` with a `WWW-Authenticate: Bearer` header
- agent failure on the buffered endpoint -> `500` with a generic message
  (the real traceback goes to the server log, not to the client)
- agent failure mid-stream -> an `error` event inside the stream, because
  the HTTP status line was already sent when streaming began
- an unknown or expired session id -> `404` with a hint to create a new
  session. Only ids minted by `POST /sessions` are valid, and idle
  sessions are evicted after the TTL (see "What is a session" in
  section 4).

## 8. Defining the server vs starting it

A common point of confusion: which file IS the server? The answer is
that no single file is -- the work is deliberately split between
defining the server and starting it.

**`agent_api.py` defines, but never starts.** It is a library module.
`create_app()` is a factory function: calling it builds and returns a
FastAPI app object -- a Python object in memory that describes the
endpoints and how to handle them. At that point nothing is listening on
any port. Importing the module, or even calling `create_app()`, starts
no server.

**`cli_api.py` starts, but defines nothing.** Despite the name, it is
NOT a chat client and never sends a message to the agent (the
interactive chat client is the existing `cli.py`, which talks to the
LangGraph graph directly in-process -- no HTTP involved). `cli_api.py`
is the launcher: "cli" in its name only means "the thing you invoke
from the command line to start the server". Its `main()` contains the
one line that actually starts serving:

```python
uvicorn.run(create_app(), host=host, port=port)
```

`uvicorn.run` is the moment a real network socket opens on the
configured host and port and the process begins accepting HTTP
requests. It blocks until you stop it with ctrl-c.

**`agent-api` is only a name.** It is a console script declared in
`pyproject.toml`:

```toml
[project.scripts]
agent-api = "ease_clients.cli_api:main"
```

When `uv sync` installs the project it generates a tiny wrapper
executable, `.venv/bin/agent-api`, whose entire body is "import
`cli_api`, call `main()`". It adds no behavior -- it is a launcher for
the launcher, existing so you can type `uv run agent-api` instead of a
file path.

The full chain when you start the server:

```
uv run agent-api
  -> .venv/bin/agent-api            (generated wrapper -- just a name)
  -> cli_api.main()                 (load .env, read host/port)
  -> agent_api.create_app()         (build the app object -- nothing listening yet)
  -> uvicorn.run(app, ...)          (open the port, serve until ctrl-c)
```

Why the split matters: because `agent_api.py` never starts anything on
import, the test suite can call `create_app()` directly, hand the app
to FastAPI's `TestClient`, and exercise every endpoint with no port, no
uvicorn, and no process management -- while injecting a fake agent
instead of the real one (see section 13). If the module started the
server at import time, none of that would be possible.

## 9. Session hygiene: bounded memory and one turn at a time

The first API release had two deliberate gaps, found in a later code
review and fixed together. Both fixes live in
[`agent_api.py`](../agent-client/src/ease_clients/agent_api.py); this
section explains the problems and walks the code. (Line links are
current as of this writing; the symbol names are the stable
reference.)

### Problem 1: server memory could grow forever

Three things compounded:

1. **Nothing was ever freed.** Conversation state lives in `MemorySaver`,
   a dict inside the server process. Sessions were added on every
   `POST /sessions` but never removed -- a session abandoned weeks ago
   cost as much memory as an active one, until a restart threw
   everything away. This was harmless in the CLI (one thread id, process
   exits when you quit) but the API moved the same checkpointer into a
   long-running multi-session process.
2. **Each session grows faster than expected.** LangGraph saves a
   checkpoint after every graph step (router, specialist, tools, ...),
   and MemorySaver keeps all of them, each holding the full message
   list at that moment. Storage per conversation grows roughly with the
   square of its length -- and stored tool results are the untrimmed
   originals, which can be tens of kilobytes each.
3. **Clients controlled how many sessions existed.** Any string was
   accepted as a session id -- `POST /sessions/anything/messages`
   silently created state. The memory ceiling was set by clients, not
   the server.

### Problem 2: concurrent messages could corrupt a conversation

Nothing stopped two simultaneous POSTs with the same session id. Both
turns would write into the same LangGraph thread at once, interleaving
their checkpoints -- most likely producing garbled history rather than
a crash, which is worse because it is quiet.

### The fix: a session registry inside AgentService

One structure solves both problems. Every live session gets a
[`_Session`](../agent-client/src/ease_clients/agent_api.py#L77)
bookkeeping entry holding two things: a `last_used` timestamp (taken
from `time.monotonic()`, a steady clock that never jumps backwards)
and an `asyncio.Lock`. The registry itself is a dict on
[`AgentService`](../agent-client/src/ease_clients/agent_api.py#L107),
configured by two env vars read in `AgentService.create()`:
`AGENT_API_SESSION_TTL_MINUTES` (default 60) and
`AGENT_API_MAX_SESSIONS` (default 500).

How each piece addresses the problems:

- **Explicit sessions only.**
  [`create_session()`](../agent-client/src/ease_clients/agent_api.py#L172)
  registers every id it mints, and
  [`_require_session()`](../agent-client/src/ease_clients/agent_api.py#L202)
  raises `UnknownSessionError` for anything not in the registry --
  which the endpoints translate to a `404` with a "create a new
  session" hint. Ids can no longer be invented by clients (closes
  problem 1, layer 3). The streaming endpoint
  [checks before the response starts](../agent-client/src/ease_clients/agent_api.py#L594)
  via `has_session()`, because once streaming begins the HTTP status
  line has already been sent and a 404 can no longer be delivered.
- **TTL eviction.**
  [`sweep_expired_sessions()`](../agent-client/src/ease_clients/agent_api.py#L221)
  evicts every session idle longer than the TTL;
  [`sweep_loop()`](../agent-client/src/ease_clients/agent_api.py#L239)
  runs it once a minute as a background task that the app's
  [lifespan handler](../agent-client/src/ease_clients/agent_api.py#L454)
  starts at boot and cancels at shutdown.
- **Eviction actually frees the memory.**
  [`_evict()`](../agent-client/src/ease_clients/agent_api.py#L210)
  removes the registry entry AND calls the checkpointer's
  `delete_thread()` -- the second part is the one that matters, because
  the checkpoints are where the megabytes live (problem 1, layers 1-2).
- **LRU cap as a backstop.** When the registry is at
  `AGENT_API_MAX_SESSIONS`,
  [`create_session()`](../agent-client/src/ease_clients/agent_api.py#L172)
  evicts the least recently used session before minting a new id, so
  even a client that leaks sessions cannot push memory past the cap.
- **Eviction is lock-aware.** Neither the sweeper nor the cap will
  evict a session whose turn is currently running (its lock is held) --
  deleting checkpointer state under a running graph would corrupt that
  conversation. If the cap is hit while every session is mid-turn,
  `create_session()` allows a brief overshoot instead, and the sweeper
  catches up once turns finish.
- **One turn at a time.**
  [`stream()`](../agent-client/src/ease_clients/agent_api.py#L276) runs
  the whole turn inside
  [`async with session.lock`](../agent-client/src/ease_clients/agent_api.py#L298).
  A second message on the same session waits for the first to finish
  instead of interleaving writes (closes problem 2). Different sessions
  are unaffected -- each has its own lock. The turn also refreshes
  `last_used` when it completes, so a long-running answer never counts
  as idleness.

### What clients see

- an unknown or expired session id -> `404 Unknown or expired session`
- the POC web client handles this automatically: on a 404 it creates a
  fresh session and retries the message once (conversation context is
  gone, but the question still gets answered)
- a second message sent while one is in flight simply takes longer --
  it queues on the lock

The behavior is covered by six dedicated tests in
[`test_agent_api.py`](../agent-client/tests_api/test_agent_api.py):
unknown-session rejection at both service and endpoint level, sweep
eviction (asserting the checkpointer thread is deleted too), survival
of active sessions, LRU cap order, and lock serialisation proven with
a fake agent that counts overlapping turns.

## 10. Configuration -- one single .env file

ALL configuration is read from one place: the gitignored `.env` file in
`agent-client/` -- the same file the CLI already uses. The code reads
nothing else; there is no second env file at runtime and no required
shell exports.

`.env.example` is the committed TEMPLATE for that file. It is never
read by the code. It lists every variable the framework needs -- Azure
OpenAI settings, agent behavior (`AGENT_LOG_LEVEL`, `KEEP_LAST_N_MSGS`,
`MAX_TOOL_CONTENT_LEN`), MCP connection (`MCP_TRANSPORT`,
`MCP_SERVER_URL`, ...), and the API settings -- with placeholder values.
Before starting the API, copy it to `.env` (or copy the values you need
into your existing `.env`) and fill in real values:

```bash
cd agent-client
cp .env.example .env    # then edit .env with real values
```

Because `.env.example` is committed to git it holds placeholders only;
the real secrets live in your local `.env`, which git ignores.

The API-specific settings:

| Variable | Default | Meaning |
|----------|---------|---------|
| `AGENT_API_AUTH` | `static` | auth mode (see section 6) |
| `AGENT_API_TOKEN` | unset | the shared secret for static mode |
| `AGENT_API_HOST` | `127.0.0.1` | bind address; the template sets `0.0.0.0` so other machines can connect |
| `AGENT_API_PORT` | `8080` | port |
| `AGENT_API_CORS_ORIGINS` | `*` | which page origins browsers may call from |
| `AGENT_API_SESSION_TTL_MINUTES` | `60` | evict sessions idle longer than this |
| `AGENT_API_MAX_SESSIONS` | `500` | hard cap; least recently used evicted when full |
| `AGENT_API_STREAM_FILE` | `agent_api_stream.txt` | graph execution log for tail -f; empty disables |

## 11. Running the API server

```bash
# terminal 1 -- the MCP server, exactly as before
cd mcp-server && uv run mcp-docs-server

# terminal 2 -- the API server
cd agent-client
uv sync

# one-time per checkout: create the single .env from the template,
# then edit it with real values (Azure key, AGENT_API_TOKEN, ...)
cp .env.example .env

uv run agent-api
```

The server logs
`Starting Access Governance Agent API on 127.0.0.1:8080` and then
`Agent API ready (12 tools)` once the MCP connection is up.

The existing CLI and scanner are unaffected and run exactly as before.

### Watching the graph work: the stream file

The CLI has always written every graph node's output to
`agent_stream.txt` so you can watch the agent think with `tail -f`.
The API gives you the same visibility: as each node completes, its
messages are appended to `agent_api_stream.txt` (configurable via
`AGENT_API_STREAM_FILE`; empty disables) in the same format:

```
--- turn 14:02:31  session ab12cd34 ---

[HumanMessage]  (node: input, session: ab12cd34)
How do I set up delegations?

[AIMessage]  (node: knowledgebase_agent, session: ab12cd34)
  -> tool_call: search_docs({'query': 'delegation setup'})
...
```

Because the API serves many sessions at once, every turn header and
entry carries the session id -- turns within one session never
interleave (the per-session lock guarantees that), but turns from
different sessions can, so `grep "session: ab12cd34"` reconstructs one
conversation. The file is reset each time the server starts, keeping
its size bounded per uptime. For per-session inspection over HTTP
instead of on the server's disk, use
`GET /sessions/{id}/messages?raw=true` (section 7).

## 12. Calling the API -- worked examples

### With curl

```bash
TOKEN=pick-something-secret
BASE=http://127.0.0.1:8080

# 1. liveness (no token needed)
curl $BASE/health

# 2. create a session
SID=$(curl -s -X POST $BASE/sessions \
  -H "Authorization: Bearer $TOKEN" | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['session_id'])")

# 3a. buffered -- one JSON answer
curl -X POST $BASE/sessions/$SID/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "How do I set up delegations?"}'

# 3b. streaming -- events as they happen (-N disables curl buffering)
curl -N -X POST $BASE/sessions/$SID/messages/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What access do finance analysts have?"}'
```

### From Python (a minimal bot-style client)

```python
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"Authorization": "Bearer pick-something-secret"}

session_id = requests.post(f"{BASE}/sessions", headers=HEADERS).json()["session_id"]

reply = requests.post(
    f"{BASE}/sessions/{session_id}/messages",
    headers=HEADERS,
    json={"message": "How do I order an entitlement?"},
)
print(reply.json()["answer"])
```

Because both calls reuse `session_id`, a follow-up like
`{"message": "and who approves it?"}` keeps the conversation context.

### Reading the stream from Python

```python
import json
import requests

with requests.post(
    f"{BASE}/sessions/{session_id}/messages/stream",
    headers=HEADERS,
    json={"message": "hello"},
    stream=True,
) as response:
    event_type = None
    for line in response.iter_lines(decode_unicode=True):
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))["data"]
            if event_type == "token":
                print(data, end="", flush=True)   # live typing
            elif event_type == "phase":
                pass                              # could update a status bar
            elif event_type == "answer":
                print()                           # data holds the full text
```

### A browser client: webclient_api.html

`agent-client/webclient_api.html` is a working single-page POC client
for the streaming endpoint -- plain HTML and vanilla JavaScript in one
file, no framework or build step. Open it directly in a browser while
the API server is running and pass the token in the url:

```
file:///path/to/agent-client/webclient_api.html?token=your-secret-token
```

Two implementation details worth knowing (both commented in the file):

- The browser's built-in SSE client (`EventSource`) only supports GET
  requests, but our stream endpoint is a POST with a JSON body and an
  Authorization header. So the page uses `fetch()` and reads the
  response body chunk by chunk, splitting on blank lines to parse the
  SSE format by hand (about 20 lines of code).
- Browsers block a page on one origin (a local file) from reading
  responses of an api on another origin (127.0.0.1:8080) unless the
  api sends CORS headers. The API includes CORS middleware for this,
  controlled by `AGENT_API_CORS_ORIGINS` (default `*` for local dev).
  CORS is not authentication -- the bearer token is still required.

## 13. Testing

The test suite (`tests_api/test_agent_api.py`, 36 tests) needs neither
Azure OpenAI credentials nor a running MCP server. It builds a `FakeAgent`
that replays canned graph events, so the tests exercise the real
AgentService event handling, the real endpoints, and the real auth code
against predictable input:

- event sequence: phase -> tokens -> answer; fallback when nothing streams
- `ask()` returns the assembled answer
- auth matrix: no header 401, wrong token 401, correct token 200,
  `/health` open, `none` mode accepts all, static mode without a token
  refuses startup
- SSE wire format parses and ends with the `answer` event

```bash
cd agent-client
uv run --with pytest --with httpx pytest tests_api/ -q
```

## 14. The future: Azure Entra OAuth2

The static token is a stopgap: one shared secret, no idea WHO is calling.
The end goal is Azure Entra ID (formerly Azure AD): each user or app
obtains a short-lived JWT access token from Microsoft, and the API
validates it cryptographically.

The design already reserves the slot. What changes when Entra lands:

1. a new `EntraAuthenticator` class in `auth_api.py` implementing the
   same `authenticate(token) -> Principal` interface. It will fetch
   Microsoft's public signing keys (JWKS), verify the JWT signature,
   issuer, audience, and expiry, and build a Principal whose `subject`
   is the user's id and whose `claims` carry name/roles/etc.
2. `build_authenticator()` grows an `entra` branch reading tenant id and
   audience from env vars.
3. a dependency on a JWT library (e.g. `pyjwt[crypto]`).

What does NOT change: endpoints, clients, the header format. OAuth2
access tokens travel in `Authorization: Bearer <jwt>` -- the exact header
clients already send. That is why the bearer scheme was chosen on day
one. Once real identity exists, sessions can additionally be bound to
`principal.subject` so callers only see their own conversations.

## 15. What was deliberately not changed

- the agent logic itself: `utils/agnes_agent_graph.py` (formerly
  `agent.py`), `cli.py`, `utils/scanner.py`, `scanner_cli.py`,
  `utils/incident_sources.py`, `utils/llm.py` -- the API only imports
  from them; the interactive CLI works as before
- the entire `mcp-server/` package -- untouched

The API was originally built fully additively (no existing file
modified at all, with separate `pyproject_api.toml` and `.env_api`
supersets). That constraint has since been lifted in stages: the API's
dependencies, script, and settings moved into the regular
`pyproject.toml` and `.env.example`, CLAUDE.md documents the API
layer, and the whole package was later restructured from
`src/agent_client` to `src/ease_clients` (with shared internals under
`utils/`) to match the server deployment.

Remaining accepted tradeoff: the CLI keeps its own streaming loop in
`agnes_agent_graph.py` while the API has the event-based loop in
`agent_api.py`. The two share the graph and all agent logic but render
output separately. A later cleanup can port the CLI onto AgentService
events and delete the duplication.

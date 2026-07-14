# Switching the MCP transport from SSE to streamable-http

This document explains the switch of the agent-to-MCP-server transport
from SSE to streamable-http. Branch: `streamable-http`, based on the
default branch. It assumes no prior knowledge of MCP transports --
every concept is explained from scratch.

The one-line summary: the agent and the MCP server now talk over a
SINGLE ordinary HTTP endpoint (`/mcp`) instead of the two-endpoint
SSE arrangement, and the server runs in stateless mode so that, in a
production setup, several copies of it can sit behind a load balancer
and any copy can answer any request. Nothing about the tools, the
auth model, or the agent's behavior changes.

## Contents

1. [What is a transport?](#1-what-is-a-transport)
2. [How the old SSE transport worked](#2-how-the-old-sse-transport-worked)
3. [How streamable-http works](#3-how-streamable-http-works)
4. [What stateless mode means](#4-what-stateless-mode-means)
5. [Why switch -- the reasons](#5-why-switch----the-reasons)
6. [What changed, file by file](#6-what-changed-file-by-file)
7. [Configuration reference](#7-configuration-reference)
8. [Authentication: nothing changed, and why](#8-authentication-nothing-changed-and-why)
9. [How to run and verify](#9-how-to-run-and-verify)
10. [Rollback](#10-rollback)
11. [FAQ](#11-faq)

---

## 1. What is a transport?

MCP (Model Context Protocol) defines WHAT the agent and the tool
server say to each other: "list your tools", "call search_docs with
this query", "here is the result". A TRANSPORT defines HOW those
messages physically travel between the two processes. The protocol
messages are identical on every transport -- only the delivery
mechanism differs. Think of the letter versus the postal service.

This project supports three transports, all selected by the
`MCP_TRANSPORT` environment variable on both sides:

| Transport | How messages travel | When to use |
|---|---|---|
| `stdio` | the agent starts the server as a child process and they talk through stdin/stdout pipes | both on the same machine, simplest local setup |
| `streamable-http` | ordinary HTTP requests to one endpoint, `/mcp` | **the default for anything over a network** |
| `sse` | HTTP, but split across two endpoints with a long-lived stream | legacy -- kept only for rollback |

## 2. How the old SSE transport worked

SSE (Server-Sent Events) is a browser-era technique for a server to
push a stream of events over one long-lived HTTP response. The MCP
SSE transport uses it like this:

1. The client opens `GET /sse` and KEEPS that connection open. This
   is the "downstream" -- every reply the server ever sends arrives
   as an event on this stream.
2. The server's first event tells the client a second URL (with a
   session id in it).
3. Every request the client makes -- initialize, list tools, call a
   tool -- is a `POST /messages/?session_id=...` to that second URL.
   The response to each POST arrives NOT in the POST's own reply,
   but as an event pushed down the long-lived `/sse` stream.

Two properties of this design matter here:

- **It is a matched pair.** The `GET /sse` stream and the
  `POST /messages` requests belong to one session and MUST reach the
  SAME server process. If a load balancer sends the GET to replica A
  and the POSTs to replica B, the session is broken -- replica B has
  never heard of it.
- **A long-lived idle connection.** Load balancers, ingress
  controllers, and proxies routinely time out or buffer long-lived
  streams unless specially configured.

Both are fine for one agent talking to one server on a trusted
network -- which is exactly what this project was until now. Both are
a problem the moment you want several server replicas behind one
address, which is the AKS production shape.

## 3. How streamable-http works

Streamable-http is the MCP spec's newer HTTP transport, designed to
replace SSE. There is ONE endpoint, `/mcp`, and the client simply
POSTs each protocol message to it and reads the reply from that same
POST's response:

```
old (sse):                          new (streamable-http):

GET  /sse            <- long-lived  POST /mcp  {initialize}    -> reply
POST /messages {initialize}         POST /mcp  {list tools}    -> reply
POST /messages {list tools}         POST /mcp  {call tool}     -> reply
POST /messages {call tool}
     ... all replies arrive on
     the /sse stream ...
```

The name "streamable" refers to an option, not an obligation: when a
reply is long-running, the server MAY stream that one response as
events -- but each request/response pair is self-contained. There is
no permanently-open side channel that has to be routed to the same
process as everything else.

For a novice, the mental model is: **streamable-http behaves like a
normal REST API.** Every standard piece of HTTP infrastructure --
load balancers, Kubernetes services, ingress rules, proxies --
already knows how to handle it.

## 4. What stateless mode means

Even with one endpoint, an MCP session normally has a tiny bit of
server-side memory: the client runs `initialize` once, the server
issues a session id, and later calls reference it. That still ties a
session to the one process that issued the id.

`stateless_http=True` (a FastMCP server option, exposed here as
`MCP_STATELESS_HTTP`, default `true`) removes that last piece of
state: every request is treated as self-contained, so ANY replica of
the server can answer ANY request. That is the property that makes
`replicas: 2+` behind a Kubernetes service just work.

The cost: the transport re-runs its small initialize handshake per
call instead of once per connection -- a few milliseconds against
tool calls that take tens to hundreds of milliseconds, and the server
holds no per-session memory in exchange. Nothing in OUR code held MCP
session state anyway (the tools are pure functions over the indexes),
which is why this is a flag and not a redesign.

Set `MCP_STATELESS_HTTP=false` only if you run exactly one server
process and want the per-connection handshake back.

## 5. Why switch -- the reasons

1. **Production topology (the trigger).** The prod plan is AKS with
   the MCP server as a multi-replica Deployment behind a ClusterIP
   service. SSE's matched-pair design cannot survive that without
   sticky routing; streamable-http in stateless mode requires nothing.
2. **Spec direction.** The MCP specification has deprecated the SSE
   transport in favor of streamable-http; the SDKs keep it for
   compatibility. Better to move while the surface is small.
3. **Simpler operations.** One endpoint, no long-lived idle stream to
   configure timeouts and buffering for on every proxy in the path.
4. **Timing.** Doing this BEFORE migrating P0/P1.1/Vector means every
   later feature is tested against the production transport from day
   one -- and the change barely overlaps those branches (see 6).

## 6. What changed, file by file

The auth layer needed ZERO changes -- the auth gate in `server.py`
was already written as
`if MCP_TRANSPORT in ("sse", "streamable-http")`, and the SDK applies
the same `TokenVerifier` to both HTTP transports.

### mcp-server

1. **`src/mcp_docs_server/server.py`** -- **[DONE]**
   - `MCP_TRANSPORT` is normalized (lowercase, `_` -> `-`), so both
     `streamable-http` and `streamable_http` spellings work.
   - New `MCP_STATELESS_HTTP` env (default `true`), passed to the
     `FastMCP(...)` constructor as `stateless_http`.
   - The startup log line shows the stateless flag for HTTP runs.
   - Everything else -- tools, auth wiring, indexes -- untouched.
2. **`.env.example`** -- transport comments rewritten
   (streamable-http recommended, sse marked legacy), new
   `MCP_STATELESS_HTTP` entry. **[DONE]**
3. **`README.md`** -- architecture sketch, config table, and Running
   section updated. **[DONE]**

### agent-client

4. **`src/ease_clients/utils/agnes_agent_graph.py`**,
   `_get_mcp_server_config()` -- **[DONE]**
   - Default transport is now `streamable-http` with default URL
     `http://127.0.0.1:8000/mcp`.
   - The HTTP branch handles both `streamable-http` and `sse` (same
     URL + bearer-token header logic; only the default path and the
     transport literal differ).
   - One wrinkle worth knowing: the server SDK spells the transport
     `streamable-http` (hyphen) while the client adapter library
     (`langchain-mcp-adapters`) spells it `streamable_http`
     (underscore). The env var accepts either; the code maps to
     whatever each library expects. You never have to care.
   - stdio branch and the error for unknown transports unchanged
     (the error message now names all three).
5. **`.env.example`** -- `MCP_TRANSPORT=streamable-http`,
   `MCP_SERVER_URL=.../mcp`, sse block kept commented as the legacy
   option. **[DONE]**
6. **`README.md`** -- architecture sketch, config table, usage
   commands updated. **[DONE]**
7. **`tests_api/test_agent_api.py`** -- four new tests: default is
   streamable_http on /mcp; bearer token attached on streamable-http;
   underscore spelling normalized; unknown transport raises a
   ValueError naming the valid options. The two existing sse-shape
   tests still pass, proving the legacy path works. tests_api goes
   36 -> 40. **[DONE]**

### docs

8. **`CLAUDE.md`** -- transport summary, repo map line, recent-work
   entry. **[DONE]**
9. **`docs/streamable-http.md`** -- this document. **[DONE]**

Nothing else changed. In particular `auth.py`, all 12 tools,
`pdf_indexer.py`, `csv_store.py`, the API layer, and the prompts are
byte-identical -- which is also why rebasing the P0 / P1.1 / Vector
branches onto this one is low-risk: they edit different regions.

## 7. Configuration reference

Server side (`mcp-server/.env`):

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `streamable-http` for network setups; `sse` legacy; hyphen/underscore both accepted |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` | bind address for the HTTP transports |
| `MCP_STATELESS_HTTP` | `true` | any-replica-can-answer mode; leave on unless single-instance |
| `MCP_AUTH` / `MCP_AUTH_TOKENS` | `static` / unset | unchanged -- see mcp-server/README.md Authentication |

Client side (`agent-client/.env`):

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` | `sse` (legacy) and `stdio` still supported |
| `MCP_SERVER_URL` | `http://127.0.0.1:8000/mcp` | use the `/sse` path if you roll back to sse |
| `MCP_SERVER_TOKEN` | unset | unchanged -- this deployment's bearer token |

## 8. Authentication: nothing changed, and why

The bearer-token model (one `name:token` pair per agent deployment,
fail-closed `MCP_AUTH=static`, caller name in every tool audit log)
carries over exactly as it was. Two design choices made this free:

- The client attaches the token as an ordinary HTTP header
  (`Authorization: Bearer ...`). Both HTTP transports carry headers
  the same way, so the same three lines of client code serve both.
- On the server, auth was wired through the MCP SDK's `TokenVerifier`
  hook, which the SDK enforces for BOTH HTTP transports; the gate in
  `server.py` already listed `streamable-http` when it was written.

stdio still has no auth, for the same reason as before: there is no
HTTP layer -- the server runs as a child process of a caller who
already has local access.

## 9. How to run and verify

```bash
# terminal 1 -- the server, on the new transport
cd mcp-server
# .env: MCP_TRANSPORT=streamable-http, MCP_AUTH_TOKENS=agnes:<token>
uv run mcp-docs-server
# startup log shows: Transport: streamable-http (host=..., port=8000, stateless=True)

# terminal 2 -- any client (CLI, scanner, or API)
cd agent-client
# .env: MCP_TRANSPORT=streamable-http, MCP_SERVER_URL=http://<host>:8000/mcp,
#       MCP_SERVER_TOKEN=<token>
uv run agent-client
```

What to look for:

- The client log line `MCP server=... transport=streamable-http
  url=... auth=bearer` at startup.
- The server's per-tool audit lines are unchanged and still carry the
  caller: `search_docs caller=agnes query=... took=12.3ms`.
- A wrong or missing `MCP_SERVER_TOKEN` fails the connection (401),
  exactly as under sse.

Test suites:

```bash
cd mcp-server    && uv run --with pytest pytest tests/ -q                  # 13 (auth)
cd agent-client  && uv run --with pytest --with httpx pytest tests_api/ -q # 40
```

(On this branch the server suite is the 13 auth tests; the csv-store
and pdf-indexer suites arrive with the P0 / P1.1 branches, which will
be rebased on top of this one.)

## 10. Rollback

The sse code paths were kept on both sides, so rollback is
config-only -- no redeploy of code:

```bash
# server .env
MCP_TRANSPORT=sse
# client .env
MCP_TRANSPORT=sse
MCP_SERVER_URL=http://<host>:8000/sse
```

## 11. FAQ

**Q: Did the tools change at all?**
No. Transports carry messages; the 12 tools, their schemas, and their
results are byte-identical. The agent cannot tell the difference.

**Q: Why does the transport have two spellings?**
Two libraries, two conventions: the server SDK's `run()` takes
`"streamable-http"` (hyphen); the client adapter's connection dict
takes `"streamable_http"` (underscore, it is a Python identifier in a
typed dict). The `MCP_TRANSPORT` env var accepts either spelling on
either side and the code translates.

**Q: What happens on each tool call now?**
The client POSTs to `/mcp`: initialize handshake, then the tool call,
then the reply comes back on those same responses. In stateless mode
the handshake repeats per call -- a few milliseconds. A later
optimization (P2.1 in docs/PerformanceRecommendations.md) can hold
one session open per AgentService lifetime instead.

**Q: Does stateless mode make the server forget my conversation?**
No. Conversation state lives in the AGENT (LangGraph checkpointer),
never in the MCP server -- its tools were always stateless functions
over the indexes. `MCP_STATELESS_HTTP` only affects the transport's
internal session bookkeeping.

**Q: When would I still use sse?**
Only as a rollback if something unexpected surfaces in an existing
sse deployment. It is deprecated in the MCP spec; new setups should
not start with it.

**Q: And stdio?**
Unchanged and still the default for a purely local, single-machine
setup -- it involves no network and needs no auth.

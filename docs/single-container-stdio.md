# Single-container stdio deployment -- agent and MCP server in one image

Design for colocating the agent API and the MCP server in ONE container,
with the agent launching the MCP server as a child process over stdio.
Branch: `stdio`, based on `denorm_bugfix`.

This is a deliberate step BACKWARD from the streamable-http topology in
[aks.md](aks.md) and [deploy.md](deploy.md), taken for one reason: the
authentication infrastructure the split topology needs does not exist
yet. Section 8 is honest about what it costs, and section 10 is the
route back when OAuth lands.

## Contents

1. [Why: the auth constraint](#1-why-the-auth-constraint)
2. [Why stdio removes the problem entirely](#2-why-stdio-removes-the-problem-entirely)
3. [Why ONE container, not one pod](#3-why-one-container-not-one-pod)
4. [Two blocking findings](#4-two-blocking-findings)
5. [Code changes](#5-code-changes)
6. [The image and the entry point](#6-the-image-and-the-entry-point)
7. [Chart changes](#7-chart-changes)
8. [What this costs](#8-what-this-costs)
9. [What it gains](#9-what-it-gains)
10. [The path back](#10-the-path-back)
11. [Work items](#11-work-items)
12. [Open questions](#12-open-questions)

---

## 1. Why: the auth constraint

The MCP server fronts entitlement data, so an HTTP transport must
authenticate its callers. Two options exist today and neither is
available:

- **OAuth2 / Entra JWT.** The intended end state -- `StaticTokenVerifier`
  swapped for a JWKS validator in the same `TokenVerifier` slot
  (entra_auth_guide.md, and the to-do item in CLAUDE.md). The infra is
  work in progress and not ready.
- **Static bearer tokens.** Implemented and working (`MCP_AUTH=static`,
  `MCP_AUTH_TOKENS=name:token`), but ruled out as a deployment option.

With both HTTP paths closed, the server cannot be exposed over the
network at all. The remaining transport is the one that has no network
surface to protect.

## 2. Why stdio removes the problem entirely

stdio is not "auth turned off". It is a transport with **no HTTP layer
to authenticate**. The server runs as a child process of the agent,
speaking JSON-RPC over the pipe pair it was started with. There is no
port, no listener, and nothing for a third party to connect to.

The code already says this explicitly. `mcp-server/auth.py`:

> stdio transport has no HTTP layer, so auth does not apply there (the
> server is a child process of whoever already has local access).

and `server.py` only builds a verifier for HTTP transports:

```python
if MCP_TRANSPORT in ("sse", "streamable-http"):
    _token_verifier = build_token_verifier()   # fails closed without tokens
```

So on this path `MCP_AUTH` and `MCP_AUTH_TOKENS` are simply unused. No
token to mint, store in Key Vault, rotate, or leak. The security
boundary becomes the container boundary: whoever can execute inside the
container can already call the tools, which is exactly the local-access
assumption stdio is built on.

The client side is equally ready -- `_get_mcp_server_config()` already
has the branch:

```python
if transport == "stdio":
    command = os.environ.get("MCP_SERVER_COMMAND")
    args = os.environ.get("MCP_SERVER_ARGS", "").split()
    return {server_name: {"command": command, "args": args, "transport": "stdio"}}
```

## 3. Why ONE container, not one pod

A pod with two containers is the more idiomatic Kubernetes answer, and
it is the wrong one here. Containers in a pod share a network namespace
and can share volumes -- but each has its OWN filesystem and, by
default, its own PID namespace.

stdio needs neither of those things shared. It needs the agent process
to **exec the server binary and own the resulting child process**, so
that the pipes connecting them are the parent-child pipes the transport
is defined over. That requires the `mcp-docs-server` entry point to be
present in the agent's own filesystem and launchable by it.

Two containers in a pod could only talk over the shared network
namespace -- i.e. HTTP on localhost -- which puts us straight back at
the problem in section 1. (`shareProcessNamespace: true` lets containers
SEE each other's processes; it does not let one exec a binary that lives
in the other's image.)

Hence one image carrying both projects, and one process tree:

```
container
  +-- agent-api (uvicorn, PID 1-ish)
        +-- mcp-docs-server            <- child process, stdio pipes
```

## 4. Two blocking findings

Both were found by reading the installed libraries, and both must be
fixed before this design works at all.

### 4.1 The MCP server logs to stdout, which IS the protocol channel

`server.py` configures logging as:

```python
handlers=[logging.StreamHandler(sys.stdout)]
```

Under stdio, stdout carries the JSON-RPC message stream. Every log line
-- including the per-tool `took=ms` audit lines from P0 -- would be
interleaved into that stream and corrupt it. This is a hard protocol
break, not a cosmetic issue.

**Fix:** send logs to `sys.stderr`. stderr is inherited by the child and
can be captured by the parent or the container runtime without touching
the protocol. This is safe on every transport: HTTP transports do not
use stdout for anything either, so the change is unconditional and needs
no branching.

### 4.2 A session is created PER TOOL CALL, which would spawn a server per call

`langchain_mcp_adapters/tools.py` builds each tool so that:

```python
if session is None:
    # If a session is not provided, we will create one on the fly
    async with create_session(effective_connection, ...) as tool_session:
        await tool_session.initialize()
        ...
```

and both call sites use exactly that path:

```python
client = MultiServerMCPClient(mcp_config)
tools = await client.get_tools()        # agnes_agent_graph.py:1099, agent_api.py:167
```

Over streamable-http this is harmless and intended -- a fresh HTTP
session per call is why `MCP_STATELESS_HTTP=true` exists. Over stdio,
`create_session` **spawns the server subprocess**. Every tool call would
start a new `mcp-docs-server`, which at import time runs `DocIndex(...)`
(PDF extraction) and `CsvStore(...)` (DuckDB open) before answering.

P0's warm start softens this -- `MCP_DB_PATH` persists the DuckDB file,
so a re-spawn skips ingestion (0.04 s warm vs 10.2 s cold) -- but the
interpreter start, the imports, and the PDF extraction are paid every
time, because persisting extraction is still open work (P1.2). On a turn
with 2-4 tool calls that is seconds of pure waste, repeated.

**Fix:** hold ONE session for the process lifetime. The library
documents this shape in its own error text:

```python
client = MultiServerMCPClient(...)
async with client.session(server_name) as session:
    tools = await load_mcp_tools(session)
```

The session is an async context manager, so it has to be entered when
the service starts and exited at shutdown, rather than inside a
function that returns. In `AgentService.create()` that means holding the
context open on the service object (an `AsyncExitStack` is the tidy way)
and closing it in the FastAPI lifespan's shutdown half. The CLI needs
the same treatment around its loop.

One subprocess then lives for the life of the agent process. With the
one-uvicorn-worker-per-pod rule from aks.md section 8, that is exactly
one MCP server per pod.

## 5. Code changes

| Change | File | Why |
|---|---|---|
| Logging to stderr | `mcp-server/.../server.py` | 4.1 -- stdout is the protocol channel |
| Persistent session + `load_mcp_tools` | `agent_api.py`, `agnes_agent_graph.py` | 4.2 -- else one subprocess per tool call |
| Session lifecycle on the service | `agent_api.py` | enter on create, exit on lifespan shutdown |
| Subprocess supervision | `agent_api.py` | if the child dies the session is dead; decide crash vs restart (section 12) |

Config becomes:

```
MCP_TRANSPORT=stdio
MCP_SERVER_COMMAND=uv
MCP_SERVER_ARGS=run --no-sync mcp-docs-server
```

`MCP_SERVER_URL`, `MCP_SERVER_TOKEN`, `MCP_AUTH`, `MCP_AUTH_TOKENS` and
`MCP_STATELESS_HTTP` all become unused on this path. Leave them in
`.env.example` -- they are the return path (section 10).

The MCP server keeps reading its own env (`MCP_DOCS_DIR`, `MCP_DB_PATH`,
`MCP_DATA_SOURCE`, `MCP_PARQUET_SOURCES`, ...). A child process inherits
the parent's environment, so one env block in the pod spec configures
both halves.

## 6. The image and the entry point

One image, both projects. The two-step `uv sync` pattern from the
existing Dockerfiles is kept per project, so dependency layers still
cache independently:

```
/app/mcp-server/     <- pyproject, uv.lock, src/, docs/ (PDFs + JSONs)
/app/agent-client/   <- pyproject, uv.lock, src/
```

The agent is the entry point; `MCP_SERVER_ARGS` must resolve to the MCP
server's environment, not the agent's. Simplest is an absolute
invocation (`uv run --no-sync --project /app/mcp-server mcp-docs-server`)
rather than relying on the working directory.

Both projects currently pin python 3.12 via `.python-version`
(agent-client resolves to 3.11 locally -- worth unifying while the
images merge; see section 12).

## 7. Chart changes

The MCP tier stops being a workload and becomes a process. Removed:

- `mcp-deployment.yaml` -- no separate Deployment
- `mcp-service.yaml` -- nothing to reach over the network
- `hpa-mcp.yaml` -- MCP no longer scales independently (section 8)
- the MCP half of `pdb.yaml` and all of `networkpolicy.yaml` -- there is
  no agent -> mcp hop left to police

Moved onto the agent Deployment:

- the `stage-parquet` initContainer and the `/data` emptyDir
- the `/duckdb` emptyDir for `MCP_DB_PATH`
- the MCP env block, merged into the agent's ConfigMap

Key Vault shrinks from five secrets to three -- `mcp-server-token` and
`mcp-auth-tokens` are no longer used.

## 8. What this costs

Stated plainly, because these are real losses and the reason section 10
exists.

**Independent scaling of the MCP tier disappears.** aks.md section 4
sizes MCP at 2 replicas plus a CPU HPA specifically because BM25 scoring
and fuzzy regex are GIL-bound: more pods was the GIL workaround. Now the
tool workload scales only with the agent, which is sized for IO-bound
waits on Azure OpenAI. The MCP subprocess does have its own interpreter
and its own GIL, so agent and tools do not contend for one lock -- but
they do share the container's CPU limit, so the pod must now be budgeted
for both (roughly the sum of the two tiers' current requests).

**Per-pod data duplication.** Every agent pod stages its own parquet copy
and builds its own DuckDB file: measured ~108 MB RSS at 5M rows, plus the
file on disk, per pod.

**Cold start compounds.** Agent readiness now covers parquet staging,
subprocess boot, PDF extraction and the DuckDB open. The agent's
`startupProbe` (currently 30 x 5 s) will need re-measuring and probably
raising.

**The MCP server becomes private, and that forecloses a stated goal.**
aks.md section 12 is about letting OTHER agents use these tools, and you
are actively setting up a Copilot Studio agent to do exactly that. A
subprocess with no listener cannot serve anyone but its parent. This
design and that goal are mutually exclusive while it is in force -- which
makes it a deliberate trade for the duration, not a permanent shape.

**Audit identity degrades.** `_caller()` derives the agent name from the
bearer token; with no token every tool log line becomes
`caller=anonymous`. Per-agent attribution was already thin, and this
removes what there was.

## 9. What it gains

- **The auth problem disappears** rather than being worked around. No
  token to mint, store, rotate, or leak, and no open port fronting
  entitlement data.
- **Fewer moving parts**: five Kubernetes objects removed, two Key Vault
  secrets removed, one less failure mode between agent and tools.
- **No network hop** -- worth single-digit milliseconds per tool call,
  which is negligible against a turn, but it is not negative.
- **The seam survives.** Transport is an env var; nothing in the graph,
  the prompts, or the tool contracts knows which one is in use.

## 10. The path back

When Entra/OAuth lands, reversing this is a config change plus
re-splitting the deployment:

1. Set `MCP_TRANSPORT=streamable-http` and `MCP_SERVER_URL`; provide the
   agent's token.
2. Restore the MCP Deployment, Service, HPA, PDB and NetworkPolicy from
   the chart history on `AKS-DEPLOY`.
3. Rebuild as two images.

The persistent-session change from 4.2 should STAY -- it is correct on
both transports and removes a per-call session setup over HTTP too. The
stderr logging change should stay likewise.

## 11. Work items

1. **stderr logging** in `mcp-server/.../server.py`.
   - [ ] done
2. **Persistent MCP session** in `agent_api.py` and
   `agnes_agent_graph.py`, with lifecycle tied to service start/shutdown.
   - [ ] done
3. **Subprocess supervision** decision + implementation (section 12).
   - [ ] done
4. **Combined Dockerfile**, both projects, agent as entry point.
   - [ ] done
5. **Chart**: fold MCP into the agent Deployment, delete the MCP tier
   objects, merge env and volumes.
   - [ ] done
6. **Tests**: a stdio-transport test that asserts one subprocess serves
   many tool calls (the 4.2 regression), and that no log output reaches
   stdout (the 4.1 regression).
   - [ ] done
7. **Measure**: cold start, per-tool latency vs the HTTP path, pod
   memory with both halves resident.
   - [ ] done

## 12. Open questions

- **Supervision.** If the MCP child dies, the held session is dead and
  every subsequent tool call fails. Crash the pod and let Kubernetes
  restart it (simple, honest, loses in-flight turns) or restart the
  child in-process (kinder, more code, risks a half-initialised store)?
  The tcpSocket liveness probe will NOT catch this -- the agent's port
  stays open with a dead child.
- **Where does the child's stderr go?** It should reach container logs so
  the per-tool `took=ms` lines stay queryable in Container Insights.
- **Python version unification** -- both projects should resolve to 3.12
  in one image.
- **Does `/health` need to check the child?** Today it pings Redis. A
  dead MCP child is just as fatal to a turn, and readiness is the right
  place to surface it.
- **Scanner and CLI** share `_get_mcp_server_config()` and get stdio for
  free, but each spawns its own server -- fine for batch runs, worth
  knowing before running the scanner alongside a live API.

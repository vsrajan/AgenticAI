# Resource agent only, sessions in memory, one pod

Branch: `ResourceAgentOnly`, based on `stdio`.

Two constraints from the infrastructure, and what they cost. Both are
reversible; section 6 is the way back.

## Contents

1. [What changed](#1-what-changed)
2. [Constraint A -- no Redis, so one pod](#2-constraint-a----no-redis-so-one-pod)
3. [Constraint B -- one specialist](#3-constraint-b----one-specialist)
4. [The graph](#4-the-graph)
5. [What this costs](#5-what-this-costs)
6. [The path back](#6-the-path-back)
7. [What was deleted](#7-what-was-deleted)

## 1. What changed

| Before | Now |
|---|---|
| Router + 3 specialists + handoff, 8 nodes | 2 nodes: `resource_agent` + `resource_tools` |
| `AgentState(messages, active_agent)` | plain `MessagesState` |
| Sessions in Redis (P3.1) or memory | memory only |
| 2-3 replicas | 1 |
| `agnes_agent_graph.py` is the live module | `agnes_agent.py` is; the old one is frozen |
| `scan-cli` batch scanner | deleted |

The MCP server is untouched: it still serves all 12 tools over stdio as
a child process of the agent container
([single-container-stdio.md](single-container-stdio.md)).

## 2. Constraint A -- no Redis, so one pod

There is no Redis on the infrastructure, so P3.1's shared session state
has nothing to point at. `REDIS_URL` is unset, which puts `AgentService`
back on exactly the in-process path it had before P3.1: an
`InMemorySaver` checkpointer, an in-process session registry with the
TTL sweeper, and `asyncio.Lock` turn locks.

That forces `replicas: 1`. With sessions in one pod's heap, a second
replica behind the same Service would answer a user's next message from
a pod that has never heard of their session: 404, history gone. The
chart refuses to render the combination rather than leaving it to a
comment:

```
agent.replicas > 1 needs agent.redis.enabled: sessions live in the
pod's memory, so a second replica cannot serve a session the first
one owns
```

Three consequences worth stating plainly.

**Deploys are downtime.** The Deployment uses `strategy: Recreate` when
Redis is off. The default `RollingUpdate` would briefly run a surge pod
alongside the old one, both behind one Service, splitting sessions
between them. Recreate trades that for an outage of one cold start --
parquet staging, PDF extraction, the DuckDB open -- plus up to the 300s
termination grace while in-flight turns drain. Every live conversation
is lost, which is true of any restart in this mode. The web client
already handles it: a 404 makes it create a fresh session and retry.

**A readiness failure is a total outage.** At one replica, failing
`/health` empties the Service and the whole API 503s. `/health` pings
the MCP child, and the child runs its tools on its own event loop, so a
probe issued during a long tool call waits for it. All three probes
therefore set `timeoutSeconds` explicitly -- the Kubernetes default is
1s, which is far too tight for a round-trip that can queue behind a
DuckDB query.

**Memory is bounded only by the session cap.** Everything lives in one
heap, and `KEEP_LAST_N_MSGS` trims only what is SENT to the model -- the
stored history of a live session is not trimmed. `AGENT_API_MAX_SESSIONS`
drops from 500 to 150 to match, with the 60 minute idle TTL unchanged.
Re-set both against a measured pod.

### The stuck-turn hole this opened

The Redis turn lock was `SET NX PX`: it expired on its own. An
`asyncio.Lock` does not. A turn parked on an await that never returns
(neither the LLM nor the MCP session carries a timeout yet) would have
held its session's lock for the life of the process -- and because the
sweeper never evicts a session whose turn is running, that session and
its history could never be reclaimed either. One wedged turn, one
permanently dead session and a leaked slot.

Closed in `agent_api.py`:

- `_Session.locked_at` records when the turn took the lock;
  `held_too_long()` says whether it has been held past
  `AGENT_API_LOCK_TIMEOUT_SECONDS`.
- Eviction (both the TTL sweep and the LRU cap) treats a lock held past
  that as stuck rather than running, so the session becomes reclaimable.
- `_turn_lock()` bounds the WAIT: a second message on a wedged session
  raises `TurnBusyError` -> 503 on the buffered endpoint, an `error`
  event in-band on the SSE endpoint, instead of hanging silently.

What this does NOT do: cancel the stuck turn. It stays parked on its
await. Bounding the LLM and MCP calls themselves is the real fix and is
still open (the LLM-resilience item in CLAUDE.md; `read_timeout_seconds`
is the one-liner on the MCP side).

## 3. Constraint B -- one specialist

Only the Resource specialist is in this iteration. The knowledgebase and
quality agents are not.

The MCP server still exposes all 12 tools -- `build_graph` binds only
the 8 in `RESOURCE_TOOL_NAMES`. Binding is what makes this a
resource-only agent; the other four are loaded and left invisible to the
model. A prompt asking the model not to use a tool it can see is not the
same thing.

With one specialist there is nothing to route to, so the router is gone
along with its three routing tools, and `hand_off_to_router` with it.
That removes a full LLM round-trip from the first turn of every
conversation.

Two jobs the router used to do moved into `RESOURCE_PROMPT`'s new SCOPE
section:

- greetings and general chat, which the router answered directly;
- declining process, policy and how-to questions. This is the one real
  behavioral risk in the change. The agent now has no documentation
  tools AND nowhere to hand off, so the failure mode is that it answers
  from the model's prior knowledge -- a confident, uncited, plausible
  guess about this firm's process, which the user cannot tell apart
  from a sourced answer. The prompt says to name the limitation and
  stop, in those words.

The three handoff passages were deleted from the prompt, including the
"ask me separately so I can route it to the right specialist" line,
which promised something nothing can now do. The ~200 lines of Strategy
0 / Step R / peer-lookup content are unchanged.

## 4. The graph

```
START -> resource_agent <-> resource_tools -> END
```

The conditional edge reads the last message: tool calls go to the tool
node, anything else ends the turn. Tool results always return to the
model. That is the whole graph -- a ReAct loop, which is what one
specialist reduces to.

`ToolNode` is used directly. The old `_make_tool_node` existed only to
reconcile domain tool calls arriving alongside `hand_off_to_router`,
which cannot happen now.

`build_graph`, `_get_mcp_server_config`, `_NODE_PHASES` and
`_write_stream_entry` keep their names and signatures, so `agent_api.py`
needed a one-line import change and nothing else. `_NODE_PHASES` is
the map the API turns node starts into SSE phase events with, so its
keys must stay in step with the node names.

All three MCP transports are kept, with stdio the default.

## 5. What this costs

- **No documentation or quality answers at all.** Not degraded --
  absent. Users who ask get told so.
- **Sessions die on restart and on every deploy**, and there is exactly
  one pod to lose.
- **No horizontal scale.** Azure OpenAI quota was already the ceiling
  on turn latency, but there is now no answer at all to more concurrent
  users than one pod can hold.
- **No node-drain protection.** The PDB is off, because a budget over a
  single pod either blocks drains outright (`minAvailable: 1`) or
  permits exactly what would happen anyway. The template now uses
  `maxUnavailable: 1` for when replicas return.
- **The batch scanner is gone** -- it existed to run incidents through
  the knowledgebase agent.
- **Two copies of `RESOURCE_PROMPT` exist.** The live one is in
  `agnes_agent.py`. The copy inside the frozen `agnes_agent_graph.py`
  will rot; that is the price of keeping the multi-agent shape readable
  as a reference.

## 6. The path back

**Redis, replicas > 1**: set `agent.redis.enabled: true`, put
`redis-url` in the environment's Key Vault, raise `agent.replicas`. The
guard, the `Recreate` strategy and the absent `REDIS_URL` are all
conditioned on that one value, and `redis_state.py` and its 8 tests
were kept for exactly this. Turn the PDB on at the same time.

**More specialists**: `agnes_agent_graph.py` still holds the router,
both other prompts, the handoff node and the mixed-call tool node. The
shape to restore is that file's `build_graph`; the prompt to port
forward is this branch's, minus its SCOPE section. The scanner is in
git history at this branch's base.

Nothing about the MCP server, its tools, the transports or the
container layout has to change in either direction.

## 7. What was deleted

| Path | Lines | Why |
|---|---|---|
| `src/ease_clients/utils/scanner.py` | 224 | knowledgebase-only batch engine |
| `src/ease_clients/scanner_cli.py` | 105 | its CLI |
| `src/ease_clients/utils/incident_sources.py` | 215 | its input adapters |
| `data/Incidents.csv` | -- | its sample data |
| `docs/plan-incident-resolution.md` | -- | its plan doc |
| `scan-cli` entry point | 1 | pyproject |

The scanner could not simply be left behind: it invoked the graph with
`active_agent="knowledgebase_agent"`, and a `MessagesState` graph
silently ignores an unknown state key. It would have run every incident
through the resource agent under the resource prompt and produced
plausible, wrong output with no error.

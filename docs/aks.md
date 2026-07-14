# Running the stack on managed AKS -- deployment analysis, tuned for E2E performance

Analysis of how to run the MCP server and the agent API (+ agent
graph) in a managed AKS cluster, written against the full `P1.1`
stack: streamable-http transport -> P0 DuckDB data layer -> P3.1 Redis
session state -> P1.1 page-level retrieval. Topology diagram:
[06_aks_topology.excalidraw](06_aks_topology.excalidraw) (section 6 of
[architecture_excalidraw.md](architecture_excalidraw.md)).

The one-paragraph summary: both tiers are now horizontally scalable
-- the MCP server since the stateless streamable-http switch, the
agent API since P3.1 -- so this is a genuinely elastic two-tier
deployment behind a plain load balancer. The cluster layout below is
close to optimal on day one and buys single-digit milliseconds plus
operational resilience; the levers that own user-visible latency and
total throughput are the Azure OpenAI quota and the tokens each turn
consumes. Tune the topology once; keep tuning the token axis.

## Contents

1. [What the current stack already provides](#1-what-the-current-stack-already-provides)
2. [Target topology](#2-target-topology)
3. [Agent tier](#3-agent-tier)
4. [MCP tier](#4-mcp-tier)
5. [Redis](#5-redis)
6. [E2E latency budget, hop by hop](#6-e2e-latency-budget-hop-by-hop)
7. [Concrete starting point](#7-concrete-starting-point)
8. [Deployment hygiene](#8-deployment-hygiene)
9. [What AKS deliberately does not solve](#9-what-aks-deliberately-does-not-solve)
10. [Rollout order](#10-rollout-order)

---

## 1. What the current stack already provides

Every AKS-blocking property has already been engineered out, feature
by feature:

| Property AKS needs | Provided by |
|---|---|
| MCP replicas answer any request (no sticky routing) | streamable-http with `MCP_STATELESS_HTTP=true` (default) |
| agent replicas share one session pool | P3.1: `REDIS_URL` moves sessions, turn locks, and checkpoints into shared Redis |
| turn serialization across replicas | P3.1 distributed lock (SET NX PX, `AGENT_API_LOCK_TIMEOUT_SECONDS`) |
| readiness signal that includes dependencies | `/health` pings Redis and returns 503 when it is unreachable |
| cheap MCP pod rebuilds | P0: DuckDB cache rebuilt from source, 10.2 s cold ingest at 5M rows |
| small per-turn payloads over the cluster network | P1.1: page-level reads, ~40x smaller knowledgebase tool results |
| per-agent credentials on the MCP hop | bearer-token auth (MCP_AUTH_TOKENS), TokenVerifier slot ready for Entra |

## 2. Target topology

```
                         Internet
                            |
              Ingress (TLS; SSE-tuned for /stream)
                            |  plain round-robin -- NO stickiness
        +-------------------+--------------------+
        |   agent-api Deployment, replicas 2-3   |----> Azure OpenAI
        |   (REDIS_URL set; stream file off)     |      (same region;
        +-------------------+--------------------+       quota = the wall)
                            | ClusterIP :8000 + NetworkPolicy
        +-------------------+--------------------+
        |  mcp-server Deployment, replicas 2+HPA |----> Azure SQL/Postgres
        |  (streamable-http, stateless)          |      (DatabaseSource, P0 s11)
        +----------------------------------------+
                            |
              Azure Cache for Redis (rediss://, same region)
              Azure Key Vault (CSI: API token, MCP tokens, REDIS_URL)
```

Both Deployments live in one namespace; co-locating them delivers the
old P2.2 recommendation (network co-location) as a side effect of the
deployment shape.

## 3. Agent tier

**replicas: 2-3 from day one.** P3.1 made this legal: any pod serves
any session, the distributed lock keeps turns serialized, the load
balancer stays dumb. Two replicas is the availability floor; add a
third for headroom, and see section 7 before adding more -- extra
agent pods against a saturated Azure OpenAI quota just parallelize
429 errors.

**Rolling deploys stop being session-wiping events.** Sessions live
in Redis, so a deploy loses only the turns in flight at that moment
(clients retry), never the conversations. Set
`terminationGracePeriodSeconds` about equal to the lock timeout
(300 s) with a preStop sleep so in-flight turns finish draining.

**Memory profile is flat now.** Checkpoints moved to Redis, so the
old MemorySaver growth risk is gone in this mode. 512Mi-1Gi limits
are realistic, and an OOM restart is both unlikely and cheap (nothing
of value lives in process memory).

**Probes -- one nuance to get right.**

- readinessProbe: `GET /health`. It pings Redis and 503s when Redis
  is unreachable, which is exactly what readiness should do -- a pod
  that cannot reach the session store must leave rotation.
- livenessProbe: NOT `/health`. A Redis blip would fail liveness on
  every pod simultaneously and restart the whole tier, and restarting
  pods does not fix Redis. Use a tcpSocket check on the app port. (A
  dedicated `/livez` endpoint that skips the Redis ping is a one-line
  follow-up if http liveness is preferred.)

**Scaling signal.** Turns are I/O-bound waits (the pod spends ~95% of
a turn awaiting Azure OpenAI), so CPU-based HPA under-measures load
badly. Pragmatic v1: fixed replicas. If autoscaling is wanted later,
scale on in-flight turns / requests per pod (KEDA), not CPU.

**Config for prod pods:**

| Setting | Value | Why |
|---|---|---|
| `REDIS_URL` | from Secret | the multi-instance switch; rediss:// for Azure Cache |
| `AGENT_API_STREAM_FILE` | "" (empty) | the per-node debug log does sync writes on the event loop for every node of every turn -- a debugging feature, disable in prod (P4) |
| `AGENT_API_LOCK_TIMEOUT_SECONDS` | 300 | see P3.1.md section 14 |
| `AGENT_API_CORS_ORIGINS` | the real frontend origin | not `*` in prod |

## 4. MCP tier

**replicas: 2 + HPA on CPU.** The hot paths that saturate a pod --
BM25 scoring, fuzzy regex scans -- are GIL-bound Python, so one pod
effectively has one core of Python no matter the threadpool size.
More replicas is the clean way around the GIL: 3 pods = ~3x concurrent
tool-call throughput, zero code change. DuckDB itself releases the
GIL, so mixed workloads scale even better. Stateless streamable-http
(already the default) means a plain ClusterIP service spreads calls
with no affinity anywhere.

**Cold start is the HPA caveat.** A fresh pod ingests the DuckDB
cache (measured 10.2 s at 5M rows) and still extracts PDFs at boot
(P1.2 not yet done). So: gate readiness on startup completion, keep
`minReplicas: 2`, and give the HPA a scale-up stabilization window
(~60 s). Treat HPA as burst absorption, not instant elasticity. P1.2
is the item that cheapens pod cold starts -- schedule it before
leaning hard on autoscaling.

**Data:**

- DuckDB cache on an `emptyDir` -- it is a CACHE, rebuilt from source;
  the per-pod fingerprint sweeper (`MCP_DATA_REFRESH_MINUTES`) keeps
  it fresh. Replicas can disagree about freshness for at most one
  sweep interval after a source change -- harmless for this workload.
- Production rows come from Azure SQL / Postgres via DatabaseSource
  (the P0 section 11 runbook); pods pull on boot and on sweep.
- PDFs: bake into the image (immutable, fast, the default choice) or
  mount an Azure Files share read-only if documents must change
  without a redeploy.

**Sizing:** measured 108 MB RSS at 5M rows -> 512Mi limits are
comfortable; 0.5 CPU request with a 1-2 CPU limit for scan bursts.

**Security shape unchanged:** ClusterIP only (never an external IP),
NetworkPolicy admitting only agent pods to :8000, `MCP_AUTH_TOKENS`
from a Secret, bearer auth exactly as on VMs.

## 5. Redis

Azure Cache for Redis, **Standard tier or above** for the SLA and
replication. The P3.1 plain-Redis design (no RedisJSON/RediSearch)
means ANY tier works functionally -- that was the point of the custom
saver -- so the tier choice is purely an availability decision, never
a feature one. Same region as the cluster, VNet-injected or behind a
private endpoint; `rediss://` URL (TLS, port 6380) in a Kubernetes
Secret via the Key Vault CSI driver.

One genuinely new thing to watch: checkpoint volume now lands in
REDIS memory. LangGraph writes the full conversation state per
superstep; the session TTL bounds total growth, but nothing prunes
superseded checkpoints yet (P3.2). Budget cache memory for
(active sessions x conversation size) and put P3.2 on the roadmap
before very-long-conversation workloads.

## 6. E2E latency budget, hop by hop

| Hop | Cost per turn | Tuning |
|---|---|---|
| client -> ingress | ~ms | TLS at the ingress. For the `/stream` route: proxy buffering OFF, read-timeout >= 300 s, no gzip on SSE. This is the #1 "works locally, breaks on AKS" trap for this app |
| ingress -> agent pod | ~0 | plain round-robin; nothing to configure -- the P3.1 dividend |
| agent -> Azure OpenAI | seconds; ~95% of the turn | same-region deployment + private endpoint shaves ms on each of the 2-6 calls per turn. The REAL levers are token levers: P1.1 already cut knowledgebase tool results ~40x; KEEP_LAST_N_MSGS tuning; P1.3 prompt caching next. These raise turns-per-quota -- no pod count can |
| agent -> MCP (x2-5 calls) | low ms | co-location makes the stateless per-call handshake negligible (~1-3 ms in-cluster). P2.1 (persistent MCP session) is now marginal -- deprioritized |
| agent -> Redis | ~5-15 ms | a turn makes roughly 8-15 pipelined round-trips (require, touch, lock acquire/release, one pipelined write per superstep, reads). Measured 2.6 ms per turn on localhost; same-region Azure Cache adds ~0.5-1 ms per round-trip, hence the projection. Re-measure once deployed; still noise vs the LLM |
| MCP -> DuckDB | ~25 ms measured (5M-row filtered group-by) | already done (P0); 167 ms for fuzzy regex is the slow case |

Reading of the table: the topology work buys milliseconds and
resilience; the quota and the per-turn token count own everything the
user actually feels. Concretely, system-wide throughput is
`TPM quota / tokens-per-turn` (a turn re-sends prompts and history
across 2-6 LLM calls, ~10-30k tokens): a 100k-TPM deployment
sustains ~4-8 turns/minute TOTAL regardless of pod counts; 450k TPM
~20-40. Size quota (or PTU) to the user population first, then size
agent replicas to concurrency.

## 7. Concrete starting point

| Component | Setting |
|---|---|
| agent-api | 2 replicas fixed, 0.5 CPU / 1Gi, readiness `/health`, liveness tcpSocket, grace 300 s |
| mcp-server | 2 replicas, HPA 2->6 at 70% CPU with 60 s scale-up window, 0.5 CPU / 512Mi, startup-gated readiness |
| Redis | Azure Cache Standard C1, same region, rediss:// via Secret |
| ingress | TLS; /stream route: buffering off, 300 s read timeout |
| spread | pod anti-affinity across zones for both Deployments; PDB minAvailable 1 each |

Then load-test against the actual Azure OpenAI quota BEFORE adding
agent replicas beyond 3.

## 8. Deployment hygiene

- **Secrets**: Key Vault CSI for `AGENT_API_TOKEN`, `MCP_AUTH_TOKENS`
  / `MCP_SERVER_TOKEN`, and `REDIS_URL` (it embeds the access key --
  Secret, never ConfigMap; never logged, the code logs host only).
- **Workload identity**: pods get an Entra identity; Azure OpenAI can
  then use keyless auth (the OpenAI SDK accepts Entra tokens), which
  retires `AZURE_OPENAI_API_KEY` entirely. Slots into the existing
  Entra roadmap (API auth, MCP auth, Redis Entra auth later).
- **Observability**: both services log to stdout -> Container
  Insights. The per-tool `took=ms` lines (P0) and per-node/per-turn
  timing (P0 agent side) become queryable fleet-wide. The stream FILE
  stays off in prod (section 3).
- **One uvicorn worker per pod**, always -- scaling unit is the pod.
  In-process mode with replicas > 1 is the one forbidden combination
  (private session pools behind one load balancer); rollback from
  Redis mode therefore always pairs "unset REDIS_URL" with "scale to
  1" in the same action.

## 9. What AKS deliberately does not solve

| Ceiling | Owner | Next step |
|---|---|---|
| Azure OpenAI TPM quota | subscription quota / PTU | raise quota; land P1.3 (prompt caching) to raise turns-per-quota |
| per-turn token volume | prompts + history size | KEEP_LAST_N tuning; P1.3 hysteresis trimming |
| MCP pod cold start | per-pod PDF extraction + ingest | P1.2 (persist extraction/index) |
| checkpoint growth in Redis | LangGraph full-state-per-superstep | P3.2 (prune superseded checkpoints) |
| Vector branch per-pod re-embedding (future) | embeddings cost per fresh pod | embed in CI, ship the DuckDB file as an artifact + initContainer (already noted in vector.md context) |
| http liveness endpoint | `/health` depends on Redis | optional `/livez` that skips the ping |

## 10. Rollout order

1. Containerize both services (Dockerfiles; no code changes needed --
   config is already fully env-driven).
2. Deploy the section 7 starting point with `MCP_TRANSPORT=
   streamable-http`, `REDIS_URL` set, stream file off.
3. Verify the P3.1 rig semantics in-cluster: kill an agent pod
   mid-conversation -> session continues on the other pod; watch for
   the lock-expiry warning in logs (none expected).
4. Point DatabaseSource at the production database (P0 section 11).
5. Load-test to the quota ceiling; only then revisit replica counts,
   PTU, and P1.3.

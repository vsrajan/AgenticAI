# Local container smoke test -- podman on one RHEL box

The step between "it works as processes on my RHEL machine" and "deploy
it to AKS". Everything here runs on ONE host with no registry, no
cluster, and no Docker daemon: podman builds the image into its local
store, and you run the whole stack -- agent + MCP child, Redis, nginx
-- exactly as the cluster will, minus the cluster.

This is the highest-value hour in the whole deployment path. The first
time this application runs inside a container should happen on a
machine you control, where a rebuild costs seconds, not inside a shared
cluster where the same mistake costs a pipeline run and a support
ticket. Path assumptions, SELinux labels, UID mappings, and missing
files all surface here.

Companion docs: [deploy.md](deploy.md) section 4.1 (the short version
of this, in context), [single-container-stdio.md](single-container-stdio.md)
(why one container carries both halves), [P3.1.md](P3.1.md) section 9.1
(the nginx rig this reuses), [aks_guide.md](aks_guide.md) (the
Kubernetes concepts these commands are rehearsing).

## Contents

1. [What this validates, and what it cannot](#1-what-this-validates-and-what-it-cannot)
2. [Prerequisite: does podman exist here](#2-prerequisite-does-podman-exist-here)
3. [Prepare the host](#3-prepare-the-host)
4. [Build the image](#4-build-the-image)
5. [Inspect the image before you run it](#5-inspect-the-image-before-you-run-it)
6. [First run: one container, in the foreground](#6-first-run-one-container-in-the-foreground)
7. [The full rig: Redis, two containers, nginx](#7-the-full-rig-redis-two-containers-nginx)
8. [Verification checklist](#8-verification-checklist)
9. [Measurements to carry into the chart](#9-measurements-to-carry-into-the-chart)
10. [Troubleshooting](#10-troubleshooting)
11. [Cleanup](#11-cleanup)

---

## 1. What this validates, and what it cannot

**Validates:**

| Claim | How this rig proves it |
|---|---|
| The image builds from committed source alone | `podman build` from a clean tree |
| Both projects coexist on one interpreter | the container starts and loads 12 tools |
| The agent can spawn the MCP server as a child | `podman top` shows it |
| Configuration reaches the child | `Data store ready: 2 datasets` |
| One subprocess serves many tool calls | the child count stays at 1 across turns |
| Readiness and liveness differ meaningfully | stop Redis -> `/health` 503, `/livez` 200 |
| A dead child is invisible to a port check | kill it -> the port still listens, `/livez` 503 |
| Sessions survive instance switching | two containers behind nginx, one conversation |
| SSE survives a proxy | tokens trickle through nginx, not one lump |
| Startup budget and memory footprint | measured, section 9 |

**Cannot validate** -- these need a real cluster, and pretending
otherwise is how surprises reach prod:

- Key Vault CSI driver and workload identity (no Azure identity here)
- the parquet-staging initContainer (you stage by hand instead)
- real ingress TLS, multi-zone spreading, HPA behaviour
- the probes actually causing a restart -- podman will not restart the
  container when `/livez` fails, because nothing here is watching it.
  That absence is itself instructive; see section 8.

## 2. Prerequisite: does podman exist here

```bash
command -v podman && podman --version
```

Expected: `podman version 5.6.0` or similar. If it is missing, install
it -- and note the same modular-metadata trap you hit with nginx and
redis, on a RHEL box wearing CentOS 8 repos:

```bash
sudo dnf install -y podman --setopt=centos-8-appstream.module_hotfixes=true
```

(Adjust the repo id to whatever `dnf repolist` shows.)

Remember the machine is a POD: `dnf` installs land on an ephemeral
container filesystem and vanish when the pod is recreated. Home
persists, so keep a bootstrap line in `~/.bashrc`:

```bash
command -v podman >/dev/null || sudo dnf install -y podman \
  --setopt=centos-8-appstream.module_hotfixes=true
```

**Confirm rootless mode and that your user namespace is set up.**
Rootless is the default and the right mode here -- no daemon, no root:

```bash
podman info --format '{{.Host.Security.Rootless}}'     # -> true
grep "^$(whoami):" /etc/subuid /etc/subgid
```

The `grep` must return a line from EACH file, e.g.
`youruser:100000:65536`. Those ranges are what lets a rootless
container have users other than you inside it -- and this image runs as
a non-root `app` user, so it needs them. If either file has no entry:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$(whoami)"
podman system migrate        # re-creates the user namespace
```

**Set the base image once.** Images come from the firm's internal
registry, not a public one. The Dockerfile takes it as a build arg with
a placeholder default, so set it here and reuse it in every command
below:

```bash
export BASE_IMAGE=registry.internal.example.com/python:3.12-slim   # REPLACE
```

It must carry Python 3.12 -- what `mcp-server/.python-version` pins --
because the Dockerfile sets `UV_PYTHON_DOWNLOADS=never` and will fail
loudly rather than quietly fetch a different interpreter.

A quick end-to-end check that the engine and the registry both work:

```bash
podman pull "$BASE_IMAGE"
podman run --rm "$BASE_IMAGE" python -V      # -> Python 3.12.x
```

Do this before anything else. It is the only registry the build
touches, and a pull failure here is a credentials or network problem,
not an application problem. If the registry needs a login:

```bash
podman login registry.internal.example.com     # REPLACE
```

**The build also needs a PyPI index.** `pip install uv` and both
`uv sync` runs resolve packages, so an internal container registry
usually implies an internal PyPI mirror too. If PyPI is unreachable
from this host, set `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` in the
Dockerfile before building -- there is a comment marking the spot.

## 3. Prepare the host

### 3.1 Stage the parquet exports

The container cannot see host paths -- they must be MOUNTED in, and the
paths in `MCP_PARQUET_SOURCES` are absolute CONTAINER paths on the
mounted volume, never host paths. This is the container edition of the
parquet lesson.

```bash
export AGNES_DATA=/projects/agnes-agent/mcp-server/mcp-data    # host side
ls -R "$AGNES_DATA" | head -20
```

You should see the Spark export layout -- each `.parquet` is a
DIRECTORY of part files, not a file:

```
export/Resource.parquet/part-00000-....parquet
export/Entitlement.parquet/part-00000-....parquet
```

Both export names are SINGULAR and capitalised (`Resource`,
`Entitlement`); the dataset names the agent sees are the plurals
(`Resources`, `Entitlements`), set on the left of each pair in
`MCP_PARQUET_SOURCES`. Getting the case wrong is a silent
`0 datasets`, because a glob that matches nothing is not an error.

`mcp-data` sits beside `docs/` inside `mcp-server/`, and both are
excluded from the build context by the root `.dockerignore` (`mcp-data`
and `*/mcp-data`) -- the exports are mounted at runtime, never baked.
If your `mcp-data` is one level up at `/projects/agnes-agent/mcp-data`
instead, change `AGNES_DATA` and everything below follows, since every
command references the variable.

### 3.2 Make the files readable by the container user

This is the single most common first failure, and the reason is worth
understanding rather than working around.

In rootless podman your host UID maps to **root inside the container**;
every other container UID maps into your subuid range. The image runs
as `app` (uid 1000), which maps to a high host subuid that owns nothing
on your filesystem. So the container can read your staged parquet only
if the files are world-readable and the directories world-executable:

```bash
chmod -R a+rX "$AGNES_DATA"
```

`a+rX` -- capital X -- adds execute only to directories and to files
that already had it, which is exactly what you want for a data tree.

Verify from inside a container before you build anything real:

```bash
podman run --rm -v "$AGNES_DATA":/data:z "$BASE_IMAGE" \
  sh -c 'id; ls -la /data/export'
```

### 3.3 SELinux: `:z` lowercase, not `:Z`

On SELinux-enforcing RHEL a bind mount needs a relabel suffix or every
read fails with EACCES. Which one matters:

- **`:z`** (lowercase) applies a **shared** label -- multiple containers
  may read the same host directory.
- **`:Z`** (uppercase) applies a **private, per-container** label. A
  second container relabels the same files again and **breaks the
  first**.

This rig runs two agent containers over one staged export, so it is
`:z` throughout. (deploy.md section 4.1 uses `:Z` because it describes
a single container -- both are correct in their own context.)

### 3.4 Redis

Leave Redis running as a host process, exactly as you already have it:

```bash
redis-server --port 6379 --daemonize yes
redis-cli -p 6379 ping        # -> PONG
```

Do not containerize it. In AKS, Redis is **outside** the cluster
(Azure Cache), so a host process on the container host is the more
faithful model -- it is an external dependency reached over TCP, which
is precisely what section 8's readiness test depends on.

### 3.5 A working directory for nginx

```bash
mkdir -p ~/lb/tmp
```

## 4. Build the image

Always build from committed source. A dirty tree gives you an image you
cannot reproduce or trace back to a SHA:

```bash
cd /projects/agnes-agent          # THE REPO ROOT
ls                                # -> agent-client  mcp-server  Dockerfile
git status                        # should be clean
git rev-parse --short HEAD        # the tag you would use in the chart
```

`/projects/agnes-agent` holds both project folders plus the root
`Dockerfile` and `.dockerignore`. That is the context the build needs.

```bash
podman build -t agnes:local -f Dockerfile \
  --build-arg BASE_IMAGE="$BASE_IMAGE" .
```

Two things about that command line matter:

- **The build context is `.` -- the repo root**, not a project
  directory. The Dockerfile copies from both `mcp-server/` and
  `agent-client/`, so a context rooted at either one cannot see the
  other. This is the most likely way to get `COPY failed: no such file
  or directory`.
- **One image, not two.** The agent runs the MCP server as a child
  process over stdio, which needs a parent-child process relationship
  and so a shared filesystem. Two containers in a pod share a network
  namespace, not a filesystem. The reasoning is
  [single-container-stdio.md](single-container-stdio.md) sections 2-3.

Expect four `uv sync` runs -- two per project: one for dependencies
right after the manifests are copied (`--no-install-project`, the heavy
layer, cached until the lockfile changes), then one after `src/` lands
that installs just the package. A first build is dominated by the base
image pull; rebuilds after a source-only edit take seconds.

```bash
podman images agnes
```

**Note on ignore files:** podman reads `.containerignore` if present,
otherwise `.dockerignore`. This repo ships `.dockerignore` at the root,
and it matters more than usual here -- the default `MCP_DB_PATH` puts
the DuckDB cache INSIDE `mcp-server/docs/`, so without the committed
exclusions `COPY mcp-server/docs/` would bake gigabytes of disposable
cache, or a real `.env`, into the image.

## 5. Inspect the image before you run it

Cheap, and it answers "what actually got baked in" definitively.

**The env the image sets** -- this is how the agent knows to spawn the
child:

```bash
podman inspect agnes:local --format '{{range .Config.Env}}{{println .}}{{end}}'
```

Expect `MCP_TRANSPORT=stdio`, `MCP_SERVER_COMMAND=uv`, and
`MCP_SERVER_ARGS=run --no-sync --project /app/mcp-server mcp-docs-server`.
The `--project` path is absolute on purpose: the child must resolve the
mcp-server environment regardless of the parent's working directory.

**Both projects are present, each with its own venv:**

```bash
podman run --rm agnes:local ls -la /app /app/mcp-server /app/agent-client
```

**Both packages import** -- the failure you spent an afternoon on
locally, caught in two seconds here:

```bash
podman run --rm agnes:local \
  uv run --no-sync --project /app/mcp-server \
  python -c "import mcp_docs_server as m; print(m.__file__)"

podman run --rm agnes:local \
  uv run --no-sync --project /app/agent-client \
  python -c "import ease_clients as m; print(m.__file__)"
```

**No `.env` was baked in** (configuration must arrive from outside):

```bash
podman run --rm agnes:local sh -c \
  'ls /app/agent-client/.env /app/mcp-server/.env 2>&1 || echo "GOOD: no .env in the image"'
```

**The PDFs are baked, the DuckDB cache is not:**

```bash
podman run --rm agnes:local ls /app/mcp-server/docs
podman run --rm agnes:local sh -c \
  'ls /app/mcp-server/docs/.mcp_data.duckdb 2>&1 || echo "GOOD: no duckdb cache baked"'
```

A locally built image includes whatever PDFs this machine has. A
CI-built image only has what is COMMITTED -- today, the two config
JSONs. That gap is the open point tracked as work item 3 in deploy.md;
decide it before the first pipeline-built image reaches a cluster.

**It runs as a non-root user:**

```bash
podman run --rm agnes:local id      # -> uid=...(app) gid=...(app), NOT root
```

## 6. First run: one container, in the foreground

Run it in the foreground the first time. On the stdio path the MCP
server's logs are its **stderr**, which the agent inherits, so both
halves' output lands in this one terminal -- and you want to see it
live rather than discover it later in `podman logs`.

### 6.1 An env file, not `-e` flags

Keep secrets off the command line and out of your shell history. This
also mirrors how the pod gets its configuration (a ConfigMap plus three
Key Vault secrets, never a file in the image).

Create `~/agnes-run/agnes.env` -- **not** in the repo tree:

```bash
mkdir -p ~/agnes-run
cat > ~/agnes-run/agnes.env <<'EOF'
# -- Azure OpenAI --
AZURE_OPENAI_API_KEY=<your-key>
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o
AZURE_OPENAI_API_VERSION=2024-12-01-preview

# -- agent API --
AGENT_API_AUTH=static
AGENT_API_TOKEN=<pick-one>
AGENT_API_HOST=0.0.0.0
AGENT_API_CORS_ORIGINS=*
AGENT_API_SESSION_TTL_MINUTES=60
AGENT_API_MAX_SESSIONS=500
AGENT_API_LOCK_TIMEOUT_SECONDS=300
KEEP_LAST_N_MSGS=20
MAX_TOOL_CONTENT_LEN=80000

# -- session state: Redis on the host, reachable via --network=host --
REDIS_URL=redis://127.0.0.1:6379/0

# -- MCP server settings, read by the CHILD process --
# absolute CONTAINER paths on the mounted volume, never host paths
MCP_DATA_SOURCE=parquet
MCP_PARQUET_SOURCES=Resources=/data/export/Resource.parquet/*.parquet,Entitlements=/data/export/Entitlement.parquet/*.parquet
MCP_DB_PATH=/duckdb/mcp_data.duckdb
MCP_LOG_LEVEL=INFO
MCP_DATA_REFRESH_MINUTES=15
MCP_SEARCH_MAX_ROWS=50000
EOF

chmod 600 ~/agnes-run/agnes.env
```

Note there is no `MCP_SERVER_TOKEN` and no `MCP_AUTH_TOKENS`. stdio has
no HTTP layer to authenticate -- that is the entire reason this
topology exists.

Note also what is NOT in the file: `AGENT_API_PORT`. That differs per
container and goes on the command line.

### 6.2 Run it

```bash
podman run --rm -it --network=host --name agnes-probe \
  --env-file ~/agnes-run/agnes.env \
  -e AGENT_API_PORT=8001 \
  -v "$AGNES_DATA":/data:z \
  -v agnes-duckdb-probe:/duckdb \
  agnes:local
```

Why each flag:

- `--network=host` -- the container shares the host's network, so
  `127.0.0.1:6379` reaches your Redis and `localhost:8001` reaches the
  agent, exactly like the process rig. Only the agent listens; the MCP
  child has no port at all on this path.
- `-v "$AGNES_DATA":/data:z` -- the staged parquet, shared-labelled for
  SELinux (section 3.3).
- `-v agnes-duckdb-probe:/duckdb` -- a named volume for the DuckDB
  cache, mirroring the chart's emptyDir at `/duckdb`. Keeping it off
  the image layer is the point; a named volume also lets you
  demonstrate warm starts (and delete it to force a cold one).
- `--rm` -- this is a probe, not a service.

### 6.3 What healthy output looks like

```
[INFO] mcp_docs_server: Docs directory: /app/mcp-server/docs
[INFO] mcp_docs_server: Server name: access-governance-docs
[INFO] mcp_docs_server: Transport: stdio (host=127.0.0.1, port=8000, stateless=n/a)
[INFO] mcp_docs_server.pdf_indexer: Scanning for PDFs in /app/mcp-server/docs
[INFO] mcp_docs_server: PDF index ready: N documents
[INFO] mcp_docs_server.csv_store: Data refresh complete in N.NNs: Resources=..., Entitlements=...
[INFO] mcp_docs_server: Data store ready: 2 datasets
INFO:     Uvicorn running on http://0.0.0.0:8001
```

**`Data store ready: 2 datasets` is the line that matters.** If it says
`0 datasets`, stop and fix it before going further -- see section 10.
Zero means the MCP child fell back to CSV mode and found no CSVs, which
means either the mount is wrong, the container paths are wrong, or the
environment did not reach the child. The agent will still start, still
answer questions, and give you confidently wrong answers from no data.

From a second terminal:

```bash
TOK=<the AGENT_API_TOKEN you set>

curl -s localhost:8001/health   # {"status":"ok","tools":12,"mcp":"ok","session_store":"redis"}
curl -s localhost:8001/livez    # {"status":"ok"}
podman top agnes-probe          # agent-api AND exactly one mcp-docs-server
```

`tools: 12` confirms the agent loaded the tool list over stdio -- the
handshake worked. `podman top` is used rather than `podman exec ... ps`
because `python:3.12-slim` has no `ps` installed; podman reads the
process list from the host instead.

One real turn:

```bash
SID=$(curl -s -X POST localhost:8001/sessions \
  -H "Authorization: Bearer $TOK" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "localhost:8001/sessions/$SID/messages" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"message":"I am on the same team as GPN 1234, what access do I need?"}'
```

Then confirm the child did NOT multiply:

```bash
podman top agnes-probe | grep -c mcp-docs-server     # still 1
```

That count staying at 1 across many turns is the held-session
behaviour. If it climbs, the agent is opening a session per tool call
and spawning a fresh MCP server every time -- which over stdio means a
process launch, a PDF re-index and a DuckDB open per call.

Ctrl-C to stop.

## 7. The full rig: Redis, two containers, nginx

Now the production shape: one Redis, two agent instances, one load
balancer in front. Each container carries its own MCP child, its own
DuckDB cache, and its own copy of the staged data -- that per-replica
duplication is real and is recorded as a cost in
single-container-stdio.md section 8. Here you get to see it.

### 7.1 Two containers

```bash
podman run -d --network=host --name agnes-a \
  --env-file ~/agnes-run/agnes.env \
  -e AGENT_API_PORT=8001 \
  -v "$AGNES_DATA":/data:z \
  -v agnes-duckdb-a:/duckdb \
  agnes:local

podman run -d --network=host --name agnes-b \
  --env-file ~/agnes-run/agnes.env \
  -e AGENT_API_PORT=8002 \
  -v "$AGNES_DATA":/data:z \
  -v agnes-duckdb-b:/duckdb \
  agnes:local

podman ps
podman logs agnes-a | tail -5
podman logs agnes-b | tail -5
```

**Separate `/duckdb` volumes are mandatory.** DuckDB is single-writer:
two processes opening one database file gives
`IOException: Could not set lock on file ... Conflicting lock is held`,
and the second instance dies at startup. In the host-process rig you
had to give each instance a distinct `MCP_DB_PATH`; here the same
`MCP_DB_PATH` is safe **only because** each container mounts a
different volume at that path. Mount the same named volume into both
and you reproduce the lock error exactly.

The staged parquet at `/data`, by contrast, is read-only in practice
and is deliberately shared.

### 7.2 nginx in front

Reuse the rig from [P3.1.md](P3.1.md) section 9.1 with the container
ports. nginx needs no root and no `/etc/nginx` -- it runs as your user
from a self-contained config. Save as `~/lb/lb.conf`:

```nginx
# lb.conf -- round-robin over two agent containers, SSE-safe
worker_processes 1;
pid nginx-lb.pid;
error_log nginx-lb-error.log;

events { worker_connections 128; }

http {
    access_log nginx-lb-access.log;

    # keep all temp paths local so no root-owned dirs are needed
    client_body_temp_path tmp;
    proxy_temp_path       tmp;
    fastcgi_temp_path     tmp;
    uwsgi_temp_path       tmp;
    scgi_temp_path        tmp;

    # "agent_api" is NOT a hostname -- it is a name this block defines.
    # When proxy_pass below says http://agent_api, nginx matches the
    # name against its upstream groups (no DNS lookup ever happens)
    # and sends each request to one member of the list. server{} says
    # where requests ARRIVE (listen 8000); upstream{} says where they
    # can GO; proxy_pass wires the two together by name. The name is
    # arbitrary but must match in both places. This is the hand-rolled
    # version of a Kubernetes Service: one stable name resolving to
    # many changing backends -- except k8s maintains the member list
    # automatically as pods come and go, and here we list both by hand.
    upstream agent_api {
        # round-robin is the default -- no directive needed.
        # deliberately NO stickiness: that is the P3.1 claim under test.
        server 127.0.0.1:8001;
        server 127.0.0.1:8002;
    }

    server {
        listen 8000;

        location / {
            # no path after the name, so the original request path is
            # forwarded unchanged
            proxy_pass http://agent_api;
            proxy_http_version 1.1;
            proxy_set_header Connection "";

            # the two settings the AKS ingress annotations map to:
            proxy_buffering off;        # SSE: forward each token as it arrives
            proxy_read_timeout 300s;    # a full turn (matches the lock timeout)
            proxy_send_timeout 300s;
        }
    }
}
```

```bash
cd ~/lb && mkdir -p tmp
nginx -p "$PWD" -c lb.conf -g 'daemon off;'
```

One cosmetic alert at startup is expected: `could not open error log
file ... /var/log/nginx/error.log ... Permission denied`. nginx probes
its compile-time default log path BEFORE reading the config; one line
later it switches to the local `nginx-lb-error.log` and never touches
`/var/log` again. Ignore it (or on nginx >= 1.19.5 add
`-e nginx-lb-error.log` to suppress it at the source).

## 8. Verification checklist

Work through these in order. Each one proves a specific claim the chart
depends on.

### 8.1 Load balancing reaches both containers

```bash
for i in 1 2 3 4; do curl -s http://localhost:8000/health > /dev/null; done
podman logs agnes-a | tail -3
podman logs agnes-b | tail -3
```

Hits alternate. nginx balances per REQUEST, not per connection, so even
one keep-alive client gets spread.

### 8.2 One session, served by both containers

```bash
SID=$(curl -s -X POST localhost:8000/sessions \
  -H "Authorization: Bearer $TOK" | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')

for q in "I am on the same team as GPN 1234, what access do I need?" \
         "Which of those are the most commonly held?" ; do
  curl -s -X POST "localhost:8000/sessions/$SID/messages" \
    -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
    -d "{\"message\":\"$q\"}"
  echo
done
```

The follow-up question has no subject of its own -- it only makes sense
with the first turn's context. Getting a sensible answer while the
turns landed on different containers is the P3.1 payoff end to end:
session id in the URL, everything else keyed off it in Redis.

### 8.3 Readiness and liveness are genuinely different

This is the check that justifies having two endpoints, and it takes
thirty seconds:

```bash
redis-cli -p 6379 shutdown nosave      # or: pkill redis-server

curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/health   # -> 503
curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/livez    # -> 200

redis-server --port 6379 --daemonize yes
curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/health   # -> 200
```

In the cluster that difference means: a Redis blip takes pods OUT OF
ROTATION but does not restart them. If liveness also checked Redis, one
Azure Cache hiccup would restart the entire fleet, and restarting
changes nothing about Redis being down.

### 8.4 A dead MCP child is invisible to a port check

```bash
podman top agnes-a hpid args | grep mcp-docs-server   # note the host pid
kill <that-pid>

curl -s -o /dev/null -w '%{http_code}\n' localhost:8001/livez   # -> 503
ss -ltnp | grep 8001                                            # STILL LISTENING
```

The port is open, the container is "up", `podman ps` says healthy --
and every turn fails. That is exactly why the chart's liveness probe is
`httpGet /livez` and not `tcpSocket`.

Note what does NOT happen: podman does not restart anything. Nothing on
this host is watching. `--restart=on-failure` would not help either,
because the AGENT process is alive and well -- only its child died. In
AKS the kubelet fails the probe three times and replaces the pod, which
is the supervision this design deliberately delegates upward rather
than building a supervisor into the image.

Restart the container to recover:

```bash
podman restart agnes-a
```

### 8.5 Streaming is not buffered

```bash
curl -N -X POST "localhost:8000/sessions/$SID/messages/stream" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"message":"Summarise the joiner process"}'
```

Tokens should trickle. Worth two extra minutes: comment out
`proxy_buffering off;`, restart nginx, repeat. The tokens now arrive as
one lump at the end. That silent lump is the number-one "works locally,
breaks on AKS" trap, and this directive is exactly what the chart's
`nginx.ingress.kubernetes.io/proxy-buffering: "off"` annotation sets
cluster-side. Re-enable it.

### 8.6 Both containers really loaded the data

```bash
podman logs agnes-a | grep "Data store ready"
podman logs agnes-b | grep "Data store ready"
podman volume ls | grep agnes-duckdb
```

Two independent ingests, two independent cache volumes. This is the
per-replica duplication made visible -- at three replicas it is three
downloads and three DuckDB builds.

## 9. Measurements to carry into the chart

Two numbers in `deploy/chart/values.yaml` are currently reasoned
guesses, not measurements. This rig is where you replace them. Both are
tracked as work item 7 in single-container-stdio.md.

### 9.1 Cold start -> `startupProbe.failureThreshold`

Startup now covers more than it did when MCP was its own pod: spawning
the child, its PDF extraction, the DuckDB open, and loading 12 tools
all happen before the app answers.

```bash
podman rm -f agnes-a; podman volume rm -f agnes-duckdb-a    # force a COLD start

start=$(date +%s)
podman run -d --network=host --name agnes-a \
  --env-file ~/agnes-run/agnes.env -e AGENT_API_PORT=8001 \
  -v "$AGNES_DATA":/data:z -v agnes-duckdb-a:/duckdb agnes:local

until curl -sf localhost:8001/health > /dev/null 2>&1; do sleep 1; done
echo "cold start: $(( $(date +%s) - start ))s"
```

Then measure a WARM start (keep the volume, just restart the
container) -- P0 measured 0.04s warm versus 10.2s cold for the data
layer alone, and in AKS every pod start is cold because the emptyDir is
new.

The chart currently sets `failureThreshold: 60` with `periodSeconds: 5`
-- a 300-second budget. Set it from your cold number with generous
headroom; an under-budgeted startup probe shows up as pods crash-looping
before they ever go Ready, which looks like an application bug.

### 9.2 Memory and CPU -> `agent.resources`

```bash
podman stats --no-stream agnes-a agnes-b
```

Watch it during a turn, not just at idle. The chart requests 1 CPU /
1536Mi and limits 2 CPU / 3Gi, derived by adding the old two tiers
together. Both halves now share one container's budget -- the MCP child
has its own interpreter and its own GIL, so they do not contend for one
lock, but they do share the limit.

To find out what happens at the limit before AKS finds out for you:

```bash
podman run -d --network=host --name agnes-limited \
  --memory=3g --cpus=2 \
  --env-file ~/agnes-run/agnes.env -e AGENT_API_PORT=8003 \
  -v "$AGNES_DATA":/data:z -v agnes-duckdb-limited:/duckdb agnes:local
```

If podman refuses the resource flags, your host is on cgroups v1 (the
RHEL 8 default), where rootless containers cannot enforce limits. That
is a host limitation, not a problem with the image: drop the flags and
measure with `podman stats` instead. The number you need is the
observed footprint, not an enforced ceiling.

### 9.3 Per-tool latency vs the HTTP path

`took=ms` is logged per tool on the MCP side and per node per turn on
the agent side. Compare a few turns here against the same questions on
your streamable-http rig. The expectation is that stdio is no slower
per call -- the transport was never the cost; LLM round-trips are.

```bash
podman logs agnes-a | grep -E 'took=' | tail -20
```

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `COPY failed: no such file or directory` | build context was a project directory | build from `/projects/agnes-agent`, the parent holding BOTH `agent-client/` and `mcp-server/` |
| `Data store ready: 0 datasets` | the child fell back to CSV mode | check the env file reached it: `podman exec agnes-a env \| grep MCP_`; check the paths are CONTAINER paths under `/data`; check the glob matches actual part files |
| `permission denied` reading `/data` | rootless UID mapping (section 3.2) | `chmod -R a+rX "$AGNES_DATA"`, verify with `podman run --rm -v ...:z ... ls -la /data` |
| Reads worked, then stopped after starting the second container | `:Z` private relabel, applied twice | use `:z` lowercase on both |
| `IOException: Could not set lock on file` | two containers, one DuckDB file | one named volume per container at `/duckdb` |
| `ModuleNotFoundError: No module named 'mcp_docs_server'` | stale build, or the package tree is wrong in the repo | confirm `src/mcp_docs_server/__init__.py` exists, then rebuild (the image has no editable-install pointer to go stale, so this is a source-layout problem) |
| `address already in use` on 8001/8002/8000 | the earlier process-based rig is still running | `ss -ltnp \| grep -E '800[0-9]'` and stop the old processes |
| `/health` 503, `/livez` 200 | Redis is down -- working as designed | start Redis; this is section 8.3, not a bug |
| `/livez` 503 | the MCP child died | `podman restart agnes-a`; in AKS the kubelet does this for you |
| Tokens arrive as one lump | proxy buffering | `proxy_buffering off;` in `lb.conf` |
| `cannot set limit ... cgroup` | cgroups v1 rootless (RHEL 8 default) | drop `--memory`/`--cpus`, measure with `podman stats` |
| `there might not be enough IDs available` | no subuid/subgid entries | section 2: `usermod --add-subuids ...` then `podman system migrate` |
| Base image pull fails | wrong internal registry path, or not logged in | `podman pull "$BASE_IMAGE"` on its own (section 2); `podman login <registry>` |
| `pip`/`uv sync` cannot reach an index | PyPI blocked on this network | set `PIP_INDEX_URL` / `UV_DEFAULT_INDEX` to the firm's mirror in the Dockerfile (there is a comment marking the spot) |
| nginx `/var/log/nginx/error.log` permission denied | cosmetic probe before the config is read | ignore, or add `-e nginx-lb-error.log` |

**A general note on `podman logs`.** Both halves log to this one
stream: the agent's own output plus the MCP child's stderr, which it
inherits. That is deliberate -- under stdio, **stdout carries the
JSON-RPC message stream**, so a log line written there lands in the
middle of a protocol frame and corrupts it. If you ever see garbled
tool results or protocol errors, a stray `print()` on the server side is
the first thing to suspect.

## 11. Cleanup

```bash
podman rm -f agnes-a agnes-b agnes-probe agnes-limited 2>/dev/null
podman volume rm -f agnes-duckdb-a agnes-duckdb-b agnes-duckdb-probe agnes-duckdb-limited 2>/dev/null

# nginx: ctrl-c the foreground process, or
kill "$(cat ~/lb/nginx-lb.pid)"

# the image, if you want the space back
podman rmi agnes:local
```

Nothing here touched a registry, a cluster, or any shared state. The
staged parquet on the host is untouched -- the containers only ever
read it.

Moving an image to another machine without a registry, if you ever need
to: `podman save agnes:local -o agnes.tar` -> scp ->
`podman load -i agnes.tar`. Also a useful reminder of what a registry
IS: save/load over HTTP, with tags and auth.

---

**When everything in section 8 passes**, the remaining unknowns are all
Azure-side: Key Vault and the CSI driver, workload identity, the
parquet-staging initContainer, real ingress and TLS, and Azure Cache
for Redis. Those are section 3 of the deployment walkthrough in
[deploy.md](deploy.md); the application itself is proven.

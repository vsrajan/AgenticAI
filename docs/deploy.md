# Deployment implementation -- containers, Helm, and the four-cluster path

Taking the stack from its current form (processes on two RHEL hosts,
no Docker anywhere) to AKS across four clusters: dev -> test -> uat
-> prod. It builds on [aks.md](aks.md) (the topology, sizing, and
probe decisions) and assumes its section 11 glossary.

> **Status (branch AKS-DEPLOY):** the repo-side artifacts are now
> IMPLEMENTED -- both Dockerfiles, the Helm chart, the four values
> files, and `.gitlab-ci.yml` are committed and referenced throughout
> this doc. The chart lints and renders cleanly against all four
> values files (`helm lint` + `helm template`, verified). What
> remains is environment-side work: the podman smoke test on the MCP
> server machine, the Phase B registry decision, Key Vaults, and the
> actual cluster deploys. Section 9 tracks both halves.

The three decisions that shape everything here:

1. **docker-compose is not used, ever.** It solves one problem --
   running multiple containers on a single Docker host -- and that
   machine never exists in this lifecycle (RHEL-without-Docker today,
   AKS tomorrow). The local dev environment REMAINS processes
   (`uv run ...`, redislite); containers appear only at the CI
   boundary.
2. **Helm from day one.** With four known environments there is no
   single-cluster interim worth writing plain manifests for: one
   chart encodes the aks.md topology, four small values files carry
   what differs per environment, promotion moves the same chart+image
   through the clusters, and `helm rollback` is the undo button.
3. **No Docker daemon is ever required, and the LOCAL phase needs no
   registry at all.** Images are built and tested locally with podman
   on the MCP server machine (RHEL's daemonless, rootless builder --
   podman 5.6.0 confirmed there); they live in podman's local image
   store and never leave the box. A registry enters the picture only
   when AKS must PULL images -- a Phase B decision (GitLab Container
   Registry vs a firm ACR; creating ACR resources is blocked by
   corporate policy, so `az acr build` is not assumed anywhere).
   Real pipeline artifacts come from GitLab CI (kaniko, also
   daemonless). All of these produce standard OCI images; AKS cannot
   tell the difference.

One more decision confirmed since the plan draft: **Redis lives
OUTSIDE the cluster** (Azure Cache for Redis). The chart therefore
contains no Redis workload at all -- the agent reaches it through
`REDIS_URL`, which arrives from Key Vault because it embeds the
access key. Anything Redis-shaped that ever DOES land in a cluster
must be exposed as a plain TCP service, never behind an http-labeled
port (the RESP-over-TCP lesson, aks.md section 6).

## Contents

1. [The progression at a glance](#1-the-progression-at-a-glance)
2. [Design decisions](#2-design-decisions)
3. [The two images](#3-the-two-images)
4. [Building images without Docker](#4-building-images-without-docker)
5. [The Helm chart](#5-the-helm-chart)
6. [Configuration: values files vs Key Vault](#6-configuration-values-files-vs-key-vault)
7. [CI/CD on GitLab](#7-cicd-on-gitlab)
8. [Validation per environment](#8-validation-per-environment)
9. [Work items](#9-work-items)
10. [Rollback](#10-rollback)

---

## 1. The progression at a glance

```
RHEL hosts (uv processes, .env, redislite)     <- dev inner loop, unchanged forever
      |
      v  Dockerfiles (committed: mcp-server/Dockerfile, agent-client/Dockerfile)
build (local):  podman on the MCP server machine -- registry-free,
                images in the local store, full stack smoke-tested there
build (CI):     GitLab CI kaniko (committed: .gitlab-ci.yml) -> registry   <- Phase B
      |
      v  images in the registry, tagged with the git SHA
Helm chart (committed: deploy/chart) + deploy/values-dev.yaml  ->  AKS dev
      |   validate: health, MCP tool round-trip, P3.1 rig semantics in-cluster
      v
same chart version + deploy/values-test.yaml  -> test
same chart version + deploy/values-uat.yaml   -> uat     (manual gates in CI)
same chart version + deploy/values-prod.yaml  -> prod
```

The app itself never changes for any of this: it was built strictly
env-var-driven with a single `.env`, and that is precisely the
contract containers want -- the same variables arrive from a Secret
and a ConfigMap instead of a file. Process-mode on RHEL and
container-mode on AKS are behaviorally identical by construction.

## 2. Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Local multi-service orchestration | none (processes) | compose needs a Docker host that will never exist here; the redislite + two-terminal rig (P3.1.md section 15) already covers local integration |
| Manifest management | Helm chart, from day one | four known environments; versioned releases, rollback, promotion of one artifact; conform to any existing firm chart/GitOps standard if one surfaces |
| Image build, local | podman 5.6.0 on the MCP server machine (confirmed present) | daemonless/rootless; the local phase is fully REGISTRY-FREE -- build, run, and smoke-test both images from the local store; "first run in a container" happens on this machine, not in the cluster |
| Image build, pipeline | GitLab CI + kaniko | daemonless, GitLab-native; `az acr build` is NOT assumed (creating ACR resources is blocked by corporate policy) -- it returns only if a firm ACR with Tasks enabled materialises |
| Registry | DEFERRED to Phase B (first AKS deploy) | nothing local depends on one; `.gitlab-ci.yml` defaults `REGISTRY_BASE` to the project's GitLab Container Registry and overriding it to a firm ACR is a one-variable change; images stay environment-agnostic and git-SHA-tagged either way |
| Image tags | `<git-sha>` per build | the SHA is the identity that moves through environments; the chart REFUSES to render without an explicit `image.tag`; never promote `latest` |
| Secrets | per-environment Key Vault + CSI driver | values files are plain YAML in git -- they hold no secrets, ever (the .env.example vs .env rule, cluster edition) |
| Azure identity | one user-assigned managed identity per environment, workload-identity-federated to the chart's service account | keyless access to Key Vault (CSI) and the parquet storage account (initContainer) -- same direction as the Entra auth roadmap (aks.md section 8) |
| Redis | Azure Cache for Redis, OUTSIDE the cluster | nothing to run or template; `REDIS_URL` (embeds the key) comes from Key Vault; `rediss://` on 6380 when TLS is enforced |
| Base image | `python:3.12-slim` via mcr.microsoft.com mirror | 3.12 is what `mcp-server/.python-version` pins (the doc originally said 3.11 -- corrected); the mirror is byte-identical with no Docker Hub rate limits; uv sync from the committed lockfiles gives identical dependency trees to the RHEL rig |
| One chart or two? | ONE chart containing both Deployments | the two services version and promote together today; split later only if their release cadences genuinely diverge |

## 3. The two images

Two committed Dockerfiles, one per service, living next to the code
they package: [`mcp-server/Dockerfile`](../mcp-server/Dockerfile) and
[`agent-client/Dockerfile`](../agent-client/Dockerfile), each with a
[`.dockerignore`](../mcp-server/.dockerignore) beside it. Both follow
the same pattern; the choices that need explaining:

- **Two-step dependency install.** `uv sync` installs the project
  itself as well as its dependencies, so a naive
  `COPY manifests -> uv sync -> COPY src` fails (the package source
  is not in the layer yet). The committed files do it in two steps:
  `uv sync --frozen --no-install-project --no-dev` right after
  copying `pyproject.toml` + `uv.lock` (this heavy layer caches until
  the lockfile changes), then `COPY src/`, then a final
  `uv sync --frozen --no-dev` that installs just the package.
- **`UV_PYTHON_DOWNLOADS=never`.** The venv must use the image's
  interpreter; if the pins ever drift from the base image, the build
  fails loudly instead of silently downloading a different Python.
- **Non-root from the start.** An `app` user is created first and
  everything below runs as it (`COPY --chown`), so there is no
  slow, layer-doubling `chown -R` at the end and the pod never runs
  as root. `/app` is created explicitly before `WORKDIR` so its
  ownership does not depend on builder quirks.
- **Startup is `uv run --no-sync <console-script>`** -- the same
  entry points as the RHEL rig (`mcp-docs-server`, `agent-api`);
  `--no-sync` stops uv from re-resolving the environment on every
  container start.
- **What gets baked:** the MCP image carries `src/` and `docs/`
  (PDFs + the config JSONs -- immutable, fast, per aks.md section 4);
  the agent image carries `src/` only. Parquet data does NOT bake --
  it is staged at runtime by an initContainer (P0 section 11.9).
  No `.env` is ever copied into an image; configuration arrives from
  the pod spec (section 6).
- **The `.dockerignore` files matter more than usual.** The default
  `MCP_DB_PATH` puts the DuckDB cache INSIDE `docs/`, and the staged
  parquet lives in the repo tree on the MCP machine -- without the
  committed exclusions (`*.duckdb`, `mcp-data/`, `.venv`, `.env`),
  `COPY docs/` would bake gigabytes of disposable cache (or worse, a
  real `.env`) into the image.
- **Open point -- PDFs in CI-built images.** `COPY docs/` bakes
  whatever the BUILD MACHINE has: local podman builds on the MCP
  machine include its PDFs; a GitLab runner only has what is
  committed (today: the two config JSONs). Before the first
  pipeline-built image goes to a cluster, decide: commit the PDFs to
  the repo (simple, versioned -- right answer if they are small and
  non-sensitive) or stage them at runtime like parquet. Tracked in
  work item 3.

## 4. Building images without Docker

### 4.1 The local path: podman on the MCP server machine, registry-free

This is the PRIMARY local workflow. podman 5.6.0 is present on the
MCP server machine; the agent machine needs nothing (podman is a
build-host tool, not a service-host tool -- images are built wherever
podman is, from repo source). Everything happens in podman's LOCAL
image store: no registry, no push, no credentials.

```bash
# on the MCP server machine -- always build from committed source:
git pull                       # NOT scp from the other machine; the
git status                     # repo is the source of truth, and a
                               # clean tree = a reproducible image

cd mcp-server  && podman build -t mcp-server:local .
cd ../agent-client && podman build -t agent-api:local .
podman images                  # both images, local store only
```

Running the stack: the parquet exports live on the HOST filesystem,
and a container cannot see host paths -- they must be MOUNTED in.
This is the container edition of the parquet lesson: the paths in
`MCP_PARQUET_SOURCES` are absolute CONTAINER paths on the mounted
volume, not the host paths. The `:Z` suffix is required on RHEL
(SELinux enforcing): it relabels the mounted files so the container
may read them; without it every read fails with EACCES.

```bash
# 1. MCP server -- mount the staged exports at /data inside the
#    container (mirrors what the initContainer does in AKS):
podman run -d --network=host --name mcp \
  -v /absolute/host/path/to/mcp-data:/data:Z \
  -e MCP_TRANSPORT=streamable-http \
  -e MCP_AUTH=static -e MCP_AUTH_TOKENS=agnes:smoketok \
  -e MCP_DATA_SOURCE=parquet \
  -e MCP_PARQUET_SOURCES='Resources=/data/export/Resource.parquet/*.parquet,Entitlements=/data/export/entitlement.parquet/*.parquet' \
  mcp-server:local
podman logs mcp                # MUST show 'Loaded parquet ... rows'
                               # and 'Data store ready: 2 datasets' --
                               # 0 datasets means the mount or the
                               # container paths are wrong

# 2. agent-api -- all three Azure OpenAI vars are required:
podman run -d --network=host --name agent \
  -e MCP_SERVER_URL=http://localhost:8000/mcp -e MCP_SERVER_TOKEN=smoketok \
  -e AZURE_OPENAI_API_KEY=... -e AZURE_OPENAI_ENDPOINT=... \
  -e AZURE_OPENAI_DEPLOYMENT=... \
  -e AGENT_API_AUTH=static -e AGENT_API_TOKEN=... \
  agent-api:local

# 3. containerized E2E: health, then one real turn
curl -s http://localhost:8080/health
podman rm -f mcp agent         # cleanup
```

Notes:

- `--network=host` keeps the localhost URLs working exactly like the
  process-based rig; both containers share the host's network.
- The point: the first time this app runs inside a container happens
  on this machine, not in a shared cluster. Path assumptions, SELinux
  labels, user permissions, and missing files surface here in
  minutes, and a Dockerfile iteration costs seconds. Record any fix
  back into the Dockerfiles (work item 2).
- The ONE external touch: the first build pulls the base image from
  mcr.microsoft.com once; it is cached afterwards. That is anonymous
  consumption of a public registry, not a registry dependency.
- Config arrives via -e flags here (ad hoc); in AKS the same
  variables arrive from Secrets/ConfigMaps. Never bake a .env into an
  image.
- If an image is ever needed on another machine without a registry:
  `podman save img -o img.tar` -> scp -> `podman load -i img.tar`.
  (Also a useful reminder of what a registry IS: save/load over HTTP
  with tags and auth.)

### 4.2 Cloud builds (az acr build) -- NOT currently available

Kept for reference: `az acr build` uploads the build context and ACR
builds server-side, no local container engine. It requires an ACR
with Tasks enabled -- and creating ACR resources is blocked by
corporate policy, so this path is NOT part of the plan unless a
firm-managed ACR with Tasks surfaces during AKS onboarding (the
Phase B registry decision). If it does: the SP needs `AcrPush`
(assign with `az role assignment create --role AcrPush ...` -- the
same data-plane-RBAC family as Storage Blob Data Reader),
`az acr login --expose-token` is the daemonless auth check, and
`az acr run --cmd '$Registry/<image>'` gives a cloud-side execution
smoke. Until then: podman locally (4.1), kaniko in CI (section 7).

## 5. The Helm chart

One chart, committed under [`deploy/chart/`](../deploy/chart/):

```
deploy/
  chart/
    Chart.yaml
    values.yaml               # defaults + full documentation of every knob
    templates/
      _helpers.tpl            # names, labels, image refs, anti-affinity block
      serviceaccount.yaml     # one SA, workload-identity annotated
      secretproviderclass.yaml# Key Vault -> k8s Secret (5 fixed object names)
      configmap.yaml          # two ConfigMaps (mcp + agent non-secret env)
      mcp-deployment.yaml     # incl. parquet-staging initContainer
      mcp-service.yaml        # ClusterIP :8000, protocol TCP
      agent-deployment.yaml   # grace 300 s, /health readiness, tcp liveness
      agent-service.yaml      # ClusterIP :8080
      ingress.yaml            # TLS; SSE: buffering off, 300 s timeouts
      networkpolicy.yaml      # only agent pods -> mcp :8000
      hpa-mcp.yaml            # conditional: mcp.hpa.enabled per environment
      pdb.yaml                # conditional: one PDB per tier
  values-dev.yaml             # 1 mcp / 2 agents, stream file on, CORS *
  values-test.yaml            # 2/2, stream file off, exact CORS origin
  values-uat.yaml             # prod-shaped: HPA on
  values-prod.yaml            # agent 3 fixed, mcp HPA 2->6 (aks.md s7)
```

What the TEMPLATES fix permanently (the aks.md decisions -- not
per-environment knobs):

- streamable-http + `MCP_STATELESS_HTTP=true`; `MCP_HOST=0.0.0.0`.
- The in-cluster MCP URL derived from the service name
  (`http://<release>-mcp:8000/mcp`) -- clients never configure it per
  environment.
- Agent probes: readiness = `/health` (pings Redis in Redis mode, so
  a pod with a broken Redis connection stops receiving traffic),
  liveness = tcpSocket (NEVER `/health` -- a Redis blip must not
  restart the fleet). Termination grace 300 s so in-flight turns
  drain.
- MCP probes: plain TCP everywhere, with a generous startupProbe
  (30 x 10 s). The server ingests data at import time and binds the
  port only after, so "port open" IS the ingest-finished readiness
  signal -- no HTTP health endpoint needed on the MCP tier.
- `MCP_DB_PATH` on an emptyDir (`/duckdb`), staged parquet on an
  emptyDir (`/data`) -- both disposable, both rebuilt from source.
- Pod anti-affinity spreading each tier across zones and nodes
  (preferred, so small dev clusters still schedule).
- A `checksum/config` pod annotation, so a values change that only
  touches a ConfigMap still rolls the pods (env vars are read once
  at process start).
- Services are ClusterIP with `protocol: TCP` -- and any future
  in-cluster TCP dependency must follow the same rule (see the Redis
  note in the header; there is deliberately no Redis template).
- The chart refuses to render without `image.tag`, and with workload
  identity enabled it refuses to render without the identity client
  id / Key Vault name -- fail at template time, not in the cluster.

Deploying any environment is one command (the pipeline runs exactly
this; the `--set image.registry` override is how Phase B's registry
choice reaches the chart):

```bash
helm upgrade --install agnes deploy/chart \
  -f deploy/values-uat.yaml \
  --set image.tag=<git-sha> \
  --namespace agnes --create-namespace
```

Verified in-repo (no cluster needed): `helm lint` passes and
`helm template` renders cleanly against each of the four values
files -- dev produces 10 objects (no PDB, no HPA), test 12 (+2 PDBs),
uat and prod 13 (+HPA).

## 6. Configuration: values files vs Key Vault

Every env var the app reads, classified once. Rule: if it is a
secret it comes from the environment's Key Vault via the CSI driver;
if it varies per environment it is in the values file; if it is a
topology truth it is fixed in the template.

| Env var | Source | Notes |
|---|---|---|
| AZURE_OPENAI_API_KEY | Key Vault `azure-openai-api-key` | until workload identity retires it (aks.md s8) |
| AGENT_API_TOKEN | Key Vault `agent-api-token` | leg-1 auth |
| MCP_SERVER_TOKEN | Key Vault `mcp-server-token` | leg-2 auth, agent side |
| MCP_AUTH_TOKENS | Key Vault `mcp-auth-tokens` | leg-2 auth, server side (`agnes:<token>[,other-agent:...]`) |
| REDIS_URL | Key Vault `redis-url` | embeds the Azure Cache access key; `rediss://...:6380` under TLS |
| AZURE_OPENAI_ENDPOINT / DEPLOYMENT / API_VERSION | values `agent.azureOpenAI.*` | dev/prod use different resources and quotas |
| ingress hostname | values `ingress.host` | agnes-dev... -> agnes... |
| replicas, HPA min/max, resources | values `mcp.*` / `agent.*` | dev 1 mcp + 2 agents -> prod per aks.md s7 |
| MCP_PARQUET_SOURCES | values `mcp.parquet.sources` | ABSOLUTE container paths onto the staged /data volume |
| parquet storage account/container (initContainer) | values `mcp.parquet.*` | per-environment data |
| AGENT_API_CORS_ORIGINS | values `agent.corsOrigins` | permissive in dev, exact origin from test up |
| AGENT_API_STREAM_FILE | values `agent.streamFile` | on in dev, "" from test upward (P4) |
| AGENT_API_SESSION_TTL / MAX_SESSIONS / LOCK_TIMEOUT | values `agent.*` | defaults usually fine; P3.1.md s14 before touching the lock TTL |
| KEEP_LAST_N_MSGS / MAX_TOOL_CONTENT_LEN / log levels | values | tuning knobs |
| MCP_TRANSPORT / MCP_STATELESS_HTTP / MCP_HOST / ports | template (fixed) | topology truths |
| MCP_SERVER_URL | template (derived) | in-cluster service DNS |
| MCP_DATA_SOURCE / MCP_DB_PATH / MCP_DOCS_DIR | template (fixed) | parquet mode, emptyDir paths, baked docs dir |

How the secret half works (all committed in
`deploy/chart/templates/secretproviderclass.yaml`): each environment
gets one Key Vault holding exactly five secrets with the FIXED names
in the table above. The Secrets Store CSI driver reads them using the
workload identity bound to the chart's service account, and syncs
them into one k8s Secret the Deployments consume as env vars.
(Mounting the CSI volume is what triggers that sync, which is why
both Deployments mount it even though nothing reads the files.) The
identity needs two data-plane roles per environment: Key Vault
secret get, and Storage Blob Data Reader on the parquet account for
the initContainer.

## 7. CI/CD on GitLab

Committed as [`.gitlab-ci.yml`](../.gitlab-ci.yml). Stages:
test -> build -> deploy, with manual gates from test upward.
The file is runnable as-is against the GitLab Container Registry;
adapt image names/runners to firm conventions during onboarding.

What it does, job by job:

- **test:mcp / test:agent** -- the exact per-branch suites run at
  every feature checkpoint, in a uv-equipped image.
- **build:mcp / build:agent** -- kaniko (daemonless) builds each
  Dockerfile and pushes `$REGISTRY_BASE/agnes/<name>:$CI_COMMIT_SHORT_SHA`.
  `REGISTRY_BASE` defaults to `$CI_REGISTRY_IMAGE` (the project's
  GitLab Container Registry), for which the built-in job credentials
  just work; Phase B's ACR alternative is a three-variable override
  (`REGISTRY_BASE`, `REGISTRY_USER`, `REGISTRY_PASSWORD`).
- **deploy:dev/test/uat/prod** -- azure-cli image, installs
  kubectl+kubelogin (`az aks install-cli`) and a pinned helm, logs in
  with the environment-scoped SP, converts kubeconfig with
  `kubelogin convert-kubeconfig -l azurecli` (AAD clusters must not
  prompt in CI), then runs the one helm command from section 5 with
  `--wait`.

GitLab specifics that matter:

- **Environment-scoped CI/CD variables** hold the per-cluster
  `AKS_CLUSTER` / `AKS_RG` / SP credentials -- the prod credentials are
  only exposed to jobs whose `environment` is prod, and protected
  environments restrict WHO can press the manual button.
- **The promotion invariant**: every deploy job uses
  `$CI_COMMIT_SHORT_SHA` -- the same images and the same chart move
  through all four clusters; only the values file differs. Nothing is
  rebuilt between environments.
- Prefer **GitLab-Azure OIDC federation** over stored SP secrets when
  the firm supports it (`id_tokens` -> Entra workload identity
  federation) -- same keyless direction as the rest of the auth
  roadmap.
- **Runner egress**: job images pull from ghcr.io (uv) and gcr.io
  (kaniko); the Dockerfiles pull from mcr.microsoft.com; the deploy
  job downloads helm from get.helm.sh. Confirm all are reachable from
  the firm's runners during onboarding and substitute the firm's
  mirror convention where they are not -- assume egress is blocked
  until proven otherwise (the extensions.duckdb.org lesson; the
  in-repo chart validation for this very branch had to build helm
  from source because get.helm.sh was blocked).

## 8. Validation per environment

After each deploy, in order (the aks.md section 10 checks, made
concrete):

1. `kubectl get pods -n agnes` -- all Ready; MCP readiness held until
   the parquet ingest finished (check `Loaded parquet ... rows` and
   `Data store ready: 2 datasets` in `kubectl logs`; a fast-start pod
   showing 0 datasets means the initContainer staged nothing).
2. `GET /health` through the ingress -- 200; in Redis mode the body
   carries `"session_store": "redis"`.
3. One authenticated MCP round-trip via the agent (a resource question
   through the API) -- proves ingress -> agent -> MCP -> DuckDB ->
   Azure OpenAI end to end.
4. Dev cluster only, once: the P3.1 rig semantics in-cluster -- kill
   an agent pod mid-conversation, session continues on the other pod;
   two simultaneous messages to one session serialize. (This is why
   values-dev.yaml runs TWO agent replicas.)
5. Watch the two log signals that were built for exactly this:
   per-tool `took=ms` on MCP, per-turn agent/tool split on the agent.

## 9. Work items

Repo-side artifacts are done; environment-side work remains.

1. **`mcp-server/Dockerfile`** + **`agent-client/Dockerfile`** per
   section 3, with `.dockerignore` files.
   - [x] done -- committed on branch AKS-DEPLOY, incl. the two-step
     uv sync, non-root user, and the duckdb/parquet ignore rules
2. **podman build + full-stack smoke** of both images on the MCP
   server machine, registry-free (section 4.1 -- note the `-v ...:Z`
   parquet mount); record any path/permission fixes back into the
   Dockerfiles.
   - [ ] done
3. **Registry decision (Phase B gate)**: GitLab Container Registry vs
   an existing firm ACR -- resolved during AKS onboarding. Then: push
   access for CI, pull access for each cluster (imagePullSecret or
   AcrPull), fill `image.registry` in the four values files. Decide
   the PDFs-in-CI-images question (section 3 open point) at the same
   time.
   - [ ] done
4. **`deploy/chart/`** per section 5, `values.yaml` fully commented.
   - [x] done -- lints + renders against all four values files
5. **Four values files** per section 6.
   - [x] done -- placeholders marked `<...>` filled during onboarding
6. **Key Vault per environment** (five fixed-name secrets) + the
   managed identity with its two data-plane roles, federated to the
   `agnes` service account.
   - [ ] done (the SecretProviderClass template is committed; the
     vaults, identities, and role assignments are portal/CLI work)
7. **`.gitlab-ci.yml`** per section 7.
   - [x] done -- committed at the repo root; runner/registry
     conventions confirmed during onboarding
8. **Deploy dev; run section 8 validation** incl. the in-cluster P3.1
   rig; then promote test -> uat -> prod through the manual gates.
   - [ ] done
9. **Docs**: update this file's checkboxes with measured results
   (image sizes, cold-start times, pipeline duration); CLAUDE.md
   recent-work entry.
   - [ ] done (partially -- this revision; measurements pending)

## 10. Rollback

- **App level**: `helm rollback agnes <revision>` -- previous chart +
  image tag, one command, per cluster. `helm history agnes` lists
  revisions.
- **Config level**: every knob is a values change; re-running the
  deploy job with the previous values file is a rollback. The
  checksum/config annotation means config rollbacks also roll pods.
- **Session impact**: with Redis mode on (P3.1), a rollback is a
  rolling restart -- sessions survive it; only in-flight turns fail
  and clients retry. Without Redis (single-replica rigs), sessions
  are lost, as always.
- **Data level**: parquet re-staging is fingerprint-gated and the
  DuckDB file is a disposable cache -- no data rollback story is
  needed on the MCP tier.

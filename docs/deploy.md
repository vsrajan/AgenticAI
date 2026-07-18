# Deployment implementation plan -- containers, Helm, and the four-cluster path

Plan for taking the stack from its current form (processes on two
RHEL hosts, no Docker anywhere) to AKS across four clusters:
dev -> test -> uat -> prod. This is a PLAN -- nothing in it is
implemented yet; the work items in section 9 get checked off when it
is. It builds on [aks.md](aks.md) (the topology, sizing, and probe
decisions) and assumes its section 11 glossary.

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
      v  Dockerfiles (2) committed to the repo
build (local):  podman on the MCP server machine -- registry-free,
                images in the local store, full stack smoke-tested there
build (CI):     GitLab CI (kaniko) -> the firm registry   <- Phase B
      |
      v  images in the registry, tagged with the git SHA
Helm chart (deploy/chart) + values-dev.yaml  ->  AKS dev
      |   validate: health, MCP tool round-trip, P3.1 rig semantics in-cluster
      v
same chart version + values-test.yaml  -> test
same chart version + values-uat.yaml   -> uat     (manual gates in CI)
same chart version + values-prod.yaml  -> prod
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
| Registry | DEFERRED to Phase B (first AKS deploy) | nothing local depends on one; the decision is GitLab Container Registry vs an existing firm ACR -- resolved when AKS onboarding clarifies what the firm sanctions; images stay environment-agnostic and git-SHA-tagged either way |
| Image tags | `<git-sha>` per build, plus a moving `dev` tag | the SHA is the identity that moves through environments; never promote `latest` |
| Secrets | per-environment Key Vault + CSI driver | values files are plain YAML in git -- they hold no secrets, ever (the .env.example vs .env rule, cluster edition) |
| Base image | `python:3.11-slim` + uv installed in-image | matches the dev interpreter; uv sync from the committed lockfiles gives identical dependency trees to the RHEL rig |
| One chart or two? | ONE chart containing both Deployments | the two services version and promote together today; split later only if their release cadences genuinely diverge |

## 3. The two images

Two Dockerfiles, one per service, living next to the code they
package (`mcp-server/Dockerfile`, `agent-client/Dockerfile`). The
shape (illustrative, finalized at implementation):

```dockerfile
# mcp-server/Dockerfile -- the agent-client one is the same pattern
# base from Microsoft's Docker Hub mirror: same image as python:3.11-slim,
# no Docker Hub rate limits, reachable from ACR/CI build agents
FROM mcr.microsoft.com/mirror/docker/library/python:3.11-slim
# uv from PyPI (reachable from both dev machines -- confirmed), version
# PINNED so builds are reproducible end to end
RUN pip install --no-cache-dir uv==<pinned-version>
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev          # locked, reproducible deps
COPY src/ ./src/
COPY docs/ ./docs/                     # PDFs + config JSONs baked in
RUN useradd -m app && chown -R app /app
USER app                               # never run as root in a pod
EXPOSE 8000
CMD ["uv", "run", "mcp-docs-server"]   # same entry point as the RHEL rig
```

Notes:

- **Entry points are the existing console scripts** (`mcp-docs-server`,
  `agent-api`) -- nothing new to maintain.
- **PDFs and the JSON configs bake into the MCP image** (immutable,
  fast, per aks.md section 4). Parquet data does NOT -- it is staged
  at runtime by an initContainer (P0 section 11.9).
- **DuckDB extensions**: when the direct-`az://` parquet mode or the
  vss/HNSW index arrive, the extension install happens HERE, at build
  time, where egress exists -- never at runtime (INSTALL 403s in
  restricted pods; observed).
- No `.env` is ever copied into an image. Configuration arrives from
  the pod spec (section 6).

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

# run the FULL stack on this one machine (--network=host keeps the
# localhost URLs working exactly like the process-based rig):
podman run -d --network=host --name mcp \
  -e MCP_TRANSPORT=streamable-http -e MCP_HOST=0.0.0.0 \
  -e MCP_AUTH=static -e MCP_AUTH_TOKENS=agnes:smoketok \
  -e MCP_DATA_SOURCE=parquet -e MCP_PARQUET_SOURCES=<ABSOLUTE paths> \
  mcp-server:local
podman run -d --network=host --name agent \
  -e MCP_SERVER_URL=http://localhost:8000/mcp -e MCP_SERVER_TOKEN=smoketok \
  -e AZURE_OPENAI_API_KEY=... -e AZURE_OPENAI_ENDPOINT=... \
  -e AGENT_API_AUTH=static -e AGENT_API_TOKEN=... \
  agent-api:local

curl -s http://localhost:8080/health            # containerized E2E
podman logs mcp                                  # 'Loaded parquet ... rows'
podman rm -f mcp agent                           # cleanup
```

Notes:

- The point: the first time this app runs inside a container happens
  on this machine, not in a shared cluster. File-path assumptions
  (ABSOLUTE paths everywhere -- the parquet lesson), user permissions,
  and missing files surface here in minutes, and a Dockerfile
  iteration costs seconds.
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

One chart under `deploy/chart/`:

```
deploy/
  chart/
    Chart.yaml
    values.yaml               # defaults + full documentation of every knob
    templates/
      mcp-deployment.yaml     # incl. parquet-staging initContainer
      mcp-service.yaml        # ClusterIP :8000, protocol TCP
      agent-deployment.yaml
      agent-service.yaml
      ingress.yaml            # TLS; /stream route: buffering off, 300s timeout
      networkpolicy.yaml      # only agent pods -> mcp :8000
      hpa-mcp.yaml            # conditional: enabled per environment
      secretproviderclass.yaml# Key Vault CSI mapping
      configmap.yaml          # the non-secret env vars
  values-dev.yaml
  values-test.yaml
  values-uat.yaml
  values-prod.yaml
```

What the TEMPLATES fix permanently (the aks.md decisions -- not
per-environment knobs): streamable-http + stateless on, one uvicorn
worker per pod, readiness = `/health` and liveness = tcpSocket (never
/health -- a Redis blip must not restart the fleet), termination grace
~300 s on the agent, pod anti-affinity across zones, PDBs, the
in-cluster MCP URL derived from the service name
(`http://<release>-mcp:8000/mcp` -- clients never configure it per
environment), Redis service ports strictly TCP.

Deploying any environment is one command:

```bash
helm upgrade --install agnes deploy/chart \
  -f deploy/values-uat.yaml \
  --set image.tag=<git-sha> \
  --namespace agnes --create-namespace
```

## 6. Configuration: values files vs Key Vault

Every env var the app reads, classified once. Rule: if it is a
secret it comes from the environment's Key Vault via the CSI driver;
if it varies per environment it is in the values file; if it is a
topology truth it is fixed in the template.

| Env var | Source | Notes |
|---|---|---|
| AZURE_OPENAI_API_KEY | Key Vault | until workload identity retires it (aks.md s8) |
| AGENT_API_TOKEN | Key Vault | leg-1 auth |
| MCP_SERVER_TOKEN / MCP_AUTH_TOKENS | Key Vault | leg-2 auth pair |
| REDIS_URL | Key Vault | embeds the cache access key |
| AZURE_OPENAI_ENDPOINT / DEPLOYMENT / API_VERSION | values | dev/prod use different resources and quotas |
| ingress hostname | values | agnes-dev... -> agnes... |
| replicas, HPA min/max, resources | values | dev 1/off/minimal -> prod per aks.md s7 |
| MCP_PARQUET_SOURCES | values | ABSOLUTE container paths onto the staged volume |
| parquet storage account/container (initContainer) | values | per-environment data |
| AGENT_API_CORS_ORIGINS | values | permissive in dev, exact origin in prod |
| AGENT_API_STREAM_FILE | values | on in dev, "" from test upward (P4) |
| AGENT_API_SESSION_TTL / MAX_SESSIONS / LOCK_TIMEOUT | values | defaults usually fine |
| KEEP_LAST_N_MSGS / MAX_TOOL_CONTENT_LEN / log levels | values | tuning knobs |
| MCP_TRANSPORT / MCP_STATELESS_HTTP / MCP_HOST / ports | template (fixed) | topology truths |
| MCP_SERVER_URL | template (derived) | in-cluster service DNS |
| MCP_DATA_SOURCE / MCP_DB_PATH / MCP_DOCS_DIR | template (fixed) | parquet mode, emptyDir paths, baked docs dir |

## 7. CI/CD on GitLab

Stages: test -> build -> deploy, with manual gates from test upward.
Sketch of `.gitlab-ci.yml` (finalized at implementation, and adapted
to the firm's runner/template conventions):

```yaml
stages: [test, build, deploy]

test:mcp:
  stage: test
  image: ghcr.io/astral-sh/uv:python3.11-bookworm
  script:
    - cd mcp-server && uv run --with pytest --with pymupdf pytest tests/ -q

test:agent:
  stage: test
  image: ghcr.io/astral-sh/uv:python3.11-bookworm
  script:
    - cd agent-client
    - uv run --with pytest --with httpx --with 'fakeredis[lua]' pytest tests_api/ -q

build:images:            # daemonless build -- kaniko (or az acr build)
  stage: build
  image: gcr.io/kaniko-project/executor:debug
  script:
    - /kaniko/executor --context mcp-server
        --destination $ACR/agnes/mcp-server:$CI_COMMIT_SHORT_SHA
    - /kaniko/executor --context agent-client
        --destination $ACR/agnes/agent-api:$CI_COMMIT_SHORT_SHA
  rules: [{ if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH' }]

.deploy: &deploy
  stage: deploy
  image: mcr.microsoft.com/azure-cli   # az + kubelogin + helm
  script:
    - az login --service-principal -u $AZ_CLIENT_ID -p $AZ_CLIENT_SECRET -t $AZ_TENANT
    - az aks get-credentials -n $AKS_CLUSTER -g $AKS_RG
    - helm upgrade --install agnes deploy/chart
        -f deploy/values-$CI_ENVIRONMENT_NAME.yaml
        --set image.tag=$CI_COMMIT_SHORT_SHA --namespace agnes

deploy:dev:  { <<: *deploy, environment: { name: dev } }
deploy:test: { <<: *deploy, environment: { name: test }, when: manual }
deploy:uat:  { <<: *deploy, environment: { name: uat },  when: manual }
deploy:prod: { <<: *deploy, environment: { name: prod }, when: manual }
```

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
- The existing per-branch test discipline maps directly onto the test
  stage: those are the same suites run at every feature checkpoint.

## 8. Validation per environment

After each deploy, in order (the aks.md section 10 checks, made
concrete):

1. `kubectl get pods` -- all Ready; MCP readiness held until the
   parquet ingest finished (check `Loaded parquet ... rows` in logs).
2. `GET /health` through the ingress -- 200; in Redis mode the body
   carries `"session_store": "redis"`.
3. One authenticated MCP round-trip via the agent (a resource question
   through the API) -- proves ingress -> agent -> MCP -> DuckDB ->
   Azure OpenAI end to end.
4. Dev cluster only, once: the P3.1 rig semantics in-cluster -- kill
   an agent pod mid-conversation, session continues on the other pod;
   two simultaneous messages to one session serialize.
5. Watch the two log signals that were built for exactly this:
   per-tool `took=ms` on MCP, per-turn agent/tool split on the agent.

## 9. Work items

All unchecked -- this is a plan.

1. **`mcp-server/Dockerfile`** + **`agent-client/Dockerfile`** per
   section 3; `.dockerignore` files (exclude .env, .venv, caches).
   - [ ] done
2. **podman build + full-stack smoke** of both images on the MCP
   server machine, registry-free (section 4.1); record any
   path/permission fixes back into the Dockerfiles.
   - [ ] done
3. **Registry decision (Phase B gate)**: GitLab Container Registry vs
   an existing firm ACR -- resolved during AKS onboarding. Then: push
   access for CI, pull access for each cluster (imagePullSecret or
   AcrPull), and the section 7 pipeline's destination filled in.
   - [ ] done
4. **`deploy/chart/`** per section 5, `values.yaml` fully commented in
   the house style.
   - [ ] done
5. **Four values files** per section 6.
   - [ ] done
6. **Key Vault per environment** + SecretProviderClass template wired
   to the four secrets.
   - [ ] done
7. **`.gitlab-ci.yml`** per section 7.
   - [ ] done
8. **Deploy dev; run section 8 validation** incl. the in-cluster P3.1
   rig; then promote test -> uat -> prod through the manual gates.
   - [ ] done
9. **Docs**: update this file's checkboxes with measured results
   (image sizes, cold-start times, pipeline duration); CLAUDE.md
   recent-work entry.
   - [ ] done

## 10. Rollback

- **App level**: `helm rollback agnes <revision>` -- previous chart +
  image tag, one command, per cluster. `helm history agnes` lists
  revisions.
- **Config level**: every knob is a values change; re-running the
  deploy job with the previous values file is a rollback.
- **Session impact**: with Redis mode on (P3.1), a rollback is a
  rolling restart -- sessions survive it; only in-flight turns fail
  and clients retry. Without Redis (dev with a single replica),
  sessions are lost, as always.
- **Data level**: parquet re-staging is fingerprint-gated and the
  DuckDB file is a disposable cache -- no data rollback story is
  needed on the MCP tier.

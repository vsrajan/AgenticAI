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
3. **No Docker daemon is ever required on a dev machine.** Images are
   built either locally with podman (RHEL's daemonless, rootless
   builder -- for smoke tests) or in the cloud with `az acr build` /
   GitLab CI. Both produce standard OCI images; AKS cannot tell the
   difference.

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
build:  podman build (local smoke test)  OR  az acr build (cloud)  OR  GitLab CI
      |
      v  images in ACR, tagged with the git SHA
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
| Image build, local | podman (if present on RHEL -- check `podman --version`) | daemonless/rootless, builds standard OCI images, lets "first run in a container" happen BEFORE "first run in the cluster" |
| Image build, pipeline | `az acr build` first, GitLab CI + kaniko in steady state | both are daemonless; az acr build needs nothing but az cli and works from the RHEL shell today; kaniko is the GitLab-native equivalent once the pipeline owns builds |
| Registry | one ACR, shared by all four clusters | images are environment-agnostic (config is not baked in); tag with git SHA, promote by reference |
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
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
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

### 4.1 Local smoke test with podman (recommended first step)

podman is Red Hat's daemonless container engine; on RHEL 8 it is a
`dnf install podman` away if not already present. Same CLI surface as
docker:

```bash
cd mcp-server
podman build -t mcp-server:smoke .
podman run --rm -p 8000:8000 \
  -e MCP_TRANSPORT=streamable-http -e MCP_HOST=0.0.0.0 \
  -e MCP_AUTH=static -e MCP_AUTH_TOKENS=agnes:smoketok \
  mcp-server:smoke
# in another terminal:
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Authorization: Bearer smoketok' http://localhost:8000/mcp   # not 401 = alive
```

The point: the first time this app runs inside a container should be
on your desk, not in a shared cluster. File-path assumptions (use
ABSOLUTE paths everywhere -- the parquet lesson), user permissions,
and missing files all surface here in minutes. podman can also push
straight to ACR after `az acr login` (`podman push <acr>.azurecr.io/...`).

### 4.2 Cloud build with az cli (no container engine at all)

`az acr build` uploads the build context and ACR builds the image
server-side -- nothing container-shaped runs on the RHEL host:

```bash
az login --service-principal --username <client-id> \
    --password "$AZ_SP_SECRET" --tenant <tenant-id>
az account set --subscription "<subscription>"

# one-time: create the registry (or use the firm's existing one)
az acr create -n <acrname> -g <resource-group> --sku Standard

# build + push in one shot, tagged with the current commit
az acr build --registry <acrname> \
  --image agnes/mcp-server:$(git rev-parse --short HEAD) \
  mcp-server/
az acr build --registry <acrname> \
  --image agnes/agent-api:$(git rev-parse --short HEAD) \
  agent-client/
```

RBAC note (same family as the storage lesson): the SP needs `AcrPush`
on the registry to build/push, and the AKS clusters' kubelet
identities need `AcrPull` to run the images.

Use 4.1 when iterating on a Dockerfile; use 4.2 to produce the real
artifacts. They converge in CI (section 7).

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
2. **podman smoke test** of both images on the RHEL rig (section 4.1);
   record any path/permission fixes back into the Dockerfiles.
   - [ ] done
3. **ACR**: registry (or firm's existing), `AcrPush` for the CI SP,
   `AcrPull` for each cluster's kubelet identity; first images via
   `az acr build` (section 4.2).
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

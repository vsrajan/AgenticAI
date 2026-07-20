# Kubernetes and Helm on AKS, from the ground up

The fifth volume of the tutorial set ([async.md](async.md),
[langgraph.md](langgraph.md), [bm25.md](bm25.md), [duckdb.md](duckdb.md)),
covering the DEPLOYMENT side: the Kubernetes primitives this stack
runs on, and how the committed Helm chart assembles them. Part 1
builds the concepts with standalone samples you can run on minikube;
part 2 walks the real `deploy/chart`, the two Dockerfiles, and
`.gitlab-ci.yml`, and closes on the pieces only a real AKS cluster
can provide.

This guide is the runnable companion to two analysis documents you
have already skimmed: [aks.md](aks.md) (WHY the topology is shaped the
way it is -- sizing, probes, latency budget) and [deploy.md](deploy.md)
(the container/Helm/CI plan and its work items). Where those explain
decisions, this one builds the vocabulary from zero.

Running the samples: you need `minikube` (a whole Kubernetes cluster
on your laptop -- one node, real API), `kubectl` (the client that
talks to it), and `helm` (section 9). Start the cluster once:

```bash
minikube start                 # boots the single-node cluster
kubectl get nodes              # one node, STATUS Ready
```

Save each manifest below as a `.yaml` file and apply it with
`kubectl apply -f file.yaml`; delete it with `kubectl delete -f
file.yaml`. A few samples need a minikube addon (`metrics-server`,
`ingress`) -- the sample says so. Everything uses tiny public images
(`nginx`, `busybox`), never this project's images, so nothing here
depends on Azure or the app building first.

Minikube is NOT AKS -- it is one node on your laptop with no cloud
around it. It is perfect for learning the Kubernetes PRIMITIVES
(pods, services, probes, Helm), which are identical everywhere. The
Azure-specific pieces (Key Vault, workload identity, a real load
balancer, multi-zone spreading) have no minikube equivalent; part 2
section 14 names each one and what AKS puts in its place.

## Contents

Part 1 -- the concepts (on minikube)
1. [Kubernetes is a reconciliation loop](#1-kubernetes-is-a-reconciliation-loop)
2. [Deployments: replicas, self-healing, rolling updates](#2-deployments-replicas-self-healing-rolling-updates)
3. [Services and cluster DNS](#3-services-and-cluster-dns)
4. [ConfigMaps and Secrets: configuration as environment](#4-configmaps-and-secrets-configuration-as-environment)
5. [Probes: readiness, liveness, startup](#5-probes-readiness-liveness-startup)
6. [initContainers and emptyDir: prepare, then serve](#6-initcontainers-and-emptydir-prepare-then-serve)
7. [Requests, limits, and the HPA](#7-requests-limits-and-the-hpa)
8. [Ingress and the SSE streaming trap](#8-ingress-and-the-sse-streaming-trap)
9. [Helm: one template, many environments](#9-helm-one-template-many-environments)

Part 2 -- the concepts in this repo
10. [The map](#10-the-map)
11. [The unit of deployment: the two images](#11-the-unit-of-deployment-the-two-images)
12. [The chart, template by template](#12-the-chart-template-by-template)
13. [Four values files and the promotion invariant](#13-four-values-files-and-the-promotion-invariant)
14. [What minikube cannot show](#14-what-minikube-cannot-show)
15. [The whole picture in one turn](#15-the-whole-picture-in-one-turn)

---

## 1. Kubernetes is a reconciliation loop

The single idea under everything: you never TELL Kubernetes to do
things ("start a container", "restart it"). You DECLARE the state you
want as an object, and a controller runs forever comparing wanted
against actual and closing the gap. Imperative vs declarative -- the
whole system is the second one.

The smallest unit is the POD: one or more containers that share a
network address and lifecycle. Here every pod holds exactly one
container. Watch the loop by creating a bare pod and destroying it:

```bash
# sample_1_pod.yaml
apiVersion: v1
kind: Pod
metadata:
  name: solo
spec:
  containers:
    - name: web
      image: nginx:1.27-alpine
```

```bash
kubectl apply -f sample_1_pod.yaml
kubectl get pods                 # solo, Running
kubectl delete pod solo
kubectl get pods                 # gone -- nothing brought it back
```

Nothing recreated it, because a bare Pod declares no desired COUNT --
it is a one-off. That is exactly why you almost never write bare pods:
they are disposable (Kubernetes kills and reschedules them at will --
node upgrades, evictions, crashes) and nothing owns their continued
existence. This disposability is not a flaw to work around; it is the
model. It is also the property that forced P3.1: session state had to
leave the process before the agent could survive its own pod being
replaced (aks.md section 11, "Pod").

## 2. Deployments: replicas, self-healing, rolling updates

A DEPLOYMENT is the object that says "keep N identical pods of this
image Running, forever". It is the reconciliation loop with a count
attached, and it owns three behaviors this stack leans on.

```bash
# sample_2_deploy.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 3
  selector:
    matchLabels: { app: web }        # which pods this Deployment owns
  template:                          # the pod spec it stamps out N times
    metadata:
      labels: { app: web }           # must match the selector above
    spec:
      containers:
        - name: web
          image: nginx:1.27-alpine
```

```bash
kubectl apply -f sample_2_deploy.yaml
kubectl get pods -l app=web          # three web-... pods

# (a) self-healing: kill one, the loop restores the count
kubectl delete pod "$(kubectl get pod -l app=web -o name | head -1)"
kubectl get pods -l app=web          # still three -- a new one is starting

# (b) scaling: change the desired number, nothing else
kubectl scale deploy/web --replicas=5
kubectl get pods -l app=web          # five now

# (c) rolling update: change the image, watch the wave
kubectl set image deploy/web web=nginx:1.27
kubectl rollout status deploy/web    # new pods up, old pods retired, no gap
```

The label SELECTOR is the mechanism: the Deployment owns exactly the
pods whose labels match, and a Service (next section) finds pods the
same way. The rolling update is why a deploy is not an outage -- new
pods must go Ready before old ones are retired, so there is never a
moment with zero pods serving. For the agent tier, P3.1 upgraded this
from "not an outage" to "not even a session loss": conversations live
in Redis, so a rollout drops only the turns in flight during the swap
(aks.md section 3).

## 3. Services and cluster DNS

Every pod gets a fresh IP when it starts, and pods start constantly.
So you never address a pod directly. A SERVICE is a stable name and
virtual IP that load-balances over whatever pods currently match its
selector -- the indirection that makes disposable pods usable.

```bash
# sample_3_service.yaml -- add a Service in front of sample 2's Deployment
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  type: ClusterIP                    # internal-only: no outside access
  selector: { app: web }             # same label the Deployment stamps
  ports:
    - port: 80
      targetPort: 80
```

```bash
kubectl apply -f sample_3_service.yaml

# a throwaway pod resolves the Service by NAME and hits it repeatedly.
# kubernetes runs a DNS server; "web" resolves cluster-wide.
kubectl run probe --rm -it --restart=Never --image=busybox -- \
  sh -c 'for i in 1 2 3 4 5; do wget -qO- http://web | grep -o "<title>.*</title>"; done'
```

`ClusterIP` is the internal-only flavor: reachable from any pod IN the
cluster, invisible from outside. That is exactly the MCP server's
posture -- agent pods call one stable DNS name and the Service spreads
calls across MCP pods, while nothing outside the cluster can reach it
at all. In this project the agent finds MCP at
`http://<release>-mcp:8000/mcp`, a name the chart derives from the
Service (part 2 section 12); no pod IP is ever configured. Getting
traffic FROM outside is a different object -- the Ingress, section 8.

## 4. ConfigMaps and Secrets: configuration as environment

The app was built to read every setting from environment variables
(the single `.env` contract). Kubernetes has two objects that become
those env vars: a CONFIGMAP for ordinary config and a SECRET for
sensitive values. They are nearly identical mechanically; the split
is about access control and intent.

```bash
# sample_4_config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  GREETING: "hello from a configmap"
  LOG_LEVEL: "INFO"
---
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
type: Opaque
stringData:                          # stringData: plain text in, base64 at rest
  API_TOKEN: "s3cr3t-value"
---
apiVersion: v1
kind: Pod
metadata:
  name: envdemo
spec:
  restartPolicy: Never
  containers:
    - name: show
      image: busybox
      command: ["sh", "-c", "echo GREETING=$GREETING LOG_LEVEL=$LOG_LEVEL TOKEN=$API_TOKEN; sleep 3"]
      envFrom:
        - configMapRef: { name: app-config }   # every key -> an env var
      env:
        - name: API_TOKEN                       # one key, pulled explicitly
          valueFrom:
            secretKeyRef: { name: app-secret, key: API_TOKEN }
```

```bash
kubectl apply -f sample_4_config.yaml
kubectl logs envdemo                 # GREETING=... LOG_LEVEL=INFO TOKEN=s3cr3t-value
```

Note the two injection styles, both used in the real chart:
`envFrom` splashes every ConfigMap key in as an env var (bulk
non-secret config), while `secretKeyRef` pulls one named Secret key
(explicit, per-secret). A Secret is only base64-encoded at rest, not
encrypted -- its protection is RBAC (who may read it) and the rule
that secrets never land in a ConfigMap or a log line. This project's
split: `MCP_PORT` and endpoints are ConfigMap material;
`AGENT_API_TOKEN`, `MCP_AUTH_TOKENS`, and `REDIS_URL` (it embeds the
cache access key) are always Secrets (aks.md section 11, "Secret /
ConfigMap"). On AKS the Secret's VALUES do not even live in the
cluster -- they are projected from Azure Key Vault (section 14).

## 5. Probes: readiness, liveness, startup

This is the concept the whole aks.md analysis pivots on, so build it
by hand. A PROBE is a periodic health check the node runs against a
pod. Three kinds, and confusing them is how you cause an outage:

- READINESS -- "may this pod receive traffic right now?" Failing it
  removes the pod from its Service's rotation but does NOT restart it.
- LIVENESS -- "is this process wedged?" Failing it RESTARTS the pod.
- STARTUP -- "has it finished booting?" Suppresses the other two until
  it passes.

Watch readiness take a pod out of rotation without killing it:

```bash
# sample_5_probe.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probed
spec:
  replicas: 1
  selector: { matchLabels: { app: probed } }
  template:
    metadata: { labels: { app: probed } }
    spec:
      containers:
        - name: web
          image: nginx:1.27-alpine
          readinessProbe:
            exec:
              command: ["cat", "/tmp/ready"]   # passes only if the file exists
            periodSeconds: 2
          livenessProbe:
            httpGet: { path: /, port: 80 }     # nginx serving = alive
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata: { name: probed }
spec:
  selector: { app: probed }
  ports: [{ port: 80, targetPort: 80 }]
```

```bash
kubectl apply -f sample_5_probe.yaml
kubectl get pod -l app=probed        # READY 0/1 -- /tmp/ready does not exist yet
kubectl get endpoints probed         # no addresses: Service has nobody to route to

# create the file the readiness probe checks -> pod joins rotation
kubectl exec deploy/probed -- touch /tmp/ready
kubectl get pod -l app=probed        # READY 1/1
kubectl get endpoints probed         # the pod IP now appears

# remove it -> pod leaves rotation, but is NOT restarted (RESTARTS stays 0)
kubectl exec deploy/probed -- rm /tmp/ready
kubectl get pod -l app=probed        # READY 0/1, RESTARTS 0
```

That last line IS the design decision in aks.md section 3. The
agent's readiness probe is `GET /health`, which pings Redis and 503s
when Redis is down -- a pod that lost the session store should leave
rotation. But its LIVENESS probe is a plain TCP check, deliberately
NOT `/health`: a Redis blip would fail liveness on every pod at once
and restart the entire tier for nothing, and restarting pods does not
fix Redis. Readiness reacts to dependencies; liveness must not. The
MCP tier adds the third kind -- a generous STARTUP probe covering its
~10 s DuckDB cold ingest (duckdb.md) so it is not declared broken
while loading.

## 6. initContainers and emptyDir: prepare, then serve

Two small concepts that together are how production DATA reaches the
MCP pod. An INITCONTAINER runs to completion BEFORE the main container
starts; an EMPTYDIR is a scratch volume created empty at pod start and
deleted when the pod dies. Mount one emptyDir into both containers and
the init step can stage files the main process then reads.

```bash
# sample_6_init.yaml
apiVersion: v1
kind: Pod
metadata:
  name: staged
spec:
  restartPolicy: Never
  volumes:
    - name: data
      emptyDir: {}                   # scratch disk, pod lifetime
  initContainers:
    - name: stage                    # runs first, to completion
      image: busybox
      command: ["sh", "-c", "echo 'part-00000 rows...' > /data/export.txt"]
      volumeMounts:
        - { name: data, mountPath: /data }
  containers:
    - name: serve                    # starts only after stage exits 0
      image: busybox
      command: ["sh", "-c", "cat /data/export.txt; sleep 5"]
      volumeMounts:
        - { name: data, mountPath: /data }
```

```bash
kubectl apply -f sample_6_init.yaml
kubectl logs staged -c stage         # the init step's output
kubectl logs staged                  # 'part-00000 rows...' -- main read what init wrote
```

This is precisely the MCP pod's shape (part 2 section 12): an
initContainer runs `az storage blob download-batch` to pull the Spark
parquet exports from Azure Storage onto a `/data` emptyDir, and only
then does the server start and ingest them (P0 section 11.9). The
emptyDir is the right choice because the data is a CACHE -- the DuckDB
file rebuilds from those parts, and losing it on a pod restart costs
one ~10 s re-ingest, not real data (aks.md section 11, "emptyDir").
Data you genuinely cannot rebuild would need a PersistentVolume;
nothing in this stack qualifies.

## 7. Requests, limits, and the HPA

Two numbers on every container drive both scheduling and stability.
The REQUEST is what the scheduler reserves (and what it uses to pick a
node); the LIMIT is the ceiling the node enforces. For CPU, crossing
the limit THROTTLES; for memory, crossing it gets the container
OOM-killed. The Horizontal Pod Autoscaler (HPA) then watches a metric
and changes the replica COUNT between a floor and a ceiling.

```bash
minikube addons enable metrics-server   # the HPA needs CPU numbers to read
```

```bash
# sample_7_hpa.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cpu
spec:
  replicas: 1
  selector: { matchLabels: { app: cpu } }
  template:
    metadata: { labels: { app: cpu } }
    spec:
      containers:
        - name: worker
          image: busybox
          command: ["sh", "-c", "while true; do :; done"]   # pegs one core
          resources:
            requests: { cpu: "100m" }    # 0.1 core reserved
            limits:   { cpu: "200m" }    # throttled above 0.2 core
```

```bash
kubectl apply -f sample_7_hpa.yaml
kubectl autoscale deploy/cpu --cpu-percent=50 --min=1 --max=4
kubectl get hpa cpu --watch          # TARGETS climbs past 50%, REPLICAS grows 1 -> 4
```

The busy loop drives CPU past 50% of its request, so the HPA adds
pods up to the max, then removes them when you delete the Deployment.
This is the MCP tier's model exactly (aks.md section 4): BM25 scoring
and fuzzy regex are GIL-bound Python -- one pod is effectively one
core of Python regardless of threads -- so more PODS is the clean way
to more throughput, and CPU is a fair scaling signal. The AGENT tier
deliberately does NOT autoscale on CPU: its turns are ~95% I/O wait on
Azure OpenAI, so a pod running 30 concurrent turns shows almost no
CPU. A CPU HPA would never fire; if the agent tier ever autoscales it
scales on in-flight turns (KEDA), not CPU. Measured sizing lives in
aks.md section 7 (agent 0.5 CPU / 1Gi, MCP 0.5 CPU / 512Mi).

## 8. Ingress and the SSE streaming trap

A ClusterIP is invisible from outside. An INGRESS is the front door:
one component (an ingress controller, e.g. NGINX) that terminates TLS
and routes external URLs to internal Services. Crucially it is a
REVERSE PROXY -- and a proxy's buffering behavior is the single
biggest "works locally, breaks on AKS" trap for this app.

```bash
minikube addons enable ingress       # runs an NGINX ingress controller
```

```bash
# sample_8_ingress.yaml -- expose sample 3's "web" Service
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: web
  annotations:
    # the two annotations that matter for token STREAMING (SSE):
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
spec:
  rules:
    - host: web.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service: { name: web, port: { number: 80 } }
```

```bash
kubectl apply -f sample_8_ingress.yaml
echo "$(minikube ip) web.local" | sudo tee -a /etc/hosts   # resolve the demo host
curl -s http://web.local | grep -o "<title>.*</title>"     # reached nginx through the ingress
```

The nginx welcome page is served THROUGH the ingress controller now.
For a plain page the annotations do nothing visible -- their whole
point is streaming. The agent's `/stream` endpoint emits Server-Sent
Events: the LLM's tokens as they are generated. A proxy that BUFFERS
responses (the default) holds those tokens until the response
completes, so the user sees nothing, then everything at once -- the
streaming UX silently dead. `proxy-buffering: off` makes the proxy
forward each chunk immediately; the 300 s read timeout stops it
severing a long turn mid-stream (a turn can run the length of the
Redis lock timeout). This is aks.md's "#1 works-locally-breaks-on-AKS
trap" and it is fixed permanently in the chart's ingress template
(part 2 section 12) -- never a per-environment knob.

## 9. Helm: one template, many environments

Everything so far was hand-written YAML for one setup. The real
deployment has FOUR environments (dev, test, uat, prod) that differ in
a dozen small ways -- replica counts, hostnames, resource sizes -- and
are identical in a hundred others. Maintaining four copies of every
manifest is how they drift apart. HELM is the fix: templated manifests
(a CHART) plus a small VALUES file per environment. One source of
truth, parameterized.

```bash
# a minimal chart by hand: two files under mychart/
mkdir -p mychart/templates
```

```yaml
# mychart/Chart.yaml
apiVersion: v2
name: mychart
version: 0.1.0
```

```yaml
# mychart/values.yaml -- the defaults
replicas: 1
image: nginx:1.27-alpine
greeting: "default greeting"
```

```yaml
# mychart/templates/deploy.yaml -- {{ }} are substitution points
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-web
spec:
  replicas: {{ .Values.replicas }}
  selector: { matchLabels: { app: {{ .Release.Name }}-web } }
  template:
    metadata: { labels: { app: {{ .Release.Name }}-web } }
    spec:
      containers:
        - name: web
          image: {{ .Values.image }}
          env:
            - name: GREETING
              value: {{ .Values.greeting | quote }}
```

```bash
# render locally WITHOUT a cluster -- the fastest way to check a chart
helm template demo ./mychart                       # defaults: replicas 1
helm template demo ./mychart --set replicas=3      # override one value
helm template demo ./mychart -f prod-values.yaml   # a whole env's overrides

# actually install it (creates the objects in the cluster)
helm install demo ./mychart --set replicas=2
kubectl get pods -l app=demo-web                    # two demo-web pods
helm upgrade demo ./mychart --set replicas=4        # same release, new state
helm rollback demo 1                                # back to revision 1, one command
helm uninstall demo
```

`helm template` renders the final YAML on your laptop with no cluster
touched -- this is how the real chart is validated in CI (part 2
section 13) and how you should inspect any change before applying it.
`{{ .Values.x }}` reads the values file, `{{ .Release.Name }}` is the
install name -- so `helm install dev ...` and `helm install prod ...`
from ONE chart produce independently-named object sets. The payoff
that matters operationally: `helm rollback` makes a bad deploy a
one-command undo, and the SAME chart version promoted across four
clusters guarantees they run the same topology (only the values
differ). That promotion invariant is the spine of this project's
deployment story -- section 13.

---

## 10. The map

Every concept above, and where it lives in the committed deployment
artifacts. File paths are relative to the repo root.

| Concept (sample) | In this repo |
|---|---|
| Pod, the disposable unit (1) | every pod is one container; disposability is why P3.1 exists (aks.md s11) |
| Deployment + replicas + rolling update (2) | `deploy/chart/templates/{mcp,agent}-deployment.yaml` |
| Service / ClusterIP + DNS (3) | `{mcp,agent}-service.yaml`; derived URL `http://<release>-mcp:8000/mcp` in `configmap.yaml` |
| ConfigMap + Secret as env (4) | `configmap.yaml` (two ConfigMaps), `secretproviderclass.yaml` (the Secret), `env`/`envFrom` in both deployments |
| readiness vs liveness vs startup (5) | agent readiness `/health` + tcp liveness; MCP tcp startup gate -- both deployment templates |
| initContainer + emptyDir (6) | the `stage-parquet` initContainer + `/data` and `/duckdb` emptyDirs in `mcp-deployment.yaml` |
| requests/limits + HPA (7) | `resources` in both deployments; `hpa-mcp.yaml` (conditional) |
| Ingress + SSE annotations (8) | `ingress.yaml` |
| Helm template + values (9) | the whole `deploy/chart/` + `deploy/values-{dev,test,uat,prod}.yaml` |
| image as the deploy unit | `mcp-server/Dockerfile`, `agent-client/Dockerfile` |
| promotion across environments | `.gitlab-ci.yml` |

## 11. The unit of deployment: the two images

Kubernetes schedules CONTAINERS, so the first step is turning each
service into an image. There are two Dockerfiles, one per service,
each with a `.dockerignore` beside it. Both follow the same pattern;
[duckdb.md](duckdb.md) and [async.md](async.md) cover what runs INSIDE
them, so here only the container-shaping choices matter (full
reasoning in deploy.md section 3):

- **`FROM mcr.microsoft.com/mirror/docker/library/python:3.12-slim`**
  -- Python 3.12 (what `.python-version` pins) from Microsoft's Docker
  Hub mirror, so builds never hit Docker Hub rate limits.
- **Two-step `uv sync`.** `uv sync` installs the project itself as
  well as its dependencies, so copying only the manifests and syncing
  first (`--no-install-project`), THEN copying `src/` and syncing
  again, puts the slow dependency install in a layer that caches until
  `uv.lock` changes. Editing source rebuilds only the fast final
  layer.
- **Non-root.** An `app` user is created before anything is copied,
  and everything runs as it -- a pod should never run as root.
- **What bakes in:** the MCP image carries `docs/` (PDFs + config
  JSONs -- immutable and fast); the agent image carries `src/` only.
  Parquet data does NOT bake -- it is staged at runtime by the
  initContainer (concept 6). No `.env` ever enters an image; the
  `.dockerignore` files also keep the DuckDB cache and staged parquet
  out of the build context.

You built these exact images on the MCP machine with podman
(deploy.md section 4.1). Kubernetes pulls the same kind of image from
a registry -- the only difference is where the bytes come from.

## 12. The chart, template by template

`deploy/chart/` is one chart for both services. The rule dividing its
contents (deploy.md section 6): TOPOLOGY TRUTHS are fixed in the
templates, PER-ENVIRONMENT values come from the values files, and
SECRETS come from Key Vault. Walking the templates:

- **`_helpers.tpl`** -- shared naming (`<release>-mcp`,
  `<release>-agent`), the common label block, image-reference
  assembly, and the pod anti-affinity snippet. Helm's version of
  factoring out repetition.
- **`mcp-deployment.yaml`** -- concepts 2, 5, 6, 7 in one object: the
  `stage-parquet` initContainer (concept 6) authenticating with
  workload identity and running `az storage blob download-batch` onto
  the `/data` emptyDir; the container with its `/duckdb` cache
  emptyDir; a generous tcp STARTUP probe as the ingest gate, with tcp
  readiness/liveness (the MCP tier has no dependency to health-check,
  so plain tcp everywhere is right); and -- when `mcp.hpa.enabled` --
  the `replicas` field is OMITTED so it does not fight the autoscaler.
- **`mcp-service.yaml`** -- a ClusterIP on :8000, `protocol: TCP`. The
  comment there carries the RESP-over-TCP lesson: never label a port
  with a protocol it does not speak (aks.md section 5's Redis trap).
- **`agent-deployment.yaml`** -- concept 5's load-bearing split made
  real: readiness `GET /health` (pings Redis; a pod that lost Redis
  leaves rotation) vs liveness `tcpSocket` (a Redis blip must not
  restart the fleet). Plus `terminationGracePeriodSeconds: 300` so an
  in-flight turn drains and releases its Redis lock before the pod
  dies (aks.md section 11, "terminationGracePeriod"). Secrets arrive
  via `secretKeyRef` (concept 4), non-secret config via `envFrom`.
- **`agent-service.yaml`** -- ClusterIP :8080; only the ingress needs
  it.
- **`ingress.yaml`** -- concept 8, permanent: `proxy-buffering: off`
  and the 300 s timeouts baked in so token streaming survives the
  proxy hop. TLS host from values.
- **`configmap.yaml`** -- two ConfigMaps (concept 4). The topology
  truths are literal (`MCP_TRANSPORT=streamable-http`,
  `MCP_STATELESS_HTTP=true`, the derived
  `MCP_SERVER_URL=http://<release>-mcp:8000/mcp`); per-environment
  knobs interpolate from values.
- **`secretproviderclass.yaml`** -- the Key Vault bridge (concept 4's
  Secret, AKS edition -- section 14). Five fixed object names; the
  driver syncs them into one k8s Secret the deployments read.
- **`networkpolicy.yaml`** -- a pod-to-pod firewall: only agent pods
  may reach MCP pods on :8000. Defense in depth under the bearer-token
  auth; the allowlist you widen to admit another agent (aks.md
  section 12).
- **`hpa-mcp.yaml`** / **`pdb.yaml`** -- concept 7's HPA (MCP tier
  only) and a PodDisruptionBudget keeping >=1 pod of each tier through
  node drains. Both conditional (`{{- if ... }}`), so dev switches
  them off.

Two chart-wide habits worth naming. A `checksum/config` annotation on
the pod templates hashes the ConfigMap, so a config-only change still
rolls the pods (env vars are read once at process start). And the
templates use `required` on `image.tag`, the Key Vault name, and the
identity client id -- so a missing critical value fails at
`helm template` time, on your laptop, not as a broken pod in the
cluster.

## 13. Four values files and the promotion invariant

`deploy/values-{dev,test,uat,prod}.yaml` are concept 9's per-env
overrides -- small on purpose, since the chart carries the shared
shape. The differences that matter:

- **dev**: 1 MCP replica, HPA and PDBs off, permissive CORS, the
  per-node stream-file debug log ON. But TWO agent replicas
  deliberately -- the one in-cluster P3.1 check (kill an agent pod
  mid-conversation, watch the session continue on the other) needs a
  second replica to be meaningful (deploy.md section 8).
- **test**: both tiers at 2, stream file off, an exact CORS origin.
- **uat**: prod-shaped on purpose (HPA on) so it rehearses what prod
  runs.
- **prod**: agent 3 fixed replicas, MCP HPA 2->6 -- the aks.md section
  7 sizing.

The invariant these serve is the point of the whole structure: the
SAME chart version and the SAME image (tagged with the git SHA) move
through all four clusters; only the values file changes. Nothing is
rebuilt between environments, so what passed in test is bit-for-bit
what reaches prod. `.gitlab-ci.yml` enforces it -- one `helm upgrade
--install` per environment, each passing
`--set image.tag=$CI_COMMIT_SHORT_SHA -f deploy/values-<env>.yaml`,
with manual gates from test upward. The chart was validated the way
concept 9 showed -- `helm lint` plus `helm template` against all four
values files (10/12/13/13 objects, the PDB/HPA conditionals firing
correctly) -- with no cluster involved.

## 14. What minikube cannot show

Four pieces of the real deployment have no minikube equivalent,
because they are Azure services, not Kubernetes primitives. Each is a
concept you now understand, wearing a cloud implementation:

- **Key Vault CSI driver** -- concept 4 said a Secret's values are
  base64 at rest IN the cluster. On AKS they are not in the cluster at
  all: the Secrets Store CSI driver projects them from Azure Key Vault
  into a k8s Secret at pod start (`secretproviderclass.yaml`). The
  secret material stays rotated and audited in Key Vault; Kubernetes
  sees only a projection. Minikube has no Key Vault, so locally you
  would use a plain Secret (concept 4) in its place.
- **Workload identity** -- how the initContainer authenticates to
  Azure Storage and how the agent will eventually reach Azure OpenAI
  without an API key. The pod gets its own Microsoft Entra identity
  and proves WHO IT IS, so there is no key to store or leak. This is
  the same Entra direction as the app's auth roadmap; minikube pods
  have no cloud identity to assume.
- **A real Ingress load balancer + Azure Cache for Redis** -- concept
  8's controller on AKS gets a real external (private) IP from an
  Azure load balancer; minikube fakes this with `minikube tunnel`.
  Redis is Azure Cache for Redis (`rediss://` on 6380, TLS), a managed
  service OUTSIDE the cluster -- the chart contains no Redis workload
  by design. Locally you would point `REDIS_URL` at redislite or a
  local redis (P3.1.md section 15).
- **Multi-zone spreading** -- the pod anti-affinity and PDBs (concept
  7's neighbors) only mean something across real failure domains
  (nodes, availability zones). On a single minikube node they render
  and apply but cannot actually spread anything; on AKS they keep a
  node or zone failure from taking a whole tier down.

The NetworkPolicy is a fifth asterisk: it renders on minikube but is
only ENFORCED if you start minikube with a CNI that implements it
(`minikube start --cni=calico`); the default CNI ignores it, so the
rule exists but nothing blocks traffic.

## 15. The whole picture in one turn

Putting every concept in motion, a single user question on the
deployed stack: TLS terminates at the INGRESS (concept 8), which
forwards -- buffering off, so tokens stream -- to the agent SERVICE
(3), which round-robins to one agent POD in a DEPLOYMENT of 2-3
replicas (2). That pod passed its `/health` READINESS probe (5), so it
is in rotation; it reads its config from a CONFIGMAP and its tokens
from a Key-Vault-projected SECRET (4, 14). It calls the MCP server by
its stable ClusterIP DNS NAME (3), reaching an MCP pod whose
INITCONTAINER already staged parquet onto an EMPTYDIR (6) and whose
tcp STARTUP probe (5) held it out of rotation until its DuckDB ingest
finished. Under load the MCP tier's HPA (7) added pods; the agent tier
did not, because its time is I/O wait on Azure OpenAI (aks.md section
6). The turn's Redis writes go to Azure Cache OUTSIDE the cluster
(14); a rolling DEPLOY (2) mid-turn would cost only that turn, because
the session lives in Redis. And every object in that paragraph came
from ONE Helm chart (9) rendered with `values-prod.yaml`, the same
chart and image that passed through dev, test, and uat before it.

The lesson aks.md states and this walkthrough earns: the topology
buys milliseconds and resilience -- what the user actually FEELS is
owned by the Azure OpenAI quota and the tokens each turn spends. Tune
the cluster once (this guide); keep tuning the token axis (P1.1
already done, P1.3 next).

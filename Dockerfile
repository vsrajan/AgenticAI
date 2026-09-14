# Combined agent + MCP server image (branch stdio).
#
# ONE container carrying BOTH projects, because the stdio transport
# needs the agent to exec the MCP server binary and own the resulting
# child process -- see docs/single-container-stdio.md section 3. Two
# containers in a pod share a network namespace, not a filesystem, so
# they could only talk over localhost HTTP, which is the auth problem
# this design exists to avoid.
#
# The build context is the REPO ROOT (/projects/agnes-agent), not a
# project directory -- this file copies from BOTH agent-client/ and
# mcp-server/:
#   podman build -t agnes:local -f Dockerfile \
#     --build-arg BASE_IMAGE=<registry>/python:3.12-slim .
#
# Each project keeps its own virtualenv and its own locked dependency
# tree; nothing is merged or resolved across the two. The only thing
# they share is the interpreter.

# -- base image --
# PLACEHOLDER: replace the default with the firm's internal registry
# path, or override per build:
#   podman build --build-arg BASE_IMAGE=<registry>/python:3.12-slim ...
# The CI pipeline passes it from the BASE_IMAGE variable so the value
# lives in ONE place per environment.
#
# 3.12 is what mcp-server/.python-version pins; whatever image this
# resolves to must carry that interpreter, because UV_PYTHON_DOWNLOADS
# below forbids fetching a different one.
ARG BASE_IMAGE=registry.internal.example.com/python:3.12-slim
FROM ${BASE_IMAGE}

# uv, pinned so builds are reproducible end to end.
#
# NOTE for a restricted network: this pulls from PyPI, and so does
# every `uv sync` below. An internal container registry usually comes
# with an internal PyPI mirror -- if PyPI itself is unreachable, set
# PIP_INDEX_URL and UV_DEFAULT_INDEX to the firm's mirror here. They
# are deliberately NOT parameterised: an empty index URL is worse than
# no index URL, so this is an explicit edit rather than a build arg
# that silently defaults to nothing.
RUN pip install --no-cache-dir uv==0.8.17

# non-root from the start; /app subdirectories created up front so
# COPY --chown never has to rewrite ownership of a large tree
RUN useradd --create-home app \
    && mkdir -p /app/mcp-server /app/agent-client \
    && chown -R app:app /app
USER app
WORKDIR /app

# 3.12 is what mcp-server/.python-version pins. agent-client had no pin
# and resolved to 3.11 locally; both lockfiles declare requires-python
# >=3.10 and agent-client's full dependency set was verified to install
# on 3.12, so one interpreter serves both (section 12's open question,
# now closed). Never silently download a different one.
ENV UV_PYTHON_DOWNLOADS=never

# -- dependency layers, both projects --
# manifests only, so editing source never re-installs dependencies.
# --no-install-project skips each package itself; its source is not in
# the layer yet.
COPY --chown=app:app mcp-server/pyproject.toml mcp-server/uv.lock ./mcp-server/
RUN uv sync --frozen --no-install-project --no-dev --project /app/mcp-server

COPY --chown=app:app agent-client/pyproject.toml agent-client/uv.lock ./agent-client/
RUN uv sync --frozen --no-install-project --no-dev --project /app/agent-client

# -- project layers --
# mcp-server carries docs/ (PDFs + the config JSONs): immutable, fast,
# per aks.md section 4. Parquet data does NOT bake in -- it is staged at
# runtime by the initContainer (docs/P0.md section 11.9).
COPY --chown=app:app mcp-server/src/ ./mcp-server/src/
COPY --chown=app:app mcp-server/docs/ ./mcp-server/docs/
RUN uv sync --frozen --no-dev --project /app/mcp-server

COPY --chown=app:app agent-client/src/ ./agent-client/src/
RUN uv sync --frozen --no-dev --project /app/agent-client

# -- how the agent starts the server --
# MCP_SERVER_ARGS is split on whitespace by _get_mcp_server_config, and
# --project is ABSOLUTE on purpose: the child must resolve the
# mcp-server environment regardless of the parent's working directory.
# --no-sync stops uv re-resolving on every spawn.
ENV MCP_TRANSPORT=stdio \
    MCP_SERVER_COMMAND=uv \
    MCP_SERVER_ARGS="run --no-sync --project /app/mcp-server mcp-docs-server"

# no .env is ever copied in; configuration arrives from the pod spec.
# MCP_DOCS_DIR and MCP_DB_PATH default relative to the server module, so
# they resolve correctly whatever the cwd -- but the chart sets
# MCP_DB_PATH onto the /duckdb emptyDir so the cache is not written into
# the image layer.

EXPOSE 8080
WORKDIR /app/agent-client
CMD ["uv", "run", "--no-sync", "--project", "/app/agent-client", "agent-api"]

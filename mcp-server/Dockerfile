# mcp-server image. Built with podman locally (docs/deploy.md section
# 4.1) and kaniko in CI (section 7). Base image comes from Microsoft's
# Docker Hub mirror: byte-identical to python:3.12-slim (the version
# .python-version pins), no Docker Hub rate limits.
FROM mcr.microsoft.com/mirror/docker/library/python:3.12-slim

# uv from PyPI (reachable from both dev machines), version pinned so
# builds are reproducible end to end
RUN pip install --no-cache-dir uv==0.8.17

# never run as root in a pod; /app is created up front so WORKDIR
# ownership does not depend on the builder's WORKDIR semantics
RUN useradd --create-home app && mkdir /app && chown app:app /app
USER app
WORKDIR /app

# the image's interpreter is the one the venv must use -- fail the
# build instead of silently downloading a different python
ENV UV_PYTHON_DOWNLOADS=never

# dependency layer first: manifests only, so source edits do not
# re-install dependencies. --no-install-project skips the package
# itself (its source is not in the layer yet).
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# project layer: source + baked-in docs (PDFs and the config JSONs;
# see docs/deploy.md section 3 on what docs/ contains per build
# context). Parquet data is NOT baked -- it is staged at runtime by
# an initContainer (docs/P0.md section 11.9).
COPY --chown=app:app src/ ./src/
COPY --chown=app:app docs/ ./docs/
RUN uv sync --frozen --no-dev

EXPOSE 8000

# --no-sync: the environment was built above; container start must
# not re-resolve or write to it. Same entry point as the RHEL rig.
CMD ["uv", "run", "--no-sync", "mcp-docs-server"]

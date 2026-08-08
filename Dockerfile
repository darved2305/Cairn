# One image, one deploy path — PROJECT.md §6.1. Worker and console tasks
# (infra/ecs.tf) run this same image with different container commands;
# there is no separate console image.
#
# Multi-stage: the builder resolves the locked dependency set with uv (the
# same resolver/lock the env fingerprint hashes — PROJECT.md §4.2), the
# final stage is a slim runtime with only the built venv and source, no
# compiler toolchain.

FROM python:3.12-slim AS builder

# certs/local-ca.crt is an empty placeholder by default (a no-op to trust)
# — see certs/README.md. Machines behind TLS-intercepting antivirus or a
# corporate proxy replace it locally (never committed) so `docker build`
# can still reach PyPI; CI and most developer machines never touch it.
COPY certs/local-ca.crt /usr/local/share/ca-certificates/local-ca.crt
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN pip install --no-cache-dir uv==0.10.7

# Built at the SAME absolute path (/app) the runtime stage copies it back
# to below — uv bakes an absolute shebang (`#!/build/.venv/bin/python`)
# into every console-script entrypoint it generates, so building at
# /build and running from /app silently produced a venv whose own `cairn`
# command pointed at a python that doesn't exist in the final image.
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY README.md ./

# --no-dev: the image runs the app, not the test suite; ruff/mypy/pytest
# have no reason to ship to production.
RUN uv sync --frozen --no-dev

# The console's React SPA. PROJECT.md §6.1 commits to "one image, one deploy
# path", so the built bundle is baked into this same image rather than being
# served from a second container or an S3/CloudFront origin of its own —
# `cairn.console.api` mounts it from /app/src/cairn/console/static and serves
# the API and the app from one port.
#
# Node appears only in this stage. The runtime image below copies the emitted
# static files and never sees a node_modules, so the production image gains a
# frontend without gaining a JavaScript runtime.
FROM node:22-slim AS frontend

WORKDIR /ui
COPY console/frontend/package.json console/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY console/frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

# PYTHONHASHSEED must be set before the interpreter starts —
# workload/determinism.py's apply() checks this and fails loudly rather
# than silently running with a determinism guarantee that isn't true.
ENV PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=frontend /ui/dist /app/src/cairn/console/static
COPY cairn.yaml /app/cairn.yaml
COPY db/migrations /app/db/migrations

RUN useradd --create-home --shell /usr/sbin/nologin cairn
USER cairn

# No default work to do — worker tasks are launched via ECS RunTask with
# an explicit containerOverrides command (`cairn claim-demo ...` today;
# `cairn run ...` once the full agent loop lands). This default only has
# to exist so `docker run` without an override doesn't error.
CMD ["cairn", "--help"]

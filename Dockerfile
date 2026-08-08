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

FROM python:3.12-slim AS runtime

# psycopg's sslmode=verify-full needs a populated system trust store to
# validate CockroachDB Cloud's publicly-signed cert (sslrootcert=system in
# the connection string, set at deploy time) — the base image ships
# without one, and unlike the builder stage this layer deliberately does
# NOT copy certs/local-ca.crt (a per-developer TLS-intercepting-proxy
# workaround, sometimes containing real local cert bytes via skip-worktree)
# into the image that actually gets deployed.
#
# CockroachDB Cloud's TLS endpoint sends only its leaf cert, not the
# intermediate ("Let's Encrypt YE2" -> "ISRG Root YE") needed to chain up
# to the publicly-trusted "ISRG Root X2" — confirmed by inspecting the
# handshake directly (openssl s_client -starttls postgres). Clients that
# don't do AIA chasing (libpq doesn't) fail verify-full without it.
# certs/cockroachlabs-lets-encrypt-chain.crt is that intermediate chain,
# fetched from its own AIA URLs (ye2.i.lencr.org, ye.i.lencr.org) — public
# certs, safe to commit, same mechanism as local-ca.crt above.
COPY certs/cockroachlabs-lets-encrypt-chain.crt /usr/local/share/ca-certificates/cockroachlabs-lets-encrypt-chain.crt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# PYTHONHASHSEED must be set before the interpreter starts —
# workload/determinism.py's apply() checks this and fails loudly rather
# than silently running with a determinism guarantee that isn't true.
ENV PYTHONHASHSEED=0 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY cairn.yaml /app/cairn.yaml
COPY db/migrations /app/db/migrations

RUN useradd --create-home --shell /usr/sbin/nologin cairn
USER cairn

# No default work to do — worker tasks are launched via ECS RunTask with
# an explicit containerOverrides command (`cairn claim-demo ...` today;
# `cairn run ...` once the full agent loop lands). This default only has
# to exist so `docker run` without an override doesn't error.
CMD ["cairn", "--help"]

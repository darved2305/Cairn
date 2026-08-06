#!/usr/bin/env bash
# Single-node CockroachDB in Docker for local dev/testing without a
# CockroachDB Cloud account. This is the fallback path PLAN.md §8 calls
# for — same schema, same SERIALIZABLE semantics, same wire protocol, just
# not the vector index or multi-region topology the demo cluster needs.
#
# Usage:
#   ./scripts/local_cluster.sh up      # start it, create the DB, print the URL
#   ./scripts/local_cluster.sh down    # stop and remove the container
set -euo pipefail

CONTAINER_NAME="cairn-crdb-dev"
IMAGE="cockroachdb/cockroach:latest-v25.2"
DB_URL="postgresql://root@localhost:26257/cairn?sslmode=disable"

case "${1:-up}" in
  up)
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
      echo "already running"
    else
      docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
      docker run -d --name "$CONTAINER_NAME" \
        -p 26257:26257 -p 8080:8080 \
        "$IMAGE" start-single-node --insecure >/dev/null
      echo "waiting for SQL to come up..."
      until docker exec "$CONTAINER_NAME" cockroach sql --insecure -e "SELECT 1" >/dev/null 2>&1; do
        sleep 2
      done
      docker exec "$CONTAINER_NAME" cockroach sql --insecure -e "CREATE DATABASE IF NOT EXISTS cairn" >/dev/null
    fi
    echo "export CAIRN_DATABASE_URL=\"$DB_URL\""
    ;;
  down)
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    echo "stopped"
    ;;
  *)
    echo "usage: $0 {up|down}" >&2
    exit 1
    ;;
esac

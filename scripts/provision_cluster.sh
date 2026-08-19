#!/usr/bin/env bash
# Stand up (or reuse) the CockroachDB Cloud dev cluster and write the
# connection string to .env. Idempotent: safe to re-run.
#
# Requires: the `ccloud` CLI, authenticated (`ccloud auth login`), and an
# org with a Standard-plan trial available (docs/project/PLAN.md §8, open decision 1 —
# vector index support is what forces Standard over Basic).
set -euo pipefail

CLUSTER_NAME="${CAIRN_CLUSTER_NAME:-cairn-dev}"
CLUSTER_REGION="${CAIRN_CLUSTER_REGION:-us-east-1}"
SQL_USER="cairn_app"
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"

command -v ccloud >/dev/null 2>&1 || {
  echo "error: ccloud CLI not found on PATH." >&2
  echo "       install: https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started" >&2
  exit 1
}

if ! ccloud auth whoami >/dev/null 2>&1; then
  echo "not logged in — opening ccloud auth login"
  ccloud auth login
fi

if ccloud cluster describe "$CLUSTER_NAME" >/dev/null 2>&1; then
  echo "cluster '$CLUSTER_NAME' already exists, reusing it"
else
  echo "creating cluster '$CLUSTER_NAME' (plan standard, region $CLUSTER_REGION)"
  ccloud cluster create "$CLUSTER_NAME" \
    --plan standard \
    --provider aws \
    --region "$CLUSTER_REGION" \
    --nodes 3
fi

# ccloud cluster user create is idempotent-if-exists on most ccloud versions;
# if the user already exists this just re-prints the same identity.
echo "ensuring app role '$SQL_USER' exists"
ccloud cluster user create "$SQL_USER" --cluster "$CLUSTER_NAME"

CONNECTION_URL="$(ccloud cluster sql --cluster "$CLUSTER_NAME" --user "$SQL_USER" --url)"

if [ -z "$CONNECTION_URL" ]; then
  echo "error: ccloud did not return a connection URL" >&2
  exit 1
fi

if [ -f "$ENV_FILE" ] && grep -q '^CAIRN_DATABASE_URL=' "$ENV_FILE"; then
  # portable in-place edit across GNU/BSD sed
  sed -i.bak "s#^CAIRN_DATABASE_URL=.*#CAIRN_DATABASE_URL=${CONNECTION_URL}#" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
else
  {
    echo "CAIRN_DATABASE_URL=${CONNECTION_URL}"
    echo "CAIRN_CLUSTER_NAME=${CLUSTER_NAME}"
  } >> "$ENV_FILE"
fi

echo "wrote connection URL to $ENV_FILE"
echo "next: make migrate"

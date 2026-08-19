#!/usr/bin/env bash
# Crash-recovery driver — D7. Launches a real ECS task holding a claim via
# `cairn claim-demo`, kills it mid-run with a real ECS StopTask call, waits
# past the 45s lease (db/claims.py::LEASE_SECONDS), then launches a second
# task on the SAME work_key and shows it winning via takeover — a fenced,
# serializable takeover against the real claim protocol (D2), not a
# simulated one.
#
# docs/project/PROJECT.md §4.5 describes the full version of this proof against the
# `features` stage's fragment resume (D3's shard-level checkpointing) —
# this is the claim-protocol layer that mechanism sits on top of; the
# fragment-resume version needs the real workload wired into an
# ECS-invokable command, which is a separate, later piece of work.
#
# Requires: terraform apply already ran (infra/ecs.tf's cluster + task
# definition must exist), and AWS credentials for the account they're in.
#
# CAIRN_CLUSTER/CAIRN_TASK_DEF/CAIRN_SUBNET/CAIRN_SECURITY_GROUP all come
# straight out of infra/outputs.tf, e.g.:
#   export CAIRN_CLUSTER=$(terraform -chdir=infra output -raw cluster_primary)
#   export CAIRN_TASK_DEF=$(terraform -chdir=infra output -raw worker_task_definition_primary)
#   export CAIRN_SUBNET=$(terraform -chdir=infra output -raw subnet_primary)
#   export CAIRN_SECURITY_GROUP=$(terraform -chdir=infra output -raw worker_security_group_primary)
set -euo pipefail

# Git Bash otherwise rewrites leading-slash AWS resource names such as
# /ecs/cairn/worker-us-east-1 into Windows filesystem paths.
export MSYS_NO_PATHCONV=1

CLUSTER="${CAIRN_CLUSTER:?set CAIRN_CLUSTER — see terraform output cluster_primary}"
TASK_DEF="${CAIRN_TASK_DEF:?set CAIRN_TASK_DEF — see terraform output worker_task_definition_primary}"
REGION="${CAIRN_REGION:-us-east-1}"
SUBNET="${CAIRN_SUBNET:?set CAIRN_SUBNET — see terraform output subnet_primary}"
SECURITY_GROUP="${CAIRN_SECURITY_GROUP:?set CAIRN_SECURITY_GROUP — see terraform output worker_security_group_primary}"
DATABASE_URL="${CAIRN_DATABASE_URL:?set CAIRN_DATABASE_URL for direct claim verification}"
WORK_KEY="kill-worker-demo-$(date +%s)"
HOLD_SECONDS="${CAIRN_HOLD_SECONDS:-120}"
LEASE_SECONDS=45 # db/claims.py::LEASE_SECONDS — must match to wait long enough

if [ -z "${CAIRN_PYTHON:-}" ]; then
  if [ -x .venv/Scripts/python.exe ]; then
    CAIRN_PYTHON=.venv/Scripts/python.exe
  elif [ -x .venv/bin/python ]; then
    CAIRN_PYTHON=.venv/bin/python
  else
    CAIRN_PYTHON=python
  fi
fi
export CAIRN_DATABASE_URL="$DATABASE_URL"

_run_task() {
  local hold="$1"
  aws ecs run-task \
    --cluster "$CLUSTER" \
    --task-definition "$TASK_DEF" \
    --launch-type FARGATE \
    --region "$REGION" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SECURITY_GROUP],assignPublicIp=ENABLED}" \
    --overrides "{\"containerOverrides\":[{\"name\":\"worker\",\"command\":[\"cairn\",\"claim-demo\",\"$WORK_KEY\",\"--region\",\"$REGION\",\"--hold-seconds\",\"$hold\"]}]}" \
    --query 'tasks[0].taskArn' --output text
}

_wait_running() {
  local task_arn="$1"
  local deadline=$((SECONDS + 90))
  while [ "$SECONDS" -lt "$deadline" ]; do
    local status
    status="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" --region "$REGION" \
      --query 'tasks[0].lastStatus' --output text)"
    if [ "$status" = "RUNNING" ] || [ "$status" = "STOPPED" ]; then
      return 0
    fi
    sleep 3
  done
  echo "task $task_arn did not reach RUNNING within 90s" >&2
  return 1
}

_task_id() {
  printf '%s' "${1##*/}"
}

_log_stream() {
  printf 'worker/worker/%s' "$(_task_id "$1")"
}

_wait_for_claim() {
  # CloudWatch buffered worker A's first log line until after process exit in
  # real trials. The database claim is the authoritative readiness signal.
  local deadline=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if "$CAIRN_PYTHON" - "$WORK_KEY" <<'PY'
import os
import sys

import psycopg

with psycopg.connect(os.environ["CAIRN_DATABASE_URL"]) as conn:
    row = conn.execute(
        "SELECT state, fence FROM work_claims WHERE work_key = %s",
        (sys.argv[1],),
    ).fetchone()

raise SystemExit(
    0 if row is not None and row[0] in {"CLAIMED", "RUNNING"} and row[1] == 1 else 1
)
PY
    then
      return 0
    fi
    sleep 3
  done
  echo "worker A did not establish the expected fence=1 claim within 120s" >&2
  return 1
}

echo "work_key = $WORK_KEY"
echo "launching worker A (holding ${HOLD_SECONDS}s)..."
TASK_ARN_A="$(_run_task "$HOLD_SECONDS")"
echo "worker A: $TASK_ARN_A"

echo "waiting for worker A's Fargate task to actually reach RUNNING (cold-start image pull can exceed a fixed sleep)..."
_wait_running "$TASK_ARN_A"
echo "worker A is running — waiting for its real fence=1 claim..."
_wait_for_claim

echo "killing worker A mid-run (StopTask)"
aws ecs stop-task \
  --cluster "$CLUSTER" \
  --task "$TASK_ARN_A" \
  --region "$REGION" \
  --reason "kill_worker.sh crash-recovery demo" >/dev/null

WAIT_FOR_EXPIRY=$((LEASE_SECONDS + 10))
echo "worker A killed. Waiting ${WAIT_FOR_EXPIRY}s for its lease to expire..."
sleep "$WAIT_FOR_EXPIRY"

echo "launching worker B on the SAME work_key — expect a fenced takeover"
TASK_ARN_B="$(_run_task 0)"
echo "worker B: $TASK_ARN_B"

echo "waiting for worker B to stop and verifying its exact task log..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN_B" --region "$REGION"
EXIT_B="$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN_B" \
  --region "$REGION" --query 'tasks[0].containers[0].exitCode' --output text)"
LOG_GROUP="/ecs/cairn/worker-${REGION}"
LOG_STREAM_B="$(_log_stream "$TASK_ARN_B")"
LOGS_B="$(aws logs get-log-events --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM_B" --region "$REGION" \
  --query 'events[].message' --output text)"
printf '%s\n' "$LOGS_B"

if [ "$EXIT_B" != "0" ]; then
  echo "worker B exited $EXIT_B" >&2
  exit 1
fi
if ! grep -Fq "WON fence=2" <<<"$LOGS_B"; then
  echo "worker B did not prove fence=2 takeover" >&2
  exit 1
fi
if ! grep -Fq "COMPLETED ok=True" <<<"$LOGS_B"; then
  echo "worker B did not complete the taken-over claim" >&2
  exit 1
fi

echo
echo "PASS: worker B logged WON fence=2 and COMPLETED ok=True after worker A was stopped."

#!/usr/bin/env bash
# Crash-recovery driver — D7. Launches a real ECS task holding a claim via
# `cairn claim-demo`, kills it mid-run with a real ECS StopTask call, waits
# past the 45s lease (db/claims.py::LEASE_SECONDS), then launches a second
# task on the SAME work_key and shows it winning via takeover — a fenced,
# serializable takeover against the real claim protocol (D2), not a
# simulated one.
#
# PROJECT.md §4.5 describes the full version of this proof against the
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

CLUSTER="${CAIRN_CLUSTER:?set CAIRN_CLUSTER — see terraform output cluster_primary}"
TASK_DEF="${CAIRN_TASK_DEF:?set CAIRN_TASK_DEF — see terraform output worker_task_definition_primary}"
REGION="${CAIRN_REGION:-us-east-1}"
SUBNET="${CAIRN_SUBNET:?set CAIRN_SUBNET — see terraform output subnet_primary}"
SECURITY_GROUP="${CAIRN_SECURITY_GROUP:?set CAIRN_SECURITY_GROUP — see terraform output worker_security_group_primary}"
WORK_KEY="kill-worker-demo-$(date +%s)"
HOLD_SECONDS="${CAIRN_HOLD_SECONDS:-120}"
LEASE_SECONDS=45 # db/claims.py::LEASE_SECONDS — must match to wait long enough

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

echo "work_key = $WORK_KEY"
echo "launching worker A (holding ${HOLD_SECONDS}s)..."
TASK_ARN_A="$(_run_task "$HOLD_SECONDS")"
echo "worker A: $TASK_ARN_A"

echo "waiting for worker A's Fargate task to actually reach RUNNING (cold-start image pull can exceed a fixed sleep)..."
_wait_running "$TASK_ARN_A"
echo "worker A is running — waiting 5s for it to acquire the claim..."
sleep 5

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

LOG_GROUP="/ecs/cairn/worker-${REGION}"
echo
echo "worker B should log 'WON fence=2' (fence incremented from worker A's 1) and COMPLETED."
echo "check logs with:"
echo "  aws logs tail '$LOG_GROUP' --since 10m --region $REGION --follow"

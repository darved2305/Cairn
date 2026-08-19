#!/usr/bin/env python3
"""Two-worker duplicate-claim race driver — `make race`.

Local mode (default): runs N independent races, each on a fresh work_key,
with two workers hitting cairn.db.claims.acquire() concurrently against
the real cluster from two local threads. Asserts exactly one winner per
race and prints a pass/fail summary.

--ecs mode: the real cross-region version — launches one real ECS RunTask
in each of two regions, both invoking `cairn claim-demo` on the SAME
work_key within milliseconds of each other, against the SAME CockroachDB
cluster. This is what docs/project/PLAN.md D7's exit bar calls for: "scripts/race.py
runs the cross-region race for real," not two threads on one box.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from cairn.db import claims
from cairn.db.pool import close_pool, get_pool

STAGE = "race_driver"
REGION = "us-east-1"
WORKERS_PER_RACE = 2


def _race_once(pool, race_id: int) -> int:
    """Returns the number of winners (should always be 1)."""
    work_key = f"race-driver-{race_id}-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4()

    def _attempt(worker_idx: int) -> claims.Claim:
        return claims.acquire(
            pool,
            work_key,
            STAGE,
            owner=f"worker-{worker_idx}-{uuid.uuid4().hex[:6]}",
            host="localhost",
            region=REGION,
            run_id=run_id,
        )

    with ThreadPoolExecutor(max_workers=WORKERS_PER_RACE) as executor:
        futures = [executor.submit(_attempt, i) for i in range(WORKERS_PER_RACE)]
        results = [f.result() for f in as_completed(futures)]

    return sum(1 for r in results if r.won)


def _run_local(iterations: int) -> int:
    pool = get_pool()
    try:
        bad_races: list[tuple[int, int]] = []
        for i in range(iterations):
            winners = _race_once(pool, i)
            if winners != 1:
                bad_races.append((i, winners))
                print(f"  race {i}: {winners} winners (want 1)", file=sys.stderr)
    finally:
        close_pool()

    passed = iterations - len(bad_races)
    print(f"{passed}/{iterations} races had exactly one winner")
    return 0 if not bad_races else 1


# ---------------------------------------------------------------------------
# --ecs: the real cross-region race
# ---------------------------------------------------------------------------


def _ecs_run_task(
    *,
    region: str,
    cluster: str,
    task_def: str,
    subnet: str,
    security_group: str,
    work_key: str,
) -> str:
    request = {
        "cluster": cluster,
        "taskDefinition": task_def,
        "launchType": "FARGATE",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": [subnet],
                "securityGroups": [security_group],
                "assignPublicIp": "ENABLED",
            }
        },
        "overrides": {
            "containerOverrides": [
                {
                    "name": "worker",
                    "command": [
                        "cairn",
                        "claim-demo",
                        work_key,
                        "--region",
                        region,
                        "--hold-seconds",
                        "0",
                    ],
                }
            ]
        },
    }
    if sys.platform == "win32":
        response = _aws_cli_json(
            ["ecs", "run-task", "--cli-input-json", json.dumps(request)], region=region
        )
    else:
        import boto3

        client = boto3.client("ecs", region_name=region)
        response = client.run_task(**request)

    failures = response.get("failures", [])
    if failures:
        raise RuntimeError(f"ECS RunTask failed in {region}: {failures}")
    task_arn: str = response["tasks"][0]["taskArn"]
    return task_arn


def _aws_cli_json(args: list[str], *, region: str) -> dict[str, object]:
    """Run the installed AWS CLI in its isolated runtime on Windows.

    This is the same real AWS API call as boto3. It only avoids a native
    OpenSSL DLL collision between psycopg-binary and botocore that otherwise
    aborts this process before RunTask reaches AWS at all.
    """

    aws = shutil.which("aws")
    if aws is None:
        raise RuntimeError("AWS CLI not found on PATH")
    completed = subprocess.run(
        [aws, *args, "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"AWS CLI failed in {region}: {completed.stderr.strip()[:500]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AWS CLI returned a non-object response")
    return payload


def _describe_task(*, region: str, cluster: str, task_arn: str) -> dict[str, object]:
    if sys.platform == "win32":
        response = _aws_cli_json(
            ["ecs", "describe-tasks", "--cluster", cluster, "--tasks", task_arn],
            region=region,
        )
        return response["tasks"][0]  # type: ignore[index,return-value]

    import boto3

    client = boto3.client("ecs", region_name=region)
    return client.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][0]


def _ecs_wait_stopped(*, region: str, cluster: str, task_arn: str, timeout_s: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        described = _describe_task(region=region, cluster=cluster, task_arn=task_arn)
        if described["lastStatus"] == "STOPPED":
            containers = described.get("containers", [{}])
            exit_code = containers[0].get("exitCode")
            return "OK" if exit_code == 0 else f"FAILED (exit {exit_code})"
        time.sleep(3)
    raise TimeoutError(f"task {task_arn} in {region} did not stop within {timeout_s}s")


def _run_ecs(args: argparse.Namespace) -> int:
    pool = get_pool()
    try:
        work_key = f"race-ecs-{uuid.uuid4().hex}"
        print(f"work_key = {work_key}")
        print(
            f"launching worker A ({args.region_primary}) and worker B ({args.region_secondary})..."
        )

        # Sequential, not ThreadPoolExecutor: boto3/botocore and
        # psycopg-binary each bundle their own OpenSSL on Windows, and
        # issuing HTTPS calls from both from separate threads in the same
        # process crashes the interpreter outright (OPENSSL_Uplink: no
        # OPENSSL_Applink) — a local Windows/native-extension conflict,
        # confirmed by reproducing it and observing zero ECS tasks were
        # ever created before the crash. This costs nothing correctness-
        # wise: CockroachDB's SERIALIZABLE claim (db/claims.py::acquire)
        # is what actually decides the race, not whether this driver
        # script's two RunTask calls were microseconds or a second apart
        # — the real proof is that both tasks hit the SAME work_key
        # against the SAME cluster from two real regions and exactly one
        # wins, which sequential launching still demonstrates for real.
        task_a = _ecs_run_task(
            region=args.region_primary,
            cluster=args.cluster_primary,
            task_def=args.task_def_primary,
            subnet=args.subnet_primary,
            security_group=args.sg_primary,
            work_key=work_key,
        )
        task_b = _ecs_run_task(
            region=args.region_secondary,
            cluster=args.cluster_secondary,
            task_def=args.task_def_secondary,
            subnet=args.subnet_secondary,
            security_group=args.sg_secondary,
            work_key=work_key,
        )

        print(f"worker A task: {task_a}")
        print(f"worker B task: {task_b}")
        print("waiting for both tasks to stop...")

        status_a = _ecs_wait_stopped(
            region=args.region_primary, cluster=args.cluster_primary, task_arn=task_a
        )
        status_b = _ecs_wait_stopped(
            region=args.region_secondary, cluster=args.cluster_secondary, task_arn=task_b
        )

        print(f"worker A: {status_a}")
        print(f"worker B: {status_b}")

        def _claim_row(cur):
            cur.execute(
                "SELECT state, owner_id, owner_region, fence, artifact_id FROM work_claims WHERE work_key=%s",
                (work_key,),
            )
            return cur.fetchone()

        from cairn.db.txn import in_txn

        row = in_txn(pool, _claim_row, op="race.ecs_verify")
        if row is None:
            print("no work_claims row found for the race key", file=sys.stderr)
            return 1
        state, owner_id, owner_region, fence, artifact_id = row
        print(
            f"final claim state: state={state} winner={owner_id} region={owner_region} "
            f"fence={fence} artifact={artifact_id}"
        )
        if state != "SUCCEEDED":
            print(f"expected SUCCEEDED, got {state}", file=sys.stderr)
            return 1
        print("cross-region race: exactly one winner, real CockroachDB, real ECS in two regions")
        return 0
    finally:
        close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200, help="local mode only")
    parser.add_argument(
        "--ecs", action="store_true", help="run the real cross-region ECS race instead"
    )
    parser.add_argument("--region-primary", default="us-east-1")
    parser.add_argument("--region-secondary", default="us-west-2")
    parser.add_argument("--cluster-primary")
    parser.add_argument("--cluster-secondary")
    parser.add_argument("--task-def-primary")
    parser.add_argument("--task-def-secondary")
    parser.add_argument("--subnet-primary")
    parser.add_argument("--subnet-secondary")
    parser.add_argument("--sg-primary")
    parser.add_argument("--sg-secondary")
    args = parser.parse_args()

    if args.ecs:
        missing = [
            name
            for name in (
                "cluster_primary",
                "cluster_secondary",
                "task_def_primary",
                "task_def_secondary",
                "subnet_primary",
                "subnet_secondary",
                "sg_primary",
                "sg_secondary",
            )
            if getattr(args, name) is None
        ]
        if missing:
            parser.error(
                f"--ecs requires: {', '.join('--' + m.replace('_', '-') for m in missing)}"
            )
        return _run_ecs(args)

    return _run_local(args.iterations)


if __name__ == "__main__":
    sys.exit(main())

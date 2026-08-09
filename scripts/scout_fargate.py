#!/usr/bin/env python3
"""Run the bundled mapper under ``cairn scout`` on Fargate (Day 2 AWS gate).

Only the project-controlled ``examples/embed_mapper.py --selftest`` path is
accepted — this does not authorize arbitrary remote commands. The worker task
definition must include SYS_PTRACE (infra/ecs.tf). Image must be digest-pinned
via CAIRN_IMAGE_DIGEST / container_image_tag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import boto3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("CAIRN_AWS_REGION", "us-east-1"))
    parser.add_argument(
        "--cluster",
        default=os.environ.get("CAIRN_ECS_CLUSTER"),
        help="ECS cluster name (or CAIRN_ECS_CLUSTER).",
    )
    parser.add_argument(
        "--task-def",
        default=os.environ.get("CAIRN_TASK_DEF"),
        help="Worker task definition ARN/family (or CAIRN_TASK_DEF).",
    )
    parser.add_argument(
        "--subnets",
        default=os.environ.get("CAIRN_SUBNETS", ""),
        help="Comma-separated subnet IDs.",
    )
    parser.add_argument(
        "--security-groups",
        default=os.environ.get("CAIRN_SECURITY_GROUPS", ""),
        help="Comma-separated security group IDs.",
    )
    parser.add_argument(
        "--record-candidate",
        action="store_true",
        help="Pass --record-candidate through to cairn scout.",
    )
    args = parser.parse_args()
    if not args.cluster or not args.task_def:
        print(
            "scout_fargate: set --cluster/--task-def or CAIRN_ECS_CLUSTER/CAIRN_TASK_DEF",
            file=sys.stderr,
        )
        return 2
    subnets = [s for s in args.subnets.split(",") if s]
    sgs = [s for s in args.security_groups.split(",") if s]
    if not subnets or not sgs:
        print("scout_fargate: set --subnets and --security-groups", file=sys.stderr)
        return 2

    command = [
        "cairn",
        "scout",
        "--json",
        "--output-file",
        ".cairn/out/selftest.json",
    ]
    if args.record_candidate:
        command.append("--record-candidate")
    command.extend(["--", "python", "examples/embed_mapper.py", "--selftest"])

    client = boto3.client("ecs", region_name=args.region)
    resp = client.run_task(
        cluster=args.cluster,
        taskDefinition=args.task_def,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": sgs,
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={"containerOverrides": [{"name": "worker", "command": command}]},
    )
    failures = resp.get("failures") or []
    if failures:
        print(json.dumps({"ok": False, "failures": failures}, sort_keys=True, indent=2))
        return 1
    tasks = resp.get("tasks") or []
    if not tasks:
        print(json.dumps({"ok": False, "error": "no tasks started"}, sort_keys=True))
        return 1
    task_arn = tasks[0]["taskArn"]
    print(
        json.dumps({"ok": True, "task_arn": task_arn, "command": command}, sort_keys=True, indent=2)
    )

    # Wait until STOPPED so CloudWatch holds the scout JSON.
    for _ in range(120):
        desc = client.describe_tasks(cluster=args.cluster, tasks=[task_arn])
        task = (desc.get("tasks") or [None])[0]
        if task and task.get("lastStatus") == "STOPPED":
            containers = task.get("containers") or []
            exit_code = containers[0].get("exitCode") if containers else None
            print(
                json.dumps(
                    {
                        "ok": exit_code == 0,
                        "task_arn": task_arn,
                        "exit_code": exit_code,
                        "stopped_reason": task.get("stoppedReason"),
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0 if exit_code == 0 else 1
        time.sleep(5)
    print(json.dumps({"ok": False, "task_arn": task_arn, "error": "timeout"}, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

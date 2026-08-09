#!/usr/bin/env python3
"""Launch the bundled jsonl-map mapper on Fargate (Day 3 AWS gate).

Refuses anything except the project-controlled embed_mapper envelope.
Does not authorize arbitrary remote workspace commands.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import boto3

from cairn.flight.ecs_envelope import JsonlMapEcsEnvelope, cairn_exec_command_from_envelope


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("CAIRN_AWS_REGION", "us-east-1"))
    parser.add_argument("--cluster", default=os.environ.get("CAIRN_ECS_CLUSTER"))
    parser.add_argument("--task-def", default=os.environ.get("CAIRN_TASK_DEF"))
    parser.add_argument("--subnets", default=os.environ.get("CAIRN_SUBNETS", ""))
    parser.add_argument("--security-groups", default=os.environ.get("CAIRN_SECURITY_GROUPS", ""))
    parser.add_argument(
        "--oci-image",
        required=True,
        help="Full immutable OCI ref: REPO@sha256:HEX",
    )
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--id-field", default="id")
    parser.add_argument("--partitions", type=int, default=64)
    parser.add_argument("--output-file", default=".cairn/out/features.jsonl")
    parser.add_argument("--namespace", default="local")
    parser.add_argument(
        "--mapper-argv",
        default='["python","/workspace/examples/embed_mapper.py"]',
        help="JSON argv array for the image-baked mapper",
    )
    args = parser.parse_args()

    if not args.cluster or not args.task_def:
        print("jsonl_map_fargate: set --cluster/--task-def", file=sys.stderr)
        return 2
    subnets = [s for s in args.subnets.split(",") if s]
    sgs = [s for s in args.security_groups.split(",") if s]
    if not subnets or not sgs:
        print("jsonl_map_fargate: set --subnets and --security-groups", file=sys.stderr)
        return 2
    if "@sha256:" not in args.oci_image:
        print("jsonl_map_fargate: --oci-image must include @sha256:", file=sys.stderr)
        return 2

    argv = tuple(json.loads(args.mapper_argv))
    image_digest = "sha256:" + args.oci_image.rsplit("@sha256:", 1)[1]
    input_path = Path(args.input_file)
    if not input_path.is_file():
        print(f"jsonl_map_fargate: missing input {input_path}", file=sys.stderr)
        return 2
    slice_digest = hashlib.sha256(input_path.read_bytes()).hexdigest()

    envelope = JsonlMapEcsEnvelope(
        argv=argv,
        oci_image=args.oci_image,
        image_digest=image_digest,
        input_file=args.input_file,
        id_field=args.id_field,
        partitions=args.partitions,
        output_file=args.output_file,
        namespace_id=args.namespace,
        slice_manifest_digest=slice_digest,
    )
    command = cairn_exec_command_from_envelope(envelope)

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
        tags=[
            {"key": "cairn.contract", "value": "jsonl-map/v1"},
            {"key": "cairn.slice_manifest_digest", "value": slice_digest},
        ],
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
        json.dumps(
            {
                "ok": True,
                "task_arn": task_arn,
                "envelope": envelope.as_dict(),
                "command": command,
            },
            sort_keys=True,
            indent=2,
        )
    )
    for _ in range(180):
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

#!/usr/bin/env python3
"""Gate D whole-stage takeover driver (Day 5 kill-order fallback).

Measured Gate C leaf bucket 26 had microchunk_count=1 and ~1s from first
commit to leaf row — not enough for sub-leaf StopTask. This driver proves
real ECS death + lease expiry + higher-fence replacement for jsonl-map/v1
without claiming resumed microchunks.

Requires: AWS profile with ecs:RunTask/StopTask, worker task role with
CRDB+S3 (already used for Gate C). DB polls run as short ECS probe tasks
because developer laptops may not reach CockroachDB :26257.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3

REGION = os.environ.get("CAIRN_AWS_REGION", "us-east-1")
PROFILE = os.environ.get("AWS_PROFILE", "cairn")
CLUSTER = os.environ.get("CAIRN_ECS_CLUSTER", "cairn-us-east-1")
TASK_FAMILY = os.environ.get("CAIRN_ECS_TASK_FAMILY", "cairn-worker-us-east-1")
BUCKET = os.environ["CAIRN_S3_BUCKET"]
IMAGE_REF = os.environ["CAIRN_IMAGE_REF"]  # REPO@sha256:...
SUBNETS = [s for s in os.environ["CAIRN_ECS_SUBNETS"].split(",") if s]
SG = os.environ["CAIRN_ECS_SECURITY_GROUP"]
ROW_COUNT = int(os.environ.get("GATE_D_ROW_COUNT", "40"))
LEASE_SECONDS = 45

OUT = Path(".cairn/out/gate_d_whole")
OUT.mkdir(parents=True, exist_ok=True)


def _session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def _network() -> dict[str, object]:
    return {
        "awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": [SG],
            "assignPublicIp": "ENABLED",
        }
    }


def _register_taskdef(ecs: object) -> str:
    td = ecs.describe_task_definition(taskDefinition=TASK_FAMILY)["taskDefinition"]
    for k in (
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    ):
        td.pop(k, None)
    cd = td["containerDefinitions"][0]
    cd["image"] = IMAGE_REF
    digest = IMAGE_REF.rsplit("@", 1)[-1]
    env = [e for e in cd.get("environment", []) if e.get("name") != "CAIRN_IMAGE_DIGEST"]
    env.append({"name": "CAIRN_IMAGE_DIGEST", "value": digest})
    cd["environment"] = env
    (OUT / "taskdef.json").write_text(json.dumps(td), encoding="utf-8")
    resp = ecs.register_task_definition(**td)
    arn = resp["taskDefinition"]["taskDefinitionArn"]
    print(f"taskdef {arn}", flush=True)
    return str(arn)


def _run(ecs: object, taskdef: str, command: list[str], *, tag: str) -> str:
    resp = ecs.run_task(
        cluster=CLUSTER,
        taskDefinition=taskdef,
        launchType="FARGATE",
        networkConfiguration=_network(),
        overrides={"containerOverrides": [{"name": "worker", "command": command}]},
        tags=[{"key": "cairn.gate", "value": tag}],
    )
    failures = resp.get("failures") or []
    if failures:
        raise RuntimeError(f"RunTask failures: {failures}")
    arn = resp["tasks"][0]["taskArn"]
    print(f"started {tag}: {arn}", flush=True)
    return str(arn)


def _wait_stopped(ecs: object, arn: str, *, timeout: int = 3600) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = ecs.describe_tasks(cluster=CLUSTER, tasks=[arn])["tasks"][0]
        if d["lastStatus"] == "STOPPED":
            return d["containers"][0].get("exitCode")  # type: ignore[no-any-return]
        time.sleep(10)
    raise TimeoutError(arn)


def _log_tail(logs: object, arn: str, *, limit: int = 40) -> list[str]:
    stream = "worker/worker/" + arn.rsplit("/", 1)[-1]
    try:
        r = logs.get_log_events(
            logGroupName="/ecs/cairn/worker-us-east-1",
            logStreamName=stream,
            startFromHead=True,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostics
        return [f"log fetch failed: {exc}"]
    return [e["message"] for e in r.get("events", [])][-limit:]


def _owner_command(ns: str, corpus_key: str) -> list[str]:
    # boto3 is in the image; aws CLI may not be. Download corpus then exec.
    code = f"""
import json, pathlib, subprocess, boto3, os
ns = {ns!r}
key = {corpus_key!r}
bucket = os.environ['CAIRN_S3_BUCKET']
out_dir = pathlib.Path('.cairn/out/gate_d_whole')
out_dir.mkdir(parents=True, exist_ok=True)
corpus = out_dir / 'corpus.jsonl'
s3 = boto3.client('s3', region_name=os.environ.get('CAIRN_AWS_REGION','us-east-1'))
s3.download_file(bucket, key, str(corpus))
cmd = [
  'cairn','exec','--contract','jsonl-map/v1','--namespace',ns,
  '--input-file',str(corpus),'--id-field','id','--partitions','64',
  '--output-file',str(out_dir/'features.jsonl'),'--json','--',
  'python','examples/embed_mapper.py'
]
p = subprocess.run(cmd, check=False)
raise SystemExit(p.returncode)
"""
    return ["python", "-c", code]


def _probe_owner_command(ns: str) -> list[str]:
    code = f"""
import json, os, time, psycopg
ns = {ns!r}
deadline = time.time() + 180
while time.time() < deadline:
    with psycopg.connect(os.environ['CAIRN_DATABASE_URL']) as conn, conn.cursor() as cur:
        cur.execute(
            '''SELECT wc.owner_id, wc.fence, wc.state FROM work_claims wc
               JOIN work_generations g ON g.claim_key = wc.work_key
               WHERE g.namespace_id = %s AND wc.state IN ('CLAIMED','RUNNING')
               ORDER BY wc.fence DESC LIMIT 5''',
            (ns,),
        )
        rows = cur.fetchall()
        if rows:
            print(json.dumps({{
                'ok': True,
                'owners': [{{'owner_id': r[0], 'fence': r[1], 'state': r[2]}} for r in rows],
            }}, sort_keys=True))
            raise SystemExit(0)
    time.sleep(2)
print(json.dumps({{'ok': False, 'error': 'no live owner within 180s'}}))
raise SystemExit(1)
"""
    return ["python", "-c", code]


def _probe_fences_command(ns: str) -> list[str]:
    code = f"""
import json, os, psycopg
ns = {ns!r}
with psycopg.connect(os.environ['CAIRN_DATABASE_URL']) as conn, conn.cursor() as cur:
    cur.execute(
        '''SELECT wc.fence, wc.state FROM work_claims wc
           JOIN work_generations g ON g.claim_key = wc.work_key
           WHERE g.namespace_id = %s ORDER BY wc.fence''',
        (ns,),
    )
    rows = cur.fetchall()
fences = sorted({{r[0] for r in rows}})
print(json.dumps({{
    'ok': len(fences) >= 2,
    'fences': fences,
    'rows': [{{'fence': r[0], 'state': r[1]}} for r in rows],
}}, sort_keys=True))
raise SystemExit(0 if len(fences) >= 2 else 1)
"""
    return ["python", "-c", code]


def main() -> int:
    ns = f"gate-d-ws-{int(time.time())}"
    corpus = OUT / "corpus.jsonl"
    subprocess.check_call(
        [sys.executable, "scripts/cairnbench_generate.py", "--count", str(ROW_COUNT), "--output", str(corpus)]
    )
    corpus_key = f"datasets/gate-d/{ns}/corpus.jsonl"
    session = _session()
    s3 = session.client("s3")
    s3.upload_file(str(corpus), BUCKET, corpus_key)
    print(f"corpus s3://{BUCKET}/{corpus_key}", flush=True)

    ecs = session.client("ecs")
    logs = session.client("logs")
    taskdef = _register_taskdef(ecs)

    owner = _run(ecs, taskdef, _owner_command(ns, corpus_key), tag="gate-d-owner")
    (OUT / "owner_task_arn.txt").write_text(owner + "\n", encoding="utf-8")

    # Wait until ECS reports RUNNING, then confirm CRDB claim via probe.
    for _ in range(60):
        st = ecs.describe_tasks(cluster=CLUSTER, tasks=[owner])["tasks"][0]["lastStatus"]
        print(f"owner status={st}", flush=True)
        if st in {"RUNNING", "STOPPED"}:
            break
        time.sleep(5)
    if st == "STOPPED":
        print("owner stopped before probe — logs:", flush=True)
        print("\n".join(_log_tail(logs, owner)), flush=True)
        return 1

    probe = _run(ecs, taskdef, _probe_owner_command(ns), tag="gate-d-probe-owner")
    probe_exit = _wait_stopped(ecs, probe, timeout=240)
    print(f"owner-claim probe exit={probe_exit}", flush=True)
    print("\n".join(_log_tail(logs, probe)), flush=True)
    if probe_exit != 0:
        return 1

    print("StopTask owner", flush=True)
    t0 = time.time()
    ecs.stop_task(cluster=CLUSTER, task=owner, reason="gate_d_whole_stage")
    owner_exit = _wait_stopped(ecs, owner, timeout=300)
    print(f"owner STOPPED exit={owner_exit} after {time.time() - t0:.1f}s", flush=True)

    print(f"waiting lease expiry ({LEASE_SECONDS + 1}s)", flush=True)
    time.sleep(LEASE_SECONDS + 1)

    repl = _run(ecs, taskdef, _owner_command(ns, corpus_key), tag="gate-d-replacement")
    (OUT / "replacement_task_arn.txt").write_text(repl + "\n", encoding="utf-8")
    repl_exit = _wait_stopped(ecs, repl, timeout=3600)
    print(f"replacement exit={repl_exit}", flush=True)
    repl_logs = _log_tail(logs, repl, limit=80)
    print("\n".join(repl_logs), flush=True)
    if repl_exit != 0:
        return 1

    fence = _run(ecs, taskdef, _probe_fences_command(ns), tag="gate-d-probe-fence")
    fence_exit = _wait_stopped(ecs, fence, timeout=240)
    fence_logs = _log_tail(logs, fence)
    print(f"fence probe exit={fence_exit}", flush=True)
    print("\n".join(fence_logs), flush=True)
    if fence_exit != 0:
        return 1

    result = {
        "gate": "D",
        "mode": "whole_stage_takeover",
        "sub_leaf_resume_claimed": False,
        "namespace": ns,
        "owner_task_arn": owner,
        "replacement_task_arn": repl,
        "image": IMAGE_REF,
        "row_count": ROW_COUNT,
        "timing_basis": {
            "REAL_RECORD_ID": "cb-000020",
            "REAL_BUCKET": 26,
            "microchunk_count": 1,
            "seconds_first_microchunk_to_leaf_row": 1.022625,
            "decision": "whole_stage_takeover",
        },
        "owner_exit": owner_exit,
        "replacement_exit": repl_exit,
        "ok": True,
    }
    (OUT / "RESULT.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("Gate D WHOLE-STAGE PASSED (StopTask + lease + higher fence + replacement exit 0)", flush=True)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyError as exc:
        print(f"missing env {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

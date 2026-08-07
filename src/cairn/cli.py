"""Cairn command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cairn.config import ConfigError, TrackedConfig
from cairn.db import claims
from cairn.db.graph import persist_code_graph
from cairn.db.pool import close_pool, get_pool
from cairn.db.txn import in_txn
from cairn.planner import PipelinePlan, plan_pipeline
from cairn.workload.stage_env import EnvManifest
from cairn.workload.stage_env import run as capture_environment

app = typer.Typer(no_args_is_help=True, help="Causal reuse memory for expensive compute.")


@app.callback()
def main() -> None:
    """Causal reuse memory for expensive compute."""


@app.command("plan")
def plan_command(
    config_path: Annotated[
        Path, typer.Option("--config", exists=True, dir_okay=False, readable=True)
    ] = Path("cairn.yaml"),
    source_root: Annotated[
        Path, typer.Option("--source-root", exists=True, file_okay=False, readable=True)
    ] = Path("src"),
    output: Annotated[str, typer.Option("--output", help="table or json")] = "table",
    persist_graph: Annotated[
        bool, typer.Option("--persist-graph", help="Write code_units/code_edges to CockroachDB")
    ] = False,
) -> None:
    """Print deterministic work keys for all five pipeline stages."""

    if output not in {"table", "json"}:
        raise typer.BadParameter("must be 'table' or 'json'", param_hint="--output")
    try:
        config = TrackedConfig.load(config_path)
        environment = capture_environment()
        result = plan_pipeline(
            config,
            source_root=source_root,
            env_fingerprint=environment.env_fingerprint,
        )
    except (ConfigError, KeyError, ValueError) as exc:
        typer.echo(f"plan failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if persist_graph:
        _persist(result)
    if output == "json":
        typer.echo(json.dumps(_as_json(result), sort_keys=True, indent=2))
    else:
        _print_table(result)


@app.command("claim-demo")
def claim_demo_command(
    work_key: Annotated[str, typer.Argument(help="The work_key to claim.")],
    stage: Annotated[str, typer.Option("--stage")] = "race_driver",
    region: Annotated[str, typer.Option("--region")] = "us-east-1",
    owner: Annotated[str | None, typer.Option("--owner", help="Defaults to hostname-pid.")] = None,
    hold_seconds: Annotated[
        float,
        typer.Option(
            "--hold-seconds",
            help=(
                "Sleep while heartbeating before completing — gives "
                "scripts/kill_worker.sh a window to kill this process mid-run "
                "and prove a real takeover (PROJECT.md §4.5)."
            ),
        ),
    ] = 0.0,
    poll_interval_seconds: Annotated[
        float, typer.Option("--poll-interval-s", help="Loser's subscribe() poll interval.")
    ] = 1.0,
    max_wait_seconds: Annotated[
        float, typer.Option("--max-wait-s", help="Loser's subscribe() timeout.")
    ] = 600.0,
) -> None:
    """Exercise the real distributed claim protocol (D2) as a standalone
    process — acquire / heartbeat / complete / subscribe — without the
    full nine-action agent loop, which reads config and a code diff this
    command doesn't need. This is what ECS RunTask invokes twice, once per
    region, for the D7 cross-region race and takeover demos
    (scripts/race.py, scripts/kill_worker.sh)."""

    host = socket.gethostname()
    resolved_owner = owner or f"{host}-{os.getpid()}"
    run_id = uuid.uuid4()
    pool = get_pool()
    try:
        environment = capture_environment()
        _ensure_environment(pool, environment)

        claim = claims.acquire(pool, work_key, stage, resolved_owner, host, region, run_id)
        if not claim.won:
            if claim.reuse_artifact_id is not None:
                typer.echo(f"REUSE artifact={claim.reuse_artifact_id}")
                return
            typer.echo(
                f"SUBSCRIBE owner={claim.owner} host={claim.owner_host} region={claim.owner_region}"
            )
            result = claims.subscribe(
                pool,
                work_key,
                poll_interval_s=poll_interval_seconds,
                max_wait_s=max_wait_seconds,
            )
            typer.echo(f"ADOPTED state={result.terminal_state} artifact={result.artifact_id}")
            return

        assert claim.fence is not None, "acquire() must set fence when won=True"
        fence = claim.fence
        typer.echo(f"WON fence={fence}")
        deadline = time.monotonic() + hold_seconds
        while time.monotonic() < deadline:
            time.sleep(min(claims.HEARTBEAT_SECONDS, max(0.0, deadline - time.monotonic())))
            if not claims.heartbeat(pool, work_key, resolved_owner, fence):
                typer.echo("DISPOSSESSED", err=True)
                raise typer.Exit(code=1)

        payload = f"{work_key}:{run_id}".encode()
        artifact_id = hashlib.sha256(payload).hexdigest()
        record = claims.ArtifactRecord(
            artifact_id=artifact_id,
            stage=stage,
            work_key=work_key,
            s3_uri=f"s3://cairn-claim-demo/{artifact_id}",
            size_bytes=len(payload),
            env_fingerprint=environment.env_fingerprint,
            produced_by_run=run_id,
            duration_ms=int(hold_seconds * 1000),
            vcpu=2.0,
            mem_mib=4096,
            region=region,
        )
        completed = claims.complete(pool, work_key, resolved_owner, fence, record)
        typer.echo(f"COMPLETED ok={completed} artifact={artifact_id}")
    finally:
        close_pool()


def _ensure_environment(pool: ConnectionPool, environment: EnvManifest) -> None:
    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO environments (env_fingerprint, image_digest, python_version, deps, torch_threads)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (env_fingerprint) DO NOTHING
            """,
            (
                environment.env_fingerprint,
                environment.image_digest,
                environment.python_version,
                Jsonb(environment.deps),
                environment.torch_threads,
            ),
        )

    in_txn(pool, _tx, op="cli.ensure_environment")


def _persist(result: PipelinePlan) -> None:
    if not os.environ.get("CAIRN_DATABASE_URL"):
        typer.echo("--persist-graph requires CAIRN_DATABASE_URL", err=True)
        raise typer.Exit(code=2)
    commit_sha = _commit_sha()
    pool = get_pool()
    try:
        unit_count, edge_count = persist_code_graph(pool, result.code_graph, commit_sha=commit_sha)
    finally:
        close_pool()
    typer.echo(f"persisted {unit_count} code units and {edge_count} edges at {commit_sha[:12]}")


def _commit_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _print_table(result: PipelinePlan) -> None:
    typer.echo(
        "STAGE       WORK KEY                                                          INPUTS"
    )
    for stage in result.stages:
        sound = "sound" if stage.structurally_sound else "unsound"
        provisional = ", provisional upstream" if stage.provisional_upstream else ""
        typer.echo(
            f"{stage.stage:<11} {stage.work_key.value}  "
            f"{len(stage.config_reads)} config / {len(stage.reachable_units)} code "
            f"({sound}{provisional})"
        )
        if stage.escape_hatches:
            typer.echo(f"            escape hatches: {', '.join(stage.escape_hatches)}")


def _as_json(result: PipelinePlan) -> dict[str, object]:
    return {
        "data_fingerprint": result.data_fingerprint,
        "env_fingerprint": result.env_fingerprint,
        "stages": [
            {
                "stage": stage.stage,
                "work_key": stage.work_key.value,
                "code_fingerprint": stage.work_key.code_fingerprint,
                "config_fingerprint": stage.work_key.config_fingerprint,
                "config_reads": stage.config_reads,
                "upstream": stage.work_key.upstream,
                "provisional_upstream": stage.provisional_upstream,
                "structurally_sound": stage.structurally_sound,
                "escape_hatches": stage.escape_hatches,
            }
            for stage in result.stages
        ],
    }

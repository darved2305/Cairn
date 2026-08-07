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

import typer
from psycopg_pool import ConnectionPool

from cairn.agent.loop import Escalation, Refusal, run_pipeline
from cairn.config import ConfigError, TrackedConfig
from cairn.db import claims
from cairn.db.contradictions import unquarantine as unquarantine_artifact
from cairn.db.environments import ensure_environment
from cairn.db.graph import persist_code_graph
from cairn.db.pool import close_pool, get_pool
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


@app.command("run")
def run_command(
    stage: Annotated[
        str, typer.Argument(help="Target stage: env, dataset, features, checkpoint, or eval.")
    ] = "eval",
    all_stages: Annotated[
        bool, typer.Option("--all", help="Alias for `stage=eval` — runs the whole DAG.")
    ] = False,
    config_path: Annotated[
        Path, typer.Option("--config", exists=True, dir_okay=False, readable=True)
    ] = Path("cairn.yaml"),
    source_root: Annotated[
        Path, typer.Option("--source-root", exists=True, file_okay=False, readable=True)
    ] = Path("src"),
    bucket: Annotated[str, typer.Option("--bucket", envvar="CAIRN_S3_BUCKET")] = "cairn-dev",
    owner: Annotated[str | None, typer.Option("--owner", help="Defaults to hostname-pid.")] = None,
    region: Annotated[str, typer.Option("--region")] = "us-east-1",
    approval_usd: Annotated[
        float, typer.Option("--approval-usd", envvar="CAIRN_APPROVAL_USD")
    ] = 0.50,
) -> None:
    """The agent loop (PROJECT.md §6.4): perceive, recall, decide, act,
    learn — for real, against the live cluster and real S3 storage. Exits
    non-zero on REFUSE_DOOMED/REFUSE_DUPLICATE (2) or ESCALATE (3), the
    same way `cairn plan` exits non-zero on a doomed plan, so this is
    usable as a CI gate."""

    target_stage = "eval" if all_stages else stage
    host = socket.gethostname()
    resolved_owner = owner or f"{host}-{os.getpid()}"
    pool = get_pool()
    try:
        config = TrackedConfig.load(config_path)
        outcomes = run_pipeline(
            pool,
            config=config,
            source_root=str(source_root),
            bucket=bucket,
            owner=resolved_owner,
            host=host,
            region=region,
            target_stage=target_stage,
            approval_usd=approval_usd,
        )
        for outcome in outcomes:
            typer.echo(
                f"{outcome.stage:<11} {outcome.action.value:<10} work_key={outcome.work_key} "
                f"artifact={outcome.artifact.artifact_id if outcome.artifact else '-'} "
                f"({outcome.detail})"
            )
    except Refusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    except Escalation as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    except (ConfigError, ValueError) as exc:
        typer.echo(f"run failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        close_pool()


@app.command("unquarantine")
def unquarantine_command(
    artifact_id: Annotated[str, typer.Argument(help="The quarantined artifact_id to clear.")],
    reason: Annotated[str, typer.Option("--reason", help="Required audit trail.")],
) -> None:
    """PROJECT.md §6.5: quarantine is one-way except through this explicit,
    audited human override."""

    pool = get_pool()
    try:
        cleared = unquarantine_artifact(pool, artifact_id, reason)
    finally:
        close_pool()
    if not cleared:
        typer.echo(f"artifact_id={artifact_id!r} was not quarantined", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"unquarantined artifact_id={artifact_id}")


def _ensure_environment(pool: ConnectionPool, environment: EnvManifest) -> None:
    ensure_environment(
        pool,
        env_fingerprint=environment.env_fingerprint,
        image_digest=environment.image_digest,
        python_version=environment.python_version,
        deps=environment.deps,
        torch_threads=environment.torch_threads,
    )


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

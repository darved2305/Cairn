"""Cairn command-line interface."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from cairn.config import ConfigError, TrackedConfig
from cairn.db.graph import persist_code_graph
from cairn.db.pool import close_pool, get_pool
from cairn.planner import PipelinePlan, plan_pipeline
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

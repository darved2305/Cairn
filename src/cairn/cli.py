"""Cairn command-line interface."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import psycopg
import typer
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from dotenv import find_dotenv, load_dotenv
from psycopg_pool import ConnectionPool

from cairn.agent.loop import Escalation, Refusal, run_pipeline
from cairn.config import ConfigError, TrackedConfig
from cairn.db import claims
from cairn.db.contradictions import Contradiction
from cairn.db.contradictions import contradictions_for_artifact as _contradictions_for_artifact
from cairn.db.contradictions import unquarantine as unquarantine_artifact
from cairn.db.decisions import DecisionRecord
from cairn.db.decisions import decisions_for_artifact as _decisions_for_artifact
from cairn.db.decisions import latest_refusal as _latest_refusal
from cairn.db.environments import ensure_environment
from cairn.db.graph import (
    ArtifactDetails,
    describe_artifact,
    downstream_artifacts,
    persist_code_graph,
)
from cairn.db.memory import search_text, vector_index_status
from cairn.db.pool import close_pool, get_pool
from cairn.db.txn import in_txn
from cairn.embeddings import default_provider
from cairn.obs.events import emit_event
from cairn.planner import PipelinePlan, plan_pipeline
from cairn.workload.stage_env import EnvManifest
from cairn.workload.stage_env import run as capture_environment

# Infra failures a real cloud deployment actually produces (connection
# drops, DNS blips, TLS handshake timeouts against CockroachDB Cloud;
# throttling/auth errors against Bedrock; RuntimeError covers both a
# missing CAIRN_DATABASE_URL — db/pool.py::_env_url — and
# cairn.embeddings.EmbeddingError, which subclasses it) — every command
# below should degrade the way `doctor` already does: one clean line and
# a correct exit code, never a raw Python traceback.
_INFRA_ERRORS: tuple[type[BaseException], ...] = (
    RuntimeError,
    psycopg.OperationalError,
    ClientError,
    EndpointConnectionError,
    BotoCoreError,
    TimeoutError,
    subprocess.CalledProcessError,
)


class ChildExit(Exception):
    """Wrapped child terminated — ``_graceful`` must re-raise as the child's code.

    Cairn's own infra failures become exit 1; the traced command's status
    must pass through unchanged so ``cairn scout`` is transparent to scripts.
    """

    def __init__(self, code: int) -> None:
        self.code = int(code)
        super().__init__(f"child exited with {self.code}")


def _graceful[T](fn: Callable[..., T]) -> Callable[..., T]:
    """Outer safety net around a command body. Command-specific exceptions
    (Refusal/Escalation/ConfigError/...) are already handled inside the
    function and raise typer.Exit themselves — that passes through here
    untouched. ``ChildExit`` re-raises as the child's status. Anything in
    _INFRA_ERRORS that reaches this point instead gets one clean message + a
    non-zero exit, and an emitted event so a TUI/console watching the event
    stream still sees the failure."""

    name = fn.__name__.removesuffix("_command")

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> T:
        try:
            return fn(*args, **kwargs)
        except typer.Exit:
            raise
        except ChildExit as exc:
            close_pool()
            raise typer.Exit(code=exc.code) from None
        except _INFRA_ERRORS as exc:
            typer.echo(f"cairn {name}: {exc}", err=True)
            emit_event(
                "cli.infra_error",
                {"command": name, "error_class": type(exc).__name__, "error": str(exc)},
            )
            close_pool()
            raise typer.Exit(code=1) from exc

    return wrapper


app = typer.Typer(help="Causal reuse memory for expensive compute.")
memory_app = typer.Typer(help="Query negative computational memory directly — PROJECT.md §7.1.")
app.add_typer(memory_app, name="memory")

# Mirrors this repo's own cairn.yaml (PLAN.md §1's stage registry shape) —
# `init` gives a fresh repo a starting point that round-trips through
# TrackedConfig.load, not a minimal/empty stub that would fail plan/run.
_INIT_TEMPLATE = """\
version: 1

data:
  # Set by scripts/vendor_dataset.py to the content digest of the vendored
  # dataset snapshot. Left explicit and provisional here so a fresh `cairn
  # plan` is honest about not having vendored data yet.
  snapshot_digest: unvendored-dataset-v1

dataset:
  test_every_nth: 5

features:
  model: sentence-transformers/all-MiniLM-L6-v2
  embedding_dim: 384
  max_seq_length: 256
  batch_size: 32
  shard_count: 3

train:
  input_dim: 384
  hidden_dim: 256
  num_labels: 4
  epochs: 12
  learning_rate: 0.001
  batch_size: 32

eval:
  metrics:
    - accuracy
    - macro_f1
"""


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Causal reuse memory for expensive compute.

    Bare `cairn` (no subcommand) launches the interactive terminal when
    stdout is a TTY — every other invocation (a real subcommand, or bare
    `cairn` piped/redirected) is completely unaffected: this callback
    only acts when `ctx.invoked_subcommand` is None, and even then it
    falls back to printing help rather than launching a TUI into a
    non-interactive pipe (Part B §41's amendment)."""

    # `scripts/provision_cluster.sh` writes the live connection details to
    # `.env`, and the documented quickstart immediately invokes `make`/`cairn`
    # without asking the user to source that file. Load the nearest project
    # file from the invocation directory and preserve any explicit process
    # environment overrides. Passing values through the process environment
    # also keeps connection strings containing `&` intact on Windows.
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)

    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdout.isatty():
        typer.echo(ctx.get_help())
        return
    _launch_tui()


def _launch_tui() -> None:
    """Spawn the native TUI binary (tui-rs/crates/cairn-tui) as a
    subprocess with inherited stdio, so raw terminal I/O passes straight
    through, and forward its exit code as our own.

    The binary is self-contained, so there is no interpreter to locate
    first — the Node dependency the TypeScript TUI carried is gone.
    `CAIRN_PYTHON` still tells the TUI which interpreter to re-invoke for
    the real `cairn <subcommand>` calls it makes as it runs
    (tui-rs/crates/cairn-backend/src/lib.rs) — the same venv this bare
    invocation is already running under, not a PATH lookup."""

    entry = _resolve_tui_entry()
    if entry is None:
        typer.echo(
            "cairn: the interactive terminal isn't built yet. "
            "Run `cd tui-rs && cargo build --release`.",
            err=True,
        )
        typer.echo(
            "Run a specific subcommand instead, e.g. `cairn plan` or `cairn doctor`.", err=True
        )
        raise typer.Exit(code=1)

    env = dict(os.environ)
    env["CAIRN_PYTHON"] = sys.executable
    completed = subprocess.run([str(entry)], env=env, check=False)
    raise typer.Exit(code=completed.returncode)


def _resolve_tui_entry() -> Path | None:
    """Bundled location first (an installed package ships
    `cairn/_tui/cairn-tui[.exe]`), falling back to this repo's own
    `tui-rs/target/` build for local development straight from a git
    checkout. `CAIRN_TUI_ENTRY` is an explicit override for both cases,
    mainly for tests.

    A debug build is accepted as a last resort with a warning rather than
    silently: it is several times slower to start and is not what a
    packaged install would ever use, so an operator seeing sluggish
    startup deserves to know which binary they are actually running."""

    suffix = ".exe" if sys.platform == "win32" else ""

    override = os.environ.get("CAIRN_TUI_ENTRY")
    if override:
        path = Path(override)
        return path if path.is_file() else None

    bundled = Path(__file__).resolve().parent / "_tui" / f"cairn-tui{suffix}"
    if bundled.is_file():
        return bundled

    target = Path(__file__).resolve().parents[2] / "tui-rs" / "target"
    release = target / "release" / f"cairn-tui{suffix}"
    if release.is_file():
        return release
    debug = target / "debug" / f"cairn-tui{suffix}"
    if debug.is_file():
        typer.echo(
            "cairn: using the debug build of the interactive terminal. "
            "Run `cd tui-rs && cargo build --release` for the real thing.",
            err=True,
        )
        return debug
    return None


@app.command(
    "scout",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@_graceful
def scout_command(
    ctx: typer.Context,
    output_file: Annotated[
        Path,
        typer.Option("--output-file", help="Declared regular output file (workspace-relative)."),
    ] = Path(".cairn/out/scout.bin"),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable trace summary on stdout.")
    ] = False,
    record_candidate: Annotated[
        bool,
        typer.Option(
            "--record-candidate",
            help="Persist trace + CANDIDATE/INCOMPLETE observation in CockroachDB.",
        ),
    ] = False,
    namespace_id: Annotated[
        str, typer.Option("--namespace", help="Namespace for --record-candidate.")
    ] = "local",
    keep_raw: Annotated[
        bool, typer.Option("--keep-raw-trace", help="Retain the private strace log.")
    ] = False,
) -> None:
    """Trace COMMAND under the Flight Recorder collector — evidence only, never reuse.

    Usage: ``cairn scout --output-file FILE -- COMMAND``

    Native Windows runs without portable identity (INCOMPLETE_PLATFORM /
    SHADOW_UNQUALIFIED). Gate A: observation alone does not authorize
    generic verified ``cairn exec`` reuse.
    """
    from cairn.flight import identity as flight_identity
    from cairn.trace.normalize import semantic_resource_set
    from cairn.trace.scout import ARBITRARY_EXEC_COVERAGE, cleanup_scout_tmpdir, run_scout

    raw_args = list(ctx.args)
    if raw_args and raw_args[0] == "--":
        raw_args = raw_args[1:]
    if not raw_args:
        typer.echo("cairn scout: COMMAND is required after --", err=True)
        raise typer.Exit(code=2)

    result = run_scout(
        raw_args,
        output_file=output_file,
        namespace_id=namespace_id,
        keep_raw_trace=keep_raw,
    )
    trace = result.trace
    semantic = semantic_resource_set(trace)
    t_digest = flight_identity.trace_digest(trace)

    observation: dict[str, object] | None = None
    if record_candidate:
        from cairn.db.flight import persist_candidate_observation

        pool = get_pool()
        run_id = uuid.uuid4()
        region = (
            os.environ.get("CAIRN_WORKER_REGION") or os.environ.get("CAIRN_AWS_REGION") or "local"
        )
        persisted = persist_candidate_observation(
            pool,
            namespace_id=namespace_id,
            spec=result.spec,
            trace=trace,
            run_id=run_id,
            region=region,
            task_arn=result.task_arn,
        )
        observation = {
            "observation_id": str(persisted.observation_id),
            "trace_digest": persisted.trace_digest,
            "run_id": str(persisted.run_id),
            "lifecycle_state": persisted.lifecycle_state,
        }
        close_pool()

    payload = {
        "coverage_state": trace.coverage_state.value,
        "incomplete_reasons": list(trace.incomplete_reasons),
        "trace_digest": t_digest,
        "child_exit_code": result.child_exit_code,
        "resource_count": len(trace.resources),
        "semantic_resources": [
            {
                "kind": r.kind.value,
                "ref": r.ref,
                "access_mode": r.access_mode.value,
                "exists": r.exists,
                "version_digest": r.version_digest,
                "resolver": r.resolver,
            }
            for r in semantic
        ],
        "arbitrary_exec_coverage": ARBITRARY_EXEC_COVERAGE.value,
        "platform_supported": result.collected.platform_supported,
        "redacted_trace_path": str(result.redacted_trace_path)
        if result.redacted_trace_path
        else None,
        "observation": observation,
        "task_arn": result.task_arn,
    }

    if as_json:
        typer.echo(json.dumps(payload, sort_keys=True, indent=2))
    else:
        typer.echo(f"coverage   {trace.coverage_state.value}")
        if trace.incomplete_reasons:
            typer.echo("reasons    " + "; ".join(trace.incomplete_reasons))
        typer.echo(f"resources  {len(trace.resources)} ({len(semantic)} semantic)")
        typer.echo(f"trace      {t_digest}")
        typer.echo(f"child_exit {result.child_exit_code}")
        typer.echo(
            f"note       arbitrary exec frozen at {ARBITRARY_EXEC_COVERAGE.value} "
            "(observation alone never authorizes reuse)"
        )
        if observation:
            typer.echo(
                f"observation {observation['observation_id']} ({observation['lifecycle_state']})"
            )

    emit_event("scout.completed", payload)
    if not keep_raw:
        cleanup_scout_tmpdir(result)
    raise ChildExit(result.child_exit_code)


@app.command("plan")
@_graceful
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
    emit_event("plan.started", {"config_path": str(config_path), "source_root": str(source_root)})
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
    emit_event(
        "plan.completed",
        {
            "stages": [
                {
                    "stage": s.stage,
                    "work_key": s.work_key.value,
                    "structurally_sound": s.structurally_sound,
                }
                for s in result.stages
            ]
        },
    )
    if output == "json":
        typer.echo(json.dumps(_as_json(result), sort_keys=True, indent=2))
    else:
        _print_table(result)


@app.command("claim-demo")
@_graceful
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
@_graceful
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
@_graceful
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


@app.command("init")
def init_command(
    config_path: Annotated[
        Path, typer.Option("--config", help="Where to write the scaffolded config.")
    ] = Path("cairn.yaml"),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config file.")
    ] = False,
) -> None:
    """Scaffold a cairn.yaml stage registry (PROJECT.md §7.1)."""

    if config_path.exists() and not force:
        typer.echo(f"{config_path} already exists — pass --force to overwrite", err=True)
        raise typer.Exit(code=1)
    config_path.write_text(_INIT_TEMPLATE, encoding="utf-8")
    try:
        TrackedConfig.load(config_path)
    except ConfigError as exc:  # pragma: no cover - _INIT_TEMPLATE is fixed and valid
        typer.echo(f"init wrote an invalid config: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"wrote {config_path}")


@app.command("explain")
@_graceful
def explain_command(
    artifact_id: Annotated[str, typer.Argument(help="Artifact to explain.")],
    output: Annotated[str, typer.Option("--output", help="table or json")] = "table",
) -> None:
    """Full provenance and decision chain for one artifact — PROJECT.md §7.1:
    the artifact row, its input edges, direct downstream artifacts, every
    decision that cites it, and its contradiction history. Exits 1 if the
    artifact_id is unknown."""

    if output not in {"table", "json"}:
        raise typer.BadParameter("must be 'table' or 'json'", param_hint="--output")

    pool = get_pool()
    try:
        details = describe_artifact(pool, artifact_id)
        if details is None:
            emit_event("explain.not_found", {"artifact_id": artifact_id})
            typer.echo(f"unknown artifact_id={artifact_id!r}", err=True)
            raise typer.Exit(code=1)
        downstream = downstream_artifacts(pool, artifact_id)
        decisions = _decisions_for_artifact(pool, artifact_id)
        contradictions = _contradictions_for_artifact(pool, artifact_id)
    finally:
        close_pool()

    emit_event(
        "explain.completed",
        _explain_as_json(details, downstream, decisions, contradictions),
    )
    if output == "json":
        typer.echo(
            json.dumps(
                _explain_as_json(details, downstream, decisions, contradictions),
                sort_keys=True,
                indent=2,
            )
        )
    else:
        _print_explain(details, downstream, decisions, contradictions)


@memory_app.command("search")
@_graceful
def memory_search_command(
    text: Annotated[str, typer.Argument(help="Free text describing a failure to search for.")],
    stage: Annotated[
        str | None, typer.Option("--stage", help="Restrict to one pipeline stage.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 8,
) -> None:
    """Search negative computational memory directly — PROJECT.md §7.1.
    Prints plain cosine distances only, never a tier verdict: tiering
    (db/memory.py::tier) needs a proposed plan's structured features to
    know which fields are causal, which a bare text query doesn't have."""

    provider = default_provider()
    typer.echo(f"provider={type(provider).__name__}")
    embedding = provider.embed(text)
    pool = get_pool()
    try:
        matches = search_text(pool, embedding=embedding, stage=stage, limit=limit)
    finally:
        close_pool()

    emit_event(
        "memory.search_completed",
        {
            "query": text,
            "stage": stage,
            "limit": limit,
            "provider": type(provider).__name__,
            "matches": [
                {
                    "signature_id": str(m.signature_id),
                    "stage": m.stage,
                    "error_class": m.error_class,
                    "summary_text": m.summary_text,
                    "traceback_head": m.traceback_head,
                    "cosine_distance": m.cosine_distance,
                    "wasted_ms": m.wasted_ms,
                    "created_at": m.created_at.isoformat(),
                    "has_verified_remediation": m.causal_keys is not None,
                }
                for m in matches
            ],
        },
    )
    if not matches:
        typer.echo("no matches")
        return
    for match in matches:
        typer.echo(
            f"{match.stage:<11} {match.error_class:<24} dist={match.cosine_distance:.4f} "
            f"{match.created_at.isoformat()}"
        )
        typer.echo(f"  {match.summary_text}")


@memory_app.command("why-blocked")
@_graceful
def memory_why_blocked_command(
    output: Annotated[str, typer.Option("--output", help="table or json")] = "table",
) -> None:
    """Explain the most recent refusal in the decision ledger — PROJECT.md §7.1."""

    if output not in {"table", "json"}:
        raise typer.BadParameter("must be 'table' or 'json'", param_hint="--output")

    pool = get_pool()
    try:
        refusal = _latest_refusal(pool)
    finally:
        close_pool()

    if refusal is None:
        emit_event("memory.why_blocked_completed", {"refusal": None})
        typer.echo("no refusal on record", err=True)
        raise typer.Exit(code=1)

    emit_event("memory.why_blocked_completed", {"refusal": _decision_as_json(refusal)})
    if output == "json":
        typer.echo(json.dumps(_decision_as_json(refusal), sort_keys=True, indent=2))
        return
    typer.echo(f"work_key    {refusal.work_key}")
    typer.echo(f"stage       {refusal.stage}")
    typer.echo(f"action      {refusal.action}")
    typer.echo(f"proposed_by {refusal.proposed_by}")
    typer.echo(f"created_at  {refusal.created_at.isoformat()}")
    typer.echo(f"explanation {refusal.explanation}")


@app.command("doctor")
def doctor_command(
    cloud: Annotated[bool, typer.Option("--cloud", help="Include ccloud topology check.")] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a machine-readable JSON report on stdout.")
    ] = False,
) -> None:
    """Diagnose the environment — PROJECT.md §7.1: database, schema, vector
    index, ccloud cluster identity, and AWS credentials. Only the database
    and schema checks gate the exit code; ccloud/AWS/vector-index are
    reported honestly but never block a green `doctor` on a machine that
    simply doesn't have them configured.

    ``--cloud --json`` is the Day-1 eligibility gate: documented
    ``ccloud cluster info`` output is parsed into a normalized topology
    record (or an explicit fail-closed reason). Never invent a ``--json``
    flag on ccloud itself.
    """
    _ = cloud  # flag retained so the Appendix E gate command is a stable CLI

    try:
        pool = get_pool()

        def _version_tx(cur: psycopg.Cursor) -> str:
            cur.execute("SELECT version()")
            row = cur.fetchone()
            return str(row[0]) if row else "unknown"

        version = in_txn(pool, _version_tx, op="doctor.database")
        db_ok, db_detail = True, version
    except Exception as exc:  # noqa: BLE001 - doctor must report, never crash
        db_ok, db_detail = False, f"unreachable: {exc}"

    schema_ok, schema_detail = _doctor_schema()

    try:
        status = vector_index_status(get_pool())
        vector_index_detail = status.detail
        vector_ok = status.active
    except Exception as exc:  # noqa: BLE001
        vector_index_detail = f"unavailable: {exc}"
        vector_ok = False

    ccloud_report = _doctor_ccloud_report()
    aws_ok, aws_detail = _doctor_aws()
    gating_ok = db_ok and schema_ok

    report: dict[str, object] = {
        "database_ok": db_ok,
        "database_detail": db_detail,
        "schema_ok": schema_ok,
        "schema_detail": schema_detail,
        "vector_index_ok": vector_ok,
        "vector_index_detail": vector_index_detail,
        "ccloud": ccloud_report,
        "aws_ok": aws_ok,
        "aws_detail": aws_detail,
        "gating_ok": gating_ok,
    }

    if as_json:
        typer.echo(json.dumps(report, sort_keys=True))
    else:
        typer.echo(f"{'PASS' if db_ok else 'FAIL'}  database     {db_detail}")
        typer.echo(f"{'PASS' if schema_ok else 'FAIL'}  schema       {schema_detail}")
        typer.echo(f"{'PASS' if vector_ok else 'INFO'}  vector-index {vector_index_detail}")
        ccloud_line = ccloud_report.get("detail") or ccloud_report.get("fail_closed_reason")
        typer.echo(f"INFO  ccloud       {ccloud_line}")
        typer.echo(f"{'PASS' if aws_ok else 'INFO'}  aws          {aws_detail}")
        if gating_ok:
            typer.echo("Everything load-bearing looks healthy.")

    close_pool()
    emit_event("doctor.completed", report)
    if not gating_ok:
        raise typer.Exit(code=1)


def _doctor_schema() -> tuple[bool, str]:
    migrations_dir = Path(__file__).resolve().parents[2] / "db" / "migrations"
    if not migrations_dir.is_dir():
        return False, f"migrations directory not found at {migrations_dir}"
    files = sorted(
        p.name
        for p in migrations_dir.iterdir()
        if p.is_file() and p.suffix in {".sql", ".py"} and not p.name.startswith("_")
    )
    if not files:
        return False, f"no migration files found in {migrations_dir}"
    try:
        pool = get_pool()

        def _applied_tx(cur: psycopg.Cursor) -> set[str]:
            cur.execute("SELECT version FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}

        applied = in_txn(pool, _applied_tx, op="doctor.schema")
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read schema_migrations: {exc}"
    missing = [f for f in files if f not in applied]
    if missing:
        return False, f"{len(missing)} migration(s) not applied: {', '.join(missing)}"
    return True, f"{len(files)} migrations applied"


def _doctor_ccloud_report() -> dict[str, object]:
    """Parse documented ``ccloud cluster info`` into a topology record.

    Fail closed on missing binary, missing cluster name, nonzero exit, or
    unrecognised output. Never call undocumented ``--json`` on ccloud.
    """
    from cairn.ccloud_parse import CcloudParseError, parse_cluster_info, topology_to_jsonable

    ccloud_path = shutil.which("ccloud")
    if ccloud_path is None:
        # Persist install location used by Day-1 Windows setup.
        candidate = Path(os.environ.get("APPDATA", "")) / "ccloud" / "ccloud.exe"
        if candidate.is_file():
            os.environ["PATH"] = f"{candidate.parent}{os.pathsep}{os.environ.get('PATH', '')}"
            ccloud_path = str(candidate)
    if ccloud_path is None:
        return {
            "ok": False,
            "fail_closed_reason": "ccloud not found on PATH (not installed / not configured)",
            "detail": "ccloud not found on PATH (not installed / not configured)",
        }
    cluster_name = os.environ.get("CAIRN_CLUSTER_NAME", "").strip()
    if not cluster_name:
        dotenv_path = find_dotenv(usecwd=True)
        if dotenv_path:
            load_dotenv(dotenv_path, override=False)
        cluster_name = os.environ.get("CAIRN_CLUSTER_NAME", "").strip()
    if not cluster_name:
        return {
            "ok": False,
            "fail_closed_reason": "CAIRN_CLUSTER_NAME is unset; cannot run ccloud cluster info",
            "detail": "CAIRN_CLUSTER_NAME is unset; cannot run ccloud cluster info",
        }
    try:
        completed = subprocess.run(
            ["ccloud", "cluster", "info", cluster_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "fail_closed_reason": "ccloud cluster info timed out after 20s",
            "detail": "ccloud cluster info timed out after 20s",
        }
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()[:200]
        return {
            "ok": False,
            "fail_closed_reason": f"ccloud cluster info failed: {err}",
            "detail": f"ccloud cluster info failed: {err}",
        }
    try:
        topo = parse_cluster_info(completed.stdout)
    except CcloudParseError as exc:
        return {
            "ok": False,
            "fail_closed_reason": f"ccloud output unrecognised (fail closed): {exc}",
            "detail": f"ccloud output unrecognised (fail closed): {exc}",
        }
    payload = topology_to_jsonable(topo)
    payload["ok"] = True
    payload["detail"] = (
        f"reachable, cluster={topo.cluster_name}, regions={list(topo.cluster_regions)}"
    )
    return payload


def _doctor_ccloud() -> str:
    """Human-readable one-liner used by non-JSON doctor output."""
    report = _doctor_ccloud_report()
    detail = report.get("detail") or report.get("fail_closed_reason")
    return str(detail)


# A tiny standalone script, run via `sys.executable -c`, not imported into
# this process — isolates the real boto3 STS call in its own process so a
# native-DLL crash there (this machine's Avast/OpenSSL conflict when boto3
# is imported into a process that already imported torch, A.2) can never
# take `cairn doctor` down with it. ~20s timeout bounds the worst case.
_AWS_STS_PROBE = (
    "import json, sys\n"
    "try:\n"
    "    import boto3\n"
    "    identity = boto3.client('sts').get_caller_identity()\n"
    "    print(json.dumps({'ok': True, 'account': identity.get('Account')}))\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'ok': False, 'error': str(exc)}))\n"
)


def _doctor_aws() -> tuple[bool, str]:
    probe_error = "unknown error"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _AWS_STS_PROBE],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        probe_error = "AWS SDK STS check timed out after 20s"
    else:
        lines = [line for line in completed.stdout.strip().splitlines() if line]
        if lines:
            try:
                payload = json.loads(lines[-1])
            except json.JSONDecodeError:
                probe_error = f"AWS SDK STS check produced unparsable output: {lines[-1][:200]}"
            else:
                if payload.get("ok"):
                    return True, f"credentials valid, account={payload.get('account')} (SDK)"
                probe_error = (
                    f"AWS SDK unreachable: {str(payload.get('error', 'unknown error'))[:200]}"
                )
        else:
            probe_error = f"AWS SDK STS check produced no output: {completed.stderr.strip()[:200]}"

    # Some Windows Python environments have a native OpenSSL DLL conflict
    # (the SDK probe above exits before it can emit JSON) while the installed
    # AWS CLI remains healthy because it ships an isolated runtime. Doctor is
    # diagnostics, so use that independent real STS client as a fallback and
    # say which path succeeded rather than reporting valid credentials as
    # unavailable.
    aws_path = shutil.which("aws")
    if aws_path is None:
        return False, probe_error
    try:
        cli = subprocess.run(
            [aws_path, "sts", "get-caller-identity", "--output", "json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, f"{probe_error}; AWS CLI STS check timed out after 20s"
    if cli.returncode != 0:
        return False, f"{probe_error}; AWS CLI failed: {cli.stderr.strip()[:200]}"
    try:
        identity = json.loads(cli.stdout)
    except json.JSONDecodeError:
        return False, f"{probe_error}; AWS CLI returned invalid JSON"
    account = identity.get("Account")
    if not account:
        return False, f"{probe_error}; AWS CLI response omitted Account"
    return True, f"credentials valid, account={account} (AWS CLI fallback)"


def _print_explain(
    details: ArtifactDetails,
    downstream: list[str],
    decisions: list[DecisionRecord],
    contradictions: list[Contradiction],
) -> None:
    typer.echo(f"{details.stage} · {details.artifact_id}")
    typer.echo("")
    typer.echo(f"work_key       {details.work_key}")
    typer.echo(f"s3_uri         {details.s3_uri}")
    typer.echo(f"size_bytes     {details.size_bytes}")
    typer.echo(f"duration_ms    {details.duration_ms}")
    typer.echo(f"region         {details.region}")
    typer.echo(f"env            {details.env_fingerprint}")
    typer.echo(f"created_at     {details.created_at.isoformat()}")
    if details.quarantined_at is not None:
        typer.echo(f"quarantined_at {details.quarantined_at.isoformat()}")
    typer.echo("")
    typer.echo("inputs")
    for item in details.inputs:
        typer.echo(f"  {item.input_kind:<10} {item.input_ref}  digest={item.input_digest[:16]}")
    if not details.inputs:
        typer.echo("  (none)")
    typer.echo("")
    typer.echo("downstream")
    for artifact_id in downstream:
        typer.echo(f"  {artifact_id}")
    if not downstream:
        typer.echo("  (none)")
    typer.echo("")
    typer.echo("decisions")
    for decision in decisions:
        typer.echo(
            f"  {decision.created_at.isoformat()}  {decision.action:<22} "
            f"verdict={decision.verdict:<10} authorized_by={decision.authorized_by or '-'} "
            f"class={decision.change_class or '-'}"
        )
    if not decisions:
        typer.echo("  (none)")
    typer.echo("")
    typer.echo("contradictions")
    for contradiction in contradictions:
        typer.echo(
            f"  {contradiction.created_at.isoformat()}  "
            f"quarantined={contradiction.quarantined}  {contradiction.evidence}"
        )
    if not contradictions:
        typer.echo("  (none)")


def _explain_as_json(
    details: ArtifactDetails,
    downstream: list[str],
    decisions: list[DecisionRecord],
    contradictions: list[Contradiction],
) -> dict[str, object]:
    return {
        "artifact_id": details.artifact_id,
        "stage": details.stage,
        "work_key": details.work_key,
        "s3_uri": details.s3_uri,
        "size_bytes": details.size_bytes,
        "duration_ms": details.duration_ms,
        "region": details.region,
        "env_fingerprint": details.env_fingerprint,
        "created_at": details.created_at.isoformat(),
        "quarantined_at": details.quarantined_at.isoformat() if details.quarantined_at else None,
        "inputs": [
            {"input_kind": i.input_kind, "input_ref": i.input_ref, "input_digest": i.input_digest}
            for i in details.inputs
        ],
        "downstream": downstream,
        "decisions": [_decision_as_json(d) for d in decisions],
        "contradictions": [
            {
                "contradiction_id": str(c.contradiction_id),
                "contradicting_run": str(c.contradicting_run),
                "evidence": c.evidence,
                "quarantined": c.quarantined,
                "created_at": c.created_at.isoformat(),
            }
            for c in contradictions
        ],
    }


def _decision_as_json(decision: DecisionRecord) -> dict[str, object]:
    return {
        "decision_id": str(decision.decision_id),
        "work_key": decision.work_key,
        "stage": decision.stage,
        "action": decision.action,
        "verdict": decision.verdict,
        "change_class": decision.change_class,
        "proposed_by": decision.proposed_by,
        "authorized_by": decision.authorized_by,
        "candidate_artifact_id": decision.candidate_artifact_id,
        "latency_ms": decision.latency_ms,
        "explanation": decision.explanation,
        "created_at": decision.created_at.isoformat(),
    }


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


if __name__ == "__main__":
    # Makes `python -m cairn.cli <args>` behave identically to the
    # installed `cairn` console script (pyproject.toml's
    # `cairn = "cairn.cli:app"`) — without this, `-m` imports the module,
    # defines `app`, and exits without ever invoking it: a real, confirmed
    # bug (every subprocess the TUI spawns via `<python> -m cairn.cli
    # <args>` — tui/src/connection/subprocess.ts — silently did nothing).
    app()

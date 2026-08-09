"""Orchestrate collect → companion → normalize for ``cairn scout``."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cairn.flight.types import (
    CoverageState,
    ExecutionSpec,
    OutputContract,
    PlatformContract,
    PurityPolicy,
    TraceContent,
)
from cairn.trace.collector import TRACER_VERSION, CollectorResult, collect
from cairn.trace.companion import (
    COMPANION_VERSION,
    companion_env,
    load_companion_resources,
)
from cairn.trace.normalize import NormalizeConfig, normalize_trace, semantic_resource_set

# Gate A: observation alone does not authorize generic verified reuse.
# Day-3 ``cairn exec`` must honour this until a named contract qualifies.
ARBITRARY_EXEC_COVERAGE = CoverageState.SHADOW_UNQUALIFIED
ADAPTER_REGISTRY_VERSION = "adapters/v1"


@dataclass(frozen=True, slots=True)
class ScoutResult:
    trace: TraceContent
    child_exit_code: int
    collected: CollectorResult
    spec: ExecutionSpec
    redacted_trace_path: Path | None
    task_arn: str | None = None


def build_shadow_spec(
    *,
    argv: tuple[str, ...],
    cwd_rel: str,
    output_rel: str,
    namespace_id: str = "local",
    image_digest: str | None = None,
) -> ExecutionSpec:
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    env_arch = os.environ.get("CAIRN_ARCH")
    if env_arch:
        arch = env_arch
    elif hasattr(os, "uname"):
        machine = os.uname().machine
        arch = "arm64" if machine in {"aarch64", "arm64"} else "amd64"
    else:
        arch = "amd64"
    return ExecutionSpec(
        namespace_id=namespace_id,
        argv=argv,
        cwd_rel=cwd_rel,
        output=OutputContract(path_rel=output_rel),
        platform=PlatformContract(os=os_name, arch=arch, image_digest=image_digest),
        purity=PurityPolicy(
            contract_id="shadow",
            network="observe",
            clock="observe",
            random="observe",
            env_names=(),
            qualification_runs=1,
        ),
        adapter=None,
        tracer_version=TRACER_VERSION,
        companion_version=COMPANION_VERSION,
        adapter_registry_version=ADAPTER_REGISTRY_VERSION,
    )


def run_scout(
    argv: list[str] | tuple[str, ...],
    *,
    output_file: Path,
    workspace: Path | None = None,
    namespace_id: str = "local",
    image_digest: str | None = None,
    keep_raw_trace: bool = False,
    task_arn: str | None = None,
) -> ScoutResult:
    """Trace ``argv`` and return coverage + resources. Never publishes reuse."""
    command = tuple(str(a) for a in argv)
    if not command:
        raise ValueError("COMMAND is required")
    root = (workspace or Path.cwd()).resolve()
    try:
        output_rel = output_file.resolve().relative_to(root).as_posix()
    except ValueError:
        # Output outside workspace — still record as relative-looking POSIX
        # path under a synthetic prefix so ExecutionSpec invariants hold.
        output_rel = f".cairn/out/{output_file.name}"

    cwd_rel = "."
    image_digest = image_digest or os.environ.get("CAIRN_IMAGE_DIGEST") or None
    if image_digest is not None:
        # Accept bare hex, sha256:…, or repo@sha256:… (docker RepoDigests).
        if "@sha256:" in image_digest:
            image_digest = "sha256:" + image_digest.rsplit("@sha256:", 1)[1]
        elif not image_digest.startswith("sha256:") and len(image_digest) == 64:
            image_digest = f"sha256:{image_digest}"

    spec = build_shadow_spec(
        argv=command,
        cwd_rel=cwd_rel,
        output_rel=output_rel,
        namespace_id=namespace_id,
        image_digest=image_digest,
    )

    trace_dir = Path(tempfile.mkdtemp(prefix="cairn-scout-"))
    private_tmp = trace_dir / "tmp"
    private_tmp.mkdir()

    # Companion + strace live under trace_dir so their opens are not
    # undeclared workspace writes (Appendix C.3 INCOMPLETE_WRITE).
    with companion_env(root, base_dir=private_tmp) as session:
        env = dict(os.environ)
        env.update(session.env_updates)
        # Bytecode writes under the workspace would otherwise look like
        # undeclared outputs (INCOMPLETE_WRITE) and poison coverage.
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        collected = collect(
            list(command),
            cwd=root,
            env=env,
            trace_dir=trace_dir,
        )
        companion_rows = load_companion_resources(session.log_path, workspace=root)

    linux = sys.platform.startswith("linux")
    trace = normalize_trace(
        collected,
        config=NormalizeConfig(
            workspace=root,
            output_rel=output_rel,
            private_temp_root=trace_dir,
            purity_contract_id=spec.purity.contract_id,
            image_pinned=spec.platform.image_digest is not None,
            linux=linux,
        ),
        companion_resources=companion_rows,
    )

    redacted_path = _write_redacted(trace_dir, trace, collected)
    if not keep_raw_trace and collected.trace_path is not None:
        with contextlib.suppress(OSError):
            collected.trace_path.unlink(missing_ok=True)

    return ScoutResult(
        trace=trace,
        child_exit_code=collected.child_exit_code,
        collected=collected,
        spec=spec,
        redacted_trace_path=redacted_path,
        task_arn=task_arn,
    )


def _write_redacted(
    trace_dir: Path,
    trace: TraceContent,
    collected: CollectorResult,
) -> Path:
    path = trace_dir / "trace.redacted.json"
    payload = {
        "coverage_state": trace.coverage_state.value,
        "incomplete_reasons": list(trace.incomplete_reasons),
        "resources": [
            {
                "kind": r.kind.value,
                "ref": r.ref,
                "access_mode": r.access_mode.value,
                "exists": r.exists,
                "version_digest": r.version_digest,
                "resolver": r.resolver,
                "source": r.source.value,
            }
            for r in trace.resources
        ],
        "semantic_resources": [
            {
                "kind": r.kind.value,
                "ref": r.ref,
                "access_mode": r.access_mode.value,
                "exists": r.exists,
                "version_digest": r.version_digest,
                "resolver": r.resolver,
            }
            for r in semantic_resource_set(trace)
        ],
        "child_exit_code": collected.child_exit_code,
        "platform_supported": collected.platform_supported,
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def cleanup_scout_tmpdir(result: ScoutResult) -> None:
    """Remove the private tmpdir that held the raw strace log."""
    if result.redacted_trace_path is None:
        return
    parent = result.redacted_trace_path.parent
    if parent.name.startswith("cairn-scout-"):
        shutil.rmtree(parent, ignore_errors=True)

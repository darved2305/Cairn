"""Python sitecustomize audit-hook enrichment (Appendix C.4 §6).

May only ADD resource rows and refine ``ref`` labels. Never upgrades
``coverage_state`` — audit hooks are not a sandbox boundary.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cairn.fingerprint.canon import canonical_json
from cairn.flight.types import (
    AccessMode,
    ObservationSource,
    ResourceIdentity,
    ResourceKind,
)

COMPANION_VERSION = "python-audit/v1"

_SITECUSTOMIZE = r'''
"""Cairn Flight Recorder companion — injected via PYTHONPATH sitecustomize."""
from __future__ import annotations

import json
import os
import sys

_LOG = os.environ.get("CAIRN_COMPANION_LOG")
_WORKSPACE = os.environ.get("CAIRN_COMPANION_WORKSPACE", "")

def _under_workspace(path: str) -> bool:
    if not _WORKSPACE:
        return False
    try:
        full = os.path.abspath(path)
        root = os.path.abspath(_WORKSPACE)
        return full == root or full.startswith(root + os.sep)
    except (OSError, TypeError, ValueError):
        return False

def _emit(event: dict[str, object]) -> None:
    if not _LOG:
        return
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        return

def _hook(event: str, args: tuple[object, ...]) -> None:
    try:
        if event == "import":
            # Imports are enrichment labels only — never hash module files here.
            # Skip stdlib/site floods; project modules show up via strace paths.
            return
        elif event == "open":
            path, mode = (args + (None, None))[:2]
            if isinstance(path, (str, bytes)):
                text = path.decode() if isinstance(path, bytes) else path
                # Kernel strace already covers absolute/stdlib opens. The
                # companion only adds workspace-relative rows so startup
                # open storms cannot hash the world.
                if _under_workspace(text):
                    _emit({"kind": "open", "path": text, "mode": str(mode)})
        elif event == "os.listdir":
            path = args[0] if args else None
            if isinstance(path, (str, bytes, os.PathLike)):
                text = os.fspath(path)
                if _under_workspace(text):
                    _emit({"kind": "listdir", "path": text})
        elif event == "os.scandir":
            path = args[0] if args else "."
            if isinstance(path, (str, bytes, os.PathLike)) or path is None:
                text = os.fspath(path or ".")
                if _under_workspace(text):
                    _emit({"kind": "scandir", "path": text})
        elif event in {"os.mkdir", "os.makedirs", "os.remove", "os.unlink", "os.rename"}:
            paths = [os.fspath(a) for a in args[:2] if isinstance(a, (str, bytes, os.PathLike))]
            if any(_under_workspace(p) for p in paths):
                _emit({"kind": "fs", "event": event, "args": paths})
        elif event == "subprocess.Popen":
            _emit({"kind": "subprocess", "args": [str(a) for a in (args[0] if args else [])][:8]})
        elif event == "os.getenv" or event == "os.environ.__getitem__":
            name = args[0] if args else None
            if isinstance(name, str) and name.startswith("CAIRN_"):
                # Never persist the value — name only (C.4 §5). Bound to
                # CAIRN_* so ambient getenv storms stay out of the log.
                _emit({"kind": "env", "name": name})
    except Exception:
        # Companion must never break the traced program.
        return

try:
    sys.addaudithook(_hook)
    _emit({"kind": "companion_start", "version": "python-audit/v1", "workspace": _WORKSPACE})
except Exception:
    pass
'''


@dataclass(frozen=True, slots=True)
class CompanionSession:
    log_path: Path
    site_dir: Path
    env_updates: dict[str, str]


@contextmanager
def companion_env(workspace: Path) -> Iterator[CompanionSession]:
    """Install a temporary sitecustomize on PYTHONPATH and yield session paths."""
    site_dir = Path(tempfile.mkdtemp(prefix="cairn-companion-"))
    log_path = site_dir / "companion.jsonl"
    log_path.write_text("", encoding="utf-8")
    (site_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE.lstrip("\n"), encoding="utf-8")
    updates = {
        "CAIRN_COMPANION_LOG": str(log_path),
        "CAIRN_COMPANION_WORKSPACE": str(workspace.resolve()),
        "PYTHONPATH": (
            str(site_dir)
            + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
        ),
    }
    try:
        yield CompanionSession(log_path=log_path, site_dir=site_dir, env_updates=updates)
    finally:
        # Best-effort cleanup; scout may still read the log before this runs
        # when used as a context around collect only.
        pass


def load_companion_resources(
    log_path: Path,
    *,
    workspace: Path,
) -> tuple[ResourceIdentity, ...]:
    """Parse companion JSONL into additive ResourceIdentity rows."""
    if not log_path.is_file():
        return ()
    workspace_res = workspace.resolve()
    rows: dict[tuple[str, str, str], ResourceIdentity] = {}

    def rel_of(path_str: str) -> str:
        raw = Path(path_str)
        try:
            resolved = raw if raw.is_absolute() else (workspace_res / raw)
            resolved = resolved.resolve(strict=False)
            try:
                return resolved.relative_to(workspace_res).as_posix()
            except ValueError:
                return resolved.as_posix()
        except OSError:
            return path_str.replace("\\", "/")

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("kind")
        if kind == "import":
            module = str(event.get("module", ""))
            if not module:
                continue
            # Refine label only — module name as ref under env-like namespace.
            digest = sha256(
                canonical_json({"label": "python_module/v1", "module": module})
            ).hexdigest()
            resource = ResourceIdentity(
                kind=ResourceKind.FILE,
                ref=f"py-module:{module}",
                access_mode=AccessMode.READ,
                exists=True,
                version_digest=digest,
                resolver="python_module/v1",
                source=ObservationSource.PYTHON,
            )
            rows[resource.identity_tuple] = resource
        elif kind in {"open", "listdir", "scandir"}:
            path = event.get("path")
            if not isinstance(path, str) or not path:
                continue
            ref = rel_of(path)
            # Defense in depth: never hash outside the workspace from companion.
            if ref.startswith("/") or (len(ref) >= 2 and ref[1] == ":"):
                continue
            mode_str = str(event.get("mode", "r"))
            writing = any(c in mode_str for c in "wax+")
            try:
                p = Path(path if os.path.isabs(path) else workspace_res / path)
                if not str(p.resolve()).startswith(str(workspace_res)):
                    continue
                if writing:
                    access = AccessMode.WRITE
                    if p.exists() and p.is_file():
                        digest = sha256(p.read_bytes()).hexdigest()
                    else:
                        digest = sha256(
                            canonical_json({"label": "python_write", "ref": ref})
                        ).hexdigest()
                    exists = True
                elif kind in {"listdir", "scandir"}:
                    access = AccessMode.ENUMERATE
                    if not p.exists():
                        continue
                    entries = sorted(
                        (e.name, "dir" if e.is_dir(follow_symlinks=False) else "reg")
                        for e in os.scandir(p)
                    )
                    digest = sha256(
                        canonical_json({"label": "dir_entries/v1", "body": entries})
                    ).hexdigest()
                    resource = ResourceIdentity(
                        kind=ResourceKind.DIRECTORY,
                        ref=ref,
                        access_mode=access,
                        exists=True,
                        version_digest=digest,
                        resolver="dir_entries/v1",
                        source=ObservationSource.PYTHON,
                    )
                    rows[resource.identity_tuple] = resource
                    continue
                else:
                    access = AccessMode.READ
                    if not p.exists():
                        resource = ResourceIdentity(
                            kind=ResourceKind.FILE,
                            ref=ref,
                            access_mode=AccessMode.NEGATIVE,
                            exists=False,
                            version_digest="absent",
                            resolver="workspace_file/v1",
                            source=ObservationSource.PYTHON,
                        )
                        rows[resource.identity_tuple] = resource
                        continue
                    digest = sha256(p.read_bytes()).hexdigest()
                    exists = True
            except OSError:
                continue
            resource = ResourceIdentity(
                kind=ResourceKind.FILE,
                ref=ref,
                access_mode=access,
                exists=exists,
                version_digest=digest,
                resolver="workspace_file/v1",
                source=ObservationSource.PYTHON,
            )
            rows[resource.identity_tuple] = resource
        elif kind == "env":
            name = event.get("name")
            if not isinstance(name, str) or not name:
                continue
            # Name only — HMAC of value is Day-3+ when identity requires it.
            digest = sha256(canonical_json({"label": "env_name/v1", "name": name})).hexdigest()
            resource = ResourceIdentity(
                kind=ResourceKind.ENV,
                ref=name,
                access_mode=AccessMode.READ,
                exists=True,
                version_digest=digest,
                resolver="env_name/v1",
                source=ObservationSource.PYTHON,
            )
            rows[resource.identity_tuple] = resource
        elif kind == "subprocess":
            argv = event.get("args") or []
            ref = " ".join(str(a) for a in argv)[:200] or "subprocess"
            digest = sha256(
                canonical_json({"label": "subprocess/v1", "argv": list(argv)})
            ).hexdigest()
            resource = ResourceIdentity(
                kind=ResourceKind.SUBPROCESS,
                ref=ref,
                access_mode=AccessMode.EXECUTE,
                exists=True,
                version_digest=digest,
                resolver="subprocess/v1",
                source=ObservationSource.PYTHON,
            )
            rows[resource.identity_tuple] = resource

    return tuple(sorted(rows.values(), key=lambda r: r.identity_tuple))


def merge_companion(
    base: tuple[ResourceIdentity, ...],
    extra: tuple[ResourceIdentity, ...],
) -> tuple[ResourceIdentity, ...]:
    """Additive merge: companion rows fill gaps; may refine refs, never drop."""
    merged: dict[tuple[str, str, str], ResourceIdentity] = {r.identity_tuple: r for r in base}
    for resource in extra:
        key = resource.identity_tuple
        if key not in merged:
            merged[key] = resource
    return tuple(sorted(merged.values(), key=lambda r: r.identity_tuple))

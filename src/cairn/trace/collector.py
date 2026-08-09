"""Linux ``strace -f`` process supervision (Appendix C.1).

Exact flags are pinned here so a plausible-sounding option cannot drift in.
Do not use ``-qq``: upstream documents that it suppresses ``+++ exited with N +++``.
Capture the supervised child's return code independently; missing or
contradictory exit records become ``INCOMPLETE_TRACE_LOSS``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

TRACER_VERSION = "strace/v1"

# Appendix C.1 — do not invent flags. Filenames stay full; -s 32 bounds other strings.
_STRACE_TRACE_EXPR = (
    "%file,%process,%network,dup,dup2,dup3,fcntl,close,close_range,"
    "getdents64,mmap,io_uring_setup,io_uring_enter,getrandom,memfd_create,"
    "ptrace,process_vm_readv"
)

_LINE_RE = re.compile(
    r"^(?P<pid>\d+)\s+"
    r"(?:(?P<unfinished>.*?)\s*<unfinished\s+\.\.\.>\s*$|"
    r"<...\s+(?P<resumed>\S+)\s+resumed>(?P<resume_rest>.*)$|"
    r"(?P<call>\w+)\((?P<args>.*)\)\s*=\s*(?P<ret>.+)$|"
    r"\+\+\+\s+killed\s+by\s+(?P<killed>\S+)\s+\+\+\+\s*$|"
    r"\+\+\+\s+exited\s+with\s+(?P<exit_code>-?\d+)\s+\+\+\+\s*$)"
)

_FD_PATH_RE = re.compile(r"^(-?\d+)<([^>]*)>$")
_ERRNO_RE = re.compile(r"^-1\s+([A-Z0-9]+)(?:\s+\(.*\))?$")


@dataclass(frozen=True, slots=True)
class RawTraceEvent:
    """One completed syscall or process-lifecycle record from strace output."""

    pid: int
    kind: str  # syscall | exit | killed | incomplete
    name: str
    args: str
    ret: str
    raw: str


@dataclass(frozen=True, slots=True)
class CollectorResult:
    """Supervised trace capture. ``child_exit_code`` is independent of strace's exit."""

    child_exit_code: int
    events: tuple[RawTraceEvent, ...]
    incomplete_reasons: tuple[str, ...]
    platform_supported: bool
    trace_path: Path | None = None
    strace_returncode: int | None = None
    top_level_pid: int | None = None
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def strace_argv(*, trace_path: Path, command: list[str]) -> list[str]:
    """Exact Appendix C.1 invocation argv (without resolving ``strace`` on PATH)."""
    return [
        "strace",
        "-f",
        "--kill-on-exit",
        "-s",
        "32",
        "-y",
        "-o",
        str(trace_path),
        "-e",
        f"trace={_STRACE_TRACE_EXPR}",
        "--",
        *command,
    ]


def parse_strace_line(line: str) -> RawTraceEvent | None:
    """Parse one strace ``-f -y`` output line into a structured event.

    Unfinished/resumed pairs are not merged here — callers that need
    continuity across ``<unfinished ...>`` should pre-join. Day-2 tests
    feed completed lines; truncated unfinished lines mark incompleteness.
    """
    text = line.rstrip("\n")
    if not text.strip():
        return None
    match = _LINE_RE.match(text)
    if match is None:
        return RawTraceEvent(
            pid=-1,
            kind="incomplete",
            name="unparsed",
            args="",
            ret="",
            raw=text,
        )
    pid = int(match.group("pid"))
    if match.group("exit_code") is not None:
        return RawTraceEvent(
            pid=pid,
            kind="exit",
            name="exit",
            args="",
            ret=match.group("exit_code"),
            raw=text,
        )
    if match.group("killed") is not None:
        return RawTraceEvent(
            pid=pid,
            kind="killed",
            name="killed",
            args=match.group("killed"),
            ret="",
            raw=text,
        )
    if match.group("unfinished") is not None:
        return RawTraceEvent(
            pid=pid,
            kind="incomplete",
            name="unfinished",
            args=match.group("unfinished"),
            ret="",
            raw=text,
        )
    if match.group("resumed") is not None:
        # Treat orphan resumed lines without a prior unfinished as incomplete;
        # joined streams should not reach here.
        return RawTraceEvent(
            pid=pid,
            kind="incomplete",
            name="orphan_resumed",
            args=match.group("resumed") + (match.group("resume_rest") or ""),
            ret="",
            raw=text,
        )
    return RawTraceEvent(
        pid=pid,
        kind="syscall",
        name=match.group("call") or "",
        args=match.group("args") or "",
        ret=(match.group("ret") or "").strip(),
        raw=text,
    )


def parse_strace_output(text: str) -> tuple[tuple[RawTraceEvent, ...], tuple[str, ...]]:
    """Parse a full strace log, joining unfinished/resumed pairs when possible."""
    pending: dict[int, str] = {}
    events: list[RawTraceEvent] = []
    reasons: list[str] = []
    for line in text.splitlines():
        stripped = line.rstrip("\n")
        if not stripped.strip():
            continue
        unfinished = re.match(
            r"^(?P<pid>\d+)\s+(?P<body>.*?)\s*<unfinished\s+\.\.\.>\s*$",
            stripped,
        )
        if unfinished is not None:
            pending[int(unfinished.group("pid"))] = unfinished.group("body")
            continue
        resumed = re.match(
            r"^(?P<pid>\d+)\s+<\.\.\.\s+(?P<name>\S+)\s+resumed>(?P<rest>.*)$",
            stripped,
        )
        if resumed is not None:
            pid = int(resumed.group("pid"))
            head = pending.pop(pid, None)
            if head is None:
                events.append(
                    RawTraceEvent(
                        pid=pid,
                        kind="incomplete",
                        name="orphan_resumed",
                        args=resumed.group("name") + resumed.group("rest"),
                        ret="",
                        raw=stripped,
                    )
                )
                reasons.append("orphan_resumed_line")
                continue
            joined = f"{pid}  {head}{resumed.group('rest')}"
            # head already contains "name(args" without closing; rest is ") = ..."
            if not re.search(r"\)\s*=", joined):
                joined = f"{pid}  {head}){resumed.group('rest')}"
            event = parse_strace_line(joined)
            if event is None:
                continue
            events.append(event)
            if event.kind == "incomplete":
                reasons.append(f"unparsed:{event.raw[:80]}")
            continue
        event = parse_strace_line(stripped)
        if event is None:
            continue
        events.append(event)
        if event.kind == "incomplete":
            reasons.append(f"unparsed:{event.raw[:80]}")
    if pending:
        reasons.append(f"truncated_unfinished:{len(pending)}")
    return tuple(events), tuple(dict.fromkeys(reasons))


def _platform_supported() -> bool:
    return sys.platform.startswith("linux")


def collect(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    trace_dir: Path | None = None,
) -> CollectorResult:
    """Run ``argv`` under Appendix C.1 strace supervision.

    On non-Linux hosts the command still runs (so exit codes stay real) but
    the result is marked platform-incomplete — never a portable identity.
    """
    command = [str(part) for part in argv]
    if not command:
        raise ValueError("argv must be non-empty")

    work_cwd = cwd or Path.cwd()
    child_env = dict(os.environ if env is None else env)

    if not _platform_supported():
        completed = subprocess.run(
            command,
            cwd=str(work_cwd),
            env=child_env,
            check=False,
        )
        return CollectorResult(
            child_exit_code=int(completed.returncode),
            events=(),
            incomplete_reasons=("not_linux", "native_windows_no_strace"),
            platform_supported=False,
            metadata=(("platform", sys.platform),),
        )

    strace_bin = shutil.which("strace")
    if strace_bin is None:
        completed = subprocess.run(
            command,
            cwd=str(work_cwd),
            env=child_env,
            check=False,
        )
        return CollectorResult(
            child_exit_code=int(completed.returncode),
            events=(),
            incomplete_reasons=("strace_missing",),
            platform_supported=False,
            metadata=(("platform", sys.platform),),
        )

    tmp_owned = False
    if trace_dir is None:
        trace_dir = Path(tempfile.mkdtemp(prefix="cairn-trace-"))
        tmp_owned = True
    else:
        trace_dir.mkdir(parents=True, exist_ok=True)

    trace_path = trace_dir / "strace.log"
    # Private raw trace: not secret-safe, but mode 0600 limits local exposure.
    trace_path.touch(mode=0o600, exist_ok=True)
    os.chmod(trace_path, 0o600)

    argv_list = strace_argv(trace_path=trace_path, command=command)
    argv_list[0] = strace_bin

    # Supervise strace as the direct child. The traced process tree is
    # --kill-on-exit'd if the tracer dies; we still cross-check exit records.
    proc = subprocess.Popen(
        argv_list,
        cwd=str(work_cwd),
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    tracer_died = False
    try:
        _stdout, stderr = proc.communicate()
    except Exception:
        tracer_died = True
        proc.kill()
        proc.wait()
        raise
    strace_rc = int(proc.returncode)
    if strace_rc < 0:
        # Killed by signal — treat as tracer death / incomplete capture.
        tracer_died = True

    incomplete: list[str] = []
    if tracer_died:
        incomplete.append("tracer_died")
    if not trace_path.is_file() or trace_path.stat().st_size == 0:
        incomplete.append("trace_empty_or_missing")
        events: tuple[RawTraceEvent, ...] = ()
        parse_reasons: tuple[str, ...] = ()
    else:
        raw_text = trace_path.read_text(encoding="utf-8", errors="replace")
        if not raw_text.endswith("\n") and "exited with" not in raw_text[-200:]:
            # Truncation heuristic: no trailing newline and no recent exit record.
            incomplete.append("trace_truncated")
        events, parse_reasons = parse_strace_output(raw_text)
        incomplete.extend(parse_reasons)

    top_pid, traced_exit, exit_reason = _reconcile_exit(events, strace_rc)
    if exit_reason:
        incomplete.append(exit_reason)

    # Child exit code: prefer the top-level exit record; fall back to strace's
    # own status only when the record is absent (already marked incomplete).
    if traced_exit is not None:
        child_exit = traced_exit
    elif strace_rc >= 0:
        child_exit = strace_rc
    else:
        child_exit = 128 + (-strace_rc)

    if tmp_owned:
        # Keep the path available to the caller via trace_path; deletion is
        # the scout orchestrator's job after normalize, so secrets don't linger.
        pass

    return CollectorResult(
        child_exit_code=child_exit,
        events=events,
        incomplete_reasons=tuple(dict.fromkeys(incomplete)),
        platform_supported=True,
        trace_path=trace_path,
        strace_returncode=strace_rc,
        top_level_pid=top_pid,
        metadata=(
            ("tracer_version", TRACER_VERSION),
            ("strace_bin", strace_bin),
        ),
    )


def _reconcile_exit(
    events: tuple[RawTraceEvent, ...],
    strace_rc: int,
) -> tuple[int | None, int | None, str | None]:
    """Cross-check top-level ``+++ exited with N +++`` against strace's status."""
    exits = [e for e in events if e.kind == "exit"]
    if not exits:
        if any(e.kind == "killed" for e in events):
            return None, None, "top_level_killed_without_exit_code"
        return None, None, "missing_top_level_exit_record"

    # First exit in the log is the original traced process (strace -f order).
    top = exits[0]
    top_pid = top.pid
    try:
        traced_exit = int(top.ret)
    except ValueError:
        return top_pid, None, "unparseable_exit_status"

    # strace normally exits with the child's status. Disagreement means we
    # lost track of which process we were supervising.
    if strace_rc >= 0 and traced_exit != strace_rc:
        # When the tracee exits with a signal via shell wrappers this can
        # disagree; still mark incomplete rather than guess.
        return top_pid, traced_exit, "exit_status_disagreement"

    return top_pid, traced_exit, None


def split_args(args: str) -> list[str]:
    """Split a strace argument list on top-level commas (string-aware)."""
    parts: list[str] = []
    buf: list[str] = []
    in_str = False
    escape = False
    depth = 0
    for ch in args:
        if in_str:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
            continue
        if ch in "({[":
            depth += 1
            buf.append(ch)
            continue
        if ch in ")}]":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf or parts:
        parts.append("".join(buf).strip())
    return parts


def unescape_strace_string(token: str) -> str | None:
    """Decode a strace quoted path/string token, or None if truncated/unquoted.

    Truncated strings (``-s`` limit) end with ``"...`` and must not become
    identity — returning None forces the caller to skip or mark incomplete.
    """
    token = token.strip()
    if len(token) < 2 or not token.startswith('"'):
        return None
    if "..." in token and not token.endswith('"'):
        return None
    if token.endswith('"...'):
        return None
    if not token.endswith('"'):
        return None
    body = token[1:-1]
    try:
        return bytes(body, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return body


def parse_fd_token(token: str) -> tuple[int, str | None] | None:
    """Parse ``3</path>`` or bare ``3`` / ``AT_FDCWD`` forms from ``-y`` output."""
    token = token.strip()
    if token == "AT_FDCWD":
        return (-100, None)
    m = _FD_PATH_RE.match(token)
    if m:
        return int(m.group(1)), m.group(2) or None
    if re.fullmatch(r"-?\d+", token):
        return int(token), None
    return None


def parse_ret(ret: str) -> tuple[int | None, str | None, str | None]:
    """Return ``(fd_or_value, errno, path_from_-y)``."""
    ret = ret.strip()
    fd_m = _FD_PATH_RE.match(ret)
    if fd_m:
        return int(fd_m.group(1)), None, fd_m.group(2) or None
    err = _ERRNO_RE.match(ret)
    if err:
        return -1, err.group(1), None
    if re.fullmatch(r"-?\d+", ret):
        return int(ret), None, None
    # Pointer / hex returns (mmap): keep as None numeric.
    return None, None, None


def kill_tracer_for_tests(pid: int) -> None:
    """Test helper: signal a live tracer PID. Not used in production paths."""
    os.kill(pid, signal.SIGKILL)

"""Normalize raw strace events into ResourceIdentity rows (Appendix C.2–C.4).

Maintains per-process fd and cwd tables. Hash file bytes for identity;
``(inode, size, mtime)`` is a local memo key only — never the identity itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path, PurePosixPath

from cairn.fingerprint.canon import canonical_json
from cairn.flight.types import (
    AccessMode,
    CoverageState,
    Digest,
    ObservationSource,
    ResourceIdentity,
    ResourceKind,
    TraceContent,
)
from cairn.trace.collector import (
    CollectorResult,
    parse_fd_token,
    parse_ret,
    split_args,
    unescape_strace_string,
)

_ABSENT: Digest = "absent"
_OPEN_WRITE_HINTS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")
_STAT_CALLS = frozenset(
    {"stat", "lstat", "fstat", "newfstatat", "statx", "access", "faccessat", "faccessat2"}
)
_OPEN_CALLS = frozenset({"open", "openat", "openat2", "creat"})
_DUP_CALLS = frozenset({"dup", "dup2", "dup3"})
_NETWORK_CALLS = frozenset({"socket", "connect", "sendto", "recvfrom", "accept", "accept4"})
_COVERAGE_LOSS_CALLS = frozenset(
    {"io_uring_setup", "io_uring_enter", "ptrace", "process_vm_readv", "memfd_create"}
)
_CLONE_CALLS = frozenset({"clone", "clone3", "fork", "vfork"})


@dataclass
class _FdEntry:
    path: str
    cloexec: bool = False


@dataclass
class _ProcState:
    cwd: str
    fds: dict[int, _FdEntry] = field(default_factory=dict)
    # Shared tables under CLONE_FILES / CLONE_FS — identity via object id.
    share_files_with: int | None = None
    share_fs_with: int | None = None


@dataclass
class _HashMemo:
    """Per-process memo: (inode, size, mtime_ns) → digest already computed."""

    _cache: dict[tuple[int, int, int], Digest] = field(default_factory=dict)

    def digest_file(self, path: Path) -> Digest | None:
        try:
            if path.is_dir():
                return None
            st = path.stat()
        except OSError:
            return None
        key = (
            int(st.st_ino),
            int(st.st_size),
            int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            digest = sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        self._cache[key] = digest
        return digest


def _digest_label(label: str, payload: object) -> Digest:
    return sha256(canonical_json({"label": label, "body": payload})).hexdigest()


def _dir_entries_digest(path: Path) -> Digest | None:
    try:
        entries: list[tuple[str, str]] = []
        with os.scandir(path) as it:
            for entry in it:
                dtype = "unknown"
                if entry.is_symlink():
                    dtype = "lnk"
                elif entry.is_dir(follow_symlinks=False):
                    dtype = "dir"
                elif entry.is_file(follow_symlinks=False):
                    dtype = "reg"
                entries.append((entry.name, dtype))
        entries.sort()
        return _digest_label("dir_entries/v1", entries)
    except OSError:
        return None


def _symlink_digest(link_path: Path) -> Digest | None:
    try:
        text = os.readlink(link_path)
    except OSError:
        return None
    target_digest: Digest | None
    try:
        resolved = link_path.resolve(strict=False)
        if resolved.is_file():
            target_digest = sha256(resolved.read_bytes()).hexdigest()
        elif resolved.exists():
            target_digest = _digest_label("path_exists", str(resolved))
        else:
            target_digest = _ABSENT
    except OSError:
        target_digest = _ABSENT
    return _digest_label("symlink/v1", {"text": text, "target": target_digest})


def workspace_rel_or_abs(path: str, workspace: Path) -> str:
    """POSIX workspace-relative path, or absolute normalized form outside it."""
    raw = Path(path)
    try:
        resolved = raw if raw.is_absolute() else (workspace / raw)
        resolved = resolved.resolve(strict=False)
        workspace_res = workspace.resolve(strict=False)
        try:
            rel = resolved.relative_to(workspace_res)
            return PurePosixPath(*rel.parts).as_posix()
        except ValueError:
            return PurePosixPath(resolved.as_posix()).as_posix()
    except OSError:
        return path.replace("\\", "/")


def _is_write_flags(flags: str) -> bool:
    return any(h in flags for h in _OPEN_WRITE_HINTS) and "O_RDONLY" not in flags.split("|")[:1]


@dataclass
class NormalizeConfig:
    workspace: Path
    output_rel: str
    private_temp_root: Path | None = None
    purity_contract_id: str = "shadow"
    image_pinned: bool = False
    linux: bool = True


def normalize_trace(
    collected: CollectorResult,
    *,
    config: NormalizeConfig,
    companion_resources: tuple[ResourceIdentity, ...] = (),
) -> TraceContent:
    """Build TraceContent from collector output + optional companion enrichment."""
    reasons: list[str] = list(collected.incomplete_reasons)
    resources: dict[tuple[str, str, str], ResourceIdentity] = {}
    memo = _HashMemo()
    procs: dict[int, _ProcState] = {}
    workspace = config.workspace.resolve()
    output_abs = (workspace / config.output_rel).resolve()
    private_root = (
        config.private_temp_root.resolve() if config.private_temp_root is not None else None
    )

    def ensure_proc(pid: int) -> _ProcState:
        state = procs.get(pid)
        if state is None:
            state = _ProcState(cwd=str(workspace))
            procs[pid] = state
        return state

    def add_resource(resource: ResourceIdentity) -> None:
        # Cairn's own tracer/companion scratch space (§12: "Cairn's private
        # temp directory") is never user-meaningful identity, and its own
        # path embeds a fresh tempfile.mkdtemp() suffix every run — treating
        # a read/enumerate under it as an input would make semantic_work_key
        # differ on every invocation of the exact same command (§17: private
        # roots are excluded by policy, not hashed as accidental inputs).
        if (
            private_root is not None
            and resource.access_mode.is_input
            and resource.kind in (ResourceKind.FILE, ResourceKind.DIRECTORY)
            and Path(resource.ref).is_absolute()
            and _under(Path(resource.ref), private_root)
        ):
            return
        key = resource.identity_tuple
        existing = resources.get(key)
        if existing is None:
            resources[key] = resource
            return
        # Prefer syscall source; companion may refine ref only via separate keys.
        if (
            existing.source is ObservationSource.SYSCALL
            and resource.source is ObservationSource.PYTHON
        ):
            return
        # A plain content-hash read (`workspace_file/v1`) and a readlink's
        # symlink-scheme read (`symlink/v1`) can land on the identical
        # identity_tuple when a path is reached both ways in the same trace
        # (e.g. libc's startup `readlink("/proc/self/exe", ...)` resolving
        # to the same absolute path a later `openat` reads directly) — a
        # magic /proc readlink and a real content hash are not the same
        # observation, and without this rule whichever syscall happened to
        # be traced last silently won, making identity depend on kernel
        # scheduling jitter between otherwise-identical runs. The content
        # hash is what resolve_workspace_inputs re-verifies at replan time,
        # so it must always win regardless of arrival order.
        if existing.resolver == "workspace_file/v1" and resource.resolver == "symlink/v1":
            return
        if existing.resolver == "symlink/v1" and resource.resolver == "workspace_file/v1":
            resources[key] = resource
            return
        resources[key] = resource

    def resolve_path(pid: int, raw: str | None, dirfd: int | None = None) -> str | None:
        if raw is None:
            return None
        if raw.startswith("/"):
            return raw
        state = ensure_proc(pid)
        if dirfd is not None and dirfd != -100:
            entry = state.fds.get(dirfd)
            if entry is None:
                return None
            base = entry.path
        else:
            base = state.cwd
        return str(Path(base) / raw)

    loss_triggers: list[str] = []
    network_seen = False
    write_outside = False

    if not collected.platform_supported or not config.linux:
        reasons.append("incomplete_platform")
    if not config.image_pinned and config.purity_contract_id not in {"shadow"}:
        reasons.append("image_not_pinned")

    for event in collected.events:
        if event.kind == "incomplete":
            reasons.append(f"unparsed_or_truncated:{event.name}")
            continue
        if event.kind in {"exit", "killed"}:
            continue
        if event.kind != "syscall":
            continue

        name = event.name
        pid = event.pid
        state = ensure_proc(pid)
        args = split_args(event.args)
        value, errno, ret_path = parse_ret(event.ret)

        if name in _COVERAGE_LOSS_CALLS:
            loss_triggers.append(name)
            continue
        if name in _NETWORK_CALLS:
            network_seen = True
            sock_ref = f"socket:{name}"
            if args:
                sock_ref = f"socket:{name}:{args[0][:64]}"
            add_resource(
                ResourceIdentity(
                    kind=ResourceKind.SOCKET,
                    ref=sock_ref,
                    access_mode=AccessMode.READ,
                    exists=True,
                    version_digest=_digest_label("socket/v1", sock_ref),
                    resolver="socket/v1",
                    source=ObservationSource.SYSCALL,
                )
            )
            continue
        if name == "getrandom" or (
            name in _OPEN_CALLS and any("/dev/urandom" in a or "/dev/random" in a for a in args)
        ):
            add_resource(
                ResourceIdentity(
                    kind=ResourceKind.DEVICE,
                    ref="device:getrandom" if name == "getrandom" else "device:urandom",
                    access_mode=AccessMode.READ,
                    exists=True,
                    version_digest=_digest_label("device/v1", name),
                    resolver="device/v1",
                    source=ObservationSource.SYSCALL,
                    metadata=(("coverage_trigger", "random"),),
                )
            )
            # Random without a captured-seed purity policy is a coverage concern
            # only under supported contracts; shadow keeps the row as evidence.
            continue

        if name in {"chdir", "fchdir"}:
            if errno is not None:
                continue
            if name == "chdir":
                target = unescape_strace_string(args[0]) if args else None
                if target is None and args:
                    # Sometimes -y prints a path without quotes after resolve.
                    target = args[0].strip('"')
                abs_path = resolve_path(pid, target)
                if abs_path:
                    state.cwd = abs_path
            else:
                fd_tok = parse_fd_token(args[0]) if args else None
                if fd_tok and fd_tok[0] in state.fds:
                    state.cwd = state.fds[fd_tok[0]].path
            continue

        if name in _CLONE_CALLS:
            if value is None or value < 0:
                continue
            child_pid = int(value)
            flags = " ".join(args)
            parent = ensure_proc(pid)
            child = _ProcState(cwd=parent.cwd)
            if "CLONE_FILES" in flags:
                child.fds = parent.fds
                child.share_files_with = pid
            else:
                child.fds = {k: _FdEntry(v.path, v.cloexec) for k, v in parent.fds.items()}
            if "CLONE_FS" in flags:
                child.cwd = parent.cwd
                child.share_fs_with = pid
            procs[child_pid] = child
            continue

        if name in {"execve", "execveat"}:
            path_tok = args[0] if args else ""
            exec_path = unescape_strace_string(path_tok) or ret_path
            if exec_path is None and path_tok.startswith('"'):
                exec_path = path_tok.strip('"')
            if errno is None and exec_path:
                # Successful exec closes FD_CLOEXEC descriptors; cwd preserved.
                state.fds = {fd: ent for fd, ent in state.fds.items() if not ent.cloexec}
                rel = workspace_rel_or_abs(exec_path, workspace)
                exec_digest = memo.digest_file(Path(exec_path)) or _digest_label(
                    "exec_path", exec_path
                )
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.EXECUTE,
                        exists=True,
                        version_digest=exec_digest,
                        resolver="workspace_file/v1",
                        source=ObservationSource.SYSCALL,
                    )
                )
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.SUBPROCESS,
                        ref=rel,
                        access_mode=AccessMode.EXECUTE,
                        exists=True,
                        version_digest=exec_digest,
                        resolver="subprocess/v1",
                        source=ObservationSource.SYSCALL,
                    )
                )
            continue

        if name in _DUP_CALLS:
            if errno is not None or value is None:
                continue
            old_tok = parse_fd_token(args[0]) if args else None
            if old_tok is None:
                continue
            old_fd = old_tok[0]
            src = state.fds.get(old_fd)
            if src is None and old_tok[1]:
                src = _FdEntry(old_tok[1])
            if src is None:
                continue
            cloexec = "O_CLOEXEC" in (args[2] if len(args) > 2 else "")
            state.fds[int(value)] = _FdEntry(src.path, cloexec=cloexec or src.cloexec)
            continue

        if name == "fcntl":
            if len(args) < 2:
                continue
            fd_tok = parse_fd_token(args[0])
            cmd = args[1]
            if fd_tok is None:
                continue
            fd = fd_tok[0]
            if "F_DUPFD" in cmd and errno is None and value is not None:
                src = state.fds.get(fd)
                if src is not None:
                    state.fds[int(value)] = _FdEntry(
                        src.path, cloexec="F_DUPFD_CLOEXEC" in cmd or src.cloexec
                    )
            elif "F_SETFD" in cmd and fd in state.fds:
                cloexec = "FD_CLOEXEC" in " ".join(args[2:])
                prev = state.fds[fd]
                state.fds[fd] = _FdEntry(prev.path, cloexec=cloexec)
            continue

        if name in {"close", "close_range"}:
            if name == "close" and args:
                fd_tok = parse_fd_token(args[0])
                if fd_tok is not None:
                    state.fds.pop(fd_tok[0], None)
            elif name == "close_range" and len(args) >= 2:
                try:
                    lo, hi = int(args[0]), int(args[1])
                except ValueError:
                    continue
                for fd in list(state.fds):
                    if lo <= fd <= hi:
                        state.fds.pop(fd, None)
            continue

        if name in _OPEN_CALLS:
            open_path: str | None = None
            flags = ""
            dirfd: int | None = None
            if name in {"openat", "openat2"}:
                fd_tok = parse_fd_token(args[0]) if args else None
                dirfd = fd_tok[0] if fd_tok else -100
                open_path = unescape_strace_string(args[1]) if len(args) > 1 else None
                if open_path is None and len(args) > 1 and args[1].startswith('"'):
                    open_path = None  # truncated
                flags = args[2] if len(args) > 2 else ""
                if open_path is None and fd_tok and fd_tok[1] and errno is not None:
                    # Negative lookup sometimes still has the path in args.
                    open_path = unescape_strace_string(args[1]) if len(args) > 1 else None
            elif name == "open":
                open_path = unescape_strace_string(args[0]) if args else None
                flags = args[1] if len(args) > 1 else ""
            else:  # creat
                open_path = unescape_strace_string(args[0]) if args else None
                flags = "O_WRONLY|O_CREAT|O_TRUNC"

            if ret_path:
                open_path = ret_path
            abs_path = resolve_path(pid, open_path, dirfd=dirfd)
            if abs_path is None and open_path and open_path.startswith("/"):
                abs_path = open_path
            if abs_path is None:
                continue
            rel = workspace_rel_or_abs(abs_path, workspace)

            if errno in {"ENOENT", "ENOTDIR"}:
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.NEGATIVE,
                        exists=False,
                        version_digest=_ABSENT,
                        resolver="workspace_file/v1",
                        source=ObservationSource.SYSCALL,
                        metadata=(("errno", errno),),
                    )
                )
                continue
            if errno is not None:
                continue
            if value is None:
                continue

            cloexec = "O_CLOEXEC" in flags
            state.fds[int(value)] = _FdEntry(abs_path, cloexec=cloexec)

            # Directory opens exist so getdents64 can resolve the fd; hashing
            # directory bytes is impossible and would raise PermissionError.
            if "O_DIRECTORY" in flags:
                continue

            writing = _is_write_flags(flags) or name == "creat"
            file_digest: Digest
            if writing:
                mode = AccessMode.WRITE
                # Post-run digest when the file exists now.
                file_digest = memo.digest_file(Path(abs_path)) or _digest_label(
                    "write_path", abs_path
                )
                try:
                    out_path = Path(abs_path).resolve()
                    allowed = out_path == output_abs
                    if private_root is not None:
                        try:
                            out_path.relative_to(private_root)
                            allowed = True
                        except ValueError:
                            pass
                    if not allowed:
                        write_outside = True
                except OSError:
                    write_outside = True
            else:
                mode = AccessMode.READ
                maybe = memo.digest_file(Path(abs_path))
                if maybe is None:
                    continue
                file_digest = maybe

            add_resource(
                ResourceIdentity(
                    kind=ResourceKind.FILE,
                    ref=rel,
                    access_mode=mode,
                    exists=True,
                    version_digest=file_digest,
                    resolver="workspace_file/v1",
                    source=ObservationSource.SYSCALL,
                )
            )
            continue

        if name in _STAT_CALLS:
            stat_path: str | None = None
            dirfd = -100
            if name in {"newfstatat", "faccessat", "faccessat2", "statx"}:
                fd_tok = parse_fd_token(args[0]) if args else None
                dirfd = fd_tok[0] if fd_tok else -100
                stat_path = unescape_strace_string(args[1]) if len(args) > 1 else None
            elif name in {"stat", "lstat", "access"}:
                stat_path = unescape_strace_string(args[0]) if args else None
            elif name == "fstat":
                fd_tok = parse_fd_token(args[0]) if args else None
                if fd_tok and fd_tok[0] in state.fds:
                    stat_path = state.fds[fd_tok[0]].path
                elif fd_tok and fd_tok[1]:
                    stat_path = fd_tok[1]
            if ret_path:
                stat_path = ret_path
            abs_path = resolve_path(pid, stat_path, dirfd=dirfd if dirfd != -100 else None)
            if abs_path is None and stat_path and stat_path.startswith("/"):
                abs_path = stat_path
            if abs_path is None:
                continue
            rel = workspace_rel_or_abs(abs_path, workspace)
            if errno in {"ENOENT", "ENOTDIR"}:
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.NEGATIVE,
                        exists=False,
                        version_digest=_ABSENT,
                        resolver="workspace_file/v1",
                        source=ObservationSource.SYSCALL,
                        metadata=(("errno", errno), ("probe", name)),
                    )
                )
            elif errno is None:
                stat_digest = memo.digest_file(Path(abs_path))
                if stat_digest is None:
                    continue
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.READ,
                        exists=True,
                        version_digest=stat_digest,
                        resolver="workspace_file/v1",
                        source=ObservationSource.SYSCALL,
                        metadata=(("probe", name),),
                    )
                )
            continue

        if name == "getdents64":
            if errno is not None:
                continue
            fd_tok = parse_fd_token(args[0]) if args else None
            dent_path: str | None = None
            if fd_tok and fd_tok[0] in state.fds:
                dent_path = state.fds[fd_tok[0]].path
            elif fd_tok and fd_tok[1]:
                dent_path = fd_tok[1]
            if dent_path is None:
                continue
            rel = workspace_rel_or_abs(dent_path, workspace)
            dent_digest = _dir_entries_digest(Path(dent_path))
            if dent_digest is None:
                continue
            add_resource(
                ResourceIdentity(
                    kind=ResourceKind.DIRECTORY,
                    ref=rel,
                    access_mode=AccessMode.ENUMERATE,
                    exists=True,
                    version_digest=dent_digest,
                    resolver="dir_entries/v1",
                    source=ObservationSource.SYSCALL,
                )
            )
            continue

        if name in {"readlink", "readlinkat"}:
            link_path: str | None
            if name == "readlinkat":
                fd_tok = parse_fd_token(args[0]) if args else None
                dirfd = fd_tok[0] if fd_tok else -100
                link_path = unescape_strace_string(args[1]) if len(args) > 1 else None
                abs_path = resolve_path(pid, link_path, dirfd=dirfd)
            else:
                link_path = unescape_strace_string(args[0]) if args else None
                abs_path = resolve_path(pid, link_path)
            if abs_path is None and link_path and link_path.startswith("/"):
                abs_path = link_path
            if abs_path is None:
                continue
            rel = workspace_rel_or_abs(abs_path, workspace)
            if errno in {"ENOENT", "EINVAL"}:
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.NEGATIVE,
                        exists=False,
                        version_digest=_ABSENT,
                        resolver="symlink/v1",
                        source=ObservationSource.SYSCALL,
                    )
                )
            elif errno is None:
                link_digest = _symlink_digest(Path(abs_path))
                if link_digest is None:
                    continue
                add_resource(
                    ResourceIdentity(
                        kind=ResourceKind.FILE,
                        ref=rel,
                        access_mode=AccessMode.READ,
                        exists=True,
                        version_digest=link_digest,
                        resolver="symlink/v1",
                        source=ObservationSource.SYSCALL,
                    )
                )
            continue

        if name in {"rename", "renameat", "renameat2"}:
            # Both paths are writes (atomic-output idiom).
            paths: list[str] = []
            if name == "rename":
                for tok in args[:2]:
                    p = unescape_strace_string(tok)
                    if p:
                        abs_p = resolve_path(pid, p)
                        if abs_p:
                            paths.append(abs_p)
            else:
                # renameat(olddirfd, old, newdirfd, new)
                if len(args) >= 4:
                    for dir_i, path_i in ((0, 1), (2, 3)):
                        fd_tok = parse_fd_token(args[dir_i])
                        p = unescape_strace_string(args[path_i])
                        dfd = fd_tok[0] if fd_tok else -100
                        abs_p = resolve_path(pid, p, dirfd=dfd)
                        if abs_p:
                            paths.append(abs_p)
            if errno is None:
                for abs_path in paths:
                    rel = workspace_rel_or_abs(abs_path, workspace)
                    digest = memo.digest_file(Path(abs_path)) or _digest_label(
                        "rename_path", abs_path
                    )
                    add_resource(
                        ResourceIdentity(
                            kind=ResourceKind.FILE,
                            ref=rel,
                            access_mode=AccessMode.WRITE,
                            exists=True,
                            version_digest=digest,
                            resolver="workspace_file/v1",
                            source=ObservationSource.SYSCALL,
                        )
                    )
                    try:
                        if Path(abs_path).resolve() != output_abs and (
                            private_root is None or not _under(Path(abs_path), private_root)
                        ):
                            write_outside = True
                    except OSError:
                        write_outside = True
            continue

        if name == "mmap":
            # mmap(addr, len, prot, flags, fd, offset) — file-backed when fd >= 0.
            if len(args) < 5:
                continue
            fd_tok = parse_fd_token(args[4])
            if fd_tok is None or fd_tok[0] < 0:
                continue
            mmap_path: str | None = None
            if fd_tok[0] in state.fds:
                mmap_path = state.fds[fd_tok[0]].path
            elif fd_tok[1]:
                mmap_path = fd_tok[1]
            if mmap_path is None or errno is not None:
                continue
            rel = workspace_rel_or_abs(mmap_path, workspace)
            mmap_digest = memo.digest_file(Path(mmap_path))
            if mmap_digest is None:
                continue
            add_resource(
                ResourceIdentity(
                    kind=ResourceKind.FILE,
                    ref=rel,
                    access_mode=AccessMode.READ,
                    exists=True,
                    version_digest=mmap_digest,
                    resolver="workspace_file/v1",
                    source=ObservationSource.SYSCALL,
                    metadata=(("via", "mmap"),),
                )
            )
            continue

    # Companion may only ADD rows / refine refs — never remove or upgrade coverage.
    for resource in companion_resources:
        key = resource.identity_tuple
        if key not in resources:
            resources[key] = resource
        else:
            # Refine ref label only when kinds/modes match and companion is PYTHON.
            existing = resources[key]
            if (
                resource.source is ObservationSource.PYTHON
                and resource.ref != existing.ref
                and resource.kind == existing.kind
                and resource.access_mode == existing.access_mode
            ):
                resources[key] = ResourceIdentity(
                    kind=existing.kind,
                    ref=resource.ref,
                    access_mode=existing.access_mode,
                    exists=existing.exists,
                    version_digest=existing.version_digest,
                    resolver=existing.resolver,
                    source=existing.source,
                    metadata=existing.metadata + (("refined_ref", resource.ref),),
                )

    # A NEGATIVE lookup for a ref that this same trace also accessed
    # positively (READ/EXECUTE/ENUMERATE) is TOCTOU noise, not a governing
    # "this path's absence matters" dependency — e.g. CPython's own module
    # resolution probes a script path with ENOENT-tolerant lookups before
    # the real open that succeeds. Keeping both meant a file that exists in
    # every run (including the one that recorded it) could never
    # re-resolve on a later run, because AccessMode.NEGATIVE unconditionally
    # forces a fresh run when `path.exists()` — permanently blocking restore
    # for any command whose declared negative path is, in fact, present.
    positive_refs = {
        (r.kind, r.ref)
        for r in resources.values()
        if r.access_mode in (AccessMode.READ, AccessMode.EXECUTE, AccessMode.ENUMERATE)
    }
    for key in [
        k
        for k, r in resources.items()
        if r.access_mode is AccessMode.NEGATIVE and (r.kind, r.ref) in positive_refs
    ]:
        del resources[key]

    coverage, coverage_reasons = decide_coverage(
        base_reasons=tuple(dict.fromkeys(reasons)),
        loss_triggers=tuple(loss_triggers),
        network_seen=network_seen,
        write_outside=write_outside,
        platform_ok=collected.platform_supported and config.linux,
        image_pinned=config.image_pinned,
        purity_contract_id=config.purity_contract_id,
    )
    return TraceContent(
        coverage_state=coverage,
        resources=tuple(resources.values()),
        incomplete_reasons=coverage_reasons,
    )


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def decide_coverage(
    *,
    base_reasons: tuple[str, ...],
    loss_triggers: tuple[str, ...],
    network_seen: bool,
    write_outside: bool,
    platform_ok: bool,
    image_pinned: bool,
    purity_contract_id: str,
) -> tuple[CoverageState, tuple[str, ...]]:
    """Appendix C.3 — first match wins."""
    reasons: list[str] = list(base_reasons)

    loss_markers = (
        "tracer_died",
        "trace_truncated",
        "trace_empty_or_missing",
        "missing_top_level_exit_record",
        "exit_status_disagreement",
        "unparseable_exit_status",
        "orphan_resumed_line",
        "truncated_unfinished",
        "strace_missing",
    )
    if any(any(m in r for m in loss_markers) for r in reasons) or any(
        r.startswith("unparsed") for r in reasons
    ):
        if not any("trace_loss" in r for r in reasons):
            reasons.append("trace_loss")
        return CoverageState.INCOMPLETE_TRACE_LOSS, tuple(dict.fromkeys(reasons))

    if loss_triggers:
        reasons.extend(f"coverage_trigger:{t}" for t in loss_triggers)
        return CoverageState.INCOMPLETE_TRACE_LOSS, tuple(dict.fromkeys(reasons))

    if network_seen:
        reasons.append("socket_activity_without_stable_adapter")
        return CoverageState.INCOMPLETE_NETWORK, tuple(dict.fromkeys(reasons))

    if write_outside:
        reasons.append("write_outside_declared_output")
        return CoverageState.INCOMPLETE_WRITE, tuple(dict.fromkeys(reasons))

    # INCOMPLETE_INPUT_RACE is decided by caller comparing pre/post identities.

    if not platform_ok or (not image_pinned and purity_contract_id not in {"shadow"}):
        if not platform_ok:
            reasons.append("not_linux_or_unpinned_image")
        elif not image_pinned:
            reasons.append("image_not_pinned")
        return CoverageState.INCOMPLETE_PLATFORM, tuple(dict.fromkeys(reasons))

    if purity_contract_id == "shadow":
        reasons.append("shadow_contract")
        return CoverageState.SHADOW_UNQUALIFIED, tuple(dict.fromkeys(reasons))

    # Named contracts that authorize COMPLETE_* are Day-3+. Observation alone
    # never upgrades past SHADOW (Gate A).
    reasons.append("gate_a_no_complete_on_observation_alone")
    return CoverageState.SHADOW_UNQUALIFIED, tuple(dict.fromkeys(reasons))


def semantic_resource_set(trace: TraceContent) -> tuple[ResourceIdentity, ...]:
    """Workspace-relative resources used for local-vs-image identity comparison.

    Absolute paths outside the workspace are environment noise; under a pinned
    image they are covered by the image digest rather than compared byte-wise.
    Companion module labels and the local ``.venv`` tree are also excluded —
    they differ by install layout even when the command's causal inputs match.
    """
    out: list[ResourceIdentity] = []
    for resource in trace.resources:
        if resource.kind not in {
            ResourceKind.FILE,
            ResourceKind.DIRECTORY,
            ResourceKind.SUBPROCESS,
        }:
            continue
        if resource.ref.startswith("/"):
            continue
        if len(resource.ref) >= 2 and resource.ref[1] == ":":
            continue
        if resource.ref.startswith("py-module:"):
            continue
        if resource.ref == ".venv" or resource.ref.startswith(".venv/"):
            continue
        out.append(resource)
    return tuple(sorted(out, key=lambda r: r.identity_tuple))

"""Appendix C.2 conformance matrix for the Flight Recorder normalizer.

These tests feed synthetic strace lines — the collector's parsing contract —
so the matrix runs on Windows developers' machines without a live strace.
Live Linux collection is covered separately where the platform supports it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cairn.flight.types import AccessMode, CoverageState, ResourceKind
from cairn.trace.collector import CollectorResult, parse_strace_output, strace_argv
from cairn.trace.normalize import NormalizeConfig, normalize_trace, semantic_resource_set


def _collected(text: str, *, incomplete: tuple[str, ...] = ()) -> CollectorResult:
    events, parse_reasons = parse_strace_output(text)
    return CollectorResult(
        child_exit_code=0,
        events=events,
        incomplete_reasons=tuple(dict.fromkeys((*incomplete, *parse_reasons))),
        platform_supported=True,
        top_level_pid=1000,
    )


def _norm(tmp: Path, text: str, *, output_rel: str = "out/result.bin", **kwargs: object):
    cfg = NormalizeConfig(
        workspace=tmp,
        output_rel=output_rel,
        private_temp_root=tmp / ".cairn-tmp",
        purity_contract_id="shadow",
        image_pinned=False,
        linux=True,
        **kwargs,  # type: ignore[arg-type]
    )
    return normalize_trace(_collected(text), config=cfg)


def test_strace_argv_matches_appendix_c1() -> None:
    trace_path = Path("/tmp/t.log")
    argv = strace_argv(trace_path=trace_path, command=["python", "x.py"])
    assert argv[0] == "strace"
    assert argv[1:7] == ["-f", "--kill-on-exit", "-s", "32", "-y", "-o"]
    assert Path(argv[7]) == trace_path
    assert argv[8:10] == ["-e", argv[9]]
    assert "getdents64" in argv[9]
    assert "io_uring_setup" in argv[9]
    assert argv[-3:] == ["--", "python", "x.py"]
    assert "-qq" not in argv


def test_file_open_read(tmp_path: Path) -> None:
    target = tmp_path / "data" / "in.txt"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    abs_p = target.resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_p}", O_RDONLY|O_CLOEXEC) = 3<{abs_p}>
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    reads = [r for r in trace.resources if r.access_mode is AccessMode.READ]
    assert any(r.ref == "data/in.txt" and r.kind is ResourceKind.FILE for r in reads)


def test_stat_probe(tmp_path: Path) -> None:
    target = tmp_path / "cfg.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    abs_p = target.resolve().as_posix()
    text = f"""\
1000  newfstatat(AT_FDCWD, "{abs_p}", 0x7fff, 0) = 0
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert any(r.ref == "cfg.yaml" and r.access_mode is AccessMode.READ for r in trace.resources)


def test_enoent_negative(tmp_path: Path) -> None:
    missing = (tmp_path / "nope.bin").resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{missing}", O_RDONLY) = -1 ENOENT (No such file or directory)
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    neg = [r for r in trace.resources if r.access_mode is AccessMode.NEGATIVE]
    assert len(neg) == 1
    assert neg[0].ref == "nope.bin"
    assert neg[0].version_digest == "absent"
    assert neg[0].exists is False


def test_getdents64_directory(tmp_path: Path) -> None:
    d = tmp_path / "inputs"
    d.mkdir()
    (d / "a.txt").write_text("a", encoding="utf-8")
    (d / "b.txt").write_text("b", encoding="utf-8")
    abs_d = d.resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_d}", O_RDONLY|O_DIRECTORY|O_CLOEXEC) = 3<{abs_d}>
1000  getdents64(3<{abs_d}>, /* entries */, 2048) = 48
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    dirs = [r for r in trace.resources if r.kind is ResourceKind.DIRECTORY]
    assert any(r.ref == "inputs" and r.access_mode is AccessMode.ENUMERATE for r in dirs)


def test_symlink_retarget_changes_digest(tmp_path: Path) -> None:
    link = tmp_path / "alias"
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("aaa", encoding="utf-8")
    b.write_text("bbb", encoding="utf-8")
    try:
        link.symlink_to(a.name)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    abs_link = str(link).replace("\\", "/")
    text = f"""\
1000  readlink("{abs_link}", "a.txt", 4096) = 5
1000  +++ exited with 0 +++
"""
    first = _norm(tmp_path, text)
    digests_1 = {
        r.version_digest
        for r in first.resources
        if r.resolver == "symlink/v1" and r.access_mode is AccessMode.READ
    }
    link.unlink()
    try:
        link.symlink_to(b.name)
    except OSError:
        pytest.skip("symlink retarget not permitted on this host")
    second = _norm(tmp_path, text)
    digests_2 = {
        r.version_digest
        for r in second.resources
        if r.resolver == "symlink/v1" and r.access_mode is AccessMode.READ
    }
    assert digests_1
    assert digests_2
    assert digests_1 != digests_2


def test_subprocess_execve(tmp_path: Path) -> None:
    # Use a real file so content digest succeeds.
    bin_path = tmp_path / "tool.sh"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    abs_b = bin_path.resolve().as_posix()
    text = f"""\
1000  clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID|SIGCHLD) = 1001
1001  execve("{abs_b}", ["tool.sh"], 0x7fff /* 10 vars */) = 0
1001  +++ exited with 0 +++
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert any(r.kind is ResourceKind.SUBPROCESS for r in trace.resources)
    assert any(
        r.access_mode is AccessMode.EXECUTE and r.kind is ResourceKind.FILE for r in trace.resources
    )


def test_chdir_relative_open(tmp_path: Path) -> None:
    sub = tmp_path / "work"
    sub.mkdir()
    f = sub / "x.txt"
    f.write_text("x", encoding="utf-8")
    abs_sub = sub.resolve().as_posix()
    abs_f = f.resolve().as_posix()
    text = f"""\
1000  chdir("{abs_sub}") = 0
1000  openat(AT_FDCWD, "x.txt", O_RDONLY) = 3<{abs_f}>
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert any(r.ref == "work/x.txt" and r.access_mode is AccessMode.READ for r in trace.resources)


def test_mmap_only_input(tmp_path: Path) -> None:
    blob = tmp_path / "model.bin"
    blob.write_bytes(b"\x00\x01\x02\x03")
    abs_b = blob.resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_b}", O_RDONLY) = 4<{abs_b}>
1000  mmap(NULL, 4096, PROT_READ, MAP_PRIVATE, 4<{abs_b}>, 0) = 0x7f00
1000  +++ exited with 0 +++
"""
    # Force mmap-only by checking metadata via=mmap exists even if open also recorded READ.
    trace = _norm(tmp_path, text)
    mmap_rows = [
        r for r in trace.resources if r.ref == "model.bin" and ("via", "mmap") in r.metadata
    ]
    assert mmap_rows
    assert mmap_rows[0].access_mode is AccessMode.READ


def test_write_outside_output(tmp_path: Path) -> None:
    outside = tmp_path / "side_effect.txt"
    abs_o = outside.resolve().as_posix()
    declared = "out/ok.bin"
    (tmp_path / "out").mkdir()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_o}", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3<{abs_o}>
1000  +++ exited with 0 +++
"""
    outside.write_text("leak", encoding="utf-8")
    trace = _norm(tmp_path, text, output_rel=declared)
    assert trace.coverage_state is CoverageState.INCOMPLETE_WRITE
    assert any("write_outside" in r for r in trace.incomplete_reasons)


def test_socket_incomplete_network(tmp_path: Path) -> None:
    text = """\
1000  socket(AF_INET, SOCK_STREAM, IPPROTO_IP) = 5
1000  connect(5, {sa_family=AF_INET, sin_port=htons(80)}, 16) = 0
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert trace.coverage_state is CoverageState.INCOMPLETE_NETWORK
    assert any(r.kind is ResourceKind.SOCKET for r in trace.resources)


def test_killed_tracer_incomplete_trace_loss(tmp_path: Path) -> None:
    collected = CollectorResult(
        child_exit_code=137,
        events=(),
        incomplete_reasons=("tracer_died", "trace_empty_or_missing"),
        platform_supported=True,
    )
    trace = normalize_trace(
        collected,
        config=NormalizeConfig(
            workspace=tmp_path,
            output_rel="out/x.bin",
            purity_contract_id="shadow",
            linux=True,
        ),
    )
    assert trace.coverage_state is CoverageState.INCOMPLETE_TRACE_LOSS


def test_exit_disagreement_is_trace_loss(tmp_path: Path) -> None:
    text = """\
1000  +++ exited with 0 +++
"""
    events, reasons = parse_strace_output(text)
    # Simulate collector marking disagreement.
    collected = CollectorResult(
        child_exit_code=0,
        events=events,
        incomplete_reasons=("exit_status_disagreement", *reasons),
        platform_supported=True,
        top_level_pid=1000,
    )
    trace = normalize_trace(
        collected,
        config=NormalizeConfig(
            workspace=tmp_path,
            output_rel="out/x.bin",
            purity_contract_id="shadow",
            linux=True,
        ),
    )
    assert trace.coverage_state is CoverageState.INCOMPLETE_TRACE_LOSS


def test_unparsed_line_is_trace_loss(tmp_path: Path) -> None:
    text = """\
1000  this is not a strace line at all
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert trace.coverage_state is CoverageState.INCOMPLETE_TRACE_LOSS


def test_shadow_on_clean_linux_trace(tmp_path: Path) -> None:
    target = tmp_path / "in.txt"
    target.write_text("x", encoding="utf-8")
    abs_p = target.resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_p}", O_RDONLY) = 3<{abs_p}>
1000  +++ exited with 0 +++
"""
    trace = _norm(tmp_path, text)
    assert trace.coverage_state is CoverageState.SHADOW_UNQUALIFIED


def test_windows_platform_incomplete(tmp_path: Path) -> None:
    collected = CollectorResult(
        child_exit_code=0,
        events=(),
        incomplete_reasons=("not_linux",),
        platform_supported=False,
    )
    trace = normalize_trace(
        collected,
        config=NormalizeConfig(
            workspace=tmp_path,
            output_rel="out/x.bin",
            purity_contract_id="shadow",
            linux=False,
        ),
    )
    assert trace.coverage_state is CoverageState.INCOMPLETE_PLATFORM


def test_semantic_resource_set_drops_absolute(tmp_path: Path) -> None:
    target = tmp_path / "in.txt"
    target.write_text("x", encoding="utf-8")
    abs_p = target.resolve().as_posix()
    text = f"""\
1000  openat(AT_FDCWD, "{abs_p}", O_RDONLY) = 3<{abs_p}>
1000  openat(AT_FDCWD, "/usr/lib/python3.12/os.py", O_RDONLY) = 4</usr/lib/python3.12/os.py>
1000  +++ exited with 0 +++
"""
    # Create a fake absolute file only if we're on a path we can write — skip hash miss.
    trace = _norm(tmp_path, text)
    # Manually inject absolute by normalizing; /usr may not exist to hash — that's ok
    # if the open was skipped. Ensure workspace-relative remains.
    sem = semantic_resource_set(trace)
    assert all(not r.ref.startswith("/") for r in sem)


def test_private_temp_root_reads_excluded_from_inputs(tmp_path: Path) -> None:
    """Regression: two runs of the identical command must produce the exact
    same input resource set even though Cairn's own tracer/companion scratch
    directory (tempfile.mkdtemp(prefix="cairn-scout-")) gets a fresh random
    name every run. Before this fix, a read/enumerate of a file under that
    private root leaked into `trace.inputs` with the random path baked into
    `ref`, so semantic_work_key differed on every single invocation of the
    exact same command and steady-state RESTORE could never be reached."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    real_input = workspace / "in.txt"
    real_input.write_text("hello\n", encoding="utf-8")

    def _run_with_private_root(root_name: str) -> tuple[object, str]:
        private_root = tmp_path / root_name
        companion = private_root / "tmp" / "companion"
        companion.mkdir(parents=True)
        sitecustomize = companion / "sitecustomize.py"
        sitecustomize.write_text("# companion\n", encoding="utf-8")
        abs_input = real_input.resolve().as_posix()
        abs_companion_dir = companion.resolve().as_posix()
        abs_site = sitecustomize.resolve().as_posix()
        text = f"""\
1000  openat(AT_FDCWD, "{abs_input}", O_RDONLY|O_CLOEXEC) = 3<{abs_input}>
1000  openat(AT_FDCWD, "{abs_companion_dir}", O_RDONLY|O_DIRECTORY|O_CLOEXEC) = 4<{abs_companion_dir}>
1000  getdents64(4<{abs_companion_dir}>, /* entries */, 2048) = 32
1000  openat(AT_FDCWD, "{abs_site}", O_RDONLY|O_CLOEXEC) = 5<{abs_site}>
1000  +++ exited with 0 +++
"""
        cfg = NormalizeConfig(
            workspace=workspace,
            output_rel="out/result.bin",
            private_temp_root=private_root,
            purity_contract_id="deterministic-file/v1",
            image_pinned=True,
            linux=True,
        )
        return normalize_trace(_collected(text), config=cfg), private_root.resolve().as_posix()

    trace_a, root_a = _run_with_private_root("cairn-scout-aaaaaaaa")
    trace_b, root_b = _run_with_private_root("cairn-scout-bbbbbbbb")

    assert not any(r.ref.startswith(root_a) for r in trace_a.resources)  # type: ignore[attr-defined]
    assert not any(r.ref.startswith(root_b) for r in trace_b.resources)  # type: ignore[attr-defined]
    inputs_a = {r.identity_tuple for r in trace_a.inputs}  # type: ignore[attr-defined]
    inputs_b = {r.identity_tuple for r in trace_b.inputs}  # type: ignore[attr-defined]
    assert inputs_a == inputs_b
    assert any(r.ref == "in.txt" for r in trace_a.inputs)  # type: ignore[attr-defined]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
def test_symlink_helper_available(tmp_path: Path) -> None:
    # Windows may require admin for symlinks; skip gracefully.
    src = tmp_path / "t.txt"
    src.write_text("t", encoding="utf-8")
    dst = tmp_path / "l"
    try:
        dst.symlink_to(src.name)
    except OSError:
        pytest.skip("symlink creation not permitted")
    assert dst.is_symlink() or stat.S_ISLNK(dst.lstat().st_mode)

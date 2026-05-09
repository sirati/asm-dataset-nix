"""Tests for the SLURM run-invariant checks.

The file-only checks (1-4) use synthetic on-disk fixtures; the cluster
checks (5-7) use a hand-rolled :class:`ClusterProbe` mock that returns
canned :class:`PodmanRow`, :class:`ListenerRow` and :class:`ProcessRow`
objects so the tests never touch a live cluster.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    ListenerRow,
    PodmanRow,
    ProcessRow,
)
from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    check_build_failures,
    check_clean_exit,
    check_manifest_count_matches,
    check_no_bind_errors,
    check_no_leaked_containers,
    check_no_leaked_listener_ports,
    check_no_leaked_processes,
    run_all_invariants,
    run_cluster_invariants,
    run_file_invariants,
    wait_squeue_empty,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


# A minimal slurm_<jobid>.out tail mirroring the real format (ANSI
# escapes, ``task completed ... task_type=Some("variant") ...
# success=true``, success markers near the end).
_OUT_HEAD = textwrap.dedent(
    """\
    \x1b[2m2026-05-08T09:07:50Z\x1b[0m INFO _native::cli: starting secondary
    Some image-pull noise here, irrelevant to the invariants.
    """
)

_VARIANT_LINE = (
    '\x1b[2m2026-05-08T09:08:42Z\x1b[0m INFO '
    '\x1b[2mdynrunner_manager_distributed::secondary::processing\x1b[0m: '
    'task completed secondary=secondary-0 worker_id=0 '
    'task_id=Some("variant__x86_64-linux__hello-x86_64-clang10-O0") '
    'phase=Some("phase_build") task_type=Some("variant") '
    'task_hash=Some("a83ece0a80571a68") success=true\n'
)

_TOOLCHAIN_LINE = (
    '\x1b[2m2026-05-08T09:08:15Z\x1b[0m INFO '
    '\x1b[2mdynrunner_manager_distributed::secondary::processing\x1b[0m: '
    'task completed secondary=secondary-0 worker_id=0 '
    'task_id=Some("toolchain__x86_64-linux__x86_64__clang10") '
    'phase=Some("phase_build") task_type=Some("toolchain") '
    'task_hash=Some("34cc7b97b9033a15") success=true\n'
)

_OUT_TAIL = textwrap.dedent(
    """\
    \x1b[2m2026-05-08T09:08:42Z\x1b[0m INFO \x1b[2mdynrunner_manager_distributed::secondary\x1b[0m: secondary finished secondary=secondary-0 completed=2
    \x1b[2m2026-05-08T09:08:42Z\x1b[0m INFO \x1b[2m_native::managers::secondary\x1b[0m: secondary finished successfully
    Container exited with code: 0
    ==================================================
    Job completed
    Time: Fri May  8 09:08:44 AM UTC 2026
    ==================================================
    Cleaning up temporary directory: /tmp/asm-c4b7f661
    """
)


def _write_out(path: Path, *, variant_count: int) -> None:
    parts = [_OUT_HEAD, _TOOLCHAIN_LINE]
    parts.extend(_VARIANT_LINE for _ in range(variant_count))
    parts.append(_OUT_TAIL)
    path.write_text("".join(parts), encoding="utf-8")


def _make_run_dir(
    tmp_path: Path,
    *,
    jobid: int = 16,
    variant_count: int = 1,
    manifest_count: int | None = None,
    err_text: str = "",
    build_failure_count: int = 0,
    skip_out: bool = False,
    skip_err: bool = False,
    out_text_override: str | None = None,
) -> Path:
    """Build a synthetic run-log directory under ``tmp_path``.

    By default produces a clean run with ``variant_count`` completed
    variants and matching manifest files. ``manifest_count=None`` means
    "match the variant count" (the happy path).
    """
    run_dir = tmp_path / f"run_2026{jobid:08d}"
    run_dir.mkdir()

    if not skip_out:
        out_path = run_dir / f"slurm_{jobid}.out"
        if out_text_override is not None:
            out_path.write_text(out_text_override, encoding="utf-8")
        else:
            _write_out(out_path, variant_count=variant_count)

    if not skip_err:
        (run_dir / f"slurm_{jobid}.err").write_text(
            err_text, encoding="utf-8"
        )

    manifests = run_dir / "manifests"
    manifests.mkdir()
    n_manifests = (
        variant_count if manifest_count is None else manifest_count
    )
    for i in range(n_manifests):
        (manifests / f"variant_{i}.json").write_text("{}\n")

    build_failures = run_dir / "build-failures"
    build_failures.mkdir()
    for i in range(build_failure_count):
        (build_failures / f"task_{i}.log").write_text(
            "synthetic failure\n"
        )

    return run_dir


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_all_invariants_pass(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, variant_count=3)
    artifacts = RunArtifacts(run_dir=run_dir)

    results = run_file_invariants(artifacts, expected_failure_count=0)
    assert len(results) == 4
    assert all(isinstance(r, InvariantResult) for r in results)
    assert all(r.passed for r in results), [str(r) for r in results]


def test_happy_path_with_expected_failures(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, variant_count=2, build_failure_count=1
    )
    artifacts = RunArtifacts(run_dir=run_dir)
    results = run_file_invariants(artifacts, expected_failure_count=1)
    assert all(r.passed for r in results), [str(r) for r in results]


# ---------------------------------------------------------------------------
# Invariant 1: clean exit
# ---------------------------------------------------------------------------


def test_clean_exit_missing_secondary_finished(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path,
        out_text_override=(
            _OUT_HEAD + _VARIANT_LINE + "Container exited with code: 0\n"
        ),
        variant_count=1,
    )
    result = check_clean_exit(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "secondary finished successfully" in result.detail


def test_clean_exit_missing_container_exit(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path,
        out_text_override=(
            _OUT_HEAD + _VARIANT_LINE + "secondary finished successfully\n"
        ),
        variant_count=1,
    )
    result = check_clean_exit(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "Container exited with code: 0" in result.detail


def test_clean_exit_no_out_files(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, skip_out=True)
    result = check_clean_exit(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "no slurm_*.out" in result.detail


# ---------------------------------------------------------------------------
# Invariant 2: bind errors
# ---------------------------------------------------------------------------


def test_no_bind_errors_address_in_use(tmp_path: Path) -> None:
    err_text = (
        "some warning\n"
        "OSError: [Errno 98] Address already in use\n"
        "  at peer_push.py:222\n"
    )
    run_dir = _make_run_dir(tmp_path, err_text=err_text)
    result = check_no_bind_errors(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "Address already in use" in result.detail


def test_no_bind_errors_eaddrinuse(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, err_text="bind() failed: EADDRINUSE\n"
    )
    result = check_no_bind_errors(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "EADDRINUSE" in result.detail


def test_no_bind_errors_clean_err(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path,
        err_text="image pull warning, totally unrelated noise\n",
    )
    result = check_no_bind_errors(RunArtifacts(run_dir=run_dir))
    assert result.passed


def test_no_bind_errors_no_err_files_passes(tmp_path: Path) -> None:
    # An absent .err is not itself a bind error.
    run_dir = _make_run_dir(tmp_path, skip_err=True)
    result = check_no_bind_errors(RunArtifacts(run_dir=run_dir))
    assert result.passed


# ---------------------------------------------------------------------------
# Invariant 3: manifest count vs completed variants
# ---------------------------------------------------------------------------


def test_manifest_count_too_low(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, variant_count=3, manifest_count=2
    )
    result = check_manifest_count_matches(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "2 entries" in result.detail
    assert "3 completed" in result.detail


def test_manifest_count_too_high(tmp_path: Path) -> None:
    run_dir = _make_run_dir(
        tmp_path, variant_count=1, manifest_count=4
    )
    result = check_manifest_count_matches(RunArtifacts(run_dir=run_dir))
    assert not result.passed
    assert "4 entries" in result.detail


def test_manifest_count_missing_dir_with_zero_completed(
    tmp_path: Path,
) -> None:
    # Empty/missing manifests dir + zero completed = match.
    run_dir = _make_run_dir(
        tmp_path,
        variant_count=0,
        manifest_count=0,
        out_text_override=_OUT_HEAD + _OUT_TAIL,
    )
    # Remove the manifests dir entirely so we exercise the missing-dir path.
    (run_dir / "manifests").rmdir()
    result = check_manifest_count_matches(RunArtifacts(run_dir=run_dir))
    assert result.passed


def test_manifest_count_toolchain_lines_ignored(tmp_path: Path) -> None:
    # Toolchain task_type lines must NOT count toward the manifest total.
    run_dir = _make_run_dir(tmp_path, variant_count=2)
    result = check_manifest_count_matches(RunArtifacts(run_dir=run_dir))
    assert result.passed, result.detail


def test_manifest_count_meta_and_toolchain_files_excluded(
    tmp_path: Path,
) -> None:
    # The framework drops ``_meta.json`` and ``toolchain_<x>__<y>.json``
    # alongside the per-variant manifests; only true variant manifests
    # should count.
    run_dir = _make_run_dir(tmp_path, variant_count=1)
    manifests = run_dir / "manifests"
    (manifests / "_meta.json").write_text("{}\n")
    (manifests / "toolchain__aarch64__clang10.json").write_text("{}\n")
    result = check_manifest_count_matches(RunArtifacts(run_dir=run_dir))
    assert result.passed, result.detail


def test_manifest_count_shared_fs_overrides_run_dir(tmp_path: Path) -> None:
    # When ``shared_fs`` is set, manifests are read from
    # ``shared_fs/manifests/`` rather than ``run_dir/manifests/``.
    run_dir = _make_run_dir(tmp_path, variant_count=1)
    # Drop the run-dir manifests/ so it can't accidentally satisfy the
    # check; the variant manifest only lives under shared_fs.
    for entry in (run_dir / "manifests").iterdir():
        entry.unlink()
    shared_fs = tmp_path / "shared"
    (shared_fs / "manifests").mkdir(parents=True)
    (shared_fs / "manifests" / "variant_0.json").write_text("{}\n")
    artifacts = RunArtifacts.from_dir(run_dir, shared_fs=shared_fs)
    assert artifacts.manifests_dir == shared_fs / "manifests"
    result = check_manifest_count_matches(artifacts)
    assert result.passed, result.detail


# ---------------------------------------------------------------------------
# Invariant 4: build-failure count
# ---------------------------------------------------------------------------


def test_build_failures_unexpected(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=1)
    result = check_build_failures(
        RunArtifacts(run_dir=run_dir), expected_count=0
    )
    assert not result.passed
    assert "1 entries" in result.detail


def test_build_failures_too_few(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=1)
    result = check_build_failures(
        RunArtifacts(run_dir=run_dir), expected_count=3
    )
    assert not result.passed


def test_build_failures_match(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=2)
    result = check_build_failures(
        RunArtifacts(run_dir=run_dir), expected_count=2
    )
    assert result.passed


def test_build_failures_missing_dir_passes_when_zero(
    tmp_path: Path,
) -> None:
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "build-failures").rmdir()
    result = check_build_failures(
        RunArtifacts(run_dir=run_dir), expected_count=0
    )
    assert result.passed


def test_build_failures_negative_expected_raises(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path)
    with pytest.raises(ValueError):
        check_build_failures(
            RunArtifacts(run_dir=run_dir), expected_count=-1
        )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "compiler_suit_runner.tests.slurm.invariants",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exit_zero_on_clean_run(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, variant_count=1)
    result = _run_cli(str(run_dir))
    assert result.returncode == 0, result.stderr
    assert "[PASS]" in result.stdout
    assert "[FAIL]" not in result.stdout


def test_cli_exit_one_on_failure(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=1)
    result = _run_cli(str(run_dir))
    assert result.returncode == 1
    assert "[FAIL]" in result.stdout


def test_cli_accepts_expected_failure_count(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=2)
    result = _run_cli(str(run_dir), "2")
    assert result.returncode == 0, result.stdout + result.stderr


def test_cli_missing_dir_returns_2(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    result = _run_cli(str(bogus))
    assert result.returncode == 2
    assert "does not exist" in result.stderr


def test_cli_no_args_returns_2() -> None:
    result = _run_cli()
    assert result.returncode == 2
    assert "usage" in result.stderr


# ---------------------------------------------------------------------------
# RunArtifacts.from_dir
# ---------------------------------------------------------------------------


def test_from_dir_parses_run_id_and_started_at(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_20260508_120300"
    run_dir.mkdir()
    artifacts = RunArtifacts.from_dir(run_dir)
    assert artifacts.run_id == "run_20260508_120300"
    assert artifacts.started_at == datetime(2026, 5, 8, 12, 3, 0)


def test_from_dir_keeps_unparseable_name(tmp_path: Path) -> None:
    run_dir = tmp_path / "weird_name"
    run_dir.mkdir()
    artifacts = RunArtifacts.from_dir(run_dir)
    assert artifacts.run_id == "weird_name"
    assert artifacts.started_at is None


# ---------------------------------------------------------------------------
# Mock ClusterProbe and helpers
# ---------------------------------------------------------------------------


@dataclass
class _StubCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess used by the UID probe."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _StubProbe:
    """Hand-rolled mock matching the parts of ClusterProbe consumed by the
    cluster invariants. Each attribute represents one method's return
    value; the methods are implemented inline so the tests can wire any
    combination of canned rows / failures.
    """

    reachable: bool = True
    squeue_rows: list[object] = field(default_factory=list)
    squeue_polls: list[list[object]] | None = None
    podman_rows_by_worker: dict[str, list[PodmanRow]] = field(
        default_factory=dict,
    )
    listener_rows_by_worker: dict[str, list[ListenerRow]] = field(
        default_factory=dict,
    )
    process_rows_by_worker: dict[str, list[ProcessRow]] = field(
        default_factory=dict,
    )
    gateway_uid: int | None = 1000
    # Track squeue_me() call count so tests can poke the polling loop.
    _squeue_calls: int = 0

    def is_reachable(self, *, timeout: float = 5.0) -> bool:
        return self.reachable

    def squeue_me(self) -> list[object]:
        if self.squeue_polls is not None:
            idx = min(self._squeue_calls, len(self.squeue_polls) - 1)
            self._squeue_calls += 1
            return self.squeue_polls[idx]
        self._squeue_calls += 1
        return list(self.squeue_rows)

    def podman_ps(self, worker: str) -> list[PodmanRow]:
        return list(self.podman_rows_by_worker.get(worker, ()))

    def port_listeners(
        self, worker: str, ports: list[int],
    ) -> list[ListenerRow]:
        rows = self.listener_rows_by_worker.get(worker, ())
        port_set = {int(p) for p in ports}
        return [r for r in rows if r.local_port in port_set]

    def processes_by_pattern(
        self, worker: str, pattern: str,
    ) -> list[ProcessRow]:
        # The real probe filters by regex server-side / client-side; for
        # the mock the caller already pre-filters via the dict.
        return list(self.process_rows_by_worker.get(worker, ()))

    def gateway_ssh(
        self, cmd: str, *, timeout: float | None = None,
    ) -> _StubCompletedProcess:
        if cmd != "id -u":
            return _StubCompletedProcess(returncode=2, stdout="", stderr="")
        if self.gateway_uid is None:
            return _StubCompletedProcess(returncode=1, stdout="", stderr="")
        return _StubCompletedProcess(
            returncode=0, stdout=f"{self.gateway_uid}\n", stderr="",
        )


def _artifacts_with_started_at(
    tmp_path: Path,
    started_at: datetime | None = None,
    run_id: str = "run_20260508_120300",
) -> RunArtifacts:
    """Build an empty run_dir under ``tmp_path`` whose RunArtifacts
    carries the given ``started_at`` / ``run_id`` values."""
    run_dir = tmp_path / run_id
    run_dir.mkdir(exist_ok=True)
    if started_at is None:
        started_at = datetime(2026, 5, 8, 12, 3, 0)
    return RunArtifacts(
        run_dir=run_dir, run_id=run_id, started_at=started_at,
    )


def _podman_row(
    *,
    name: str = "k8s_secondary",
    cid: str = "abc1234567890",
    state: str = "running",
    started_at: str = "2026-05-08T12:04:00Z",
    labels: dict[str, str] | None = None,
) -> PodmanRow:
    return PodmanRow(
        id=cid,
        name=name,
        image="img",
        state=state,
        started_at=started_at,
        labels=labels or {},
        raw={},
    )


def _listener_row(
    *,
    port: int = 5050,
    pid: int = 12345,
    process: str | None = "peer_push",
    uid: int | None = 1000,
) -> ListenerRow:
    return ListenerRow(
        proto="tcp",
        local_address="0.0.0.0",
        local_port=port,
        pid=pid,
        process=process,
        uid=uid,
    )


def _process_row(
    *,
    pid: int = 67094,
    ppid: int = 1,
    user: str = "sirati",
    etime: str = "00:42",
    cmd: str = "/usr/bin/harmonia-cache --listen 0.0.0.0:5000",
) -> ProcessRow:
    return ProcessRow(
        pid=pid, ppid=ppid, user=user, etime=etime, cmd=cmd,
    )


# ---------------------------------------------------------------------------
# Invariant 5: leaked containers
# ---------------------------------------------------------------------------


def test_check_no_leaked_containers_pass_when_empty(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(podman_rows_by_worker={"slurm-worker1": []})
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed
    assert result.status == "pass"


def test_check_no_leaked_containers_label_match(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    leaked = _podman_row(
        name="rando", labels={"run_id": artifacts.run_id},
    )
    probe = _StubProbe(podman_rows_by_worker={"slurm-worker1": [leaked]})
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert result.status == "fail"
    assert "1 leaked container" in result.detail
    assert leaked in result.rows


def test_check_no_leaked_containers_name_substring(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    leaked = _podman_row(name=f"asm-secondary-{artifacts.run_id}-0")
    probe = _StubProbe(podman_rows_by_worker={"slurm-worker1": [leaked]})
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert leaked in result.rows


def test_check_no_leaked_containers_started_at_window(tmp_path: Path) -> None:
    # The container has neither label nor name match; the started_at
    # falls inside the run window so it counts as a leak.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime(2026, 5, 8, 12, 0, 0),
    )
    leaked = _podman_row(
        name="some-other-container",
        started_at="2026-05-08T12:05:00Z",
    )
    probe = _StubProbe(podman_rows_by_worker={"slurm-worker1": [leaked]})
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert leaked in result.rows


def test_check_no_leaked_containers_started_at_before(tmp_path: Path) -> None:
    # Container started BEFORE the run -> not a leak from this run.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime(2026, 5, 8, 12, 0, 0),
    )
    pre_existing = _podman_row(
        name="pre-existing",
        started_at="2026-05-08T11:55:00Z",
    )
    probe = _StubProbe(
        podman_rows_by_worker={"slurm-worker1": [pre_existing]},
    )
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed


def test_check_no_leaked_containers_unreachable(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(reachable=False)
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert result.status == "skip"
    assert "live cluster unavailable" in result.detail


def test_check_no_leaked_containers_multi_worker(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    leaked = _podman_row(labels={"run_id": artifacts.run_id})
    probe = _StubProbe(
        podman_rows_by_worker={
            "slurm-worker1": [],
            "slurm-worker2": [leaked],
            "slurm-worker3": [],
        },
    )
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1", "slurm-worker2", "slurm-worker3"],
    )
    assert not result.passed
    assert "slurm-worker2" in result.detail


def test_check_no_leaked_containers_probe_failure_surfaces_as_fail(
    tmp_path: Path,
) -> None:
    """A WorkerProbeError on any worker must turn the leak check into
    a hard failure rather than a silent pass — the user explicitly
    asked for "after each test no slurm jobs remain, after that
    succeeded confirm that on the slurm worker nodes no processes are
    leaked", which is impossible to verify when the SSH probe itself
    is broken (the prior empty-list-on-rc!=0 behaviour silently
    declared the worker clean)."""
    from compiler_suit_runner.tests.slurm.cluster_probe import (
        WorkerProbeError,
    )

    class _ErrorProbe(_StubProbe):
        def podman_ps(self, worker: str) -> list[PodmanRow]:
            raise WorkerProbeError(
                worker, "podman ps -a", 255, "Permission denied",
            )

    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _ErrorProbe(reachable=True)
    result = check_no_leaked_containers(
        artifacts, probe, ["slurm-worker1", "slurm-worker2"],
    )
    assert not result.passed
    assert result.status == "fail"
    assert "podman_ps probe failed" in result.detail
    assert "slurm-worker1" in result.detail
    assert "slurm-worker2" in result.detail


# ---------------------------------------------------------------------------
# Invariant 6: leaked listener ports
# ---------------------------------------------------------------------------


def test_check_no_leaked_listener_ports_pass(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(
        listener_rows_by_worker={"slurm-worker1": []},
        gateway_uid=1000,
    )
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed
    assert result.status == "pass"


def test_check_no_leaked_listener_ports_uid_match(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    leaked = _listener_row(port=5050, uid=1000, process="peer_push")
    probe = _StubProbe(
        listener_rows_by_worker={"slurm-worker1": [leaked]},
        gateway_uid=1000,
    )
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert "1 leaked listener" in result.detail
    assert "5050" in result.detail


def test_check_no_leaked_listener_ports_other_uid_skipped(
    tmp_path: Path,
) -> None:
    # A listener bound by a different UID is NOT this run's leak.
    artifacts = _artifacts_with_started_at(tmp_path)
    other = _listener_row(port=5050, uid=99, process="someone-else")
    probe = _StubProbe(
        listener_rows_by_worker={"slurm-worker1": [other]},
        gateway_uid=1000,
    )
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed


def test_check_no_leaked_listener_ports_no_uid_skipped(
    tmp_path: Path,
) -> None:
    # Kernel-style listener with no UID surfaced -> cannot attribute,
    # skip rather than blame.
    artifacts = _artifacts_with_started_at(tmp_path)
    no_uid = _listener_row(port=5050, uid=None, process=None, pid=None)
    probe = _StubProbe(
        listener_rows_by_worker={"slurm-worker1": [no_uid]},
        gateway_uid=1000,
    )
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed


def test_check_no_leaked_listener_ports_filters_by_port(tmp_path: Path) -> None:
    # Only the requested ports are inspected; the mock applies the same
    # client-side filter as the real probe.
    artifacts = _artifacts_with_started_at(tmp_path)
    other_port = _listener_row(port=22, uid=1000)
    leaked = _listener_row(port=5000, uid=1000)
    probe = _StubProbe(
        listener_rows_by_worker={"slurm-worker1": [other_port, leaked]},
        gateway_uid=1000,
    )
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"], ports=[5000, 5050],
    )
    assert not result.passed
    assert "5000" in result.detail
    assert "22" not in result.detail


def test_check_no_leaked_listener_ports_unreachable(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(reachable=False)
    result = check_no_leaked_listener_ports(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.status == "skip"
    assert not result.passed


# ---------------------------------------------------------------------------
# Invariant 7: leaked processes
# ---------------------------------------------------------------------------


def test_check_no_leaked_processes_pass(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(process_rows_by_worker={"slurm-worker1": []})
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed
    assert result.status == "pass"


def test_check_no_leaked_processes_orphaned_in_window(tmp_path: Path) -> None:
    # Run started ~2 minutes ago; process etime 00:42 is inside that
    # window AND ppid==1 -> leaked orphan.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime.now() - timedelta(minutes=2),
    )
    leaked = _process_row(
        ppid=1, etime="00:42", cmd="harmonia-cache --listen 0.0.0.0:5000",
    )
    probe = _StubProbe(
        process_rows_by_worker={"slurm-worker1": [leaked]},
    )
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert "1 leaked PPID=1" in result.detail


def test_check_no_leaked_processes_ppid_not_one_skipped(
    tmp_path: Path,
) -> None:
    # ppid != 1 means it has a live parent; we only flag orphans.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime.now() - timedelta(minutes=2),
    )
    not_orphan = _process_row(
        ppid=42, etime="00:30", cmd="harmonia-cache",
    )
    probe = _StubProbe(
        process_rows_by_worker={"slurm-worker1": [not_orphan]},
    )
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed


def test_check_no_leaked_processes_outside_window_skipped(
    tmp_path: Path,
) -> None:
    # Process started 1h ago but the run started 2min ago -> it's
    # leftover from another run, not ours.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime.now() - timedelta(minutes=2),
    )
    older = _process_row(
        ppid=1, etime="01:00:00", cmd="harmonia-cache",
    )
    probe = _StubProbe(
        process_rows_by_worker={"slurm-worker1": [older]},
    )
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.passed


def test_check_no_leaked_processes_d_etime_format(tmp_path: Path) -> None:
    # Process etime in D-HH:MM:SS form is parsed correctly.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime.now() - timedelta(minutes=5),
    )
    long_runner = _process_row(
        ppid=1, etime="2-03:04:05", cmd="harmonia-cache",
    )
    probe = _StubProbe(
        process_rows_by_worker={"slurm-worker1": [long_runner]},
    )
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    # 2 days etime > 5 minutes window -> not from this run.
    assert result.passed


def test_check_no_leaked_processes_unparseable_etime_in_window(
    tmp_path: Path,
) -> None:
    # Conservative: an unparseable etime is treated as in-window so we
    # don't mask a real leak via parser fragility.
    artifacts = _artifacts_with_started_at(
        tmp_path, started_at=datetime.now() - timedelta(minutes=2),
    )
    weird = _process_row(
        ppid=1, etime="???", cmd="compiler_suit_runner build",
    )
    probe = _StubProbe(
        process_rows_by_worker={"slurm-worker1": [weird]},
    )
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed


def test_check_no_leaked_processes_missing_started_at(tmp_path: Path) -> None:
    # No started_at -> cannot scope a window; the check soft-skips with
    # passed=False and status="skip" so the caller knows.
    run_dir = tmp_path / "run_no_ts"
    run_dir.mkdir()
    artifacts = RunArtifacts(run_dir=run_dir, run_id="run_no_ts")
    probe = _StubProbe()
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert not result.passed
    assert result.status == "skip"
    assert "started_at" in result.detail


def test_check_no_leaked_processes_unreachable(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(reachable=False)
    result = check_no_leaked_processes(
        artifacts, probe, ["slurm-worker1"],
    )
    assert result.status == "skip"
    assert not result.passed


# ---------------------------------------------------------------------------
# wait_squeue_empty
# ---------------------------------------------------------------------------


def test_wait_squeue_empty_returns_true_when_empty() -> None:
    probe = _StubProbe(squeue_rows=[])
    assert wait_squeue_empty(probe, timeout_s=1.0) is True


def test_wait_squeue_empty_returns_false_on_timeout() -> None:
    probe = _StubProbe(squeue_rows=["job1"])  # never empties
    assert wait_squeue_empty(
        probe, timeout_s=0.05, poll_interval_s=0.01,
    ) is False


def test_wait_squeue_empty_polls_until_drained() -> None:
    # First poll returns one job, second poll empty -> success.
    probe = _StubProbe(squeue_polls=[["jobA"], []])
    assert wait_squeue_empty(
        probe, timeout_s=2.0, poll_interval_s=0.01,
    ) is True
    assert probe._squeue_calls == 2


# ---------------------------------------------------------------------------
# run_cluster_invariants composition
# ---------------------------------------------------------------------------


def test_run_cluster_invariants_unreachable_returns_three_skips(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(reachable=False)
    results = run_cluster_invariants(
        artifacts, probe, ["slurm-worker1"],
    )
    assert len(results) == 3
    assert all(r.status == "skip" for r in results)
    assert [r.name for r in results] == [
        "no_leaked_containers",
        "no_leaked_listener_ports",
        "no_leaked_processes",
    ]


def test_run_cluster_invariants_still_running(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(squeue_rows=["jobX"])  # never drains
    results = run_cluster_invariants(
        artifacts, probe, ["slurm-worker1"], squeue_timeout_s=0.05,
    )
    assert len(results) == 3
    assert all(r.status == "still-running" for r in results)
    assert all(not r.passed for r in results)


def test_run_cluster_invariants_full_pass(tmp_path: Path) -> None:
    artifacts = _artifacts_with_started_at(tmp_path)
    probe = _StubProbe(
        squeue_rows=[],
        podman_rows_by_worker={"slurm-worker1": []},
        listener_rows_by_worker={"slurm-worker1": []},
        process_rows_by_worker={"slurm-worker1": []},
        gateway_uid=1000,
    )
    results = run_cluster_invariants(
        artifacts, probe, ["slurm-worker1"],
    )
    assert len(results) == 3
    assert all(r.passed for r in results), [str(r) for r in results]


# ---------------------------------------------------------------------------
# run_all_invariants composition
# ---------------------------------------------------------------------------


def test_run_all_invariants_returns_seven_results(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, variant_count=1)
    # Re-anchor the artifacts to use from_dir-style timestamps so check 7
    # has a window. _make_run_dir uses ``run_2026<jobid>``; skip parsing
    # and inject started_at directly.
    artifacts = RunArtifacts(
        run_dir=run_dir,
        run_id=run_dir.name,
        started_at=datetime(2026, 5, 8, 12, 0, 0),
    )
    probe = _StubProbe(
        squeue_rows=[],
        podman_rows_by_worker={"slurm-worker1": []},
        listener_rows_by_worker={"slurm-worker1": []},
        process_rows_by_worker={"slurm-worker1": []},
        gateway_uid=1000,
    )
    results = run_all_invariants(
        artifacts, probe, ["slurm-worker1"], expected_failure_count=0,
    )
    assert len(results) == 7
    names = [r.name for r in results]
    assert names == [
        "clean_exit",
        "no_bind_errors",
        "manifest_count_matches",
        "build_failures",
        "no_leaked_containers",
        "no_leaked_listener_ports",
        "no_leaked_processes",
    ]


# ---------------------------------------------------------------------------
# CLI --cluster
# ---------------------------------------------------------------------------


def test_cli_cluster_requires_workers(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, variant_count=1)
    # --cluster without --workers should error out.
    result = _run_cli(str(run_dir), "--cluster")
    assert result.returncode == 2
    assert "--workers" in result.stderr


def test_cli_argparse_accepts_expected_failures_flag(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, build_failure_count=2)
    result = _run_cli(str(run_dir), "--expected-failures", "2")
    assert result.returncode == 0, result.stdout + result.stderr

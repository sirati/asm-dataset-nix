"""Tests for the file-only SLURM run-invariant checks.

Synthetic on-disk fixtures only — never touches a real run-log tree.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    check_build_failures,
    check_clean_exit,
    check_manifest_count_matches,
    check_no_bind_errors,
    run_file_invariants,
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

"""Unit tests for :mod:`compiler_suit_runner.tests.slurm.run_helpers`.

These tests mock ``subprocess.run`` outright — they never invoke the
real CLI. End-to-end coverage of the wrapper-against-real-cluster
path lives in T1 (``test_t01_clean_tiny.py``, owned by Batch B1).
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any
from unittest import mock

import pytest

from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_GATEWAY_ROOT,
    SLURM_TEST_ENV_GATEWAY_URL,
    SLURM_TEST_ENV_LOG_ROOT,
    RunInvocation,
    clear_incremental_cache,
    default_invocation_for_smoke,
    iter_log_files,
    parse_run_id,
    resolve_log_dir,
    run_compiler_suit,
    wait_for_log_dir,
)


# ---------------------------------------------------------------------------
# parse_run_id
# ---------------------------------------------------------------------------


def test_parse_run_id_extracts_dynamic_runner_format():
    sample = (
        "2026-05-08 09:21:31,123 INFO dynamic_runner.packaging.pipeline:"
        " Run ID: run_20260508_092131\n"
        "more log lines\n"
    )
    assert parse_run_id(sample) == "run_20260508_092131"


def test_parse_run_id_returns_last_match_when_multiple():
    sample = (
        "Run ID: run_20260508_092131\n"
        "framework retried\n"
        "Run ID: run_20260508_092201\n"
    )
    assert parse_run_id(sample) == "run_20260508_092201"


def test_parse_run_id_returns_none_when_absent():
    assert parse_run_id("nothing matched here") is None
    assert parse_run_id("") is None


def test_parse_run_id_ignores_non_matching_run_prefix():
    # Bare ``run_``-without-timestamp must NOT match (would otherwise
    # collide with framework's per-task ``run_<n>`` worker tags).
    assert parse_run_id("Run ID: run_5") is None
    assert parse_run_id("run_20260508_092131") is None  # missing prefix


# ---------------------------------------------------------------------------
# resolve_log_dir / wait_for_log_dir
# ---------------------------------------------------------------------------


def test_resolve_log_dir_default_root():
    out = resolve_log_dir("run_20260508_092131")
    assert out == SLURM_TEST_ENV_LOG_ROOT / "run_20260508_092131"


def test_resolve_log_dir_custom_root(tmp_path: pathlib.Path):
    out = resolve_log_dir("run_X", log_root=tmp_path)
    assert out == tmp_path / "run_X"


def test_wait_for_log_dir_returns_when_present(tmp_path: pathlib.Path):
    target = tmp_path / "run_present"
    target.mkdir()
    got = wait_for_log_dir(
        "run_present", log_root=tmp_path, timeout_s=0.5, poll_interval_s=0.1
    )
    assert got == target


def test_wait_for_log_dir_returns_none_on_timeout(tmp_path: pathlib.Path):
    got = wait_for_log_dir(
        "run_absent", log_root=tmp_path, timeout_s=0.2, poll_interval_s=0.05,
    )
    assert got is None


# ---------------------------------------------------------------------------
# RunInvocation.to_argv
# ---------------------------------------------------------------------------


def test_to_argv_minimal_invocation(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path / "shared")
    argv = inv.to_argv(python="/usr/bin/python3")
    # First three tokens are fixed; we test them explicitly.
    assert argv[0] == "/usr/bin/python3"
    assert argv[1:4] == ["-m", "compiler_suit_runner", "submit"]
    # Required flags always emitted.
    assert "--shared-fs" in argv
    assert str(tmp_path / "shared") in argv
    assert "--multi-computer" in argv
    assert argv[argv.index("--multi-computer") + 1] == "slurm"
    # Defaults should embed the gateway constants so the test-env
    # routing is the path-of-least-resistance for callers.
    assert SLURM_TEST_ENV_GATEWAY_URL in argv
    assert str(SLURM_TEST_ENV_GATEWAY_ROOT) in argv


def test_to_argv_packages_and_archs(tmp_path: pathlib.Path):
    inv = RunInvocation(
        shared_fs=tmp_path,
        packages=("hello", "coreutils"),
        archs=("x86_64",),
    )
    argv = inv.to_argv()
    # nargs="+" emits multiple tokens after the flag.
    pkg_idx = argv.index("--packages")
    assert argv[pkg_idx + 1] == "hello"
    assert argv[pkg_idx + 2] == "coreutils"
    arch_idx = argv.index("--archs")
    assert argv[arch_idx + 1] == "x86_64"


def test_to_argv_optional_flags_only_when_set(tmp_path: pathlib.Path):
    inv = RunInvocation(
        shared_fs=tmp_path,
        gateway=None,
        slurm_root_folder=None,
    )
    argv = inv.to_argv()
    assert "--gateway" not in argv
    assert "--slurm-root-folder" not in argv


def test_to_argv_no_cache_flag(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path, no_cache=True)
    assert "--no-cache" in inv.to_argv()
    inv2 = RunInvocation(shared_fs=tmp_path, no_cache=False)
    assert "--no-cache" not in inv2.to_argv()


# ---------------------------------------------------------------------------
# default_invocation_for_smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("workload", "expected_sample", "expected_max"),
    [
        ("tiny", 1, 1),
        ("medium", 2, 10),
        ("large", 4, 50),
    ],
)
def test_default_invocation_for_smoke_workload_sizing(
    workload: str, expected_sample: int, expected_max: int,
):
    inv = default_invocation_for_smoke(jobs=2, workload=workload)  # type: ignore[arg-type]
    assert inv.jobs == 2
    assert inv.packages == ("hello",)
    assert inv.variant_sample == expected_sample
    assert inv.max_variants == expected_max
    # Gateway / slurm-root must default to the test-env paths so a
    # caller can simply pass jobs+workload and have it route locally.
    assert inv.gateway == SLURM_TEST_ENV_GATEWAY_URL
    assert inv.slurm_root_folder == SLURM_TEST_ENV_GATEWAY_ROOT


def test_default_invocation_for_smoke_argv_contains_workload_knobs(
    tmp_path: pathlib.Path,
):
    inv = default_invocation_for_smoke(
        jobs=1, workload="medium", shared_fs=tmp_path / "s",
    )
    argv = inv.to_argv()
    assert "--variant-sample" in argv
    assert argv[argv.index("--variant-sample") + 1] == "2"
    assert "--max-variants" in argv
    assert argv[argv.index("--max-variants") + 1] == "10"
    assert "--packages" in argv
    assert argv[argv.index("--packages") + 1] == "hello"
    # jobs flows through as "--jobs 1".
    assert "--jobs" in argv
    assert argv[argv.index("--jobs") + 1] == "1"


def test_default_invocation_for_smoke_unknown_workload_raises():
    with pytest.raises(ValueError, match="unknown workload"):
        default_invocation_for_smoke(jobs=1, workload="huge")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_compiler_suit (subprocess mocked)
# ---------------------------------------------------------------------------


def _fake_completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_run_compiler_suit_invokes_with_constructed_argv(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path / "s", packages=("hello",), jobs=1)
    expected_argv_prefix = inv.to_argv()
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _fake_completed(stderr="Run ID: run_20260508_092131\n")

    with mock.patch("subprocess.run", side_effect=fake_run):
        res = run_compiler_suit(inv)

    assert captured["argv"] == expected_argv_prefix
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["check"] is False
    assert res.exit_code == 0
    assert res.run_id == "run_20260508_092131"
    assert res.log_dir == SLURM_TEST_ENV_LOG_ROOT / "run_20260508_092131"
    assert res.argv == tuple(expected_argv_prefix)


@pytest.mark.parametrize(
    ("workload", "expected_max"),
    [("tiny", "1"), ("medium", "10"), ("large", "50")],
)
def test_run_compiler_suit_with_smoke_defaults_emits_workload_flags(
    tmp_path: pathlib.Path, workload: str, expected_max: str,
):
    inv = default_invocation_for_smoke(
        jobs=1, workload=workload, shared_fs=tmp_path,  # type: ignore[arg-type]
    )
    captured_argv: list[str] = []

    def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        captured_argv.extend(argv)
        return _fake_completed()

    with mock.patch("subprocess.run", side_effect=fake_run):
        run_compiler_suit(inv)

    assert "--max-variants" in captured_argv
    idx = captured_argv.index("--max-variants")
    assert captured_argv[idx + 1] == expected_max
    assert "submit" in captured_argv  # forwarded subcommand
    assert "--multi-computer" in captured_argv
    assert captured_argv[captured_argv.index("--multi-computer") + 1] == "slurm"


def test_run_compiler_suit_extracts_run_id_from_stdout_fallback(
    tmp_path: pathlib.Path,
):
    inv = RunInvocation(shared_fs=tmp_path)
    with mock.patch(
        "subprocess.run",
        return_value=_fake_completed(stdout="Run ID: run_20260101_000000\n"),
    ):
        res = run_compiler_suit(inv)
    assert res.run_id == "run_20260101_000000"


def test_run_compiler_suit_run_id_none_when_absent(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path)
    with mock.patch(
        "subprocess.run",
        return_value=_fake_completed(stdout="no run id here", stderr=""),
    ):
        res = run_compiler_suit(inv)
    assert res.run_id is None
    assert res.log_dir is None


def test_run_compiler_suit_propagates_nonzero_exit(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path)
    with mock.patch(
        "subprocess.run",
        return_value=_fake_completed(returncode=2, stderr="boom\n"),
    ):
        res = run_compiler_suit(inv)
    assert res.exit_code == 2
    assert res.stderr == "boom\n"


def test_run_compiler_suit_handles_timeout(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path)
    err = subprocess.TimeoutExpired(
        cmd=["x"], timeout=1.0, output=b"partial-out", stderr=b"Run ID: run_20260101_000001\n",
    )
    with mock.patch("subprocess.run", side_effect=err):
        res = run_compiler_suit(inv, timeout_s=1.0)
    assert res.exit_code == -1
    assert res.run_id == "run_20260101_000001"
    assert "partial-out" in res.stdout


def test_run_compiler_suit_merges_env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    inv = RunInvocation(shared_fs=tmp_path)
    monkeypatch.setenv("PRESERVED_VAR", "from-os-environ")
    captured_env: dict[str, str] = {}

    def fake_run(_argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(kwargs.get("env") or {})
        return _fake_completed()

    with mock.patch("subprocess.run", side_effect=fake_run):
        run_compiler_suit(inv, env={"EXTRA_VAR": "yes"})

    # Caller-supplied env layered on top of os.environ.
    assert captured_env.get("PRESERVED_VAR") == "from-os-environ"
    assert captured_env.get("EXTRA_VAR") == "yes"


def test_run_compiler_suit_workdir_passed_as_cwd(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path / "s", workdir=tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(_argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return _fake_completed()

    with mock.patch("subprocess.run", side_effect=fake_run):
        run_compiler_suit(inv)

    assert captured["cwd"] == str(tmp_path)


def test_run_compiler_suit_no_workdir_keeps_cwd_none(tmp_path: pathlib.Path):
    inv = RunInvocation(shared_fs=tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(_argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return _fake_completed()

    with mock.patch("subprocess.run", side_effect=fake_run):
        run_compiler_suit(inv)

    assert captured["cwd"] is None


# ---------------------------------------------------------------------------
# clear_incremental_cache
# ---------------------------------------------------------------------------


def test_clear_incremental_cache_missing_root_returns_zero(tmp_path: pathlib.Path):
    missing = tmp_path / "does-not-exist"
    assert clear_incremental_cache(missing) == 0


def test_clear_incremental_cache_removes_entries(tmp_path: pathlib.Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "hash-a").mkdir()
    (cache / "hash-a" / "manifests.tar").write_text("x")
    (cache / "hash-b").mkdir()
    (cache / "stray.txt").write_text("y")

    removed = clear_incremental_cache(cache)
    assert removed == 3
    # Cache root itself remains; only its children are gone.
    assert cache.is_dir()
    assert list(cache.iterdir()) == []


def test_clear_incremental_cache_idempotent(tmp_path: pathlib.Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "h").mkdir()
    assert clear_incremental_cache(cache) == 1
    assert clear_incremental_cache(cache) == 0


# ---------------------------------------------------------------------------
# iter_log_files
# ---------------------------------------------------------------------------


def test_iter_log_files_returns_empty_for_missing_dir(tmp_path: pathlib.Path):
    assert iter_log_files(tmp_path / "nope") == []


def test_iter_log_files_collects_matches_sorted(tmp_path: pathlib.Path):
    log_dir = tmp_path / "run_20260101_000000"
    log_dir.mkdir()
    (log_dir / "slurm_2.out").write_text("")
    (log_dir / "slurm_1.out").write_text("")
    (log_dir / "slurm_1.err").write_text("")
    (log_dir / "ignored.txt").write_text("")

    files = iter_log_files(log_dir)
    names = [p.name for p in files]
    # slurm_*.out comes first (pattern order), each pattern's matches sorted.
    assert names == ["slurm_1.out", "slurm_2.out", "slurm_1.err"]


def test_iter_log_files_uses_glob_on_path(tmp_path: pathlib.Path):
    """Verify ``Path.glob`` is the actual call site (mock-friendly)."""
    log_dir = tmp_path / "run"
    log_dir.mkdir()

    real_glob = pathlib.Path.glob
    glob_calls: list[str] = []

    def spying_glob(self: pathlib.Path, pat: str):
        glob_calls.append(pat)
        return real_glob(self, pat)

    with mock.patch.object(pathlib.Path, "glob", spying_glob):
        iter_log_files(log_dir, patterns=("slurm_*.out", "harmonia-*.log"))

    assert glob_calls == ["slurm_*.out", "harmonia-*.log"]

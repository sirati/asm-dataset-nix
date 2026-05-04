"""Tests for ``compiler_suit_runner.prebuild``.

Hermetic: no real nix build is invoked. The injection seam is the
``run_subprocess`` parameter on :func:`prebuild_drvs`. Concurrency is
exercised with thread-safe counters in the fake runner.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from compiler_suit_runner.prebuild import (
    PrebuildResult,
    prebuild_drvs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drv_token(argv: list[str]) -> str:
    """Extract the ``<drv>^*`` argument from a fake-runner argv."""
    # argv: ["nix", "build", "--no-link", "--print-out-paths", ..., "<drv>^*"]
    return argv[-1]


def _drv_from_token(token: str) -> str:
    """Reverse of :func:`_drv_token` -- strip the trailing ``^*`` glob."""
    assert token.endswith("^*"), token
    return token[:-2]


class ProgrammableRunner:
    """Fake ``run_subprocess`` mapping drv path -> response.

    ``responses`` keys are drv paths (without the ``^*`` suffix); values
    are ``(stdout, stderr, returncode)`` tuples. Calls are recorded in
    ``calls`` (the full argv list) for argv-shape assertions.
    """

    def __init__(
        self,
        responses: dict[str, tuple[bytes, bytes, int]],
        *,
        per_call_delay: float = 0.0,
    ) -> None:
        self.responses = dict(responses)
        self.per_call_delay = per_call_delay
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        # Track concurrency.
        with self._lock:
            self.calls.append(list(argv))
            self.in_flight += 1
            if self.in_flight > self.peak_in_flight:
                self.peak_in_flight = self.in_flight
        try:
            if self.per_call_delay > 0.0:
                time.sleep(self.per_call_delay)
            drv = _drv_from_token(_drv_token(argv))
            if drv not in self.responses:
                return b"", f"no fake for {drv}".encode("utf-8"), 1
            return self.responses[drv]
        finally:
            with self._lock:
                self.in_flight -= 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_succeed():
    drvs = [f"/nix/store/aaaa{i}-pkg.drv" for i in range(5)]
    responses = {
        drv: (f"/nix/store/out-{i}\n".encode("utf-8"), b"", 0)
        for i, drv in enumerate(drvs)
    }
    runner = ProgrammableRunner(responses)

    result = prebuild_drvs(drvs, jobs=4, run_subprocess=runner)

    assert isinstance(result, PrebuildResult)
    assert len(result.succeeded) == 5
    assert result.failed == ()
    succeeded_map = dict(result.succeeded)
    for i, drv in enumerate(drvs):
        assert succeeded_map[drv] == f"/nix/store/out-{i}"

    # Argv shape: every call uses the documented flag set.
    for argv in runner.calls:
        assert argv[0] == "nix"
        assert argv[1] == "build"
        assert "--no-link" in argv
        assert "--print-out-paths" in argv
        assert argv[-1].endswith("^*")


def test_mixed_success_and_failure():
    drvs = [f"/nix/store/mix-{i}.drv" for i in range(4)]
    responses = {
        drvs[0]: (b"/nix/store/o0\n", b"", 0),
        drvs[1]: (b"", b"build of /nix/store/mix-1.drv failed: boom\n", 1),
        drvs[2]: (b"/nix/store/o2\n", b"", 0),
        drvs[3]: (b"", b"missing dependency widgets-1.0\n", 1),
    }
    runner = ProgrammableRunner(responses)

    result = prebuild_drvs(drvs, jobs=2, run_subprocess=runner)

    succeeded_map = dict(result.succeeded)
    failed_map = dict(result.failed)

    assert set(succeeded_map.keys()) == {drvs[0], drvs[2]}
    assert succeeded_map[drvs[0]] == "/nix/store/o0"
    assert succeeded_map[drvs[2]] == "/nix/store/o2"

    assert set(failed_map.keys()) == {drvs[1], drvs[3]}
    assert "boom" in failed_map[drvs[1]]
    assert "widgets" in failed_map[drvs[3]]


def test_all_fail():
    drvs = [f"/nix/store/bad-{i}.drv" for i in range(3)]
    responses = {
        drv: (b"", f"failure {i}\n".encode("utf-8"), 1)
        for i, drv in enumerate(drvs)
    }
    runner = ProgrammableRunner(responses)

    result = prebuild_drvs(drvs, jobs=4, run_subprocess=runner)

    assert result.succeeded == ()
    failed_map = dict(result.failed)
    assert set(failed_map.keys()) == set(drvs)
    for i, drv in enumerate(drvs):
        assert f"failure {i}" in failed_map[drv]


def test_empty_input_is_instant():
    runner = ProgrammableRunner({})
    start = time.monotonic()
    result = prebuild_drvs([], jobs=8, run_subprocess=runner)
    elapsed = time.monotonic() - start

    assert result.succeeded == ()
    assert result.failed == ()
    assert result.total_duration_seconds == 0.0
    # Empty input -> no thread pool spin-up; should be nearly instant.
    assert elapsed < 1.0
    assert runner.calls == []


def test_failure_with_empty_stderr_still_records_excerpt():
    """rc!=0 with no stderr should still produce a non-empty error string
    so the caller can distinguish "this drv failed" from "this drv was
    never attempted"."""
    drv = "/nix/store/silentfail.drv"
    runner = ProgrammableRunner({drv: (b"", b"", 1)})

    result = prebuild_drvs([drv], jobs=1, run_subprocess=runner)

    assert result.succeeded == ()
    assert len(result.failed) == 1
    err_drv, err_excerpt = result.failed[0]
    assert err_drv == drv
    assert err_excerpt  # non-empty
    assert "rc=1" in err_excerpt


def test_runner_exception_folded_into_failure():
    """A run_subprocess that raises must become a failed entry, not bubble up."""
    drv = "/nix/store/explodes.drv"

    def runner(argv):
        raise RuntimeError("subprocess module exploded")

    result = prebuild_drvs([drv], jobs=1, run_subprocess=runner)

    assert result.succeeded == ()
    assert len(result.failed) == 1
    err_drv, err_excerpt = result.failed[0]
    assert err_drv == drv
    assert "exploded" in err_excerpt


def test_multi_output_drv_picks_last_path():
    """nix build --print-out-paths prints one path per output for a
    multi-output drv (e.g. ``out``, ``dev``). The recorded out_path is
    the last non-blank line."""
    drv = "/nix/store/multi.drv"
    multi_stdout = (
        b"/nix/store/aaa-multi-bin\n"
        b"/nix/store/bbb-multi-dev\n"
        b"/nix/store/ccc-multi-out\n"
    )
    runner = ProgrammableRunner({drv: (multi_stdout, b"", 0)})

    result = prebuild_drvs([drv], jobs=1, run_subprocess=runner)

    assert result.failed == ()
    assert len(result.succeeded) == 1
    rec_drv, out_path = result.succeeded[0]
    assert rec_drv == drv
    assert out_path == "/nix/store/ccc-multi-out"


def test_multi_output_with_trailing_blank_lines():
    """Trailing blank lines / whitespace must not mask the real out-path."""
    drv = "/nix/store/blank.drv"
    runner = ProgrammableRunner(
        {drv: (b"/nix/store/path-a\n/nix/store/path-b\n\n  \n", b"", 0)}
    )

    result = prebuild_drvs([drv], jobs=1, run_subprocess=runner)

    assert result.failed == ()
    succeeded_map = dict(result.succeeded)
    assert succeeded_map[drv] == "/nix/store/path-b"


def test_jobs_caps_concurrency():
    """The ``jobs`` parameter must bound parallel invocations.

    We sleep inside each fake call so multiple invocations are in
    flight simultaneously; the runner's thread-safe peak counter must
    not exceed the configured cap.
    """
    drvs = [f"/nix/store/cc-{i}.drv" for i in range(8)]
    responses = {drv: (f"/nix/store/o-{i}\n".encode("utf-8"), b"", 0)
                 for i, drv in enumerate(drvs)}
    runner = ProgrammableRunner(responses, per_call_delay=0.05)

    result = prebuild_drvs(drvs, jobs=2, run_subprocess=runner)

    assert len(result.succeeded) == 8
    assert result.failed == ()
    # Should never exceed the configured cap.
    assert runner.peak_in_flight <= 2
    # And should actually achieve some concurrency (not serialised).
    assert runner.peak_in_flight >= 2


def test_jobs_cap_is_applied_when_drvs_fewer_than_jobs():
    """``max_workers = min(jobs, len(drvs))`` -- we don't spin up more
    threads than there are tasks. With one drv and jobs=8 we should see
    at most 1 in flight."""
    drv = "/nix/store/lonely.drv"
    runner = ProgrammableRunner(
        {drv: (b"/nix/store/o\n", b"", 0)},
        per_call_delay=0.02,
    )

    result = prebuild_drvs([drv], jobs=8, run_subprocess=runner)

    assert len(result.succeeded) == 1
    assert runner.peak_in_flight == 1


def test_log_records_one_info_per_success_one_warning_per_failure(caplog):
    drvs = [
        "/nix/store/log-ok-1.drv",
        "/nix/store/log-bad-1.drv",
        "/nix/store/log-ok-2.drv",
        "/nix/store/log-bad-2.drv",
    ]
    runner = ProgrammableRunner(
        {
            drvs[0]: (b"/nix/store/ok1\n", b"", 0),
            drvs[1]: (b"", b"oops\n", 1),
            drvs[2]: (b"/nix/store/ok2\n", b"", 0),
            drvs[3]: (b"", b"oops2\n", 1),
        }
    )

    log = logging.getLogger("test_prebuild_log")
    log.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="test_prebuild_log"):
        result = prebuild_drvs(drvs, jobs=4, run_subprocess=runner, log=log)

    assert len(result.succeeded) == 2
    assert len(result.failed) == 2

    info_records = [
        r for r in caplog.records
        if r.name == "test_prebuild_log" and r.levelno == logging.INFO
    ]
    warning_records = [
        r for r in caplog.records
        if r.name == "test_prebuild_log" and r.levelno == logging.WARNING
    ]
    assert len(info_records) == 2
    assert len(warning_records) == 2

    # INFO lines mention prebuilt + the drv.
    info_text = "\n".join(r.getMessage() for r in info_records)
    assert "prebuilt" in info_text
    assert "/nix/store/log-ok-1.drv" in info_text
    assert "/nix/store/log-ok-2.drv" in info_text

    # WARNING lines mention failed + the stderr excerpt.
    warning_text = "\n".join(r.getMessage() for r in warning_records)
    assert "failed" in warning_text
    assert "oops" in warning_text


def test_extra_args_are_propagated():
    drv = "/nix/store/extras.drv"
    runner = ProgrammableRunner({drv: (b"/nix/store/o\n", b"", 0)})

    prebuild_drvs(
        [drv],
        jobs=1,
        run_subprocess=runner,
        extra_args=["--option", "build-cores", "1", "-L"],
    )

    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert "--option" in argv
    assert "build-cores" in argv
    assert "1" in argv
    assert "-L" in argv
    # extras must be inserted before the trailing drv^* token.
    assert argv[-1] == drv + "^*"
    # And after the documented flags.
    assert argv.index("--no-link") < argv.index("--option")


def test_drv_glob_suffix_present():
    """Every invocation must use the ``^*`` outputs glob so multi-output
    drvs realise every output (otherwise harmonia can't serve the
    closure for a missing output)."""
    drvs = ["/nix/store/g1.drv", "/nix/store/g2.drv"]
    responses = {d: (b"/nix/store/x\n", b"", 0) for d in drvs}
    runner = ProgrammableRunner(responses)

    prebuild_drvs(drvs, jobs=2, run_subprocess=runner)

    for argv in runner.calls:
        token = argv[-1]
        assert token.endswith("^*")
        assert _drv_from_token(token) in drvs


def test_total_duration_is_set_for_nonempty_input():
    drv = "/nix/store/timing.drv"
    runner = ProgrammableRunner({drv: (b"/nix/store/o\n", b"", 0)})

    result = prebuild_drvs([drv], jobs=1, run_subprocess=runner)

    assert result.total_duration_seconds >= 0.0
    # Sanity: we never sleep, so even a tight machine stays well under a second.
    assert result.total_duration_seconds < 5.0


def test_default_jobs_uses_cpu_count(monkeypatch):
    """When ``jobs`` is None, we fall back to ``os.cpu_count()`` (or 1)."""
    drv = "/nix/store/cpu.drv"
    runner = ProgrammableRunner({drv: (b"/nix/store/o\n", b"", 0)})

    # Force os.cpu_count to a known value just to make the fallback path
    # exercised. We don't assert thread count -- just that the call works
    # with the default and produces the expected result.
    monkeypatch.setattr("os.cpu_count", lambda: 3)

    result = prebuild_drvs([drv], run_subprocess=runner)
    assert len(result.succeeded) == 1


def test_filters_out_blank_or_non_string_drvs():
    """Defensive: ignore empty/non-string entries in the iterable."""
    drvs_iter = ["/nix/store/keep.drv", "", None, "  not-empty-but-no-prefix.drv"]
    responses = {
        "/nix/store/keep.drv": (b"/nix/store/o\n", b"", 0),
        "  not-empty-but-no-prefix.drv": (b"/nix/store/o2\n", b"", 0),
    }
    runner = ProgrammableRunner(responses)

    # ``None`` would trip a type check -- verify we tolerate the iterable.
    result = prebuild_drvs(drvs_iter, jobs=2, run_subprocess=runner)  # type: ignore[arg-type]

    succeeded_map = dict(result.succeeded)
    failed_map = dict(result.failed)
    # The empty string and None must be skipped silently.
    assert "" not in succeeded_map and "" not in failed_map
    # Non-empty strings (even weird ones) are passed through.
    assert "/nix/store/keep.drv" in succeeded_map

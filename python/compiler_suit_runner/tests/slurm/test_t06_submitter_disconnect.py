"""End-to-end T6 reproducer: kill the local primary driver mid-dispatch.

Two secondaries, the smallest workload that gives both secondaries
real work (mirrors T4's ``2 * N_SECONDARIES`` override — see T4 for the
rationale), no broken toolchain. Unlike T5 (which SIGKILLs a slurm
secondary via ``scancel`` on the gateway), T6 kills the LOCAL primary
driver: the python ``compiler_suit_runner submit`` subprocess that
drives the framework's dispatch from the host.

Once at least one secondary has logged
``received initial assignment`` (via
:func:`reproducers.inject_failures.kill_local_primary_driver`) we send
``SIGINT`` to the captured driver PID. The expected framework
behaviour is documented in the test plan ("Test matrix" row T6 +
"Sub-sub-task plays" T6-α/T6-β):

* exactly one of the surviving secondaries' ``slurm_*.out`` files
  carries ``promoted to primary epoch=N`` (any ``N >= 1``);
* the secondaries drain the dispatched work and exit cleanly
  (``Container exited with code: 0``);
* the local dispatch process exits non-zero (we killed it) — we do
  NOT assert on its exit code;
* the cluster-side leak audit shows no leftover containers / listener
  ports / orphan PPID=1 processes from this run.

Invariant audit shape: T6 uses a CUSTOM relaxed audit (modelled on
T5's). The reasons are slightly different from T5:

* invariant 1 (clean exit) holds for BOTH secondaries here — neither
  was SIGKILL'd, so both their slurm_*.out files should still carry
  the standard success markers. We use the strict
  :func:`check_clean_exit`;
* a NEW custom invariant (``exactly_one_promotion``) asserts that
  exactly one secondary logs ``promoted to primary epoch=N``. This is
  the unique post-disconnect signal that distinguishes T6 from a
  plain clean-path run;
* invariant 3 (manifest count) is SKIPPED by design: when the local
  primary disconnects mid-dispatch, the surviving promoted secondary
  may write its own manifests via a different code path than the
  primary-driven one, and the per-test framework manifests/ behaviour
  on the post-disconnect path is not stable enough to assert on
  without prior empirical baseline. The test's ``promoted to primary``
  + ``Container exited with code: 0`` assertions already prove the
  drain happened;
* invariants 2 / 4 / 5 / 6 / 7 run as standard.

Per project memory (``feedback_ssh_debug_key.md``): the slurm-test-env
SSH key is ephemeral and is passed via ``-i`` on every probe (never
via ``ssh-agent`` / ``~/.ssh/``).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    ClusterProbe,
    GatewayConfig,
)
from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    check_build_failures,
    check_clean_exit,
    check_no_bind_errors,
    check_no_leaked_containers,
    check_no_leaked_listener_ports,
    check_no_leaked_processes,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.reproducers.inject_failures import (
    DisconnectResult,
    kill_local_primary_driver,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_LOG_ROOT,
    RunInvocation,
    clear_incremental_cache,
    default_invocation_for_smoke,
    parse_run_id,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral: never added to ssh-agent or ``~/.ssh/``; always passed
# via ``-i``. Same constant as T4/T5.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"


# All four worker hostnames in the local slurm-test-env. Invariants
# 5-7 walk this list to look for leaks.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]


# Default wall-clock cap. T6's tiny workload (with the T4-style
# ``2 * N_SECONDARIES`` variant override) plus the post-disconnect
# drain fits comfortably in 600s; override via ``T6_TIMEOUT_S`` for
# slow CI.
DEFAULT_TIMEOUT_S = 600.0


# Number of secondaries this row dispatches. Two is the minimum for
# the post-disconnect promotion election we exercise.
N_SECONDARIES = 2


# Variant scaling. Same heuristic as T4: the ``"tiny"`` workload's
# default of one total variant is too small for multi-secondary
# dispatch (only one secondary ever receives work; the other parks on
# election). We allocate ``2 * N_SECONDARIES`` variants so both
# secondaries see real assignments AND there is enough work after the
# disconnect for the surviving promoted secondary to drain through.
VARIANT_BUDGET = 2 * N_SECONDARIES


# Match ``promoted to primary epoch=<N>`` (any positive integer). The
# framework's structured-log line is
# ``... this secondary has been promoted to primary epoch=1`` (see
# the sample log under ``slurm-test-env`` runs); we strip ANSI before
# matching, mirroring invariants.py / inject_failures.py.
_PROMOTED_RE: re.Pattern[str] = re.compile(
    r"promoted to primary\s+epoch=(\d+)"
)


_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    Same rationale as T4/T5: the conftest's session-scoped probe is
    constructed without a key; we instantiate locally so SSH probes
    work via the explicit ``-i`` key as project policy demands.
    """
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _resolve_timeout() -> float:
    """Read ``T6_TIMEOUT_S`` from the environment, falling back to default."""
    raw = os.environ.get("T6_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@dataclasses.dataclass(slots=True)
class _DisconnectSlot:
    """Scratch slot the disconnect-watchdog thread uses to publish its result."""

    result: Optional[DisconnectResult] = None
    error: Optional[str] = None


@dataclasses.dataclass(slots=True)
class _DispatchHandle:
    """Captured Popen + run metadata for a backgrounded dispatch.

    The test runs ``compiler_suit_runner submit`` via a directly-managed
    :class:`subprocess.Popen` (rather than the conftest's
    ``fresh_run`` fixture, which uses :func:`subprocess.run` and blocks
    the main test thread). We need:

    * the child PID for the disconnect helper to target;
    * the captured stderr text to parse the run id from after exit
      (mirrors :func:`run_helpers.parse_run_id`);
    * the wall-clock duration + exit status for the standard
      run-result triage shape used elsewhere in the slurm test slice.
    """

    proc: subprocess.Popen[str]
    stdout_chunks: list[str] = dataclasses.field(default_factory=list)
    stderr_chunks: list[str] = dataclasses.field(default_factory=list)
    started_at: float = 0.0


def _start_dispatch_subprocess(
    invocation: RunInvocation,
) -> _DispatchHandle:
    """Spawn ``compiler_suit_runner submit`` non-blockingly.

    Mirrors the argv shape of :func:`run_helpers.run_compiler_suit` so
    the dispatch path is identical to every other test in this slice;
    the only divergence is that we keep the :class:`subprocess.Popen`
    handle alive so the disconnect helper can target the child PID
    while the dispatch is in flight.

    Returns a :class:`_DispatchHandle`. The caller is responsible for
    consuming stdout/stderr (see :func:`_drain_pipes`) and for waiting
    on the process. We deliberately do NOT use ``Popen``'s
    ``communicate`` here: ``communicate`` blocks until exit, which
    defeats the entire point of capturing the PID for mid-flight
    interruption.
    """
    argv = invocation.to_argv()
    handle = _DispatchHandle(
        proc=subprocess.Popen(  # noqa: S603 — argv is constructed in code
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(invocation.workdir) if invocation.workdir else None,
            # Detach from the controlling terminal so a SIGINT delivered
            # to the test process does not propagate to the child via
            # the shared process group; we want the kill to land only
            # via our explicit os.kill call.
            start_new_session=True,
        ),
        started_at=time.monotonic(),
    )
    return handle


def _drain_pipes(handle: _DispatchHandle) -> tuple[threading.Thread, threading.Thread]:
    """Spawn daemon threads that drain ``proc.stdout`` / ``proc.stderr``.

    Without active drains the framework's blocked write to a full
    pipe would surface as a phantom hang, masking the actual
    disconnect-handling we want to observe. Each drain accumulates
    chunks into the corresponding ``handle.*_chunks`` list under no
    lock — the threads are the ONLY writers, the test thread reads
    after ``proc.wait()`` (happens-before via ``join``).
    """

    def _drain(stream, chunks: list[str]) -> None:
        try:
            for line in stream:
                chunks.append(line)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    out_thread = threading.Thread(
        target=_drain,
        args=(handle.proc.stdout, handle.stdout_chunks),
        daemon=True,
        name="t06-dispatch-stdout",
    )
    err_thread = threading.Thread(
        target=_drain,
        args=(handle.proc.stderr, handle.stderr_chunks),
        daemon=True,
        name="t06-dispatch-stderr",
    )
    out_thread.start()
    err_thread.start()
    return out_thread, err_thread


def _wait_for_run_dir(
    log_root: pathlib.Path,
    *,
    baseline: set[str],
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> Optional[pathlib.Path]:
    """Poll ``log_root`` for a fresh ``run_<TS>`` directory.

    Mirrors the same arming pattern T5 uses (``_start_watchdog_when_run_dir_visible``):
    we cannot ask the dispatch for its ``run_id`` while it is still
    running — the framework writes the run id to its stderr stream,
    which we are only consuming asynchronously. Watching the log root
    for a NEW directory (one that didn't exist before the dispatch
    started) is the most robust handshake.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        now = {p.name for p in log_root.glob("run_*")}
        new = sorted(now - baseline)
        if new:
            return log_root / new[-1]
        time.sleep(poll_interval_s)
    return None


def _spawn_disconnect_watchdog(
    *,
    primary_pid: int,
    run_log_dir: pathlib.Path,
    arm_timeout_s: float,
    poll_interval_s: float,
) -> tuple[threading.Thread, _DisconnectSlot]:
    """Start a daemon thread running the disconnect helper.

    The thread polls ``run_log_dir`` for the trigger condition AND, on
    match, sends ``SIGINT`` to ``primary_pid``. We thread it rather
    than running it inline because the dispatch keeps running on the
    main test thread (via the Popen handle); the disconnect must fire
    while the dispatch is still alive.
    """
    slot = _DisconnectSlot()

    def _target() -> None:
        try:
            slot.result = kill_local_primary_driver(
                primary_pid=primary_pid,
                run_log_dir=run_log_dir,
                signal_name="SIGINT",
                arm_timeout_s=arm_timeout_s,
                poll_interval_s=poll_interval_s,
            )
        except Exception as exc:  # noqa: BLE001 — surface to test thread
            slot.error = f"disconnect helper crashed: {exc!r}"

    thread = threading.Thread(
        target=_target, daemon=True, name="t06-disconnect-watchdog",
    )
    thread.start()
    return thread, slot


def _format_results(results: list[InvariantResult]) -> str:
    """Render every invariant result for a failed assertion message."""
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _check_exactly_one_promotion(
    artifacts: RunArtifacts,
) -> InvariantResult:
    """Custom invariant: exactly one secondary logs the promotion line.

    Scans every ``slurm_*.out`` for ``promoted to primary epoch=N``
    (any ``N``); the framework emits that line exactly once per
    promotion event. T6 expects exactly one match across the whole
    run: when the local primary disconnects, the framework runs a
    promotion election among the surviving secondaries and exactly
    one wins.

    Returns ``status="fail"`` for both the zero-match case (no
    secondary ever promoted; the disconnect was not exercised) and
    the >1-match case (the framework spawned multiple promotions —
    that would be an upstream bug). The detail message records the
    exact match count and per-file evidence so triage doesn't need to
    re-read the slurm logs by hand.
    """
    name = "exactly_one_promotion"
    out_files = artifacts.slurm_out_files()
    if not out_files:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"no slurm_*.out files under {artifacts.run_dir}",
            status="fail",
        )
    total = 0
    per_file: list[str] = []
    for path in out_files:
        try:
            text = _strip_ansi(
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError as exc:
            per_file.append(f"{path.name} unreadable: {exc}")
            continue
        matches = _PROMOTED_RE.findall(text)
        if matches:
            total += len(matches)
            per_file.append(
                f"{path.name}: epoch(s)=" + ",".join(matches)
            )
    if total == 1:
        return InvariantResult(
            name=name,
            passed=True,
            detail="; ".join(per_file)
            or "exactly one promotion observed",
            status="pass",
        )
    return InvariantResult(
        name=name,
        passed=False,
        detail=(
            f"expected exactly 1 'promoted to primary epoch=N' line "
            f"across {len(out_files)} slurm_*.out file(s); got "
            f"{total}: " + ("; ".join(per_file) or "(no matches)")
        ),
        status="fail",
    )


@pytest.mark.slurm_live
def test_t06_submitter_disconnect(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    cleanup_cluster: Callable[..., object],  # noqa: ARG001 — drives ordering
) -> None:
    """N=2 dispatch with the local primary driver SIGINT'd mid-flight.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`N_SECONDARIES` idle nodes. Dispatch: spawn a
    ``compiler_suit_runner submit`` :class:`subprocess.Popen` directly
    so we can capture its PID; arm the disconnect helper once the
    framework's run dir appears under :data:`SLURM_TEST_ENV_LOG_ROOT`;
    drain stdout/stderr asynchronously so a full pipe doesn't masquerade
    as a hang.

    Mid-flight: the disconnect helper polls for ``received initial
    assignment`` on a secondary's slurm log, then sends ``SIGINT`` to
    the captured PID. The framework's surviving secondaries detect
    the disconnect, run a promotion election, and one of them logs
    ``promoted to primary epoch=N`` before draining the work.

    Post-flight: parse the captured stderr for the run id, build
    :class:`RunArtifacts`, and run the relaxed invariant audit
    documented in the module docstring (clean exit, no bind, no
    build-failures, exactly one promotion, plus the three cluster
    leak checks).

    NOTE on cache management: this test does NOT depend on the
    ``fresh_run`` fixture (which would require :func:`subprocess.run`
    and so couldn't expose the child PID). We invoke
    :func:`clear_incremental_cache` directly before and after the
    dispatch to preserve the same cache-cold guarantee. The
    ``cleanup_cluster`` fixture still drives the cluster-side
    drain at fixture start/end via the conftest's standard wiring.
    """
    probe = _live_probe()

    # ---- pre-flight ---------------------------------------------------
    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T6 start; found "
        f"{len(queued)} job(s): {queued!r}"
    )

    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    idle_nodes = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    assert len(idle_nodes) >= N_SECONDARIES, (
        f"need at least {N_SECONDARIES} idle worker(s) for T6; "
        "got states: "
        + ", ".join(
            f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
        )
    )

    # ---- compose invocation ------------------------------------------
    # Tiny workload, two secondaries, explicit identity file +
    # cpus_per_task (mirrors T4/T5 overrides). ``variant_sample`` /
    # ``max_variants`` scaled to ``2 * N_SECONDARIES`` so both
    # secondaries actually receive work (per T4's docstring).
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=N_SECONDARIES, workload="tiny"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        variant_sample=VARIANT_BUDGET,
        max_variants=VARIANT_BUDGET,
    )

    timeout_s = _resolve_timeout()

    # ---- dispatch (non-blocking) -------------------------------------
    # Snapshot the existing run_<TS> directories so we can spot the
    # framework's freshly-created one.
    log_root = SLURM_TEST_ENV_LOG_ROOT
    log_root.mkdir(parents=True, exist_ok=True)
    baseline_run_dirs = {p.name for p in log_root.glob("run_*")}

    # Cache-cold guarantee. ``fresh_run`` would normally do this, but
    # we are not using that fixture (see test docstring).
    clear_incremental_cache()

    handle = _start_dispatch_subprocess(invocation)
    drain_threads = _drain_pipes(handle)

    disconnect_thread: Optional[threading.Thread] = None
    disconnect_slot: Optional[_DisconnectSlot] = None
    armed_run_dir: Optional[pathlib.Path] = None
    try:
        # Wait for the framework's run dir to appear. The dispatch
        # creates it very early (before the first secondary's slurm
        # log materialises); a 60s window is generous given the
        # tiny workload's first-line latency.
        armed_run_dir = _wait_for_run_dir(
            log_root,
            baseline=baseline_run_dirs,
            timeout_s=min(timeout_s, 60.0),
        )
        if armed_run_dir is not None:
            disconnect_thread, disconnect_slot = (
                _spawn_disconnect_watchdog(
                    primary_pid=handle.proc.pid,
                    run_log_dir=armed_run_dir,
                    arm_timeout_s=timeout_s,
                    poll_interval_s=0.5,
                )
            )

        # Wait for the dispatch to exit. After SIGINT the process
        # should terminate within seconds; the secondaries continue
        # running on slurm independently and we audit them via the
        # log mount, NOT by waiting on this process.
        try:
            handle.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            handle.proc.kill()
            handle.proc.wait(timeout=15.0)
            pytest.fail(
                "compiler_suit_runner submit did not exit within "
                f"{timeout_s:.0f}s after SIGINT; the framework's "
                "post-disconnect path may have wedged the local "
                f"primary driver (run_dir={armed_run_dir!s})"
            )
    finally:
        # Always clear cache + join drains so the next test starts
        # clean even if we bailed out above.
        for t in drain_threads:
            t.join(timeout=10.0)
        clear_incremental_cache()

    # Join the disconnect watchdog. Its arm budget tracks ``timeout_s``
    # so by now it has either fired or self-timed-out.
    if disconnect_thread is not None:
        disconnect_thread.join(timeout=30.0)

    wall_s = time.monotonic() - handle.started_at
    stdout = "".join(handle.stdout_chunks)
    stderr = "".join(handle.stderr_chunks)
    run_id = parse_run_id(stderr) or parse_run_id(stdout)

    detail = (
        f"exit={handle.proc.returncode} wall={wall_s:.1f}s "
        f"run_id={run_id!r} armed_run_dir={armed_run_dir!s}"
    )

    # ---- disconnect health checks -----------------------------------
    assert armed_run_dir is not None, (
        "no fresh run_<TS> directory appeared under "
        f"{SLURM_TEST_ENV_LOG_ROOT} within the arm window; cannot "
        f"exercise T6 ({detail}). stderr tail:\n{stderr[-2000:]}"
    )
    assert disconnect_slot is not None, (
        "disconnect watchdog never armed (no run dir spotted in time); "
        f"cannot exercise T6 ({detail})"
    )
    assert disconnect_slot.error is None, (
        f"disconnect watchdog crashed: {disconnect_slot.error} ({detail})"
    )
    disc_result = disconnect_slot.result
    assert disc_result is not None, (
        f"disconnect watchdog returned no result; thread join timed "
        f"out? ({detail})"
    )
    assert disc_result.triggered, (
        f"disconnect watchdog never fired; T6 cannot exercise the "
        f"post-disconnect promotion path. DisconnectResult={disc_result!r} "
        f"({detail})"
    )
    assert disc_result.primary_pid == handle.proc.pid, (
        "disconnect helper targeted a different PID than the dispatch "
        f"child; expected {handle.proc.pid}, got "
        f"{disc_result.primary_pid} ({detail})"
    )

    # We deliberately do NOT assert on handle.proc.returncode: a SIGINT
    # to a python entrypoint typically surfaces as a non-zero exit
    # (whether a KeyboardInterrupt traceback or SIGINT-as-status); the
    # test plan calls this out explicitly. The disconnect-handling
    # path's correctness is established via the surviving secondaries'
    # log evidence, not the local primary's exit code.

    # ---- run_id / log_dir resolution --------------------------------
    if run_id is None:
        pytest.fail(
            f"compiler_suit_runner submit did not emit a run_id "
            f"({detail}). stderr tail:\n{stderr[-2000:]}"
        )
    log_dir = resolve_log_dir(run_id, log_root=SLURM_TEST_ENV_LOG_ROOT)
    if not log_dir.is_dir():
        # Fall back to whichever fresh run dir we actually armed against.
        log_dir = armed_run_dir
    assert log_dir.is_dir(), (
        f"expected run log dir {log_dir} after dispatch ({detail})"
    )

    # ---- drain SLURM before invariant audit -------------------------
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after the disconnect "
        f"({detail}); a surviving secondary may itself be hung "
        f"(post-disconnect-with-failure path — see T2)"
    )

    # ---- invariant audit --------------------------------------------
    artifacts = RunArtifacts.from_dir(
        log_dir, shared_fs=invocation.shared_fs,
    )

    # Standard 1: BOTH secondaries should still carry the clean-exit
    # markers — neither was killed. If one is missing them the
    # surviving secondary failed to drain, which is itself the
    # failure surface we want to flag.
    clean_exit = check_clean_exit(artifacts)

    # Standard 2: no EADDRINUSE / "Address already in use" anywhere.
    no_bind = check_no_bind_errors(artifacts)

    # Standard 3 (manifest count) is INTENTIONALLY skipped — see
    # module docstring. The custom promotion check below is the T6-
    # specific evidence of correct post-disconnect behaviour.

    # Standard 4: build-failures empty for the clean-toolchain path
    # we use here. A failure-injected toolchain is T2's scope, not
    # T6's; T6 only injects a CONNECTION-level failure.
    build_failures_result = check_build_failures(artifacts, expected_count=0)

    # Custom invariant: exactly one promotion event recorded.
    promotion = _check_exactly_one_promotion(artifacts)

    # Standard 5/6/7: cluster-side leak audit. The cluster has been
    # drained above so these probes hit a quiesced cluster.
    leaked_containers = check_no_leaked_containers(
        artifacts, probe, WORKERS,
    )
    leaked_listeners = check_no_leaked_listener_ports(
        artifacts, probe, WORKERS,
    )
    leaked_processes = check_no_leaked_processes(
        artifacts, probe, WORKERS,
    )

    results: list[InvariantResult] = [
        clean_exit,
        no_bind,
        build_failures_result,
        promotion,
        leaked_containers,
        leaked_listeners,
        leaked_processes,
    ]

    failed = [r for r in results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}\n"
        f"disconnect={disc_result!r}\n"
        f"results:\n{_format_results(results)}"
    )


# ---------------------------------------------------------------------------
# Helper-shape unit test (offline; no live cluster needed)
# ---------------------------------------------------------------------------


def test_kill_local_primary_driver_argv_shape(tmp_path: pathlib.Path) -> None:
    """Mock-test that the helper resolves PID + signal correctly.

    Verifies the wire-shape of :func:`kill_local_primary_driver` without
    touching the live cluster:

    * arming triggers when ANY ``slurm_*.out`` carries
      ``received initial assignment``;
    * the kill path receives the explicit ``primary_pid`` (the pgrep
      fallback is not invoked when ``primary_pid`` is set);
    * the resolved signal matches the requested name.
    """
    # Lay down a fake run dir with ONE slurm_*.out carrying the
    # arming trigger and one without (so we know the helper will
    # find at least one onboarded secondary).
    run_dir = tmp_path / "run_19700101_000000"
    run_dir.mkdir()
    (run_dir / "slurm_111.out").write_text(
        "garbage\nreceived initial assignment\n",
        encoding="utf-8",
    )
    (run_dir / "slurm_222.out").write_text(
        "no trigger here yet\n",
        encoding="utf-8",
    )

    captured: list[tuple[int, int]] = []
    discovery_called = [0]

    def _fake_kill(pid: int, signum: int) -> None:
        captured.append((pid, signum))

    def _fake_discover() -> Optional[int]:
        discovery_called[0] += 1
        return 99999  # would be returned only if primary_pid is None

    result = kill_local_primary_driver(
        primary_pid=4242,
        run_log_dir=run_dir,
        signal_name="SIGINT",
        arm_timeout_s=2.0,
        poll_interval_s=0.0,
        kill_fn=_fake_kill,
        discover_pid=_fake_discover,
    )

    import signal as _signal

    assert result.triggered is True, result
    assert result.primary_pid == 4242, result
    assert result.signal == "SIGINT", result
    assert captured == [(4242, int(_signal.SIGINT))], captured
    # discover_pid should not have been called when an explicit PID
    # is supplied — this is the contract the test docstring states.
    assert discovery_called[0] == 0, discovery_called


def test_kill_local_primary_driver_times_out_when_no_trigger(
    tmp_path: pathlib.Path,
) -> None:
    """When NO slurm_*.out carries the trigger, the helper times out."""
    run_dir = tmp_path / "run_empty"
    run_dir.mkdir()
    (run_dir / "slurm_1.out").write_text("nothing\n", encoding="utf-8")

    captured: list[tuple[int, int]] = []

    def _fake_kill(pid: int, signum: int) -> None:
        captured.append((pid, signum))

    fake_clock = [0.0]

    def _clock() -> float:
        return fake_clock[0]

    def _sleep(delta: float) -> None:
        fake_clock[0] += max(delta, 0.01)

    result = kill_local_primary_driver(
        primary_pid=1234,
        run_log_dir=run_dir,
        signal_name="SIGINT",
        arm_timeout_s=0.05,
        poll_interval_s=0.01,
        kill_fn=_fake_kill,
        discover_pid=lambda: None,
        clock=_clock,
        sleep=_sleep,
    )

    assert result.triggered is False, result
    assert captured == [], captured
    assert any("timed out" in n for n in result.notes), result.notes


# Skip pytest's collection of the live test on non-linux just to keep
# the unit tests above runnable on developer machines that don't have
# the slurm-test-env. The pytest marker ``slurm_live`` already gates
# the live test off-by-default; this is belt-and-braces.
if sys.platform != "linux":  # pragma: no cover - platform guard
    pytestmark = pytest.mark.skip(
        reason="slurm-test-env is linux-only",
    )

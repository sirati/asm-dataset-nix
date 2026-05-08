"""End-to-end T2 reproducer: post-promotion-with-failure secondary hang.

The framework has an open bug: when a secondary's task fails AFTER it
has been promoted to primary (because the local primary disconnected),
the secondary HANGS instead of draining and exiting. This test plants a
deliberately-broken phase-3 variant manifest, lets the dispatch run,
and watches the secondary's slurm_*.out for the hang pre-conditions
(``promoted to primary epoch=N`` AND ``local primary disconnected``).
If the secondary then sits without a ``secondary finished successfully``
line for ``T2_TIMEOUT_S`` seconds, we attach py-spy via ``podman exec``
and save the stack to ``<base>/artifacts/t02_<TS>_pyspy.txt`` for
offline analysis.

Two outcomes are documented and surface differently:

* ``HANG REPRODUCED``: secondary still alive at the deadline, py-spy
  stack captured. Test fails with a diagnostic referencing the saved
  artifact files. Expected outcome under the open bug.
* ``HANG NOT REPRODUCED``: secondary exited (cleanly OR with non-zero)
  before the deadline, build-failure log carries the
  ``TEST_BROKEN`` signature. Test passes — the framework correctly
  drained.

A run where the secondary exits with code 0 without ever attempting
the broken build is ALSO a failure (the planted broken drv didn't
reach the secondary, e.g. because the manifest mutator raced past the
dispatch). The diagnostic message lists the watcher state so the test
caller can decide between bug-not-reproduced (rare-but-OK) and
test-bug (always-fix).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import threading
import time
from typing import Callable, Optional

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    ClusterProbe,
    GatewayConfig,
)
from compiler_suit_runner.tests.slurm.invariants import (
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.reproducers.broken_toolchain import (
    BROKEN_BUILD_FAILURE_SIGNATURE,
    HangCaptureResult,
    HangTriggerState,
    build_failure_log_carries_signature,
    capture_hang_stack,
    detect_hang_trigger,
    make_broken_smoke_invocation,
    remove_dir_if_present,
    reset_artifacts_dir,
    resolve_broken_drv_path,
    watch_and_mutate_manifests,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    SLURM_TEST_ENV_LOG_ROOT,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``. See test_t01_clean_tiny.py for the same convention.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. The hang
# capture harness searches them in order for the secondary's container.
WORKERS: tuple[str, ...] = (
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
)

# Default wall-clock cap for the dispatch. 600s mirrors the plan's
# T2 deep-dive deadline; override via ``T2_TIMEOUT_S=<seconds>`` for
# pre-CI sanity checks against a faster cluster.
DEFAULT_TIMEOUT_S = 600.0

# How long we wait for the post-preflight manifest-mutate to land.
# Preflight typically writes manifests within 10-30s on the live env;
# 120s is a generous upper bound that still keeps the test bounded if
# ``compiler_suit_runner submit`` never reaches the manifest step.
MANIFEST_WATCH_TIMEOUT_S = 120.0

# Polling interval for the hang-trigger detector. The framework
# flushes slurm_*.out at most every few seconds, so 2s is plenty for
# detection without burning CPU.
HANG_POLL_INTERVAL_S = 2.0

# How long we keep watching for the bug pre-conditions before giving
# up and treating the run as "didn't reach the post-promotion path".
# The full 600s budget is dominated by the actual hang wait; the bug
# pre-conditions usually surface within the first 90s.
PRECONDITION_WATCH_BUDGET_S = 240.0


# Local artifacts dir; the test writes py-spy and ps dumps here.
ARTIFACTS_DIR = (
    pathlib.Path(__file__).parent / "artifacts"
)


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    Mirrors the rationale in test_t01: the conftest fixture's probe is
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
    """Read ``T2_TIMEOUT_S`` from the environment, falling back to default."""
    raw = os.environ.get("T2_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@dataclasses.dataclass(slots=True)
class _WatcherSlot:
    """Scratch slot the watcher thread uses to publish its result."""

    polled_for_s: float = 0.0
    mutated: tuple[pathlib.Path, ...] = ()
    error: Optional[str] = None


def _spawn_manifest_watcher(
    shared_fs: pathlib.Path,
    *,
    broken_drv: str,
    timeout_s: float,
) -> tuple[threading.Thread, _WatcherSlot]:
    """Start a background watcher that mutates the variant manifest.

    The watcher polls ``shared_fs/manifests/`` until a phase-3 variant
    JSON shows up, rewrites its ``payload.drv`` to the broken drv, and
    exits. The (thread, slot) tuple lets the caller join + inspect the
    outcome after the dispatch returns.
    """
    slot = _WatcherSlot()

    def _target() -> None:
        try:
            result = watch_and_mutate_manifests(
                shared_fs / "manifests",
                broken_drv=broken_drv,
                timeout_s=timeout_s,
            )
            if result is None:
                slot.error = (
                    f"manifest watcher timed out after {timeout_s:.0f}s "
                    f"with no phase-3 variant manifest in "
                    f"{shared_fs / 'manifests'}"
                )
            else:
                slot.polled_for_s = result.polled_for_s
                slot.mutated = result.mutated
        except Exception as exc:  # noqa: BLE001 — surface to test thread
            slot.error = f"manifest watcher crashed: {exc!r}"

    thread = threading.Thread(
        target=_target, daemon=True, name="t02-manifest-watcher",
    )
    thread.start()
    return thread, slot


@dataclasses.dataclass(slots=True)
class _HangWatchOutcome:
    """Final result of the hang-trigger watch loop.

    ``saw_preconditions``: ``True`` once the secondary log shows BOTH
    ``promoted to primary`` AND ``local primary disconnected``.
    ``saw_finished``: secondary printed ``secondary finished successfully``.
    ``deadline_reached``: wall-clock budget for the trigger watch hit
    before either of the above; in practice the test then runs the
    full 600s hang wait anyway, but the flag distinguishes
    "bug-not-reproduced because pre-conditions never met" from
    "bug-not-reproduced because secondary cleanly exited".
    ``trigger_state``: the last-observed :class:`HangTriggerState`
    snapshot — surfaced in the test failure message so a triage
    reader sees the EXACT log-line state the watcher saw.
    """

    saw_preconditions: bool = False
    saw_finished: bool = False
    deadline_reached: bool = False
    trigger_state: HangTriggerState = dataclasses.field(
        default_factory=lambda: HangTriggerState(False, False, False),
    )


def _watch_for_hang_trigger(
    log_dir: pathlib.Path,
    *,
    deadline: float,
    poll_interval_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> _HangWatchOutcome:
    """Block until pre-conditions OR finished OR deadline.

    Polls ``log_dir`` every ``poll_interval_s`` for the three log
    markers. Returns as soon as one of (a) hang pre-conditions are
    met, (b) ``secondary finished successfully`` shows up, (c) the
    deadline is reached. The caller then decides what wall-clock
    behaviour to expect from the secondary process.
    """
    outcome = _HangWatchOutcome()
    while clock() < deadline:
        state = detect_hang_trigger(log_dir)
        outcome.trigger_state = state
        if state.finished:
            outcome.saw_finished = True
            return outcome
        if state.hang_pre_conditions_met:
            outcome.saw_preconditions = True
            return outcome
        time.sleep(poll_interval_s)
    outcome.deadline_reached = True
    return outcome


def _format_capture(result: HangCaptureResult) -> str:
    """Render a :class:`HangCaptureResult` into an assertion message."""
    parts: list[str] = []
    parts.append(
        f"worker={result.worker!r} container_id={result.container_id!r}"
    )
    if result.pyspy_dump is not None:
        parts.append(f"py-spy stack: {result.pyspy_dump}")
    else:
        parts.append("py-spy stack: (not captured)")
    if result.ps_dump is not None:
        parts.append(f"ps auxf:    {result.ps_dump}")
    else:
        parts.append("ps auxf:    (not captured)")
    if result.notes:
        parts.append("notes: " + " | ".join(result.notes))
    return "\n  ".join(parts)


def _drain_then_invariants(
    probe: ClusterProbe,
    *,
    drain_timeout_s: float,
) -> bool:
    """Wait for ``squeue --me`` to drain. Return whether it actually did."""
    return wait_squeue_empty(probe, timeout_s=drain_timeout_s)


@pytest.mark.slurm_live
def test_t02_post_promotion_hang(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- ordering dep
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- ordering dep
) -> None:
    """Reproduce the post-promotion-with-failure hang (or fail loudly).

    See module docstring for the protocol; this function is the
    orchestration. The high-level shape:

    1. Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo idle.
    2. Resolve the broken drv path (eval-only, no build).
    3. Reset the artifacts dir (drop stale ``t02_*`` files).
    4. Compose a tiny invocation, wipe the per-test shared_fs.
    5. Spawn the manifest-mutation watcher BEFORE dispatch starts.
    6. Run ``compiler_suit_runner submit`` via ``fresh_run``.
    7. After dispatch returns OR times out, scan the run's logs for
       hang pre-conditions / secondary-finished markers.
    8. Decide the outcome:
       a. PASS -> secondary cleanly exited, build-failure log
          carries the broken-drv signature. Bug did NOT reproduce.
       b. FAIL+CAPTURE -> secondary still alive past the deadline OR
          stuck after pre-conditions met. py-spy + ps captured.
       c. FAIL -> secondary exited with code 0 (false positive: the
          broken drv never reached the secondary).
    """
    probe = _live_probe()

    # ---- pre-flight ----------------------------------------------------
    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T2 start; found "
        f"{len(queued)} job(s): {queued!r}"
    )

    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    # T2 only needs ONE healthy worker (jobs=1). Other workers being
    # down (e.g. slurm-worker1 marked NOT_RESPONDING by slurmctld in
    # the local test env) doesn't block this test, unlike T1 which
    # asserts the full 4-node baseline. Require at least 1 idle.
    idle_workers = [
        w for w in WORKERS
        if w in sinfo_by_node and sinfo_by_node[w].state.startswith("idle")
    ]
    assert idle_workers, (
        "no idle workers in sinfo; cannot dispatch any secondary. "
        + ", ".join(
            f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            if w in sinfo_by_node
        )
    )

    # ---- broken-drv eval ----------------------------------------------
    try:
        drv_info = resolve_broken_drv_path()
    except RuntimeError as exc:
        pytest.fail(
            f"could not resolve broken drv path "
            f"(_drvPaths.x86_64-linux.__test_broken__): {exc}"
        )
    broken_drv = drv_info.drv_path

    # ---- artifacts dir reset ------------------------------------------
    reset_artifacts_dir(ARTIFACTS_DIR)

    # ---- compose invocation -------------------------------------------
    invocation = make_broken_smoke_invocation(
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
    )
    shared_fs = invocation.shared_fs

    # If the per-test shared_fs already exists from a stale run, drop
    # it: stale manifests would be picked up by the watcher and
    # mutated, then preflight would overwrite them with the un-mutated
    # version (race-on-stale-state).
    remove_dir_if_present(shared_fs)

    # ---- spawn watcher BEFORE dispatch --------------------------------
    watcher_thread, watcher_slot = _spawn_manifest_watcher(
        shared_fs,
        broken_drv=broken_drv,
        timeout_s=MANIFEST_WATCH_TIMEOUT_S,
    )

    timeout_s = _resolve_timeout()
    started = time.monotonic()
    result = fresh_run(invocation, timeout_s=timeout_s)
    dispatch_wall_s = time.monotonic() - started

    # The watcher should be done by now (the dispatch can't have
    # finished without preflight emitting manifests). Join with a small
    # cap so a stuck watcher doesn't dangle.
    watcher_thread.join(timeout=5.0)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"dispatch_wall={dispatch_wall_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"log_dir={result.log_dir!s} "
        f"broken_drv={broken_drv!r} "
        f"watcher.mutated={watcher_slot.mutated!r} "
        f"watcher.polled_for_s={watcher_slot.polled_for_s:.1f} "
        f"watcher.error={watcher_slot.error!r}"
    )

    # ---- watcher health check -----------------------------------------
    # If the watcher never mutated a manifest, the test cannot reach
    # the bug. Surface as a hard failure with full context so the
    # triage path is obvious (likely cause: preflight failed, or the
    # manifest dir is somewhere else than expected).
    if not watcher_slot.mutated:
        pytest.fail(
            f"manifest watcher did not mutate any phase-3 variant "
            f"manifest; cannot exercise the broken-drv path "
            f"({detail})"
        )

    # ---- run_id / log_dir resolution ----------------------------------
    if result.run_id is None or result.log_dir is None:
        # If the framework crashed before even emitting the run id,
        # the test cannot reach its bug-checking path. Surface the
        # CLI tail so the operator sees the cause.
        pytest.fail(
            f"compiler_suit_runner submit did not produce a run_id "
            f"({detail}). stderr tail:\n{result.stderr[-2000:]}"
        )

    log_dir = result.log_dir
    if not log_dir.is_dir():
        # The framework may exit before flushing its first log file.
        # Re-derive from SLURM_TEST_ENV_LOG_ROOT in case the wrapper
        # mis-resolved.
        log_dir = resolve_log_dir(
            result.run_id, log_root=SLURM_TEST_ENV_LOG_ROOT,
        )
    assert log_dir.is_dir(), (
        f"expected run log dir {log_dir} after dispatch ({detail})"
    )

    # ---- hang detection -----------------------------------------------
    # The dispatch has returned (the local primary is done). The
    # secondary may have already finished, may be in the middle of the
    # post-promotion drain, or may be hung. Scan the log for the
    # markers; if neither finished nor pre-conditions show up within
    # PRECONDITION_WATCH_BUDGET_S, treat the run as "didn't reach the
    # post-promotion path" — surface as a soft skip with diagnostics.
    outcome = _watch_for_hang_trigger(
        log_dir,
        deadline=time.monotonic() + PRECONDITION_WATCH_BUDGET_S,
        poll_interval_s=HANG_POLL_INTERVAL_S,
    )

    if outcome.saw_finished:
        # Secondary cleanly exited. Either the bug didn't reproduce on
        # this run (good — the framework drained correctly) or the
        # broken drv build never reached the secondary (false negative
        # — should be a test failure). Distinguish via the build-
        # failure log.
        bf_dir = log_dir / "build-failures"
        carries_signature = build_failure_log_carries_signature(
            bf_dir,
            signature=BROKEN_BUILD_FAILURE_SIGNATURE,
        )
        if carries_signature:
            # Bug DID NOT reproduce; framework correctly drained after
            # the planted failure. Test passes — we surfaced one clean
            # data point that the upstream patch (when written) needs
            # to preserve.
            return
        # No failure-log signature: the secondary exited cleanly without
        # ever attempting the broken build. That's a test bug
        # (mutator raced past the dispatch, or the manifest mutation
        # didn't take effect for some other reason).
        pytest.fail(
            f"secondary cleanly finished without attempting the "
            f"broken build (no '{BROKEN_BUILD_FAILURE_SIGNATURE}' in "
            f"{bf_dir}); the planted manifest mutation may have been "
            f"overwritten before dispatch picked it up. {detail} "
            f"trigger_state={outcome.trigger_state!r}"
        )

    if not outcome.saw_preconditions:
        # Neither finished nor pre-conditions met within the budget.
        # This is unusual — the dispatch is supposed to make at least
        # one secondary visible. Capture diagnostics anyway.
        capture = capture_hang_stack(
            probe,
            artifacts_dir=ARTIFACTS_DIR,
            workers=WORKERS,
        )
        pytest.fail(
            f"secondary log never showed hang pre-conditions OR "
            f"finished marker within {PRECONDITION_WATCH_BUDGET_S:.0f}s; "
            f"{detail} trigger_state={outcome.trigger_state!r}\n  "
            f"capture: {_format_capture(capture)}"
        )

    # Pre-conditions met. The secondary is now in the post-promotion
    # drain path (or hung therein). Per the plan ("T+600s wall-clock
    # with no secondary finished successfully"), we wait
    # ``timeout_s`` more seconds from THIS point — not from test
    # start — so cluster-startup latency doesn't eat into the hang
    # observation window. If the secondary cleanly finishes before
    # the deadline we PASS; if not, we capture py-spy and FAIL.
    pre_cond_observed_at = time.monotonic()
    deadline = pre_cond_observed_at + timeout_s
    while time.monotonic() < deadline:
        state = detect_hang_trigger(log_dir)
        if state.finished:
            outcome.saw_finished = True
            outcome.trigger_state = state
            break
        time.sleep(HANG_POLL_INTERVAL_S)

    if outcome.saw_finished:
        # Bug did NOT reproduce: secondary drained even though it was
        # promoted-and-failed. Confirm signature.
        bf_dir = log_dir / "build-failures"
        carries_signature = build_failure_log_carries_signature(
            bf_dir,
            signature=BROKEN_BUILD_FAILURE_SIGNATURE,
        )
        if not carries_signature:
            pytest.fail(
                f"secondary finished after pre-conditions but no "
                f"'{BROKEN_BUILD_FAILURE_SIGNATURE}' signature in "
                f"build-failures/; mutator may have raced. {detail}"
            )
        # Bug not reproduced this run. Pass.
        return

    # Secondary still alive at deadline -> HANG REPRODUCED. Capture.
    capture = capture_hang_stack(
        probe,
        artifacts_dir=ARTIFACTS_DIR,
        workers=WORKERS,
    )
    # Drain SLURM as part of the failure path so subsequent tests see
    # a clean cluster — the conftest cleanup hook handles this too,
    # but doing it here means the failure message reflects post-drain
    # state.
    _drain_then_invariants(probe, drain_timeout_s=120.0)

    pytest.fail(
        f"HANG REPRODUCED: secondary still alive {timeout_s:.0f}s "
        f"after pre-conditions (promoted + primary_disconnected).\n  "
        f"{detail}\n  "
        f"trigger_state={outcome.trigger_state!r}\n  "
        f"capture:\n  {_format_capture(capture)}\n  "
        f"NEXT STEP: read the py-spy stack to identify the suit_task "
        f"or dynamic_runner frame holding the secondary; either patch "
        f"or open an upstream issue with that frame as the lede."
    )

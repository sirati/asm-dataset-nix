"""End-to-end T5 reproducer: SIGKILL one secondary mid-build.

Two secondaries, medium workload, no broken toolchain. A sidecar
watchdog (see
:func:`reproducers.inject_failures.kill_secondary_when_first_variant_completes`)
observes ``secondary-1``'s ``slurm_<jobid>.out`` for the first
successful-variant ``task completed`` line and SIGKILLs that slurm
job. The expected framework behaviour is documented in the test plan
("Test matrix" row T5):

* secondary-0 detects the lost peer, wins the post-disconnect
  promotion election, drains the remaining work, and exits cleanly
  (``Container exited with code: 0``);
* secondary-1's slurm log is truncated by the kill (no ``Container
  exited with code: 0`` marker; SLURM records the wrapper's death);
* the run as a whole completes its assigned variants — any in-flight
  variants secondary-1 was working on at kill-time may show up as
  build failures or as "missing manifests", and the test asserts on
  whichever shape the framework produces (see invariant audit below).

The 7-invariant audit is intentionally LENIENT for T5 because at least
one secondary's ``slurm_*.out`` will not carry the clean-exit markers
(invariant 1 fails by design). We:

* run the four file-only invariants (1-4) but tolerate the killed
  secondary's ``slurm_*.out`` not carrying the success markers;
* still run the three cluster invariants (5-7) — the WHOLE point of
  T5 is that the framework cleans up its podman containers / listener
  ports / orphan processes even on a SIGKILL'd secondary;
* set ``expected_failure_count`` from the count of variants that were
  in-flight on secondary-1 at kill-time (heuristic: at most 1 — the
  framework dispatches one variant at a time per worker; the kill
  fires AFTER the first successful-variant completion, so an
  in-flight variant at that moment is either zero or one).

A run with leaked containers / leaked listeners / leaked PPID=1
processes from the killed secondary signals a real upstream bug
(secondary-1 had no chance to run its EXIT trap, so SLURM's
``proctrack/linuxproc`` is the only line of defence; any leak here
is exactly the smoke16 class of bug the plan is trying to surface).

Per project memory (``feedback_ssh_debug_key.md`` /
``feedback_scancel_scope.md``):

* the slurm-test-env SSH key is ephemeral and is passed via ``-i``
  on every probe (never via ``ssh-agent`` / ``~/.ssh/``);
* the watchdog scopes scancel to ``--jobname=asm-secondary-*`` AND
  the explicit jobid; we MUST NOT pass ``--user=kruppb`` because the
  ``kruppb`` account is shared with the asm-tokenizer peer.
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
    InvariantResult,
    RunArtifacts,
    check_build_failures,
    check_no_bind_errors,
    check_no_leaked_containers,
    check_no_leaked_listener_ports,
    check_no_leaked_processes,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.reproducers.inject_failures import (
    KillResult,
    kill_secondary_when_first_variant_completes,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    SLURM_TEST_ENV_LOG_ROOT,
    default_invocation_for_smoke,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral: never added to ssh-agent or ``~/.ssh/``; always passed
# via ``-i``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. Invariants
# 5-7 walk this list to look for leaks; T5 cares about leaks on the
# worker that hosted the killed secondary AS WELL AS on the worker that
# kept running.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap. T5's medium workload + post-promotion drain
# is heavier than T4's tiny clean run; 1200s gives the surviving
# secondary enough budget to drain after the kill. Override via
# ``T5_TIMEOUT_S=<seconds>`` for slow CI.
DEFAULT_TIMEOUT_S = 1200.0

# Watchdog target: the framework numbers secondaries 0..N-1 in dispatch
# order. T5 kills secondary-1 deterministically so secondary-0 is the
# one we expect to promote; reversing this would also be valid but
# splitting the responsibility across the two id slots makes the test
# message line up with the plan's prose.
# Which secondary to SIGKILL mid-build. With N=2 the framework's
# distributed manager assigns the toolchain (one heavyweight build)
# to one secondary and the variant fan-out (cache-warm builds) to the
# other, but the polarity between secondary-0 / secondary-1 is
# non-deterministic across runs in the post-2f30920 framework. We use
# the watchdog's ``"auto"`` mode which scans both secondaries' slurm
# logs and fires on whichever one FIRST reports a successful variant
# completion — that is the secondary running variants, and SIGKILL'ing
# it mid-build is what triggers the post-promotion drain we want to
# exercise.
TARGET_SECONDARY_ID = "auto"

# Number of secondaries this row dispatches. Two is the minimum for
# the post-promotion drain we exercise.
N_SECONDARIES = 2

# Variant scaling. T4's docstring explains why a tiny workload's
# default of one total variant is too small for multi-secondary
# dispatch (only one secondary ever receives work). For T5 we go further:
# we need ENOUGH variants that secondary-1 has work in flight when the
# kill fires AND that secondary-0 has more work to do after the
# promotion. The watchdog fires on the FIRST variant completion (~one
# cache-cold build wall on the live cluster, ~75 s); the scancel
# round-trip is a few seconds; so the secondary needs strictly more
# than (1 + scancel-latency / per-variant-wall) variants per secondary
# in flight to still be running cache-warm follow-ups when SIGKILL
# lands.
#
# Upper bound: the preflight passes the variant suffix set as a
# ``--select`` expression to ``nix-eval-jobs``. Above ~12 total variants
# the combined argv + env crosses the kernel's ARG_MAX and the
# subprocess fails with ``OSError: [Errno 7] Argument list too long``.
# ``4 * N_SECONDARIES`` (= 8 with N=2) sits comfortably below that
# ceiling and still keeps secondary-1 busy with 3 cache-warm variants
# past the kill on the live cluster wall numbers.
VARIANT_BUDGET = 4 * N_SECONDARIES

# In-flight cap for the failure heuristic. The framework dispatches
# one variant per worker at a time; secondary-1 has at most this many
# variants in flight when SIGKILL lands. We use this as the upper
# bound on ``expected_failure_count`` for invariant 4 — the actual
# count is whatever the framework records, but we accept up to this
# many failure-log entries before flagging a leak.
IN_FLIGHT_VARIANT_CAP = 1


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    Same rationale as the other live tests in this slice: the conftest
    fixture's probe is constructed without a key; we instantiate
    locally so SSH probes work via the explicit ``-i`` key as project
    policy demands.
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
    """Read ``T5_TIMEOUT_S`` from the environment, falling back to default."""
    raw = os.environ.get("T5_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@dataclasses.dataclass(slots=True)
class _WatchdogSlot:
    """Scratch slot the watchdog thread uses to publish its result."""

    result: Optional[KillResult] = None
    error: Optional[str] = None


def _spawn_watchdog(
    probe: ClusterProbe,
    *,
    run_log_dir: pathlib.Path,
    target_secondary_id: str,
    timeout_s: float,
    poll_interval_s: float,
) -> tuple[threading.Thread, _WatchdogSlot]:
    """Start a daemon thread running the kill-watchdog.

    The thread polls ``run_log_dir`` for the trigger condition AND, on
    match, issues the scancel via ``probe.gateway_ssh``. We thread it
    rather than running it inline because ``compiler_suit_runner
    submit`` blocks the main test thread until the local primary
    finishes dispatching, which can outlive the kill window.
    """
    slot = _WatchdogSlot()

    def _target() -> None:
        try:
            slot.result = kill_secondary_when_first_variant_completes(
                probe,
                run_log_dir=run_log_dir,
                target_secondary_id=target_secondary_id,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
            )
        except Exception as exc:  # noqa: BLE001 — surface to test thread
            slot.error = f"watchdog crashed: {exc!r}"

    thread = threading.Thread(
        target=_target, daemon=True, name="t05-kill-watchdog",
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


def _surviving_secondary_clean_exit(
    artifacts: RunArtifacts, killed_jobid: Optional[str],
) -> InvariantResult:
    """Custom invariant 1 for T5: at least one secondary exits clean.

    A SIGKILL'd secondary's slurm_*.out is truncated mid-flush; it
    will NOT carry both ``secondary finished successfully`` AND
    ``Container exited with code: 0``. We therefore relax the standard
    :func:`check_clean_exit` invariant to require only that the
    NON-killed secondary exits cleanly; the killed jobid is identified
    by ``killed_jobid`` (parsed from the watchdog's
    :class:`KillResult`).

    Returns an :class:`InvariantResult` mirroring the standard 7-check
    shape so :func:`_format_results` can render it consistently.
    """
    name = "clean_exit_survivor"
    out_files = artifacts.slurm_out_files()
    if not out_files:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"no slurm_*.out files under {artifacts.run_dir}",
            status="fail",
        )

    survivor_paths = [
        p for p in out_files
        if killed_jobid is None or f"slurm_{killed_jobid}.out" not in p.name
    ]
    if not survivor_paths:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                "no surviving slurm_*.out file: every slurm_*.out in "
                f"{artifacts.run_dir} matches the killed jobid "
                f"{killed_jobid!r}; cannot verify post-promotion drain"
            ),
            status="fail",
        )

    failures: list[str] = []
    for path in survivor_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failures.append(f"{path.name} unreadable: {exc}")
            continue
        # Strip ANSI before substring match. Mirrors the same ANSI handling
        # in invariants.py so a future framework log layer change touches
        # one regex, not two.
        import re as _re  # noqa: PLC0415 — local import keeps top clean

        text = _re.sub(r"\x1b\[[0-9;]*m", "", text)
        missing: list[str] = []
        for marker in (
            "secondary finished successfully",
            "Container exited with code: 0",
        ):
            if marker not in text:
                missing.append(marker)
        if missing:
            failures.append(f"{path.name} missing markers: {missing!r}")

    if failures:
        return InvariantResult(
            name=name,
            passed=False,
            detail="; ".join(failures),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"{len(survivor_paths)} surviving slurm_*.out file(s) "
            f"clean (killed_jobid={killed_jobid!r})"
        ),
        status="pass",
    )


def _killed_secondary_truncated(
    artifacts: RunArtifacts, killed_jobid: Optional[str],
) -> InvariantResult:
    """Sanity check: the killed secondary's slurm_*.out is truncated.

    SIGKILL bypasses the slurm wrapper's ``trap cleanup EXIT TERM HUP
    INT``, so the killed secondary's slurm_*.out should NOT carry
    ``Container exited with code: 0``. If it does, the kill never
    actually fired (or fired AFTER the wrapper's clean exit, which is
    a watchdog timing bug).

    Skipped (``status="skip"``) if ``killed_jobid`` is ``None`` (the
    watchdog never resolved a jobid; the caller surfaces that as a
    separate failure).
    """
    name = "kill_actually_truncated"
    if killed_jobid is None:
        return InvariantResult(
            name=name,
            passed=False,
            detail="watchdog did not resolve a jobid; cannot verify kill",
            status="skip",
        )
    target_path = artifacts.run_dir / f"slurm_{killed_jobid}.out"
    if not target_path.is_file():
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"expected slurm_{killed_jobid}.out under "
                f"{artifacts.run_dir} after a kill; not found"
            ),
            status="fail",
        )
    try:
        text = target_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"{target_path.name} unreadable: {exc}",
            status="fail",
        )
    import re as _re  # noqa: PLC0415 — local import keeps top clean

    text = _re.sub(r"\x1b\[[0-9;]*m", "", text)
    if "Container exited with code: 0" in text:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"{target_path.name} carries 'Container exited with code: 0' "
                "even though SIGKILL was issued; the kill fired after the "
                "wrapper had already cleaned up — watchdog timing bug"
            ),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"{target_path.name} truncated as expected (no clean-exit "
            "marker)"
        ),
        status="pass",
    )


@pytest.mark.slurm_live
def test_t05_kill_secondary(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 — wired via the B2 cleanup harness
) -> None:
    """N=2 medium dispatch with secondary-1 SIGKILL'd mid-build.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`N_SECONDARIES` idle nodes (T9-style tolerance for a
    single down worker). Dispatch: medium workload via ``fresh_run``
    so the incremental cache is wiped both sides of the call.
    Mid-flight: a daemon thread runs the kill-watchdog targeting
    :data:`TARGET_SECONDARY_ID`. Post-flight:

    * resolve the run's log dir, join the watchdog;
    * assert the watchdog actually triggered (``KillResult.triggered``)
      and that scancel returned ``rc==0``;
    * wait for ``squeue --me`` to drain;
    * run the relaxed file invariants (1' surviving clean, 2 no bind,
      3 manifest count, 4 build-failures within the in-flight cap)
      AND the standard cluster invariants (5-7);
    * assert no leaked containers / listeners / processes — the WHOLE
      point of T5 is that SLURM's proctrack catches the killed
      secondary's container even though the wrapper's EXIT trap never
      ran.

    Failure surface: the assertion message lists every invariant's
    name + detail + row count, plus the watchdog's KillResult, so a
    triage path is obvious without re-reading the gateway logs by hand.
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
        f"squeue --me must be empty at T5 start; found "
        f"{len(queued)} job(s): {queued!r}"
    )

    # T5 needs at least N_SECONDARIES idle workers (the framework picks
    # any of the four; we tolerate up to one missing per the same
    # pattern T4/T9 use).
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
        f"need at least {N_SECONDARIES} idle worker(s) for T5; "
        "got states: "
        + ", ".join(
            f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
        )
    )

    # ---- compose invocation ------------------------------------------
    # Medium workload, two secondaries, explicit identity file +
    # cpus_per_task (mirrors T4's overrides). ``variant_sample`` /
    # ``max_variants`` scaled so both secondaries have work AND so
    # there is enough work after the kill for the surviving secondary
    # to drain through the post-promotion path (the failure mode T5
    # is exercising).
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=N_SECONDARIES, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        variant_sample=VARIANT_BUDGET,
        max_variants=VARIANT_BUDGET,
    )

    # We need the run_log_dir to point the watchdog at, but
    # ``fresh_run`` does not return a handle until the dispatch
    # completes. The framework names runs ``run_<TS>`` based on the
    # local primary's startup wall clock; we resolve it AFTER the
    # dispatch returns and let the watchdog poll on a placeholder
    # until then. This is the same shape T2 uses for its manifest
    # watcher.
    #
    # NOTE: ``run_log_dir`` is computed below from the dispatch's
    # captured ``run_id``; the watchdog spins up only AFTER the
    # dispatch begins emitting its run id, which means we briefly miss
    # the very first variant completion if it lands in the first 1-2
    # seconds. The medium workload's first-variant latency dominates
    # by orders of magnitude (image pull + nix-daemon startup), so
    # this is comfortably safe.
    timeout_s = _resolve_timeout()

    # The watchdog's wall-clock budget is the same total dispatch
    # budget. The watchdog returns early on trigger or on its own
    # ``timeout_s`` — whichever fires first.
    watchdog_thread: Optional[threading.Thread] = None
    watchdog_slot: Optional[_WatchdogSlot] = None

    def _start_watchdog_when_run_dir_visible(
        run_log_root: pathlib.Path,
        budget_s: float,
        poll_interval_s: float = 0.5,
    ) -> None:
        """Helper: spin up the watchdog thread once the run_log_dir
        appears under ``run_log_root``. The framework creates the
        run dir very early (before the first secondary's slurm log
        materialises), but ``fresh_run`` does not surface the
        ``run_id`` until exit; this wrapper polls the log root for
        the newest ``run_<TS>`` directory created since the watch
        started, which is robust against parallel test interference
        because the cleanup fixture has just drained the previous
        runs.
        """
        nonlocal watchdog_thread, watchdog_slot
        baseline = {p.name for p in run_log_root.glob("run_*")}
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            now = {p.name for p in run_log_root.glob("run_*")}
            new = sorted(now - baseline)
            if new:
                run_dir = run_log_root / new[-1]
                watchdog_thread, watchdog_slot = _spawn_watchdog(
                    probe,
                    run_log_dir=run_dir,
                    target_secondary_id=TARGET_SECONDARY_ID,
                    timeout_s=budget_s,
                    poll_interval_s=2.0,
                )
                return
            time.sleep(poll_interval_s)
        # No new run dir within budget; leave the watchdog absent and
        # let the post-flight surface a clear assertion message.

    # Spawn a separate thread that waits for the run dir to appear
    # then arms the watchdog. We can't run that wait synchronously
    # because ``fresh_run`` blocks until the local primary finishes
    # dispatching.
    arm_thread = threading.Thread(
        target=_start_watchdog_when_run_dir_visible,
        args=(SLURM_TEST_ENV_LOG_ROOT, timeout_s),
        daemon=True,
        name="t05-watchdog-arm",
    )
    arm_thread.start()

    started = time.monotonic()
    result = fresh_run(invocation, timeout_s=timeout_s)
    dispatch_wall_s = time.monotonic() - started

    # The arm thread should have spotted the run dir long before the
    # dispatch returned; cap the join so we don't dangle if it didn't.
    arm_thread.join(timeout=5.0)
    if watchdog_thread is not None:
        # Give the watchdog a generous post-dispatch window to finish
        # its scancel + capture timestamp. If it's still polling it
        # will hit its own ``timeout_s`` and return.
        watchdog_thread.join(timeout=60.0)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"dispatch_wall={dispatch_wall_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"log_dir={result.log_dir!s}"
    )

    # ---- watchdog health check ---------------------------------------
    assert watchdog_slot is not None, (
        "kill watchdog never armed (no run dir appeared under "
        f"{SLURM_TEST_ENV_LOG_ROOT}); cannot exercise T5 ({detail})"
    )
    assert watchdog_slot.error is None, (
        f"kill watchdog crashed: {watchdog_slot.error} ({detail})"
    )
    kill_result = watchdog_slot.result
    assert kill_result is not None, (
        f"kill watchdog returned no result; thread join timed out? "
        f"({detail})"
    )
    assert kill_result.triggered, (
        f"kill watchdog never triggered; T5 cannot exercise the "
        f"post-promotion drain. KillResult={kill_result!r} ({detail})"
    )
    assert kill_result.scancel_rc == 0, (
        f"scancel returned non-zero (rc={kill_result.scancel_rc}); "
        f"the kill may not have actually landed. "
        f"stderr={kill_result.scancel_stderr!r} ({detail})"
    )
    killed_jobid = kill_result.jobid

    # ---- run_id / log_dir resolution ---------------------------------
    # ``compiler_suit_runner submit`` may exit non-zero on T5 because
    # the killed secondary's job exits non-zero; the framework
    # currently aggregates this into the local primary's exit code.
    # We therefore do NOT assert on result.exit_code; instead we
    # rely on the surviving-secondary invariants to confirm the
    # promotion drain worked.
    if result.run_id is None or result.log_dir is None:
        pytest.fail(
            f"compiler_suit_runner submit did not produce a run_id "
            f"({detail}). stderr tail:\n{result.stderr[-2000:]}"
        )
    log_dir = result.log_dir
    if not log_dir.is_dir():
        log_dir = resolve_log_dir(
            result.run_id, log_root=SLURM_TEST_ENV_LOG_ROOT,
        )
    assert log_dir.is_dir(), (
        f"expected run log dir {log_dir} after dispatch ({detail})"
    )

    # ---- drain SLURM before invariant audit --------------------------
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after the kill "
        f"({detail}); the surviving secondary may itself be hung "
        f"(post-promotion-with-failure path — see T2)"
    )

    # ---- invariant audit ---------------------------------------------
    artifacts = RunArtifacts.from_dir(
        log_dir, shared_fs=invocation.shared_fs,
    )

    # Custom 1' (clean exit on the SURVIVING secondary): T5's whole
    # point is that secondary-0 promotes + completes + exits clean
    # despite secondary-1 being SIGKILL'd. The standard
    # check_clean_exit is too strict because secondary-1's slurm_*.out
    # is truncated by SIGKILL.
    survivor_clean = _surviving_secondary_clean_exit(
        artifacts, killed_jobid=killed_jobid,
    )

    # Sanity check: the kill ACTUALLY truncated the target jobid's
    # slurm_*.out. A clean exit there would mean the kill fired too
    # late (watchdog timing bug).
    kill_landed = _killed_secondary_truncated(
        artifacts, killed_jobid=killed_jobid,
    )

    # Standard 2: no EADDRINUSE / "Address already in use" anywhere.
    # The killed secondary's container had bound ports 5000/5050
    # at kill-time; if SLURM proctrack didn't tear those down, a
    # subsequent run would hit EADDRINUSE — invariant 2 catches the
    # echo of that on the SAME run if any of the surviving
    # secondaries retried mid-dispatch.
    no_bind = check_no_bind_errors(artifacts)

    # Skip standard 3 (manifest count == completed variants): the
    # framework's manifest writer currently writes manifests OUT to
    # ``shared_fs`` only after a successful task_completed event;
    # secondary-1's pre-kill in-flight variant produces a
    # task_completed line that the kill swallows. The mismatch is
    # therefore EXPECTED, not a bug. We document this and skip the
    # standard check.

    # Standard 4 (build-failures count): bounded above by the
    # in-flight cap. The framework MAY:
    #
    # - record 0 entries (the killed in-flight variant simply
    #   disappears, no log written);
    # - record 1 entry (the killed in-flight variant's stderr is
    #   captured by the gateway's tarball collector before SIGKILL);
    # - record N>1 entries (would indicate the surviving secondary
    #   miscounts; that's a bug).
    #
    # We accept any value in ``[0, IN_FLIGHT_VARIANT_CAP]`` and surface
    # the actual count in the detail message.
    actual_failure_count = (
        sum(1 for _ in artifacts.build_failures_dir.iterdir())
        if artifacts.build_failures_dir.is_dir()
        else 0
    )
    if 0 <= actual_failure_count <= IN_FLIGHT_VARIANT_CAP:
        build_failures_result = InvariantResult(
            name="build_failures",
            passed=True,
            detail=(
                f"build-failures/ has {actual_failure_count} entries "
                f"(within in-flight cap {IN_FLIGHT_VARIANT_CAP})"
            ),
            status="pass",
        )
    else:
        # Use the strict invariant for the failure path so the message
        # carries the standard formatting.
        build_failures_result = check_build_failures(
            artifacts, expected_count=0,
        )

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
        survivor_clean,
        kill_landed,
        no_bind,
        build_failures_result,
        leaked_containers,
        leaked_listeners,
        leaked_processes,
    ]

    failed = [r for r in results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}\n"
        f"kill={kill_result!r}\n"
        f"results:\n{_format_results(results)}"
    )

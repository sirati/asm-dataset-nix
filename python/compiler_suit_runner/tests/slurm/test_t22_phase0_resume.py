"""End-to-end T22 reproducer: Phase 0 resume marker honoured after kill.

Verifies the contract documented in
:func:`compiler_suit_runner.workers.eval_worker.run_eval_task`: the
per-binary resume marker at ``<shared_fs>/out/<binary>/_phase0/
manifest.json`` short-circuits Step 0 of ``run_eval_task`` on every
subsequent invocation, so a kill-then-resubmit run finishes the Phase 0
fan-out in O(seconds) instead of the cache-cold ~minute it costs the
first time round.

Test shape (matches the plan's Part B Verification T22 row):

* Spawn a distributed-eval ``submit`` (``--distributed-eval --jobs 4
  --variant-sample 1 --packages hello``) in the background. The single-
  binary, single-variant shape keeps Phase 0 small enough that the
  marker appears within tens of seconds even cache-cold, narrowing the
  watch loop's wall-clock budget.
* Watch ``<shared_fs>/out/hello/_phase0/manifest.json`` for arrival.
  That file is the resume marker the framework reads from (see
  :func:`eval_worker._marker_path`) — its presence ON the shared FS is
  EXACTLY the boundary between "Phase 0 done" and "Phase 1 dispatch".
* The moment it appears, SIGKILL the submitter process group (the
  framework's primary plus any helper subprocesses). This lands the
  kill AT the Phase 0 → Phase 1 boundary by definition.
* Drain the cluster (the killed primary cannot run its EXIT trap;
  cleanup_cluster handles the residual sbatch jobs).
* Re-submit the same dispatch via ``fresh_run`` — the local
  incremental cache is wiped between calls but ``shared_fs`` is NOT,
  so the on-disk marker persists into the re-submit. Assert the run
  completes inside :data:`RESUME_TIMEOUT_S` (start with a generous
  60 s; the plan suggests 30 s once we have a fresh-run baseline).
* Run the standard 7-invariant audit against the re-submit.

What this test does NOT assert (deferred, see plan):

* That ``nix-eval-jobs`` does not spawn on the re-submit. The plan
  documents this as a "best-effort" stderr/log scrape; the worker
  process tree is opaque from the host once dispatched inside the
  podman cluster, so we surface the wall-clock signal (< 60 s) as the
  hard guarantee and leave the subprocess-count check for a future
  T22b that grows the per-worker tracing surface.

The kill mechanism here is deliberately the simplest available: file-
existence trigger + SIGKILL on the submitter PID. There is no
"kill at signal" hook in :mod:`run_helpers`; the
:mod:`tests.slurm.reproducers.inject_failures` helpers are scoped to
gateway-side scancel of a slurm jobid, not the host-side primary.
Polling the on-disk marker is robust because the marker is a single
atomic ``os.replace`` (see :func:`eval_worker._write_marker`).

KILL TIMING NOTE: this test races the framework. The marker appears
AT the Phase 0 → Phase 1 boundary, but the primary's plan_phase1
callback could still fire between marker-write and SIGKILL. The
resume contract is symmetric — even if Phase 1 starts, the marker is
still on disk for the next run to consume. If the kill latency
proves consistently slow enough that Phase 1 makes meaningful
progress, the test would silently stop exercising the resume path
and turn into a "second clean dispatch" check. Operators should
treat a re-submit wall-clock greater than the fresh-run baseline as
a regression even if the test passes.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import signal
import subprocess
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
    run_all_invariants,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    default_invocation_for_smoke,
    parse_run_id,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# Full worker hostname list. Invariants 5-7 walk this to look for
# leaks left behind by the killed primary; a leak on ANY worker is
# the kind of cleanup-on-kill regression T22 is downstream of.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Plan specifies ``--jobs 4``. The four-secondary shape stays inside
# the live test-env (which has four worker nodes); below 3 idle
# nodes we skip rather than degrade.
N_SECONDARIES = 4
MIN_IDLE_WORKERS = 3

# Per-run binary list. Plan: ``--packages hello`` -> exactly one
# binary, one Phase 0 marker to watch for.
TARGET_BINARY = "hello"

# Watch budget for the marker. The first ``nix-eval-jobs`` cold-cache
# fan-out on the live cluster has been observed at ~60-90 s for
# ``hello`` with one variant per arch. Give the watcher 300 s before
# giving up; this is the test's "did the framework ever get to Phase
# 0 done?" guard, not the resume budget.
MARKER_WATCH_TIMEOUT_S = 300.0
MARKER_POLL_INTERVAL_S = 0.5

# Wall-clock budget for the FIRST submit. The kill watchdog should
# fire well before this elapses; if it doesn't we bail out so a
# stalled primary doesn't burn the whole CI budget. Generous because
# Phase 0 on the cold cluster includes podman image pull on the
# first secondary per worker.
FIRST_SUBMIT_TIMEOUT_S = 900.0

# Wall-clock budget for the RE-SUBMIT. Plan suggests <30 s vs. fresh-
# run baseline; we start with 60 s as a generous initial threshold so
# the first CI runs surface a fresh-run baseline number, then tighten
# in a follow-up. Override via ``T22_RESUME_TIMEOUT_S=<float>``.
RESUME_TIMEOUT_S = 60.0


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    Same rationale as the rest of this test slice — the conftest
    fixture's probe is keyless; we instantiate locally so SSH probes
    authenticate against the ephemeral test-env key without ever
    touching ssh-agent.
    """
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _format_results(results: list[InvariantResult]) -> str:
    """Render every invariant result for a failed assertion message."""
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _resolve_resume_timeout() -> float:
    """Read ``T22_RESUME_TIMEOUT_S`` from the environment.

    Invalid (non-float) values are silently ignored in favour of the
    default — we'd rather a confusing pass than a confusing crash
    mid-test.
    """
    raw = os.environ.get("T22_RESUME_TIMEOUT_S")
    if not raw:
        return RESUME_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return RESUME_TIMEOUT_S


def _marker_path(shared_fs: pathlib.Path, binary: str) -> pathlib.Path:
    """Resume marker path for ``binary`` under ``shared_fs``.

    Mirrors :func:`eval_worker._marker_path` — kept local so the test
    doesn't import the worker module just for one constant; a layout
    change in the worker is visible here as a test-side string fix.
    """
    return shared_fs / "out" / binary / "_phase0" / "manifest.json"


def _wait_for_marker(
    marker: pathlib.Path,
    *,
    timeout_s: float = MARKER_WATCH_TIMEOUT_S,
    poll_interval_s: float = MARKER_POLL_INTERVAL_S,
) -> bool:
    """Poll ``marker`` until it exists, with a deadline.

    Returns ``True`` on success, ``False`` on timeout. The marker is
    written via ``os.replace`` (see :func:`eval_worker._write_marker`)
    so once ``exists()`` returns True the file is complete.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(poll_interval_s)
    return marker.exists()


def _read_marker_variants(marker: pathlib.Path) -> Optional[list[dict]]:
    """Best-effort load of ``marker`` and extract its variant list.

    Returns ``None`` on read / parse failure so the caller can surface
    a unified message; a corrupt marker file is itself a regression
    worth flagging, but we don't crash the test thread.
    """
    import json

    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    variants = data.get("variants")
    if not isinstance(variants, list):
        return None
    return variants


def _spawn_submit_background(
    argv: list[str],
    *,
    cwd: Optional[str] = None,
) -> tuple[subprocess.Popen[str], list[str], list[str]]:
    """Spawn ``compiler_suit_runner submit`` in a new process group.

    Process-group isolation matters because SIGKILL'ing only the
    parent leaves the framework's child helpers (peer harmonia,
    SSH master shims, etc.) attached to PID 1 — exactly the leak
    invariants 5-7 hunt for. We want to kill the whole tree at
    once via ``os.killpg``; that requires the children to share
    a session, which ``start_new_session=True`` gives us.

    Returns the live :class:`Popen` handle and two reference list
    objects: the captured stdout / stderr buffers, populated by the
    drain helpers in the caller. We do NOT use ``capture_output``
    because we need to keep the process alive while watching disk.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        start_new_session=True,
    )
    return proc, [], []


def _kill_submit(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the submitter process group, best-effort.

    Two-step: try the whole group via ``os.killpg(SIGKILL)``; if
    the group has already exited (race) the call raises
    ``ProcessLookupError`` and we fall through to the per-PID
    fallback. Either way the local primary is gone after this
    returns.
    """
    if proc.poll() is not None:
        return  # already exited
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def _drain_subprocess(
    proc: subprocess.Popen[str],
    *,
    timeout_s: float = 10.0,
) -> tuple[str, str]:
    """Wait for ``proc`` to exit and capture remaining output.

    Used after the kill to harvest whatever the primary wrote before
    the SIGKILL landed. ``communicate`` is bounded so a stuck
    pipe-buffer drain doesn't dangle the test thread; on timeout we
    return whatever we have.
    """
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # Force a second kill (the first SIGKILL should have landed
        # but ``communicate``'s deadline can clip on slow pipe flush).
        _kill_submit(proc)
        try:
            out, err = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    return out or "", err or ""


@pytest.mark.slurm_live
def test_t22_phase0_resume(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: Callable[..., object],
) -> None:
    """Kill-then-resubmit: the resume marker short-circuits Phase 0.

    Pre-flight: gateway reachable, ``squeue --me`` empty, ≥
    :data:`MIN_IDLE_WORKERS` idle nodes. Dispatch one of:

    1. Background ``submit`` with ``--distributed-eval --jobs 4
       --variant-sample 1 --packages hello``.
    2. Watch loop polls ``<shared_fs>/out/hello/_phase0/manifest.json``.
       On arrival: SIGKILL the submitter process group, wait for it
       to exit, then drain ``squeue`` so the killed sbatch jobs
       don't pollute the re-submit's pre-flight.
    3. Assert: marker exists and lists ≥ 1 variant entry.
    4. Re-submit via ``fresh_run`` (cache wipe between, shared_fs
       preserved). Assert wall-time <
       :data:`RESUME_TIMEOUT_S`, run completes cleanly, standard
       7-invariant audit passes.

    Failure surface: assertion messages call out the marker path, its
    contents (or read error), the first-run wall + run_id, and the
    re-submit wall + invariant detail. A regression where the
    re-submit re-runs ``nix-eval-jobs`` from scratch shows up as a
    wall-time blow-up; a regression where the marker isn't honoured
    shows up as an invariant failure (manifest count mismatch or
    excessive build-failures during eval).
    """
    probe = _live_probe()

    # ---- pre-flight --------------------------------------------------
    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T22 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    idle = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    if len(idle) < MIN_IDLE_WORKERS:
        pytest.skip(
            f"T22 needs at least {MIN_IDLE_WORKERS} idle worker(s); "
            f"got {len(idle)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    n_secondaries = min(N_SECONDARIES, len(idle))

    # ---- compose invocation ------------------------------------------
    # ``default_invocation_for_smoke(jobs=N, workload="tiny")`` already
    # pins ``packages=("hello",)``, ``--variant-sample 1``, etc — the
    # exact "single binary, single variant" shape T22 wants. We layer
    # ``--distributed-eval`` on via ``extra_args`` because the test
    # slice's ``RunInvocation`` does not (yet) carry a boolean for
    # it; this is consistent with T11's ``--replication-k`` knob.
    base_invocation = default_invocation_for_smoke(
        jobs=n_secondaries, workload="tiny",
    )
    invocation = dataclasses.replace(
        base_invocation,
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
        extra_args=("--distributed-eval",),
    )

    shared_fs = invocation.shared_fs
    marker = _marker_path(shared_fs, TARGET_BINARY)

    # ---- first submit: background, kill at Phase 0 → Phase 1 ---------
    # Spawn directly via Popen so we can SIGKILL the process group
    # the moment the marker lands. ``fresh_run`` would block until
    # exit, defeating the watch loop.
    argv = invocation.to_argv()
    started_first = time.monotonic()
    proc, _, _ = _spawn_submit_background(argv)

    try:
        marker_seen = _wait_for_marker(
            marker,
            timeout_s=MARKER_WATCH_TIMEOUT_S,
            poll_interval_s=MARKER_POLL_INTERVAL_S,
        )
        first_wall = time.monotonic() - started_first

        # Submitter must still be alive when we kill it — a clean exit
        # before the marker appears would mean the framework never
        # got to Phase 0, which is a separate (T20-class) failure
        # mode and should be surfaced as such.
        if proc.poll() is not None and not marker_seen:
            stdout_tail, stderr_tail = _drain_subprocess(proc, timeout_s=2.0)
            pytest.fail(
                f"submitter exited before Phase 0 marker appeared: "
                f"exit={proc.returncode} wall={first_wall:.1f}s "
                f"marker={marker} (still missing).\n"
                f"stderr tail:\n{stderr_tail[-2000:]}"
            )

        if not marker_seen:
            # Watchdog timeout — kill before bailing so the cluster
            # cleanup harness has something to drain.
            _kill_submit(proc)
            _drain_subprocess(proc, timeout_s=10.0)
            pytest.fail(
                f"Phase 0 resume marker did not appear at {marker} "
                f"within {MARKER_WATCH_TIMEOUT_S:.0f}s of submit start "
                f"(wall={first_wall:.1f}s); either Phase 0 is failing "
                "or the marker layout changed."
            )

        # Marker is on disk — kill at the boundary.
        _kill_submit(proc)
        first_stdout, first_stderr = _drain_subprocess(proc, timeout_s=15.0)
    finally:
        # Belt-and-braces: regardless of how we exit the watch block,
        # don't leave the submitter wandering around. ``_kill_submit``
        # is idempotent on an already-dead process.
        _kill_submit(proc)

    first_detail = (
        f"first_wall={first_wall:.1f}s "
        f"first_exit={proc.returncode} "
        f"marker={marker!s}"
    )

    # ---- post-kill assertions on the marker --------------------------
    assert marker.is_file(), (
        f"resume marker disappeared between watch and assertion at "
        f"{marker} ({first_detail}). stderr tail:\n"
        f"{first_stderr[-2000:]}"
    )
    variants = _read_marker_variants(marker)
    assert variants is not None and len(variants) >= 1, (
        f"resume marker {marker} did not list ≥ 1 variant entry: "
        f"variants={variants!r} ({first_detail}). stderr tail:\n"
        f"{first_stderr[-2000:]}"
    )

    # Resolve the killed run's id (best-effort) for the failure message.
    # We harvest stdout + stderr because cli._setup_logging attaches to
    # stderr but the framework may flush partial state to stdout under
    # SIGKILL (the stream layer's buffering is implementation-defined).
    first_run_id = (
        parse_run_id(first_stderr) or parse_run_id(first_stdout)
    )
    first_log_dir = (
        resolve_log_dir(first_run_id) if first_run_id else None
    )

    # Drain the cluster: the killed primary cannot run its EXIT trap,
    # so cleanup_cluster's scancel pass below is what tears the
    # secondary sbatch jobs down. We do that BEFORE the re-submit so
    # the re-submit's own pre-flight (which asserts squeue empty)
    # passes.
    cleanup_cluster()
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after the kill; the "
        f"killed primary left sbatch jobs we couldn't scancel. "
        f"({first_detail})"
    )

    # ---- re-submit ---------------------------------------------------
    # Same invocation -> same shared_fs -> the on-disk marker is
    # consumed by Step 0 of run_eval_task. The dispatch budget is the
    # plan's resume threshold; we treat exceedance as a regression.
    resume_timeout_s = _resolve_resume_timeout()
    started_resume = time.monotonic()
    result = fresh_run(invocation, timeout_s=resume_timeout_s * 4)
    resume_wall = time.monotonic() - started_resume

    detail = (
        f"{first_detail} "
        f"resume_wall={resume_wall:.1f}s "
        f"resume_exit={result.exit_code} "
        f"resume_run_id={result.run_id!r} "
        f"resume_log_dir={result.log_dir!s} "
        f"first_log_dir={first_log_dir!s}"
    )

    assert result.exit_code == 0, (
        f"re-submit returned non-zero ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.run_id is not None, (
        f"resume run_id not parsed from CLI output ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.log_dir is not None and result.log_dir.is_dir(), (
        f"resume log_dir missing or not a directory ({detail})"
    )

    # ---- THE T22 ASSERTION: wall-time under the resume budget --------
    assert resume_wall < resume_timeout_s, (
        f"resume wall-time {resume_wall:.1f}s exceeded the budget "
        f"{resume_timeout_s:.1f}s; the resume marker was not honoured "
        f"(nix-eval-jobs likely re-ran). ({detail})\n"
        f"stderr tail:\n{result.stderr[-2000:]}"
    )

    # ---- standard 7-invariant audit on the re-submit -----------------
    # The re-submit completes Phase 0 fast (resume) but still runs
    # Phase 1 (variant builds) end-to-end; the full audit therefore
    # applies unchanged.
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after the re-submit "
        f"completed ({detail})"
    )

    artifacts = RunArtifacts.from_dir(
        result.log_dir, shared_fs=invocation.shared_fs,
    )
    inv_results = run_all_invariants(
        artifacts,
        probe,
        WORKERS,
        expected_failure_count=0,
    )

    failed = [r for r in inv_results if not r.passed]
    assert not failed, (
        f"invariant(s) failed on the re-submit for {detail}:\n"
        f"{_format_results(inv_results)}"
    )

    # The marker MUST still exist post-resume — the worker's Step 0
    # short-circuit returns the existing marker dict; it does not
    # rewrite it. A missing marker here would mean some cleanup path
    # is racing the resume.
    assert marker.is_file(), (
        f"resume marker {marker} disappeared after a clean re-submit "
        f"({detail}); a cleanup race deleted the resume signal."
    )

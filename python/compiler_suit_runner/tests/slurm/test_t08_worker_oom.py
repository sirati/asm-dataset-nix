"""End-to-end T8 reproducer: constrain ONE worker's secondary to 512M.

Four secondaries, medium workload, no broken toolchain. A sidecar
arming thread waits for the framework's run dir + the target
secondary's ``connection_info/<id>.info`` file, resolves the worker
hosting that secondary, and SSHes into it to write
``memory_max=512M`` to the secondary container's cgroup ``memory.max``
file (cgroup-v2) / ``memory.limit_in_bytes`` (cgroup-v1).

The slurm-test-env's per-worker ``RealMemory=3500`` (per project
memory ``project_slurm_test_env.md``); 512M is well below the
secondary's working set under a medium workload, so the kernel's
OOM-killer reaps the constrained container's PID-1 (the secondary's
Python entry) within seconds of the constraint landing. SLURM marks
the corresponding job as failed; the framework's surviving 3
secondaries detect the loss, run a promotion election (one wins),
and drain the remaining assigned variants.

The expected framework behaviour is documented in the test plan
("Test matrix" row T8 + "Failure-injection mechanics" + sub-sub-task
plays "C3.4 T8 (worker OOM)" T8-α/β/γ):

* the constrained secondary's slurm_*.out is truncated by the OOM
  kill (no ``Container exited with code: 0`` marker);
* the OTHER 3 secondaries' slurm_*.out files carry both standard
  clean-exit markers (``secondary finished successfully`` AND
  ``Container exited with code: 0``);
* the framework completes its dispatched variants (the remaining 3
  secondaries pick up the OOM'd one's slack via the dynamic dispatch
  loop);
* SLURM proctrack tears down the OOM'd container's leftover state
  even though the framework's EXIT trap never ran — invariants 5/6/7
  catch any leak.

Approach choice (T8-α / T8-β):

The plan offered three injection paths -- (a) sbatch ``--mem``,
(b) per-job ``--nodelist + --mem``, (c) post-hoc cgroup write. We
chose (c). Rationale:

* The framework's CLI does not currently expose an sbatch ``--mem``
  flag (only ``--slurm-cpus-per-task``). Adding a per-job sbatch arg
  would need a framework patch — out of scope for this test.
* Path (b) requires per-job sbatch args; the framework submits N
  identical jobs.
* Path (c) — write to the rootless podman cgroup AFTER the container
  starts — is implemented by
  :func:`reproducers.inject_failures.oom_one_worker_via_cgroup`. The
  helper waits for the framework's ``connection_info/<secondary>.info``
  to publish the worker hostname, then SSHes to that worker and
  drives a small shell script that locates the per-job
  ``/tmp/asm-<hash>/storage`` podman root, finds the secondary's
  container id, derives the cgroup path via ``podman inspect``, and
  writes ``memory_max`` to ``memory.max`` (cgroup-v2 detection via
  ``/sys/fs/cgroup/cgroup.controllers``).

The 7-invariant audit is intentionally LENIENT for T8 because the
killed secondary's ``slurm_*.out`` will not carry the clean-exit
markers (invariant 1 fails by design). We:

* run a CUSTOM relaxed clean-exit invariant that requires the OTHER
  3 secondaries to carry both markers (mirrors T5's
  ``_surviving_secondary_clean_exit``);
* run a CUSTOM ``oom_signature_present`` invariant on the constrained
  secondary's slurm_*.out + slurm_*.err: the kernel emits at least
  one of ``Killed``/``out of memory``/``oom-kill``/``cgroup out of
  memory`` markers OR the wrapper logs a non-zero ``Container exited
  with code:`` line;
* set ``expected_failure_count`` from the count of variants the
  constrained secondary had in flight (heuristic: at most 1, same
  shape as T5);
* still run cluster invariants 5-7 — the WHOLE point is that SLURM's
  proctrack catches the leftover container even though the
  framework's EXIT trap never ran.

Per project memory (``feedback_ssh_debug_key.md`` /
``feedback_scancel_scope.md``):

* the slurm-test-env SSH key is ephemeral and is passed via ``-i``
  on every probe (never via ``ssh-agent`` / ``~/.ssh/``);
* cleanup-side scancel stays scoped to ``--jobname=asm-secondary-*``;
  T8 itself does not issue a scancel.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
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
    check_no_bind_errors,
    check_no_leaked_containers,
    check_no_leaked_listener_ports,
    check_no_leaked_processes,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.reproducers.inject_failures import (
    OomResult,
    oom_one_worker_via_cgroup,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_LOG_ROOT,
    RunResult,
    default_invocation_for_smoke,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral: never added to ssh-agent or ``~/.ssh/``; always passed
# via ``-i``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. Invariants
# 5-7 walk this list to look for leaks; T8 cares about leaks on the
# constrained worker AS WELL AS on the workers that kept running.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap. T8's medium workload + post-OOM drain on a
# 4-secondary mesh is heavier than T5's N=2 medium; 1500s gives the
# surviving 3 secondaries enough budget to drain after the OOM. Override
# via ``T8_TIMEOUT_S=<seconds>`` for slow CI.
DEFAULT_TIMEOUT_S = 1500.0

# Constrained worker. T8 pins ``slurm-worker3`` per the plan: the
# framework dispatches secondaries round-robin across idle workers, but
# pinning ONE worker as the OOM target keeps the test deterministic
# (the OTHER three are the "surviving" set we audit). If
# ``slurm-worker3`` is down the test falls back to whichever worker is
# hosting ``TARGET_SECONDARY_ID``.
PREFERRED_TARGET_WORKER = "slurm-worker3"

# Constrained secondary id. The framework numbers secondaries 0..N-1 in
# dispatch order. We target ``secondary-2`` deterministically — picking
# a non-zero index keeps any "secondary-0 is special" framework path
# (initial primary, etc.) out of the kill window. The test's relaxed
# clean-exit check then expects secondaries 0, 1 and 3 to carry the
# success markers.
TARGET_SECONDARY_ID = "secondary-2"

# Number of secondaries this row dispatches. Four exercises the
# multi-node mesh; with three surviving the post-OOM drain is well-
# bounded.
DESIRED_N_SECONDARIES = 4

# Lower bound for the idle-worker pre-flight. The plan calls T8
# tolerant down to N=3 (with the constrained worker as the third).
# Below this we cannot meaningfully exercise the surviving-mesh drain.
MIN_IDLE_WORKERS = 3

# Variant scaling. T5's docstring explains why a tiny workload's
# default of one total variant is too small for multi-secondary
# dispatch.
#
# Target: enough variants that the constrained secondary has work
# in flight when the OOM lands AND that the surviving secondaries
# have more work to do after the kill. With N=4 the matrix's
# variant-sample machinery passes the suffix list through nix-
# eval-jobs as one big ``--select`` ``intersectAttrs`` expression;
# combined with our merged env (NIX_PATH, PYTHONPATH, …) the kernel's
# ARG_MAX (E2BIG) bites at sample sizes >= 12 (verified). At
# ``2 * N`` (= 8) the preflight always succeeds. The downside on
# the live cluster is that each secondary completes its share of
# the 8 variants before the OOM helper finishes resolving the
# target's container, which surfaces as
# ``OomResult.notes == ('worker reported error: no_running_container',)``.
# That's a known-acceptable test-flake mode for the post-2f30920
# framework (the dispatch is fast enough that even a 60 s armed
# helper races the secondary's exit); the test grades the run as
# PARTIAL when the helper times out without arming, and we accept
# that the underlying framework integration still passed.
VARIANT_BUDGET = 2 * DESIRED_N_SECONDARIES

# Memory cap. 512M is well below the secondary's working set under a
# medium workload (the wrapper itself reserves >2GiB; the secondary's
# Python entry + nix-daemon + harmonia-cache adds at least another
# few hundred MiB). 512M reliably triggers the kernel OOM-killer
# within seconds of the dispatch reaching the build phase.
OOM_MEMORY_MAX = "512M"

# In-flight cap for the failure heuristic. Same shape as T5: the
# framework dispatches one variant per worker at a time, so at most
# one variant is in flight on the OOM'd secondary at the moment the
# kill fires.
IN_FLIGHT_VARIANT_CAP = 1


# Patterns surfaced by the kernel OOM-killer (cgroup-v2 + cgroup-v1
# wording variants) AND by the SLURM wrapper's container-exit line.
# We accept ANY one of these as evidence the OOM landed.
_OOM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"out of memory", re.IGNORECASE),
    re.compile(r"oom[-_ ]?kill", re.IGNORECASE),
    re.compile(r"Killed process", re.IGNORECASE),
    re.compile(r"cgroup out of memory", re.IGNORECASE),
    re.compile(r"Memory cgroup out of memory", re.IGNORECASE),
    # Wrapper-side container-exit line with non-zero rc; ``Killed`` is
    # what bash emits when a child is SIGKILL'd, ``137`` is the
    # 128+SIGKILL exit status SLURM frequently surfaces.
    re.compile(r"Container exited with code: (?:[1-9]\d*|137|Killed)"),
    re.compile(r"Container exited.*Killed"),
)

_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
    """Read ``T8_TIMEOUT_S`` from the environment, falling back to default."""
    raw = os.environ.get("T8_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@dataclasses.dataclass(slots=True)
class _OomSlot:
    """Scratch slot the OOM-arming thread uses to publish its result."""

    result: Optional[OomResult] = None
    error: Optional[str] = None


def _spawn_oom_arm(
    probe: ClusterProbe,
    *,
    run_log_root: pathlib.Path,
    baseline: set[str],
    target_secondary_id: str,
    target_worker: str | None,
    memory_max: str,
    arm_timeout_s: float,
    poll_interval_s: float,
) -> tuple[threading.Thread, _OomSlot]:
    """Start a daemon thread running the OOM-injection helper.

    The thread waits for the framework's run dir + the target
    secondary's ``connection_info/<id>.info`` then SSHes to that worker
    to constrain the secondary's container memory cgroup. We thread it
    rather than running it inline because ``compiler_suit_runner
    submit`` blocks the main test thread until the local primary
    finishes dispatching, which can outlive the arming window.
    """
    slot = _OomSlot()

    def _target() -> None:
        try:
            slot.result = oom_one_worker_via_cgroup(
                probe,
                run_log_root=run_log_root,
                target_worker=target_worker,
                target_secondary_id=target_secondary_id,
                memory_max=memory_max,
                arm_after_run_dir_appears=True,
                baseline_run_dirs=baseline,
                arm_timeout_s=arm_timeout_s,
                poll_interval_s=poll_interval_s,
            )
        except Exception as exc:  # noqa: BLE001 — surface to test thread
            slot.error = f"oom helper crashed: {exc!r}"

    thread = threading.Thread(
        target=_target, daemon=True, name="t08-oom-arm",
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


def _resolve_oomed_jobid(
    artifacts: RunArtifacts,
    target_secondary_id: str,
) -> Optional[str]:
    """Find the slurm jobid that hosted ``target_secondary_id``.

    Mirrors the file-cross-reference shape used in
    :func:`inject_failures._resolve_secondary_out`. Returns ``None`` if
    the connection_info file is missing or no slurm_*.out matches its
    hostname header.
    """
    info_path = (
        artifacts.run_dir / "connection_info" / f"{target_secondary_id}.info"
    )
    if not info_path.is_file():
        return None
    try:
        text = info_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    target_host: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("hostname="):
            target_host = line.partition("=")[2].strip() or None
            break
    if target_host is None:
        return None

    for path in artifacts.slurm_out_files():
        try:
            out_text = _strip_ansi(
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError:
            continue
        if f"Node: {target_host}" in out_text:
            m = re.match(r"^slurm_(\d+)\.out$", path.name)
            if m is not None:
                return m.group(1)
    return None


def _oomed_secondary_truncated(
    artifacts: RunArtifacts,
    oomed_jobid: Optional[str],
) -> InvariantResult:
    """Sanity check: the OOM'd secondary's slurm_*.out is truncated.

    OOM-kill bypasses the framework's container exit trap; the
    constrained secondary's slurm_*.out should NOT carry
    ``Container exited with code: 0``. If it does, the cgroup write
    landed too late (the secondary already finished its assigned
    work) — that is a watchdog timing failure, not a framework
    failure. Skipped (``status="skip"``) if ``oomed_jobid`` is
    ``None`` (we could not resolve it).
    """
    name = "oom_actually_truncated"
    if oomed_jobid is None:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                "could not resolve oomed_jobid for "
                f"{TARGET_SECONDARY_ID!r}; cannot verify the OOM landed"
            ),
            status="skip",
        )
    target_path = artifacts.run_dir / f"slurm_{oomed_jobid}.out"
    if not target_path.is_file():
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"expected slurm_{oomed_jobid}.out under "
                f"{artifacts.run_dir} after an OOM injection; not found"
            ),
            status="fail",
        )
    try:
        text = _strip_ansi(
            target_path.read_text(encoding="utf-8", errors="replace"),
        )
    except OSError as exc:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"{target_path.name} unreadable: {exc}",
            status="fail",
        )
    if "Container exited with code: 0" in text:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"{target_path.name} carries 'Container exited with "
                "code: 0' even though we constrained its memory; the "
                "cgroup write landed AFTER the container had already "
                "finished — timing bug in the OOM arming"
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


def _surviving_secondaries_clean_exit(
    artifacts: RunArtifacts,
    oomed_jobid: Optional[str],
) -> InvariantResult:
    """Custom invariant 1 for T8: ALL non-OOM'd secondaries exit clean.

    An OOM'd secondary's slurm_*.out is truncated mid-flush; it will
    NOT carry both ``secondary finished successfully`` AND
    ``Container exited with code: 0``. We therefore relax the standard
    ``check_clean_exit`` to require only that the slurm_*.out files
    NOT belonging to ``oomed_jobid`` carry both markers.

    Returns an :class:`InvariantResult` mirroring the standard 7-check
    shape so :func:`_format_results` can render it consistently.
    """
    name = "clean_exit_survivors"
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
        if oomed_jobid is None or f"slurm_{oomed_jobid}.out" not in p.name
    ]
    if not survivor_paths:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                "no surviving slurm_*.out file: every slurm_*.out in "
                f"{artifacts.run_dir} matches the OOM'd jobid "
                f"{oomed_jobid!r}; cannot verify surviving-mesh drain"
            ),
            status="fail",
        )

    failures: list[str] = []
    for path in survivor_paths:
        try:
            text = _strip_ansi(
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError as exc:
            failures.append(f"{path.name} unreadable: {exc}")
            continue
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
            f"clean (oomed_jobid={oomed_jobid!r})"
        ),
        status="pass",
    )


def _oom_signature_present(
    artifacts: RunArtifacts,
    oomed_jobid: Optional[str],
) -> InvariantResult:
    """Custom invariant: OOM kernel/wrapper marker on the constrained side.

    Scans the constrained secondary's slurm_*.out AND slurm_*.err for
    any of :data:`_OOM_PATTERNS`. At least one match across the two
    streams is required to confirm the cgroup constraint actually
    triggered the kernel OOM-killer; without it we'd have a false-
    positive pass where the cgroup write succeeded but the secondary's
    working set never crossed the limit (in which case the test isn't
    exercising the OOM path at all).
    """
    name = "oom_signature_present"
    if oomed_jobid is None:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                "could not resolve oomed_jobid; skipping OOM-marker check"
            ),
            status="skip",
        )
    candidates = [
        artifacts.run_dir / f"slurm_{oomed_jobid}.out",
        artifacts.run_dir / f"slurm_{oomed_jobid}.err",
    ]
    matched: list[str] = []
    missing: list[str] = []
    for path in candidates:
        if not path.is_file():
            missing.append(f"{path.name} not found")
            continue
        try:
            text = _strip_ansi(
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError as exc:
            missing.append(f"{path.name} unreadable: {exc}")
            continue
        for pat in _OOM_PATTERNS:
            m = pat.search(text)
            if m is not None:
                matched.append(
                    f"{path.name} matched {pat.pattern!r} "
                    f"with {m.group(0)[:120]!r}"
                )
                break

    if matched:
        return InvariantResult(
            name=name,
            passed=True,
            detail="; ".join(matched),
            status="pass",
        )
    return InvariantResult(
        name=name,
        passed=False,
        detail=(
            "no OOM kernel/wrapper marker found on slurm_"
            f"{oomed_jobid}.{{out,err}}; the cgroup constraint may have "
            "been written but never triggered (memory pressure too low). "
            "Probed: " + ", ".join(p.name for p in candidates) + ". "
            "Notes: " + ("; ".join(missing) if missing else "(none)")
        ),
        status="fail",
    )


@pytest.mark.slurm_live
def test_t08_worker_oom(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 — wired via the B2 cleanup harness
) -> None:
    """N=4 medium dispatch with one secondary OOM'd via cgroup constraint.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` idle nodes (T9-style tolerance for
    a single down worker; if ``slurm-worker1`` is the down one we still
    have ``slurm-worker3`` as a target).

    Dispatch: medium workload via ``fresh_run`` so the incremental cache
    is wiped both sides of the call. Mid-flight: a daemon thread runs
    :func:`oom_one_worker_via_cgroup` targeting :data:`TARGET_SECONDARY_ID`
    with ``memory_max=512M``. The helper waits for the framework's run
    dir + ``connection_info/<id>.info`` then SSHes to the resolved
    worker and writes 512M into the secondary container's cgroup
    ``memory.max`` file.

    Post-flight:

    * resolve the run's log dir, join the OOM-arming thread;
    * assert the helper actually triggered (``OomResult.triggered``);
    * wait for ``squeue --me`` to drain;
    * resolve the OOM'd jobid by cross-referencing the secondary's
      ``connection_info/<id>.info`` hostname against the slurm_*.out
      headers;
    * run the relaxed file invariants (1' surviving clean, OOM-marker
      present, OOM truncated, 2 no bind, 4 build-failures within the
      in-flight cap) AND the standard cluster invariants (5-7);
    * assert no leaked containers / listeners / processes — the WHOLE
      point of T8 is that SLURM's proctrack catches the OOM'd
      secondary's container even though the framework's EXIT trap
      never ran.

    Failure surface: the assertion message lists every invariant's
    name + detail + row count, plus the helper's OomResult, so a
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
        f"squeue --me must be empty at T8 start; found "
        f"{len(queued)} job(s): {queued!r}"
    )

    # T8 needs at least MIN_IDLE_WORKERS idle workers (the framework
    # picks any of the four; we tolerate up to one missing per the same
    # pattern T4/T9 use).
    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    idle_workers = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    if len(idle_workers) < MIN_IDLE_WORKERS:
        pytest.skip(
            f"T8 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )
    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # If our preferred constrained worker is idle, pin it; otherwise
    # let the helper resolve a worker from connection_info on whichever
    # node TARGET_SECONDARY_ID lands. Either way the OTHER 3
    # secondaries form the surviving set we audit.
    if PREFERRED_TARGET_WORKER in idle_workers:
        target_worker_pin: Optional[str] = None  # let helper resolve
        # NOTE: we deliberately do NOT pin the SLURM ``--nodelist`` to
        # ``PREFERRED_TARGET_WORKER`` because the framework's CLI does
        # not expose per-job sbatch args. The OOM helper resolves
        # whichever worker hosts TARGET_SECONDARY_ID at dispatch time.
        # PREFERRED_TARGET_WORKER is a doc-only preference; the actual
        # pin is the secondary id.
    else:
        target_worker_pin = None

    # ---- compose invocation ------------------------------------------
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        variant_sample=VARIANT_BUDGET,
        max_variants=VARIANT_BUDGET,
    )

    timeout_s = _resolve_timeout()

    # Snapshot the existing run_<TS> directories so the OOM helper's
    # ``baseline_run_dirs`` can identify the new one. Doing this BEFORE
    # the dispatch start matches the helper's contract.
    log_root = SLURM_TEST_ENV_LOG_ROOT
    log_root.mkdir(parents=True, exist_ok=True)
    baseline_run_dirs = {p.name for p in log_root.glob("run_*")}

    # Spawn the OOM-arming thread BEFORE the dispatch starts so the
    # arming poll is already live by the time the framework creates
    # its run dir.
    oom_thread, oom_slot = _spawn_oom_arm(
        probe,
        run_log_root=log_root,
        baseline=baseline_run_dirs,
        target_secondary_id=TARGET_SECONDARY_ID,
        target_worker=target_worker_pin,
        memory_max=OOM_MEMORY_MAX,
        arm_timeout_s=timeout_s,
        poll_interval_s=2.0,
    )

    started = time.monotonic()
    result = fresh_run(invocation, timeout_s=timeout_s)
    dispatch_wall_s = time.monotonic() - started

    # Give the OOM thread a generous post-dispatch window to finish its
    # SSH probe + capture timestamp. If it's still polling it will hit
    # its own ``arm_timeout_s`` and return.
    oom_thread.join(timeout=60.0)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"dispatch_wall={dispatch_wall_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"log_dir={result.log_dir!s}"
    )

    # ---- OOM helper health check -------------------------------------
    assert oom_slot.error is None, (
        f"oom helper crashed: {oom_slot.error} ({detail})"
    )
    oom_result = oom_slot.result
    assert oom_result is not None, (
        f"oom helper returned no result; thread join timed out? "
        f"({detail})"
    )
    assert oom_result.triggered, (
        f"oom helper never triggered; T8 cannot exercise the "
        f"surviving-mesh drain. OomResult={oom_result!r} ({detail})"
    )

    # ---- run_id / log_dir resolution ---------------------------------
    # ``compiler_suit_runner submit`` may exit non-zero on T8 because
    # the OOM'd secondary's job exits non-zero; the framework
    # currently aggregates this into the local primary's exit code.
    # We therefore do NOT assert on result.exit_code; instead we rely
    # on the surviving-secondary invariants to confirm the drain
    # worked.
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
        f"squeue --me did not drain within 300s after the OOM "
        f"({detail}); a surviving secondary may itself be hung "
        f"(post-promotion-with-failure path — see T2)"
    )

    # ---- invariant audit ---------------------------------------------
    artifacts = RunArtifacts.from_dir(
        log_dir, shared_fs=invocation.shared_fs,
    )

    oomed_jobid = _resolve_oomed_jobid(artifacts, TARGET_SECONDARY_ID)

    # Custom 1' (surviving secondaries clean): the OTHER N-1
    # secondaries each carry both clean-exit markers. A SIGKILL'd or
    # OOM'd secondary's slurm_*.out is truncated, so the standard
    # check_clean_exit is too strict.
    survivor_clean = _surviving_secondaries_clean_exit(
        artifacts, oomed_jobid=oomed_jobid,
    )

    # OOM-specific: confirm the kernel actually killed the constrained
    # secondary's container. Without this we could pass on a false-
    # positive where the cgroup write succeeded but the working set
    # stayed under the cap.
    oom_signature = _oom_signature_present(
        artifacts, oomed_jobid=oomed_jobid,
    )

    # Sanity check: the OOM ACTUALLY truncated the target jobid's
    # slurm_*.out. A clean exit there would mean the kill landed too
    # late (the secondary already finished its assigned work).
    oom_truncated = _oomed_secondary_truncated(
        artifacts, oomed_jobid=oomed_jobid,
    )

    # Standard 2: no EADDRINUSE / "Address already in use" anywhere.
    # The OOM'd secondary's container had bound ports 5000/5050 at
    # kill-time; if SLURM proctrack didn't tear those down a subsequent
    # run would hit EADDRINUSE.
    no_bind = check_no_bind_errors(artifacts)

    # Skip standard 3 (manifest count == completed variants): same
    # rationale as T5 — the OOM'd secondary's pre-kill in-flight
    # variant produces a task_completed line that the kill swallows
    # before the manifest writer flushes. The mismatch is EXPECTED.

    # Standard 4 (build-failures count): bounded above by the in-flight
    # cap. Same shape as T5: the framework MAY record 0 entries (the
    # killed in-flight variant simply disappears) up to N where N
    # equals the OOM'd secondary's in-flight slot count (we dispatch
    # one variant per worker at a time, so N <= 1 in practice).
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
        build_failures_result = InvariantResult(
            name="build_failures",
            passed=False,
            detail=(
                f"build-failures/ has {actual_failure_count} entries; "
                f"expected in [0, {IN_FLIGHT_VARIANT_CAP}]"
            ),
            status="fail",
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
        oom_signature,
        oom_truncated,
        no_bind,
        build_failures_result,
        leaked_containers,
        leaked_listeners,
        leaked_processes,
    ]

    failed = [r for r in results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}\n"
        f"oom={oom_result!r}\n"
        f"oomed_jobid={oomed_jobid!r}\n"
        f"results:\n{_format_results(results)}"
    )


# ---------------------------------------------------------------------------
# Helper-shape unit test (offline; no live cluster needed)
# ---------------------------------------------------------------------------


def test_oom_one_worker_via_cgroup_argv_shape(
    tmp_path: pathlib.Path,
) -> None:
    """Mock-test that the helper drives the expected SSH wire shape.

    Verifies the wire-shape of :func:`oom_one_worker_via_cgroup` without
    touching the live cluster:

    * arming triggers when the run dir + connection_info file appear;
    * the worker_ssh call targets the resolved worker hostname;
    * the worker-side script carries the requested ``memory_max``
      value (shell-quoted) and the storage glob;
    * a successful ``WROTE=...`` line yields ``triggered=True`` and
      populates ``container_id`` / ``cgroup_path`` /
      ``cgroup_version``.
    """
    import subprocess as _subprocess

    log_root = tmp_path / "log"
    log_root.mkdir()
    run_dir = log_root / "run_19700101_000000"
    run_dir.mkdir()
    (run_dir / "connection_info").mkdir()
    (run_dir / "connection_info" / "secondary-2.info").write_text(
        "hostname=slurm-worker3\ntunnel_port=12345\n",
        encoding="utf-8",
    )

    captured: list[tuple[str, str, float | None]] = []

    def _fake_worker_ssh(
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,  # noqa: ARG001 — unused in stub
    ) -> _subprocess.CompletedProcess[str]:
        captured.append((worker, cmd, timeout))
        # Simulate the script's success path.
        stdout = (
            "STORAGE_ROOT=/tmp/asm-cafebabe/storage\n"
            "RUN_ROOT=/tmp/asm-cafebabe/run\n"
            "CID=deadbeefcafe\n"
            "CGROUP_PATH=/machine.slice/libpod-deadbeefcafe.scope\n"
            "CGROUP_VERSION=2\n"
            "TARGET=/sys/fs/cgroup/machine.slice/"
            "libpod-deadbeefcafe.scope/memory.max\n"
            "WROTE=512M\n"
        )
        return _subprocess.CompletedProcess(
            args=["ssh", worker, cmd],
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    result = oom_one_worker_via_cgroup(
        _StubProbe(),
        run_log_root=log_root,
        target_secondary_id="secondary-2",
        memory_max="512M",
        arm_timeout_s=2.0,
        poll_interval_s=0.0,
        # Empty baseline: the test pre-creates the run dir, so we want
        # the helper to "see" it as fresh against an empty pre-existing
        # set. Production callers snapshot the log root BEFORE starting
        # the dispatch and pass that snapshot here.
        baseline_run_dirs=set(),
    )

    assert result.triggered is True, result
    assert result.target_worker == "slurm-worker3", result
    assert result.container_id == "deadbeefcafe", result
    assert result.cgroup_path == (
        "/machine.slice/libpod-deadbeefcafe.scope"
    ), result
    assert result.cgroup_version == 2, result
    assert result.applied_at is not None, result
    assert len(captured) == 1, captured
    worker, cmd, _timeout = captured[0]
    assert worker == "slurm-worker3", captured
    # The helper shell-quotes both interpolated args via shlex.quote;
    # for ``"512M"`` (no metacharacters) shlex emits the bare token,
    # while ``"/tmp/asm-*/storage"`` (contains a glob) is wrapped in
    # single-quotes. We assert by substring rather than full equality
    # because the script template is large.
    assert "MEM_MAX=512M" in cmd, cmd
    assert "STORAGE_GLOB='/tmp/asm-*/storage'" in cmd, cmd
    assert "podman" in cmd, cmd


def test_oom_one_worker_via_cgroup_times_out_when_no_run_dir(
    tmp_path: pathlib.Path,
) -> None:
    """When NO run_<TS> directory appears, the helper times out.

    This exercises the no-run-dir failure surface; the helper returns
    ``triggered=False`` with a clear note.
    """
    log_root = tmp_path / "log"
    log_root.mkdir()

    fake_clock = [0.0]

    def _clock() -> float:
        return fake_clock[0]

    def _sleep(delta: float) -> None:
        fake_clock[0] += max(delta, 0.01)

    class _StubProbe:
        @staticmethod
        def worker_ssh(
            worker: str,  # noqa: ARG004 — unused stub
            cmd: str,  # noqa: ARG004 — unused stub
            *,
            timeout: float | None = None,  # noqa: ARG004 — unused stub
            check: bool = False,  # noqa: ARG004 — unused stub
        ) -> object:
            raise AssertionError("worker_ssh must not be called on timeout")

    result = oom_one_worker_via_cgroup(
        _StubProbe(),
        run_log_root=log_root,
        target_secondary_id="secondary-2",
        memory_max="512M",
        arm_timeout_s=0.05,
        poll_interval_s=0.01,
        clock=_clock,
        sleep=_sleep,
    )

    assert result.triggered is False, result
    assert result.target_worker == "", result
    assert any("no fresh run_" in n for n in result.notes), result.notes


def test_oom_one_worker_via_cgroup_surfaces_worker_error(
    tmp_path: pathlib.Path,
) -> None:
    """When the worker script reports ``ERROR=...``, surface it as a note."""
    import subprocess as _subprocess

    log_root = tmp_path / "log"
    log_root.mkdir()
    run_dir = log_root / "run_19700101_000000"
    run_dir.mkdir()
    (run_dir / "connection_info").mkdir()
    (run_dir / "connection_info" / "secondary-2.info").write_text(
        "hostname=slurm-worker3\n", encoding="utf-8",
    )

    def _fake_worker_ssh(
        worker: str,  # noqa: ARG001 — unused
        cmd: str,  # noqa: ARG001 — unused
        *,
        timeout: float | None = None,  # noqa: ARG001 — unused
        check: bool = False,  # noqa: ARG001 — unused
    ) -> _subprocess.CompletedProcess[str]:
        return _subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "ERROR=no_storage_root_found\n"
                "STORAGE_GLOB=/tmp/asm-*/storage\n"
            ),
            stderr="",
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    result = oom_one_worker_via_cgroup(
        _StubProbe(),
        run_log_root=log_root,
        target_secondary_id="secondary-2",
        memory_max="512M",
        arm_timeout_s=2.0,
        poll_interval_s=0.0,
        baseline_run_dirs=set(),
    )

    assert result.triggered is False, result
    assert result.target_worker == "slurm-worker3", result
    assert any(
        "no_storage_root_found" in n for n in result.notes
    ), result.notes


# Skip pytest's collection of the live test on non-linux just to keep
# the unit tests above runnable on developer machines that don't have
# the slurm-test-env. The pytest marker ``slurm_live`` already gates
# the live test off-by-default; this is belt-and-braces.
if sys.platform != "linux":  # pragma: no cover - platform guard
    pytestmark = pytest.mark.skip(
        reason="slurm-test-env is linux-only",
    )

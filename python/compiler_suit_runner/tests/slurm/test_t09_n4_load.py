"""End-to-end T9 smoke: N=4 secondaries, large workload, against the live env.

T9 mirrors T3's shape (single-row dispatch + invariant audit) but pushes the
matrix to its load-smoke corner: four secondaries against the
``"large"`` workload, which ``default_invocation_for_smoke`` resolves to
``--variant-sample 4 --max-variants 50`` per ``run_helpers.py``. There
is no failure injection -- the row's purpose is to surface cache hit/miss
patterns, peer-mesh fan-out, and contention-class regressions that only
manifest when all four workers are doing real work concurrently.

The expected outcome is identical to T3's: clean exit, manifests for
every dispatched variant, no leaks. The framework computes the actual
build set from ``variant_sample x max_variants x archs`` so the on-disk
manifest count may be smaller than 50; we don't pin it -- invariant
check 3 (``manifest_count_matches_completed``) compares the manifest
directory against the per-run completion log line-by-line and is the
authoritative cross-check.

Operational quirks specific to this row (vs T3/T4):

* **Worker tolerance**: T9 wants four secondaries, but ``slurm-worker1``
  has been observed in DOWN+NOT_RESPONDING state independently of the
  framework. We require at least three of the four expected workers
  idle and fall back to ``jobs = available_idle_count`` if fewer than
  four are present. With <3 idle we skip rather than mask a real cluster
  outage. This makes the test "contention-tolerant" without being
  silently degraded: the assertion message records the actual jobs
  count used so a triager can tell at a glance whether a failure ran
  with N=3 or N=4.
* **CPU pinning**: ``slurm_cpus_per_task=2`` matches the local
  test-env's ``CPUTot=2`` per worker; sbatch's default 14 is rejected.
* **Hang detection**: parent observed dispatches stalling in the
  framework's primary-secondary connect loop on this cluster. The
  600s-default timeout is widened to 1800s for the load workload, but
  the test still treats a hang as a failure (timeout exit) -- it is
  emphatically *not* re-classified as a pass on timeout.

The probe construction follows T3's: a dedicated :class:`ClusterProbe`
with the explicit identity-file path, instead of the conftest's
``cluster_probe`` fixture, so the SSH key stays out of the agent and
``~/.ssh/`` (project policy: the slurm-test-env key is ephemeral).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Callable

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
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``. The path lives outside the worktree so signed-history
# operations stay free of the private key blob.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. ``invariants``
# checks 5-7 walk this list to look for leaks. Wired here as a literal
# (not derived from sinfo) so the leak audit covers every worker that
# *could* have been dispatched to, even if SLURM only assigned a subset.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Desired secondary count. The actual ``jobs`` value passed to the
# framework may be lower if fewer than four workers are idle (see
# ``MIN_IDLE_WORKERS`` and the pre-flight fallback in the test body).
DESIRED_N_SECONDARIES = 4

# Lower bound for the idle-worker pre-flight. T9's intent is contention
# testing under load, so we tolerate one missing worker
# (``slurm-worker1`` has been observed DOWN+NOT_RESPONDING for reasons
# unrelated to the framework). Below this floor we'd no longer be
# meaningfully exercising the contention path, so the test skips.
MIN_IDLE_WORKERS = 3

# Default wall-clock cap for the dispatch. The "large" workload runs up
# to ~50 variants of ``hello`` across four secondaries; cache-cold this
# can take 20-30 minutes including image pull on the first secondary.
# Override via ``T9_TIMEOUT_S=<seconds>`` if a particularly cold cache
# or a contended host pushes the run further.
DEFAULT_TIMEOUT_S = 1800.0


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    The conftest's session-scoped ``cluster_probe`` fixture does not
    set ``identity_file`` (see module docstring); for the live path we
    need it, so we instantiate locally. ``timeout=8.0`` matches the
    cluster_probe self-test in the same package.
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
    """Render every invariant result for a failed assertion message.

    The harness returns up to seven results (file-only 1-4 + cluster
    5-7); we list each on its own line with status + detail + a row
    count so the test failure surfaces enough context to triage
    without re-running.
    """
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _resolve_timeout() -> float:
    """Read ``T9_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored; we prefer the
    safer default to a confusing ``ValueError`` mid-test.
    """
    raw = os.environ.get("T9_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@pytest.mark.slurm_live
def test_t09_n4_load(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- wired via the B2 cleanup harness
) -> None:
    """Four-secondary large-workload dispatch with full invariant audit.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` of the four expected workers idle
    (we tolerate one missing for the known-DOWN ``slurm-worker1``
    case). Dispatch: large workload (~50 variants of ``hello``) via
    ``fresh_run`` so the incremental cache is wiped both sides of the
    call. The ``jobs`` count is reduced to the available idle worker
    count when fewer than four are idle; the assertion details record
    the actual N used.

    Post-flight: build :class:`RunArtifacts` from the captured run_dir
    and assert every invariant passes with ``expected_failure_count=0``.

    Failure surface: the assertion message lists every invariant's
    name + detail + row count so a CI run yields enough context to
    decide whether the failure is a test bug or a real framework
    regression. A wall-clock timeout (1800s default) is treated as a
    failure -- parent has observed dispatches hanging in the
    framework's primary-secondary connect loop on this cluster, so
    timing out must NOT be silently re-classified as a pass.
    """
    probe = _live_probe()

    # Reachability gate. ``is_reachable()`` swallows every conceivable
    # subprocess failure and returns False, so this is a safe pre-flight.
    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    # Pre-flight: cluster must be quiet at start. A non-empty squeue
    # would mean another caller (or a stale prior run) is still active;
    # we refuse to start so we don't pollute their leak audit.
    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T9 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    # Pre-flight: sinfo should show enough idle workers for a
    # meaningful load-smoke. We tolerate one missing worker (per the
    # plan: ``slurm-worker1`` may be DOWN+NOT_RESPONDING for reasons
    # unrelated to the framework) and fall back to ``jobs =
    # idle_count`` when fewer than four are idle. Below
    # :data:`MIN_IDLE_WORKERS` we skip rather than mask a real outage.
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
            f"T9 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    # Degraded-mode fallback: prefer the desired N=4, but drop to the
    # available idle count (>= MIN_IDLE_WORKERS) when a worker is out.
    # The test still exercises the multi-secondary contention path; the
    # invariant harness audits all four worker hosts regardless of how
    # many actually received a secondary, so leak detection stays
    # comprehensive.
    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # Compose the invocation. ``default_invocation_for_smoke(jobs=N,
    # workload="large")`` already pins ``packages=("hello",)``,
    # ``--variant-sample 4``, ``--max-variants 50``,
    # ``--slurm-partition debug`` and seeds the variant pick for
    # repeatability -- exactly the T9 row's ~50-variant shape. Two
    # overrides (same as T3):
    #
    # * ``ssh_identity_file`` -- so the framework's own gateway SSH
    #   uses the same explicit key as the probe (the default
    #   invocation leaves it None to keep the dataclass usable outside
    #   the test slice).
    # * ``slurm_cpus_per_task`` -- the local slurm-test-env workers
    #   each expose 2 CPUs; the framework's default sbatch request is
    #   14, which sbatch rejects with "CPU count per node can not be
    #   satisfied". 2 is the maximum that fits on a single worker.
    # * ``archs`` -- narrow to native x86_64 only. The cross-toolchain
    #   variants (aarch64-clang10, ...) drag in a heavy cross-LLVM
    #   build per arch that fork-storms past the worker's per-container
    #   process/memory budget on the 3.5 GiB slurm-test-env cap. T9's
    #   contention contract is "N=4 secondaries dispatching the same
    #   workload concurrently" — that's orthogonal to compiler family,
    #   so narrowing keeps the row contention-meaningful and inside the
    #   memory envelope.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="large"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    # Surface the dispatch wall-time + exit code in the assertion
    # message so a clean-but-failing run is easy to triage. The
    # ``n_secondaries`` value is recorded explicitly so a degraded-mode
    # run is unambiguous in the failure log.
    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"log_dir={result.log_dir!s}"
    )

    # A timeout in ``fresh_run`` is reported as a non-zero ``exit_code``
    # (the subprocess wrapper returns the timeout signal); we let the
    # standard exit-code assertion below catch it. The plan calls out
    # explicitly that hangs must NOT be re-classified as passes -- if
    # ``wall_time_s`` is at or near ``timeout_s`` and exit is non-zero
    # the message already surfaces both.
    assert result.exit_code == 0, (
        f"compiler_suit_runner submit returned non-zero ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.run_id is not None, (
        f"run_id not parsed from CLI output ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.log_dir is not None and result.log_dir.is_dir(), (
        f"log_dir missing or not a directory ({detail})"
    )

    # The framework's ``submit`` exits as soon as the local primary
    # finishes dispatching; the secondaries' sbatch jobs (and the
    # ``Container exited with code: 0`` lines in their slurm_*.out
    # files) may still be in flight when the CLI returns. Wait for
    # squeue to drain before running file-only invariants too --
    # otherwise check 1 (``clean_exit``) races the framework's last
    # writes to the slurm logs. ``run_all_invariants`` already waits
    # internally for checks 5-7, but checks 1-4 don't, so we gate
    # explicitly here. The 300s drain window matches T3's; even with
    # four secondaries the post-dispatch tail is dominated by per-
    # secondary container teardown rather than count.
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
        f"completed ({detail})"
    )

    # All seven invariants. ``run_all_invariants`` re-checks
    # ``wait_squeue_empty`` for the cluster checks; the second poll is
    # a fast no-op now that we drained above.
    artifacts = RunArtifacts.from_dir(
        result.log_dir, shared_fs=invocation.shared_fs,
    )
    results = run_all_invariants(
        artifacts,
        probe,
        WORKERS,
        expected_failure_count=0,
    )

    failed = [r for r in results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}:\n{_format_results(results)}"
    )

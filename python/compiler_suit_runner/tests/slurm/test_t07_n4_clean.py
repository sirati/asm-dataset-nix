"""End-to-end T7 smoke: N=4 secondaries, medium workload, full peer-mesh
audit against the live slurm-test-env.

T7's reason-to-exist is the multi-node specifics that no smaller row
exercises: the four-way peer mesh formed by the secondaries'
``PeerListWatcher`` instances, the per-secondary substituter file
shape, and the per-URL HTTP reachability of every peer's harmonia
binary cache. The standard 7 invariants (clean exit, no bind errors,
manifest count, build-failure floor, no-leaked containers / listener
ports / processes) ride along; on top of them T7 adds:

* :func:`compiler_suit_runner.tests.slurm.peer_mesh_assertions.assert_mesh_shape`
  -- per-file URL count, per-URL host/port format, per-file
  no-self-reference, submitter-URL consistency;
* :func:`compiler_suit_runner.tests.slurm.peer_mesh_assertions.probe_substituter_reachability`
  -- ``curl --max-time 5 <peer_url>nix-cache-info`` from each peer's
  worker, asserting HTTP 200.

T7 differs from T9 (the load smoke at N=4) in that the mesh assertions
fail the test loudly: a missing peer URL or an unreachable harmonia is
the bug T7 exists to catch.

Operational quirks specific to this row:

* **Worker tolerance**: the run wants four secondaries, but
  ``slurm-worker1`` has been observed in DOWN+NOT_RESPONDING state
  independently of the framework (carry-over from T9's test note).
  We tolerate the degraded N=3 case: the test still runs, the mesh
  assertions are made against the actual N (each remaining secondary
  expects N-1 = 2 peer URLs instead of 3), and the assertion message
  records the actual N used. Below MIN_IDLE_WORKERS we skip rather
  than mask a real cluster outage.
* **CPU pinning**: ``slurm_cpus_per_task=2`` matches the local
  test-env's ``CPUTot=2`` per worker; sbatch's default 14 is rejected.
* **Wall-clock cap**: 1500s default (override via ``T7_TIMEOUT_S``).
  Medium workload + four secondaries + harmonia / nix-daemon startup
  per worker fits comfortably in this budget cache-cold; load contention
  is T9's concern, not T7's.

The probe construction follows T3/T4/T9: a dedicated :class:`ClusterProbe`
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
from compiler_suit_runner.tests.slurm.peer_mesh_assertions import (
    ProbeResult,
    assert_mesh_shape,
    parse_mesh,
    probe_substituter_reachability,
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

# Lower bound for the idle-worker pre-flight. T7's intent is the
# multi-node mesh contract, so we tolerate one missing worker
# (``slurm-worker1`` may be DOWN+NOT_RESPONDING for reasons unrelated
# to the framework). Below this floor the N=2 case is what T4 already
# covers, so T7 skips rather than degrade further.
MIN_IDLE_WORKERS = 3

# Default wall-clock cap. The medium workload runs ~10 variants of
# ``hello`` across four secondaries; cache-cold this fits in 1500s
# including image pull on the first secondary per worker. Override
# via ``T7_TIMEOUT_S=<seconds>`` if a particularly cold cache pushes
# the run further.
DEFAULT_TIMEOUT_S = 1500.0


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
    """Render every invariant result for a failed assertion message."""
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _format_probe_results(results: list[ProbeResult]) -> str:
    """Render every reachability probe result for a failed assertion."""
    lines: list[str] = []
    for r in results:
        tag = "PASS" if (r.error is None and r.http_status == 200) else "FAIL"
        priority = "P" if r.has_priority_header else "-"
        status = "?" if r.http_status is None else str(r.http_status)
        err = f" err={r.error!r}" if r.error else ""
        lines.append(
            f"  [{tag}] {r.secondary_id} {r.url} status={status} "
            f"priority={priority}{err}"
        )
    return "\n".join(lines)


def _resolve_timeout() -> float:
    """Read ``T7_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored; we prefer the
    safer default to a confusing ``ValueError`` mid-test.
    """
    raw = os.environ.get("T7_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@pytest.mark.slurm_live
def test_t07_n4_clean(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- wired via the B2 cleanup harness
) -> None:
    """N=4 medium-workload dispatch with full invariant + peer-mesh audit.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` of the four expected workers
    idle. The test tolerates one missing worker (degraded N=3 path
    -- documented inline below) but skips below the floor.

    Dispatch: medium workload (~10 variants) via ``fresh_run`` so the
    incremental cache is wiped both sides of the call. The ``jobs``
    count is reduced to the available idle worker count when fewer
    than four are idle.

    Post-flight: build :class:`RunArtifacts` from the captured run_dir
    and assert every standard invariant passes with
    ``expected_failure_count=0``. Then parse the peer mesh from
    ``<run_dir>/peers/`` and assert:

    * mesh shape matches the actual N (each secondary lists N-1 peer
      URLs, port matches harmonia's, no self-reference, submitter URL
      consistent if published);
    * each peer URL is HTTP-200 reachable from its hosting worker.

    Failure surface: assertion messages list every invariant /
    probe with status + detail so a CI run yields enough context to
    triage without re-running. A wall-clock timeout (1500s default)
    is treated as a failure -- timing out must NOT be silently
    re-classified as a pass.
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
        f"squeue --me must be empty at T7 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    # Pre-flight: sinfo should show enough idle workers for a
    # meaningful multi-node run. We tolerate one missing worker
    # (``slurm-worker1`` may be DOWN+NOT_RESPONDING) and fall back to
    # ``jobs = idle_count`` when fewer than four are idle. Below
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
            f"T7 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    # Degraded-mode fallback. Prefer the desired N=4 multi-node mesh,
    # but accept the N=3 fallback when a worker is out -- the mesh
    # assertions then verify the smaller (still multi-node) mesh shape:
    # each remaining secondary expects N-1 = 2 peer URLs, all probes
    # land on the available worker set. The test still exercises every
    # T7 axis (peer-mesh formation, URL format, reachability); only
    # the per-file URL count expectation changes.
    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # Compose the invocation. ``default_invocation_for_smoke(jobs=N,
    # workload="medium")`` already pins ``packages=("hello",)``,
    # ``--variant-sample 2``, ``--max-variants 10``,
    # ``--slurm-partition debug`` and seeds the variant pick for
    # repeatability -- exactly the T7 row's ~10-variant shape. Two
    # overrides:
    #
    # * ``ssh_identity_file`` -- so the framework's own gateway SSH
    #   uses the same explicit key as the probe (the default
    #   invocation leaves it None to keep the dataclass usable outside
    #   the test slice).
    # * ``slurm_cpus_per_task`` -- the local slurm-test-env workers
    #   each expose 2 CPUs; the framework's default sbatch request is
    #   14, which sbatch rejects with "CPU count per node can not be
    #   satisfied". 2 is the maximum that fits on a single worker.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
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
    # standard exit-code assertion below catch it. Hangs MUST NOT be
    # re-classified as passes -- if ``wall_time_s`` is at or near
    # ``timeout_s`` and exit is non-zero the message already surfaces
    # both.
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
    # explicitly here. The 300s drain window matches T9's; even with
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
    inv_results = run_all_invariants(
        artifacts,
        probe,
        WORKERS,
        expected_failure_count=0,
    )

    failed = [r for r in inv_results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}:\n{_format_results(inv_results)}"
    )

    # ------------------------------------------------------------------
    # T7-specific assertions: peer-mesh shape + reachability.
    # ------------------------------------------------------------------
    # Parse the mesh from the run dir. The parser raises
    # ``MeshAssertionError`` (subclass of AssertionError) on a malformed
    # substituter file, which fails the test directly with a diagnostic
    # message; ``parse_mesh`` does NOT count peer URLs -- that's
    # ``assert_mesh_shape``'s job.
    mesh = parse_mesh(result.log_dir, n_secondaries=n_secondaries)

    # Shape assertion. ``require_submitter`` is left at its default
    # (``False``): the dev-box submitter publishes its peer file
    # asynchronously and may not have landed before all secondaries
    # finished; we accept either presence (verified for consistency)
    # or absence. ``expected_harmonia_port`` is left at None so the
    # parser-derived value is used; the current image's harmonia binds
    # 5000.
    assert_mesh_shape(mesh, n_secondaries=n_secondaries)

    # Reachability probe. Every peer URL (excluding the submitter, by
    # default) is curl'd from its hosting worker. We assert all probes
    # report HTTP 200; the ``Priority`` header is a softer signal
    # surfaced in the failure message but not asserted hard (older
    # harmonia builds emitted slightly different headers; the canonical
    # check is the status code).
    probe_results = probe_substituter_reachability(probe, mesh)
    bad_probes = [
        r for r in probe_results
        if r.error is not None or r.http_status != 200
    ]
    assert not bad_probes, (
        f"peer-mesh reachability probe(s) failed for {detail}:\n"
        + _format_probe_results(probe_results)
    )

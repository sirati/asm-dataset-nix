"""End-to-end T3 smoke: N=1 secondary, medium workload, against the live env.

T3 mirrors T1's shape (same single-secondary dispatch, same gateway, same
invariant audit) but exercises a *medium* workload --
``default_invocation_for_smoke(jobs=1, workload="medium")`` resolves to
``--variant-sample 2 --max-variants 10`` per ``run_helpers.py``, i.e. up
to ten variants of ``hello`` rather than T1's single one. The expected
outcome is unchanged: clean exit, manifests for every dispatched
variant, no leaks.

The wider workload primarily stresses the per-item worker protocol and
the manifest emission path -- if a regression slows manifest fan-out or
desyncs item completion logging, T1 will still pass while T3 surfaces
the issue. Therefore the wall-clock cap is bumped to 1200s; override
via ``T3_TIMEOUT_S`` if a particularly cold cache is expected.

The probe construction follows T1: a dedicated :class:`ClusterProbe`
with the explicit identity-file path is used instead of the conftest's
``cluster_probe`` fixture so the SSH key stays out of the agent and
``~/.ssh/`` (project policy: the slurm-test-env key is ephemeral). See
``test_t01_clean_tiny.py`` for the rationale at length.
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
from compiler_suit_runner.tests.slurm.placement_assertions import (
    assert_placement_files_present_and_nonempty as _assert_placement_files_present_and_nonempty,
    assert_targeted_nix_copy_in_secondary_logs as _assert_targeted_nix_copy_in_secondary_logs,
    assert_validate_manifests_emitted as _assert_validate_manifests_emitted,
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
# (not derived from sinfo) so the test fails noisily if the env shape
# changes -- the same workers are referenced in the plan file.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap for the dispatch. T3 exercises ~10 variants on
# a single secondary so we double T1's 600s floor; override via
# ``T3_TIMEOUT_S=<seconds>`` if a cold cache or slow image pull is
# expected to push the run past 20 minutes.
DEFAULT_TIMEOUT_S = 1200.0


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
    """Read ``T3_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored; we prefer the
    safer default to a confusing ``ValueError`` mid-test.
    """
    raw = os.environ.get("T3_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@pytest.mark.slurm_live
def test_t03_clean_medium(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- placeholder; B2 wires the real harness
) -> None:
    """Single-secondary medium-workload dispatch with full invariant audit.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    the four expected idle nodes. Dispatch: medium workload (~10
    variants of ``hello``) via ``fresh_run`` so the incremental cache
    is wiped both sides of the call. Post-flight: build
    :class:`RunArtifacts` from the captured run_dir and assert every
    invariant passes with ``expected_failure_count=0``.

    Failure surface: the assertion message lists every invariant's
    name + detail + row count, so a CI run yields enough context to
    decide whether the failure is a test bug or a real framework
    regression.
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
        f"squeue --me must be empty at T3 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    # Pre-flight: sinfo should show the four expected workers idle.
    # We tolerate the partition column varying (``debug`` vs ``debug*``
    # depending on SLURM version) and only insist on per-node presence
    # + ``idle`` state.
    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    # T3 runs with jobs=1 so we only need at least one idle worker.
    # Tolerate the rest being down/drained (eg. ``slurm-worker1``
    # landing in DOWN+NOT_RESPONDING after a forced cleanup) so a
    # single bad worker doesn't hide an otherwise-healthy harness.
    idle = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    assert idle, "no idle workers: " + ", ".join(
        f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
    )

    # Compose the invocation. ``default_invocation_for_smoke(jobs=1,
    # workload="medium")`` already pins ``packages=("hello",)``,
    # ``--variant-sample 2``, ``--max-variants 10``,
    # ``--slurm-partition debug`` and seeds the variant pick for
    # repeatability -- exactly the T3 row's "~10 variants" shape. Two
    # overrides (same as T1):
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
        default_invocation_for_smoke(jobs=1, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)
    # Dump full dispatcher stdout/stderr to a file for offline triage.
    # This is a temporary diagnostic for the
    # primary-exit-reason mystery (Path A/B/C); remove once root-cause is
    # identified.
    import pathlib as _pl
    _dump = _pl.Path("/tmp/t03-disp-full.log")
    _dump.write_text(
        "=== STDOUT ===\n" + result.stdout + "\n=== STDERR ===\n" + result.stderr
    )

    # Surface the dispatch wall-time + exit code in the assertion
    # message so a clean-but-failing run is easy to triage.
    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"log_dir={result.log_dir!s}"
    )

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
    # finishes dispatching; the secondary's sbatch job (and the
    # ``Container exited with code: 0`` line in slurm_*.out) may still
    # be in flight when the CLI returns. Wait for squeue to drain
    # before running file-only invariants too -- otherwise check 1
    # ("clean_exit") races the framework's last writes to the slurm
    # log. ``run_all_invariants`` already waits internally for checks
    # 5-7, but checks 1-4 don't, so we gate explicitly here. The drain
    # window is a touch wider than T1's because medium runs can leave
    # the secondary post-processing manifests for tens of seconds
    # after the primary disconnects.
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

    # ------------------------------------------------------------------
    # Placement-map plumbing (cluster-wide ``dict[outpath, set[sid]]``).
    # These assertions exercise the targeted-fetch refactor:
    #   * manifests should be ``phase2_toolchain_validate`` (no build-on-
    #     secondaries) — only path-info + nix-copy from a peer.
    #   * the on-disk gossip ``peers/_paths_<sid>.jsonl`` files should
    #     exist post-run and reference the toolchain outpaths.
    #   * secondary logs should show ``nix copy --from http://...
    #     --no-check-sigs`` invocations (point-to-point, no fanout).
    # All four are file-only inspections of state the run left behind;
    # nothing needs a live cluster connection at this stage.
    _assert_validate_manifests_emitted(artifacts)
    _assert_placement_files_present_and_nonempty(artifacts)
    _assert_targeted_nix_copy_in_secondary_logs(artifacts)

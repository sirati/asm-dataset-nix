"""End-to-end T1 smoke: N=1 secondary against the live slurm-test-env.

This is the smallest possible compiler_suit_runner dispatch the matrix
exercises -- 1 secondary, 1 toolchain x 1 variant of ``hello``, no
failure injection. The expected outcome is a clean run with all seven
per-run invariants passing.

The test does NOT mock anything: it spawns a real
``python -m compiler_suit_runner submit`` against the local
slurm-test-env (gateway on ``ssh://sirati@localhost:2244``) and waits
for the dispatch to settle. The 600s wall-clock cap is configurable
via ``T1_TIMEOUT_S`` for the rare retry scenario where the gateway is
slow to flush the first slurm log file.

Why a dedicated ``ClusterProbe`` instance instead of the
``cluster_probe`` fixture: the conftest fixture currently constructs
``ClusterProbe()`` with no ``GatewayConfig`` override, which leaves
``identity_file=None``. Per project policy the slurm-test-env key is
ephemeral (must NOT be added to ssh-agent or ``~/.ssh/``), so SSH only
works when the test passes ``-i <key>`` explicitly. A T1-local probe
with the right ``GatewayConfig`` is the documented work-around in this
worktree's instructions; switching the conftest fixture itself is
sibling B2's responsibility.
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
# (not derived from sinfo) so the test fails noisily if the env shape
# changes -- the same workers are referenced in the plan file.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap for the dispatch. 600s mirrors the invariant-
# harness floor; override via ``T1_TIMEOUT_S=<seconds>`` if a
# particularly cold cache or slow image pull is expected.
DEFAULT_TIMEOUT_S = 600.0


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    The conftest's session-scoped ``cluster_probe`` fixture does not
    set ``identity_file`` (see module docstring); for the live path
    we need it, so we instantiate locally. ``timeout=8.0`` matches the
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
    """Read ``T1_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored; we prefer the
    safer default to a confusing ``ValueError`` mid-test.
    """
    raw = os.environ.get("T1_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@pytest.mark.slurm_live
def test_t01_clean_tiny(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- placeholder; B2 wires the real harness
) -> None:
    """Single-secondary clean-path dispatch with full invariant audit.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    the four expected idle nodes. Dispatch: tiny workload (1 variant)
    via ``fresh_run`` so the incremental cache is wiped both sides of
    the call. Post-flight: build :class:`RunArtifacts` from the
    captured run_dir and assert every invariant passes with
    ``expected_failure_count=0``.

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
    # would mean another caller (or a stale T0 run) is still active;
    # we refuse to start so we don't pollute their leak audit.
    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T1 start; found {len(queued)} "
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
    not_idle = [
        w for w in WORKERS
        if not sinfo_by_node[w].state.startswith("idle")
    ]
    assert not not_idle, "sinfo nodes not idle: " + ", ".join(
        f"{w}={sinfo_by_node[w].state!r}" for w in not_idle
    )

    # Compose the invocation. ``default_invocation_for_smoke(jobs=1,
    # workload="tiny")`` already pins ``packages=("hello",)``,
    # ``--variant-sample 1``, ``--max-variants 1``,
    # ``--slurm-partition debug`` and seeds the variant pick for
    # repeatability -- exactly the T1 row's ``1 toolchain x 1 variant``
    # shape. Two overrides:
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
        default_invocation_for_smoke(jobs=1, workload="tiny"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

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
    # 5-7, but checks 1-4 don't, so we gate explicitly here.
    drained = wait_squeue_empty(probe, timeout_s=180.0)
    assert drained, (
        f"squeue --me did not drain within 180s after dispatch "
        f"completed ({detail})"
    )

    # All seven invariants. ``run_all_invariants`` re-checks
    # ``wait_squeue_empty`` for the cluster checks; the second poll is
    # a fast no-op now that we drained above.
    artifacts = RunArtifacts.from_dir(result.log_dir)
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

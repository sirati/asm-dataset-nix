"""End-to-end T4 smoke: N=2 secondaries against the live slurm-test-env.

Two secondaries, the smallest possible ``--variant-sample`` workload, no
failure injection. The expected outcome is a clean run with all seven
per-run invariants passing AND a basic peer-mesh sanity check showing
that both secondaries registered themselves and published a per-secondary
substituters file.

This is the "basic" mesh check; full peer-mesh assertions (URL format,
per-port reachability, exact peer-count semantics under N=4) are the
scope of T7 (`test_t07_n4_clean.py`) which will introduce
`peer_mesh_assertions.py`. Here we only verify:

* Both secondaries' `_substituters.secondary-N.txt` files exist under
  the run's ``peers/`` directory.
* Each substituters file is non-empty and carries the expected 4-line
  shape (``--extra-substituters`` / URLs / ``--extra-trusted-public-keys``
  / keys).
* Each substituters file lists at least one peer URL (the primary's
  harmonia is normally also listed; T7 will pin the exact URL count and
  format).
* ``peers/secondary-0.json`` and ``peers/secondary-1.json`` are both
  present, demonstrating that both secondaries successfully announced
  themselves to the shared peer dir.

A dedicated ``ClusterProbe`` instance is constructed locally (rather
than using the conftest fixture) for the same reason as T1: the
fixture leaves ``identity_file=None`` and the slurm-test-env key is
ephemeral — must be passed via ``-i`` explicitly.
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
# ephemeral: never added to ssh-agent or ``~/.ssh/``; always passed via
# ``-i``. The path lives outside the worktree so signed-history
# operations stay free of the private key blob.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. ``invariants``
# checks 5-7 walk this list to look for leaks. T4 runs N=2 secondaries
# so SLURM picks two of the four; we still pass all four to the leak
# audit because we don't know in advance which two were used.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap. 600s mirrors the invariant-harness floor for
# a tiny workload; override via ``T4_TIMEOUT_S=<seconds>`` if a slow
# image-pull or cold cache pushes the run past the default.
DEFAULT_TIMEOUT_S = 600.0

# Number of secondaries this row dispatches. Carried as a constant so
# the basic-mesh assertions stay readable.
N_SECONDARIES = 2


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


def _resolve_timeout() -> float:
    """Read ``T4_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored; we prefer the
    safer default to a confusing ``ValueError`` mid-test.
    """
    raw = os.environ.get("T4_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _assert_basic_peer_mesh(
    run_dir: pathlib.Path, n_secondaries: int,
) -> None:
    """Basic peer-mesh sanity for an N-secondary run.

    Asserts the minimal "mesh formed" signal expected for any
    multi-secondary clean run:

    * Each secondary registered a ``peers/secondary-<N>.json`` file
      (so its peers were discoverable by the others).
    * Each secondary's ``peers/_substituters.secondary-<N>.txt`` file
      exists, is non-empty, and carries the canonical 4-line shape
      emitted by ``compiler_suit_runner.peer_cache._write_substituters_file``
      (``--extra-substituters`` / URLs / ``--extra-trusted-public-keys``
      / keys).
    * Each substituters file has at least one peer URL on its
      URLs line. The exact count (primary-only vs primary + peer
      secondaries) is intentionally NOT asserted here — T7 owns the
      precise count + URL-format checks via a dedicated
      ``peer_mesh_assertions`` module.

    On any failure the assertion message lists the run-dir-relative
    paths so a CI run yields enough context to triage without
    re-reading the logs by hand.
    """
    peers_dir = run_dir / "peers"
    assert peers_dir.is_dir(), (
        f"peers/ dir missing under {run_dir!s}; "
        "peer mesh did not initialise"
    )

    # Per-secondary peer-registration JSON files.
    expected_jsons = [
        peers_dir / f"secondary-{i}.json" for i in range(n_secondaries)
    ]
    missing_jsons = [p for p in expected_jsons if not p.is_file()]
    assert not missing_jsons, (
        "missing per-secondary peer registration file(s): "
        + ", ".join(str(p.relative_to(run_dir)) for p in missing_jsons)
    )

    # Per-secondary substituters files.
    expected_subs = [
        peers_dir / f"_substituters.secondary-{i}.txt"
        for i in range(n_secondaries)
    ]
    missing_subs = [p for p in expected_subs if not p.is_file()]
    assert not missing_subs, (
        "missing per-secondary substituters file(s): "
        + ", ".join(str(p.relative_to(run_dir)) for p in missing_subs)
    )

    # Shape check. ``_write_substituters_file`` emits one arg per line
    # and ``build_nix_extra_args`` always returns 4 args (or zero) when
    # there is at least one peer: ``--extra-substituters``, the
    # space-joined URL list, ``--extra-trusted-public-keys``, the
    # space-joined key list.
    for sub_path in expected_subs:
        rel = sub_path.relative_to(run_dir)
        text = sub_path.read_text(encoding="utf-8", errors="replace")
        assert text.strip(), f"substituters file {rel!s} is empty"

        lines = text.splitlines()
        assert len(lines) >= 4, (
            f"substituters file {rel!s} has only {len(lines)} line(s), "
            f"expected the 4-line --extra-substituters/URLs/"
            f"--extra-trusted-public-keys/keys shape; content:\n{text!r}"
        )
        assert lines[0] == "--extra-substituters", (
            f"substituters file {rel!s} line 1 is {lines[0]!r}, "
            f"expected '--extra-substituters'"
        )
        assert lines[2] == "--extra-trusted-public-keys", (
            f"substituters file {rel!s} line 3 is {lines[2]!r}, "
            f"expected '--extra-trusted-public-keys'"
        )

        # Line 2 carries one or more space-separated peer URLs. Counting
        # tokens, not asserting URL format (that's T7's scope).
        url_tokens = lines[1].split()
        assert len(url_tokens) >= 1, (
            f"substituters file {rel!s} line 2 has zero peer URLs; "
            f"content:\n{text!r}"
        )


@pytest.mark.slurm_live
def test_t04_n2_clean(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 — wired via the B2 cleanup harness
) -> None:
    """Two-secondary clean-path dispatch with full invariant audit and
    basic peer-mesh sanity.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least two of the four expected idle nodes (SLURM picks two at
    dispatch time; we don't pin which). Dispatch: tiny workload via
    ``fresh_run`` so the incremental cache is wiped both sides of the
    call. Post-flight: build :class:`RunArtifacts` from the captured
    run_dir, assert every invariant passes with
    ``expected_failure_count=0``, and run :func:`_assert_basic_peer_mesh`
    for the per-secondary substituters/registration files.

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
    # would mean another caller (or a stale earlier run) is still
    # active; we refuse to start so we don't pollute their leak audit.
    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T4 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    # Pre-flight: sinfo should show at least N_SECONDARIES idle nodes
    # of the four expected workers. Tolerate the partition column
    # varying (``debug`` vs ``debug*`` depending on SLURM version) and
    # only insist on per-node presence + ``idle`` state.
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
        f"need at least {N_SECONDARIES} idle worker(s) for T4; "
        "got states: "
        + ", ".join(
            f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
        )
    )

    # Compose the invocation. ``default_invocation_for_smoke(jobs=2,
    # workload="tiny")`` pins ``packages=("hello",)``,
    # ``--variant-sample 1``, ``--max-variants 1``, ``--slurm-partition
    # debug`` and seeds the variant pick for repeatability. Three
    # overrides:
    #
    # * ``ssh_identity_file`` -- so the framework's own gateway SSH
    #   uses the same explicit key as the probe (the default
    #   invocation leaves it None to keep the dataclass usable outside
    #   the test slice).
    # * ``slurm_cpus_per_task=2`` -- the local slurm-test-env workers
    #   each expose 2 CPUs; the framework's default sbatch request is
    #   14, which sbatch rejects with "CPU count per node can not be
    #   satisfied". 2 is the maximum that fits on a single worker.
    # * ``variant_sample`` / ``max_variants`` scaled to ``N_SECONDARIES``
    #   workers' worth of variants. The ``"tiny"`` workload's default
    #   of one total variant is tiny *per-cluster*, not tiny
    #   *per-secondary*: with a single in-flight variant only one
    #   secondary ever receives work, the other parks on election, and
    #   when the working secondary exits the parked one gets stuck in
    #   the post-promotion hang reproduced by T2. We allocate enough
    #   variants for both secondaries to receive at least one
    #   assignment (``2 * N_SECONDARIES`` variants spread across the
    #   sampled toolchains) so both follow the clean-exit code path.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=N_SECONDARIES, workload="tiny"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        variant_sample=2 * N_SECONDARIES,
        max_variants=2 * N_SECONDARIES,
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
    # finishes dispatching; the two secondary sbatch jobs (and the
    # ``Container exited with code: 0`` lines in their slurm_*.out
    # files) may still be in flight when the CLI returns. Wait for
    # squeue to drain before running file-only invariants too --
    # otherwise check 1 (``clean_exit``) races the framework's last
    # writes to the slurm logs. ``run_all_invariants`` already waits
    # internally for checks 5-7, but checks 1-4 don't, so we gate
    # explicitly here.
    drained = wait_squeue_empty(probe, timeout_s=180.0)
    assert drained, (
        f"squeue --me did not drain within 180s after dispatch "
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

    # Basic peer-mesh sanity. T7 will introduce a dedicated module
    # (``peer_mesh_assertions.py``) with the full URL-format + count
    # semantics; here we only verify the per-secondary files exist
    # with the canonical shape.
    _assert_basic_peer_mesh(result.log_dir, N_SECONDARIES)

"""End-to-end T11 smoke: K=3 toolchain replication on N=4 secondaries.

T11 exercises the receive-side cascade of the K=3 plan: after a single
``submit`` finishes, every toolchain outpath must be held by at least
K distinct secondaries (default K=3). The cluster only has four
worker nodes, so the asymptotic upper bound is 4 holders (race
outcome — acceptable per plan).

What this test asserts:

* Run completes cleanly (standard 7-invariant audit).
* Validate-only manifests emitted (no build-on-secondary).
* Placement-map gossip files contain the toolchain outpath under
  **≥ K=3 distinct secondaries** post-run. This is the K=3 cascade
  succeeding through ``ReplicationSender`` + ``Receiver`` +
  the ``ReplicationRepairWorker.on_diff`` cascade path.
* Secondary logs show ``path-offer`` / ``path-accept`` events
  consistent with the handshake protocol (best-effort grep — the
  log shape is informational, the placement-count assertion is the
  hard one).

What this test does NOT yet assert (deferred):

* **Death-test**: kill one of the K=3 holders mid-run and verify the
  remaining holders run repair-on-death back up to ≥ K. Requires a
  ``scancel`` injection helper that triggers AFTER cascade has
  converged. Future T12 (or extension to T11) once the framework
  peer-removed hook (Ask #4) is shipped — the fallback NFS-poll
  signal is too slow to verify reliably in a 5-min CI window.
* **Race-test**: kill two holders simultaneously to exercise the
  ``already-targeted`` reject coordination. Same blocker as above.

Builds on T07's N=4 shape: same probe construction, same WORKERS
list, same invocation modulo the explicit ``--replication-k=3`` knob.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import pathlib
import re
from typing import Callable

import pytest

from compiler_suit_runner.peer_cache import PATHS_FILE_PREFIX
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
    assert_validate_manifests_emitted as _assert_validate_manifests_emitted,
    parse_placement_records,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    default_invocation_for_smoke,
)


LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# The K=3 cascade needs at least K=3 alive secondaries for the
# invariant to be satisfiable; T11 runs with N=4 to leave headroom for
# the K-1=2 push-on-receive cascade plus a fourth holder from the race
# tolerance.
N_SECONDARIES = 4
MIN_IDLE_WORKERS = 3  # tolerate one bad worker; cascade can still reach K=3
REPLICATION_K = 3

DEFAULT_TIMEOUT_S = 1500.0

# Regex matching a path-offer / path-accept event in secondary logs.
# The exact log line shape depends on the runtime's structured
# logging; we match the JSON key names rather than positional fields
# so a logging refactor doesn't silently break the assertion.
_PATH_OFFER_RE = re.compile(r"path[-_]offer", re.IGNORECASE)
_PATH_ACCEPT_RE = re.compile(r"path[-_]accept", re.IGNORECASE)


def _live_probe() -> ClusterProbe:
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _format_results(results: list[InvariantResult]) -> str:
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _resolve_timeout() -> float:
    raw = os.environ.get("T11_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _collect_holder_counts(
    shared_fs: pathlib.Path,
) -> dict[str, set[str]]:
    """Aggregate placement files into ``{outpath: {secondary_id, ...}}``.

    Reads every ``peers/_paths_<sid>.jsonl`` in *shared_fs/peers/*
    and produces the cluster-wide placement map as it stood when the
    test reads it (post-run; the manager processes have already
    exited but the gossip files persist on the NFS mount).
    """
    peers_dir = shared_fs / "peers"
    placements: dict[str, set[str]] = collections.defaultdict(set)
    for paths_file in peers_dir.glob(f"{PATHS_FILE_PREFIX}*.jsonl"):
        for rec in parse_placement_records(paths_file):
            sid = rec.get("secondary_id")
            outpath = rec.get("outpath")
            item_class = rec.get("item_class")
            if (
                isinstance(sid, str)
                and isinstance(outpath, str)
                and outpath.startswith("/nix/store/")
                and item_class == "toolchain"
            ):
                placements[outpath].add(sid)
    return dict(placements)


def _grep_handshake_events(log_dir: pathlib.Path) -> dict[str, int]:
    """Count path-offer / path-accept mentions across secondary logs.

    Returns a ``{"offer": N, "accept": M}`` dict. The exact log shape
    is best-effort (depends on the runtime's logging emission); a
    zero count is informational, not a hard failure.
    """
    counts = {"offer": 0, "accept": 0}
    if not log_dir or not log_dir.is_dir():
        return counts
    for log_path in log_dir.rglob("slurm_*.out"):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        counts["offer"] += len(_PATH_OFFER_RE.findall(text))
        counts["accept"] += len(_PATH_ACCEPT_RE.findall(text))
    return counts


@pytest.mark.slurm_live
def test_t11_k3_replication_cascade(
    cluster_probe: ClusterProbe,  # noqa: ARG001
    slurm_log_root: pathlib.Path,  # noqa: ARG001
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001
) -> None:
    """N=4 secondaries; one toolchain; assert ≥ K=3 holders post-run.

    The cascade is driven by ``ReplicationSender.push_attempt`` in
    response to either:

      * receive-side: the placement watcher saw OUR secondary become
        a new toolchain holder (after ``record_self_has``);
        ``ReplicationRepairWorker.on_diff`` looked up the item_class
        via ``list_self_placements`` and fired push_attempt.
      * repair-side: not exercised by this test (no peer death).

    The cluster only has 4 workers, so the upper bound on holders is
    4 (race outcome per plan). We assert ``>= K=3``; equality at 3 or
    4 is acceptable.
    """
    probe = _live_probe()

    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244"
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T11 start; found {len(queued)} "
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
            f"T11 needs at least {MIN_IDLE_WORKERS} idle worker(s); "
            f"got {len(idle)} ({idle})"
        )

    # Single-arch run keeps the toolchain count low so the cascade
    # assertion is unambiguous (one outpath, one expected holder set).
    # ``--replication-k 3`` is the default but pinned explicitly so
    # any future default-bump shows up as a test-side change rather
    # than a silent behaviour shift.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=N_SECONDARIES, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
        extra_args=("--replication-k", str(REPLICATION_K)),
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={N_SECONDARIES} "
        f"k={REPLICATION_K} "
        f"log_dir={result.log_dir!s}"
    )

    assert result.exit_code == 0, (
        f"compiler_suit_runner submit returned non-zero ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.log_dir is not None and result.log_dir.is_dir(), (
        f"log_dir missing or not a directory ({detail})"
    )

    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
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
        f"invariant(s) failed for {detail}:\n{_format_results(inv_results)}"
    )

    # Placement-map baseline checks (same as T03/T07).
    _assert_validate_manifests_emitted(artifacts)
    _assert_placement_files_present_and_nonempty(artifacts)

    # ------------------------------------------------------------------
    # T11 core assertion: K=3 holder count per toolchain outpath.
    # ------------------------------------------------------------------
    placements = _collect_holder_counts(invocation.shared_fs)
    assert placements, (
        f"no toolchain placement records under "
        f"{invocation.shared_fs / 'peers'}; the cascade can't run if "
        f"record_self_has didn't fire. ({detail})"
    )

    under_replicated = [
        (outpath, sorted(holders))
        for outpath, holders in placements.items()
        if len(holders) < REPLICATION_K
    ]
    assert not under_replicated, (
        f"K=3 replication failed for {detail}; "
        f"{len(under_replicated)} toolchain(s) had < {REPLICATION_K} "
        f"holders:\n" + "\n".join(
            f"  {op}: {holders} (len={len(holders)})"
            for op, holders in under_replicated
        ) + (
            "\nFull placement map (post-run):\n" + "\n".join(
                f"  {op}: {sorted(h)}" for op, h in placements.items()
            )
        )
    )

    # Informational: log the event counts for triage; not a hard
    # assertion because the log shape depends on the runtime.
    events = _grep_handshake_events(result.log_dir)
    if events["offer"] == 0 and events["accept"] == 0:
        pytest.warns(  # NOT an assert — soft signal only
            UserWarning,
            match="no handshake events in logs",
        )

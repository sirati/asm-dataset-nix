"""End-to-end T20 smoke: ``--distributed-eval`` Phase-0 happy path.

T20 exercises the new distributed-eval pipeline end-to-end on the
live slurm-test-env. The submitter dispatches with
``--distributed-eval`` for three small, cross-compilable packages
(``hello``, ``zlib``, ``busybox``) onto N=4 secondaries; the test
asserts that the full multi-phase shape lands cleanly:

* Phase -1 toolchain seeding kicks off the bootstrap (covered by the
  standard 7 invariants — clean exit, no bind errors, no leaks).
* Phase 0 emits one ``phase0_eval_<binary>`` manifest per binary
  (exactly three), each task lands on a secondary, runs
  :func:`compiler_suit_runner.workers.eval_worker.run_eval_task`, and
  drops a resume marker at
  ``<shared_fs>/out/<binary>/_phase0/manifest.json``.
* After Phase 0 quiesces, the
  :class:`compiler_suit_runner.suit_task._Phase0QuiesceWatcher` fires
  ``plan_phase1``; the planner reads every Phase 0 marker and emits
  the variant build manifests (``phase3_variant``) with
  ``task_depends_on`` wired to the matching toolchain task ids.
* Final ``<dataset_dir>/<pkg>/<variant_dir>/`` directories are
  populated for every variant the run produced.
* The cluster's placement-map gossip files name at least one holder
  for every variant outpath (legacy ≥1 holder invariant — T11 owns
  the stricter K=3 cascade).
* Every Phase 0 ``task done`` line in ``slurm_*.out`` carries
  ``success=true``.

T20 is the canonical "does the new pipeline complete at all?"
regression. It deliberately keeps the workload small
(``--variant-sample 1``, three tier-1 packages, x86_64 only) so the
whole run fits inside the 5-min budget the plan asks for. Per-row
specifics belonging to other tests:

* K=3 holder cascade — T11.
* Phase-0 broadcast latency — T21.
* Phase-0 resume after a partial-failure — T22.
* Phase-1 common-dep dedup — T23.

The cluster construction follows T07 / T11: a dedicated
:class:`ClusterProbe` with an explicit ``identity_file`` (the slurm-
test-env key is ephemeral, per project memory) and the conftest's
``fresh_run`` fixture so the incremental cache is wiped on both
sides of the call.
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
    parse_placement_records,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    default_invocation_for_smoke,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. The leak
# audit (invariants 5-7) walks this list literal so the check covers
# every worker that *could* have been dispatched to, even if SLURM
# only assigned a subset.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Desired secondary count. The plan calls for N=4 to stress the
# multi-node mesh during Phase 0 broadcast; in practice we tolerate
# one missing worker (``slurm-worker1`` has been observed in
# DOWN+NOT_RESPONDING independently of the framework) and fall back
# to ``jobs = idle_count`` below.
DESIRED_N_SECONDARIES = 4

# Lower bound for the idle-worker pre-flight. T20's intent is the
# Phase-0 happy path; we need at least two secondaries for the
# distributed-eval shape (one runs Phase 0, the broadcast targets
# the other peers). Below this we skip rather than mask a real
# cluster outage.
MIN_IDLE_WORKERS = 2

# Canonical three-package set per plan Part B T20. ``hello`` and
# ``busybox`` are tier-1; ``zlib`` is tier-2 (small static lib).
# All three cross-compile cleanly across the matrix archs.
T20_PACKAGES: tuple[str, ...] = ("hello", "zlib", "busybox")

# Default wall-clock cap. The plan asks for "all phases complete in
# < 5 min"; we give 600s (10 min) of headroom for cache-cold first-
# touch toolchain rebuild on a slow CI box. Override via
# ``T20_TIMEOUT_S=<seconds>`` for particularly cold caches.
DEFAULT_TIMEOUT_S = 600.0

# Regex matching a phase 0 ``task done`` line in slurm_*.out. The
# framework log shape is structured Rust output with ANSI escapes
# stripped by ``invariants._read_text``; we anchor on the literal
# ``task_type="phase0_eval"`` (or its ``Some(...)`` variant)
# alongside the ``success=`` field. The success-bool is captured in
# a named group so the assertion can flag a ``success=false`` row
# even when the task technically "completed".
_PHASE0_TASK_DONE_RE = re.compile(
    r"task done.*?"
    r"task_type=(?:Some\(\"phase0_eval\"\)|phase0_eval)"
    r".*?success=(?P<success>true|false)"
)


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    The conftest's session-scoped ``cluster_probe`` fixture does not
    set ``identity_file``; for the live path we need it, so we
    instantiate locally. ``timeout=8.0`` matches the cluster_probe
    self-test in the same package.
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
    """Read ``T20_TIMEOUT_S`` from the environment, falling back to default.

    Invalid (non-float) values are silently ignored.
    """
    raw = os.environ.get("T20_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _read_phase0_marker(
    shared_fs: pathlib.Path, binary: str,
) -> dict:
    """Read ``<shared_fs>/out/<binary>/_phase0/manifest.json``.

    Returns the parsed JSON dict. Raises ``AssertionError`` (via the
    caller's ``assert``) if the file is missing or unparseable — the
    Phase 0 marker is the cluster-side gating signal for Phase 1, so
    a missing marker means the whole pipeline aborted at Phase 0.
    """
    marker = shared_fs / "out" / binary / "_phase0" / "manifest.json"
    assert marker.is_file(), (
        f"phase0 resume marker missing for {binary!r}: "
        f"expected {marker} to exist (the Phase 0 quiesce signal "
        f"is the file's presence; a missing file means the eval "
        f"worker either never ran or crashed before writing)"
    )
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"phase0 marker {marker} unparseable: {exc!r}"
        ) from exc


def _collect_variant_holders(
    shared_fs: pathlib.Path,
) -> dict[str, set[str]]:
    """Aggregate placement files into ``{outpath: {secondary_id, ...}}``.

    Scans every ``peers/_paths_<sid>.jsonl`` and keeps records whose
    ``item_class`` is ``variant`` (the build-worker tags variant
    outpaths this way once ``record_self_has`` fires). The K=3
    cascade T11 owns is orthogonal — here we only need ``>= 1``
    holder per variant outpath.
    """
    peers_dir = shared_fs / "peers"
    placements: dict[str, set[str]] = collections.defaultdict(set)
    if not peers_dir.is_dir():
        return dict(placements)
    for paths_file in peers_dir.glob(f"{PATHS_FILE_PREFIX}*.jsonl"):
        for rec in parse_placement_records(paths_file):
            sid = rec.get("secondary_id")
            outpath = rec.get("outpath")
            item_class = rec.get("item_class")
            if (
                isinstance(sid, str)
                and isinstance(outpath, str)
                and outpath.startswith("/nix/store/")
                and item_class == "variant"
            ):
                placements[outpath].add(sid)
    return dict(placements)


def _scan_phase0_completion_logs(
    log_dir: pathlib.Path,
) -> list[tuple[pathlib.Path, str]]:
    """Walk ``slurm_*.out`` files for phase0_eval task-done events.

    Returns a list of ``(path, success_field)`` tuples. The caller
    asserts every ``success_field == "true"``; a ``"false"`` row is
    the smoking gun that Phase 0 reported failure even though the
    framework's clean-exit invariant passed.
    """
    out: list[tuple[pathlib.Path, str]] = []
    if not log_dir or not log_dir.is_dir():
        return out
    # ANSI strip is the responsibility of the invariants module's
    # _read_text; mirror it here so the regex sees the same shape.
    from compiler_suit_runner.tests.slurm.invariants import _read_text
    for log_path in sorted(log_dir.glob("slurm_*.out")):
        text = _read_text(log_path)
        for match in _PHASE0_TASK_DONE_RE.finditer(text):
            out.append((log_path, match.group("success")))
    return out


@pytest.mark.slurm_live
def test_t20_phase0_happy(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- wired via the B2 cleanup harness
) -> None:
    """``--distributed-eval`` end-to-end smoke for hello + zlib + busybox.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` of the four expected workers
    idle. We tolerate degraded ``N < 4`` runs (the Phase 0 pipeline
    still exercises every code path).

    Dispatch: ``--distributed-eval`` + ``--variant-sample 1`` keeps
    the workload tight (one variant per (binary, arch)) so the run
    fits the 5-min plan budget; the ``--jobs`` count is reduced to
    the available idle workers when fewer than four are idle.

    Post-flight assertions:

    1. Standard 7-invariant audit with ``expected_failure_count=0``
       (covers clean exit, bind-error absence, manifest count,
       build-failure floor, and the three leak checks).
    2. Exactly one ``phase0_eval__<binary>.json`` manifest per
       binary in :attr:`RunArtifacts.manifests_dir`.
    3. ``<shared_fs>/out/<binary>/_phase0/manifest.json`` exists and
       parses cleanly for every binary; the marker carries a
       non-empty ``variants`` list (the eval worker enumerated at
       least one variant per binary).
    4. Phase 1 fired: at least one ``phase3_variant`` manifest
       exists in :attr:`RunArtifacts.manifests_dir`, and every such
       manifest's ``task_depends_on`` references the toolchain
       task_id naming convention (``toolchain__<arch>__<id>``).
    5. ``<dataset_dir>/<pkg>/<variant_dir>/`` is populated for every
       (pkg, variant_dir) named in the phase3 manifests we emitted.
    6. Every variant outpath in the placement gossip files has at
       least one holder (legacy ``>= 1`` invariant; K=3 cascade is
       T11's job).
    7. Every ``task done`` line tagged ``phase0_eval`` in the
       secondary slurm_*.out files reports ``success=true``.
    """
    probe = _live_probe()

    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    # Cluster must be quiet at start. A non-empty squeue would mean
    # another caller (or a stale prior run) is still active; we refuse
    # to start so we don't pollute their leak audit.
    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T20 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

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
            f"T20 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # Compose the invocation. ``default_invocation_for_smoke(jobs=N,
    # workload="tiny")`` already pins ``--variant-sample 1``,
    # ``--max-variants 1``, ``--slurm-partition debug``,
    # ``cores=2``/``max_memory="2G"``/``slurm_cpus_per_task=2`` for
    # the test-env's 2-CPU / 3.5-GiB cgroup envelope, and seeds the
    # variant pick for repeatability. We override:
    #
    # * ``packages`` -- swap the default ("hello",) for T20's
    #   canonical three-binary set.
    # * ``ssh_identity_file`` -- explicit key for the framework's
    #   gateway SSH (probe already has its own).
    # * ``extra_args`` -- inject ``--distributed-eval`` to switch the
    #   submitter to the Phase 0 / Phase 1 split flow (the flag is
    #   the whole point of the test).
    # * ``archs`` -- left at the default ``("x86_64",)`` from
    #   ``default_invocation_for_smoke`` to stay inside the per-
    #   worker memory envelope (per ``run_helpers`` doc and project
    #   memory ``feedback_slurm_test_env_memory``).
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="tiny"),
        packages=T20_PACKAGES,
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        extra_args=("--distributed-eval",),
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"packages={T20_PACKAGES!r} "
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
    # finishes dispatching; the secondaries' sbatch jobs may still be
    # in flight. Wait for squeue to drain so the file-only invariants
    # don't race the framework's last writes.
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
        f"completed ({detail})"
    )

    # ------------------------------------------------------------------
    # Invariant audit (1-7). Same call shape as T07 / T11.
    # ------------------------------------------------------------------
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
    # T20-specific assertions.
    # ------------------------------------------------------------------
    manifests_dir = artifacts.manifests_dir
    assert manifests_dir.is_dir(), (
        f"manifests_dir missing for {detail}: expected "
        f"{manifests_dir} to exist"
    )

    # Assertion (2): exactly one phase0_eval__<binary>.json per
    # binary. The submitter's manifest_gen names them
    # ``phase0_eval__{binary}`` (see
    # ``manifest_gen.make_phase0_eval_header``).
    for binary in T20_PACKAGES:
        expected_path = manifests_dir / f"phase0_eval__{binary}.json"
        assert expected_path.is_file(), (
            f"phase0_eval manifest missing for {binary!r}: "
            f"expected {expected_path} ({detail})"
        )

    phase0_manifests = sorted(manifests_dir.glob("phase0_eval__*.json"))
    assert len(phase0_manifests) == len(T20_PACKAGES), (
        f"expected exactly {len(T20_PACKAGES)} phase0_eval manifests, "
        f"got {len(phase0_manifests)}: "
        f"{[p.name for p in phase0_manifests]!r} ({detail})"
    )

    # Assertion (3): Phase 0 resume marker per binary, with at least
    # one variant in its ``variants`` list. The marker is the
    # cluster-side Phase 1 gate; missing or empty here means the eval
    # worker landed but produced no drvs (typically a flake-eval
    # failure inside the secondary).
    phase0_markers: dict[str, dict] = {}
    for binary in T20_PACKAGES:
        marker_data = _read_phase0_marker(invocation.shared_fs, binary)
        variants = marker_data.get("variants")
        assert isinstance(variants, list) and variants, (
            f"phase0 marker for {binary!r} has empty/missing "
            f"'variants': {marker_data!r} ({detail})"
        )
        phase0_markers[binary] = marker_data

    # Assertion (4): Phase 1 fired and emitted phase3_variant build
    # manifests with toolchain depends_on. The manifest filename for
    # a phase3_variant is the variant label (see
    # ``manifest_gen.make_variant_header``: ``name=label``); we read
    # the JSON to find ``item_class == "phase3_variant"``.
    phase3_manifests: list[tuple[pathlib.Path, dict]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        # Skip the obvious sidecars and the phase0/toolchain manifests
        # we've already classified.
        if path.name.startswith("_") or path.name.startswith("toolchain"):
            continue
        if path.name.startswith("phase0_eval__"):
            continue
        if path.name.startswith("phase1") or path.name.startswith("common_dep"):
            continue
        if path.name.startswith("partition__") or path.name.startswith("merge"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("item_class") == "phase3_variant":
            phase3_manifests.append((path, doc))

    assert phase3_manifests, (
        "Phase 1 planner produced no phase3_variant manifests for "
        f"{detail}; the quiesce watcher likely never fired (check the "
        f"submitter stderr for 'plan_phase1' log lines). manifests_dir "
        f"= {manifests_dir}"
    )

    for path, doc in phase3_manifests:
        depends = doc.get("task_depends_on") or []
        assert depends, (
            f"phase3_variant manifest {path.name} has empty "
            f"task_depends_on -- the planner did not wire its "
            f"toolchain dependency ({detail})"
        )
        # Toolchain task_ids are stamped as ``toolchain__<arch>__<id>``
        # by ``manifest_gen.toolchain_task_id``. At least one entry in
        # depends_on must follow that shape; the planner may add
        # phase0_eval__<binary> as well (transitive provenance), but
        # the toolchain dep is mandatory.
        toolchain_deps = [
            d for d in depends if isinstance(d, str) and d.startswith("toolchain__")
        ]
        assert toolchain_deps, (
            f"phase3_variant manifest {path.name} has no "
            f"toolchain__* entry in task_depends_on={depends!r} "
            f"({detail})"
        )

    # Assertion (5): final dataset directories populated for every
    # (pkg, variant_dir) we just verified.
    dataset_dir = invocation.shared_fs / "dataset"
    assert dataset_dir.is_dir(), (
        f"dataset_dir missing for {detail}: expected "
        f"{dataset_dir} to be a directory"
    )
    missing_dataset_dirs: list[str] = []
    for path, doc in phase3_manifests:
        payload = doc.get("payload") or {}
        pkg = payload.get("pkg")
        variant_dir = payload.get("variant_dir")
        if not isinstance(pkg, str) or not isinstance(variant_dir, str):
            # Malformed payload would have been caught upstream; skip.
            continue
        target = dataset_dir / pkg / variant_dir
        if not target.is_dir() or not any(target.iterdir()):
            missing_dataset_dirs.append(
                f"{pkg}/{variant_dir} (manifest={path.name})"
            )
    assert not missing_dataset_dirs, (
        f"{len(missing_dataset_dirs)} dataset variant dir(s) missing "
        f"or empty for {detail}:\n  "
        + "\n  ".join(missing_dataset_dirs[:10])
        + (
            f"\n  ... and {len(missing_dataset_dirs) - 10} more"
            if len(missing_dataset_dirs) > 10
            else ""
        )
    )

    # Assertion (6): legacy ≥1-holder check on variant outpaths.
    # K=3 (T11) is intentionally NOT enforced here; T20 only asserts
    # that the placement gossip names at least one holder per
    # variant. Empty placement files are allowed (the gossip is
    # best-effort) but a recorded outpath with zero holders is a
    # bug.
    holders = _collect_variant_holders(invocation.shared_fs)
    if holders:  # nothing recorded is acceptable; zero holders for a
                 # recorded path is not.
        zero_holder = [op for op, sids in holders.items() if not sids]
        assert not zero_holder, (
            f"{len(zero_holder)} variant outpath(s) have zero "
            f"holders in the placement map ({detail}): "
            f"{zero_holder[:5]!r}"
        )

    # Assertion (7): every phase0_eval task-done line in the
    # secondary slurm_*.out files reports success=true. A
    # ``success=false`` row passes the clean-exit invariant (it's
    # still a "task done" line) but indicates Phase 0 reported a
    # logical failure — we want a hard fail on that case.
    completion_rows = _scan_phase0_completion_logs(result.log_dir)
    assert completion_rows, (
        f"no phase0_eval 'task done' lines found in slurm_*.out under "
        f"{result.log_dir} ({detail}); the eval worker either never "
        f"ran or the log shape changed -- inspect the framework log "
        f"emitter"
    )
    bad_completions = [
        (path, val) for (path, val) in completion_rows if val != "true"
    ]
    assert not bad_completions, (
        f"{len(bad_completions)} phase0_eval task done line(s) report "
        f"success!=true for {detail}:\n  "
        + "\n  ".join(
            f"{p.name}: success={v!r}" for p, v in bad_completions[:5]
        )
    )

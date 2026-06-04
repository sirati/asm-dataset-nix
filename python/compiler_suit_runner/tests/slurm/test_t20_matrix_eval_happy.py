"""End-to-end T20 smoke: matrix_eval happy path on the slurm-test-env.

T20 exercises the matrix_eval pipeline end-to-end on the live
slurm-test-env. The submitter dispatches three small, cross-
compilable packages (``hello``, ``zlib``, ``busybox``) onto N=4
secondaries; the test asserts that the full multi-phase shape lands
cleanly:

* Toolchain seeding kicks off the bootstrap (covered by the standard
  7 invariants — clean exit, no bind errors, no leaks).
* The submitter emits one ``matrix_eval__<binary>`` manifest per
  binary (exactly three), each task lands on a secondary, runs
  :func:`compiler_suit_runner.workers.eval_worker.run_eval_task`, and
  publishes the kept-variant closure at
  ``<matrix_eval_out_dir>/matrix-<binary>.drv.archive`` (archive
  presence alone signals this binary's eval is done; no JSON sidecar
  is written, the dependency_graph_worker derives variant_lookup from
  the imported .drv paths).
* The framework ``dependency_graph`` phase runs
  ``workers.dependency_graph_worker``, which writes
  ``_dependency_graph.pkl``. :meth:`SuitTask.on_phase_end` then loads
  that pickle and translates the descriptors into ``build_variant`` /
  ``build_common_dep`` headers spawned via
  ``primary_handle.spawn_tasks``.
* Final ``<dataset_dir>/<pkg>/<variant_dir>/`` directories are
  populated for every variant the run produced.
* The cluster's placement-map gossip files name at least one holder
  for every variant outpath (legacy ≥1 holder invariant — T11 owns
  the stricter K=3 cascade).
* Every matrix_eval ``task done`` line in ``slurm_*.out`` carries
  ``success=true``.

T20 is the canonical "does the new pipeline complete at all?"
regression. It deliberately keeps the workload small
(``--variant-sample 1``, three tier-1 packages, x86_64 only) so the
whole run fits inside the 5-min budget the plan asks for. Per-row
specifics belonging to other tests:

* K=3 holder cascade — T11.
* dependency_graph common-dep dedup — T23.

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

# Regex matching a matrix_eval ``task done`` line in slurm_*.out. The
# framework log shape is structured Rust output with ANSI escapes
# stripped by ``invariants._read_text``; we anchor on the literal
# ``task_type="matrix_eval"`` (or its ``Some(...)`` variant)
# alongside the ``success=`` field. The success-bool is captured in
# a named group so the assertion can flag a ``success=false`` row
# even when the task technically "completed".
_MATRIX_EVAL_TASK_DONE_RE = re.compile(
    r"task done.*?"
    r"task_type=(?:Some\(\"matrix_eval\"\)|matrix_eval)"
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


def _matrix_eval_out_dir(shared_fs: pathlib.Path) -> pathlib.Path:
    """Resolve the matrix-eval output dir under ``shared_fs``.

    Mirrors :func:`cli._build_config`: ``dataset_dir`` defaults to
    ``<shared_fs>/dataset`` and ``matrix_eval_out_dir`` defaults to
    ``<dataset_dir>/_matrix_eval`` on the submitter side.
    """
    return shared_fs / "dataset" / "_matrix_eval"


def _assert_matrix_eval_archive(
    shared_fs: pathlib.Path, binary: str,
) -> pathlib.Path:
    """Assert the per-binary matrix-eval archive exists and is non-empty.

    Returns the archive path. The archive's presence signals this
    binary's matrix-eval is done, so a missing or empty archive means
    the eval worker either never ran or crashed before writing. The
    JSON sidecar has been retired — the dependency_graph_worker derives
    variant_lookup from the imported .drv paths via
    ``parse_variant_path``.
    """
    out_dir = _matrix_eval_out_dir(shared_fs)
    archive = out_dir / f"matrix-{binary}.drv.archive"
    assert archive.is_file(), (
        f"matrix-eval archive missing for {binary!r}: "
        f"expected {archive} to exist (presence of the archive signals "
        f"this binary's matrix-eval is done; a missing file means the "
        f"eval worker either never ran or crashed before writing)"
    )
    assert archive.stat().st_size > 0, (
        f"matrix-eval archive empty for {binary!r}: {archive} has "
        f"zero bytes (the eval worker would only emit a zero-byte "
        f"archive when no variants survived support-table gating; "
        f"T20 expects at least one variant per binary)"
    )
    return archive


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


def _scan_matrix_eval_completion_logs(
    log_dir: pathlib.Path,
) -> list[tuple[pathlib.Path, str]]:
    """Walk ``slurm_*.out`` files for matrix_eval task-done events.

    Returns a list of ``(path, success_field)`` tuples. The caller
    asserts every ``success_field == "true"``; a ``"false"`` row is
    the smoking gun that matrix_eval reported failure even though
    the framework's clean-exit invariant passed.
    """
    out: list[tuple[pathlib.Path, str]] = []
    if not log_dir or not log_dir.is_dir():
        return out
    # ANSI strip is the responsibility of the invariants module's
    # _read_text; mirror it here so the regex sees the same shape.
    from compiler_suit_runner.tests.slurm.invariants import _read_text
    for log_path in sorted(log_dir.glob("slurm_*.out")):
        text = _read_text(log_path)
        for match in _MATRIX_EVAL_TASK_DONE_RE.finditer(text):
            out.append((log_path, match.group("success")))
    return out


@pytest.mark.slurm_live
def test_t20_matrix_eval_happy(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- wired via the B2 cleanup harness
) -> None:
    """matrix_eval end-to-end smoke for hello + zlib + busybox.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` of the four expected workers
    idle. We tolerate degraded ``N < 4`` runs (the matrix_eval
    pipeline still exercises every code path).

    Dispatch: ``--variant-sample 1`` keeps the workload tight (one
    variant per (binary, arch)) so the run fits the 5-min plan
    budget; the ``--jobs`` count is reduced to the available idle
    workers when fewer than four are idle.

    Post-flight assertions:

    1. Standard 7-invariant audit with ``expected_failure_count=0``
       (covers clean exit, bind-error absence, manifest count,
       build-failure floor, and the three leak checks).
    2. Exactly one ``matrix_eval__<binary>.json`` manifest per
       binary in :attr:`RunArtifacts.manifests_dir`.
    3. The matrix-eval archive
       (``<matrix_eval_out_dir>/matrix-<binary>.drv.archive``) exists
       and is non-empty for every binary. Archive presence alone is
       the quiesce signal now; variant enumeration is verified
       downstream via the build_variant manifest count in assertion (4).
    4. dependency_graph fired: at least one ``build_variant``
       manifest exists in :attr:`RunArtifacts.manifests_dir`, and
       every such manifest's ``task_depends_on`` references a
       toolchain task_id minted by either
       ``build_compilers_task_id`` or ``toolchain_validate_task_id``
       (one prefix per dispatch mode).
    5. ``<dataset_dir>/<pkg>/<variant_dir>/`` is populated for every
       (pkg, variant_dir) named in the build_variant manifests we
       emitted.
    6. Every variant outpath in the placement gossip files has at
       least one holder (legacy ``>= 1`` invariant; K=3 cascade is
       T11's job).
    7. Every ``task done`` line tagged ``matrix_eval`` in the
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
    # * ``archs`` -- left at the default ``("x86_64",)`` from
    #   ``default_invocation_for_smoke`` to stay inside the per-
    #   worker memory envelope (per ``run_helpers`` doc and project
    #   memory ``feedback_slurm_test_env_memory``).
    #
    # The matrix_eval / dependency_graph split is the only mode now;
    # no extra CLI flag is required to engage it.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="tiny"),
        packages=T20_PACKAGES,
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
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

    # Assertion (2): exactly one matrix_eval__<binary>.json per
    # binary. The submitter's manifest_gen names them
    # ``matrix_eval__{binary}`` (see
    # ``manifest_gen.make_matrix_eval_header``).
    for binary in T20_PACKAGES:
        expected_path = manifests_dir / f"matrix_eval__{binary}.json"
        assert expected_path.is_file(), (
            f"matrix_eval manifest missing for {binary!r}: "
            f"expected {expected_path} ({detail})"
        )

    matrix_eval_manifests = sorted(
        manifests_dir.glob("matrix_eval__*.json"),
    )
    assert len(matrix_eval_manifests) == len(T20_PACKAGES), (
        f"expected exactly {len(T20_PACKAGES)} matrix_eval manifests, "
        f"got {len(matrix_eval_manifests)}: "
        f"{[p.name for p in matrix_eval_manifests]!r} ({detail})"
    )

    # Assertion (3): matrix-eval archive per binary (non-empty). The
    # archive signals this binary's matrix-eval is done; missing or
    # empty here means the eval worker landed but produced
    # no drvs (typically a flake-eval failure inside the secondary).
    # The JSON sidecar was retired post-migration — the
    # dependency_graph_worker derives variant_lookup from the imported
    # .drv paths via parse_variant_path, and assertion (4) below
    # surfaces missing variants via the build_variant manifest count.
    for binary in T20_PACKAGES:
        _assert_matrix_eval_archive(invocation.shared_fs, binary)

    # Assertion (4): dependency_graph fired and emitted build_variant
    # manifests with toolchain depends_on. The manifest filename for
    # a build_variant is ``build_variant__<sys>__<binary>__<label>``
    # (see ``manifest_gen.make_build_variant_header``); we read the
    # JSON to find ``item_class == "build_variant"``.
    build_variant_manifests: list[tuple[pathlib.Path, dict]] = []
    for path in sorted(manifests_dir.glob("*.json")):
        # Skip the obvious sidecars and the matrix_eval / toolchain
        # manifests we've already classified.
        if path.name.startswith("_") or path.name.startswith("toolchain"):
            continue
        if path.name.startswith("matrix_eval__"):
            continue
        if (
            path.name.startswith("common_dep__")
            or path.name.startswith("build_common_dep__")
            or path.name.startswith("build_compilers__")
        ):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("item_class") == "build_variant":
            build_variant_manifests.append((path, doc))

    assert build_variant_manifests, (
        "dependency_graph produced no build_variant manifests for "
        f"{detail}; on_phase_end likely never spawned the build phase "
        f"(check the submitter stderr for 'on_phase_end' / "
        f"'dependency_graph' log lines). manifests_dir "
        f"= {manifests_dir}"
    )

    for path, doc in build_variant_manifests:
        # The toolchain dep is CROSS-phase (Phase.BUILD_COMPILERS) and is
        # carried in the dedicated ``build_compilers_depends_on`` field —
        # NOT the intra-phase ``task_depends_on``. The bare task_id
        # (``<sys>__<arch>__<comp>``) is minted by
        # ``manifest_gen.build_compilers_task_id``; the runner wraps it
        # in a phase-tagged TaskDep when emitting the framework TaskInfo.
        toolchain_deps = doc.get("build_compilers_depends_on") or []
        assert toolchain_deps, (
            f"build_variant manifest {path.name} has empty "
            f"build_compilers_depends_on -- the planner did not wire its "
            f"toolchain dependency ({detail})"
        )

    # Assertion (5): final dataset directories populated for every
    # (pkg, variant_dir) we just verified.
    dataset_dir = invocation.shared_fs / "dataset"
    assert dataset_dir.is_dir(), (
        f"dataset_dir missing for {detail}: expected "
        f"{dataset_dir} to be a directory"
    )
    missing_dataset_dirs: list[str] = []
    for path, doc in build_variant_manifests:
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

    # Assertion (7): every matrix_eval task-done line in the
    # secondary slurm_*.out files reports success=true. A
    # ``success=false`` row passes the clean-exit invariant (it's
    # still a "task done" line) but indicates matrix_eval reported
    # a logical failure — we want a hard fail on that case.
    completion_rows = _scan_matrix_eval_completion_logs(result.log_dir)
    assert completion_rows, (
        f"no matrix_eval 'task done' lines found in slurm_*.out "
        f"under {result.log_dir} ({detail}); the eval worker either "
        f"never ran or the log shape changed -- inspect the "
        f"framework log emitter"
    )
    bad_completions = [
        (path, val) for (path, val) in completion_rows if val != "true"
    ]
    assert not bad_completions, (
        f"{len(bad_completions)} matrix_eval task done line(s) "
        f"report success!=true for {detail}:\n  "
        + "\n  ".join(
            f"{p.name}: success={v!r}" for p, v in bad_completions[:5]
        )
    )

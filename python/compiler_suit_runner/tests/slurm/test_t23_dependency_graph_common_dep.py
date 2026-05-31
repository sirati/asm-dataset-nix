"""End-to-end T23 smoke: dependency_graph common-dep dedup on the slurm-test-env.

T23 verifies that the dependency_graph worker's graph-synthesis pass
actually collapses shared inputs across ≥ 2 variant drvs into a
single ``build_common_dep`` task, and that every variant header
listing that input ends up with the common dep's ``task_id`` in its
``task_depends_on``. Without this dedup, every variant would rebuild
glibc / cross-toolchain runtime libs from source, which defeats the
whole point of moving eval into matrix_eval.

The signal we assert against is the dependency_graph worker's
authoritative on-disk artifact: ``_dependency_graph.pkl`` written
under ``<matrix_eval_out_dir>`` (defaults to
``<shared_fs>/dataset/_matrix_eval``). We load it with
:func:`dependency_graph_planner.load_phase4_descriptors` and translate
the descriptors back into :class:`ManifestHeader` instances via
:func:`dependency_graph_planner.headers_from_descriptors` — the exact
conversion :meth:`SuitTask.on_phase_end` performs before
``primary_handle.spawn_tasks``. This gives the test full visibility
into the synthesised DAG independent of whether the spawn fired.

What this test asserts:

* Run completes cleanly (standard 7-invariant audit;
  ``expected_failure_count=0``).
* ``_dependency_graph.pkl`` exists under ``<matrix_eval_out_dir>``
  (defaults to ``<shared_fs>/dataset/_matrix_eval``).
* The pickle decodes to descriptors; at least one converts to a
  header with ``item_class == "build_common_dep"``.
* At least one of those common-dep ``task_id`` values is referenced
  by ≥ 2 ``build_variant`` headers' ``task_depends_on`` arrays —
  the dedup contract.

Workload shape: ``--packages hello --archs x86_64 --variant-sample 2``
so we get two variants of the same package (e.g. hello-x86_64-O0 +
hello-x86_64-O2). They share glibc + the gcc15 toolchain outputs at
minimum, so the refcount ≥ 2 condition is satisfied trivially. The
toolchain drv set is excluded from common-dep emission (see the
dependency_graph planner), but glibc / runtime helpers remain.

Builds on T07's N=4 shape: same probe construction, same WORKERS
list, same invocation modulo the single-package +
``--variant-sample 2`` knobs.
"""

from __future__ import annotations

import collections
import dataclasses
import os
import pathlib
from typing import Any, Callable, Optional

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
# ``~/.ssh/``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. ``invariants``
# checks 5-7 walk this list to look for leaks.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Desired secondary count. The plan's dispatch shape pins ``--jobs 4``.
DESIRED_N_SECONDARIES = 4

# T23's invariant load is light (the planner runs once on the promoted
# primary; the rest of the cluster contributes phase 0 + phase 2
# builds). We tolerate one missing worker -- the planner still fires.
MIN_IDLE_WORKERS = 3

# Default wall-clock cap. matrix_eval + dependency_graph planning +
# the small variant build set fits comfortably in 1500s; matrix_eval
# is the dominant new cost.
DEFAULT_TIMEOUT_S = 1500.0


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
    raw = os.environ.get("T23_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _candidate_graph_paths(
    shared_fs: pathlib.Path,
    log_dir: Optional[pathlib.Path],
) -> list[pathlib.Path]:
    """Return every plausible location for ``_dependency_graph.pkl``.

    The dependency_graph worker writes to the configured
    ``matrix_eval_out_dir`` — which defaults to
    ``<shared_fs>/dataset/_matrix_eval`` per :func:`cli._build_config`.
    Older worktrees / framework-supplied output_dir overrides may land
    the file elsewhere under ``shared_fs``; we probe a small set of
    candidates and fall back to a recursive scan so the test stays
    robust to wiring changes.
    """
    from compiler_suit_runner.workers.dependency_graph_worker.output import (
        DEPENDENCY_GRAPH_PICKLE,
    )

    candidates: list[pathlib.Path] = [
        shared_fs / "dataset" / "_matrix_eval" / DEPENDENCY_GRAPH_PICKLE,
        shared_fs / "dataset" / DEPENDENCY_GRAPH_PICKLE,
        shared_fs / "out" / DEPENDENCY_GRAPH_PICKLE,
        shared_fs / DEPENDENCY_GRAPH_PICKLE,
    ]
    if log_dir is not None:
        candidates.append(log_dir / DEPENDENCY_GRAPH_PICKLE)
    return candidates


def _find_graph_file(
    shared_fs: pathlib.Path,
    log_dir: Optional[pathlib.Path],
) -> Optional[pathlib.Path]:
    """Pick the first existing candidate; fall back to a recursive scan."""
    from compiler_suit_runner.workers.dependency_graph_worker.output import (
        DEPENDENCY_GRAPH_PICKLE,
    )

    for c in _candidate_graph_paths(shared_fs, log_dir):
        if c.is_file():
            return c
    # Last-resort recursive scan -- the file is small, the shared-fs
    # tree is bounded by the test run, and a mismatch between the
    # candidate list and the framework's actual output_dir shows up
    # here rather than as an opaque AssertionError.
    if shared_fs.is_dir():
        for hit in shared_fs.rglob(DEPENDENCY_GRAPH_PICKLE):
            return hit
    return None


def _parse_graph(path: pathlib.Path) -> list[dict[str, Any]]:
    """Decode ``_dependency_graph.pkl`` into a header-dict list.

    Loads the descriptors via
    :func:`dependency_graph_planner.load_phase4_descriptors`, converts
    them back to :class:`ManifestHeader` instances via
    :func:`dependency_graph_planner.headers_from_descriptors`, then
    projects each header into the dict shape the dedup assertions
    below consume (``item_class``, ``name``, ``task_id`` and
    ``task_depends_on`` as a list of strings). A decode failure raises
    AssertionError directly -- that surfaces the real failure (planner
    emitted nothing / wrote a malformed pickle) rather than a generic
    pickle error.
    """
    from compiler_suit_runner.dependency_graph_planner import (
        headers_from_descriptors,
        load_phase4_descriptors,
    )

    try:
        descriptors, _summary = load_phase4_descriptors(path)
    except Exception as exc:  # noqa: BLE001 — surface as assertion
        raise AssertionError(
            f"failed to load phase-4 descriptors from {path}: {exc!r}"
        ) from exc
    headers = headers_from_descriptors(descriptors)
    return [
        {
            "item_class": h.item_class,
            "name": h.name,
            "task_id": h.task_id,
            "task_depends_on": list(h.task_depends_on),
        }
        for h in headers
    ]


@pytest.mark.slurm_live
def test_t23_dependency_graph_common_dep_dedup(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- ordering only
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- B2 cleanup harness
) -> None:
    """dependency_graph emits exactly one common_dep per shared input across variants.

    Pre-flight: same gate as T07 (gateway reachable, ``squeue --me``
    empty, ≥ :data:`MIN_IDLE_WORKERS` workers idle). T23 tolerates
    the degraded N=3 path -- the dependency_graph subprocess runs on
    a single promoted primary so worker count only affects how many
    matrix_eval / variant builds run in parallel, not the dedup
    contract itself.

    Dispatch: two variants of one package
    (``--packages hello --variant-sample 2 --archs x86_64``). The
    matrix_eval + dependency_graph split is the only mode now; no
    extra CLI flag is required to engage it.

    Post-flight: standard 7-invariant audit, then locate the worker's
    ``_dependency_graph.pkl`` under the shared FS, decode it, and
    assert:

    * ≥ 1 ``build_common_dep`` task was emitted.
    * At least one common-dep ``task_id`` is referenced by ≥ 2
      ``build_variant`` headers' ``task_depends_on`` -- which is the
      contract: a shared input becomes one task that multiple
      variants depend on.

    Failure surface: the assertion messages dump the full common-dep
    list and a per-variant dep summary so a CI run yields enough
    context to triage without re-running.
    """
    probe = _live_probe()

    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T23 start; found {len(queued)} "
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
            f"T23 needs at least {MIN_IDLE_WORKERS} idle worker(s); got "
            f"{len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # ``default_invocation_for_smoke(jobs=N, workload="medium")`` already
    # pins ``packages=("hello",)``, ``archs=("x86_64",)``,
    # ``variant_sample=2`` and ``--slurm-partition debug``. We override:
    #
    # * ``ssh_identity_file`` -- explicit key path (project policy).
    # * ``slurm_cpus_per_task`` -- 2 to match the test-env's CPUTot=2.
    # * ``max_variants`` -- 2 to keep the variant set tight (two
    #   variants is the minimum for refcount ≥ 2 dedup).
    #
    # The matrix_eval / dependency_graph split is the only mode now;
    # no extra CLI flag is required to engage it.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="medium"),
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
        max_variants=2,
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"log_dir={result.log_dir!s} "
        f"shared_fs={invocation.shared_fs!s}"
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

    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
        f"completed ({detail})"
    )

    # Standard 7-invariant audit. ``expected_failure_count=0`` because
    # T23 is a clean-path test: the variant set is tiny (2) and the
    # cross-toolchain envelope is sidestepped via ``archs=("x86_64",)``.
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
    # T23 core assertions: dependency_graph pickle + common-dep dedup
    # contract.
    # ------------------------------------------------------------------
    graph_path = _find_graph_file(invocation.shared_fs, result.log_dir)
    assert graph_path is not None, (
        f"_dependency_graph.pkl not found under "
        f"{invocation.shared_fs!s}; checked candidates: "
        + ", ".join(
            str(c) for c in _candidate_graph_paths(
                invocation.shared_fs, result.log_dir,
            )
        )
        + f" ({detail}). Did the dependency_graph worker run? "
        f"Check log_dir for dependency_graph worker output."
    )

    headers = _parse_graph(graph_path)

    common_deps = [
        h for h in headers
        if isinstance(h, dict)
        and h.get("item_class") == "build_common_dep"
    ]
    variants = [
        h for h in headers
        if isinstance(h, dict)
        and h.get("item_class") == "build_variant"
    ]

    assert common_deps, (
        f"dependency_graph emitted zero build_common_dep tasks "
        f"for graph at {graph_path!s} ({detail}); "
        f"headers ({len(headers)}): "
        + ", ".join(
            f"{h.get('item_class')!r}:{h.get('task_id')!r}"
            for h in headers if isinstance(h, dict)
        )
        + ". Two variants of the same package should share at least "
        f"one non-toolchain dep (e.g. glibc) -- refcount ≥ 2 must "
        f"fire."
    )

    # The dedup contract: a common-dep task_id must appear in ≥ 2
    # variant headers' task_depends_on. Count references per
    # common-dep id, then require at least one to hit 2.
    common_dep_ids: list[str] = [
        cd.get("task_id") for cd in common_deps  # type: ignore[misc]
        if isinstance(cd.get("task_id"), str)
    ]
    assert common_dep_ids, (
        f"common_deps headers carried no string task_id: {common_deps!r} "
        f"({detail})"
    )

    ref_counts: collections.Counter[str] = collections.Counter()
    variant_dep_map: dict[str, list[str]] = {}
    for var in variants:
        var_id = var.get("task_id") if isinstance(var, dict) else None
        deps = var.get("task_depends_on") if isinstance(var, dict) else None
        if not isinstance(deps, list):
            continue
        dep_strs = [d for d in deps if isinstance(d, str)]
        if isinstance(var_id, str):
            variant_dep_map[var_id] = dep_strs
        for d in dep_strs:
            if d in common_dep_ids:
                ref_counts[d] += 1

    shared_common_deps = [
        (cd_id, count) for cd_id, count in ref_counts.items() if count >= 2
    ]
    assert shared_common_deps, (
        f"dependency_graph dedup contract violated for graph at "
        f"{graph_path!s} ({detail}); no build_common_dep task_id is "
        f"referenced by ≥ 2 build_variant headers' task_depends_on. "
        f"\n  common_deps emitted ({len(common_deps)}): "
        + ", ".join(common_dep_ids)
        + f"\n  ref_counts across variants: {dict(ref_counts)!r}"
        + f"\n  variants ({len(variants)}):"
        + "".join(
            f"\n    {vid}: deps={deps!r}"
            for vid, deps in variant_dep_map.items()
        )
    )

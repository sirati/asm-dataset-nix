"""Top-level driver: archive walk → multi-binary sum-drv → one streaming
pass → descriptors.

Phase 5.2 collapse: every archive contributes ONE matrix wrapper to a
single sum-drv; the streaming planner runs ONCE over the resulting
tree so cross-binary template dedup fires automatically inside the
single :class:`StreamPlanner` instance. A single descriptor list is
emitted to ``_dependency_graph.pkl`` (with a human-readable companion
``_dependency_graph_summary.txt``).
"""

from __future__ import annotations

import collections
import pathlib
import time
from collections.abc import Callable, Sequence
from typing import Any, Optional, Union

from . import archive as _archive
from . import output as _output
from . import summary as _summary
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "run_dependency_graph_task",
]


def run_dependency_graph_task(
    *,
    matrix_eval_out_dir: pathlib.Path,
    bash_path: str,
    toolchain_aggregate_drv: str,
    matrix_aggregate_drvs: dict[str, str],
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
) -> DependencyGraphResult:
    """Assemble the multi-binary sum-drv from pre-built aggregate drvs
    and produce ``_dependency_graph.pkl`` (plus the
    ``_dependency_graph_summary.txt`` companion).

    Post-refactor (D.1b): phase 3 no longer rediscovers variant leaves
    from the archive import stream. The cluster has already evaluated
    every binary's matrix and the watcher hands us ONE aggregate drv
    per binary plus ONE toolchain aggregate drv via argv. We:

      1. import every ``<binary>.nix-archive`` so the closure (and
         therefore the leaves the aggregate drv references) is
         materialised in the local store — required for the
         ``nix-store --query --tree`` walk further down;
      2. derive the per-binary ``variant_lookup`` from each matrix
         aggregate via :func:`archive.derive_variant_lookup_from_aggregate`
         (D.1a) — NO post-import leaf walk;
      3. wrap the aggregate drvs in length-1 lists and call
         :func:`build_sum_drv_multi`, which in turn calls
         :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
         (the sole ``nix-instantiate`` in this phase);
      4. ``plan_from_drv_tree`` ONCE — the planner streams
         ``nix-store --query --tree`` tuples directly from
         :func:`stream_drv_tree` so cross-binary template dedup fires
         inside the single :class:`StreamPlanner`;
      5. one phase-4 descriptor list emitted for all binaries.

    ``toolchain_aggregate_drv`` and ``matrix_aggregate_drvs`` MUST both
    be non-empty — phase 3 cannot run without the producer's output.

    Any per-binary failure raises :class:`DependencyGraphWorkerError`
    tagged with the binary + stage; the caller's main loop translates
    that into a non-zero exit code.
    """
    if not toolchain_aggregate_drv:
        raise ValueError(
            "run_dependency_graph_task: toolchain_aggregate_drv is "
            "empty; phase 3 requires the producer's toolchain wrapper "
            "drv path (see preflight._build_toolchains_aggregate_drv)."
        )
    if not matrix_aggregate_drvs:
        raise ValueError(
            "run_dependency_graph_task: matrix_aggregate_drvs is "
            "empty; phase 3 requires one matrix-<binary> aggregate "
            "drv per binary (see workers/build_worker bulk-eval)."
        )

    clock_fn = clock or time.monotonic
    start = clock_fn()

    # Unlink any stale output before planning so a mid-run crash cannot
    # leave the watcher reading a previous run's artefact.
    stale_out_path = matrix_eval_out_dir / "_dependency_graph.pkl"
    stale_out_path.unlink(missing_ok=True)

    archives = _archive.discover_archives(matrix_eval_out_dir)
    if not archives:
        return _empty_result(
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})

    # Step 1: import archives so leaves are present locally; the
    # aggregate drv references them but `nix-store --query --tree`
    # cannot walk them until the closure exists in /nix/store.
    _import_all_archives(archives=archives, runner=runner)

    # Step 2: derive the per-binary variant lookup from the matrix
    # aggregate drvs (D.1a's helper). Iterate in sorted binary order so
    # the plannable-binaries list stays deterministic across runs.
    variant_lookups, plannable_binaries = (
        _derive_variant_lookups_from_aggregates(matrix_aggregate_drvs)
    )

    if not plannable_binaries:
        return _empty_result(
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    # Step 3: wrap the aggregate drvs in length-1 lists for the
    # path-form sum-drv helper (post-Phase-A.2 invariant).
    toolchain_drvs: list[str] = [toolchain_aggregate_drv]
    matrix_drvs: dict[str, list[str]] = {
        f"matrix-{binary}": [matrix_aggregate_drvs[binary]]
        for binary in plannable_binaries
    }

    descriptors, counters, violation_entries = _plan_all_binaries(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs,
        binaries=plannable_binaries,
        sys_name=sys_name,
        variant_lookups=variant_lookups,
        tc_ids=tc_ids,
    )

    _summary.emit_summary_log(
        binaries=plannable_binaries,
        counters=counters,
    )
    if counters.get("violations", 0) > 0:
        _summary.emit_violations_log(violation_entries)

    out_path = _write_outputs(
        matrix_eval_out_dir=matrix_eval_out_dir,
        descriptors=descriptors,
        binaries=plannable_binaries,
        counters=counters,
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=len(plannable_binaries),
        descriptor_count=len(descriptors),
        duration_seconds=max(0.0, clock_fn() - start),
        templates=counters.get("templates", 0),
        meta_templates=counters.get("meta_templates", 0),
        variants=counters.get("variants", 0),
        common_deps_cross_arch=counters.get("common_deps_cross_arch", 0),
        common_deps_family=counters.get("common_deps_family", 0),
        common_deps_uni_arch=counters.get("common_deps_uni_arch", 0),
        common_deps_arch_indep=counters.get("common_deps_arch_indep", 0),
        source_terminal_skipped=counters.get("source_terminal_skipped", 0),
        toolchain_wired=counters.get("toolchain_wired", 0),
        stdenv_subtrees=counters.get("stdenv_subtrees", 0),
        violations=counters.get("violations", 0),
    )


def _empty_result(
    *,
    matrix_eval_out_dir: pathlib.Path,
    duration: float,
) -> DependencyGraphResult:
    """Write an empty descriptor pickle + return a zero-counter result.

    Used on the two short-circuit paths (no archives discovered and no
    plannable binaries after lookup derivation) so the watcher's
    consumer sees a well-formed ``_dependency_graph.pkl`` even when
    there is nothing to plan.
    """
    out_path = _write_outputs(
        matrix_eval_out_dir=matrix_eval_out_dir,
        descriptors=[],
        binaries=[],
        counters={},
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=0,
        descriptor_count=0,
        duration_seconds=duration,
    )


def _import_all_archives(
    *,
    archives: list[pathlib.Path],
    runner: RunSubprocess,
) -> None:
    """Import every archive so the leaves the aggregate drv references
    are present in the local store.

    Post-D.1b: the import is still required (the streaming planner's
    ``nix-store --query --tree`` walk needs the closure resident
    locally), but we no longer derive kept drvs from the import
    stdout — D.1a's ``derive_variant_lookup_from_aggregate`` owns the
    variant_lookup, and the matrix aggregate drv replaces the leaf
    list passed to the sum-drv helper. The stdout from
    ``nix-store --import`` is therefore discarded; we only care about
    side effects (closure materialisation) and the success rc.
    """
    for archive in archives:
        _import_archive_or_raise(
            archive=archive, runner=runner, binary=archive.stem,
        )


def _derive_variant_lookups_from_aggregates(
    matrix_aggregate_drvs: dict[str, str],
) -> tuple[dict[str, dict[tuple[str, str], dict]], list[str]]:
    """Build the per-binary ``variant_lookup`` map + plannable-binary
    list from the matrix aggregate drv paths.

    Calls :func:`archive.derive_variant_lookup_from_aggregate` (D.1a)
    once per binary. Binaries whose aggregate yields an empty lookup
    are silently skipped — the ``make_sum_drv_from_paths`` contract
    forbids zero-variant matrices, and an empty lookup means the
    cluster produced no plannable variants for that binary in this
    run. The remaining binaries land in the returned
    ``plannable_binaries`` list in sorted order so downstream output
    (and operator log lines) stay deterministic.

    Raises :class:`DependencyGraphWorkerError` tagged with the binary
    + stage ``"variant_lookup"`` if the helper itself raises (e.g.
    malformed aggregate, missing closure entries).
    """
    variant_lookups: dict[str, dict[tuple[str, str], dict]] = {}
    plannable_binaries: list[str] = []
    for binary in sorted(matrix_aggregate_drvs):
        matrix_agg = matrix_aggregate_drvs[binary]
        try:
            lookup = _archive.derive_variant_lookup_from_aggregate(
                matrix_agg,
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyGraphWorkerError(
                binary=binary, stage="variant_lookup",
                message=(
                    "derive_variant_lookup_from_aggregate failed for "
                    f"{matrix_agg!r}: {exc}"
                ),
                cause=exc,
            ) from exc
        if not lookup:
            continue
        variant_lookups[binary] = lookup
        plannable_binaries.append(binary)
    return variant_lookups, plannable_binaries


def _import_archive_or_raise(
    *,
    archive: pathlib.Path,
    runner: RunSubprocess,
    binary: str,
) -> list[str]:
    """``nix-store --import`` the archive, surfacing the stdout paths.

    Raises :class:`DependencyGraphWorkerError` (stage ``"import"``)
    on failure so the worker driver can convert that to a non-zero
    exit. On success returns the parsed list of imported store
    paths.
    """
    ok, err, imported_paths = _archive.import_archive(
        archive, run_subprocess=runner,
    )
    if not ok:
        raise DependencyGraphWorkerError(
            binary=binary, stage="import",
            message=(
                "nix-store --import failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
        )
    return imported_paths


def _plan_all_binaries(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    matrix_drvs: dict[str, list[str]],
    binaries: list[str],
    sys_name: str,
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    tc_ids: dict[str, str],
) -> tuple[list[Any], dict[str, int], list[dict]]:
    """Build the multi-binary sum-drv, stream-plan it, and produce
    ``(descriptors, counters, violation_entries)`` spanning every
    plannable binary.

    Stages are surfaced via :class:`DependencyGraphWorkerError` tagged
    with ``binary="<all>"`` for the sum-drv / tree-query / plan steps.
    The planner pulls its own ``nix-store --query --tree`` stream via
    :func:`stream_drv_tree` so this helper no longer needs the
    subprocess-runner injection. ``build_sum_drv_multi`` and
    ``plan_total`` are resolved through the package namespace so test
    monkeypatches are honoured; see :func:`summary.invoke_planner`
    for the planner-call shim.
    """
    import importlib  # noqa: PLC0415
    _pkg = importlib.import_module(__package__)

    try:
        sum_drv = _pkg.build_sum_drv_multi(
            bash_path=bash_path,
            toolchain_drvs=toolchain_drvs,
            matrix_drvs=matrix_drvs,
            system=sys_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary="<all>", stage="sum_drv",
            message=f"sum-drv assembly failed: {exc}",
            cause=exc,
        ) from exc

    try:
        return _summary.invoke_planner(
            pkg=_pkg,
            sum_drv=sum_drv,
            binaries=binaries,
            variant_lookups=variant_lookups,
            tc_ids=tc_ids,
            sys_name=sys_name,
        )
    except RuntimeError as exc:
        # ``stream_drv_tree`` raises ``RuntimeError`` on a non-zero
        # ``nix-store --query --tree`` exit; keep the historical
        # ``stage="query_tree"`` label so operator-side error
        # introspection stays the same.
        raise DependencyGraphWorkerError(
            binary="<all>", stage="query_tree",
            message=str(exc), cause=exc,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary="<all>", stage="plan",
            message=f"plan_phase4 failed: {exc}",
            cause=exc,
        ) from exc


def _build_summary(
    *,
    descriptors: Sequence[Any],
    binaries: Sequence[str],
    counters: dict[str, int],
) -> dict[str, Union[int, float, str]]:
    """Build the ``summary`` dict embedded in the pickle payload and
    serialised to ``_dependency_graph_summary.txt``.

    Combines the planner-emitted counters (templates / meta_templates /
    common_deps_* / violations / etc.) with descriptor-derived
    aggregates (per-kind counts, per-priority_hint counts, binary list).
    """
    summary: dict[str, Union[int, float, str]] = dict(counters)
    summary["binary_count"] = len(binaries)
    summary["descriptor_count"] = len(descriptors)
    summary["binaries"] = "/".join(binaries) if binaries else "<none>"

    by_kind: collections.Counter = collections.Counter(
        getattr(d, "kind", "<unknown>") for d in descriptors
    )
    for kind, count in by_kind.items():
        summary[f"descriptors_by_kind.{kind}"] = count

    by_priority: collections.Counter = collections.Counter(
        getattr(d, "priority_hint", 0) for d in descriptors
    )
    for hint, count in by_priority.items():
        summary[f"descriptors_by_priority_hint.{hint}"] = count

    return summary


def _write_outputs(
    *,
    matrix_eval_out_dir: pathlib.Path,
    descriptors: Sequence[Any],
    binaries: Sequence[str],
    counters: dict[str, int],
) -> pathlib.Path:
    """Write the pickle + summary-text companion atomically and return
    the pickle path (the canonical ``output_path`` reported in
    :class:`DependencyGraphResult`).
    """
    summary = _build_summary(
        descriptors=descriptors, binaries=binaries, counters=counters,
    )
    pickle_path = matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_PICKLE
    summary_path = matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_SUMMARY
    _output.write_phase4_descriptors(
        descriptors=descriptors, summary=summary, out_path=pickle_path,
    )
    _output.write_phase4_summary_text(
        summary=summary, out_path=summary_path,
    )
    # Per-(binary, compiler, arch) sidecar manifests for the
    # placeholder build_variant + build_common_dep workers (PH-A in
    # plan/placeholder-pattern-restructure.md). Same descriptor set,
    # just sliced and re-serialised so each cell's worker reads only
    # its own bytes at dispatch.
    _output.write_per_cell_manifests(
        descriptors=descriptors,
        matrix_eval_out_dir=matrix_eval_out_dir,
    )
    return pickle_path

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
from . import sum_drv as _sum_drv
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
    toolchain_drvs: list[str],
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
) -> DependencyGraphResult:
    """Walk every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``
    and produce ``_dependency_graph.pkl`` (plus the
    ``_dependency_graph_summary.txt`` companion).

    Plan §5.2 unifies the previous per-binary loop into:

      1. discover + import every archive (per-binary skip-if-present
         retained);
      2. assemble ONE multi-binary sum-drv via
         :func:`build_sum_drv_multi`;
      3. ``nix-store --query --tree`` ONCE on the sum-drv;
      4. ``plan_from_tree_streaming`` ONCE — the
         :class:`StreamPlanner` instance dedups identically-shaped
         templates across binaries via ``find_or_register_template``
         and aggregates per-binary MetaTemplates inside
         ``finalize()``;
      5. one phase-4 descriptor list emitted for all binaries.

    Any per-binary failure raises :class:`DependencyGraphWorkerError`
    tagged with the binary + stage; the caller's main loop translates
    that into a non-zero exit code.
    """
    clock_fn = clock or time.monotonic
    start = clock_fn()

    # Unlink any stale output before planning so a mid-run crash cannot
    # leave the watcher reading a previous run's artefact.
    stale_out_path = matrix_eval_out_dir / "_dependency_graph.pkl"
    stale_out_path.unlink(missing_ok=True)

    archives = _archive.discover_archives(matrix_eval_out_dir)
    if not archives:
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
            duration_seconds=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})

    matrix_drvs, variant_lookups, plannable_binaries = (
        _collect_and_import_archives(
            archives=archives,
            runner=runner,
        )
    )

    if not plannable_binaries:
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
            duration_seconds=max(0.0, clock_fn() - start),
        )

    descriptors, counters, violation_entries = _plan_all_binaries(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs,
        binaries=plannable_binaries,
        sys_name=sys_name,
        variant_lookups=variant_lookups,
        tc_ids=tc_ids,
        runner=runner,
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


def _collect_and_import_archives(
    *,
    archives: list[pathlib.Path],
    runner: RunSubprocess,
) -> tuple[
    dict[str, list[str]],
    dict[str, dict[tuple[str, str], dict]],
    list[str],
]:
    """Import every archive and derive the kept-drv + variant_lookup
    per binary from the import-stdout paths.

    Returns ``(matrix_drvs, variant_lookups, plannable_binaries)``:

      * ``matrix_drvs`` — ``{"matrix-<binary>": [<drv>, ...]}`` ready
        to feed :func:`build_sum_drv_multi`.
      * ``variant_lookups`` — ``{binary: {(arch, label): variant_spec}}``
        ready to feed the planner per-binary slice.
      * ``plannable_binaries`` — deterministic ordered list of binary
        names (== sorted archive stems) that produced a non-empty
        kept-drv list. Archives with no kept drvs are skipped here so
        an empty matrix doesn't poison the multi-binary sum-drv (the
        ``make_sum_drv_from_paths`` contract forbids zero-variant
        matrices).

    Every archive is imported unconditionally: ``nix-store --import``'s
    stdout IS the kept-drv source post-cutover, so no probe-then-skip
    optimisation can fire ahead of the import. Re-importing a
    fully-resident archive is cheap inside nix-store (no duplicate
    inserts) — the cost is one archive read per quiesce, not one full
    store materialisation.
    """
    matrix_drvs: dict[str, list[str]] = {}
    variant_lookups: dict[str, dict[tuple[str, str], dict]] = {}
    plannable_binaries: list[str] = []

    for archive in archives:
        binary = archive.stem
        imported_paths = _import_archive_or_raise(
            archive=archive, runner=runner, binary=binary,
        )
        kept_drvs, variant_lookup = (
            _archive.discover_kept_drvs_from_imported_store(imported_paths)
        )
        if not kept_drvs:
            # No ``*-elf-folder.drv`` in the import stream — the
            # archive carried no plannable variants. Mirrors the old
            # "no kept-drv source" skip path.
            continue

        matrix_drvs[f"matrix-{binary}"] = kept_drvs
        variant_lookups[binary] = variant_lookup
        plannable_binaries.append(binary)

    return matrix_drvs, variant_lookups, plannable_binaries


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
    runner: RunSubprocess,
) -> tuple[list[Any], dict[str, int], list[dict]]:
    """Build the multi-binary sum-drv, query its tree, and produce
    ``(descriptors, counters, violation_entries)`` spanning every
    plannable binary.

    Stages are surfaced via :class:`DependencyGraphWorkerError` tagged
    with ``binary="<all>"`` for the sum-drv / tree-query / plan steps.
    ``build_sum_drv_multi`` and ``plan_total`` are resolved through
    the package namespace so test monkeypatches are honoured; see
    :func:`summary.invoke_planner` for the planner-call shim.
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
        tree_text = _sum_drv.query_drv_tree(sum_drv, run_subprocess=runner)
    except RuntimeError as exc:
        raise DependencyGraphWorkerError(
            binary="<all>", stage="query_tree",
            message=str(exc), cause=exc,
        ) from exc

    try:
        return _summary.invoke_planner(
            pkg=_pkg,
            tree_text=tree_text,
            binaries=binaries,
            variant_lookups=variant_lookups,
            tc_ids=tc_ids,
            sys_name=sys_name,
        )
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
    return pickle_path

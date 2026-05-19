"""Top-level driver: archive walk → multi-binary sum-drv → one streaming
pass → descriptors.

Phase 5.2 collapse: every archive contributes ONE matrix wrapper to a
single sum-drv; the streaming planner runs ONCE over the resulting
tree so cross-binary template dedup fires automatically inside the
single :class:`StreamPlanner` instance. A single descriptor list is
emitted to ``_dependency_graph.json``.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable
from typing import Any, Optional

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
    manifest_dir: Optional[pathlib.Path],
    bash_path: str,
    toolchain_drvs: list[str],
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
    skip_import_when_present: bool = True,
) -> DependencyGraphResult:
    """Walk every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``
    and produce ``_dependency_graph.json``.

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
        out_path = _output.write_dependency_graph_json(
            matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_JSON, []
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
            manifest_dir=manifest_dir,
            runner=runner,
            skip_import_when_present=skip_import_when_present,
        )
    )

    if not plannable_binaries:
        out_path = _output.write_dependency_graph_json(
            matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_JSON, []
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

    out_path = _output.write_dependency_graph_json(
        matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_JSON, descriptors
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
    manifest_dir: Optional[pathlib.Path],
    runner: RunSubprocess,
    skip_import_when_present: bool,
) -> tuple[
    dict[str, list[str]],
    dict[str, dict[tuple[str, str], dict]],
    list[str],
]:
    """Walk every archive, surfacing kept drvs + the variant lookup
    per binary, and import any archive whose kept drvs are not all
    locally present.

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
    """
    matrix_drvs: dict[str, list[str]] = {}
    variant_lookups: dict[str, dict[tuple[str, str], dict]] = {}
    plannable_binaries: list[str] = []

    for archive in archives:
        binary = archive.stem
        kept_drvs, variant_lookup = _archive.discover_kept_drvs(
            archive, manifest_dir,
        )
        if not kept_drvs:
            # Logged inside discover_kept_drvs; treat as skip.
            continue

        _maybe_import_archive(
            archive=archive,
            kept_drvs=kept_drvs,
            runner=runner,
            skip_import_when_present=skip_import_when_present,
            binary=binary,
        )

        matrix_drvs[f"matrix-{binary}"] = kept_drvs
        variant_lookups[binary] = variant_lookup
        plannable_binaries.append(binary)

    return matrix_drvs, variant_lookups, plannable_binaries


def _maybe_import_archive(
    *,
    archive: pathlib.Path,
    kept_drvs: list[str],
    runner: RunSubprocess,
    skip_import_when_present: bool,
    binary: str,
) -> None:
    """Import the archive unless every kept drv is already locally present."""
    need_import = True
    if skip_import_when_present:
        need_import = not all(
            _archive.is_path_locally_present(p, run_subprocess=runner)
            for p in kept_drvs
        )
    if not need_import:
        return
    ok, err = _archive.import_archive(archive, run_subprocess=runner)
    if not ok:
        raise DependencyGraphWorkerError(
            binary=binary, stage="import",
            message=(
                "nix-store --import failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
        )


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

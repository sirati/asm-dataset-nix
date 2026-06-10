"""Top-level driver: archive walk → multi-binary sum-drv → one streaming
pass → descriptors.

Phase 5.2 collapse: every archive contributes ONE matrix wrapper to a
single sum-drv; the streaming planner runs ONCE over the resulting
tree so cross-binary template dedup fires automatically inside the
single :class:`StreamPlanner` instance. A single descriptor list is
produced spanning all binaries (a human-readable
``_dependency_graph_summary.txt`` is written for operator inspection).
"""

from __future__ import annotations

import collections
import logging
import pathlib
import time
from collections.abc import Callable, Sequence
from typing import Any, Optional, Union

from . import archive as _archive
from . import output as _output
from . import summary as _summary
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .subproc import RunSubprocess, default_run_subprocess


_LOG = logging.getLogger(__name__)


__all__ = [
    "run_dependency_graph_task",
]


def run_dependency_graph_task(
    *,
    matrix_eval_out_dir: pathlib.Path,
    bash_path: str,
    toolchain_aggregate_drv: str,
    binary: Optional[str] = None,
    matrix_drv: Optional[str] = None,
    matrix_drvs: Optional[dict[str, str]] = None,
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
) -> DependencyGraphResult:
    """Assemble one sum-drv spanning ALL binaries' pre-built aggregate
    drvs and produce a single Phase 4 descriptor list (plus the
    ``_dependency_graph_summary.txt`` companion).

    Single all-binaries dispatch: the framework runs ONE
    dependency_graph task over every binary, fed each binary's
    ``matrix_aggregate_drv`` via the corresponding matrix_eval
    predecessor task's keyed outputs. The streaming planner runs ONCE
    over the combined tree so cross-binary template dedup fires inside
    the single :class:`StreamPlanner` instance.

      1. import every ``matrix-<binary>.drv.archive`` so the closures
         (and therefore the leaves each aggregate drv references) are
         materialised in the local store — required for the
         ``nix-store --query --tree`` walk further down;
      2. derive each binary's ``variant_lookup`` from its matrix
         aggregate via :func:`archive.derive_variant_lookup_from_aggregate`
         (D.1a);
      3. wrap every binary's aggregate drv in the multi-binary
         ``matrix_drvs`` mapping and call :func:`build_sum_drv_multi`,
         which in turn calls
         :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
         (the sole ``nix-instantiate`` in this phase);
      4. ``plan_from_drv_tree`` ONCE — the planner streams
         ``nix-store --query --tree`` tuples directly from
         :func:`stream_drv_tree`;
      5. one phase-4 descriptor list emitted spanning all binaries.

    Inputs: pass either ``matrix_drvs`` (a ``{binary: matrix_drv}``
    mapping, the all-binaries dispatch path) OR the single-binary
    ``binary`` + ``matrix_drv`` pair (back-compat / ad-hoc CLI path).
    ``toolchain_aggregate_drv`` MUST be non-empty — phase 3 cannot run
    without the producer's toolchain output.

    Per-binary failures raise :class:`DependencyGraphWorkerError`
    tagged with the binary + stage; the caller's main loop translates
    that into a non-zero exit code.
    """
    if not toolchain_aggregate_drv:
        raise ValueError(
            "run_dependency_graph_task: toolchain_aggregate_drv is "
            "empty; phase 3 requires the producer's toolchain wrapper "
            "drv path (see preflight._build_toolchains_aggregate_drv)."
        )

    # Normalise the two input shapes into a single ``{binary:
    # matrix_drv}`` mapping. The all-binaries dispatch passes
    # ``matrix_drvs``; the legacy / CLI path passes ``binary`` +
    # ``matrix_drv``.
    drv_by_binary: dict[str, str]
    if matrix_drvs is not None:
        if binary is not None or matrix_drv is not None:
            raise ValueError(
                "run_dependency_graph_task: pass either matrix_drvs OR "
                "the single-binary binary/matrix_drv pair, not both."
            )
        drv_by_binary = dict(matrix_drvs)
        if not drv_by_binary:
            raise ValueError(
                "run_dependency_graph_task: matrix_drvs is empty; phase "
                "3 requires at least one matrix-<binary> aggregate drv "
                "(see workers/build_worker dep_graph dispatch)."
            )
        for b, d in drv_by_binary.items():
            if not b:
                raise ValueError(
                    "run_dependency_graph_task: matrix_drvs contains an "
                    "empty binary name."
                )
            if not d:
                raise ValueError(
                    "run_dependency_graph_task: matrix_drvs[%r] is empty; "
                    "phase 3 requires each binary's matrix-<binary> "
                    "aggregate drv." % b
                )
    else:
        if not binary:
            raise ValueError(
                "run_dependency_graph_task: binary is empty; phase 3 "
                "requires the binary name this run plans."
            )
        if not matrix_drv:
            raise ValueError(
                "run_dependency_graph_task: matrix_drv is empty; phase 3 "
                "requires the matrix-<binary> aggregate drv (see "
                "workers/build_worker dep_graph dispatch)."
            )
        drv_by_binary = {binary: matrix_drv}

    clock_fn = clock or time.monotonic
    start = clock_fn()

    archives = _archive.discover_archives(matrix_eval_out_dir)
    if not archives:
        return _empty_result(
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})

    # Step 1: import the shared toolchain archive FIRST (toolchain-first),
    # then every per-binary archive. With toolchain dedup the per-binary
    # archives are diffs against the toolchain closure, so the toolchain
    # must be resident locally before they import. The leaves the
    # aggregate drv references must all be present before the
    # `nix-store --query --tree` walk further down.
    _import_toolchain_archive_or_raise(
        matrix_eval_out_dir=matrix_eval_out_dir, runner=runner,
    )
    _import_all_archives(archives=archives, runner=runner)

    # Step 2: derive the variant lookup for every binary from its
    # matrix aggregate drv (D.1a's helper). Binaries whose aggregate
    # yields an empty lookup are skipped (the path-form sum-drv helper
    # forbids zero-variant matrix wrappers).
    variant_lookups, plannable_binaries = (
        _derive_variant_lookups(drv_by_binary=drv_by_binary)
    )

    if not plannable_binaries:
        return _empty_result(
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    # Step 3: wrap each binary's aggregate drv in a length-1 list keyed
    # by ``matrix-<binary>`` for the path-form sum-drv helper
    # (post-Phase-A.2 invariant).
    toolchain_drvs: list[str] = [toolchain_aggregate_drv]
    matrix_drvs_for_sum: dict[str, list[str]] = {
        f"matrix-{b}": [drv_by_binary[b]]
        for b in plannable_binaries
    }

    descriptors, counters, violation_entries = _plan_all_binaries(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs_for_sum,
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
    """Write an empty summary + return a zero-counter result.

    Used on the two short-circuit paths (no archives discovered and no
    plannable binaries after lookup derivation) so the operator-facing
    ``_dependency_graph_summary.txt`` is well-formed even when there is
    nothing to plan.
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


def _import_toolchain_archive_or_raise(
    *,
    matrix_eval_out_dir: pathlib.Path,
    runner: RunSubprocess,
) -> None:
    """Import the shared ``toolchains.drv.archive`` (toolchain-first).

    Toolchain-dedup pre-flight writes ONE ``toolchains.drv.archive``
    carrying the whole compiler-toolchain closure; the per-binary
    ``matrix-<binary>.drv.archive`` files are diffs against it. This
    archive MUST import before any per-binary archive or those diffs
    cannot resolve. Fatal (:class:`DependencyGraphWorkerError`, stage
    ``"toolchain_import"``) if the archive is missing or zero-byte —
    every diff archive would be un-importable otherwise.
    """
    archive = _archive.toolchain_archive_path(matrix_eval_out_dir)
    try:
        size = archive.stat().st_size
    except OSError:
        size = -1
    if size <= 0:
        raise DependencyGraphWorkerError(
            binary="<toolchain>", stage="toolchain_import",
            message=(
                "toolchains.drv.archive missing or zero-byte at "
                f"{archive} (size={size}); the per-binary diff archives "
                "are un-importable without it — was the submit "
                "pre-flight toolchain export skipped?"
            ),
        )
    ok, err, _imported = _archive.import_archive(archive, run_subprocess=runner)
    if not ok:
        raise DependencyGraphWorkerError(
            binary="<toolchain>", stage="toolchain_import",
            message=(
                "nix-store --import of toolchains.drv.archive failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
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
        # A zero-byte per-binary archive means the binary was fully gated
        # (all archs filtered out) — there is nothing to import. Skip it
        # (not an error); ``nix-store --import`` on empty input would be a
        # harmless no-op anyway, but skipping avoids the spurious call.
        try:
            if archive.stat().st_size == 0:
                continue
        except OSError:
            pass
        _import_archive_or_raise(
            archive=archive, runner=runner,
            binary=_archive.binary_from_archive_name(archive),
        )


def _derive_variant_lookups(
    *,
    drv_by_binary: dict[str, str],
) -> tuple[dict[str, dict[tuple[str, str], dict]], list[str]]:
    """Build the per-binary ``variant_lookup`` map + plannable-binary
    list from every binary's matrix aggregate drv path.

    Calls :func:`archive.derive_variant_lookup_from_aggregate` (D.1a)
    on each binary's aggregate. A binary whose helper yields an empty
    lookup is silently skipped — ``make_sum_drv_from_paths`` forbids
    zero-variant matrices, and an empty lookup means the cluster
    produced no plannable variants for that binary in this run. The
    returned ``plannable_binaries`` list is sorted and contains only
    the binaries with a non-empty lookup.

    Raises :class:`DependencyGraphWorkerError` tagged with the offending
    binary + stage ``"variant_lookup"`` if the helper itself raises
    (e.g. malformed aggregate, missing closure entries).
    """
    lookups: dict[str, dict[tuple[str, str], dict]] = {}
    plannable: list[str] = []
    for binary in sorted(drv_by_binary):
        matrix_drv = drv_by_binary[binary]
        try:
            lookup = _archive.derive_variant_lookup_from_aggregate(
                matrix_drv,
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyGraphWorkerError(
                binary=binary, stage="variant_lookup",
                message=(
                    "derive_variant_lookup_from_aggregate failed for "
                    f"{matrix_drv!r}: {exc}"
                ),
                cause=exc,
            ) from exc
        if not lookup:
            continue
        lookups[binary] = lookup
        plannable.append(binary)
    return lookups, plannable


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
    """Build the ``summary`` dict serialised to
    ``_dependency_graph_summary.txt``.

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
    """Write the human-readable summary text atomically and return its
    path (the canonical ``output_path`` reported in
    :class:`DependencyGraphResult`).
    """
    summary = _build_summary(
        descriptors=descriptors, binaries=binaries, counters=counters,
    )
    summary_path = matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_SUMMARY
    _output.write_phase4_summary_text(
        summary=summary, out_path=summary_path,
    )
    return summary_path

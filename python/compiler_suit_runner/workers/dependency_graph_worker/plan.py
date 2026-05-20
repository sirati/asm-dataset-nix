"""Streaming-planner driver: sum-drv path → phase-4 descriptors.

Two entry points:

  * :func:`plan_binary` — single-binary path kept for back-compat with
    existing tests that monkey-patch it.
  * :func:`plan_total` — multi-binary path used by ``run_dependency_graph_task``
    after the per-binary loop collapse. Runs the streaming planner ONCE
    on a multi-binary sum-drv and partitions the result per binary
    before feeding :func:`plan_phase4_from_graph`.
  * :func:`plan_total_with_counters` — same as :func:`plan_total` but
    also returns the per-category integer counters used in the worker's
    post-planning summary log (Phase 6.1b).
  * :func:`compute_dependency_graph_counters` — pure-function counter
    aggregation over a streaming result + descriptor list; exported so
    the worker (and tests) can derive counters without re-running the
    planner.
"""

from __future__ import annotations

from typing import Any, Mapping


from .counters import compute_dependency_graph_counters
from .slice import _group_templates_by_binary, _slice_streaming_result


__all__ = [
    "plan_binary",
    "plan_total",
    "plan_total_with_counters",
    "compute_dependency_graph_counters",
]


def plan_binary(
    *,
    binary: str,
    sum_drv: str,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
    lax: bool = True,
) -> list[Any]:
    """Run the streaming planner + dependency_graph_planner adapter
    against ``sum_drv`` for one binary.

    ``sum_drv`` is the multi-binary sum-drv path; ``stream_drv_tree``
    spawns ``nix-store --query --tree`` and feeds the planner tuples
    as they arrive.

    Returns the list of :class:`Phase4Descriptor` records. Raises
    :class:`DependencyGraphCycleError` (from the adapter) on cycle
    detection; the caller logs + propagates.

    ``lax`` defaults to ``True`` (Phase 6.1 production default): the
    streaming planner records shape violations into
    ``streaming_result["violations"]`` instead of raising on the first
    calibration / cowalk mismatch. Worst case the planner emits
    redundant rebuilds for affected templates; violations are surfaced
    via :class:`DependencyGraphResult` so the run log preserves
    visibility for follow-up investigation.
    """
    from template_graph.streaming import plan_from_drv_tree  # noqa: PLC0415
    from compiler_suit_runner.workers.dependency_graph_worker.sum_drv import (  # noqa: PLC0415
        stream_drv_tree,
    )
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
        plan_phase4_from_graph,
    )

    streaming_result = plan_from_drv_tree(stream_drv_tree(sum_drv), lax=lax)
    inp = BinaryPlanInput(
        binary=binary,
        streaming_result=streaming_result,
        variant_lookup=variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    return plan_phase4_from_graph([inp], sys_name=sys_name)


def plan_total(
    *,
    sum_drv: str,
    binaries: list[str],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
    lax: bool = True,
) -> list[Any]:
    """Run ONE streaming pass against ``sum_drv`` and emit a single
    flat phase-4 descriptor list spanning all binaries.

    ``binaries`` is the deterministic ordered list of binary names that
    the caller assembled into the multi-binary sum-drv. ``variant_lookups``
    maps each binary to its own ``(arch, label) -> variant_spec`` dict.

    The streaming planner runs ONCE; cross-binary template dedup fires
    within the single :class:`StreamPlanner` instance (its ``templates``
    + ``find_or_register_template`` collapse equivalent shapes, and the
    finalize pass keys ``meta_templates`` / ``arch_indep_deps`` per
    binary). We then partition ``streaming_result["variant_arrays"]``
    + ``["common_deps_per_arch_template"]`` per binary by inspecting
    each template's root role (``<binary>-...-elf-folder.drv``) so each
    binary's :class:`BinaryPlanInput` only sees its own variants.

    ``lax`` defaults to ``True`` (Phase 6.1 production default): the
    streaming planner records shape violations into
    ``streaming_result["violations"]`` instead of raising on the first
    calibration / cowalk mismatch. Worst case the planner emits
    redundant rebuilds for affected templates; violations are surfaced
    via :class:`DependencyGraphResult` so the run log preserves
    visibility for follow-up investigation.

    Raises :class:`DependencyGraphCycleError` on cycle detection.
    """
    descriptors, _streaming = _plan_total_impl(
        sum_drv=sum_drv,
        binaries=binaries,
        variant_lookups=variant_lookups,
        toolchain_task_ids=toolchain_task_ids,
        sys_name=sys_name,
        lax=lax,
    )
    return descriptors


def plan_total_with_counters(
    *,
    sum_drv: str,
    binaries: list[str],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
    lax: bool = True,
) -> tuple[list[Any], dict[str, int], list[dict]]:
    """Variant of :func:`plan_total` that also returns the per-category
    integer counters used in the worker's post-planning summary log
    and the survey-mode violation entries the streaming planner recorded.

    Returns ``(descriptors, counters, violation_entries)``. ``counters``
    keys match the integer fields on :class:`DependencyGraphResult`
    (Phase 6.1b); ``violation_entries`` is the planner's
    ``streaming_result["violations"]`` list, ready for the worker's
    WARN-level dump when non-empty.
    """
    descriptors, streaming_result = _plan_total_impl(
        sum_drv=sum_drv,
        binaries=binaries,
        variant_lookups=variant_lookups,
        toolchain_task_ids=toolchain_task_ids,
        sys_name=sys_name,
        lax=lax,
    )
    counters = compute_dependency_graph_counters(
        streaming_result=streaming_result,
        descriptors=descriptors,
        binaries=binaries,
    )
    violation_entries = list(streaming_result.get("violations", []) or [])
    return descriptors, counters, violation_entries


def _plan_total_impl(
    *,
    sum_drv: str,
    binaries: list[str],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
    lax: bool = True,
) -> tuple[list[Any], Mapping[str, Any]]:
    """Shared body of :func:`plan_total` and :func:`plan_total_with_counters`.

    Runs ONE streaming pass and returns both the emitted phase-4
    descriptors and the raw streaming-planner result dict (the latter
    is consumed by :func:`compute_dependency_graph_counters` and is not
    part of the public worker contract).
    """
    from template_graph.streaming import plan_from_drv_tree  # noqa: PLC0415
    from compiler_suit_runner.workers.dependency_graph_worker.sum_drv import (  # noqa: PLC0415
        stream_drv_tree,
    )
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
        plan_phase4_from_graph,
    )

    streaming_result = plan_from_drv_tree(stream_drv_tree(sum_drv), lax=lax)

    # Group templates by the binary their root role encodes. Each
    # binary's BinaryPlanInput consumes a sliced streaming_result
    # carrying ONLY the templates / variant_arrays / classifications
    # that belong to that binary; toolchain_drvs and arch_indep_deps
    # are shared (the planner adapter only reads variant_arrays +
    # classifications + templates from the per-binary slice).
    templates = list(streaming_result.get("templates", []) or [])
    binary_to_template_ids = _group_templates_by_binary(templates)

    inputs: list[BinaryPlanInput] = []
    for binary in binaries:
        owned_ids = binary_to_template_ids.get(binary, set())
        sliced = _slice_streaming_result(streaming_result, owned_ids)
        inputs.append(BinaryPlanInput(
            binary=binary,
            streaming_result=sliced,
            variant_lookup=variant_lookups.get(binary, {}),
            toolchain_task_ids=toolchain_task_ids,
        ))

    descriptors = plan_phase4_from_graph(inputs, sys_name=sys_name)
    return descriptors, streaming_result

"""Pure-function counter aggregation for the dependency_graph_worker.

Phase 6.1b (plan §E8): the worker logs a one-line summary after
planning completes, listing how many templates / variants / common-deps
of each category were emitted, plus the diagnostic counters surfaced
by the streaming planner (``source_terminal_skipped``, ``violations``).

These counters are computed from the streaming-planner result dict
returned by :func:`template_graph.streaming.plan_from_tree_streaming`
PLUS the phase-4 descriptor list emitted by the planner adapter.
Splitting category-by-task_id-prefix lets the adapter remain the sole
authority on common-dep classification (cross_arch / family / arch_indep
/ per-cell), and avoids re-deriving the classification here.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


__all__ = ["compute_dependency_graph_counters"]


# Descriptor task_id prefixes (kept in sync with
# dependency_graph_planner.descriptors._common_dep_task_id and
# dependency_graph_planner.plan_meta._{cross_arch,family}_task_id and
# dependency_graph_planner.descriptors._arch_indep_task_id).
_PREFIX_CROSS_ARCH = "build_common_dep__cross_arch__"
_PREFIX_FAMILY = "build_common_dep__family__"
_PREFIX_ARCH_INDEP = "build_common_dep__arch_indep__"
_PREFIX_COMMON_DEP = "build_common_dep__"
_PREFIX_VARIANT = "build_variant__"
_PREFIX_TOOLCHAIN = "build_compilers__"


def compute_dependency_graph_counters(
    *,
    streaming_result: Mapping[str, Any],
    descriptors: Iterable[Any],
    binaries: Iterable[str],
) -> dict[str, int]:
    """Aggregate per-category counters for the worker summary log.

    Returns a dict whose keys match the integer fields on
    :class:`DependencyGraphResult` (Phase 6.1b). Unknown / missing
    streaming-result fields default to 0 so the function is safe to
    call on the test-stub streaming dicts that omit most of the heavy
    bookkeeping.
    """
    descriptor_list = list(descriptors)
    common_categories = _classify_common_deps(descriptor_list)
    variants_total, toolchain_wired = _variant_metrics(descriptor_list)
    return {
        "templates": _len_or_zero(streaming_result.get("templates")),
        "meta_templates": _sum_meta_templates(
            streaming_result.get("meta_templates"), binaries,
        ),
        "variants": variants_total,
        "common_deps_cross_arch": common_categories["cross_arch"],
        "common_deps_family": common_categories["family"],
        "common_deps_uni_arch": common_categories["uni_arch"],
        "common_deps_arch_indep": common_categories["arch_indep"],
        "source_terminal_skipped": _int_or_zero(
            streaming_result.get("source_terminal_skipped"),
        ),
        "toolchain_wired": toolchain_wired,
        "stdenv_subtrees": _len_or_zero(
            streaming_result.get("stdenv_subtrees"),
        ),
        "violations": _len_or_zero(streaming_result.get("violations")),
    }


def _classify_common_deps(descriptors: list[Any]) -> dict[str, int]:
    """Bucket every ``build_common_dep`` descriptor into one of four
    plan-§E2 sub-categories by its ``task_id`` prefix.
    """
    out = {"cross_arch": 0, "family": 0, "arch_indep": 0, "uni_arch": 0}
    for d in descriptors:
        if _kind_of(d) != "build_common_dep":
            continue
        task_id = _task_id_of(d)
        if task_id.startswith(_PREFIX_CROSS_ARCH):
            out["cross_arch"] += 1
        elif task_id.startswith(_PREFIX_FAMILY):
            out["family"] += 1
        elif task_id.startswith(_PREFIX_ARCH_INDEP):
            out["arch_indep"] += 1
        elif task_id.startswith(_PREFIX_COMMON_DEP):
            # Plain per-cell common_dep (one of the cross_arch / family
            # / arch_indep prefixes above would have already matched
            # because they all start with build_common_dep__).
            out["uni_arch"] += 1
    return out


def _variant_metrics(descriptors: list[Any]) -> tuple[int, int]:
    """Return ``(variants_total, toolchain_wired)``.

    ``toolchain_wired`` counts the number of ``build_variant``
    descriptors whose ``depends_on`` contains at least one
    ``build_compilers__`` task_id (i.e. the variant transitively
    depends on a framework-emitted toolchain build task).
    """
    variants_total = 0
    toolchain_wired = 0
    for d in descriptors:
        if _kind_of(d) != "build_variant":
            continue
        variants_total += 1
        deps = _depends_on(d)
        if any(dep.startswith(_PREFIX_TOOLCHAIN) for dep in deps):
            toolchain_wired += 1
    return variants_total, toolchain_wired


def _sum_meta_templates(
    meta_templates: Any, binaries: Iterable[str],
) -> int:
    """Sum the per-binary MetaTemplate counts across ``binaries``.

    ``meta_templates`` is the ``{binary: [MetaTemplate, ...]}`` mapping
    surfaced by ``finalize()``; binaries not present in the mapping
    contribute zero. Iterating ``binaries`` (rather than ``meta_templates.keys()``)
    keeps the count scoped to the planner's binary list so spurious
    keys (e.g. ``"<unknown>"`` from templates with no elf-folder root)
    don't inflate the sum.
    """
    if not isinstance(meta_templates, Mapping):
        return 0
    total = 0
    for binary in binaries:
        per_binary = meta_templates.get(binary)
        if per_binary is None:
            continue
        try:
            total += len(per_binary)
        except TypeError:
            continue
    return total


def _kind_of(descriptor: Any) -> str:
    kind = getattr(descriptor, "kind", None)
    if kind is None and isinstance(descriptor, Mapping):
        kind = descriptor.get("kind")
    return str(kind) if kind is not None else ""


def _task_id_of(descriptor: Any) -> str:
    task_id = getattr(descriptor, "task_id", None)
    if task_id is None and isinstance(descriptor, Mapping):
        task_id = descriptor.get("task_id")
    return str(task_id) if task_id is not None else ""


def _depends_on(descriptor: Any) -> tuple[str, ...]:
    deps = getattr(descriptor, "depends_on", None)
    if deps is None and isinstance(descriptor, Mapping):
        deps = descriptor.get("depends_on", ())
    if not deps:
        return ()
    return tuple(str(d) for d in deps)


def _len_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return len(value)
    except TypeError:
        return 0


def _int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

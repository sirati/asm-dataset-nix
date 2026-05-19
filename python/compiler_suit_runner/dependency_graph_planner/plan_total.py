"""Top-level planner: walk the streaming-planner output cell-by-cell
and emit phase-4 descriptors.

The cell-level helpers live in :mod:`.plan_cell`; this module owns the
per-binary :func:`plan_phase4_for_binary` driver and its multi-binary
wrapper :func:`plan_phase4_from_graph` that deduplicates cross-binary
common-deps.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .cycle import _check_no_cycles
from .descriptors import BinaryPlanInput, Phase4Descriptor
from .plan_cell import (
    _load_source_terminal_predicate,
    _plan_cell,
    _resolve_arch_indep_descriptors,
)
from .plan_meta import (
    _arch_to_labels_from_variant_arrays,
    _plan_meta_for_binary,
)
from .shapes import (
    _coerce_toolchain_node_ids,
    _iter_classifications,
    _iter_variant_arrays,
    _toolchain_idents_by_name,
)


# ``_load_source_terminal_predicate`` is re-exported here for the
# legacy import path ``dependency_graph_planner._load_source_terminal_predicate``
# (live module attribute resolution paths used in monkeypatching).
__all__ = [
    "_load_source_terminal_predicate",
    "plan_phase4_for_binary",
    "plan_phase4_from_graph",
]


def plan_phase4_for_binary(
    binary: str,
    streaming_result: Mapping[str, Any],
    variant_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    sys_name: str = "x86_64-linux",
    toolchain_task_ids: Mapping[str, str] = (),  # type: ignore[assignment]
) -> list[Phase4Descriptor]:
    """Translate one binary's streaming planner output into phase-4
    descriptors.

    Returns descriptors in a stable order: every
    ``build_common_dep`` first (sorted by ``(arch, node_name, ident)``),
    then every ``build_variant`` (sorted by ``(arch, label)``). This
    mirrors the legacy phase-1 planner's emit ordering so the framework
    matcher and any human reading the spawn log get a deterministic
    view.

    Raises :class:`DependencyGraphCycleError` if the streaming result's
    templates contain a cycle (defensive guard -- see module docstring).

    ``toolchain_task_ids`` defaults to an empty mapping; explicit empty
    is allowed via ``{}``.
    """
    if not isinstance(toolchain_task_ids, Mapping):
        toolchain_task_ids = dict(toolchain_task_ids)  # type: ignore[arg-type]

    templates = list(streaming_result.get("templates", []) or [])
    variant_arrays = streaming_result.get("variant_arrays", {}) or {}
    classifications = streaming_result.get("common_deps_per_arch_template", {})
    toolchain_node_ids = _coerce_toolchain_node_ids(
        streaming_result.get("toolchain_node_ids_per_template", {})
    )
    toolchain_idents_by_name = _toolchain_idents_by_name(
        streaming_result.get("toolchain_drvs", set())
    )
    arch_indep_deps_raw = streaming_result.get("arch_indep_deps", {}) or {}

    _check_no_cycles(templates)

    classification_map: dict[tuple[int, str], dict[int, str]] = dict(
        _iter_classifications(classifications)
    )

    arch_indep_descriptors, arch_indep_dep_task_ids = (
        _resolve_arch_indep_descriptors(
            binary=binary,
            sys_name=sys_name,
            arch_indep_deps_raw=arch_indep_deps_raw,
        )
    )

    # MetaTemplate post-pass: emit cross_arch / family build_common_dep
    # tasks (Phase 5.4). Runs BEFORE the per-cell walk so cell-level
    # common_dep emission can short-circuit on ``meta_skip_idents`` and
    # variants can wire to meta task_ids during their own minting.
    meta_templates_by_binary = streaming_result.get(
        "meta_templates", {},
    ) or {}
    meta_templates = (
        meta_templates_by_binary.get(binary, [])
        if isinstance(meta_templates_by_binary, Mapping)
        else []
    )
    (
        meta_descriptors,
        meta_extra_variant_deps,
        meta_skip_idents,
        meta_toolchain_extras,
    ) = ([], {}, set(), {})
    if meta_templates:
        is_source_terminal, drv_role = _load_source_terminal_predicate()
        arch_to_labels = _arch_to_labels_from_variant_arrays(variant_arrays)
        (
            meta_descriptors,
            meta_extra_variant_deps,
            meta_skip_idents,
            meta_toolchain_extras,
        ) = _plan_meta_for_binary(
            binary=binary,
            sys_name=sys_name,
            meta_templates=meta_templates,
            arch_to_labels=arch_to_labels,
            toolchain_idents_by_name=toolchain_idents_by_name,
            toolchain_task_ids=toolchain_task_ids,
            is_source_terminal=is_source_terminal,
            drv_role=drv_role,
        )

    common_dep_descriptors: list[Phase4Descriptor] = []
    variant_descriptors: list[Phase4Descriptor] = []

    for (tmpl_id, arch), arr in _iter_variant_arrays(variant_arrays):
        cell_common, cell_variants = _plan_cell(
            binary=binary,
            sys_name=sys_name,
            tmpl_id=tmpl_id,
            arch=arch,
            arr=arr,
            templates=templates,
            classification_map=classification_map,
            toolchain_node_ids=toolchain_node_ids,
            toolchain_idents_by_name=toolchain_idents_by_name,
            toolchain_task_ids=toolchain_task_ids,
            variant_lookup=variant_lookup,
            arch_indep_dep_task_ids=arch_indep_dep_task_ids,
            meta_extra_variant_deps=meta_extra_variant_deps,
            meta_skip_idents=meta_skip_idents,
            meta_toolchain_extras=meta_toolchain_extras,
        )
        common_dep_descriptors.extend(cell_common)
        variant_descriptors.extend(cell_variants)

    common_dep_descriptors.sort(
        key=lambda d: (d.payload["arch"], d.payload["node_name"], d.payload["ident"]),
    )
    arch_indep_descriptors.sort(
        key=lambda d: (d.payload["node_name"], d.payload["ident"]),
    )
    meta_descriptors.sort(
        key=lambda d: (d.payload["arch"], d.payload["node_name"], d.payload["ident"]),
    )
    variant_descriptors.sort(key=lambda d: (d.payload["arch"], d.payload["label"]))
    # Order: arch-indep deps first (gate every variant), then meta-
    # level cross_arch / family common_deps (span multiple archs), then
    # per-cell common_deps (intra-arch siblings), then variants. The
    # meta block lands between the arch-indep and per-cell blocks
    # because it's broader-scoped than per-cell but narrower than
    # arch-indep.
    return (
        arch_indep_descriptors
        + meta_descriptors
        + common_dep_descriptors
        + variant_descriptors
    )


def plan_phase4_from_graph(
    inputs: Iterable[BinaryPlanInput],
    *,
    sys_name: str = "x86_64-linux",
) -> list[Phase4Descriptor]:
    """Translate a sequence of per-binary streaming results into a
    single ordered phase-4 descriptor list.

    Each :class:`BinaryPlanInput` is processed independently via
    :func:`plan_phase4_for_binary`; the results are concatenated in
    binary-name order so the framework's spawn log is stable across
    runs. Cycle detection runs per-binary (the first cycle raises,
    later binaries are not visited).

    Cross-binary ``build_common_dep`` dedup: ``_common_dep_task_id``
    is keyed only on the shared-dep ident, so two binaries whose
    templates touch the same sub-drv produce the same task_id. We
    keep the FIRST descriptor seen for each task_id and drop later
    duplicates. ``build_variant`` descriptors from later binaries
    still reference the deduped task_id in their ``depends_on`` --
    that wiring is automatic since the variant builder reads the
    task_id off ``_common_dep_task_id`` directly. Stable ordering is
    enforced by sorting common-deps by ``task_id``.
    """
    common_dep_by_task_id: dict[str, Phase4Descriptor] = {}
    variant_descriptors: list[Phase4Descriptor] = []

    for inp in sorted(inputs, key=lambda i: i.binary):
        per_binary = plan_phase4_for_binary(
            inp.binary,
            inp.streaming_result,
            inp.variant_lookup,
            sys_name=sys_name,
            toolchain_task_ids=inp.toolchain_task_ids,
        )
        for d in per_binary:
            if d.kind == "build_common_dep":
                # First binary to emit this task_id wins; subsequent
                # binaries' duplicates are dropped because the task_id
                # already encodes the (content-addressed) ident.
                common_dep_by_task_id.setdefault(d.task_id, d)
            else:
                variant_descriptors.append(d)

    common_dep_descriptors = sorted(
        common_dep_by_task_id.values(), key=lambda d: d.task_id,
    )
    return common_dep_descriptors + variant_descriptors

"""MetaTemplate-driven cross-arch / per-family ``build_common_dep``
emission.

The streaming planner's finalize pass adds ``meta_templates``: per
binary, a list of :class:`template_graph.graph.MetaTemplate` objects
that project the per-arch templates' role-merged keymap into a single
cross-arch skeleton. Each position carries one of four classifications:

  * ``"cross_arch_common_dep"`` — same ident across every covered arch
    (class D). ONE meta-level task collapses the per-arch realisations.
  * ``"family_common_dep"`` — same ident within an arch family, differs
    across families (class B). ONE meta-level task per family.
  * ``"uni_arch_common_dep"`` — per-arch (class A or C). Per-cell
    emission already handles this; meta pass is a no-op.
  * ``"variant_specific"`` — folded into the variant build; no task.

:func:`_plan_meta_for_binary` returns ``(meta_descriptors,
extra_variant_deps, meta_skip_idents, toolchain_meta_extras)``:
new descriptors with task_ids
``build_common_dep__cross_arch__<ident>`` /
``build_common_dep__family__<family>__<ident>``; per-(arch,label)
extra deps + toolchain-extra dicts; idents whose per-cell common_dep
emission must be skipped to avoid duplicate dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .descriptors import Phase4Descriptor
from .shapes import (
    _coerce_ident,
    _ident_to_str,
    _iter_variant_arrays,
    _variant_array_fields,
)


_META_COMMON_DEP_PRIORITY_HINT = 10
"""Priority bias for cross-arch / per-family meta ``build_common_dep``
descriptors (plan §E7). Small positive: outranks the default-0
per-cell / per-variant tasks, leaves room above for future tiers."""


def _load_arch_families() -> dict[str, str]:
    from template_graph.streaming import _ARCH_FAMILIES  # noqa: PLC0415
    return dict(_ARCH_FAMILIES)


def _cross_arch_task_id(ident_str: str) -> str:
    return f"build_common_dep__cross_arch__{ident_str}"


def _family_task_id(family: str, ident_str: str) -> str:
    return f"build_common_dep__family__{family}__{ident_str}"


def _meta_descriptor(
    *,
    binary: str,
    sys_name: str,
    task_id: str,
    name_suffix: str,
    ident_str: str,
    role: str,
    scope: str,
) -> Phase4Descriptor:
    """Mint a meta-level ``build_common_dep`` descriptor. ``scope`` is
    ``"cross_arch"`` or ``"family__<family>"`` and lands in
    ``payload["arch"]`` so downstream tooling distinguishes meta entries
    from per-cell ones (which carry a concrete arch).
    """
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=task_id,
        name=f"build_common_dep__{binary}__{name_suffix}",
        payload={
            "sys": sys_name,
            "binary": binary,
            "arch": scope,
            "node_name": role,
            "ident": ident_str,
            "attr": ident_str,
        },
        depends_on=(),
        priority_hint=_META_COMMON_DEP_PRIORITY_HINT,
    )


def _arch_to_labels_from_variant_arrays(
    variant_arrays: Any,
) -> dict[str, list[str]]:
    """``{arch: [variant_label, ...]}`` from a streaming result; used to
    enumerate variants covered by each MetaTemplate without re-walking.
    """
    out: dict[str, list[str]] = {}
    for (_tid, arch), arr in _iter_variant_arrays(variant_arrays):
        _t, _a, variants, _h = _variant_array_fields(arr)
        out.setdefault(arch, []).extend(variants)
    return out


def _resolve_toolchain_for_role(
    role: str,
    toolchain_idents_by_name: Mapping[str, Sequence[tuple[str, str]]],
    toolchain_task_ids: Mapping[str, str],
) -> set[str]:
    """Toolchain task_ids matching ``role`` (post-hash drv name)."""
    out: set[str] = set()
    for ident in toolchain_idents_by_name.get(role, ()):
        task_id = toolchain_task_ids.get(_ident_to_str(ident))
        if task_id:
            out.add(task_id)
    return out


def _add_to_extras(
    extras: dict[tuple[str, str], set[str]],
    archs: Sequence[str],
    arch_to_labels: Mapping[str, Sequence[str]],
    task_ids: set[str],
) -> None:
    if not task_ids:
        return
    for arch in archs:
        for label in arch_to_labels.get(arch, ()):
            extras.setdefault((arch, label), set()).update(task_ids)


def _iter_meta_positions(meta: Any):
    """Yield ``(role, cls, drv)`` triples from a MetaTemplate."""
    role_at_node = getattr(meta, "role_at_node", ())
    classifications = getattr(meta, "cross_arch_classification", ())
    drv_per_node = getattr(meta, "drv_per_node", ())
    n = min(len(role_at_node), len(classifications), len(drv_per_node))
    for i in range(n):
        yield role_at_node[i], classifications[i], drv_per_node[i]


def _process_position(
    *,
    state: dict,
    role: str,
    ident_str: str,
    task_id: str,
    name_suffix: str,
    scope: str,
    target_archs: Sequence[str],
) -> None:
    """Route one position through the three plan §E2 sub-categories
    (toolchain / source_terminal / plain) and update ``state`` in place.

    ``state`` carries the shared per-binary accumulators and resolver
    callables; aggregating in one dict keeps the function signature
    short across both call sites (cross_arch and family).
    """
    role_resolved = role
    if role_resolved in state["toolchain_idents_by_name"]:
        resolved = _resolve_toolchain_for_role(
            role_resolved,
            state["toolchain_idents_by_name"],
            state["toolchain_task_ids"],
        )
        _add_to_extras(
            state["toolchain_meta_extras"], target_archs,
            state["arch_to_labels"], resolved,
        )
        state["meta_skip_idents"].add(ident_str)
        return
    if state["is_source_terminal"](state["drv_role"](role_resolved)):
        state["meta_skip_idents"].add(ident_str)
        return
    meta_descriptors: list[Phase4Descriptor] = state["meta_descriptors"]
    if not any(d.task_id == task_id for d in meta_descriptors):
        meta_descriptors.append(_meta_descriptor(
            binary=state["binary"],
            sys_name=state["sys_name"],
            task_id=task_id,
            name_suffix=name_suffix,
            ident_str=ident_str,
            role=role,
            scope=scope,
        ))
    state["meta_skip_idents"].add(ident_str)
    _add_to_extras(
        state["extra_variant_deps"], target_archs,
        state["arch_to_labels"], {task_id},
    )


def _process_cross_arch(
    state: dict, role: str, drv: Any, covered_archs: Sequence[str],
) -> None:
    ident = _coerce_ident(drv)
    if ident is None:
        return
    ident_str = _ident_to_str(ident)
    _process_position(
        state=state,
        role=role,
        ident_str=ident_str,
        task_id=_cross_arch_task_id(ident_str),
        name_suffix=f"cross_arch__{role}",
        scope="cross_arch",
        target_archs=covered_archs,
    )


def _process_family(
    state: dict,
    role: str,
    drv_map: Mapping,
    covered_archs: Sequence[str],
    arch_families: Mapping[str, str],
) -> None:
    archs_by_family: dict[str, list[str]] = {}
    for arch in covered_archs:
        archs_by_family.setdefault(
            arch_families.get(arch, "other"), [],
        ).append(arch)
    for family, ident_raw in drv_map.items():
        ident = _coerce_ident(ident_raw)
        if ident is None:
            continue
        ident_str = _ident_to_str(ident)
        fam_archs = archs_by_family.get(str(family), [])
        if not fam_archs:
            continue
        _process_position(
            state=state,
            role=role,
            ident_str=ident_str,
            task_id=_family_task_id(str(family), ident_str),
            name_suffix=f"family__{family}__{role}",
            scope=f"family__{family}",
            target_archs=fam_archs,
        )


def _plan_meta_for_binary(
    *,
    binary: str,
    sys_name: str,
    meta_templates: Sequence[Any],
    arch_to_labels: Mapping[str, Sequence[str]],
    toolchain_idents_by_name: Mapping[str, Sequence[tuple[str, str]]],
    toolchain_task_ids: Mapping[str, str],
    is_source_terminal,
    drv_role,
) -> tuple[
    list[Phase4Descriptor],
    dict[tuple[str, str], set[str]],
    set[str],
    dict[tuple[str, str], set[str]],
]:
    """MetaTemplate post-pass for one binary.

    Returns ``(meta_descriptors, extra_variant_deps, meta_skip_idents,
    toolchain_meta_extras)``. See module docstring for shapes.
    """
    state: dict = {
        "binary": binary,
        "sys_name": sys_name,
        "arch_to_labels": arch_to_labels,
        "toolchain_idents_by_name": toolchain_idents_by_name,
        "toolchain_task_ids": toolchain_task_ids,
        "is_source_terminal": is_source_terminal,
        "drv_role": drv_role,
        "meta_descriptors": [],
        "extra_variant_deps": {},
        "meta_skip_idents": set(),
        "toolchain_meta_extras": {},
    }
    arch_families: Optional[dict[str, str]] = None
    for meta in meta_templates:
        covered_archs = list(getattr(meta, "template_id_per_arch", {}).keys())
        for role, cls, drv in _iter_meta_positions(meta):
            if cls == "cross_arch_common_dep":
                _process_cross_arch(state, role, drv, covered_archs)
            elif cls == "family_common_dep" and isinstance(drv, Mapping):
                if arch_families is None:
                    arch_families = _load_arch_families()
                _process_family(
                    state, role, drv, covered_archs, arch_families,
                )
    return (
        state["meta_descriptors"],
        state["extra_variant_deps"],
        state["meta_skip_idents"],
        state["toolchain_meta_extras"],
    )


__all__ = [
    "_plan_meta_for_binary",
    "_arch_to_labels_from_variant_arrays",
    "_cross_arch_task_id",
    "_family_task_id",
]

"""Phase-4 descriptor dataclasses, plan-input record, and the small
helpers that mint one descriptor of each kind.

This module deliberately does not import the streaming-planner shape
helpers or run cycle detection; it is a pure data + constructor layer.
:mod:`.plan_total` is where the cells are walked and descriptors are
assembled into a plan.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DependencyGraphCycleError(Exception):
    """Raised when walking the streaming planner's template graph
    encounters a cycle.

    Nix drv graphs are DAGs by construction, so this is a defensive
    guard rather than an expected control flow path. Surfacing the
    cycle as a typed exception lets the watcher layer treat it as a
    hard, non-retryable error (the matrix output is corrupt and no
    amount of redispatch will fix it).
    """


# ---------------------------------------------------------------------------
# Public descriptor types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Phase4Descriptor:
    """One phase-4 task description in a manifest-gen-agnostic shape.

    ``kind`` is either ``"build_common_dep"`` or ``"build_variant"``;
    integration code maps these to the current ``manifest_gen``
    constructor names. ``payload`` carries the worker-visible
    arguments; ``depends_on`` is the tuple of task_ids whose completion
    gates this task.
    """

    kind: str
    task_id: str
    name: str
    payload: dict
    depends_on: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class BinaryPlanInput:
    """Per-binary inputs for :func:`plan_phase4_for_binary`.

    ``streaming_result`` is the dict returned by
    ``template_graph.streaming.plan_from_tree_streaming`` (or a
    JSON-roundtripped equivalent) for THIS binary's sum-drv.

    ``variant_lookup`` maps ``(arch, variant_label)`` -- where
    ``variant_label`` is what the streaming planner stores in
    ``VariantArray.variants`` (e.g. ``"gcc15-O2"``) -- to the full
    variant descriptor (a ``VariantSpec``-shaped Mapping). The
    descriptor supplies the variant's drv path, output directory
    metadata, compiler ids and so on -- i.e. everything the build
    worker needs that isn't visible in the streaming graph itself.
    Missing labels are skipped with no error (caller may legitimately
    drop variants between graph generation and planning).

    ``toolchain_task_ids`` maps a toolchain drv identifier
    (``"<hash>-<name>"`` -- the format returned by
    :func:`convert_toolchain_drvs`) to its phase-1 ``build_compilers``
    task_id. Variants whose template touches that toolchain get the
    id wired into their ``depends_on``. The streaming planner's
    ``toolchain_node_ids_per_template`` map identifies which template
    nodes are toolchains (the cowalk short-circuits their subtrees so
    ``arr.hashes`` rows stay empty); we resolve those node_ids to
    idents by matching the TemplateNode's role-name against
    ``out.toolchain_drvs`` entries. Idents not present in
    ``toolchain_task_ids`` (operator-provided toolchains with no
    framework task) are silently omitted. Empty mapping is fine
    (variants then have only common-dep deps).
    """

    binary: str
    streaming_result: Mapping[str, Any]
    variant_lookup: Mapping[tuple[str, str], Mapping[str, Any]]
    toolchain_task_ids: Mapping[str, str] = dataclasses.field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# Task-id helpers
# ---------------------------------------------------------------------------


def _common_dep_task_id(ident_str: str) -> str:
    """Stable cross-binary task id for a shared dep.

    The task id is keyed solely on the ident (``"<hash>-<name>"``) so
    that two binaries whose templates touch the same shared sub-drv
    collapse onto ONE ``build_common_dep`` descriptor. Cross-binary
    descriptor dedup at emission time (see
    :func:`plan_phase4_from_graph`) folds the duplicates and unions
    their variants' ``depends_on`` entries.

    The ident already encodes (hash, name); the hash is a content-
    addressed nix-store prefix, so identical idents are guaranteed to
    refer to the same sub-derivation regardless of which binary
    template observed it. Architecture is implicit in the hash (a
    different arch produces a different nix-store hash for the same
    role) -- explicitly prefixing arch would only hurt the cross-binary
    collapse the streaming planner just enabled.
    """
    return f"build_common_dep__{ident_str}"


def _arch_indep_task_id(binary: str, ident_str: str) -> str:
    """Per-binary task id for an ``arch_indep_deps`` ident.

    Arch-indep deps live one-level under the matrix wrapper and are
    shared by every variant of a binary (across opt-levels) but differ
    across binaries -- so the task_id retains a ``binary`` prefix. The
    arch axis is intentionally elided (the dep is arch-independent by
    construction).
    """
    return f"build_common_dep__arch_indep__{binary}__{ident_str}"


def _variant_task_id(binary: str, sys_name: str, label: str) -> str:
    return f"build_variant__{sys_name}__{binary}__{label}"


# ---------------------------------------------------------------------------
# Descriptor constructors
# ---------------------------------------------------------------------------


def _common_dep_descriptor(
    *,
    binary: str,
    arch: str,
    sys_name: str,
    node_id: int,
    node_name: str,
    ident_str: str,
) -> Phase4Descriptor:
    """Build a ``build_common_dep`` descriptor for one shared-dep node."""
    task_id = _common_dep_task_id(ident_str)
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=task_id,
        name=f"build_common_dep__{binary}__{arch}__{node_name}",
        payload={
            "sys": sys_name,
            "binary": binary,
            "arch": arch,
            "node_name": node_name,
            "node_id": node_id,
            "ident": ident_str,
            # ``attr`` is the worker-facing input; for common-dep
            # builds we point at the ident-derived drv path (the build
            # worker reconstructs the full ``/nix/store/<ident>.drv``
            # prefix). Keeping the bare ident is forward-compatible
            # with both classic ``nix build <drv>`` and the
            # archive-import path used after matrix_eval.
            "attr": ident_str,
        },
        depends_on=(),
    )


def _arch_indep_descriptor(
    *,
    binary: str,
    sys_name: str,
    ident_str: str,
    node_name: str,
) -> Phase4Descriptor:
    """Build a ``build_common_dep`` descriptor for one arch-indep dep.

    Arch-indep deps come from ``OutputState.arch_indep_deps`` (depth-2
    matrix children that aren't variant entry-points). They are
    shared across every variant of THIS binary regardless of arch /
    compiler / opt-level. The descriptor's ``arch`` payload field is
    fixed to ``"arch_indep"`` so the worker can branch its build
    invocation; the task_id encodes the same axis for spawn-log
    grepping.
    """
    task_id = _arch_indep_task_id(binary, ident_str)
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=task_id,
        name=f"build_common_dep__arch_indep__{binary}__{node_name}",
        payload={
            "sys": sys_name,
            "binary": binary,
            "arch": "arch_indep",
            "node_name": node_name,
            "ident": ident_str,
            "attr": ident_str,
        },
        depends_on=(),
    )


def _variant_descriptor(
    *,
    binary: str,
    arch: str,
    sys_name: str,
    label: str,
    variant_spec: Mapping[str, Any],
    depends_on: Sequence[str],
) -> Phase4Descriptor:
    """Build a ``build_variant`` descriptor for one matrix variant.

    Payload mirrors the legacy ``make_variant_header`` shape so the
    integration site can hand-roll a ``ManifestHeader`` with no
    field-by-field reconstruction. We do NOT call ``manifest_gen`` at
    all -- see module docstring for the decoupling rationale.
    """
    payload = {
        "sys": sys_name,
        "pkg": variant_spec.get("pkg", binary),
        "arch": arch,
        "label": label,
        "drv": variant_spec.get("drv", ""),
        "variant_dir": variant_spec.get("variant_dir", ""),
        "metadata_name": variant_spec.get("metadata_name", ""),
        "compiler_id": variant_spec.get("compiler_id", ""),
        "compiler_family": variant_spec.get("compiler_family", ""),
        "compiler_version": variant_spec.get("compiler_version", ""),
        "optimization": variant_spec.get("optimization", ""),
        "flag_set": variant_spec.get("flag_set", ""),
        "hardening": variant_spec.get("hardening", ""),
        "sanitizer": variant_spec.get("sanitizer", ""),
        "march": variant_spec.get("march", ""),
        "tier": variant_spec.get("tier", 0),
    }
    task_id = _variant_task_id(binary, sys_name, label)
    # Deterministic ordering of deps so observers comparing manifests
    # across runs see stable diffs.
    return Phase4Descriptor(
        kind="build_variant",
        task_id=task_id,
        name=f"build_variant__{binary}__{label}",
        payload=payload,
        depends_on=tuple(sorted(set(depends_on))),
    )

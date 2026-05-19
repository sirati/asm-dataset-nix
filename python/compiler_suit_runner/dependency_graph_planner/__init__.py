"""Adapter from ``template_graph.streaming.plan_from_tree_streaming`` to
phase-4 task descriptors.

Replaces the legacy ``phase1_planner.py`` refcount-and-walk model with
a direct translation of the streaming planner's classified template
graph.

The streaming planner emits, per ``(template_id, arch)`` cell:

  * a ``VariantArray`` whose ``variants`` lists the (compiler-opt)
    labels covered (e.g. ``["gcc15-O0", "gcc15-O2", ...]``);
  * a per-node hash row ``hashes[node_id][variant_index]`` carrying the
    ``(hash, name)`` ident of the drv that occupies that role in that
    variant;
  * a classification per node -- ``"common_dep"`` or
    ``"variant_specific"`` -- produced by the calibration-pair
    invariants pass.

Every ``common_dep`` node deduplicates one shared sub-derivation across
all the array's variants; we emit one ``build_common_dep`` descriptor
per such node. Every variant becomes one ``build_variant`` descriptor
whose ``depends_on`` references the common_dep task_ids it actually
covers plus the toolchain task_ids advertised by the caller.

Package layout
--------------

This package supersedes the 996-LOC single-file
``dependency_graph_planner.py`` (Phase 5 polish split per plan section
F3). Each submodule is well under the 300-LOC ceiling and is
independently testable; the public surface is unchanged -- every symbol
the legacy module exposed is re-exported here so existing
``from compiler_suit_runner.dependency_graph_planner import ...``
imports keep working unmodified.

  * :mod:`.descriptors`    -- :class:`Phase4Descriptor`,
                              :class:`BinaryPlanInput`,
                              :class:`DependencyGraphCycleError`,
                              task-id helpers and descriptor constructors.
  * :mod:`.shapes`         -- dataclass-or-dict cell readers and the
                              ``(hash, name) <-> "<hash>-<name>"`` coercion
                              shared across the package.
  * :mod:`.cycle`          -- defensive DFS cycle detection over the
                              streaming planner's template graph.
  * :mod:`.plan_cell`      -- per-``(template_id, arch)`` cell helpers
                              (arch-indep resolution, toolchain wiring,
                              common-dep minting) and the lazy
                              ``template_graph.parser`` predicate loader.
  * :mod:`.plan_meta`      -- MetaTemplate post-pass (Phase 5.4):
                              cross_arch / family ``build_common_dep``
                              emission and per-variant wiring extras.
  * :mod:`.plan_total`     -- :func:`plan_phase4_for_binary` and
                              :func:`plan_phase4_from_graph` -- the
                              per-binary and multi-binary drivers.
  * :mod:`.manifest_glue`  -- descriptor -> ``ManifestHeader`` translation,
                              ``_dependency_graph.pkl`` reader, drv-path
                              label key.

Decoupling notes
----------------

This module emits **descriptors**, not pre-built
``manifest_gen.ManifestHeader`` instances, for two reasons:

  1. ``manifest_gen`` is mid-rename (``phase2_common_dep`` ->
     ``build_common_dep`` and ``phase3_variant`` -> ``build_variant``).
     Descriptors let the integration site convert in either taxonomy
     without churning this module twice.
  2. The descriptor shape matches the ``primary_handle.spawn_tasks``
     contract one-to-one, so the wiring layer is a trivial loop with no
     hidden mapping logic.

The package does NOT import ``template_graph`` at module load: the
caller passes the streaming result as a plain dict (the same shape
``plan_from_tree_streaming`` returns, but also tolerant of a
JSON-roundtripped form produced by ``dependency_graph_worker``'s
``_dependency_graph.json``). This keeps unit tests dependency-free
and lets the worker either pickle dataclasses or serialise to JSON
without dragging the adapter into either choice.
"""

from __future__ import annotations

from .descriptors import (
    BinaryPlanInput,
    DependencyGraphCycleError,
    Phase4Descriptor,
    _arch_indep_descriptor,
    _arch_indep_task_id,
    _common_dep_descriptor,
    _common_dep_task_id,
    _variant_descriptor,
    _variant_task_id,
)
from .manifest_glue import (
    DependencyGraphPickleError,
    headers_from_descriptors,
    load_phase4_descriptors,
    variant_label_key,
)
from .plan_total import (
    _load_source_terminal_predicate,
    plan_phase4_for_binary,
    plan_phase4_from_graph,
)
from .shapes import (
    _arch_indep_idents_for_binary,
    _attr_or_key,
    _coerce_ident,
    _coerce_toolchain_node_ids,
    _ident_to_str,
    _iter_classifications,
    _iter_variant_arrays,
    _node_field,
    _template_nodes,
    _toolchain_ident_strs,
    _variant_array_fields,
    convert_toolchain_drvs,
)


__all__ = [
    "DependencyGraphCycleError",
    "DependencyGraphPickleError",
    "Phase4Descriptor",
    "BinaryPlanInput",
    "convert_toolchain_drvs",
    "plan_phase4_for_binary",
    "plan_phase4_from_graph",
    "headers_from_descriptors",
    "load_phase4_descriptors",
    "variant_label_key",
]

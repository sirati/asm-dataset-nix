"""Single-pass streaming planner over `nix-store --query --tree` output.

Replaces the two-phase (walk → plan_phase1_graph) approach: we never
call ``nix derivation show`` because every drv's immediate inputs are
already present in the tree (as direct children at depth+1). The
algorithm walks the output line-by-line, opens a per-variant RawTree
buffer when it enters a matrix variant subtree, and as soon as ANY
arch accumulates two raw trees we build the template from that
calibration pair, drain that arch's buffer into the new variant array,
and continue streaming. Subsequent variants of the same arch
stream directly into the array (no further buffering).

This subpackage replaces the legacy single-file ``streaming.py``:

    state.py     — dataclasses (OutputState, MatrixState,
                   VariantBuilderState, RawTreeNode) + the
                   ``StreamPlanner`` class (constructor, ``_record``,
                   ``_cowalk_into_arr``, ``_extend_template_with_subtree``,
                   ``_is_toolchain_child``, ``_discard_subtree``).
    dispatch.py  — per-line walk (feed_line, _on_depth1,
                   _on_matrix_inner, _finalise_current_variant).
    finalize.py  — finalize + _build_and_drain_arch +
                   _close_current_matrix.
    entry.py     — plan_from_tree_streaming convenience entry.
    _helpers.py  — drv_name_full, _classify_cross_arch_sharing,
                   _ARCH_FAMILIES. (``_ARCH_TO_TRIPLE`` /
                   ``_extract_triple`` / ``_extract_version`` /
                   ``_classify_revisit_diff`` moved to
                   ``template_graph.cowalk._helpers``; re-exported
                   here for back-compat.)

Public surface (re-exported here for back-compat):
    StreamPlanner — instantiate, feed lines via .feed_line(), call .finalize().
    plan_from_tree_streaming(tree_text, *, archs=...) -> dict
        Same return shape as ``core.plan_phase1_graph``:
            { templates, variant_arrays, placement,
              common_deps_per_arch_template,
              toolchain_drvs, arch_indep_deps }
"""

from __future__ import annotations

# Free helpers used by both this package and external callers.
from template_graph.streaming._helpers import (
    _ARCH_FAMILIES,
    _classify_cross_arch_sharing,
    drv_name_full,
)
# Back-compat: the four symbols below moved to
# ``template_graph.cowalk._helpers``; re-export so existing
# ``from template_graph.streaming import _ARCH_TO_TRIPLE`` works.
from template_graph.cowalk._helpers import (  # noqa: F401
    _ARCH_TO_TRIPLE,
    _classify_revisit_diff,
    _extract_triple,
    _extract_version,
)

# State dataclasses + StreamPlanner.
from template_graph.streaming.state import (
    MatrixState,
    OutputState,
    RawTreeNode,
    StreamPlanner,
    VariantBuilderState,
)

# Public convenience entry.
from template_graph.streaming.entry import plan_from_tree_streaming

# ── back-compat re-exports of dependencies imported through here ─────
#
# External callers (``from template_graph.streaming import drv_role,
# _classify_pair, template_to_dot, ...``) expect these surfaces to
# remain on ``template_graph.streaming``. Re-export from the original
# homes so the API does not break.

from template_graph.cowalk import _classify_pair  # noqa: F401
from template_graph.dot import (  # noqa: F401
    merge_binary_to_dot,
    save_binary_merged_dot,
    save_template_dot,
    template_to_dot,
)
from template_graph.parser.role import drv_role  # noqa: F401

__all__ = [
    # Planner class + entry.
    "StreamPlanner",
    "plan_from_tree_streaming",
    # State groupings.
    "OutputState",
    "MatrixState",
    "VariantBuilderState",
    "RawTreeNode",
    # Free helpers.
    "drv_name_full",
    "_ARCH_TO_TRIPLE",
    "_ARCH_FAMILIES",
    "_extract_triple",
    "_extract_version",
    "_classify_revisit_diff",
    "_classify_cross_arch_sharing",
    # Back-compat surface.
    "drv_role",
    "_classify_pair",
    "template_to_dot",
    "save_template_dot",
    "merge_binary_to_dot",
    "save_binary_merged_dot",
]

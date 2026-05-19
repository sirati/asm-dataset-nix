"""Standalone template-graph algorithm (Part B).

Streaming contract: never builds a full closure dict — reads one
``.drv`` record at a time via ``nix derivation show``.

This top-level module re-exports the canonical public surface from
the algorithm subpackages so external callers can do
``from template_graph import X`` for any commonly-used name. The
authoritative homes remain ``template_graph.graph``,
``template_graph.streaming``, ``template_graph.cowalk``,
``template_graph.parser.role``, ``template_graph.dot``, and
``template_graph.make_sum_drv``.
"""

# ── data layer (graph) ──────────────────────────────────────────────
from template_graph.graph import (
    MetaTemplate,
    Template,
    TemplateAlignment,
    TemplateNode,
    TemplateGraphAssertError,
    VariantArray,
    _shape_equal,
    find_or_register_template,
)

# ── drv I/O ─────────────────────────────────────────────────────────
from template_graph.drv_io import DrvIoError, read_drv_record

# ── streaming planner ───────────────────────────────────────────────
from template_graph.streaming import (
    MatrixState,
    OutputState,
    RawTreeNode,
    StreamPlanner,
    VariantBuilderState,
    plan_from_tree_streaming,
)

# ── cowalk algorithm helpers ────────────────────────────────────────
from template_graph.cowalk import (
    _classify_pair,
    build_meta_templates,
    cowalk_into_arr,
    template_shape_histogram,
)

# ── parser (role extraction) ────────────────────────────────────────
from template_graph.parser.role import (
    _is_source_terminal_role,
    drv_role,
)

# ── DOT renderers ───────────────────────────────────────────────────
from template_graph.dot import merge_binary_to_dot, template_to_dot

# ── sum-drv builder ─────────────────────────────────────────────────
from template_graph.make_sum_drv import make_sum_drv_from_paths


__all__ = [
    # graph
    "Template",
    "TemplateNode",
    "TemplateAlignment",
    "TemplateGraphAssertError",
    "VariantArray",
    "MetaTemplate",
    "find_or_register_template",
    "_shape_equal",
    # drv I/O
    "read_drv_record",
    "DrvIoError",
    # streaming
    "StreamPlanner",
    "plan_from_tree_streaming",
    "OutputState",
    "MatrixState",
    "VariantBuilderState",
    "RawTreeNode",
    # cowalk
    "build_meta_templates",
    "template_shape_histogram",
    "_classify_pair",
    "cowalk_into_arr",
    # parser
    "drv_role",
    "_is_source_terminal_role",
    # dot
    "template_to_dot",
    "merge_binary_to_dot",
    # sum-drv
    "make_sum_drv_from_paths",
]

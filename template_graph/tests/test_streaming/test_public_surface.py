"""Smoke test: top-level ``template_graph`` re-exports the full
canonical public surface.

External callers should be able to ``from template_graph import X``
for every commonly-used name without reaching into subpackages. This
test asserts each symbol is present, has the correct kind (class /
callable), and is the exact canonical object from its authoritative
home module (not a re-implementation or shadow).
"""

from __future__ import annotations

import inspect

import template_graph
from template_graph import cowalk as tg_cowalk
from template_graph import dot as tg_dot
from template_graph import graph as tg_graph
from template_graph import make_sum_drv as tg_make_sum_drv
from template_graph import streaming as tg_streaming
from template_graph.drv_io import DrvIoError, read_drv_record
from template_graph.parser.role import _is_source_terminal_role, drv_role


CLASS_SYMBOLS = {
    # graph
    "Template": tg_graph.Template,
    "TemplateNode": tg_graph.TemplateNode,
    "TemplateAlignment": tg_graph.TemplateAlignment,
    "TemplateGraphAssertError": tg_graph.TemplateGraphAssertError,
    "VariantArray": tg_graph.VariantArray,
    "MetaTemplate": tg_graph.MetaTemplate,
    # streaming
    "StreamPlanner": tg_streaming.StreamPlanner,
    "OutputState": tg_streaming.OutputState,
    "MatrixState": tg_streaming.MatrixState,
    "VariantBuilderState": tg_streaming.VariantBuilderState,
    "RawTreeNode": tg_streaming.RawTreeNode,
    # drv I/O
    "DrvIoError": DrvIoError,
}

CALLABLE_SYMBOLS = {
    # graph
    "find_or_register_template": tg_graph.find_or_register_template,
    "_shape_equal": tg_graph._shape_equal,
    # drv I/O
    "read_drv_record": read_drv_record,
    # streaming
    "plan_from_tree_streaming": tg_streaming.plan_from_tree_streaming,
    # cowalk
    "build_meta_templates": tg_cowalk.build_meta_templates,
    "template_shape_histogram": tg_cowalk.template_shape_histogram,
    "_classify_pair": tg_cowalk._classify_pair,
    "cowalk_into_arr": tg_cowalk.cowalk_into_arr,
    # parser
    "drv_role": drv_role,
    "_is_source_terminal_role": _is_source_terminal_role,
    # dot
    "template_to_dot": tg_dot.template_to_dot,
    "merge_binary_to_dot": tg_dot.merge_binary_to_dot,
    # sum-drv
    "make_sum_drv_from_paths": tg_make_sum_drv.make_sum_drv_from_paths,
}


def test_public_classes_are_classes_and_canonical():
    """Each class symbol on ``template_graph`` is a type and is the
    same object as its canonical home in the authoritative subpackage."""
    for name, canonical in CLASS_SYMBOLS.items():
        exported = getattr(template_graph, name)
        assert inspect.isclass(exported), (
            f"template_graph.{name} should be a class, got {type(exported)!r}"
        )
        assert exported is canonical, (
            f"template_graph.{name} is not the canonical object "
            f"(got {exported!r}, want {canonical!r})"
        )


def test_public_callables_are_callable_and_canonical():
    """Each callable symbol on ``template_graph`` is callable and is
    the same object as its canonical home in the authoritative
    subpackage."""
    for name, canonical in CALLABLE_SYMBOLS.items():
        exported = getattr(template_graph, name)
        assert callable(exported), (
            f"template_graph.{name} should be callable, got {type(exported)!r}"
        )
        assert exported is canonical, (
            f"template_graph.{name} is not the canonical object "
            f"(got {exported!r}, want {canonical!r})"
        )


def test_all_list_matches_exports():
    """Every name in ``template_graph.__all__`` resolves to an
    attribute on the module, and every CLASS_SYMBOLS / CALLABLE_SYMBOLS
    name is listed in ``__all__``."""
    declared = set(template_graph.__all__)
    expected = set(CLASS_SYMBOLS) | set(CALLABLE_SYMBOLS)
    missing_in_all = expected - declared
    assert not missing_in_all, (
        f"symbols missing from template_graph.__all__: {sorted(missing_in_all)}"
    )
    for name in declared:
        assert hasattr(template_graph, name), (
            f"template_graph.__all__ lists {name!r} but no such attribute"
        )


def test_backward_compat_simple_imports():
    """Backward-compat: the historically-exported names still work."""
    from template_graph import (  # noqa: F401
        DrvIoError,
        Template,
        TemplateGraphAssertError,
        TemplateNode,
        VariantArray,
        find_or_register_template,
        read_drv_record,
    )

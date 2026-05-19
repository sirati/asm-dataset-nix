"""Cowalk algorithms over raw trees + Templates.

Pure functions over ``template_graph.graph`` data types plus a typed
``StreamPlanner`` handle for planner-owned state (``out``, ``mx``,
``vb``) and helper methods (``_discard_subtree``, ``_record``,
``_walk_one_sided_subtree``). No imports from
``template_graph.streaming`` at runtime; callers in streaming.py
delegate into here.
"""

from __future__ import annotations

from template_graph.cowalk.build_template import (
    build_template,
    build_template_singleton,
    make_template_node,
    walk_one_sided_subtree,
)

__all__ = [
    "build_template",
    "build_template_singleton",
    "make_template_node",
    "walk_one_sided_subtree",
]

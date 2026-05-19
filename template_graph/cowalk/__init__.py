"""Cowalk algorithm helpers lifted out of ``template_graph.streaming``.

Pure functions over ``template_graph.graph`` data types plus a typed
``StreamPlanner`` handle for planner-owned state (``out``, ``mx``,
``vb``) and helper methods. Streaming.py delegates into here so each
primitive (template construction, cowalk-into-array, pair classification)
can be read, tested, and extended in isolation.
"""

from __future__ import annotations

from template_graph.cowalk._helpers import (
    _ARCH_TO_TRIPLE,
    _classify_revisit_diff,
    _extract_triple,
    _extract_version,
)
from template_graph.cowalk.build_template import (
    build_template,
    build_template_singleton,
    make_template_node,
    walk_one_sided_subtree,
)
from template_graph.cowalk.classify_pair import _classify_pair
from template_graph.cowalk.cowalk_variant import (
    CowalkCtx,
    cowalk_into_arr,
)

__all__ = [
    "build_template",
    "build_template_singleton",
    "make_template_node",
    "walk_one_sided_subtree",
    "_classify_pair",
    "CowalkCtx",
    "cowalk_into_arr",
    "_ARCH_TO_TRIPLE",
    "_classify_revisit_diff",
    "_extract_triple",
    "_extract_version",
]

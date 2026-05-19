"""Cowalk algorithm helpers lifted out of ``template_graph.streaming``.

The streaming planner orchestrates per-matrix calibration and per-arch
draining; the actual tree-walking primitives (template construction,
cowalk-into-array, pair classification) live here so each can be
read, tested, and extended in isolation.
"""

from template_graph.cowalk.classify_pair import _classify_pair

__all__ = [
    "_classify_pair",
]

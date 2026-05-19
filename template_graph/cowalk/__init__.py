"""Cowalk subpackage: per-variant raw-tree cowalking against an existing
template, plus calibration-pair classification and template construction.

Modules in this package are lifted from ``template_graph.streaming``;
each is a free-function rewrite of a former ``StreamPlanner`` method.
The planner keeps thin method shims that delegate to these free
functions so the public surface (``StreamPlanner._cowalk_into_arr`` etc.)
is unchanged.
"""

from template_graph.cowalk.cowalk_variant import (
    CowalkCtx,
    cowalk_into_arr,
)

__all__ = [
    "CowalkCtx",
    "cowalk_into_arr",
]

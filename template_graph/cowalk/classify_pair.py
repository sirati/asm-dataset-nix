"""Calibration-pair node classification.

Extracted from ``template_graph.streaming`` so the cowalk algorithm
helpers form a cohesive subpackage. Behaviour-identical to the
``StreamPlanner._classify_pair`` method this replaced — it does not
touch any planner state, only the supplied ``VariantArray`` and
``Template``.
"""

from __future__ import annotations

from template_graph.graph import Template, VariantArray


def _classify_pair(
    arr: VariantArray, template: Template
) -> dict[int, str]:
    """At this point arr.hashes has exactly two columns (variants
    0 and 1). For each non-toolchain node: equal → common_dep,
    differing → variant_specific.

    Subsequent variants of this arch will be checked incrementally
    by ``_cowalk_into_arr`` via ``assert_classification_after_cowalk``.
    """
    out: dict[int, str] = {}
    for nid, node in enumerate(template.nodes):
        if node.is_toolchain:
            continue
        h0 = arr.hashes[nid][0]
        h1 = arr.hashes[nid][1] if len(arr.hashes[nid]) > 1 else None
        if h0 is None:
            # Variant 0 didn't reach this node during cowalk. If
            # not already optional, promote it now — same handling
            # the cowalk-time path uses for "required but absent
            # in some variant". Downstream merged render uses
            # whichever non-None hash exists.
            if not node.optional:
                node.optional = True
            out[nid] = "common_dep"
            continue
        if h1 is None:
            # Single-variant calibration (rare; happens via
            # _close_current_matrix when an arch had only one
            # variant). Mark as common_dep.
            out[nid] = "common_dep"
            continue
        out[nid] = "common_dep" if h0 == h1 else "variant_specific"
    return out

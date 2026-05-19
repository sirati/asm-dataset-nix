"""Back-compat shim — data types live in ``template_graph.graph``.

The legacy ``nix derivation show``-based planner
(``build_template_from_closure`` / ``cowalk_and_index`` /
``assert_arch_invariants`` / ``plan_phase1_graph``) has been retired
in favour of the single-pass streaming planner in
``template_graph.streaming``. This module now exists only to keep
existing ``from template_graph.core import ...`` imports working
for the data-type primitives.
"""

from __future__ import annotations

from template_graph.graph.template import (
    Template,
    TemplateAlignment,
    TemplateGraphAssertError,
    TemplateNode,
    _shape_equal,
    find_or_register_template,
)
from template_graph.graph.variant_array import VariantArray

__all__ = [
    "Template",
    "TemplateAlignment",
    "TemplateNode",
    "VariantArray",
    "TemplateGraphAssertError",
    "_shape_equal",
    "find_or_register_template",
]

"""Template-graph data layer.

Hosts the structural primitives (``Template``, ``TemplateNode``,
``VariantArray``) and shape-level helpers (``_shape_equal``,
``TemplateAlignment``, ``find_or_register_template``) plus the
hard-assert error class. The cowalk algorithm and orchestrator live
in ``template_graph.streaming``.
"""

from template_graph.graph.meta_template import MetaTemplate
from template_graph.graph.template import (
    Template,
    TemplateAlignment,
    TemplateNode,
    TemplateGraphAssertError,
    _shape_equal,
    find_or_register_template,
)
from template_graph.graph.variant_array import VariantArray

__all__ = [
    "MetaTemplate",
    "Template",
    "TemplateAlignment",
    "TemplateNode",
    "TemplateGraphAssertError",
    "VariantArray",
    "_shape_equal",
    "find_or_register_template",
]

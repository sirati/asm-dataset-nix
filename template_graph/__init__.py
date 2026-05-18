"""Standalone template-graph algorithm (Part B, Phase 1).

Streaming contract: never builds a full closure dict — reads one
``.drv`` record at a time via ``nix derivation show``.
"""

from template_graph.graph import (
    Template,
    TemplateNode,
    TemplateGraphAssertError,
    VariantArray,
    find_or_register_template,
)
from .drv_io import read_drv_record, DrvIoError

__all__ = [
    "Template",
    "TemplateNode",
    "VariantArray",
    "TemplateGraphAssertError",
    "find_or_register_template",
    "read_drv_record",
    "DrvIoError",
]

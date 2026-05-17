"""Standalone template-graph algorithm (Part B, Phase 1).

Streaming contract: never builds a full closure dict — reads one
``.drv`` record at a time via ``nix derivation show``.
"""

from .core import (
    Template,
    TemplateNode,
    VariantArray,
    TemplateGraphAssertError,
    derivation_package_name,
    build_template_from_closure,
    find_or_register_template,
    cowalk_and_index,
    assert_arch_invariants,
    plan_phase1_graph,
    Logger,
    GetRecord,
    NameExtractor,
)
from .drv_io import read_drv_record, DrvIoError

__all__ = [
    "Template",
    "TemplateNode",
    "VariantArray",
    "TemplateGraphAssertError",
    "derivation_package_name",
    "build_template_from_closure",
    "find_or_register_template",
    "cowalk_and_index",
    "assert_arch_invariants",
    "plan_phase1_graph",
    "Logger",
    "GetRecord",
    "NameExtractor",
    "read_drv_record",
    "DrvIoError",
]

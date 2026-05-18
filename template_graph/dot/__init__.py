"""Graphviz DOT renderers for templates and per-binary merges.

Public surface:
    template_to_dot, save_template_dot — single Template renderer.
    merge_binary_to_dot, save_binary_merged_dot — per-binary merged
        view across all (template, arch) combinations belonging to
        one binary, with cross-arch sharing classification.
"""

from .template import template_to_dot, save_template_dot
from .merge_binary import merge_binary_to_dot, save_binary_merged_dot

__all__ = [
    "template_to_dot",
    "save_template_dot",
    "merge_binary_to_dot",
    "save_binary_merged_dot",
]

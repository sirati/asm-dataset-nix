"""Graphviz DOT renderers for templates and per-binary merges.

Public surface:
    template_to_dot, save_template_dot — single Template renderer.
"""

from .template import template_to_dot, save_template_dot

__all__ = [
    "template_to_dot",
    "save_template_dot",
]

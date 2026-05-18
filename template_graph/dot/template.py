"""Single-template Graphviz DOT renderer.

Renders one :class:`~template_graph.graph.Template` as DOT.

Colours: toolchain = lightgray, common_dep = palegreen,
variant_specific = lightcoral, unclassified = white. Open the
output with ``xdot``, ``dot -Tsvg``, or any DOT viewer.
"""

from __future__ import annotations

from typing import Optional

from template_graph.graph import Template


def _enforce_label(enforce: Optional[tuple[str, Optional[str]]]) -> str:
    """One-line render of an enforce tuple for use in DOT labels."""
    if enforce is None:
        return ""
    kind, val = enforce
    if kind == "this-target":
        return " ⟨this-target⟩"
    if kind == "triple":
        return " ⟨native⟩" if val is None else f" ⟨{val}⟩"
    if kind == "version":
        return f" ⟨v{val}⟩"
    return f" ⟨{kind}:{val}⟩"


def template_to_dot(
    template: Template,
    classifications: Optional[dict] = None,
    *,
    label: str = "template",
) -> str:
    """Render a Template as Graphviz DOT.

    Colours: toolchain = lightgray, common_dep = palegreen,
    variant_specific = lightcoral, unclassified = white. Open the
    output with ``xdot``, ``dot -Tsvg``, or any DOT viewer.
    """
    classifications = classifications or {}
    lines: list[str] = []
    safe_label = label.replace('"', '\\"')
    lines.append(f'digraph "{safe_label}" {{')
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=filled, fontname=monospace];")
    for nid, node in enumerate(template.nodes):
        if node.is_toolchain:
            fill = "lightgray"
        else:
            cls = classifications.get(nid)
            fill = {
                "common_dep": "palegreen",
                "variant_specific": "lightcoral",
            }.get(cls, "white")
        safe_name = node.name.replace('"', '\\"')
        style = "filled,dashed" if node.optional else "filled"
        suffix = " *" if node.optional else ""
        suffix += _enforce_label(node.enforce)
        lines.append(
            f'  n{nid} [label="{safe_name}{suffix}", '
            f'fillcolor={fill}, style="{style}"];'
        )
    for nid, node in enumerate(template.nodes):
        for cid in node.child_ids:
            lines.append(f"  n{nid} -> n{cid};")
    lines.append("}")
    return "\n".join(lines)


def save_template_dot(
    template: Template,
    classifications: Optional[dict],
    out_path: str,
    *,
    label: str = "template",
) -> None:
    with open(out_path, "w") as f:
        f.write(template_to_dot(template, classifications, label=label))
        f.write("\n")

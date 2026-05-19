"""Template-graph data types and structural matching.

This module owns the shape-level primitives of the template graph:

  * ``TemplateNode`` — one role-position in a binary's build graph.
  * ``Template`` — a structural shape (collection of nodes + root).
  * ``TemplateGraphAssertError`` — hard-assert failure carrying the
    anchor labels that locked the template plus the failing variant.
  * ``TemplateAlignment`` — role-aligned correspondence between two
    same-shape templates, returned by ``_shape_equal``.
  * ``_shape_equal`` — structural equality check between two
    templates; returns a ``TemplateAlignment`` (with the recursive-
    walk role-correspondence) iff the templates have matching shape,
    otherwise ``None``.
  * ``find_or_register_template`` — re-use-or-append helper used when
    a candidate template's shape is computed from a calibration pair.

Behaviour is unchanged from the previous ``template_graph.core``
module; this file just hosts the canonical definitions so other
modules can depend on the graph layer without pulling in the cowalk
algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TemplateNode:
    name: str
    child_ids: list[int] = field(default_factory=list)
    is_toolchain: bool = False
    visit_flag: bool = False
    # True if at calibration time we only saw this node on ONE side
    # of the pair. Variants are allowed to have None at this position
    # in arr.hashes (the variant simply doesn't include it).
    optional: bool = False
    # When a DAG-revisit reveals two distinct (hash, name) values at
    # this role-position, the template node is split. Each resulting
    # node carries a constraint:
    #   ("triple", "<triple>") — must have this exact target triple
    #   ("triple", None)       — must be native (no triple in name)
    #   ("this-target", None)  — must match cowalking variant's arch triple
    #   ("version", "<v>")     — must be this exact version
    enforce: Optional[tuple[str, Optional[str]]] = None


@dataclass
class Template:
    nodes: list[TemplateNode]
    name_to_id: dict[str, int]
    root_id: int
    # Variant labels that locked this template's shape. Up to two
    # entries: the variant whose closure was walked to build it +
    # (if present) the next variant of the same arch group whose
    # cowalk succeeded. Error messages cite both.
    template_built_from: list[str] = field(default_factory=list)


class TemplateGraphAssertError(Exception):
    """Hard-assert failure. Always carries anchor labels + failing label."""

    def __init__(
        self,
        *,
        kind: str,
        message: str,
        template_built_from: Optional[list[str]] = None,
        failing_variant: Optional[str] = None,
        node_name: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> None:
        parts = [f"[{kind}] {message}"]
        anchors = list(template_built_from or [])
        if anchors or failing_variant is not None:
            parts.append(
                "  template built from: "
                + (", ".join(repr(v) for v in anchors) if anchors else "<none>")
            )
        if failing_variant is not None:
            parts.append(f"  failing variant:     {failing_variant!r}")
        if node_name is not None:
            parts.append(f"  at node:             {node_name!r}")
        if details:
            for k, v in details.items():
                parts.append(f"  {k}: {v!r}")
        super().__init__("\n".join(parts))
        self.kind = kind
        self.template_built_from = anchors
        self.failing_variant = failing_variant
        self.node_name = node_name
        self.details = dict(details or {})


@dataclass(frozen=True)
class TemplateAlignment:
    """Role-aligned correspondence between two same-shape templates.

    ``node_pairs[i] = (node_id_in_a, node_id_in_b)`` for the i-th node
    visited in canonical recursive-walk order starting at ``a.root_id``
    / ``b.root_id``. The walk order matches the structural-equality
    walk in ``_shape_equal`` — pre-order, child-index-aligned, with
    each ``a``-node visited only on its first occurrence (DAG-revisits
    on the ``a`` side are skipped, mirroring the equality check's
    memoised recursion).

    ``None`` is returned by ``_shape_equal`` in place of an alignment
    when the templates don't have matching shape (was the ``False``
    return of the previous ``bool``-returning version).
    """

    node_pairs: tuple[tuple[int, int], ...]


def _shape_equal(a: Template, b: Template) -> Optional[TemplateAlignment]:
    """Return a ``TemplateAlignment`` iff ``a`` and ``b`` have the
    same shape (same node count, same per-node name/is_toolchain, same
    child-index-aligned recursive structure starting from each root);
    return ``None`` otherwise.

    The alignment records ``(a_id, b_id)`` pairs in the order the
    recursive walk first visits each ``a``-node, so callers (e.g.
    ``MetaTemplate`` construction in Phase 4.2) can map ``a``-side
    role-positions onto their ``b``-side counterparts.
    """
    if len(a.nodes) != len(b.nodes):
        return None
    mapping: dict[int, int] = {}
    pairs: list[tuple[int, int]] = []

    def _eq(an: int, bn: int) -> bool:
        if an in mapping:
            return mapping[an] == bn
        mapping[an] = bn
        pairs.append((an, bn))
        na = a.nodes[an]
        nb = b.nodes[bn]
        if na.name != nb.name:
            return False
        if na.is_toolchain != nb.is_toolchain:
            return False
        if len(na.child_ids) != len(nb.child_ids):
            return False
        for ac, bc in zip(na.child_ids, nb.child_ids):
            if not _eq(ac, bc):
                return False
        return True

    if not _eq(a.root_id, b.root_id):
        return None
    return TemplateAlignment(node_pairs=tuple(pairs))


def find_or_register_template(
    templates: list[Template], candidate: Template
) -> tuple[int, bool]:
    """Return (id, was_newly_registered)."""
    for i, t in enumerate(templates):
        if _shape_equal(t, candidate) is not None:
            return i, False
    templates.append(candidate)
    return len(templates) - 1, True

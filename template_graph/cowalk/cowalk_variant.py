"""Cowalk a raw variant tree into a template's ``VariantArray``.
Lifted from ``StreamPlanner._cowalk_into_arr`` and split into small
free helpers around a ``CowalkCtx`` recursion handle. The planner
keeps a thin ``_cowalk_into_arr`` shim that delegates here —
behaviour is unchanged."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from template_graph.cowalk._helpers import _ARCH_TO_TRIPLE, _classify_revisit_diff
from template_graph.graph import (
    Template,
    TemplateGraphAssertError,
    TemplateNode,
    VariantArray,
)
from template_graph.parser.role import _is_stdenv_role

if TYPE_CHECKING:  # pragma: no cover
    from template_graph.streaming import RawTreeNode, StreamPlanner


@dataclass
class CowalkCtx:
    """Per-recursion state for cowalking one variant into a template's
    VariantArray. Carries the planner (for cross-cutting helpers like
    ``_extend_template_with_subtree`` and ``_record``) plus the tuple
    every helper reads or updates."""
    planner: "StreamPlanner"
    tmpl_id: int
    arch: str
    label: str
    arr: VariantArray
    v_pos: int
    template: Template


def _resolve_enforce(
    ctx: CowalkCtx, enf: tuple[str, Optional[str]],
) -> tuple[str, Optional[str]]:
    """Collapse a triple-enforce into 'this-target' when it equals the
    current arch's triple."""
    if enf[0] == "triple":
        target = _ARCH_TO_TRIPLE.get(ctx.arch)
        if enf[1] is not None and enf[1] == target:
            return ("this-target", None)
    return enf


def _split_dag_revisit(
    ctx: CowalkCtx,
    parent_nid: int,
    cid: int,
    observed_ident: tuple[str, str],
    diff: tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]],
) -> int:
    """Clone the original template node, re-point the parent at the
    clone, and grow every variant_array of this template by one row.
    ``orig`` keeps its stored enforce for previously-cowalked variants;
    the clone gets the freshly-observed enforce."""
    orig = ctx.template.nodes[cid]
    _diff_kind, stored_enf, observed_enf = diff
    if orig.enforce is None:
        orig.enforce = _resolve_enforce(ctx, stored_enf)
    new_node = TemplateNode(
        name=orig.name, child_ids=list(orig.child_ids),
        is_toolchain=orig.is_toolchain, visit_flag=True,
        optional=orig.optional,
        enforce=_resolve_enforce(ctx, observed_enf),
    )
    new_nid = len(ctx.template.nodes)
    ctx.template.nodes.append(new_node)
    # Clone original's row: variants that agreed on both DAG paths
    # keep their value; only divergent variants overwrite later.
    for (tid_x, _ax), other_arr in ctx.planner.out.variant_arrays.items():
        if tid_x == ctx.tmpl_id:
            other_arr.hashes.append(list(other_arr.hashes[cid]))
    ctx.arr.hashes[new_nid][ctx.v_pos] = observed_ident
    parent = ctx.template.nodes[parent_nid]
    idx = parent.child_ids.index(cid)
    parent.child_ids[idx] = new_nid
    return new_nid


def _partition_children_by_name(
    ctx: CowalkCtx, node: TemplateNode, t_node: "RawTreeNode",
) -> tuple[
    dict[str, list[int]], dict[str, list["RawTreeNode"]],
    set[str], set[str], set[str],
]:
    """Group both sides by name; return tbn, abn, common, only_tmpl,
    only_actual."""
    template_by_name: dict[str, list[int]] = {}
    for cid in node.child_ids:
        template_by_name.setdefault(
            ctx.template.nodes[cid].name, [],
        ).append(cid)
    actual_by_name: dict[str, list["RawTreeNode"]] = {}
    for c in t_node.children:
        actual_by_name.setdefault(
            ctx.planner.name_extractor(c.name), [],
        ).append(c)
    common = set(template_by_name) & set(actual_by_name)
    only_tmpl = set(template_by_name) - common
    only_actual = set(actual_by_name) - common
    return template_by_name, actual_by_name, common, only_tmpl, only_actual


def _promote_optional_missing(
    ctx: CowalkCtx, node: TemplateNode,
    only_tmpl: set[str], template_by_name: dict[str, list[int]],
) -> None:
    """Mark template-only children optional and record the promotion."""
    promoted: list[str] = []
    for name in sorted(only_tmpl):
        for cid in template_by_name[name]:
            if not ctx.template.nodes[cid].optional:
                ctx.template.nodes[cid].optional = True
                promoted.append(name)
    if promoted:
        ctx.planner._record(
            "cowalk-promoted-to-optional",
            node_role=node.name, label=ctx.label,
            promoted_children=promoted,
        )


def _extend_with_new(
    ctx: CowalkCtx, node: TemplateNode, self_nid: int,
    only_actual: set[str],
    actual_by_name: dict[str, list["RawTreeNode"]],
) -> None:
    """Extend the template with each raw-only subtree (via planner)."""
    if not only_actual:
        return
    extended: list[str] = []
    for name in sorted(only_actual):
        for c in actual_by_name[name]:
            ctx.planner._extend_template_with_subtree(
                ctx.tmpl_id, ctx.arch, ctx.v_pos, self_nid, c,
            )
        extended.append(name)
    ctx.planner._record(
        "cowalk-template-extended",
        node_role=node.name, label=ctx.label, new_children=extended,
    )


def _recurse_common(
    ctx: CowalkCtx, node: TemplateNode, self_nid: int,
    common: set[str],
    template_by_name: dict[str, list[int]],
    actual_by_name: dict[str, list["RawTreeNode"]],
) -> None:
    """Per common name pair sorted-by-ident actuals with cids and
    recurse. Count mismatches raise (strict) or record (lax)."""
    for name in sorted(common):
        cids = template_by_name[name]
        actuals = sorted(actual_by_name[name], key=lambda r: r.ident)
        if len(actuals) != len(cids):
            if not ctx.planner.lax:
                raise TemplateGraphAssertError(
                    kind="child-count-mismatch",
                    message=(
                        f"variant {ctx.label!r} at node #{self_nid} "
                        f"({node.name!r}): name {name!r} "
                        f"template={len(cids)} actual={len(actuals)}"
                    ),
                    template_built_from=ctx.template.template_built_from,
                    failing_variant=ctx.label, node_name=node.name,
                )
            ctx.planner._record(
                "cowalk-child-count-mismatch",
                node_role=node.name, label=ctx.label, child_name=name,
                template_count=len(cids), actual_count=len(actuals),
            )
        n = min(len(cids), len(actuals))
        for cid, c in zip(cids[:n], actuals[:n]):
            _cowalk_visit(ctx, cid, c, self_nid)


def _cowalk_visit_children(
    ctx: CowalkCtx, self_nid: int, t_node: "RawTreeNode",
) -> None:
    """Pair this template node's children with this raw node's:
    partition → promote-missing → extend-new → recurse-common."""
    node = ctx.template.nodes[self_nid]
    tbn, abn, common, only_tmpl, only_actual = _partition_children_by_name(
        ctx, node, t_node,
    )
    _promote_optional_missing(ctx, node, only_tmpl, tbn)
    _extend_with_new(ctx, node, self_nid, only_actual, abn)
    _recurse_common(ctx, node, self_nid, common, tbn, abn)


def _handle_revisit(
    ctx: CowalkCtx, nid: int, t_node: "RawTreeNode",
    parent_nid: Optional[int], node: TemplateNode,
) -> None:
    """Revisit: stored match → noop; clean triple/version delta with
    a parent → DAG-split + recurse; else raise (strict) or record."""
    stored = ctx.arr.hashes[nid][ctx.v_pos]
    if stored is None or stored == t_node.ident:
        return
    diff = _classify_revisit_diff(stored, t_node.ident)
    if diff is not None and parent_nid is not None:
        new_nid = _split_dag_revisit(
            ctx, parent_nid, nid, t_node.ident, diff,
        )
        ctx.planner._record(
            "cowalk-dag-revisit-split",
            node_role=node.name, label=ctx.label,
            diff_kind=diff[0], stored=stored, observed=t_node.ident,
        )
        if not t_node.is_backref:
            _cowalk_visit_children(ctx, new_nid, t_node)
        return
    if not ctx.planner.lax:
        raise TemplateGraphAssertError(
            kind="dag-revisit-hash-mismatch",
            message=(
                f"DAG revisit at node #{nid} ({node.name!r}) "
                f"observed drv differs"
            ),
            template_built_from=ctx.template.template_built_from,
            failing_variant=ctx.label, node_name=node.name,
            details={"stored": stored, "observed": t_node.ident},
        )
    ctx.planner._record(
        "cowalk-dag-revisit-hash-mismatch",
        node_role=node.name, label=ctx.label,
        stored=stored, observed=t_node.ident,
    )


def _cowalk_visit(
    ctx: CowalkCtx, nid: int, t_node: "RawTreeNode",
    parent_nid: Optional[int],
) -> None:
    """Visit one template node paired with one raw node. Records the
    raw ident; on revisit defers to ``_handle_revisit``; toolchain
    siphoned via ``_discard_subtree``."""
    node = ctx.template.nodes[nid]
    if not t_node.is_backref:
        ctx.planner.mx.unclassified_nodes.discard(t_node.ident)
    if node.visit_flag:
        if not node.is_toolchain:
            _handle_revisit(ctx, nid, t_node, parent_nid, node)
        return
    node.visit_flag = True
    if node.is_toolchain:
        if _is_stdenv_role(node.name):
            ctx.planner.out.stdenv_subtrees.setdefault(t_node.ident, {
                "first_seen_in": {
                    "matrix": ctx.planner.mx.matrix_binary,
                    "arch": ctx.arch, "label": ctx.label,
                },
                "root": t_node,
            })
        ctx.planner._discard_subtree(t_node)
        return
    ctx.arr.hashes[nid][ctx.v_pos] = t_node.ident
    if t_node.is_backref:
        return
    _cowalk_visit_children(ctx, nid, t_node)


def cowalk_into_arr(
    planner: "StreamPlanner",
    tmpl_id: int,
    arch: str,
    label: str,
    tree: "RawTreeNode",
    *,
    arr: Optional[VariantArray] = None,
) -> None:
    """Cowalk one variant raw tree into this template's variant array.
    Appends a column to ``arr.hashes``, records placement, clears
    every node's ``visit_flag``."""
    template = planner.out.templates[tmpl_id]
    if arr is None:
        arr = planner.out.variant_arrays[(tmpl_id, arch)]
    v_pos = len(arr.variants)
    arr.variants.append(label)
    for row in arr.hashes:
        row.append(None)
    for n in template.nodes:
        n.visit_flag = False
    planner.out.placement[tree.ident] = (tmpl_id, arch, v_pos)
    ctx = CowalkCtx(
        planner=planner, tmpl_id=tmpl_id, arch=arch, label=label,
        arr=arr, v_pos=v_pos, template=template,
    )
    # Pair root_id ↔ raw root (no name-match; entry-point names embed
    # comp+opt and differ across variants of same (binary, arch)).
    _cowalk_visit(ctx, template.root_id, tree, parent_nid=None)

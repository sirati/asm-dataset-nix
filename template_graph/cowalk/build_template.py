"""Template construction from raw-tree calibration pairs / singletons.

Lifted out of ``template_graph.streaming``. Free functions over a
``StreamPlanner`` handle (``planner.out|mx|vb`` plus helper methods
``_discard_subtree`` and ``_record``). The reverse import is
``TYPE_CHECKING``-guarded."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from template_graph.cowalk._alloc_helpers import (
    _capture_stdenv,
    _record_source_terminal,
)
from template_graph.graph import Template, TemplateNode
from template_graph.parser.role import (
    _is_compiler_wrapper_role,
    _is_source_terminal_role,
    _is_stdenv_role,
)
from template_graph.tree_walker import TreeWalkError

if TYPE_CHECKING:
    from template_graph.streaming import RawTreeNode, StreamPlanner


def make_template_node(
    planner: "StreamPlanner", raw_nodes: list["RawTreeNode"], *,
    optional: bool, label_slots: list[str], arch: Optional[str] = None,
) -> TemplateNode:
    """Build a TemplateNode from one or two raw nodes; capture stdenv
    subtrees; classify the toolchain bit. Source-terminal roles fold
    into ``is_toolchain`` so the walker discards their subtrees; the
    counter + ``arch_indep_deps`` recording happen once per fresh alloc."""
    name = planner.name_extractor(raw_nodes[0].name)
    is_stdenv = _is_stdenv_role(name)
    eff_arch = arch if arch is not None else planner.vb.building_arch
    if is_stdenv:
        _capture_stdenv(planner, raw_nodes, label_slots, eff_arch)
    is_src_terminal = _is_source_terminal_role(name)
    if is_src_terminal:
        _record_source_terminal(planner, raw_nodes)
    is_toolchain = (
        is_stdenv
        or _is_compiler_wrapper_role(name)
        or is_src_terminal
        or any(rn.ident in planner.out.toolchain_drvs for rn in raw_nodes)
    )
    return TemplateNode(
        name=name, child_ids=[], is_toolchain=is_toolchain,
        visit_flag=False, optional=optional,
    )


def walk_one_sided_subtree(
    planner: "StreamPlanner", rn: "RawTreeNode", template: Template,
    alloc_fn: Callable[["RawTreeNode"], tuple[int, bool]],
    post_fresh: Optional[Callable[[int, "RawTreeNode"], None]] = None,
) -> int:
    """Walk a one-sided subtree. ``alloc_fn`` owns (nid, fresh)
    allocation+dedup; ``post_fresh`` runs after fresh allocation."""
    nid, fresh = alloc_fn(rn)
    if not rn.is_backref:
        planner.mx.unclassified_nodes.discard(rn.ident)
    if not fresh:
        return nid
    node = template.nodes[nid]
    if post_fresh is not None:
        post_fresh(nid, rn)
    if node.is_toolchain:
        planner._discard_subtree(rn)
        return nid
    for c in rn.children:
        cid = walk_one_sided_subtree(planner, c, template, alloc_fn, post_fresh)
        if cid not in node.child_ids:
            node.child_ids.append(cid)
    return nid


@dataclass
class _PairWalkCtx:
    """Pair-walk scratch. ``pair_to_id`` keys:
    pair ``((h0,n0),(h1,n1))``; one-sided t0 ``((h,n),None)``; one-sided
    t1 ``(None,(h,n))``."""
    template: Template
    pair_to_id: dict = field(default_factory=dict)
    label_slots: tuple[str, str] = ("", "")


def _alloc_pair(
    planner: "StreamPlanner", ctx: _PairWalkCtx,
    t0: "RawTreeNode", t1: "RawTreeNode",
) -> tuple[int, bool]:
    key = (t0.ident, t1.ident)
    if key in ctx.pair_to_id:
        return ctx.pair_to_id[key], False
    tnode = make_template_node(
        planner, [t0, t1],
        optional=False, label_slots=list(ctx.label_slots),
    )
    nid = len(ctx.template.nodes)
    ctx.template.nodes.append(tnode)
    ctx.pair_to_id[key] = nid
    return nid, True


def _walk_one_sided(
    planner: "StreamPlanner", ctx: _PairWalkCtx,
    rn: "RawTreeNode", is_t0_side: bool,
) -> int:
    """One-sided subtree walk; every allocated template node is optional."""
    slot = ctx.label_slots[0 if is_t0_side else 1]

    def _alloc(r: "RawTreeNode") -> tuple[int, bool]:
        key = (r.ident, None) if is_t0_side else (None, r.ident)
        if key in ctx.pair_to_id:
            return ctx.pair_to_id[key], False
        tnode = make_template_node(
            planner, [r], optional=True, label_slots=[slot],
        )
        nid_ = len(ctx.template.nodes)
        ctx.template.nodes.append(tnode)
        ctx.pair_to_id[key] = nid_
        return nid_, True

    return walk_one_sided_subtree(planner, rn, ctx.template, _alloc)


def _bucket_pair_children(
    name_extractor: Callable[[str], str],
    t0: "RawTreeNode",
    t1: "RawTreeNode",
) -> tuple[
    dict[str, list["RawTreeNode"]],
    dict[str, list["RawTreeNode"]],
    set[str], set[str], set[str],
]:
    """Bucket each side's children by extracted name."""
    t0_by: dict[str, list["RawTreeNode"]] = {}
    t1_by: dict[str, list["RawTreeNode"]] = {}
    for c in t0.children:
        t0_by.setdefault(name_extractor(c.name), []).append(c)
    for c in t1.children:
        t1_by.setdefault(name_extractor(c.name), []).append(c)
    common = set(t0_by) & set(t1_by)
    return t0_by, t1_by, common, set(t0_by) - common, set(t1_by) - common


def _walk_common_pair_children(
    planner: "StreamPlanner", ctx: _PairWalkCtx, node: TemplateNode,
    common: set[str],
    t0_by_name: dict[str, list["RawTreeNode"]],
    t1_by_name: dict[str, list["RawTreeNode"]],
) -> None:
    """Pair-walk common-name buckets. Strict raises on size mismatch;
    lax records and walks the shorter prefix."""
    for name in sorted(common):
        t0_lst = sorted(t0_by_name[name], key=lambda r: r.ident)
        t1_lst = sorted(t1_by_name[name], key=lambda r: r.ident)
        if len(t0_lst) != len(t1_lst):
            if not planner.lax:
                raise TreeWalkError(
                    f"calibration pair same-name child count mismatch "
                    f"at node {node.name!r}, name {name!r}: "
                    f"t0={len(t0_lst)} vs t1={len(t1_lst)}"
                )
            planner._record(
                "calibration-same-name-count-mismatch",
                node_role=node.name, child_name=name,
                t0_count=len(t0_lst), t1_count=len(t1_lst),
                labels=list(ctx.template.template_built_from),
            )
        n = min(len(t0_lst), len(t1_lst))
        for c0, c1 in zip(t0_lst[:n], t1_lst[:n]):
            node.child_ids.append(_walk_pair_node(planner, ctx, c0, c1))


def _walk_pair_node(
    planner: "StreamPlanner", ctx: _PairWalkCtx,
    t0: "RawTreeNode", t1: "RawTreeNode",
) -> int:
    """Allocate-or-lookup a paired-position template node and recurse."""
    nid, fresh = _alloc_pair(planner, ctx, t0, t1)
    if not t0.is_backref:
        planner.mx.unclassified_nodes.discard(t0.ident)
    if not t1.is_backref:
        planner.mx.unclassified_nodes.discard(t1.ident)
    if not fresh:
        return nid
    node = ctx.template.nodes[nid]
    if node.is_toolchain:
        planner._discard_subtree(t0)
        planner._discard_subtree(t1)
        return nid
    if t0.ident == t1.ident:
        ref = t0 if t0.children else t1
        for c in ref.children:
            node.child_ids.append(_walk_pair_node(planner, ctx, c, c))
        return nid
    t0_by, t1_by, common, only_t0, only_t1 = _bucket_pair_children(
        planner.name_extractor, t0, t1,
    )
    # One-sided children → optional subtrees (expected: stdenv splices
    # setup-hooks per compiler choice, etc., not a violation).
    for name in sorted(only_t0):
        for c0 in t0_by[name]:
            node.child_ids.append(_walk_one_sided(planner, ctx, c0, True))
    for name in sorted(only_t1):
        for c1 in t1_by[name]:
            node.child_ids.append(_walk_one_sided(planner, ctx, c1, False))
    _walk_common_pair_children(planner, ctx, node, common, t0_by, t1_by)
    return nid


def build_template(
    planner: "StreamPlanner",
    tree0: "RawTreeNode", tree1: "RawTreeNode",
    label0: str, label1: str,
) -> Template:
    """Parallel-walk two raw trees. Identity is the pair
    ``(t0.ident, t1.ident)``: a DAG join in both raw trees at the same
    position links back to the existing template node. Distinct
    hash-paths get distinct template nodes even if post-hash names
    match."""
    template = Template(
        nodes=[], name_to_id={}, root_id=0,
        template_built_from=[label0, label1],
    )
    ctx = _PairWalkCtx(
        template=template, label_slots=planner.vb.building_label_pair,
    )
    _walk_pair_node(planner, ctx, tree0, tree1)
    return template


def _alloc_singleton(
    planner: "StreamPlanner", template: Template,
    label: str, tn: "RawTreeNode",
) -> int:
    """Find-or-allocate a singleton template node by extracted name.
    Source-terminal roles fold into ``is_toolchain`` so the walker
    discards their subtrees; counter + ``arch_indep_deps`` recording
    happens once per fresh alloc."""
    name = planner.name_extractor(tn.name)
    is_stdenv = _is_stdenv_role(name)
    if is_stdenv:
        _capture_stdenv(planner, [tn], [label], planner.vb.building_arch)
    if name in template.name_to_id:
        return template.name_to_id[name]
    is_src_terminal = _is_source_terminal_role(name)
    if is_src_terminal:
        _record_source_terminal(planner, [tn])
    is_toolchain = (
        is_stdenv or _is_compiler_wrapper_role(name)
        or is_src_terminal
        or tn.ident in planner.out.toolchain_drvs
    )
    node = TemplateNode(
        name=name, child_ids=[],
        is_toolchain=is_toolchain, visit_flag=False,
    )
    nid = len(template.nodes)
    template.nodes.append(node)
    template.name_to_id[name] = nid
    return nid


def build_template_singleton(
    planner: "StreamPlanner", tree: "RawTreeNode", label: str,
) -> Template:
    """Single-variant arch → mirror structure; all nodes are
    common_dep candidates (no calibration possible)."""
    template = Template(
        nodes=[], name_to_id={}, root_id=0,
        template_built_from=[label],
    )
    visited: set[int] = set()

    def _walk(tn: "RawTreeNode") -> int:
        nid = _alloc_singleton(planner, template, label, tn)
        if not tn.is_backref:
            planner.mx.unclassified_nodes.discard(tn.ident)
        if nid in visited:
            return nid
        visited.add(nid)
        if template.nodes[nid].is_toolchain:
            planner._discard_subtree(tn)
            return nid
        for c in tn.children:
            cid = _walk(c)
            if cid not in template.nodes[nid].child_ids:
                template.nodes[nid].child_ids.append(cid)
        return nid

    _walk(tree)
    return template

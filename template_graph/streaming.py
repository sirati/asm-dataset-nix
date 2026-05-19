"""Single-pass streaming planner over `nix-store --query --tree` output.

Replaces the two-phase (walk → plan_phase1_graph) approach: we never
call ``nix derivation show`` because every drv's immediate inputs are
already present in the tree (as direct children at depth+1). The
algorithm walks the output line-by-line, opens a per-variant RawTree
buffer when it enters a matrix variant subtree, and as soon as ANY
arch accumulates two raw trees we build the template from that
calibration pair, drain that arch's buffer into the new variant array,
and continue streaming. Subsequent variants of the same arch
stream directly into the array (no further buffering).

Reused from ``template_graph.graph``:
    Template, TemplateNode, VariantArray,
    find_or_register_template,
    TemplateGraphAssertError.

Public surface:
    StreamPlanner — instantiate, feed lines via .feed_line(), call .finalize().
    plan_from_tree_streaming(tree_text, *, archs=...) -> dict
        Same return shape as ``core.plan_phase1_graph``:
            { templates, variant_arrays, placement,
              common_deps_per_arch_template,
              toolchain_drvs, arch_indep_deps }
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from template_graph.graph import (
    Template,
    TemplateNode,
    VariantArray,
    TemplateGraphAssertError,
    find_or_register_template,
)
from template_graph.tree_walker import (
    DEFAULT_ARCHS,
    TreeWalkError,
    _MATRIX_RE,
    _TOOLCHAINS_RE,
    VARIANT_SUFFIX,
    parse_variant_path,
    _parse_line,
)
# Role extraction lives in template_graph.parser.role; re-export the
# public-facing helpers here so existing
# ``from template_graph.streaming import drv_role`` callers keep working.
# _TRIPLE_RE is also used below by _extract_triple / _extract_version.
from template_graph.parser.role import (
    _TRIPLE_RE,
    _is_compiler_wrapper_role,
    _is_stdenv_role,
    drv_role,
)
from template_graph.dot import (
    template_to_dot,  # noqa: F401  back-compat re-export
    save_template_dot,  # noqa: F401  back-compat re-export
    merge_binary_to_dot,  # noqa: F401  back-compat re-export
    save_binary_merged_dot,  # noqa: F401  back-compat re-export
)
from template_graph.cowalk import (
    build_template,
    build_template_singleton,
    make_template_node,
    walk_one_sided_subtree,
)


def drv_name_full(name: str) -> str:
    """Identity passthrough for the post-hash drv name. Kept as a
    function so callers can switch in another extractor."""
    return name


# Map our short arch keys (matching lib/architectures.nix) to the
# canonical Nix target triple. x86_64 is native — no triple shows up
# in drv names.
_ARCH_TO_TRIPLE: dict[str, Optional[str]] = {
    "x86_64":    None,
    "i686":      "i686-unknown-linux-gnu",
    "aarch64":   "aarch64-unknown-linux-gnu",
    "armv7l-hf": "armv7l-unknown-linux-gnueabihf",
    "armv7l-sf": "armv7l-unknown-linux-gnueabi",
    "mipsel":    "mipsel-unknown-linux-gnu",
    "mips64el":  "mips64el-unknown-linux-gnuabin32",
    "ppc32":     "powerpc-unknown-linux-gnu",
    "ppc64":     "powerpc64-unknown-linux-gnuabielfv2",
    "riscv64":   "riscv64-unknown-linux-gnu",
}


def _extract_triple(name: str) -> Optional[str]:
    """Return the embedded target triple (no leading/trailing - or _)
    or None if the name has none."""
    base = name[:-4] if name.endswith(".drv") else name
    m = _TRIPLE_RE.search(base)
    return m.group(0).strip("-_") if m else None


def _extract_version(name: str) -> str:
    """Triple-strip the name, then return the first -<digits>[.<digits>...]
    sequence, or '' if none."""
    base = name[:-4] if name.endswith(".drv") else name
    no_triple = _TRIPLE_RE.sub("", base)
    m = re.search(r"-(\d[\d.]*[a-z]?\d*)", no_triple)
    return m.group(1) if m else ""


def _classify_revisit_diff(
    stored: tuple[str, str],
    observed: tuple[str, str],
) -> Optional[tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]]]:
    """Decide whether two distinct (hash, name) values at the same DAG
    position differ purely by target-triple or purely by version. Returns
    (diff_kind, stored_enforce, observed_enforce) or None for any other
    kind of difference (caller should fall through to the violation log).
    """
    sn, on = stored[1], observed[1]
    s_tri = _extract_triple(sn)
    o_tri = _extract_triple(on)
    s_ver = _extract_version(sn)
    o_ver = _extract_version(on)
    if s_tri != o_tri and s_ver == o_ver:
        return ("triple", ("triple", s_tri), ("triple", o_tri))
    if s_tri == o_tri and s_ver != o_ver:
        return ("version", ("version", s_ver), ("version", o_ver))
    return None


# ── RawTree: in-memory per-variant subtree ───────────────────────────


@dataclass
class RawTreeNode:
    """A raw-tree drv reference. ``(hash, name)`` is the canonical
    identity — the ``/nix/store/`` prefix is implied and never stored.
    Read ``rn.ident`` for set/dict keys; read ``rn.name`` for
    name-extraction (role / package name).
    """
    hash: str
    name: str
    is_backref: bool
    depth: int
    children: list["RawTreeNode"] = field(default_factory=list)

    @property
    def ident(self) -> tuple[str, str]:
        return (self.hash, self.name)


# ── State groups ─────────────────────────────────────────────────────
#
# Phase 3.2 grouping: planner state lives in three typed dataclasses
# rather than directly on ``StreamPlanner``. ``OutputState`` holds the
# accumulating outputs (the dict ``finalize()`` returns). ``MatrixState``
# holds transient state reset on each matrix transition. ``VariantBuilderState``
# holds transient state for the currently-buffering variant raw tree
# plus build-context for template construction and cowalk (``_building_*``).
#
# Phase 3.3 will move cowalk helpers out of the planner; they will take
# typed handles to these state groups rather than the planner itself.


@dataclass
class OutputState:
    """Outputs the planner produces by the end. Same shape as
    ``plan_phase1_graph``'s return dict (plus ``stdenv_subtrees`` and
    ``violations``)."""
    templates: list[Template] = field(default_factory=list)
    variant_arrays: dict[tuple[int, str], VariantArray] = field(
        default_factory=dict
    )
    placement: dict[str, tuple[int, str, int]] = field(default_factory=dict)
    classifications: dict[tuple[int, str], dict[int, str]] = field(
        default_factory=dict
    )
    # All identity-bearing collections use ``(hash, name)`` tuples.
    # The ``/nix/store/`` prefix is implied; never reconstructed.
    toolchain_drvs: set[tuple[str, str]] = field(default_factory=set)
    # Per-binary arch-indep deps surfaced at matrix depth 2.
    arch_indep_deps: dict[str, set[tuple[str, str]]] = field(
        default_factory=dict
    )
    # Stdenv raw subtrees, siphoned during template construction and
    # cowalk. Keyed by the stdenv root ``(hash, name)`` so a stdenv
    # referenced from many variants captures once.
    stdenv_subtrees: dict[tuple[str, str], dict] = field(default_factory=dict)
    # Lax-mode shape-violation log.
    violations: list[dict] = field(default_factory=list)


@dataclass
class MatrixState:
    """Per-matrix transient state. Reset (re-instantiated) when
    ``_on_depth1`` sees the next matrix wrapper."""
    matrix_binary: Optional[str] = None
    pending_raw_trees: dict[str, list[tuple[str, "RawTreeNode"]]] = field(
        default_factory=dict
    )
    arch_template_id: dict[str, int] = field(default_factory=dict)
    # Drvs encountered (non-backref) in this matrix's variants that
    # haven't been placed into any bucket. Should drain to empty by
    # end-of-matrix; non-empty surfaces a missing edge case.
    unclassified_nodes: set[tuple[str, str]] = field(default_factory=set)


@dataclass
class VariantBuilderState:
    """Per-variant raw-tree assembly state, plus build-context used
    while constructing a template or cowalking a variant.

    ``cur_*`` fields hold the raw tree being assembled from incoming
    feed_line() calls. ``building_arch`` / ``building_label_pair`` are
    scratch context passed from ``_build_and_drain_arch`` /
    ``_build_template_singleton`` into the inner ``_alloc`` / ``_visit``
    closures so stdenv captures know which ``(matrix, arch, label)``
    triple produced them.
    """
    cur_root: Optional["RawTreeNode"] = None
    cur_arch: Optional[str] = None
    cur_label: Optional[str] = None
    cur_drv: Optional[tuple[str, str]] = None
    cur_stack: list["RawTreeNode"] = field(default_factory=list)
    # ``(hash, name)`` → RawTreeNode within the variant currently being
    # built. Collapses re-encountered idents onto the existing node
    # (DAG via nix's [...] back-refs).
    cur_path_to_node: dict[tuple[str, str], "RawTreeNode"] = field(
        default_factory=dict
    )
    building_arch: Optional[str] = None
    building_label_pair: tuple[str, str] = ("", "")


# ── Streaming planner ────────────────────────────────────────────────


class StreamPlanner:
    def __init__(
        self,
        *,
        archs: tuple[str, ...] = DEFAULT_ARCHS,
        name_extractor=drv_role,
        logger=None,
        lax: bool = False,
    ):
        self.archs = archs
        self.name_extractor = name_extractor
        self.logger = logger or (lambda _msg: None)
        # Survey mode: never raise on shape inconsistencies; collect
        # them in ``violations`` and best-effort proceed.
        self.lax = lax

        # ── grouped state (see OutputState / MatrixState / VariantBuilderState) ──
        self.out = OutputState()
        self.mx = MatrixState()
        self.vb = VariantBuilderState()

        # ── tree-walk section state ──
        # Plumbing for feed_line() dispatch; not output, not per-matrix
        # data the cowalk helpers care about.
        self.section: Optional[str] = None
        self._saw_toolchain = False
        self._saw_matrix = False

    # ── back-compat: external callers read ``planner.violations`` ──
    @property
    def violations(self) -> list[dict]:
        return self.out.violations

    # ── lax-mode violation recording ──

    def _record(self, kind: str, **details) -> None:
        entry = {"kind": kind, **details}
        if self.mx.matrix_binary is not None:
            entry.setdefault("matrix", self.mx.matrix_binary)
        self.out.violations.append(entry)

    # ── public API ──

    def feed_line(self, line: str) -> None:
        depth, drv_hash, drv_name, is_backref = _parse_line(line)
        # Root line — no further work; the calling driver dispatches
        # the line index, we just need to stay consistent.
        if depth == 0:
            return

        # If we were inside a variant raw tree and depth drops back to
        # at or below 2 (the matrix-direct-child level), finalise.
        if self.vb.cur_root is not None and depth <= 2:
            self._finalise_current_variant()

        if depth == 1:
            self._on_depth1(drv_hash, drv_name, is_backref)
            return

        # Below depth 1: process per section.
        if self.section == "toolchain":
            if not is_backref:
                self.out.toolchain_drvs.add((drv_hash, drv_name))
            return

        if self.section and self.section.startswith("matrix:"):
            self._on_matrix_inner(depth, drv_hash, drv_name, is_backref)
            return

        # "other" section (e.g. bash builder ref) — ignore.

    def finalize(self) -> dict:
        """Drain the last variant and the last matrix's pending buffers."""
        if self.vb.cur_root is not None:
            self._finalise_current_variant()
        self._close_current_matrix()
        return {
            "templates": self.out.templates,
            "variant_arrays": self.out.variant_arrays,
            "placement": self.out.placement,
            "common_deps_per_arch_template": self.out.classifications,
            "toolchain_drvs": self.out.toolchain_drvs,
            "arch_indep_deps": self.out.arch_indep_deps,
            "stdenv_subtrees": self.out.stdenv_subtrees,
        }

    # ── depth-1 section transitions ──

    def _on_depth1(
        self, drv_hash: str, drv_name: str, is_backref: bool
    ) -> None:
        if _TOOLCHAINS_RE.match(drv_name):
            if self._saw_matrix:
                raise TreeWalkError(
                    f"toolchains.drv appeared after a matrix opened. "
                    f"The streaming planner relies on toolchains being "
                    f"the first depth-1 child (highest refcount). "
                    f"Ensure sum_drv.nix's mkWrapper includes the "
                    f"toolchains wrapper as a ref in every matrix."
                )
            self.section = "toolchain"
            self._saw_toolchain = True
            if not is_backref:
                self.out.toolchain_drvs.add((drv_hash, drv_name))
            return
        m = _MATRIX_RE.match(drv_name)
        if m is not None:
            if not self._saw_toolchain:
                raise TreeWalkError(
                    f"{drv_name} appeared before any toolchains.drv. "
                    f"Toolchains must sort first under the sum-root. "
                    f"See sum_drv.nix's mkWrapper for the matrix-side "
                    f"toolchains ref that drives the refcount sort."
                )
            self._close_current_matrix()
            self.mx = MatrixState(matrix_binary=m.group("binary"))
            self.section = f"matrix:{self.mx.matrix_binary}"
            self._saw_matrix = True
            self.out.arch_indep_deps.setdefault(self.mx.matrix_binary, set())
            return
        # Some other depth-1 (bash builder reference, etc.).
        self.section = "other"

    # ── matrix-inner depth handling ──

    def _on_matrix_inner(
        self,
        depth: int,
        drv_hash: str,
        drv_name: str,
        is_backref: bool,
    ) -> None:
        ident = (drv_hash, drv_name)
        if depth == 2:
            if drv_name.endswith(VARIANT_SUFFIX):
                if is_backref:
                    raise TreeWalkError(
                        f"variant entry-point {drv_name} at matrix depth 2 "
                        f"is a backref; each variant should occur exactly "
                        f"once in the tree"
                    )
                binary, arch, comp, opt = parse_variant_path(
                    drv_name, archs=self.archs
                )
                if binary != self.mx.matrix_binary:
                    raise TreeWalkError(
                        f"variant {drv_name!r} parses as binary={binary!r} "
                        f"but tree-walked under matrix-{self.mx.matrix_binary!r}"
                    )
                root = RawTreeNode(
                    hash=drv_hash, name=drv_name,
                    is_backref=False, depth=2,
                )
                self.vb.cur_root = root
                self.vb.cur_arch = arch
                self.vb.cur_label = f"{comp}-{opt}"
                self.vb.cur_drv = ident
                self.vb.cur_stack = [root]
                self.vb.cur_path_to_node = {ident: root}
            else:
                if not is_backref:
                    self.out.arch_indep_deps[self.mx.matrix_binary].add(ident)
            return

        # depth > 2: inside the current variant's subtree
        if self.vb.cur_root is None:
            raise TreeWalkError(
                f"depth-{depth} line under matrix-{self.mx.matrix_binary} "
                f"with no active variant subtree (ident={ident!r})"
            )
        while self.vb.cur_stack and self.vb.cur_stack[-1].depth >= depth:
            self.vb.cur_stack.pop()
        if not self.vb.cur_stack:
            raise TreeWalkError(
                f"raw-tree splice failed at depth {depth} for ident={ident!r}"
            )
        parent = self.vb.cur_stack[-1]
        existing = self.vb.cur_path_to_node.get(ident)
        if existing is not None:
            parent.children.append(existing)
            return
        node = RawTreeNode(
            hash=drv_hash, name=drv_name,
            is_backref=is_backref, depth=depth,
        )
        parent.children.append(node)
        self.vb.cur_path_to_node[ident] = node
        if not is_backref:
            self.vb.cur_stack.append(node)
            if ident not in self.out.toolchain_drvs:
                self.mx.unclassified_nodes.add(ident)

    # ── variant raw-tree completion ──

    def _finalise_current_variant(self) -> None:
        root = self.vb.cur_root
        arch = self.vb.cur_arch
        label = self.vb.cur_label
        assert root is not None and arch is not None
        # Reset buffer pointers BEFORE doing the heavy work so that
        # any recursive calls (shouldn't happen, but defence) don't
        # confuse state.
        self.vb.cur_root = None
        self.vb.cur_arch = None
        self.vb.cur_label = None
        self.vb.cur_drv = None
        self.vb.cur_stack = []
        self.vb.cur_path_to_node = {}
        if arch in self.mx.arch_template_id:
            # Template exists — stream-cowalk this variant immediately.
            tmpl_id = self.mx.arch_template_id[arch]
            self._cowalk_into_arr(tmpl_id, arch, label, root)
        else:
            self.mx.pending_raw_trees.setdefault(arch, []).append((label, root))
            if len(self.mx.pending_raw_trees[arch]) == 2:
                self._build_and_drain_arch(arch)

    # ── template construction from calibration pair ──

    def _build_and_drain_arch(self, arch: str) -> None:
        pair = self.mx.pending_raw_trees[arch]
        assert len(pair) == 2, (
            f"calibration pair must have exactly 2 raw trees for {arch}; "
            f"got {len(pair)}"
        )
        (label0, tree0), (label1, tree1) = pair
        self.vb.building_arch = arch
        self.vb.building_label_pair = (label0, label1)
        candidate = build_template(self, tree0, tree1, label0, label1)
        tmpl_id, _was_new = find_or_register_template(
            self.out.templates, candidate
        )
        self.mx.arch_template_id[arch] = tmpl_id
        template = self.out.templates[tmpl_id]
        arr = VariantArray(
            template_id=tmpl_id,
            arch=arch,
            variants=[],
            hashes=[[] for _ in template.nodes],
        )
        self.out.variant_arrays[(tmpl_id, arch)] = arr
        # Drain the calibration pair into the new VariantArray (these
        # are the only entries for THIS arch; other archs keep their
        # singletons buffered).
        self._cowalk_into_arr(tmpl_id, arch, label0, tree0, _arr=arr)
        self._cowalk_into_arr(tmpl_id, arch, label1, tree1, _arr=arr)
        self.mx.pending_raw_trees[arch] = []
        # Classify on the calibration pair (variants 0 and 1).
        self.out.classifications[(tmpl_id, arch)] = self._classify_pair(
            arr, template
        )

    # ── cowalk one raw tree into a VariantArray ──

    def _cowalk_into_arr(
        self,
        tmpl_id: int,
        arch: str,
        label: str,
        tree: RawTreeNode,
        *,
        _arr: Optional[VariantArray] = None,
    ) -> None:
        template = self.out.templates[tmpl_id]
        arr = (
            _arr if _arr is not None
            else self.out.variant_arrays[(tmpl_id, arch)]
        )
        v_pos = len(arr.variants)
        arr.variants.append(label)
        for row in arr.hashes:
            row.append(None)
        for n in template.nodes:
            n.visit_flag = False
        self.out.placement[tree.ident] = (tmpl_id, arch, v_pos)

        def _resolve_enforce(
            enf: tuple[str, Optional[str]],
        ) -> tuple[str, Optional[str]]:
            if enf[0] == "triple":
                target = _ARCH_TO_TRIPLE.get(arch)
                if enf[1] is not None and enf[1] == target:
                    return ("this-target", None)
            return enf

        def _split_dag_revisit(
            parent_nid: int,
            cid: int,
            observed_ident: tuple[str, str],
            diff: tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]],
        ) -> int:
            orig = template.nodes[cid]
            _diff_kind, stored_enf, observed_enf = diff
            if orig.enforce is None:
                orig.enforce = _resolve_enforce(stored_enf)
            new_node = TemplateNode(
                name=orig.name,
                child_ids=list(orig.child_ids),
                is_toolchain=orig.is_toolchain,
                visit_flag=True,
                optional=orig.optional,
                enforce=_resolve_enforce(observed_enf),
            )
            new_nid = len(template.nodes)
            template.nodes.append(new_node)
            # All variant_arrays of this template gain a row. Copy the
            # original's row so previously-cowalked variants (which
            # agreed on both DAG paths) keep their value for the new
            # node too — only divergent variants will overwrite later.
            for (tid_x, _ax), other_arr in self.out.variant_arrays.items():
                if tid_x == tmpl_id:
                    other_arr.hashes.append(list(other_arr.hashes[cid]))
            arr.hashes[new_nid][v_pos] = observed_ident
            parent = template.nodes[parent_nid]
            idx = parent.child_ids.index(cid)
            parent.child_ids[idx] = new_nid
            return new_nid

        def _visit_children(self_nid: int, t_node: RawTreeNode) -> None:
            node = template.nodes[self_nid]
            template_by_name: dict[str, list[int]] = {}
            for cid in node.child_ids:
                template_by_name.setdefault(
                    template.nodes[cid].name, []
                ).append(cid)
            actual_by_name: dict[str, list[RawTreeNode]] = {}
            for c in t_node.children:
                actual_by_name.setdefault(
                    self.name_extractor(c.name), []
                ).append(c)
            common = set(template_by_name) & set(actual_by_name)
            only_tmpl = set(template_by_name) - common
            only_actual = set(actual_by_name) - common
            promoted: list[str] = []
            for name in sorted(only_tmpl):
                for cid in template_by_name[name]:
                    if not template.nodes[cid].optional:
                        template.nodes[cid].optional = True
                        promoted.append(name)
            if promoted:
                self._record(
                    "cowalk-promoted-to-optional",
                    node_role=node.name,
                    label=label,
                    promoted_children=promoted,
                )
            if only_actual:
                extended: list[str] = []
                for name in sorted(only_actual):
                    for c in actual_by_name[name]:
                        self._extend_template_with_subtree(
                            tmpl_id, arch, v_pos, self_nid, c,
                        )
                    extended.append(name)
                self._record(
                    "cowalk-template-extended",
                    node_role=node.name,
                    label=label,
                    new_children=extended,
                )
            for name in sorted(common):
                cids = template_by_name[name]
                actuals = sorted(
                    actual_by_name[name], key=lambda r: r.ident
                )
                if len(actuals) != len(cids):
                    if not self.lax:
                        raise TemplateGraphAssertError(
                            kind="child-count-mismatch",
                            message=(
                                f"variant {label!r} at node #{self_nid} "
                                f"({node.name!r}): name {name!r} "
                                f"template={len(cids)} actual="
                                f"{len(actuals)}"
                            ),
                            template_built_from=template.template_built_from,
                            failing_variant=label,
                            node_name=node.name,
                        )
                    self._record(
                        "cowalk-child-count-mismatch",
                        node_role=node.name,
                        label=label,
                        child_name=name,
                        template_count=len(cids),
                        actual_count=len(actuals),
                    )
                n = min(len(cids), len(actuals))
                for cid, c in zip(cids[:n], actuals[:n]):
                    _visit(cid, c, self_nid)

        def _visit(
            nid: int,
            t_node: RawTreeNode,
            parent_nid: Optional[int],
        ) -> None:
            node = template.nodes[nid]
            if not t_node.is_backref:
                self.mx.unclassified_nodes.discard(t_node.ident)
            if node.visit_flag:
                if not node.is_toolchain:
                    stored = arr.hashes[nid][v_pos]
                    if stored is not None and stored != t_node.ident:
                        diff = _classify_revisit_diff(stored, t_node.ident)
                        if diff is not None and parent_nid is not None:
                            new_nid = _split_dag_revisit(
                                parent_nid, nid, t_node.ident, diff,
                            )
                            self._record(
                                "cowalk-dag-revisit-split",
                                node_role=node.name,
                                label=label,
                                diff_kind=diff[0],
                                stored=stored,
                                observed=t_node.ident,
                            )
                            if not t_node.is_backref:
                                _visit_children(new_nid, t_node)
                            return
                        if not self.lax:
                            raise TemplateGraphAssertError(
                                kind="dag-revisit-hash-mismatch",
                                message=(
                                    f"DAG revisit at node #{nid} "
                                    f"({node.name!r}) observed drv differs"
                                ),
                                template_built_from=template.template_built_from,
                                failing_variant=label,
                                node_name=node.name,
                                details={
                                    "stored": stored,
                                    "observed": t_node.ident,
                                },
                            )
                        self._record(
                            "cowalk-dag-revisit-hash-mismatch",
                            node_role=node.name,
                            label=label,
                            stored=stored,
                            observed=t_node.ident,
                        )
                return
            node.visit_flag = True
            if node.is_toolchain:
                if _is_stdenv_role(node.name):
                    self.out.stdenv_subtrees.setdefault(t_node.ident, {
                        "first_seen_in": {
                            "matrix": self.mx.matrix_binary,
                            "arch": arch,
                            "label": label,
                        },
                        "root": t_node,
                    })
                self._discard_subtree(t_node)
                return
            arr.hashes[nid][v_pos] = t_node.ident
            if t_node.is_backref:
                return
            _visit_children(nid, t_node)

        # Root pairing is by template.root_id ↔ raw tree root —
        # don't name-match here, because the variant entry-point's
        # extracted name embeds comp+opt and so differs across
        # variants of the same (binary, arch).
        _visit(template.root_id, tree, parent_nid=None)

    # ── template extension during cowalk ──

    def _extend_template_with_subtree(
        self,
        tmpl_id: int,
        arch: str,
        v_pos: int,
        parent_nid: int,
        raw_root: RawTreeNode,
    ) -> int:
        """Walk a variant's raw subtree the calibration pair didn't
        cover. Allocates optional template nodes (DAG-merging within
        this single walk), grows every ``VariantArray`` of this
        template, records the variant's hashes, then attaches the new
        subtree's root to ``parent_nid``'s ``child_ids``.
        """
        template = self.out.templates[tmpl_id]
        arr = self.out.variant_arrays[(tmpl_id, arch)]
        label = arr.variants[v_pos]
        local_path_to_nid: dict[tuple[str, str], int] = {}

        def _alloc(rn: RawTreeNode) -> tuple[int, bool]:
            if rn.ident in local_path_to_nid:
                return local_path_to_nid[rn.ident], False
            tnode = make_template_node(
                self, [rn], optional=True, label_slots=[label], arch=arch,
            )
            nid = len(template.nodes)
            template.nodes.append(tnode)
            local_path_to_nid[rn.ident] = nid
            # Every variant_array for this template gets a None row.
            for (tid_x, _ax), other_arr in self.out.variant_arrays.items():
                if tid_x == tmpl_id:
                    other_arr.hashes.append(
                        [None] * len(other_arr.variants)
                    )
            return nid, True

        def _record(nid: int, rn: RawTreeNode) -> None:
            if not template.nodes[nid].is_toolchain:
                arr.hashes[nid][v_pos] = rn.ident

        new_nid = walk_one_sided_subtree(
            self, raw_root, template, _alloc, post_fresh=_record
        )
        if new_nid not in template.nodes[parent_nid].child_ids:
            template.nodes[parent_nid].child_ids.append(new_nid)
        return new_nid

    # ── toolchain-child detection (for parent-level filtering) ──

    def _is_toolchain_child(self, raw_node: RawTreeNode) -> bool:
        """Classify a raw-tree child as toolchain *without* allocating a
        template node. Mirrors the is_toolchain logic at _alloc time."""
        name = self.name_extractor(raw_node.name)
        return (
            _is_stdenv_role(name)
            or _is_compiler_wrapper_role(name)
            or raw_node.ident in self.out.toolchain_drvs
        )

    # ── discard a raw subtree from unclassified ──

    def _discard_subtree(self, t_node: RawTreeNode) -> None:
        """When we determine a raw-tree node is toolchain-internal,
        every drv reachable from it is also toolchain-internal —
        anything not pre-listed in toolchain_drvs is just incidental
        build noise. Discard the whole subtree from unclassified."""
        stack = [t_node]
        while stack:
            n = stack.pop()
            if not n.is_backref:
                self.mx.unclassified_nodes.discard(n.ident)
            stack.extend(n.children)

    # ── classification on calibration pair ──

    def _classify_pair(
        self, arr: VariantArray, template: Template
    ) -> dict[int, str]:
        """At this point arr.hashes has exactly two columns (variants
        0 and 1). For each non-toolchain node: equal → common_dep,
        differing → variant_specific.

        Subsequent variants of this arch will be checked incrementally
        by ``_cowalk_into_arr`` via ``assert_classification_after_cowalk``.
        """
        out: dict[int, str] = {}
        for nid, node in enumerate(template.nodes):
            if node.is_toolchain:
                continue
            h0 = arr.hashes[nid][0]
            h1 = arr.hashes[nid][1] if len(arr.hashes[nid]) > 1 else None
            if h0 is None:
                # Variant 0 didn't reach this node during cowalk. If
                # not already optional, promote it now — same handling
                # the cowalk-time path uses for "required but absent
                # in some variant". Downstream merged render uses
                # whichever non-None hash exists.
                if not node.optional:
                    node.optional = True
                out[nid] = "common_dep"
                continue
            if h1 is None:
                # Single-variant calibration (rare; happens via
                # _close_current_matrix when an arch had only one
                # variant). Mark as common_dep.
                out[nid] = "common_dep"
                continue
            out[nid] = "common_dep" if h0 == h1 else "variant_specific"
        return out

    # ── end-of-matrix cleanup ──

    def _close_current_matrix(self) -> None:
        if self.mx.matrix_binary is None:
            return
        # Singletons: archs that only saw one variant. Build a
        # single-variant template from each.
        for arch, pending in list(self.mx.pending_raw_trees.items()):
            if not pending:
                continue
            if len(pending) >= 2:
                # Pair (and any extras would have been drained at the
                # moment the 2nd arrived). Safety net.
                self._build_and_drain_arch(arch)
            else:
                label, tree = pending[0]
                self.vb.building_arch = arch
                template = build_template_singleton(self, tree, label)
                tmpl_id, _ = find_or_register_template(
                    self.out.templates, template
                )
                self.mx.arch_template_id[arch] = tmpl_id
                arr = VariantArray(
                    template_id=tmpl_id,
                    arch=arch,
                    variants=[],
                    hashes=[[] for _ in self.out.templates[tmpl_id].nodes],
                )
                self.out.variant_arrays[(tmpl_id, arch)] = arr
                self._cowalk_into_arr(tmpl_id, arch, label, tree)
                self.out.classifications[(tmpl_id, arch)] = self._classify_pair(
                    arr, self.out.templates[tmpl_id]
                )
                self.mx.pending_raw_trees[arch] = []
        if self.mx.unclassified_nodes:
            sample = sorted(self.mx.unclassified_nodes)[:5]
            if not self.lax:
                raise TreeWalkError(
                    f"matrix-{self.mx.matrix_binary} ended with "
                    f"{len(self.mx.unclassified_nodes)} drvs still in "
                    f"unclassified_nodes — algorithm gap. "
                    f"First 5: {sample}"
                )
            self._record(
                "unclassified-at-matrix-end",
                count=len(self.mx.unclassified_nodes),
                sample=sample,
            )
            self.mx.unclassified_nodes = set()

# ── convenience entry point ──


_ARCH_FAMILIES: dict[str, str] = {
    "x86_64": "x86",
    "i686": "x86",
    "aarch64": "arm",
    "armv7l-hf": "arm",
    "armv7l-sf": "arm",
    "mips64el": "mips",
    "mipsel": "mips",
    "ppc32": "power",
    "ppc64": "power",
    "riscv64": "riscv",
}


def _classify_cross_arch_sharing(
    arch_to_drv: dict[str, str],
) -> str:
    """Returns one of 'A','B','C','D'. See merge_binary_to_dot doc."""
    # Single-arch presence is by definition not "shared across archs",
    # even though the unique-set is trivially of size 1. Treat as A.
    if len(arch_to_drv) <= 1:
        return "A"
    unique = set(arch_to_drv.values())
    if len(unique) == 1:
        return "D"
    if len(unique) == len(arch_to_drv):
        return "A"
    # Each drv → set of archs that have it.
    by_drv: dict[str, frozenset[str]] = {}
    for arch, drv in arch_to_drv.items():
        by_drv.setdefault(drv, set()).add(arch)
    by_drv = {d: frozenset(a) for d, a in by_drv.items()}
    # For each drv's arch-set, is it exactly one whole family
    # (or a single arch from a family with no other family members
    # present)? "Different per family" = every group is a single
    # complete family. Anything more interleaved = "mixed".
    all_families_present: dict[str, set[str]] = {}
    for arch in arch_to_drv:
        fam = _ARCH_FAMILIES.get(arch, "other")
        all_families_present.setdefault(fam, set()).add(arch)
    family_clean = True
    for archs in by_drv.values():
        fams = {_ARCH_FAMILIES.get(a, "other") for a in archs}
        if len(fams) > 1:
            family_clean = False
            break
        # All archs in this group come from one family. Check that
        # ALL archs from that family which are present in our
        # observation set are in this group — otherwise the family
        # is split across drvs (mixed).
        fam = next(iter(fams))
        if archs != all_families_present[fam]:
            family_clean = False
            break
    return "B" if family_clean else "C"


def plan_from_tree_streaming(
    tree_text: str,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
    name_extractor=drv_role,
    logger=None,
    lax: bool = False,
) -> dict:
    planner = StreamPlanner(
        archs=archs,
        name_extractor=name_extractor,
        logger=logger,
        lax=lax,
    )
    for line in tree_text.splitlines():
        planner.feed_line(line)
    result = planner.finalize()
    result["violations"] = planner.violations
    return result

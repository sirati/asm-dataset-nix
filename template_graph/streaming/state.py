"""State dataclasses and the ``StreamPlanner`` class.

``OutputState``, ``MatrixState``, ``VariantBuilderState`` are the
typed groupings of planner state introduced in Phase 3.2. ``RawTreeNode``
is the in-memory per-variant raw-tree node.

``StreamPlanner`` owns these state objects plus a few small helper
methods (``_record``, ``_cowalk_into_arr``, ``_extend_template_with_subtree``,
``_is_toolchain_child``, ``_discard_subtree``) that the cowalk module
imports back from this module via ``planner.<method>(...)``. The bulk
parser-dispatch and finalization logic lives in
``template_graph.streaming.dispatch`` and
``template_graph.streaming.finalize`` as free functions; the methods
here delegate into those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from template_graph.cowalk import make_template_node, walk_one_sided_subtree
from template_graph.graph import Template, VariantArray
from template_graph.parser.role import (
    _is_compiler_wrapper_role,
    _is_stdenv_role,
    drv_role,
)
from template_graph.tree_walker import DEFAULT_ARCHS


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
# Phase 3.3 moved cowalk helpers out of the planner; they take typed
# handles to these state groups rather than the planner itself.


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
    pending_raw_trees: dict[str, list[tuple[str, RawTreeNode]]] = field(
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
    cur_root: Optional[RawTreeNode] = None
    cur_arch: Optional[str] = None
    cur_label: Optional[str] = None
    cur_drv: Optional[tuple[str, str]] = None
    cur_stack: list[RawTreeNode] = field(default_factory=list)
    # ``(hash, name)`` → RawTreeNode within the variant currently being
    # built. Collapses re-encountered idents onto the existing node
    # (DAG via nix's [...] back-refs).
    cur_path_to_node: dict[tuple[str, str], RawTreeNode] = field(
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

    # ── public API: thin delegates to dispatch/finalize free funcs ──

    def feed_line(self, line: str) -> None:
        from template_graph.streaming.dispatch import feed_line
        feed_line(self, line)

    def finalize(self) -> dict:
        """Drain the last variant and the last matrix's pending buffers."""
        from template_graph.streaming.finalize import finalize
        return finalize(self)

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
        """Cowalk one variant raw tree into this template's variant
        array. Thin shim — implementation lives in
        ``template_graph.cowalk.cowalk_variant.cowalk_into_arr``."""
        # Local import: top-level would re-introduce the streaming ->
        # cowalk -> streaming import chain (state.py is imported by
        # cowalk via TYPE_CHECKING).
        from template_graph.cowalk import cowalk_into_arr
        cowalk_into_arr(
            self, tmpl_id, arch, label, tree, arr=_arr,
        )

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

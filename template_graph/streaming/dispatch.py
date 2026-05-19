"""Per-line walk dispatch for ``StreamPlanner``.

Free functions taking a ``StreamPlanner`` handle. Implements the
``feed_line`` entry point plus the depth-1 (toolchain/matrix
section transitions) and matrix-inner (variant entry-points and
the per-variant raw-tree builder) dispatch.

Variant finalization (``_finalise_current_variant``) decides whether
the freshly-completed variant should drain into an existing
calibrated template (cowalk) or buffer waiting for its calibration
pair (in which case the second arrival here triggers the build
inside ``template_graph.streaming.finalize``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from template_graph.tree_walker import (
    _MATRIX_RE,
    _TOOLCHAINS_RE,
    VARIANT_SUFFIX,
    TreeWalkError,
    _parse_line,
    parse_variant_path,
)

from template_graph.streaming.state import MatrixState, RawTreeNode

if TYPE_CHECKING:
    from template_graph.streaming.state import StreamPlanner


def feed_line(planner: "StreamPlanner", line: str) -> None:
    depth, drv_hash, drv_name, is_backref = _parse_line(line)
    # Root line — no further work; the calling driver dispatches
    # the line index, we just need to stay consistent.
    if depth == 0:
        return

    # If we were inside a variant raw tree and depth drops back to
    # at or below 2 (the matrix-direct-child level), finalise.
    if planner.vb.cur_root is not None and depth <= 2:
        _finalise_current_variant(planner)

    if depth == 1:
        _on_depth1(planner, drv_hash, drv_name, is_backref)
        return

    # Below depth 1: process per section.
    if planner.section == "toolchain":
        if not is_backref:
            planner.out.toolchain_drvs.add((drv_hash, drv_name))
        return

    if planner.section and planner.section.startswith("matrix:"):
        _on_matrix_inner(planner, depth, drv_hash, drv_name, is_backref)
        return

    # "other" section (e.g. bash builder ref) — ignore.


# ── depth-1 section transitions ──


def _on_depth1(
    planner: "StreamPlanner",
    drv_hash: str,
    drv_name: str,
    is_backref: bool,
) -> None:
    if _TOOLCHAINS_RE.match(drv_name):
        if planner._saw_matrix:
            raise TreeWalkError(
                f"toolchains.drv appeared after a matrix opened. "
                f"The streaming planner relies on toolchains being "
                f"the first depth-1 child (highest refcount). "
                f"Ensure sum_drv.nix's mkWrapper includes the "
                f"toolchains wrapper as a ref in every matrix."
            )
        planner.section = "toolchain"
        planner._saw_toolchain = True
        if not is_backref:
            planner.out.toolchain_drvs.add((drv_hash, drv_name))
        return
    m = _MATRIX_RE.match(drv_name)
    if m is not None:
        if not planner._saw_toolchain:
            raise TreeWalkError(
                f"{drv_name} appeared before any toolchains.drv. "
                f"Toolchains must sort first under the sum-root. "
                f"See sum_drv.nix's mkWrapper for the matrix-side "
                f"toolchains ref that drives the refcount sort."
            )
        from template_graph.streaming.finalize import _close_current_matrix
        _close_current_matrix(planner)
        planner.mx = MatrixState(matrix_binary=m.group("binary"))
        planner.section = f"matrix:{planner.mx.matrix_binary}"
        planner._saw_matrix = True
        planner.out.arch_indep_deps.setdefault(planner.mx.matrix_binary, set())
        return
    # Some other depth-1 (bash builder reference, etc.).
    planner.section = "other"


# ── matrix-inner depth handling ──


def _on_matrix_inner(
    planner: "StreamPlanner",
    depth: int,
    drv_hash: str,
    drv_name: str,
    is_backref: bool,
) -> None:
    """Thin dispatcher: depth-2 is variant entry / arch-indep dep,
    depth>2 is raw-tree splice into the current variant builder."""
    ident = (drv_hash, drv_name)
    if depth == 2:
        _on_matrix_depth2(planner, ident, drv_hash, drv_name, is_backref)
        return
    _on_matrix_depth_inner(planner, depth, ident, drv_hash, drv_name, is_backref)


def _on_matrix_depth2(
    planner: "StreamPlanner",
    ident: tuple[str, str],
    drv_hash: str,
    drv_name: str,
    is_backref: bool,
) -> None:
    """Matrix depth-2 line: either a variant entry-point (opens a new
    raw-tree buffer) or an arch-indep dep (added to per-binary set)."""
    if drv_name.endswith(VARIANT_SUFFIX):
        if is_backref:
            raise TreeWalkError(
                f"variant entry-point {drv_name} at matrix depth 2 "
                f"is a backref; each variant should occur exactly "
                f"once in the tree"
            )
        binary, arch, _comp, _opt = parse_variant_path(
            drv_name, archs=planner.archs
        )
        if binary != planner.mx.matrix_binary:
            raise TreeWalkError(
                f"variant {drv_name!r} parses as binary={binary!r} "
                f"but tree-walked under matrix-{planner.mx.matrix_binary!r}"
            )
        # Full-suffix label matching the matrix_eval sidecar
        # (``<binary>__<arch>__<comp>-<opt>-<flag>-<hardening>-san-<san>-march-<march>``).
        # Strips ``<binary>-<arch>-`` prefix and ``-elf-folder.drv``
        # suffix from drv_name, then composes with ``__`` separators
        # the way ``mkVariant`` does. Required so per-cell variants
        # with the same (comp, opt) but different inner-axes (when
        # ``matrix_eval`` samples > 1 per cell) are individually
        # identifiable in ``variant_lookup`` for descriptor emission.
        suffix = drv_name[
            len(binary) + 1 + len(arch) + 1 : -len(VARIANT_SUFFIX)
        ]
        root = RawTreeNode(
            hash=drv_hash, name=drv_name,
            is_backref=False, depth=2,
        )
        planner.vb.cur_root = root
        planner.vb.cur_arch = arch
        planner.vb.cur_label = f"{binary}__{arch}__{suffix}"
        planner.vb.cur_drv = ident
        planner.vb.cur_stack = [root]
        planner.vb.cur_path_to_node = {ident: root}
        return
    if not is_backref:
        planner.out.arch_indep_deps[planner.mx.matrix_binary].add(ident)


def _on_matrix_depth_inner(
    planner: "StreamPlanner",
    depth: int,
    ident: tuple[str, str],
    drv_hash: str,
    drv_name: str,
    is_backref: bool,
) -> None:
    """Matrix depth>2 line: splice into the active variant's raw subtree.
    Pops the stack to the parent of this depth, then attaches the
    existing DAG node (backref-collapse) or allocates a new RawTreeNode."""
    if planner.vb.cur_root is None:
        raise TreeWalkError(
            f"depth-{depth} line under matrix-{planner.mx.matrix_binary} "
            f"with no active variant subtree (ident={ident!r})"
        )
    while planner.vb.cur_stack and planner.vb.cur_stack[-1].depth >= depth:
        planner.vb.cur_stack.pop()
    if not planner.vb.cur_stack:
        raise TreeWalkError(
            f"raw-tree splice failed at depth {depth} for ident={ident!r}"
        )
    parent = planner.vb.cur_stack[-1]
    existing = planner.vb.cur_path_to_node.get(ident)
    if existing is not None:
        parent.children.append(existing)
        return
    node = RawTreeNode(
        hash=drv_hash, name=drv_name,
        is_backref=is_backref, depth=depth,
    )
    parent.children.append(node)
    planner.vb.cur_path_to_node[ident] = node
    if not is_backref:
        planner.vb.cur_stack.append(node)
        if ident not in planner.out.toolchain_drvs:
            planner.mx.unclassified_nodes.add(ident)


# ── variant raw-tree completion ──


def _finalise_current_variant(planner: "StreamPlanner") -> None:
    root = planner.vb.cur_root
    arch = planner.vb.cur_arch
    label = planner.vb.cur_label
    assert root is not None and arch is not None
    # Reset buffer pointers BEFORE doing the heavy work so that
    # any recursive calls (shouldn't happen, but defence) don't
    # confuse state.
    planner.vb.cur_root = None
    planner.vb.cur_arch = None
    planner.vb.cur_label = None
    planner.vb.cur_drv = None
    planner.vb.cur_stack = []
    planner.vb.cur_path_to_node = {}
    if arch in planner.mx.arch_template_id:
        # Template exists — stream-cowalk this variant immediately.
        tmpl_id = planner.mx.arch_template_id[arch]
        planner._cowalk_into_arr(tmpl_id, arch, label, root)
    else:
        planner.mx.pending_raw_trees.setdefault(arch, []).append((label, root))
        if len(planner.mx.pending_raw_trees[arch]) == 2:
            from template_graph.streaming.finalize import _build_and_drain_arch
            _build_and_drain_arch(planner, arch)

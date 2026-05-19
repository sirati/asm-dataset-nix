"""Terminal aggregation and template construction for ``StreamPlanner``.

Free functions taking a ``StreamPlanner`` handle. Implements the
public ``finalize()`` (the post-pass that drains in-flight state and
returns the result dict), ``_build_and_drain_arch`` (template
construction from a calibration pair), and ``_close_current_matrix``
(end-of-matrix singleton resolution + unclassified-nodes assertion).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from template_graph.cowalk import (
    _classify_pair,
    build_meta_templates,
    build_template,
    build_template_singleton,
)
from template_graph.graph import (
    MetaTemplate,
    VariantArray,
    find_or_register_template,
)
from template_graph.tree_walker import TreeWalkError

if TYPE_CHECKING:
    from template_graph.streaming.state import StreamPlanner


def finalize(planner: "StreamPlanner") -> dict:
    """Drain the last variant and the last matrix's pending buffers."""
    if planner.vb.cur_root is not None:
        from template_graph.streaming.dispatch import _finalise_current_variant
        _finalise_current_variant(planner)
    _close_current_matrix(planner)
    meta_templates_by_binary = _build_meta_templates_post_pass(planner)
    planner.out.toolchain_node_ids_per_template = (
        _build_toolchain_node_ids_post_pass(planner)
    )
    return {
        "templates": planner.out.templates,
        "variant_arrays": planner.out.variant_arrays,
        "placement": planner.out.placement,
        "common_deps_per_arch_template": planner.out.classifications,
        "toolchain_drvs": planner.out.toolchain_drvs,
        "arch_indep_deps": planner.out.arch_indep_deps,
        "stdenv_subtrees": planner.out.stdenv_subtrees,
        "meta_templates": meta_templates_by_binary,
        "toolchain_node_ids_per_template": (
            planner.out.toolchain_node_ids_per_template
        ),
    }


def _build_toolchain_node_ids_post_pass(
    planner: "StreamPlanner",
) -> dict[int, list[int]]:
    """For each template, list the node_ids whose ``is_toolchain`` is set.

    ``dependency_graph_planner`` needs this mapping to wire toolchain
    task ids into per-variant dependency sets: the cowalk short-
    circuits toolchain subtrees in ``walk_one_sided_subtree`` /
    ``_walk_pair_node``, so ``arr.hashes`` rows at toolchain node_ids
    stay empty. Without this map the consumer would have no way to
    identify which template positions are toolchains.
    """
    return {
        tid: [
            nid for nid, node in enumerate(t.nodes) if node.is_toolchain
        ]
        for tid, t in enumerate(planner.out.templates)
    }


def _build_meta_templates_post_pass(
    planner: "StreamPlanner",
) -> dict[str, list[MetaTemplate]]:
    """Run :func:`build_meta_templates` once per binary the planner saw.

    The binary set is taken from ``out.arch_indep_deps`` keys —
    ``dispatch._on_depth1`` initialises an entry there for every matrix
    wrapper, so this set covers every binary regardless of whether the
    binary surfaced any arch-indep deps. Binaries whose per-arch cells
    all dropped out (e.g. nothing matched the ``elf-folder`` root role)
    map to an empty list.
    """
    return {
        binary: build_meta_templates(planner.out, binary)
        for binary in planner.out.arch_indep_deps
    }


# ── template construction from calibration pair ──


def _build_and_drain_arch(planner: "StreamPlanner", arch: str) -> None:
    pair = planner.mx.pending_raw_trees[arch]
    assert len(pair) == 2, (
        f"calibration pair must have exactly 2 raw trees for {arch}; "
        f"got {len(pair)}"
    )
    (label0, tree0), (label1, tree1) = pair
    planner.vb.building_arch = arch
    planner.vb.building_label_pair = (label0, label1)
    candidate = build_template(planner, tree0, tree1, label0, label1)
    tmpl_id, _was_new = find_or_register_template(
        planner.out.templates, candidate
    )
    planner.mx.arch_template_id[arch] = tmpl_id
    template = planner.out.templates[tmpl_id]
    arr = VariantArray(
        template_id=tmpl_id,
        arch=arch,
        variants=[],
        hashes=[[] for _ in template.nodes],
    )
    planner.out.variant_arrays[(tmpl_id, arch)] = arr
    # Drain the calibration pair into the new VariantArray (these
    # are the only entries for THIS arch; other archs keep their
    # singletons buffered).
    planner._cowalk_into_arr(tmpl_id, arch, label0, tree0, _arr=arr)
    planner._cowalk_into_arr(tmpl_id, arch, label1, tree1, _arr=arr)
    planner.mx.pending_raw_trees[arch] = []
    # Classify on the calibration pair (variants 0 and 1).
    planner.out.classifications[(tmpl_id, arch)] = _classify_pair(
        arr, template
    )


# ── end-of-matrix cleanup ──


def _close_current_matrix(planner: "StreamPlanner") -> None:
    if planner.mx.matrix_binary is None:
        return
    # Singletons: archs that only saw one variant. Build a
    # single-variant template from each.
    for arch, pending in list(planner.mx.pending_raw_trees.items()):
        if not pending:
            continue
        if len(pending) >= 2:
            # Pair (and any extras would have been drained at the
            # moment the 2nd arrived). Safety net.
            _build_and_drain_arch(planner, arch)
        else:
            label, tree = pending[0]
            planner.vb.building_arch = arch
            template = build_template_singleton(planner, tree, label)
            tmpl_id, _ = find_or_register_template(
                planner.out.templates, template
            )
            planner.mx.arch_template_id[arch] = tmpl_id
            arr = VariantArray(
                template_id=tmpl_id,
                arch=arch,
                variants=[],
                hashes=[[] for _ in planner.out.templates[tmpl_id].nodes],
            )
            planner.out.variant_arrays[(tmpl_id, arch)] = arr
            planner._cowalk_into_arr(tmpl_id, arch, label, tree)
            planner.out.classifications[(tmpl_id, arch)] = _classify_pair(
                arr, planner.out.templates[tmpl_id]
            )
            planner.mx.pending_raw_trees[arch] = []
    if planner.mx.unclassified_nodes:
        sample = sorted(planner.mx.unclassified_nodes)[:5]
        if not planner.lax:
            raise TreeWalkError(
                f"matrix-{planner.mx.matrix_binary} ended with "
                f"{len(planner.mx.unclassified_nodes)} drvs still in "
                f"unclassified_nodes — algorithm gap. "
                f"First 5: {sample}"
            )
        planner._record(
            "unclassified-at-matrix-end",
            count=len(planner.mx.unclassified_nodes),
            sample=sample,
        )
        planner.mx.unclassified_nodes = set()

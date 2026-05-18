"""Cowalk-time template-shape tests.

Exercises:
  * the DAG-revisit split via ``enforce.triple`` (``_split_dag_revisit``
    in ``streaming.py``),
  * the one-sided child promotion path (``_walk_single`` in
    ``_build_template``).

All inputs synthetic; no nix subprocess.
"""

from __future__ import annotations

from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


def test_dag_revisit_split_via_enforce_triple():
    """A template node reachable from two parents (DAG join) gets a
    revisit during cowalk; if a later variant has TWO DIFFERENT
    actual drvs at the two DAG positions whose names differ ONLY
    in the embedded target-triple, ``_split_dag_revisit`` clones
    the template node and tags each side with ``enforce=('triple',
    ...)``.

    Construction:
      * Calibration pair (O0, O1): each variant references the SAME
        gcc-runtime drv (same hash) under BOTH parents alpha and
        beta. nix-store renders the second visit as a backref, so
        the planner's RawTree DAG-joins them. The calibration's
        ``_build_template`` produces ONE shared template node for
        gcc-runtime, child of both alpha and beta.
      * Divergent variant (O2): under parent alpha references
        ``gcc-runtime-14.0.drv`` (no triple, native baseline); under
        parent beta references
        ``gcc-runtime-aarch64-unknown-linux-gnu-14.0.drv`` (same
        version, different triple). The cowalk sees stored != observed
        on the second DAG visit, classifies the diff as 'triple',
        splits the node.
    """
    def calibration_variant(seed: int, opt: str) -> Node:
        # Same gcc-runtime ident under both parents → DAG join.
        gcc_runtime = Node(hash=make_hash(700),
                           name="gcc-runtime-14.0.drv")
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp="gcc15", opt=opt,
            children=[
                Node(hash=make_hash(seed + 1),
                     name="alpha-1.0.drv",
                     children=[gcc_runtime]),
                Node(hash=make_hash(seed + 2),
                     name="beta-1.0.drv",
                     children=[gcc_runtime]),
            ],
        )

    # Third variant: alpha→native, beta→aarch64-triple version.
    def divergent_variant(seed: int, opt: str) -> Node:
        gcc_native = Node(hash=make_hash(800),
                          name="gcc-runtime-14.0.drv")
        gcc_aarch64 = Node(
            hash=make_hash(801),
            name="gcc-runtime-aarch64-unknown-linux-gnu-14.0.drv",
        )
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp="gcc15", opt=opt,
            children=[
                Node(hash=make_hash(seed + 1),
                     name="alpha-1.0.drv",
                     children=[gcc_native]),
                Node(hash=make_hash(seed + 2),
                     name="beta-1.0.drv",
                     children=[gcc_aarch64]),
            ],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    calibration_variant(100, "O0"),
                    calibration_variant(200, "O1"),
                    divergent_variant(300, "O2"),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    tmpl = result["templates"][0]
    # After the split, there should be TWO template nodes named
    # "gcc-runtime.drv" (the original + the split clone).
    gcc_nodes = [
        (nid, n) for nid, n in enumerate(tmpl.nodes)
        if n.name == "gcc-runtime.drv"
    ]
    assert len(gcc_nodes) == 2, (
        f"expected DAG-revisit to split gcc-runtime template node, "
        f"got {len(gcc_nodes)} gcc-runtime nodes"
    )
    # At least one of the two carries an enforce tag of kind 'triple'
    # or 'this-target' (the latter when the divergent triple matches
    # the cowalking arch's canonical triple).
    enforces = [n.enforce for _, n in gcc_nodes]
    assert any(
        e is not None and e[0] in ("triple", "this-target")
        for e in enforces
    ), (
        f"expected at least one enforce tag of kind triple/this-target, "
        f"got {enforces}"
    )

    # The split was recorded in violations under the descriptive kind.
    split_records = [
        v for v in planner.violations
        if v.get("kind") == "cowalk-dag-revisit-split"
    ]
    assert split_records, (
        f"expected cowalk-dag-revisit-split violation record; got "
        f"{[v.get('kind') for v in planner.violations]}"
    )


def test_one_sided_child_promotion():
    """Variant 0 has a child node that variant 1 lacks. The
    calibration's ``_walk_single`` path allocates an *optional*
    template node for the one-sided child.
    """
    def variant_with_extra(seed: int, opt: str, with_extra: bool) -> Node:
        kids = [
            Node(hash=make_hash(seed + 1), name="lib-common-1.0.drv"),
        ]
        if with_extra:
            kids.append(
                Node(hash=make_hash(seed + 2), name="lib-extra-1.0.drv")
            )
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp="gcc15", opt=opt, children=kids,
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1000), name="toolchains.drv"),
            Node(
                hash=make_hash(2000), name="matrix-hello.drv",
                children=[
                    variant_with_extra(100, "O0", with_extra=True),
                    variant_with_extra(200, "O1", with_extra=False),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    tmpl = result["templates"][0]
    extra_nids = [
        nid for nid, n in enumerate(tmpl.nodes)
        if n.name == "lib-extra.drv"
    ]
    assert len(extra_nids) == 1, (
        f"expected one optional template node for the one-sided extra "
        f"child; got {len(extra_nids)}"
    )
    assert tmpl.nodes[extra_nids[0]].optional, (
        "one-sided calibration child must be marked optional"
    )

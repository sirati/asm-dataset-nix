"""Cross-arch ``MetaTemplate`` construction + DOT consumption tests.

Phase 4.3 wiring: ``build_meta_templates`` returns one MetaTemplate
per binary (role-merged across archs), ``StreamPlanner.finalize``
populates ``result["meta_templates"]``, and ``merge_binary_to_dot``
consumes the MetaTemplate-derived A/B/C/D letter (no inline classifier
call). All inputs are synthetic ``nix-store --query --tree`` line
buffers built via ``fixtures.py``; no nix subprocess.
"""

from __future__ import annotations

from template_graph.cowalk import build_meta_templates
from template_graph.dot.merge_binary import merge_binary_to_dot
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


def _two_arch_shared_dep_tree(shared_hash: str) -> Node:
    """Two archs, two variants each, all referencing the same
    ``libfoo`` ident → cross-arch + cross-variant common dep (class D).
    """
    def variant(arch: str, seed: int, opt: str) -> Node:
        return simple_variant(
            "hello", arch, seed_base=seed, comp="gcc15", opt=opt,
            children=[Node(hash=shared_hash, name="libfoo-1.0.drv")],
        )

    return Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant("x86_64", 10, "O0"),
                    variant("x86_64", 20, "O1"),
                    variant("aarch64", 30, "O0"),
                    variant("aarch64", 40, "O1"),
                ],
            ),
        ],
    )


def _two_arch_per_arch_dep_tree(
    x86_hash: str, aarch_hash: str,
) -> Node:
    """Two archs, two variants each. Distinct libfoo idents per arch
    → class A (uni_arch_common_dep).
    """
    def variant(arch: str, seed: int, opt: str, lib_hash: str) -> Node:
        return simple_variant(
            "hello", arch, seed_base=seed, comp="gcc15", opt=opt,
            children=[Node(hash=lib_hash, name="libfoo-1.0.drv")],
        )

    return Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant("x86_64", 10, "O0", x86_hash),
                    variant("x86_64", 20, "O1", x86_hash),
                    variant("aarch64", 30, "O0", aarch_hash),
                    variant("aarch64", 40, "O1", aarch_hash),
                ],
            ),
        ],
    )


def test_build_meta_templates_non_empty_for_multi_arch_binary():
    """Cross-arch shared dep produces one role-merged MetaTemplate.

    Reproduces the Phase 4.3 design fix: pre-fix grouping by literal
    template_id returned ``[]`` here (each arch's root role embeds the
    arch axis, so no template_id is shared); post-fix returns one
    role-merged MetaTemplate covering both archs.
    """
    shared = make_hash(900)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_shared_dep_tree(shared)))
    planner.finalize()

    metas = build_meta_templates(planner.out, "hello")
    assert len(metas) == 1
    mt = metas[0]
    assert set(mt.template_id_per_arch) == {"x86_64", "aarch64"}
    # Per-arch template ids distinct (root role embeds arch).
    assert (
        mt.template_id_per_arch["x86_64"]
        != mt.template_id_per_arch["aarch64"]
    )
    assert mt.role_at_node[0] == "hello-elf-folder.drv"
    assert "libfoo.drv" in mt.role_at_node
    libfoo_i = mt.role_at_node.index("libfoo.drv")
    assert mt.cross_arch_classification[libfoo_i] == "cross_arch_common_dep"
    assert mt.class_letter_at_node[libfoo_i] == "D"
    drv = mt.drv_per_node[libfoo_i]
    assert drv == (shared.encode("ascii"), "libfoo-1.0.drv")


def test_build_meta_templates_class_a_per_arch_distinct():
    """Distinct per-arch idents → class A → uni_arch_common_dep with
    ``drv_per_node`` shape ``{arch: ident}``.
    """
    x86 = make_hash(901)
    aar = make_hash(902)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_per_arch_dep_tree(x86, aar)))
    planner.finalize()

    metas = build_meta_templates(planner.out, "hello")
    assert len(metas) == 1
    mt = metas[0]
    libfoo_i = mt.role_at_node.index("libfoo.drv")
    assert mt.cross_arch_classification[libfoo_i] == "uni_arch_common_dep"
    assert mt.class_letter_at_node[libfoo_i] == "A"
    drv = mt.drv_per_node[libfoo_i]
    assert isinstance(drv, dict)
    assert drv == {
        "x86_64": (x86.encode("ascii"), "libfoo-1.0.drv"),
        "aarch64": (aar.encode("ascii"), "libfoo-1.0.drv"),
    }


def test_build_meta_templates_empty_for_unknown_binary():
    """No per-arch root resolves to a binary name → empty list."""
    shared = make_hash(900)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_shared_dep_tree(shared)))
    planner.finalize()
    assert build_meta_templates(planner.out, "nonexistent") == []


def test_finalize_populates_meta_templates_per_binary():
    """``StreamPlanner.finalize`` adds ``meta_templates`` to its dict,
    keyed by binary."""
    shared = make_hash(900)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_shared_dep_tree(shared)))
    result = planner.finalize()
    assert "meta_templates" in result
    assert set(result["meta_templates"]) == {"hello"}
    metas = result["meta_templates"]["hello"]
    assert len(metas) == 1
    assert "libfoo.drv" in metas[0].role_at_node


def test_merge_binary_to_dot_byte_equal_snapshot():
    """DOT output byte-equal to a fixed snapshot for the canonical
    2-arch shared-dep fixture. Regression test on the merge_binary
    refactor: the MetaTemplate-driven letter lookup must produce the
    same DOT as the pre-refactor inline-classifier implementation.
    """
    shared = make_hash(900)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_shared_dep_tree(shared)))
    result = planner.finalize()
    dot = merge_binary_to_dot(result, "hello")
    expected = (
        'digraph "merged_hello" {\n'
        '  rankdir=LR;\n'
        '  node [shape=box, style=filled, fontname=monospace];\n'
        '  n0 [label="hello-elf-folder.drv", fillcolor=lightcoral, '
        'style="filled"];\n'
        '  n1 [label="libfoo.drv [D]", fillcolor=palegreen, '
        'style="filled"];\n'
        '  n0 -> n1;\n'
        '}'
    )
    assert dot == expected, (
        f"DOT drift:\n--- expected ---\n{expected}\n"
        f"--- actual ---\n{dot}"
    )


def test_merge_binary_to_dot_falls_back_without_meta_templates():
    """Without ``meta_templates`` (legacy caller), common-dep nodes
    render white with no sharing tag rather than ``palegreen [D]``.
    """
    shared = make_hash(900)
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_arch_shared_dep_tree(shared)))
    result = planner.finalize()
    result.pop("meta_templates")
    dot = merge_binary_to_dot(result, "hello")
    assert 'label="libfoo.drv"' in dot, dot
    assert "palegreen" not in dot
    assert "[D]" not in dot


def test_meta_template_enforce_at_node_captures_dag_split_constraints():
    """DAG-revisit splits create two template nodes sharing a role
    name but differing in ``enforce``. ``enforce_at_node`` must
    surface that constraint so consumers can disambiguate by
    ``(role, enforce)`` Key.
    """
    def calibration(seed: int, opt: str) -> Node:
        shared_rt = Node(
            hash=make_hash(700), name="gcc-runtime-14.0.drv",
        )
        return simple_variant(
            "hello", "x86_64", seed_base=seed, comp="gcc15", opt=opt,
            children=[
                Node(hash=make_hash(seed + 1), name="alpha-1.0.drv",
                     children=[shared_rt]),
                Node(hash=make_hash(seed + 2), name="beta-1.0.drv",
                     children=[shared_rt]),
            ],
        )

    def divergent(seed: int, opt: str) -> Node:
        rt_native = Node(
            hash=make_hash(800), name="gcc-runtime-14.0.drv",
        )
        rt_aarch = Node(
            hash=make_hash(801),
            name="gcc-runtime-aarch64-unknown-linux-gnu-14.0.drv",
        )
        return simple_variant(
            "hello", "x86_64", seed_base=seed, comp="gcc15", opt=opt,
            children=[
                Node(hash=make_hash(seed + 1), name="alpha-1.0.drv",
                     children=[rt_native]),
                Node(hash=make_hash(seed + 2), name="beta-1.0.drv",
                     children=[rt_aarch]),
            ],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    calibration(100, "O0"),
                    calibration(200, "O1"),
                    divergent(300, "O2"),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    metas = result["meta_templates"]["hello"]
    assert len(metas) == 1
    mt = metas[0]
    rt_positions = [
        i for i, r in enumerate(mt.role_at_node)
        if r == "gcc-runtime.drv"
    ]
    assert len(rt_positions) == 2, rt_positions
    enforces = {mt.enforce_at_node[i] for i in rt_positions}
    assert len(enforces) == 2, enforces

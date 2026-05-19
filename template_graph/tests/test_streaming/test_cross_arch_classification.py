"""Cross-arch classification edge cases for ``MetaTemplate`` construction.

Phase 4.4a extension: ``test_meta_templates.py`` covers class A
(per-arch distinct) and class D (cross-arch shared). This file
covers the remaining sharing classes the A/B/C/D classifier emits:

* class B (``family_common_dep``) — one drv per arch family,
* class C (mixed within family) — folded onto
  ``uni_arch_common_dep`` by ``cowalk.cross_arch``,
* the ``variant_specific`` projection when only one arch realises
  a role.

All inputs are synthetic ``nix-store --query --tree`` line buffers
built via :mod:`template_graph.tests.test_streaming.fixtures`; no nix
subprocess.
"""

from __future__ import annotations

from template_graph.cowalk import build_meta_templates
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


def _four_arch_tree(
    arch_to_libfoo_hash: dict[str, str],
) -> Node:
    """Build a 4-arch ``sum-root`` tree with two O-level variants per
    arch. Each variant has one child ``libfoo-1.0.drv`` whose hash is
    looked up in ``arch_to_libfoo_hash``. Per-arch + per-variant seeds
    are derived from the arch-iteration index so variant-root hashes
    stay distinct across (arch, variant).
    """
    def variant(arch: str, seed: int, opt: str, lib_hash: str) -> Node:
        return simple_variant(
            "hello", arch, seed_base=seed, comp="gcc15", opt=opt,
            children=[Node(hash=lib_hash, name="libfoo-1.0.drv")],
        )

    variant_nodes: list[Node] = []
    for i, (arch, lib_hash) in enumerate(arch_to_libfoo_hash.items()):
        # Two variants per arch; seeds chosen so they don't collide
        # with each other or with the root/toolchain/matrix hashes.
        variant_nodes.append(
            variant(arch, 100 + 20 * i, "O0", lib_hash)
        )
        variant_nodes.append(
            variant(arch, 100 + 20 * i + 1, "O1", lib_hash)
        )

    return Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=variant_nodes,
            ),
        ],
    )


# ─── Class B: family_common_dep ──────────────────────────────────────


def test_class_B_family_common_dep_for_intra_family_sharing():
    """4 archs spanning 2 families; ident shared WITHIN each family,
    DIFFERS across families → class B → ``family_common_dep``.

    ``drv_per_node`` shape must be ``{family -> (hash, name)}`` and
    ``class_letter_at_node`` must read ``"B"``.
    """
    x86_hash = make_hash(910)
    arm_hash = make_hash(920)
    arch_to_lib = {
        "x86_64": x86_hash,
        "i686": x86_hash,
        "aarch64": arm_hash,
        "armv7l-hf": arm_hash,
    }
    planner = StreamPlanner(
        archs=("x86_64", "i686", "aarch64", "armv7l-hf"),
    )
    feed(planner, render_tree(_four_arch_tree(arch_to_lib)))
    planner.finalize()

    metas = build_meta_templates(planner.out, "hello")
    assert len(metas) == 1
    mt = metas[0]
    assert set(mt.template_id_per_arch) == set(arch_to_lib)
    libfoo_i = mt.role_at_node.index("libfoo.drv")
    assert mt.cross_arch_classification[libfoo_i] == "family_common_dep"
    assert mt.class_letter_at_node[libfoo_i] == "B"
    drv = mt.drv_per_node[libfoo_i]
    assert isinstance(drv, dict)
    assert drv == {
        "x86": (x86_hash, "libfoo-1.0.drv"),
        "arm": (arm_hash, "libfoo-1.0.drv"),
    }


# ─── Class C: mixed (folds onto uni_arch_common_dep) ─────────────────


def test_class_C_mixed_classification_folds_to_uni_arch():
    """4 archs across 2 families with cross-family sharing that splits
    each family across distinct drvs. ``x86_64`` shares its libfoo
    with ``aarch64`` (one drv) while ``i686`` shares its libfoo with
    ``armv7l-hf`` (another drv). Each unique drv covers archs from
    BOTH families, so the family-clean check fails and the classifier
    returns ``"C"`` → mapped to ``uni_arch_common_dep`` with the full
    per-arch ident mapping.
    """
    drv_a = make_hash(930)
    drv_b = make_hash(940)
    arch_to_lib = {
        "x86_64": drv_a,
        "aarch64": drv_a,
        "i686": drv_b,
        "armv7l-hf": drv_b,
    }
    planner = StreamPlanner(
        archs=("x86_64", "i686", "aarch64", "armv7l-hf"),
    )
    feed(planner, render_tree(_four_arch_tree(arch_to_lib)))
    planner.finalize()

    metas = build_meta_templates(planner.out, "hello")
    assert len(metas) == 1
    mt = metas[0]
    libfoo_i = mt.role_at_node.index("libfoo.drv")
    assert mt.cross_arch_classification[libfoo_i] == "uni_arch_common_dep"
    assert mt.class_letter_at_node[libfoo_i] == "C"
    drv = mt.drv_per_node[libfoo_i]
    assert isinstance(drv, dict)
    assert drv == {
        "x86_64": (drv_a, "libfoo-1.0.drv"),
        "aarch64": (drv_a, "libfoo-1.0.drv"),
        "i686": (drv_b, "libfoo-1.0.drv"),
        "armv7l-hf": (drv_b, "libfoo-1.0.drv"),
    }


# ─── variant_specific projection when only one arch realises a role ──


def test_variant_specific_classification_when_only_one_arch_realises_role():
    """A role that exists in only ONE arch's template — every other
    arch sees a different child ident at the matching position so the
    role-merge keymap sees the cell as variant-specific from at least
    one arch's perspective. The MetaTemplate position must surface as
    ``variant_specific`` (no drv recorded).
    """
    # x86_64: uses unique-per-variant lib hashes → libfoo is
    # variant_specific in that arch's template.
    # aarch64: uses the SAME libfoo hash across its variants → libfoo
    # is common_dep in that arch's template.
    # The class-priority rule (variant_specific > common_dep)
    # dominates: the merged keymap MUST surface "variant_specific" at
    # this position even though aarch64 saw it as common_dep.
    shared_aarch = make_hash(950)

    def x86_variant(seed: int, opt: str) -> Node:
        return simple_variant(
            "hello", "x86_64", seed_base=seed, comp="gcc15", opt=opt,
            children=[
                Node(
                    hash=make_hash(900 + seed), name="libfoo-1.0.drv",
                ),
            ],
        )

    def aarch_variant(seed: int, opt: str) -> Node:
        return simple_variant(
            "hello", "aarch64", seed_base=seed, comp="gcc15", opt=opt,
            children=[Node(hash=shared_aarch, name="libfoo-1.0.drv")],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    x86_variant(10, "O0"),
                    x86_variant(11, "O1"),
                    aarch_variant(30, "O0"),
                    aarch_variant(31, "O1"),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(root))
    planner.finalize()

    metas = build_meta_templates(planner.out, "hello")
    assert len(metas) == 1
    mt = metas[0]
    assert "libfoo.drv" in mt.role_at_node
    libfoo_i = mt.role_at_node.index("libfoo.drv")
    assert mt.cross_arch_classification[libfoo_i] == "variant_specific"
    assert mt.class_letter_at_node[libfoo_i] is None
    assert mt.drv_per_node[libfoo_i] is None

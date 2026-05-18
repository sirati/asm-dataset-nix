"""Basic streaming-planner shape and template-dedup tests.

Covers the happy-path smoke, cross-binary template-dedup
(``find_or_register_template``), the per-binary ``arch_indep_deps``
bucketing, and the calibration-pair + third-variant classification.

All inputs are synthetic ``nix-store --query --tree`` line buffers
constructed via ``_fixtures.py``; no nix subprocess or real
``/nix/store/`` resolution.
"""

from __future__ import annotations

from template_graph.core import find_or_register_template
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming._fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


def test_single_binary_single_variant_smoke():
    """One toolchains wrapper, one matrix-hello, one variant, one
    arch. Expect a single template, a single variant_array cell, no
    violations.
    """
    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    simple_variant(
                        "hello", "x86_64", seed_base=10,
                        children=[
                            Node(hash=make_hash(11),
                                 name="hello-2.12.drv"),
                        ],
                    ),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    assert len(result["templates"]) == 1
    template = result["templates"][0]
    # Variant root + the one child drv.
    assert len(template.nodes) == 2
    assert template.nodes[template.root_id].name.endswith(
        "-elf-folder.drv"
    )
    assert {n.name for n in template.nodes} >= {"hello.drv"}

    assert list(result["variant_arrays"].keys()) == [(0, "x86_64")]
    arr = result["variant_arrays"][(0, "x86_64")]
    assert arr.variants == ["gcc15-O0"]
    # Singleton-arch classification marks all non-toolchain nodes
    # as common_dep.
    classes = result["common_deps_per_arch_template"][(0, "x86_64")]
    for nid, node in enumerate(template.nodes):
        if not node.is_toolchain:
            assert classes[nid] == "common_dep"

    assert planner.violations == []


def test_two_binaries_shared_common_dep():
    """Two binaries (hello, goodbye) each have a matrix-direct-child
    source drv tarball + a variant subtree. Per the planner's
    design, the matrix-direct-child non-variant drv goes into
    ``arch_indep_deps[binary]`` rather than the per-variant
    template, so the shared dep surfaces there for the binary that
    first sees it (subsequent visits are nix-store backrefs).

    On the template-dedup side: although the two binaries produce
    *distinct* variant-root names (the matrix entry point bakes the
    binary name into its role), ``find_or_register_template``
    correctly returns the existing template_id for any
    structurally-equal candidate — exercised directly below.
    """
    shared_src = Node(hash=make_hash(99), name="zlib-1.3.tar.gz.drv")

    def matrix(binary: str, variant_seed: int) -> Node:
        return Node(
            hash=make_hash(variant_seed),
            name=f"matrix-{binary}.drv",
            children=[
                shared_src,
                simple_variant(
                    binary, "x86_64", seed_base=variant_seed + 100,
                    children=[
                        Node(hash=make_hash(variant_seed + 200),
                             name=f"{binary}-1.0.drv"),
                    ],
                ),
            ],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            matrix("hello", 2),
            matrix("goodbye", 3),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    # The shared matrix-direct-child source.drv is recorded under
    # the FIRST matrix that visits it. nix-store renders the second
    # visit as a backref ``[...]``; the planner's design only adds
    # non-backref idents to arch_indep_deps. So the shared ident
    # appears in hello (visited first) but not in goodbye.
    indep = result["arch_indep_deps"]
    shared_ident = (shared_src.hash, shared_src.name)
    assert shared_ident in indep["hello"]
    assert shared_ident not in indep["goodbye"], (
        "second visit is a backref; planner correctly skips re-adding"
    )
    # Both binaries are still keyed (initialised by _on_depth1 even
    # when the per-binary set ends up empty).
    assert "hello" in indep and "goodbye" in indep
    assert planner.violations == []

    # find_or_register_template re-uses an existing id for a
    # structurally-equal candidate. Build the dedup case directly:
    # take the first template and re-submit a structural duplicate.
    templates = result["templates"]
    assert templates, "expected at least one template"
    original = templates[0]
    tid, was_new = find_or_register_template(templates, original)
    assert tid == 0 and was_new is False, (
        f"find_or_register_template should match by shape; "
        f"got tid={tid} was_new={was_new}"
    )


def test_calibration_cowalk_then_third_variant_assertion():
    """Feed two variants for one (binary, arch) cell — that's the
    calibration pair. Feed a third variant that has the SAME
    structure and the SAME hashes as variant 0 / 1 at each role.

    Assert: the third variant's hashes get recorded into the array
    columns 2; classifications fixed by the pair carry through; no
    violations recorded.
    """
    # Every variant shares the same source drv (common_dep) so the
    # pair classification marks the only non-toolchain non-root role
    # as common_dep. A third variant with the same source hash must
    # NOT trigger any violation.
    shared_src_hash = make_hash(50)

    def variant(seed: int, comp: str, opt: str) -> Node:
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp=comp, opt=opt,
            children=[
                # Same hash across all three variants → common_dep.
                Node(hash=shared_src_hash, name="zlib-1.3.drv"),
            ],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant(10, "gcc15", "O0"),
                    variant(20, "gcc15", "O1"),
                    variant(30, "gcc15", "O2"),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    arr = result["variant_arrays"][(0, "x86_64")]
    assert arr.variants == ["gcc15-O0", "gcc15-O1", "gcc15-O2"]
    classes = result["common_deps_per_arch_template"][(0, "x86_64")]
    # Find the zlib node and confirm it's classified as common_dep
    # (all three variants share the same hash).
    tmpl = result["templates"][0]
    zlib_nid = next(
        nid for nid, n in enumerate(tmpl.nodes) if n.name == "zlib.drv"
    )
    assert classes[zlib_nid] == "common_dep"
    # All three variants stored the same hash at that row.
    assert arr.hashes[zlib_nid] == [
        (shared_src_hash, "zlib-1.3.drv")
    ] * 3
    assert planner.violations == []


def test_arch_indep_deps_populated_per_binary():
    """Two binaries each have a non-variant matrix-direct child —
    that's the arch-independent shared dep set. Each binary's set
    should contain its own ident, not the other's.
    """
    hello_src = Node(hash=make_hash(50), name="hello-2.12.tar.gz.drv")
    goodbye_src = Node(hash=make_hash(60),
                       name="goodbye-1.0.tar.gz.drv")

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    hello_src,
                    simple_variant("hello", "x86_64", seed_base=10),
                ],
            ),
            Node(
                hash=make_hash(3), name="matrix-goodbye.drv",
                children=[
                    goodbye_src,
                    simple_variant("goodbye", "x86_64", seed_base=20),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    indep = result["arch_indep_deps"]
    assert (hello_src.hash, hello_src.name) in indep["hello"]
    assert (hello_src.hash, hello_src.name) not in indep["goodbye"]
    assert (goodbye_src.hash, goodbye_src.name) in indep["goodbye"]
    assert (goodbye_src.hash, goodbye_src.name) not in indep["hello"]
    # Both binaries appear as keys.
    assert set(indep.keys()) == {"hello", "goodbye"}

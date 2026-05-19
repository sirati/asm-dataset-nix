"""Tests for the cross-binary template shape histogram diagnostic.

Verifies:

  * a two-binary + two-arch fixture surfaces ``multi_arch_count >= 1``
    and ``multi_binary_count >= 1`` once the variant-root canonical
    placeholder collapses arch + binary axes onto the same shape;
  * a one-binary single-arch fixture marks every shape as
    arch-specific (``arch_specific_count == len(by_shape)``).

Synthetic ``nix-store --query --tree`` inputs only; no nix subprocess.
"""

from __future__ import annotations

from template_graph.cowalk.template_histogram import (
    template_shape_histogram,
)
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


def _two_binary_two_arch_root() -> Node:
    """Two binaries (hello, world), two archs each (x86_64, aarch64),
    matching child shape under every variant.

    Each variant gets a child ``libfoo-1.0.drv`` so every per-arch
    template ends up with the same structural shape after collapsing
    the variant-root role onto the canonical placeholder.
    """
    def variant(binary: str, arch: str, seed: int) -> Node:
        return simple_variant(
            binary, arch, seed_base=seed, comp="gcc15", opt="O0",
            children=[
                Node(hash=make_hash(seed + 1), name="libfoo-1.0.drv"),
            ],
        )

    return Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant("hello", "x86_64", 100),
                    variant("hello", "x86_64", 110),
                    variant("hello", "aarch64", 200),
                    variant("hello", "aarch64", 210),
                ],
            ),
            Node(
                hash=make_hash(3), name="matrix-world.drv",
                children=[
                    variant("world", "x86_64", 300),
                    variant("world", "x86_64", 310),
                    variant("world", "aarch64", 400),
                    variant("world", "aarch64", 410),
                ],
            ),
        ],
    )


def test_two_binary_shape_histogram_surfaces_cross_arch_and_cross_binary():
    """Shape signature collapses per-arch + per-binary variant-root
    role onto a placeholder, so all four ``(template_id, arch)`` cells
    (hello/x86, hello/aarch64, world/x86, world/aarch64) hash to the
    same shape. Histogram must surface both cross-arch and
    cross-binary sharing.
    """
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_binary_two_arch_root()))
    planner.finalize()

    hist = template_shape_histogram(planner.out)
    assert hist.total == 4, hist
    assert hist.multi_arch_count >= 1, hist
    assert hist.multi_binary_count >= 1, hist
    # The shared shape should cover both archs AND both binaries.
    shared_sig = None
    for sig, slot in hist.by_shape.items():
        if (
            set(slot["per_arch"]) == {"x86_64", "aarch64"}
            and set(slot["per_binary"]) == {"hello", "world"}
        ):
            shared_sig = sig
            break
    assert shared_sig is not None, hist
    shared = hist.by_shape[shared_sig]
    assert shared["per_arch"] == {"x86_64": 2, "aarch64": 2}, shared
    assert shared["per_binary"] == {"hello": 2, "world": 2}, shared
    assert shared["total"] == 4, shared


def test_single_binary_single_arch_histogram_is_all_arch_specific():
    """One binary, one arch, two variants → exactly one template +
    one ``(template_id, arch)`` cell. The single shape is by
    definition arch-specific; ``multi_arch_count`` must be zero.
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
                    simple_variant(
                        "hello", "x86_64", seed_base=20,
                        children=[
                            Node(hash=make_hash(21),
                                 name="hello-2.12.drv"),
                        ],
                    ),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    planner.finalize()

    hist = template_shape_histogram(planner.out)
    assert hist.total == 1, hist
    assert hist.arch_specific_count == len(hist.by_shape), hist
    assert hist.multi_arch_count == 0, hist
    assert hist.multi_binary_count == 0, hist


def test_histogram_accepts_finalize_result_dict():
    """Histogram function accepts the ``StreamPlanner.finalize()`` dict
    directly (CLI shape) and produces the same histogram as when
    called on the ``OutputState`` (in-process shape).
    """
    planner = StreamPlanner(archs=("x86_64", "aarch64"))
    feed(planner, render_tree(_two_binary_two_arch_root()))
    result = planner.finalize()
    via_state = template_shape_histogram(planner.out)
    via_dict = template_shape_histogram(result)
    assert via_dict == via_state, (via_dict, via_state)


def test_distinct_shapes_per_binary_are_not_merged():
    """Two binaries whose variant subtrees have DIFFERENT child
    structures produce two distinct shape signatures even after the
    variant-root placeholder collapses the root role.
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
                                 name="libfoo-1.0.drv"),
                        ],
                    ),
                    simple_variant(
                        "hello", "x86_64", seed_base=20,
                        children=[
                            Node(hash=make_hash(21),
                                 name="libfoo-1.0.drv"),
                        ],
                    ),
                ],
            ),
            Node(
                hash=make_hash(3), name="matrix-world.drv",
                children=[
                    simple_variant(
                        "world", "x86_64", seed_base=30,
                        children=[
                            Node(hash=make_hash(31),
                                 name="libbar-2.0.drv"),
                            Node(hash=make_hash(32),
                                 name="libbaz-3.0.drv"),
                        ],
                    ),
                    simple_variant(
                        "world", "x86_64", seed_base=40,
                        children=[
                            Node(hash=make_hash(41),
                                 name="libbar-2.0.drv"),
                            Node(hash=make_hash(42),
                                 name="libbaz-3.0.drv"),
                        ],
                    ),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    planner.finalize()

    hist = template_shape_histogram(planner.out)
    assert hist.total == 2, hist
    # Two distinct shapes: hello has one child, world has two.
    assert len(hist.by_shape) == 2, hist
    # Neither shape is multi-binary (each shape lives in exactly one
    # binary) and neither is multi-arch (single-arch fixture).
    assert hist.multi_binary_count == 0, hist
    assert hist.multi_arch_count == 0, hist
    assert hist.arch_specific_count == 2, hist

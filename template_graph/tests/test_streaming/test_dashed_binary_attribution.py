"""Binaries with dashes in their names are attributed to the right matrix.

``lib/packages.nix`` has labels such as ``c-ares`` and ``zlib-ng``. Their
variant drvs start with ``c-ares-...`` and ``zlib-ng-...``, so parsing the
binary from the text before the first dash, or matching a ``zlib-`` prefix,
attributes them to ``c`` and ``zlib``. The planner records the matrix each
variant array was built under, and consumers read that instead.
"""

from __future__ import annotations

from template_graph.cowalk._role_merge import collect_per_arch
from template_graph.cowalk.template_histogram import template_shape_histogram
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)

_BINARIES = ("zlib", "zlib-ng", "c-ares")


def _matrix(binary: str, idx: int) -> Node:
    seed = 100 * (idx + 1)
    return Node(
        hash=make_hash(10 + idx),
        name=f"matrix-{binary}.drv",
        children=[
            simple_variant(
                binary, "x86_64", seed_base=seed + off, opt=opt,
                children=[Node(hash=make_hash(seed + 50), name="libfoo-1.0.drv")],
            )
            for off, opt in ((0, "O0"), (10, "O2"))
        ],
    )


def _plan():
    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[Node(hash=make_hash(1), name="toolchains.drv")]
        + [_matrix(b, i) for i, b in enumerate(_BINARIES)],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    return planner, planner.finalize()


def test_variant_arrays_record_their_matrix():
    planner, _ = _plan()
    for (tid, _arch), arr in planner.out.variant_arrays.items():
        root = planner.out.templates[tid].nodes[
            planner.out.templates[tid].root_id
        ].name
        assert root.startswith(f"{arr.binary}-x86_64-"), (arr.binary, root)
    assert {a.binary for a in planner.out.variant_arrays.values()} == set(
        _BINARIES
    )


def test_collect_per_arch_does_not_merge_zlib_ng_into_zlib():
    _, result = _plan()
    for binary in _BINARIES:
        by_arch = collect_per_arch(result, binary)
        assert set(by_arch) == {"x86_64"}, binary
        _tmpl, _classes, arr = by_arch["x86_64"]
        assert arr.binary == binary
        assert all(v.startswith(f"{binary}__x86_64__") for v in arr.variants)


def test_histogram_counts_dashed_binaries_under_their_own_name():
    planner, _ = _plan()
    hist = template_shape_histogram(planner.out)
    per_binary: dict[str, int] = {}
    for slot in hist.by_shape.values():
        for b, n in slot["per_binary"].items():
            per_binary[b] = per_binary.get(b, 0) + n
    assert per_binary == {"zlib": 1, "zlib-ng": 1, "c-ares": 1}

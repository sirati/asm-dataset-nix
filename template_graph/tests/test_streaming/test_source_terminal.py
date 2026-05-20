"""Tests for ``_is_source_terminal_role`` predicate + planner wiring.

The predicate recognises arch-independent terminal roles (source
tarballs, fetchurl, builder scripts, patches, setup-hooks) by pure
string match on the role name extracted via ``drv_role`` — no nix
call. The planner short-circuits these like toolchain nodes (no
subtree recursion), increments
``OutputState.source_terminal_skipped`` once per fresh allocation,
and still records the ident in per-binary ``arch_indep_deps`` for
diagnostic counting.
"""

from __future__ import annotations

import pytest

from template_graph.parser.role import _is_source_terminal_role
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming.fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)


# ── predicate: positive matches ─────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [
        # ``*-source`` (git/fetchgit checkouts).
        "linux-kernel-source.drv",
        "linux-kernel-source",
        "foo-source.drv",
        # Archive tarballs of the common compressions.
        "hello.tar.gz.drv",
        "hello.tar.xz.drv",
        "hello.tar.bz2.drv",
        "hello.tar.zst.drv",
        "hello.tar.lz.drv",
        "hello.tar.lzma.drv",
        "hello.tar.lz4.drv",
        "hello.tar.Z.drv",
        "hello.tar.gz",
        # fetchurl drvs (both leading and embedded).
        "fetchurl-something.drv",
        "foo-fetchurl-bar.drv",
        "fetchurl.drv",
        # Builder scripts.
        "hello-builder.sh.drv",
        "hello-builder.pl.drv",
        "hello-builder.sh",
        # Patch files.
        "some-fix.patch.drv",
        "some-fix.patch",
        # Setup-hook shell snippets.
        "foo-setup-hook.drv",
        "foo-setup-hook.sh.drv",
        "foo-setup-hook",
    ],
)
def test_predicate_positive(role: str) -> None:
    assert _is_source_terminal_role(role) is True, (
        f"expected {role!r} to match source-terminal pattern"
    )


# ── predicate: negative (non-matching) cases ────────────────────────


@pytest.mark.parametrize(
    "role",
    [
        # Plain drv names.
        "hello.drv",
        "zlib.drv",
        "libpng.drv",
        # Stdenv / compiler-wrapper roles (handled by other predicates).
        "stdenv-linux.drv",
        "gcc-wrapper.drv",
        # Roles that contain similar substrings but don't match.
        "foo-bar.drv",
        "patchelf.drv",  # 'patch' substring, not '.patch' extension
        "patch-list.drv",
        "sources.drv",  # plural — no ``-source`` boundary
        "source-stdenv.sh",  # different shape (would not pass through
                              # drv_role anyway; defensive)
        # Random tarball-looking but wrong extension.
        "hello.tar.foo.drv",
        # Builder-like but wrong extension.
        "hello-builder.py.drv",
        # ``-source`` only ENDS the role (mid-string is fine).
    ],
)
def test_predicate_negative(role: str) -> None:
    assert _is_source_terminal_role(role) is False, (
        f"expected {role!r} NOT to match source-terminal pattern"
    )


# ── integration: planner short-circuits and records counter ────────


def test_planner_source_terminal_short_circuits() -> None:
    """Feed a synthetic line tree with multiple source-terminal idents
    inside a variant subtree (where the template-alloc short-circuit
    fires). Assert that:

      * ``source_terminal_skipped`` >= 2 — one bump per fresh alloc
        whose role matches the predicate.
      * Both source-terminal idents end up in ``arch_indep_deps[binary]``
        for diagnostic counting (the wiring fires from inside
        ``make_template_node`` / ``_alloc_singleton``).
      * The source-terminal raw children's subtrees were discarded
        — no descendants of theirs leaked into the template.
    """
    # Two source-terminals inside the variant subtree. The matrix
    # depth-2 path already routes its non-variant children into
    # arch_indep_deps without alloc, so source-terminals there
    # don't bump the counter — see test_arch_indep_deps_populated_per_binary.
    inner_tarball = Node(hash=make_hash(70), name="hello-2.12.tar.gz.drv")
    inner_tarball.children.append(
        Node(hash=make_hash(71), name="hello-tarball-child.drv"),
    )
    inner_patch = Node(hash=make_hash(51), name="hello-fix.patch.drv")
    inner_patch.children.append(
        Node(hash=make_hash(52), name="hello-fix.diff"),
    )

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
                            Node(
                                hash=make_hash(11), name="hello-2.12.drv",
                                children=[inner_tarball, inner_patch],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    # Two distinct source-terminal allocations: one ``.tar.gz``, one
    # ``.patch``. Both fire the counter at fresh-alloc time.
    assert planner.out.source_terminal_skipped >= 2, (
        f"expected >= 2 source_terminal_skipped, "
        f"got {planner.out.source_terminal_skipped}"
    )

    # Both idents end up in arch_indep_deps[hello] for diagnostics.
    indep = result["arch_indep_deps"]["hello"]
    assert inner_tarball.ident in indep
    assert inner_patch.ident in indep

    # Children of the source-terminal raw nodes must NOT appear as
    # template nodes — the subtree under a source-terminal alloc is
    # discarded via the existing toolchain short-circuit.
    tmpl = result["templates"][0]
    names = {n.name for n in tmpl.nodes}
    assert "hello-fix.diff" not in names
    assert "hello-tarball-child.drv" not in names

    # Strict mode (default) shouldn't surface any violations.
    assert planner.violations == []


def test_planner_source_terminal_counter_unique_per_alloc() -> None:
    """When a source-terminal raw ident is referenced from two
    different parents inside ONE variant (DAG via nix-tree backref),
    the alloc-time short-circuit fires exactly once for that ident —
    the second occurrence renders as a backref and dedups onto the
    existing template node via ``pair_to_id`` / ``name_to_id``.
    """
    shared_src = Node(hash=make_hash(70), name="zlib-1.3.tar.xz.drv")
    parent_a = Node(
        hash=make_hash(80), name="zlib-A.drv", children=[shared_src],
    )
    parent_b = Node(
        hash=make_hash(81), name="zlib-B.drv", children=[shared_src],
    )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-zlib.drv",
                children=[
                    simple_variant(
                        "zlib", "x86_64", seed_base=10,
                        children=[parent_a, parent_b],
                    ),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    planner.finalize()

    # Exactly one fresh alloc of the .tar.xz role: parent_b's child
    # reference renders as a nix-tree backref of parent_a's child.
    assert planner.out.source_terminal_skipped == 1

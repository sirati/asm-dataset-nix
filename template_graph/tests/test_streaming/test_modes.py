"""Lax / strict mode and stdenv-capture tests for ``StreamPlanner``.

Same shape-violating input is fed twice: once with ``lax=True`` (must
record violations and finish without raising), once with ``lax=False``
(must raise). The stdenv-subtree capture test verifies that a node
whose role matches ``_is_stdenv_role`` causes ``stdenv_subtrees`` to
be populated with first-seen-in metadata.
"""

from __future__ import annotations

import pytest

from template_graph.core import TemplateGraphAssertError
from template_graph.streaming import StreamPlanner
from template_graph.tests.test_streaming._fixtures import (
    Node,
    feed,
    make_hash,
    render_tree,
    simple_variant,
)
from template_graph.tree_walker import TreeWalkError


def _build_calibration_count_mismatch_tree() -> Node:
    """Variant 0 has 2 children named ``lib-thing-N.0.drv``; variant
    1 has 3. Under ``drv_role`` both names normalise to
    ``lib-thing.drv`` so the calibration walks them under one
    ``common`` bucket and fires the same-name child-count mismatch.
    """
    def variant(seed: int, opt: str, n_kids: int) -> Node:
        kids = [
            Node(hash=make_hash(seed + 100 + i),
                 name=f"lib-thing-{1 + i}.0.drv")
            for i in range(n_kids)
        ]
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp="gcc15", opt=opt, children=kids,
        )

    return Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant(10, "O0", n_kids=2),
                    variant(20, "O1", n_kids=3),
                ],
            ),
        ],
    )


def test_lax_mode_records_violations_not_raises():
    """Under lax=True the planner records the calibration-mismatch
    violation and finishes; no exception escapes ``finalize()``.
    """
    planner = StreamPlanner(archs=("x86_64",), lax=True)
    feed(planner, render_tree(_build_calibration_count_mismatch_tree()))
    result = planner.finalize()
    kinds = {v["kind"] for v in planner.violations}
    assert "calibration-same-name-count-mismatch" in kinds, (
        f"expected calibration-same-name-count-mismatch in violations; "
        f"got {kinds}"
    )
    assert planner.violations
    # Result still produced templates (best-effort).
    assert len(result["templates"]) >= 1


def test_strict_mode_raises_on_calibration_mismatch():
    """The same calibration-shape violation under lax=False raises.
    The planner currently wraps this with ``TreeWalkError`` at
    calibration time (see ``_build_template``'s "calibration pair
    same-name child count mismatch" branch). Other code paths can
    surface ``TemplateGraphAssertError``; accept either.
    """
    planner = StreamPlanner(archs=("x86_64",), lax=False)
    with pytest.raises((TreeWalkError, TemplateGraphAssertError)):
        feed(planner, render_tree(_build_calibration_count_mismatch_tree()))
        planner.finalize()


def test_stdenv_subtrees_recorded_when_stdenv_role_appears():
    """When a variant subtree contains an stdenv-named child, the
    planner siphons the subtree into ``stdenv_subtrees`` keyed by
    the stdenv ident.
    """
    stdenv_hash = make_hash(70)
    # stdenv role is a toolchain terminal; its subtree is captured
    # and recorded in stdenv_subtrees but no children appear in the
    # template.
    stdenv_node = Node(
        hash=stdenv_hash, name="stdenv-linux.drv",
        children=[
            Node(hash=make_hash(71), name="bootstrap-tools.drv"),
        ],
    )

    def variant(seed: int, opt: str) -> Node:
        return simple_variant(
            "hello", "x86_64", seed_base=seed,
            comp="gcc15", opt=opt,
            children=[stdenv_node],
        )

    root = Node(
        hash=make_hash(0), name="sum-root.drv",
        children=[
            Node(hash=make_hash(1), name="toolchains.drv"),
            Node(
                hash=make_hash(2), name="matrix-hello.drv",
                children=[
                    variant(10, "O0"),
                    variant(20, "O1"),
                ],
            ),
        ],
    )
    planner = StreamPlanner(archs=("x86_64",))
    feed(planner, render_tree(root))
    result = planner.finalize()

    stdenv_subtrees = result["stdenv_subtrees"]
    assert (stdenv_hash, "stdenv-linux.drv") in stdenv_subtrees, (
        f"expected stdenv ident in stdenv_subtrees; "
        f"got keys={list(stdenv_subtrees)}"
    )
    entry = stdenv_subtrees[(stdenv_hash, "stdenv-linux.drv")]
    assert entry["first_seen_in"]["matrix"] == "hello"
    assert entry["first_seen_in"]["arch"] == "x86_64"

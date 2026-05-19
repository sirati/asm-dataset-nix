"""Unit tests for ``_shape_equal`` returning a ``TemplateAlignment``.

The promoted ``_shape_equal`` returns a ``TemplateAlignment`` whose
``node_pairs`` give the ``(a_id, b_id)`` correspondence in the
canonical recursive-walk order (pre-order from the root, children
visited left-to-right, each ``a``-node recorded once on its first
visit — DAG-revisits on the ``a`` side are skipped).

These tests construct same-shape pairs with deliberately distinct
``node_id`` assignments so a correct alignment can be distinguished
from an identity mapping, plus a not-equal pair to confirm the
``None`` return path.
"""

from __future__ import annotations

from template_graph.graph.template import (
    Template,
    TemplateAlignment,
    TemplateNode,
    _shape_equal,
)


def _two_node_template(*, root_first: bool) -> Template:
    """Build a 2-node template (root + single child).

    ``root_first=True`` places the root at index 0 and child at 1.
    ``root_first=False`` flips them: child at 0, root at 1. Same
    shape, different node-id layout — perfect for distinguishing a
    real alignment from an identity mapping.
    """
    root = TemplateNode(name="root.drv", child_ids=[])
    child = TemplateNode(name="child.drv", child_ids=[])
    if root_first:
        root.child_ids = [1]
        nodes = [root, child]
        return Template(
            nodes=nodes,
            name_to_id={"root.drv": 0, "child.drv": 1},
            root_id=0,
        )
    root.child_ids = [0]
    nodes = [child, root]
    return Template(
        nodes=nodes,
        name_to_id={"child.drv": 0, "root.drv": 1},
        root_id=1,
    )


def test_shape_equal_returns_alignment_for_same_shape_distinct_ids():
    """Two same-shape templates with deliberately swapped node-id
    layouts produce a non-identity alignment in canonical walk order.
    """
    a = _two_node_template(root_first=True)
    b = _two_node_template(root_first=False)

    alignment = _shape_equal(a, b)
    assert isinstance(alignment, TemplateAlignment)
    # Pre-order walk from each root: (a.root, b.root) then (a.child,
    # b.child). ``a`` has root_id=0, child_id=1; ``b`` has root_id=1,
    # child_id=0. So the canonical walk pairs are:
    assert alignment.node_pairs == ((0, 1), (1, 0))


def test_shape_equal_returns_none_when_shapes_differ():
    """Two templates that disagree on a node name return ``None``."""
    a = _two_node_template(root_first=True)
    # Same shape skeleton as ``a`` but with a different child name —
    # ``_shape_equal``'s name check must reject this.
    b = Template(
        nodes=[
            TemplateNode(name="root.drv", child_ids=[1]),
            TemplateNode(name="OTHER.drv", child_ids=[]),
        ],
        name_to_id={"root.drv": 0, "OTHER.drv": 1},
        root_id=0,
    )

    assert _shape_equal(a, b) is None


def test_shape_equal_returns_none_when_node_count_differs():
    """Different node counts short-circuit to ``None`` before walking."""
    a = _two_node_template(root_first=True)
    b = Template(
        nodes=[TemplateNode(name="root.drv", child_ids=[])],
        name_to_id={"root.drv": 0},
        root_id=0,
    )
    assert _shape_equal(a, b) is None


def test_shape_equal_alignment_covers_branching_template():
    """A root with two children + one grandchild: alignment records
    each ``a``-node once in pre-order; child-index ordering means the
    first child's subtree is fully walked before the second child.
    """
    # Template a: root(0) -> [child_l(1) -> grand(2), child_r(3)]
    a = Template(
        nodes=[
            TemplateNode(name="root.drv", child_ids=[1, 3]),
            TemplateNode(name="left.drv", child_ids=[2]),
            TemplateNode(name="grand.drv", child_ids=[]),
            TemplateNode(name="right.drv", child_ids=[]),
        ],
        name_to_id={"root.drv": 0, "left.drv": 1,
                    "grand.drv": 2, "right.drv": 3},
        root_id=0,
    )
    # Template b: same shape, distinct id layout
    # root(3) -> [left(2) -> grand(0), right(1)]
    b = Template(
        nodes=[
            TemplateNode(name="grand.drv", child_ids=[]),
            TemplateNode(name="right.drv", child_ids=[]),
            TemplateNode(name="left.drv", child_ids=[0]),
            TemplateNode(name="root.drv", child_ids=[2, 1]),
        ],
        name_to_id={"grand.drv": 0, "right.drv": 1,
                    "left.drv": 2, "root.drv": 3},
        root_id=3,
    )

    alignment = _shape_equal(a, b)
    assert isinstance(alignment, TemplateAlignment)
    # Pre-order from a.root_id=0:
    #   visit (0, 3)               -- root
    #   recurse first child: (1, 2)  -- left
    #     recurse its child:  (2, 0)  -- grand
    #   recurse second child: (3, 1) -- right
    assert alignment.node_pairs == ((0, 3), (1, 2), (2, 0), (3, 1))


def test_shape_equal_alignment_handles_dag_revisit_on_a_side():
    """When the ``a`` side shares a child between two parents (DAG),
    the second visit is short-circuited by the memo, so the alignment
    records the shared node only once — matching the equality-check
    walk that ``_shape_equal`` performs.
    """
    # a: root(0) has two parents pointing at shared(2)
    # root(0) -> [p1(1) -> shared(2), p2(3) -> shared(2)]
    a = Template(
        nodes=[
            TemplateNode(name="root.drv", child_ids=[1, 3]),
            TemplateNode(name="p1.drv", child_ids=[2]),
            TemplateNode(name="shared.drv", child_ids=[]),
            TemplateNode(name="p2.drv", child_ids=[2]),
        ],
        name_to_id={"root.drv": 0, "p1.drv": 1,
                    "shared.drv": 2, "p2.drv": 3},
        root_id=0,
    )
    # b: same shape with shared child reused under both parents.
    b = Template(
        nodes=[
            TemplateNode(name="root.drv", child_ids=[1, 3]),
            TemplateNode(name="p1.drv", child_ids=[2]),
            TemplateNode(name="shared.drv", child_ids=[]),
            TemplateNode(name="p2.drv", child_ids=[2]),
        ],
        name_to_id={"root.drv": 0, "p1.drv": 1,
                    "shared.drv": 2, "p2.drv": 3},
        root_id=0,
    )
    alignment = _shape_equal(a, b)
    assert isinstance(alignment, TemplateAlignment)
    # The shared(2) node is visited exactly once on the ``a`` side
    # (memoised), so node_pairs has 4 entries, not 5.
    assert alignment.node_pairs == ((0, 0), (1, 1), (2, 2), (3, 3))

"""Regression for toolchain DAGs encountered by the real release pilot."""
from template_graph.streaming.state import RawTreeNode, StreamPlanner


def test_discard_shared_toolchain_dag_visits_each_node_once():
    class Children(list):
        visits = 0

        def __iter__(self):
            self.visits += 1
            assert self.visits == 1, "shared subtree traversed again"
            return super().__iter__()

    planner = StreamPlanner()
    leaf = RawTreeNode(b"leaf", "source.drv", False, 40, children=Children())
    nodes = [leaf]
    for depth in range(40):
        leaf = RawTreeNode(str(depth).encode(), "toolchain.drv", False, depth,
                           children=Children([leaf, leaf]))
        nodes.append(leaf)
    unrelated = (b"unrelated", "other-release-dependency.drv")
    planner.mx.unclassified_nodes = {node.ident for node in nodes} | {unrelated}
    planner._discard_subtree(leaf)
    assert planner.mx.unclassified_nodes == {unrelated}

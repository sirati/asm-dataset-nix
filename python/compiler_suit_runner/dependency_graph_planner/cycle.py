"""Cycle-detection walk over the streaming planner's template graph.

Nix drv graphs are DAGs by construction; this is a defensive guard
that raises :class:`DependencyGraphCycleError` if a malformed input
sneaks a back-edge into the template node graph.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .descriptors import DependencyGraphCycleError
from .shapes import _node_field, _template_nodes


def _check_no_cycles(templates: Sequence[Any]) -> None:
    """Walk every template's ``nodes`` array via ``child_ids`` and raise
    :class:`DependencyGraphCycleError` if a back-edge to a node still
    on the current DFS stack is observed.

    Templates are conceptually DAGs in nix, but the streaming planner
    may register multiple template instances and a malformed input
    could in principle smuggle a cycle in. Cost is linear in
    ``sum(len(nodes))`` so the guard is cheap even at production
    matrix sizes.
    """
    for tmpl_id, template in enumerate(templates):
        nodes = _template_nodes(template)
        n = len(nodes)
        if n == 0:
            continue
        WHITE, GREY, BLACK = 0, 1, 2
        color = [WHITE] * n
        # Iterative DFS so deep templates don't blow the recursion
        # limit; the production matrix can have thousands of nodes
        # per template under stage-2 stdenv expansion.
        for start in range(n):
            if color[start] != WHITE:
                continue
            stack: list[tuple[int, list[int]]] = [
                (start, list(_node_field(nodes[start], "child_ids", []) or []))
            ]
            color[start] = GREY
            while stack:
                node_id, pending = stack[-1]
                if not pending:
                    color[node_id] = BLACK
                    stack.pop()
                    continue
                child = pending.pop()
                if not isinstance(child, int):
                    # Skip non-int child entries silently -- the
                    # streaming planner never emits these but a
                    # malformed JSON roundtrip could.
                    continue
                if child < 0 or child >= n:
                    continue
                if color[child] == GREY:
                    raise DependencyGraphCycleError(
                        f"cycle detected in template #{tmpl_id} at "
                        f"node {child} (re-entered while still on the "
                        f"DFS stack starting at node {start})"
                    )
                if color[child] == BLACK:
                    continue
                color[child] = GREY
                stack.append((
                    child,
                    list(_node_field(nodes[child], "child_ids", []) or []),
                ))

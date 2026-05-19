"""Public convenience entry point for the streaming planner.

``plan_from_tree_streaming(tree_text, *, archs=...)`` constructs a
``StreamPlanner``, feeds it line-by-line, finalizes, and returns the
result dict (same shape as ``core.plan_phase1_graph``).
"""

from __future__ import annotations

from template_graph.parser.role import drv_role
from template_graph.tree_walker import DEFAULT_ARCHS

from template_graph.streaming.state import StreamPlanner


def plan_from_tree_streaming(
    tree_text: str,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
    name_extractor=drv_role,
    logger=None,
    lax: bool = False,
) -> dict:
    planner = StreamPlanner(
        archs=archs,
        name_extractor=name_extractor,
        logger=logger,
        lax=lax,
    )
    for line in tree_text.splitlines():
        planner.feed_line(line)
    result = planner.finalize()
    result["violations"] = planner.violations
    return result

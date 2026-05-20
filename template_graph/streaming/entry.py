"""Public convenience entry point for the streaming planner.

``plan_from_tree_streaming(tree_text, *, archs=...)`` constructs a
``StreamPlanner``, feeds it line-by-line, finalizes, and returns the
planner output dict (see ``template_graph.streaming`` for the shape).
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


def plan_from_drv_tree(
    stream,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
    name_extractor=drv_role,
    logger=None,
    lax: bool = False,
) -> dict:
    """Stream-plan a parsed byte stream from nix-store --query --tree.

    ``stream`` is any iterator yielding ``(depth, drv_hash, drv_name,
    is_backref)`` tuples — typically ``drv_tree_stream(popen.stdout)``.
    Lets the planner run concurrently with the producer.
    """
    planner = StreamPlanner(
        archs=archs, name_extractor=name_extractor,
        logger=logger, lax=lax,
    )
    for depth, drv_hash, drv_name, is_backref in stream:
        planner.feed_parsed(depth, drv_hash, drv_name, is_backref)
    result = planner.finalize()
    result["violations"] = planner.violations
    return result

"""Small helpers used by ``build_template.py`` alloc sites.

``_capture_stdenv`` and ``_record_source_terminal`` are factored out of
the main module to keep ``build_template.py`` under the aspirational
300-LOC limit. They share the same shape: classify a role, then nudge
state on the planner (siphon stdenv subtrees / bump source-terminal
counter + record into ``arch_indep_deps``)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from template_graph.streaming import RawTreeNode, StreamPlanner


def _capture_stdenv(
    planner: "StreamPlanner", raw_nodes: list["RawTreeNode"],
    label_slots: list[str], eff_arch: Optional[str],
) -> None:
    """Siphon each unique stdenv raw root into ``out.stdenv_subtrees``."""
    seen: set[tuple[bytes, str]] = set()
    for rn, slot in zip(raw_nodes, label_slots):
        if rn.ident in seen:
            continue
        seen.add(rn.ident)
        planner.out.stdenv_subtrees.setdefault(rn.ident, {
            "first_seen_in": {
                "matrix": planner.mx.matrix_binary,
                "arch": eff_arch, "label": slot,
            },
            "root": rn,
        })


def _record_source_terminal(
    planner: "StreamPlanner", raw_nodes: list["RawTreeNode"],
) -> None:
    """Bump ``source_terminal_skipped`` once per fresh alloc; add each
    non-backref ident into the current matrix's ``arch_indep_deps``
    set for diagnostic counting (downstream filters by role)."""
    planner.out.source_terminal_skipped += 1
    binary = planner.mx.matrix_binary
    if binary is None:
        return
    bucket = planner.out.arch_indep_deps.setdefault(binary, set())
    for rn in raw_nodes:
        if not rn.is_backref:
            bucket.add(rn.ident)

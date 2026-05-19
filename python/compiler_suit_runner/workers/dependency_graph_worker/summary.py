"""Phase 6.1b summary + violation log helpers.

The worker logs a single INFO-level summary line after planning
completes, listing the binaries it touched, the per-category descriptor
counts, and the streaming-planner diagnostic counters
(``source_terminal_skipped`` / ``violations``). When the planner
recorded any survey-mode shape violations, the worker additionally
dumps the first :data:`VIOLATION_DUMP_LIMIT` entries at WARN level so
the operator can investigate without the worker silently swallowing
them.

The planner-invocation shim :func:`invoke_planner` handles the test
monkeypatch convention (``monkeypatch.setattr(<pkg>, "plan_total", ...)``)
by detecting whether ``plan_total`` was patched and degrading to a
counter-free best-effort. When unpatched, the worker calls into
:func:`plan.plan_total_with_counters` directly so the streaming
result's counter + violation surface is captured.
"""

from __future__ import annotations

import logging
from typing import Any


__all__ = [
    "VIOLATION_DUMP_LIMIT",
    "invoke_planner",
    "emit_summary_log",
    "emit_violations_log",
]


logger = logging.getLogger("compiler_suit_runner.dependency_graph_worker")


# Max number of violation entries the worker dumps after the summary
# line; the streaming planner can attach a long tail of survey-mode
# violations and emitting them all to the worker log would dwarf the
# rest of the run-summary output.
VIOLATION_DUMP_LIMIT = 20


def invoke_planner(
    *,
    pkg: Any,
    tree_text: str,
    binaries: list[str],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    tc_ids: dict[str, str],
    sys_name: str,
) -> tuple[list[Any], dict[str, int], list[dict]]:
    """Call into the streaming planner, returning descriptors + counters
    + violations.

    Tests monkeypatch ``plan_total`` on the package namespace; when that
    happens the patched function only returns descriptors so we degrade
    to zero counters / no violations (the worker still emits a summary
    line, just with all-zero streaming-derived fields). When unpatched
    we call :func:`plan.plan_total_with_counters` which exposes the
    streaming result and lets us populate the full counter set.
    """
    from . import plan as _plan_module  # noqa: PLC0415

    plan_total_attr = getattr(pkg, "plan_total", None)
    unpatched_plan_total = getattr(_plan_module, "plan_total", None)
    if (
        plan_total_attr is unpatched_plan_total
        and unpatched_plan_total is not None
    ):
        return _plan_module.plan_total_with_counters(
            tree_text=tree_text,
            binaries=binaries,
            variant_lookups=variant_lookups,
            toolchain_task_ids=tc_ids,
            sys_name=sys_name,
        )
    # Patched plan_total path: caller's fake supplies descriptors only.
    descriptors = pkg.plan_total(
        tree_text=tree_text,
        binaries=binaries,
        variant_lookups=variant_lookups,
        toolchain_task_ids=tc_ids,
        sys_name=sys_name,
    )
    counters = _plan_module.compute_dependency_graph_counters(
        streaming_result={},
        descriptors=descriptors,
        binaries=binaries,
    )
    return descriptors, counters, []


def emit_summary_log(
    *,
    binaries: list[str],
    counters: dict[str, int],
) -> None:
    """INFO-level summary line covering the planning outcome (plan §E8)."""
    binary_list = "/".join(binaries) if binaries else "<none>"
    logger.info(
        "dependency_graph: binaries=%s (N=%d) templates=%d meta_templates=%d "
        "variants=%d common_deps={cross_arch=%d family=%d uni_arch=%d "
        "arch_indep=%d source_terminal_skipped=%d toolchain_wired=%d} "
        "stdenv_subtrees=%d violations=%d",
        binary_list,
        len(binaries),
        counters.get("templates", 0),
        counters.get("meta_templates", 0),
        counters.get("variants", 0),
        counters.get("common_deps_cross_arch", 0),
        counters.get("common_deps_family", 0),
        counters.get("common_deps_uni_arch", 0),
        counters.get("common_deps_arch_indep", 0),
        counters.get("source_terminal_skipped", 0),
        counters.get("toolchain_wired", 0),
        counters.get("stdenv_subtrees", 0),
        counters.get("violations", 0),
    )


def emit_violations_log(violation_entries: list[dict]) -> None:
    """WARN-level dump of the first :data:`VIOLATION_DUMP_LIMIT` entries.

    Each entry is a dict whose ``kind`` + ``matrix`` keys identify the
    classification of the shape violation; remaining keys vary by kind.
    Truncation is reported in the same line so the operator knows more
    entries exist.
    """
    total = len(violation_entries)
    sample = violation_entries[:VIOLATION_DUMP_LIMIT]
    suffix = (
        f" (showing first {len(sample)} of {total})"
        if total > len(sample)
        else ""
    )
    logger.warning(
        "dependency_graph: %d violation%s recorded%s; entries=%r",
        total,
        "" if total == 1 else "s",
        suffix,
        sample,
    )

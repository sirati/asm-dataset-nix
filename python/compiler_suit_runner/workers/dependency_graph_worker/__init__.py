"""Phase 3 ``dependency_graph`` worker — primary-only template-graph adapter.

After all phase-2 ``matrix_eval`` tasks quiesce, the watcher on the
primary calls this worker to translate the per-binary ``.nix-archive``
artefacts into a phase-4 task plan (build_common_dep + build_variant).

Worker flow:

  1. ``nix-store --import < <matrix_eval_out_dir>/<binary>.nix-archive``
     loads the kept variant drvs + their transitive input closure into
     the primary's local store. Skipped per-binary when every kept-drv
     in the archive's sidecar is already present locally.
  2. Discover the kept variant-drv list (sidecar first, header second).
  3. ``template_graph.make_sum_drv.make_sum_drv_from_paths`` glues
     toolchains + per-binary kept-drvs into a single sum-root drv;
     ``nix-store --query --tree`` walks that into the line-by-line
     tree the streaming planner consumes.
  4. ``template_graph.streaming.plan_from_tree_streaming`` produces the
     classified template graph.
  5. ``dependency_graph_planner.plan_phase4_from_graph`` adapts the
     streaming result into a flat list of
     :class:`Phase4Descriptor` records.
  6. The descriptor list is JSON-dumped to
     ``<matrix_eval_out_dir>/_dependency_graph.json`` for the
     primary-side spawn-tasks step to pick up.

Module layout
-------------

This package supersedes the legacy single-file
``dependency_graph_worker.py`` (Phase 5.2 split per plan §F3). The
public surface is unchanged — every symbol that the legacy module
exposed is re-exported at the package level here so existing imports
and test monkeypatches keep working unmodified:

  * :mod:`.errors`    — :class:`DependencyGraphWorkerError`,
                         :class:`DependencyGraphResult`.
  * :mod:`.subproc`   — :data:`RunSubprocess`, default runner.
  * :mod:`.archive`   — archive discovery, kept-drv discovery,
                         presence probe + import.
  * :mod:`.sum_drv`   — sum-drv assembly + tree-walk.
  * :mod:`.plan`      — streaming-planner driver (single + multi-binary).
  * :mod:`.output`    — atomic ``_dependency_graph.json`` writer.
  * :mod:`.run`       — top-level driver function.
  * :mod:`.cli`       — argparse + ``main``.
"""

from __future__ import annotations

import sys

from .archive import (
    discover_archives,
    discover_kept_drvs,
    import_archive,
    is_path_locally_present,
)
from .cli import build_cli_parser as _build_cli_parser
from .cli import main
from .cli import parse_task_id_mappings as _parse_task_id_mappings
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .output import DEPENDENCY_GRAPH_JSON, write_dependency_graph_json
from .plan import (
    compute_dependency_graph_counters,
    plan_binary,
    plan_total,
    plan_total_with_counters,
)
from .run import run_dependency_graph_task
from .sum_drv import build_sum_drv, build_sum_drv_multi, query_drv_tree


__all__ = [
    "DependencyGraphResult",
    "DependencyGraphWorkerError",
    "DEPENDENCY_GRAPH_JSON",
    "build_sum_drv",
    "build_sum_drv_multi",
    "compute_dependency_graph_counters",
    "discover_archives",
    "discover_kept_drvs",
    "import_archive",
    "is_path_locally_present",
    "main",
    "plan_binary",
    "plan_total",
    "plan_total_with_counters",
    "query_drv_tree",
    "run_dependency_graph_task",
    "write_dependency_graph_json",
]


# Keep the underscored CLI helpers reachable for the unit-test surface
# that already references them.
globals()["_build_cli_parser"] = _build_cli_parser
globals()["_parse_task_id_mappings"] = _parse_task_id_mappings


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())

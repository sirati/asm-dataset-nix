"""Phase 3 ``dependency_graph`` worker — primary-only template-graph adapter.

After all phase-2 ``matrix_eval`` tasks quiesce, the watcher on the
primary calls this worker to translate the per-binary ``.nix-archive``
artefacts into a phase-4 task plan (build_common_dep + build_variant).

Worker flow:

  1. ``nix-store --import < <matrix_eval_out_dir>/<binary>.nix-archive``
     loads the kept variant drvs + their transitive input closure into
     the primary's local store. The store paths printed on stdout by
     ``nix-store --import`` ARE the kept-drv source — no sidecar JSON,
     no header lookup.
  2. Filter that stdout to ``*-elf-folder.drv`` to recover the kept
     variant-drv list + per-binary ``variant_lookup``.
  3. ``template_graph.make_sum_drv.make_sum_drv_from_paths`` glues
     toolchains + per-binary kept-drvs into a single sum-root drv;
     ``nix-store --query --tree`` walks that into the line-by-line
     tree the streaming planner consumes.
  4. ``template_graph.streaming.plan_from_tree_streaming`` produces the
     classified template graph.
  5. ``dependency_graph_planner.plan_phase4_from_graph`` adapts the
     streaming result into a flat list of
     :class:`Phase4Descriptor` records.
  6. The descriptor list is pickled to
     ``<matrix_eval_out_dir>/_dependency_graph.pkl`` (with a companion
     ``_dependency_graph_summary.txt`` for operator inspection) for the
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
  * :mod:`.output`    — atomic ``_dependency_graph.pkl`` writer +
                         ``_dependency_graph_summary.txt`` companion.
  * :mod:`.run`       — top-level driver function.
  * :mod:`.cli`       — argparse + ``main``.
"""

from __future__ import annotations

import sys

from .archive import (
    derive_variant_lookup_from_aggregate,
    derive_variant_lookup_from_drvs,
    discover_archives,
    discover_kept_drvs_from_imported_store,
    import_archive,
    is_path_locally_present,
)
from .cli import build_cli_parser as _build_cli_parser
from .cli import main
from .cli import parse_task_id_mappings as _parse_task_id_mappings
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .output import (
    DEPENDENCY_GRAPH_PICKLE,
    DEPENDENCY_GRAPH_SUMMARY,
    PHASE4_PICKLE_FORMAT_VERSION,
    PHASE4_PICKLE_MAGIC,
    write_phase4_descriptors,
    write_phase4_summary_text,
)
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
    "DEPENDENCY_GRAPH_PICKLE",
    "DEPENDENCY_GRAPH_SUMMARY",
    "PHASE4_PICKLE_FORMAT_VERSION",
    "PHASE4_PICKLE_MAGIC",
    "build_sum_drv",
    "build_sum_drv_multi",
    "compute_dependency_graph_counters",
    "derive_variant_lookup_from_aggregate",
    "derive_variant_lookup_from_drvs",
    "discover_archives",
    "discover_kept_drvs_from_imported_store",
    "import_archive",
    "is_path_locally_present",
    "main",
    "plan_binary",
    "plan_total",
    "plan_total_with_counters",
    "query_drv_tree",
    "run_dependency_graph_task",
    "write_phase4_descriptors",
    "write_phase4_summary_text",
]


# Keep the underscored CLI helpers reachable for the unit-test surface
# that already references them.
globals()["_build_cli_parser"] = _build_cli_parser
globals()["_parse_task_id_mappings"] = _parse_task_id_mappings


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    sys.exit(main())

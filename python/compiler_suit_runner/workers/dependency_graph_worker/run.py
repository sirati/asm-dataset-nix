"""Top-level driver: archive walk → sum-drv → streaming → descriptors."""

from __future__ import annotations

import pathlib
import time
from collections.abc import Callable
from typing import Any, Optional

from . import archive as _archive
from . import output as _output
from . import plan as _plan
from . import sum_drv as _sum_drv
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "run_dependency_graph_task",
]


def run_dependency_graph_task(
    *,
    matrix_eval_out_dir: pathlib.Path,
    manifest_dir: Optional[pathlib.Path],
    bash_path: str,
    toolchain_drvs: list[str],
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
    skip_import_when_present: bool = True,
) -> DependencyGraphResult:
    """Walk every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``
    and produce ``_dependency_graph.json``.

    Per-binary failures raise :class:`DependencyGraphWorkerError`; the
    caller's main loop translates that into a non-zero exit code.
    """
    clock_fn = clock or time.monotonic
    start = clock_fn()

    archives = _archive.discover_archives(matrix_eval_out_dir)
    if not archives:
        out_path = _output.write_dependency_graph_json(
            matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_JSON, []
        )
        return DependencyGraphResult(
            output_path=out_path,
            binary_count=0,
            descriptor_count=0,
            duration_seconds=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})
    all_descriptors: list[Any] = []
    binary_count = 0

    for archive in archives:
        binary = archive.stem
        kept_drvs, variant_lookup = _archive.discover_kept_drvs(
            archive, manifest_dir,
        )
        if not kept_drvs:
            # Logged inside discover_kept_drvs; treat as skip.
            continue

        _maybe_import_archive(
            archive=archive,
            kept_drvs=kept_drvs,
            runner=runner,
            skip_import_when_present=skip_import_when_present,
            binary=binary,
        )

        descriptors = _plan_single_binary(
            bash_path=bash_path,
            toolchain_drvs=toolchain_drvs,
            binary=binary,
            kept_drvs=kept_drvs,
            sys_name=sys_name,
            variant_lookup=variant_lookup,
            tc_ids=tc_ids,
            runner=runner,
        )
        all_descriptors.extend(descriptors)
        binary_count += 1

    out_path = _output.write_dependency_graph_json(
        matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_JSON, all_descriptors
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=binary_count,
        descriptor_count=len(all_descriptors),
        duration_seconds=max(0.0, clock_fn() - start),
    )


def _maybe_import_archive(
    *,
    archive: pathlib.Path,
    kept_drvs: list[str],
    runner: RunSubprocess,
    skip_import_when_present: bool,
    binary: str,
) -> None:
    """Import the archive unless every kept drv is already locally present."""
    need_import = True
    if skip_import_when_present:
        need_import = not all(
            _archive.is_path_locally_present(p, run_subprocess=runner)
            for p in kept_drvs
        )
    if not need_import:
        return
    ok, err = _archive.import_archive(archive, run_subprocess=runner)
    if not ok:
        raise DependencyGraphWorkerError(
            binary=binary, stage="import",
            message=(
                "nix-store --import failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
        )


def _plan_single_binary(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    binary: str,
    kept_drvs: list[str],
    sys_name: str,
    variant_lookup: dict[tuple[str, str], dict],
    tc_ids: dict[str, str],
    runner: RunSubprocess,
) -> list[Any]:
    """Build a single-binary sum-drv, query its tree, and plan its phase-4
    descriptors. Failures raise :class:`DependencyGraphWorkerError`
    tagged with the failing stage.

    The ``build_sum_drv`` and ``plan_binary`` callables are resolved
    via the package's top-level namespace at call time so test
    monkeypatches on the package-level aliases (used by the existing
    unit tests) still take effect.
    """
    # Resolve via the package object at call time so monkeypatch.setattr
    # on the package-level alias is honoured. ``from . import build_sum_drv``
    # would bind a local at import time and miss the patch.
    import importlib  # noqa: PLC0415
    _pkg = importlib.import_module(__package__)

    try:
        sum_drv = _pkg.build_sum_drv(
            bash_path=bash_path,
            toolchain_drvs=toolchain_drvs,
            binary=binary,
            variant_drvs=kept_drvs,
            system=sys_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary=binary, stage="sum_drv",
            message=f"sum-drv assembly failed: {exc}",
            cause=exc,
        ) from exc

    try:
        tree_text = _sum_drv.query_drv_tree(sum_drv, run_subprocess=runner)
    except RuntimeError as exc:
        raise DependencyGraphWorkerError(
            binary=binary, stage="query_tree",
            message=str(exc), cause=exc,
        ) from exc

    try:
        return _pkg.plan_binary(
            binary=binary,
            tree_text=tree_text,
            variant_lookup=variant_lookup,
            toolchain_task_ids=tc_ids,
            sys_name=sys_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary=binary, stage="plan",
            message=f"plan_phase4 failed: {exc}",
            cause=exc,
        ) from exc

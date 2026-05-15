"""Tests for :meth:`SuitTask.get_phases`.

The phase graph is what the dynamic_runner framework consumes to
schedule the run; every dependency edge is load-bearing. These tests
validate the four-phase ``phase1a -> phase1b -> phase2 -> phase3``
topology end-to-end without spinning up the framework.
"""

from __future__ import annotations

import pathlib

import pytest

from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig


def _make_config(
    tmp_path: pathlib.Path,
    *,
    build_max_concurrent: int | None = None,
) -> SuitTaskConfig:
    return SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "manifests",
        raw_partition_dir=tmp_path / "partition" / "raw",
        partition_dir=tmp_path / "partition",
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
        build_max_concurrent=build_max_concurrent,
    )


def _phases(task: SuitTask):
    """Materialise get_phases() into a dict keyed by phase_id.

    Skips the test if dynamic_runner isn't importable; the topology
    is declared with framework types so `get_phases()` can't run
    without it.
    """
    pytest.importorskip("dynamic_runner.task_protocol")
    return {p.phase_id: p for p in task.get_phases()}


def test_get_phases_returns_single_build_phase(tmp_path: pathlib.Path) -> None:
    """All build-shaped tasks (toolchain, common_dep, variant) live in
    one ``phase_build``; nix's daemon serializes shared deps via its
    build lock so an explicit phase 2 → phase 3 boundary isn't needed.
    Phase 1a (partition) + 1b (merge) run inline on the primary."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert set(phases.keys()) == {"phase_build"}


def test_phase_build_has_no_dependencies(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["phase_build"].depends_on == ()


def test_phase1a_phase1b_no_longer_dispatched(tmp_path: pathlib.Path) -> None:
    """Phase 1a (partition) + 1b (merge) used to dispatch to secondaries
    but now run inline on the primary as part of job-list creation —
    secondaries have empty /nix/stores and can't walk drv graphs."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert "phase1a" not in phases
    assert "phase1b" not in phases


def test_phase_build_carries_all_four_task_types(
    tmp_path: pathlib.Path,
) -> None:
    """All four build-shaped types (toolchain, toolchain_validate,
    common_dep, variant) live in the single phase_build, all routed
    to build_worker. ``toolchain_validate`` is the default fetch-only
    counterpart to ``toolchain``; only one of the two is emitted per
    submit, controlled by ``--allow-toolchain-build``."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    types = phases["phase_build"].types
    type_ids = {t.type_id for t in types}
    assert type_ids == {
        "toolchain",
        "toolchain_validate",
        "common_dep",
        "variant",
    }
    for t in types:
        assert (
            t.worker_module
            == "compiler_suit_runner.workers.build_worker"
        )


def test_estimate_memory_returns_constant(tmp_path: pathlib.Path) -> None:
    """Memory budgeting is disabled: estimate_memory returns 1 byte
    for any item, so the framework's resource scheduler is effectively
    bypassed and concurrency is bounded only by ``--jobs N``."""
    task = SuitTask(_make_config(tmp_path))
    assert task.estimate_memory(None) == 1
    assert task.estimate_memory(object()) == 1


def test_build_max_concurrent_unset_leaves_types_uncapped(
    tmp_path: pathlib.Path,
) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    for phase in phases.values():
        for t in phase.types:
            assert getattr(t, "max_concurrent", None) is None


def test_build_max_concurrent_caps_only_build_types(
    tmp_path: pathlib.Path,
) -> None:
    task = SuitTask(_make_config(tmp_path, build_max_concurrent=3))
    phases = _phases(task)
    capped = {"toolchain", "common_dep", "variant"}
    for phase in phases.values():
        for t in phase.types:
            cap = getattr(t, "max_concurrent", None)
            if t.type_id in capped:
                assert cap == 3, t.type_id
            else:
                assert cap is None, t.type_id

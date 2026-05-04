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


def test_get_phases_returns_two_build_phases(tmp_path: pathlib.Path) -> None:
    """Phase 1a (partition) + 1b (merge) are job-list creation steps now
    handled inline on the primary; only phase 2 (toolchain / common-dep)
    + phase 3 (variant) builds dispatch to secondaries."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert set(phases.keys()) == {"phase2", "phase3"}


def test_phase_dependency_chain(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["phase2"].depends_on == ()
    assert phases["phase3"].depends_on == ("phase2",)


def test_phase1a_phase1b_no_longer_dispatched(tmp_path: pathlib.Path) -> None:
    """Phase 1a (partition) + 1b (merge) used to dispatch to secondaries
    but now run inline on the primary as part of job-list creation —
    secondaries have empty /nix/stores and can't walk drv graphs."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert "phase1a" not in phases
    assert "phase1b" not in phases


def test_phase2_has_toolchain_and_common_dep_types(
    tmp_path: pathlib.Path,
) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    type_ids = {t.type_id for t in phases["phase2"].types}
    assert type_ids == {"toolchain", "common_dep"}
    # Both share the same build worker module.
    for t in phases["phase2"].types:
        assert (
            t.worker_module
            == "compiler_suit_runner.workers.build_worker"
        )


def test_phase3_has_variant_type(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    types = phases["phase3"].types
    assert len(types) == 1
    assert types[0].type_id == "variant"
    assert (
        types[0].worker_module
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

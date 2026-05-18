"""Tests for :meth:`SuitTask.get_phases`.

The phase graph is what the dynamic_runner framework consumes to
schedule the run; every dependency edge is load-bearing. These tests
validate the post-rename ``build_compilers -> matrix_eval -> build``
topology end-to-end without spinning up the framework.

The ``dependency_graph`` step (plan's phase 3) is intentionally NOT a
framework PhaseSpec — it runs primary-only via
:class:`_MatrixEvalQuiesceWatcher` invoking the dependency_graph
worker as a subprocess. The test below pins that absence so a future
revert doesn't silently turn it into a framework dispatch.
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


def test_get_phases_returns_build_compilers_matrix_eval_and_build(
    tmp_path: pathlib.Path,
) -> None:
    """Three phases are declared: ``build_compilers`` (optional, when
    --build-compilers is on), ``matrix_eval`` (distributed eval),
    ``build`` (per-binary common_dep + variant). ``matrix_eval``
    depends on ``build_compilers`` and ``build`` depends on
    ``matrix_eval`` via explicit framework edges. The
    ``dependency_graph`` step is primary-only and not part of this
    graph."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert set(phases.keys()) == {"build_compilers", "matrix_eval", "build"}


def test_dependency_graph_not_a_framework_phase(tmp_path: pathlib.Path) -> None:
    """Pin the design decision: dependency_graph runs primary-only as
    a subprocess; never appears as a PhaseSpec."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert "dependency_graph" not in phases


def test_matrix_eval_depends_on_build_compilers(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["matrix_eval"].depends_on == ("build_compilers",)
    assert phases["build_compilers"].depends_on == ()


def test_build_depends_on_matrix_eval(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["build"].depends_on == ("matrix_eval",)


def test_matrix_eval_phase_carries_eval_task_type(
    tmp_path: pathlib.Path,
) -> None:
    """``matrix_eval`` declares a single ``eval`` task type routed to
    ``compiler_suit_runner.workers.build_worker`` (the unified entry
    that sniffs item_class and dispatches to the eval worker)."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    types = phases["matrix_eval"].types
    assert len(types) == 1
    assert types[0].type_id == "eval"
    assert (
        types[0].worker_module
        == "compiler_suit_runner.workers.build_worker"
    )


def test_build_compilers_phase_carries_one_task_type(
    tmp_path: pathlib.Path,
) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    types = phases["build_compilers"].types
    assert len(types) == 1
    assert types[0].type_id == "build_compilers"
    assert (
        types[0].worker_module
        == "compiler_suit_runner.workers.build_worker"
    )


def test_build_phase_carries_validate_common_dep_variant_types(
    tmp_path: pathlib.Path,
) -> None:
    """The ``build`` phase carries toolchain_validate (rarely emitted),
    common_dep, and variant types, all routed to build_worker."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    types = phases["build"].types
    type_ids = {t.type_id for t in types}
    assert type_ids == {
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


def test_build_max_concurrent_caps_only_build_heavy_types(
    tmp_path: pathlib.Path,
) -> None:
    task = SuitTask(_make_config(tmp_path, build_max_concurrent=3))
    phases = _phases(task)
    capped = {"build_compilers", "common_dep", "variant"}
    for phase in phases.values():
        for t in phase.types:
            cap = getattr(t, "max_concurrent", None)
            if t.type_id in capped:
                assert cap == 3, t.type_id
            else:
                assert cap is None, t.type_id

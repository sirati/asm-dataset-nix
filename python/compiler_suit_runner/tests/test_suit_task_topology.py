"""Tests for :meth:`SuitTask.get_phases`.

The phase graph is what the dynamic_runner framework consumes to
schedule the run; every dependency edge is load-bearing. These tests
validate the post-rename ``build_compilers -> matrix_eval -> build``
topology end-to-end without spinning up the framework.

The ``dependency_graph`` step (plan's phase 3) is a first-class
framework PhaseSpec depending on ``matrix_eval``; the ``build`` phase
is spawned at runtime by the primary from
:meth:`SuitTask.on_phase_end` via ``primary_handle.spawn_tasks``. The
tests below pin that topology so a future change doesn't silently
alter the dependency edges.
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


def test_get_phases_returns_four_phases(
    tmp_path: pathlib.Path,
) -> None:
    """Four phases are declared: ``build_compilers`` (optional, when
    --build-compilers is on), ``matrix_eval`` (distributed eval),
    ``dependency_graph`` (per-binary streaming planner), ``build``
    (per-binary common_dep + variant). PH-A promoted dep_graph from a
    primary-only subprocess to a first-class framework phase."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert set(phases.keys()) == {
        "build_compilers", "matrix_eval", "dependency_graph", "build",
    }


def test_dependency_graph_is_a_framework_phase(tmp_path: pathlib.Path) -> None:
    """Post-PH-A: dependency_graph is a first-class PhaseSpec with
    depends_on=(matrix_eval,) so the CRDT activates each dep_graph
    task atomically with the matching matrix_eval TaskCompleted."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert "dependency_graph" in phases
    assert phases["dependency_graph"].depends_on == ("matrix_eval",)


def test_matrix_eval_depends_on_build_compilers(tmp_path: pathlib.Path) -> None:
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["matrix_eval"].depends_on == ("build_compilers",)
    assert phases["build_compilers"].depends_on == ()


def test_build_depends_on_dependency_graph(tmp_path: pathlib.Path) -> None:
    """PH-A: build runs after dep_graph (was: after matrix_eval)."""
    task = SuitTask(_make_config(tmp_path))
    phases = _phases(task)
    assert phases["build"].depends_on == ("dependency_graph",)


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


# ---------------------------------------------------------------------------
# build_worker_command_args wiring
# ---------------------------------------------------------------------------


def test_build_worker_command_args_dep_graph_has_dataset_output_dir(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: PH-A initially routed type_id=dep_graph through a
    bespoke argv branch that omitted --dataset-output-dir, which the
    build_worker.py argparse declares as required=True. The worker
    subprocess died with exit code 2 on type-shift to dep_graph,
    surfacing as 'Disconnected before Ready' on the cluster
    (run_20260521_200133 secondary-0 worker_0). Pin the fix: dep_graph
    SHARES the standard argv shape so the subprocess can start."""
    from argparse import Namespace
    task = SuitTask(_make_config(tmp_path))
    argv = task.build_worker_command_args(
        "dep_graph",
        Namespace(),
        source_dir=tmp_path,
        output_dir=tmp_path,
        skip_existing=False,
    )
    assert "--dataset-output-dir" in argv, argv
    assert "--flake-ref" in argv, argv
    # The matrix-eval-out-dir flag is what makes dep_graph dispatch
    # different from other build types — assert it's also threaded.
    # (None when config has no matrix_eval_out_dir; our _make_config
    # leaves it absent, so we just check the rest of the argv is sane.)


def test_build_worker_command_args_dep_graph_includes_matrix_eval_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """When the config carries a matrix_eval_out_dir, dep_graph argv
    threads --matrix-eval-out-dir so the worker can read the archive
    + sidecar that eval_worker produced. Same flag also threaded to
    type_id=eval (no change there)."""
    from argparse import Namespace
    from compiler_suit_runner.suit_task import SuitTaskConfig
    config = SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "manifests",
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
        matrix_eval_out_dir=tmp_path / "_matrix_eval",
    )
    task = SuitTask(config)
    argv_dep = task.build_worker_command_args(
        "dep_graph",
        Namespace(),
        source_dir=tmp_path,
        output_dir=tmp_path,
        skip_existing=False,
    )
    assert "--matrix-eval-out-dir" in argv_dep, argv_dep
    assert str(tmp_path / "_matrix_eval") in argv_dep, argv_dep
    # Sanity: eval keeps the flag too.
    argv_eval = task.build_worker_command_args(
        "eval",
        Namespace(),
        source_dir=tmp_path,
        output_dir=tmp_path,
        skip_existing=False,
    )
    assert "--matrix-eval-out-dir" in argv_eval, argv_eval

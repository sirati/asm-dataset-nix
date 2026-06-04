"""Tests for :meth:`SuitTask.discover_items`.

Verifies the manifest-scan loop emits one item per manifest, that
each item is tagged with the right (phase_id, type_id, affinity_id),
and that corrupt or unrecognised manifests are skipped without
killing the run.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import tempfile

import pytest

from compiler_suit_runner.manifest_gen import (
    make_build_common_dep_header,
    make_build_compilers_header,
    make_build_variant_header,
    write_manifest,
)
from compiler_suit_runner.partition import VariantSpec
from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig


# ---------------------------------------------------------------------------
# Tmpfs override (mirrors test_manifest_gen.py: ftruncate to GiB-scale sizes
# is fine on tmpfs and works on every common filesystem too, but tmpfs is
# fastest).


def _tmpfs_basetemp() -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(pathlib.Path(xdg))
    candidates.append(pathlib.Path("/dev/shm"))
    for candidate in candidates:
        if not candidate.is_dir() or not os.access(candidate, os.W_OK):
            continue
        probe = candidate / f"discover_probe_{os.getpid()}"
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        except OSError:
            continue
        try:
            try:
                os.ftruncate(fd, 8 * 1024 * 1024 * 1024)
            except OSError:
                continue
            return candidate
        finally:
            os.close(fd)
            try:
                probe.unlink()
            except OSError:
                pass
    return None


@pytest.fixture(scope="session", autouse=True)
def _tmp_path_on_tmpfs(tmp_path_factory: pytest.TempPathFactory):
    base = _tmpfs_basetemp()
    if base is None:
        pytest.skip(
            "no tmpfs available for discover_items tests"
            " (need /dev/shm or $XDG_RUNTIME_DIR)",
            allow_module_level=True,
        )
        return
    new_basetemp = pathlib.Path(
        tempfile.mkdtemp(prefix="discover_", dir=str(base))
    )
    tmp_path_factory._basetemp = new_basetemp  # type: ignore[attr-defined]
    yield


# ---------------------------------------------------------------------------
# Helpers


def _make_config(
    tmp_path: pathlib.Path,
    *,
    per_binary_metadata: dict[str, dict] | None = None,
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
        per_binary_metadata=per_binary_metadata,
    )


_TC_AGG = "/nix/store/aaaa-toolchains.drv"


def _per_binary_metadata() -> dict[str, dict]:
    """Two binaries' worth of phase-2/3 preflight metadata."""
    return {
        "hello": {
            "archs": ["x86_64", "aarch64"],
            "variant_sample": 64,
            "variant_seed": "seed-hello",
            "tier": 1,
            "toolchain_aggregate_drv": _TC_AGG,
        },
        "busybox": {
            "archs": ["x86_64"],
            "variant_sample": 32,
            "variant_seed": "seed-busybox",
            "tier": 1,
            "toolchain_aggregate_drv": _TC_AGG,
        },
    }


def _variant(
    pkg: str,
    arch: str,
    *,
    compiler_id: str = "gcc15",
    suffix: str = "O2",
    tier: int = 1,
) -> VariantSpec:
    label = f"{pkg}-{arch}-{compiler_id}-{suffix}"
    return {
        "label": label,
        "drv": f"/nix/store/{label}.drv",
        "variant_dir": label,
        "metadata_name": f"{label}.json",
        "compiler_id": compiler_id,
        "compiler_family": "gcc",
        "compiler_version": "15.2.0",
        "optimization": suffix,
        "flag_set": "baseline",
        "hardening": "default",
        "sanitizer": "san-off",
        "march": "march-default",
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Tests


def test_discover_items_classifies_each_manifest(tmp_path: pathlib.Path) -> None:
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)

    # build_compilers + build_common_dep + build_variant headers
    # cover the build-phase classification paths exercised by
    # discover_items today; matrix_eval headers land in their own
    # phase and would clutter the per-phase assertions below.
    write_manifest(
        config.manifest_dir,
        make_build_compilers_header("x86_64-linux", "x86_64", "gcc15"),
    )
    write_manifest(
        config.manifest_dir,
        make_build_common_dep_header("/nix/store/glibc.drv", "glibc"),
    )
    write_manifest(
        config.manifest_dir,
        make_build_variant_header(
            _variant("hello", "x86_64"), "x86_64-linux",
            toolchain_task_id="x86_64-linux__x86_64__gcc15",
        ),
    )

    task = SuitTask(config)
    items = list(task.discover_items())
    by_phase: dict[str, list] = {}
    for item in items:
        by_phase.setdefault(item.phase_id, []).append(item)

    assert set(by_phase.keys()) == {"build_compilers", "build"}

    build_types = {item.type_id for item in by_phase["build"]}
    assert build_types == {"common_dep", "variant"}
    build_compilers = next(
        item for item in by_phase["build_compilers"]
        if item.type_id == "build_compilers"
    )
    assert build_compilers.affinity_id == "gcc15-x86_64"
    common_dep = next(
        item for item in by_phase["build"] if item.type_id == "common_dep"
    )
    assert common_dep.affinity_id is None

    variant = next(
        item for item in by_phase["build"] if item.type_id == "variant"
    )
    assert variant.affinity_id == "gcc15-x86_64"
    # TaskInfo.payload now carries the full ManifestHeader dict so
    # workers can read it directly off the comm fd via FR-3.
    assert variant.payload["item_class"] == "build_variant"
    assert variant.payload["payload"]["pkg"] == "hello"


def test_discover_items_yields_size_from_manifest(tmp_path: pathlib.Path) -> None:
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(
        config.manifest_dir,
        make_build_common_dep_header("/nix/store/glibc.drv", "glibc"),
    )
    task = SuitTask(config)
    items = list(task.discover_items())
    assert len(items) == 1
    # Memory budgeting is disabled — every header.size is 0.
    assert items[0].size == 0


def test_discover_items_skips_unreadable_manifests(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)

    # One good manifest...
    write_manifest(
        config.manifest_dir,
        make_build_common_dep_header("/nix/store/glibc.drv", "glibc"),
    )
    # ...and one corrupt one (junk text in a .json file).
    bad = config.manifest_dir / "bad.json"
    bad.write_text("not json at all")

    task = SuitTask(config)
    items = list(task.discover_items())
    assert len(items) == 1
    assert items[0].type_id == "common_dep"


def test_discover_items_missing_manifest_dir_yields_nothing(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    # Don't create manifest_dir.
    task = SuitTask(config)
    assert list(task.discover_items()) == []


def test_discover_items_skips_dotfiles_and_underscored(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(
        config.manifest_dir,
        make_build_common_dep_header("/nix/store/glibc.drv", "glibc"),
    )
    (config.manifest_dir / ".hidden.json").write_text("{}")
    (config.manifest_dir / "_meta.json").write_text("{}")

    task = SuitTask(config)
    items = list(task.discover_items())
    assert len(items) == 1


def test_discover_items_strips_task_depends_on_when_disabled(
    tmp_path: pathlib.Path,
) -> None:
    """``disable_task_deps=True`` zeros out ``task_depends_on`` for every item.

    The variant header normally carries its toolchain prerequisite in
    ``build_compilers_depends_on``, which the runner wraps into a
    cross-phase ``TaskDep`` on the emitted ``task_depends_on``; with the
    workaround flag set the framework never sees those edges, and
    PendingPool.extend() accepts the variant even when its toolchain dep
    is in-flight.
    """
    base_config = _make_config(tmp_path)
    config = dataclasses.replace(base_config, disable_task_deps=True)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)

    write_manifest(
        config.manifest_dir,
        make_build_compilers_header("x86_64-linux", "x86_64", "gcc15"),
    )
    write_manifest(
        config.manifest_dir,
        make_build_variant_header(
            _variant("hello", "x86_64"), "x86_64-linux",
            toolchain_task_id="x86_64-linux__x86_64__gcc15",
        ),
    )

    items = list(SuitTask(config).discover_items())
    assert items, "expected at least one item"
    assert all(item.task_depends_on == () for item in items)

    # Sanity: with the flag OFF, the variant retains its toolchain dep
    # so this test can't pass tautologically.
    items_with_deps = list(SuitTask(base_config).discover_items())
    variant = next(item for item in items_with_deps if item.type_id == "variant")
    assert variant.task_depends_on  # non-empty


# ---------------------------------------------------------------------------
# Phase 2 (matrix_eval) + phase 3 (dependency_graph): JSON-free, built
# in-memory from config.per_binary_metadata.


def test_discover_items_matrix_eval_one_per_binary_no_json(
    tmp_path: pathlib.Path,
) -> None:
    """One matrix_eval TaskInfo per binary, built from
    ``per_binary_metadata`` — and NOT a single byte of JSON is written
    or read for phase 2/3 (the manifest dir is never even created)."""
    config = _make_config(tmp_path, per_binary_metadata=_per_binary_metadata())
    # Deliberately do NOT create manifest_dir: phase 2/3 must not touch
    # it at all.
    items = list(SuitTask(config).discover_items())

    matrix_eval = [i for i in items if i.phase_id == "matrix_eval"]
    # task_id is the bare binary (phase-local; the MATRIX_EVAL phase
    # disambiguates), NOT "matrix_eval__<binary>".
    assert {i.task_id for i in matrix_eval} == {"hello", "busybox"}
    assert all(i.type_id == "eval" for i in matrix_eval)
    # No matrix_eval task carries a dep (build_compilers wiring is TODO).
    assert all(i.task_depends_on == () for i in matrix_eval)

    # JSON-free: discover_items wrote nothing to the manifest dir.
    assert not config.manifest_dir.exists()


def test_discover_items_single_dependency_graph_depends_on_all_matrix_eval(
    tmp_path: pathlib.Path,
) -> None:
    """Exactly ONE dependency_graph TaskInfo (task_id =
    "dependency_graph", affinity None) whose task_depends_on is every
    matrix_eval task id in the run."""
    config = _make_config(tmp_path, per_binary_metadata=_per_binary_metadata())
    items = list(SuitTask(config).discover_items())

    dep_graphs = [i for i in items if i.phase_id == "dependency_graph"]
    assert len(dep_graphs) == 1
    dg = dep_graphs[0]
    assert dg.task_id == "dependency_graph"
    assert dg.type_id == "dep_graph"
    assert dg.affinity_id is None

    matrix_eval_ids = {
        i.task_id for i in items if i.phase_id == "matrix_eval"
    }
    # The dependency_graph task's deps are CROSS-phase TaskDeps: each
    # names a matrix_eval prerequisite by its bare-binary task_id AND
    # its phase ("matrix_eval"), since it lives in a different phase. A
    # bare string would resolve to the dependency_graph task's own phase
    # and be flagged a missing dep.
    assert {(d.task_id, str(d.phase_id)) for d in dg.task_depends_on} == {
        (binary, "matrix_eval") for binary in matrix_eval_ids
    }
    assert len(dg.task_depends_on) == len(matrix_eval_ids)
    # Payload carries the toolchain aggregate (the planner anchor drv)
    # but NO single binary.
    assert dg.payload["payload"]["toolchain_aggregate_drv"] == _TC_AGG
    assert "binary" not in dg.payload["payload"]


def test_discover_items_no_per_binary_metadata_yields_no_phase23(
    tmp_path: pathlib.Path,
) -> None:
    """With no per_binary_metadata (e.g. secondary config), phase 2/3
    yield nothing — and still no JSON is touched for them."""
    config = _make_config(tmp_path, per_binary_metadata=None)
    items = list(SuitTask(config).discover_items())
    assert [i for i in items if i.phase_id == "matrix_eval"] == []
    assert [i for i in items if i.phase_id == "dependency_graph"] == []


def test_discover_items_phase23_coexist_with_build_manifests(
    tmp_path: pathlib.Path,
) -> None:
    """In-memory phase 2/3 items and disk-based build manifests are
    both yielded from a single discover_items pass."""
    config = _make_config(tmp_path, per_binary_metadata=_per_binary_metadata())
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(
        config.manifest_dir,
        make_build_compilers_header("x86_64-linux", "x86_64", "gcc15"),
    )
    items = list(SuitTask(config).discover_items())
    phases = {i.phase_id for i in items}
    assert "matrix_eval" in phases
    assert "dependency_graph" in phases
    assert "build_compilers" in phases
    # Still exactly one dependency_graph task.
    assert len([i for i in items if i.phase_id == "dependency_graph"]) == 1


def test_discover_items_ignores_stale_matrix_eval_json(
    tmp_path: pathlib.Path,
) -> None:
    """A stale matrix_eval / dependency_graph JSON manifest from a
    pre-rip-out run must be ignored (not double-emitted) — phase 2/3
    come only from in-memory metadata."""
    from compiler_suit_runner.manifest_gen import make_matrix_eval_header

    config = _make_config(tmp_path, per_binary_metadata=_per_binary_metadata())
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    # Drop a stale matrix_eval JSON on disk.
    write_manifest(
        config.manifest_dir,
        make_matrix_eval_header(
            "ghost", "x86_64-linux", archs=["x86_64"],
            toolchain_aggregate_drv=_TC_AGG,
        ),
    )
    items = list(SuitTask(config).discover_items())
    matrix_eval_ids = {
        i.task_id for i in items if i.phase_id == "matrix_eval"
    }
    # Only the in-memory binaries (bare-binary task ids) — the stale
    # "ghost" JSON is ignored.
    assert matrix_eval_ids == {"hello", "busybox"}

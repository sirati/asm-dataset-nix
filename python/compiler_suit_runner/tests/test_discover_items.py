"""Tests for :meth:`SuitTask.discover_items`.

Verifies the manifest-scan loop emits one item per manifest, that
each item is tagged with the right (phase_id, type_id, affinity_id),
and that corrupt or unrecognised manifests are skipped without
killing the run.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from compiler_suit_runner.manifest_gen import (
    make_common_dep_header,
    make_merge_header,
    make_partition_shard_header,
    make_toolchain_header,
    make_variant_header,
    write_manifest,
)
from compiler_suit_runner.partition import Shard, VariantSpec
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


def _make_config(tmp_path: pathlib.Path) -> SuitTaskConfig:
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
    )


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
        "tarball_name": f"{label}.tar.zst",
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

    write_manifest(
        config.manifest_dir,
        make_partition_shard_header(
            Shard(
                pkg="hello",
                arch="x86_64",
                variants=(_variant("hello", "x86_64"),),
            )
        ),
    )
    write_manifest(config.manifest_dir, make_merge_header())
    write_manifest(
        config.manifest_dir,
        make_toolchain_header("x86_64-linux", "x86_64", "gcc15"),
    )
    write_manifest(
        config.manifest_dir,
        make_common_dep_header("/nix/store/glibc.drv", "glibc"),
    )
    write_manifest(
        config.manifest_dir,
        make_variant_header(_variant("hello", "x86_64"), "x86_64-linux"),
    )

    task = SuitTask(config)
    items = list(task.discover_items())
    by_phase: dict[str, list] = {}
    for item in items:
        by_phase.setdefault(item.phase_id, []).append(item)

    assert set(by_phase.keys()) == {
        "phase1a",
        "phase1b",
        "phase_build",
    }

    # Spot-check classification.
    assert by_phase["phase1a"][0].type_id == "partition"
    assert by_phase["phase1a"][0].affinity_id is None
    assert by_phase["phase1b"][0].type_id == "merge"

    build_types = {item.type_id for item in by_phase["phase_build"]}
    assert build_types == {"toolchain", "common_dep", "variant"}
    toolchain = next(
        item for item in by_phase["phase_build"] if item.type_id == "toolchain"
    )
    assert toolchain.affinity_id == "gcc15-x86_64"
    common_dep = next(
        item for item in by_phase["phase_build"] if item.type_id == "common_dep"
    )
    assert common_dep.affinity_id is None

    variant = next(
        item for item in by_phase["phase_build"] if item.type_id == "variant"
    )
    assert variant.affinity_id == "gcc15-x86_64"
    # TaskInfo.payload now carries the full ManifestHeader dict so
    # workers can read it directly off the comm fd via FR-3.
    assert variant.payload["item_class"] == "phase3_variant"
    assert variant.payload["payload"]["pkg"] == "hello"


def test_discover_items_yields_size_from_manifest(tmp_path: pathlib.Path) -> None:
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(config.manifest_dir, make_merge_header())
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
    write_manifest(config.manifest_dir, make_merge_header())
    # ...and one corrupt one (junk text in a .json file).
    bad = config.manifest_dir / "bad.json"
    bad.write_text("not json at all")

    task = SuitTask(config)
    items = list(task.discover_items())
    assert len(items) == 1
    assert items[0].type_id == "merge"


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
    write_manifest(config.manifest_dir, make_merge_header())
    (config.manifest_dir / ".hidden.json").write_text("{}")
    (config.manifest_dir / "_meta.json").write_text("{}")

    task = SuitTask(config)
    items = list(task.discover_items())
    assert len(items) == 1

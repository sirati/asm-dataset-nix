"""Unit tests for ``compiler_suit_runner.manifest_gen``."""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    ManifestSet,
    emit_all_manifests,
    make_common_dep_header,
    make_merge_header,
    make_partition_shard_header,
    make_toolchain_header,
    make_variant_header,
    read_manifest,
    write_manifest,
)
from compiler_suit_runner.partition import Shard, VariantSpec


# ---------------------------------------------------------------------------
# Helpers


def _variant(
    pkg: str,
    arch: str,
    suffix: str,
    *,
    compiler_id: str = "gcc15",
    tier: int = 2,
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
        "optimization": "O2",
        "flag_set": "baseline",
        "hardening": "default",
        "sanitizer": "san-off",
        "march": "march-default",
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Header constructors


def test_partition_shard_header_encodes_phase():
    variants = (
        _variant("hello", "x86_64", "O0"),
        _variant("hello", "x86_64", "O2"),
        _variant("hello", "x86_64", "O3"),
    )
    shard = Shard(pkg="hello", arch="x86_64", variants=variants)
    header = make_partition_shard_header(shard)

    assert header.item_class == "phase1a_partition"
    assert header.name == "hello__x86_64"
    assert header.size == 0
    assert header.payload["pkg"] == "hello"
    assert header.payload["arch"] == "x86_64"
    assert len(header.payload["variants"]) == 3
    # variants payload is plain dict-shaped (JSON-friendly).
    for v in header.payload["variants"]:
        assert isinstance(v, dict)
        assert v["pkg"] == "hello"
        assert v["arch"] == "x86_64"


def test_merge_header():
    h = make_merge_header()
    assert h.item_class == "phase1b_merge"
    assert h.name == "phase1b_merge"
    assert h.payload == {}
    assert h.size == 0


def test_toolchain_header_without_drv():
    h = make_toolchain_header(
        "x86_64-linux", "aarch64", "gcc14"
    )
    assert h.item_class == "phase2_toolchain"
    assert h.name == "toolchain__aarch64__gcc14"
    assert h.payload == {
        "sys": "x86_64-linux",
        "arch": "aarch64",
        "compiler_label": "gcc14",
        "attr": "_crossToolchainMap.x86_64-linux.aarch64.gcc14",
    }
    assert "drv" not in h.payload
    assert h.size == 0


def test_toolchain_header_with_drv():
    h = make_toolchain_header(
        "x86_64-linux", "armv7l", "gcc11", drv="/nix/store/tc.drv"
    )
    assert h.payload["drv"] == "/nix/store/tc.drv"
    assert h.payload["attr"] == (
        "_crossToolchainMap.x86_64-linux.armv7l.gcc11"
    )


def test_common_dep_header():
    h = make_common_dep_header("/nix/store/glibc-x.drv", "glibc")
    assert h.item_class == "phase2_common_dep"
    assert h.name == "common_dep__glibc"
    assert h.payload == {
        "drv": "/nix/store/glibc-x.drv",
        "label": "glibc",
        "attr": "/nix/store/glibc-x.drv",
    }
    assert h.size == 0


def test_variant_header_payload():
    v = _variant("hello", "x86_64", "O2", tier=1)
    h = make_variant_header(v, "x86_64-linux")
    assert h.item_class == "phase3_variant"
    assert h.name == v["label"]
    assert h.size == 0
    assert h.payload["sys"] == "x86_64-linux"
    assert h.payload["pkg"] == "hello"
    assert h.payload["arch"] == "x86_64"
    assert h.payload["label"] == v["label"]
    assert h.payload["drv"] == v["drv"]
    assert h.payload["tarball_name"] == v["tarball_name"]
    assert h.payload["compiler_id"] == v["compiler_id"]
    assert h.payload["tier"] == 1
    assert h.payload["attr"] == (
        f"dataset.x86_64-linux.hello.x86_64.{v['label']}"
    )


# ---------------------------------------------------------------------------
# write_manifest / read_manifest


def test_write_manifest_writes_compact_json_file(
    tmp_path: pathlib.Path,
):
    h = make_merge_header()
    written = write_manifest(tmp_path, h)
    assert written == tmp_path / "phase1b_merge.json"
    # The on-disk file is the JSON document only — no longer padded
    # to ``h.size`` (see the dispatch hot-path note in
    # manifest_gen.write_manifest).
    stat = os.stat(written)
    assert 0 < stat.st_size <= 4096

    # Round-trip via read_manifest.
    loaded = read_manifest(written)
    assert loaded == h


def test_write_manifest_creates_target_dir(tmp_path: pathlib.Path):
    nested = tmp_path / "a" / "b" / "c"
    h = make_merge_header()
    written = write_manifest(nested, h)
    assert written.parent == nested
    assert nested.exists()


def test_write_manifest_no_tmp_leftovers(tmp_path: pathlib.Path):
    h = make_partition_shard_header(
        Shard(pkg="hello", arch="x86_64", variants=())
    )
    write_manifest(tmp_path, h)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_write_manifest_payload_round_trip(tmp_path: pathlib.Path):
    v = _variant("sqlite", "x86_64", "O2", tier=2)
    h = make_variant_header(v, "x86_64-linux")
    write_manifest(tmp_path, h)
    loaded = read_manifest(tmp_path / f"{v['label']}.json")
    assert loaded.payload == h.payload
    assert loaded.size == h.size
    assert loaded.item_class == "phase3_variant"


def test_read_manifest_handles_legacy_sparse_padded_file(
    tmp_path: pathlib.Path,
):
    # Older copies of write_manifest padded the file with ftruncate
    # (sparse zero-fill) to a multi-GiB tail. New write_manifest
    # doesn't, but read_manifest must still load legacy files —
    # synthesise the layout and confirm parse.
    h = make_merge_header()
    target = write_manifest(tmp_path, h)
    legacy_size = 2 * 1024 * 1024 * 1024  # 2 GiB sparse tail
    fd = os.open(target, os.O_RDWR)
    try:
        os.ftruncate(fd, legacy_size)
    finally:
        os.close(fd)
    assert os.stat(target).st_size == legacy_size
    loaded = read_manifest(target)
    assert loaded == h


def test_read_manifest_rejects_non_object_top_level(
    tmp_path: pathlib.Path,
):
    target = tmp_path / "junk.json"
    target.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        read_manifest(target)


def test_read_manifest_rejects_missing_field(tmp_path: pathlib.Path):
    target = tmp_path / "bad.json"
    target.write_text(
        json.dumps(
            {
                "item_class": "phase1b_merge",
                "name": "x",
                "size": 100,
                # payload missing
            }
        )
    )
    # Pad so apparent size matches size field.
    fd = os.open(target, os.O_RDWR)
    try:
        os.ftruncate(fd, 100)
    finally:
        os.close(fd)
    with pytest.raises(ValueError):
        read_manifest(target)


# ---------------------------------------------------------------------------
# emit_all_manifests


def _build_full_input():
    """2 archs × 2 packages × 3 variants = 12 variants in 4 shards."""
    variants: list[VariantSpec] = []
    for pkg in ("hello", "sqlite"):
        for arch in ("x86_64", "aarch64"):
            for suf in ("O0", "O2", "O3"):
                tier = 1 if pkg == "hello" else 2
                variants.append(
                    _variant(pkg, arch, suf, tier=tier)
                )
    toolchain_specs = [
        ("x86_64", "gcc14"),
        ("x86_64", "gcc15"),
        ("aarch64", "gcc14"),
        ("aarch64", "gcc15"),
    ]
    common_deps = [
        ("/nix/store/glibc.drv", "glibc"),
        ("/nix/store/zlib.drv", "zlib"),
    ]
    return variants, toolchain_specs, common_deps


def test_emit_all_manifests_full_shape(tmp_path: pathlib.Path):
    variants, toolchain_specs, common_deps = _build_full_input()

    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
    )
    assert isinstance(result, ManifestSet)
    assert result.target_dir == tmp_path

    grouped = result.by_class
    # Phase 1a + 1b are now computed inline on the primary (job-list
    # creation) and don't emit dispatch manifests.
    assert len(grouped["phase1a_partition"]) == 0
    assert len(grouped["phase1b_merge"]) == 0
    assert len(grouped["phase2_toolchain"]) == 4
    assert len(grouped["phase2_common_dep"]) == 2
    assert len(grouped["phase3_variant"]) == 12

    # Every header has a corresponding file; on-disk size is just the
    # JSON document (the framework's pre-flight hashing pass reads
    # every byte of every TaskInfo path, so manifests are no longer
    # padded to ``header.size`` — that's still propagated via the
    # JSON ``size`` field for memory-budget ordering).
    for header in result.headers:
        path = tmp_path / f"{header.name}.json"
        assert path.exists()
        assert 0 < os.stat(path).st_size <= 4096
        # JSON content round-trips.
        loaded = read_manifest(path)
        assert loaded == header


def test_emit_all_manifests_iteration_order(tmp_path: pathlib.Path):
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
    )
    classes = [h.item_class for h in result.headers]
    # Phase 1a + 1b no longer dispatched; remaining phases keep order.
    expected_order = [
        "phase2_toolchain",
        "phase2_common_dep",
        "phase3_variant",
    ]
    seen_order: list[str] = []
    for c in classes:
        if not seen_order or seen_order[-1] != c:
            seen_order.append(c)
    assert seen_order == expected_order


def test_emit_all_manifests_empty_inputs(tmp_path: pathlib.Path):
    """A degenerate case: no variants, no toolchains, no deps."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    grouped = result.by_class
    assert grouped["phase1a_partition"] == ()
    assert grouped["phase1b_merge"] == ()
    assert grouped["phase2_toolchain"] == ()
    assert grouped["phase2_common_dep"] == ()
    assert grouped["phase3_variant"] == ()


def test_manifest_set_by_class_includes_all_known_classes(
    tmp_path: pathlib.Path,
):
    """``by_class`` returns a tuple for every known ItemClass even when
    no items in that class are present, so iteration is KeyError-free."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    grouped = result.by_class
    expected_keys = {
        "phase1a_partition",
        "phase1b_merge",
        "phase2_toolchain",
        "phase2_common_dep",
        "phase3_variant",
    }
    assert set(grouped.keys()) == expected_keys
    for value in grouped.values():
        assert isinstance(value, tuple)


def test_emit_all_manifests_target_dir_created(tmp_path: pathlib.Path):
    target = tmp_path / "deeply" / "nested" / "manifests"
    assert not target.exists()
    emit_all_manifests(
        target_dir=target,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    assert target.is_dir()

"""Unit tests for ``compiler_suit_runner.manifest_gen``.

Sparse-file note: manifest files have apparent sizes equal to their
per-type memory budget (1-6 GiB). All common filesystems handle this
fine, but we still re-root tmp on tmpfs to keep tests fast and avoid
filling small disk-backed tmp dirs.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

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
from compiler_suit_runner.memory_budget import (
    MEMORY_FLOOR_BYTES,
    common_dep_memory_bytes,
    merge_memory_bytes,
    partition_shard_memory_bytes,
    toolchain_memory_bytes,
    variant_memory_bytes,
)
from compiler_suit_runner.partition import Shard, VariantSpec


# ---------------------------------------------------------------------------
# Tmpfs override


def _tmpfs_basetemp() -> pathlib.Path | None:
    """Pick a tmpfs-backed directory in which to root pytest's tmp tree.

    We probe (in order) ``$XDG_RUNTIME_DIR`` and ``/dev/shm``; the first
    one that exists, is writable, and supports a 1 PiB sparse ftruncate
    is used. Returns ``None`` if no such filesystem is available, in
    which case the tmpfs-dependent tests are skipped.
    """
    candidates: list[pathlib.Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(pathlib.Path(xdg))
    candidates.append(pathlib.Path("/dev/shm"))
    for candidate in candidates:
        if not candidate.is_dir() or not os.access(candidate, os.W_OK):
            continue
        # Probe with a 1 PiB sparse ftruncate; ext4 fails with EFBIG.
        probe = candidate / f"manifest_gen_probe_{os.getpid()}"
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        except OSError:
            continue
        try:
            try:
                os.ftruncate(fd, 1 << 50)
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
    """Re-root the per-session tmp tree on a tmpfs that supports the
    multi-petabyte sparse files this module emits.

    This patches the factory in place; tests that depend on tmp_path
    transparently get a tmpfs-backed directory.
    """
    base = _tmpfs_basetemp()
    if base is None:
        pytest.skip(
            "no tmpfs available for sparse-file manifest tests"
            " (need /dev/shm or $XDG_RUNTIME_DIR)",
            allow_module_level=True,
        )
        return
    new_basetemp = pathlib.Path(
        tempfile.mkdtemp(prefix="manifest_gen_", dir=str(base))
    )
    # Replace the factory's basetemp; subsequent tmp_path / tmp_path_factory
    # calls will allocate beneath this tmpfs root.
    tmp_path_factory._basetemp = new_basetemp  # type: ignore[attr-defined]
    yield


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
        "compiler_id": compiler_id,
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Header constructors


def test_partition_shard_header_encodes_phase_and_memory():
    variants = (
        _variant("hello", "x86_64", "O0"),
        _variant("hello", "x86_64", "O2"),
        _variant("hello", "x86_64", "O3"),
    )
    shard = Shard(pkg="hello", arch="x86_64", variants=variants)
    header = make_partition_shard_header(shard)

    assert header.item_class == "phase1a_partition"
    assert header.name == "hello__x86_64"
    assert header.size == partition_shard_memory_bytes()
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
    assert h.size == merge_memory_bytes()


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
    assert h.size == toolchain_memory_bytes()


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
    assert h.size == common_dep_memory_bytes()


def test_variant_header_tier1():
    v = _variant("hello", "x86_64", "O2", tier=1)
    h = make_variant_header(v, "x86_64-linux")
    assert h.item_class == "phase3_variant"
    assert h.name == v["label"]
    assert h.size == variant_memory_bytes("hello")
    assert h.size == 1 * 1024 * 1024 * 1024
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


def test_variant_header_tier2():
    v = _variant("sqlite", "aarch64", "O3", tier=2)
    h = make_variant_header(v, "aarch64-linux")
    assert h.size == variant_memory_bytes("sqlite")
    assert h.size == 2 * 1024 * 1024 * 1024
    assert h.payload["attr"] == (
        f"dataset.aarch64-linux.sqlite.aarch64.{v['label']}"
    )


def test_variant_header_tier3():
    v = _variant("coreutils", "x86_64", "O2", tier=3)
    h = make_variant_header(v, "x86_64-linux")
    assert h.size == variant_memory_bytes("coreutils")
    assert h.size == 4 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# write_manifest / read_manifest


def test_write_manifest_creates_sparse_file_with_correct_apparent_size(
    tmp_path: pathlib.Path,
):
    h = make_merge_header()
    written = write_manifest(tmp_path, h)
    assert written == tmp_path / "phase1b_merge.json"
    stat = os.stat(written)
    assert stat.st_size == h.size

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


def test_read_manifest_size_mismatch_raises(tmp_path: pathlib.Path):
    h = make_merge_header()
    target = write_manifest(tmp_path, h)
    # Truncate the file so its apparent size no longer matches the
    # encoded size in the JSON header.
    fd = os.open(target, os.O_RDWR)
    try:
        os.ftruncate(fd, 1024)
    finally:
        os.close(fd)
    with pytest.raises(ValueError):
        read_manifest(target)


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
    assert len(grouped["phase1a_partition"]) == 4
    assert len(grouped["phase1b_merge"]) == 1
    assert len(grouped["phase2_toolchain"]) == 4
    assert len(grouped["phase2_common_dep"]) == 2
    assert len(grouped["phase3_variant"]) == 12

    # Every header has a corresponding file with the right apparent size.
    for header in result.headers:
        path = tmp_path / f"{header.name}.json"
        assert path.exists()
        assert os.stat(path).st_size == header.size
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
    # Verify the canonical phase order in the in-memory listing.
    expected_order = [
        "phase1a_partition",
        "phase1b_merge",
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
    """A degenerate case: no variants, no toolchains, no deps.

    The merge manifest is still emitted (the framework owns phase
    drain detection now, so no barrier sentinels are produced).
    """
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    grouped = result.by_class
    assert grouped["phase1a_partition"] == ()
    assert grouped["phase2_toolchain"] == ()
    assert grouped["phase2_common_dep"] == ()
    assert grouped["phase3_variant"] == ()
    assert len(grouped["phase1b_merge"]) == 1


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

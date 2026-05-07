"""Unit tests for ``compiler_suit_runner.partition``."""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.partition import (
    Partition,
    Shard,
    ShardOutput,
    VariantSpec,
    aggregate_input_drv_frequencies,
    build_partition,
    classify_input_drvs,
    read_partition_json,
    read_shard_outputs,
    split_into_shards,
    write_partition_json,
    write_shard_output,
)


# ---------------------------------------------------------------------------
# Helpers


def _variant(
    pkg: str,
    arch: str,
    suffix: str,
    *,
    compiler_id: str = "gcc15",
    tier: int = 1,
) -> VariantSpec:
    label = f"{pkg}-{arch}-{compiler_id}-{suffix}"
    return {
        "label": label,
        "drv": f"/nix/store/{label}.drv",
        "variant_dir": label,
        "compiler_id": compiler_id,
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Sharding


def test_split_into_shards_groups_by_pkg_and_arch():
    variants = [
        _variant("hello", "x86_64", "O2"),
        _variant("hello", "x86_64", "O0"),
        _variant("hello", "aarch64", "O2"),
        _variant("busybox", "x86_64", "O2"),
        _variant("busybox", "aarch64", "O2"),
    ]
    shards = split_into_shards(variants)
    assert len(shards) == 4
    # Sorted deterministically by (pkg, arch).
    assert [(s.pkg, s.arch) for s in shards] == [
        ("busybox", "aarch64"),
        ("busybox", "x86_64"),
        ("hello", "aarch64"),
        ("hello", "x86_64"),
    ]
    # Within a shard, variants are sorted by label.
    hello_x86 = shards[-1]
    assert hello_x86.pkg == "hello"
    assert hello_x86.arch == "x86_64"
    labels = [v["label"] for v in hello_x86.variants]
    assert labels == sorted(labels)
    assert len(hello_x86.variants) == 2


def test_split_into_shards_is_deterministic_under_input_reordering():
    a = _variant("hello", "x86_64", "O0")
    b = _variant("hello", "x86_64", "O2")
    s1 = split_into_shards([a, b])
    s2 = split_into_shards([b, a])
    assert s1 == s2


def test_shard_name_with_tricky_pkg_and_arch():
    shard = Shard(pkg="lua-5.4", arch="x86_64", variants=())
    assert shard.name == "lua-5.4__x86_64"


def test_split_into_shards_empty():
    assert split_into_shards([]) == []


# ---------------------------------------------------------------------------
# write_shard_output / read_shard_outputs


def test_write_and_read_shard_output_roundtrip(tmp_path: pathlib.Path):
    out = ShardOutput(
        shard_name="hello__x86_64",
        variant_to_input_drvs={
            "hello-x86_64-gcc15-O2": [
                "/nix/store/B.drv",
                "/nix/store/A.drv",
            ],
            "hello-x86_64-gcc15-O0": ["/nix/store/A.drv"],
        },
    )
    target = write_shard_output(tmp_path, out)
    assert target == tmp_path / "hello__x86_64.json"
    assert target.exists()

    on_disk = json.loads(target.read_text())
    # Drv lists are sorted on disk.
    assert (
        on_disk["variant_to_input_drvs"]["hello-x86_64-gcc15-O2"]
        == ["/nix/store/A.drv", "/nix/store/B.drv"]
    )

    loaded = read_shard_outputs(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].shard_name == "hello__x86_64"
    assert loaded[0].variant_to_input_drvs == {
        "hello-x86_64-gcc15-O0": ["/nix/store/A.drv"],
        "hello-x86_64-gcc15-O2": [
            "/nix/store/A.drv",
            "/nix/store/B.drv",
        ],
    }


def test_read_shard_outputs_skips_underscore_and_hidden(
    tmp_path: pathlib.Path,
):
    payload = {
        "shard_name": "real",
        "variant_to_input_drvs": {"v": ["/nix/store/a.drv"]},
    }
    (tmp_path / "real.json").write_text(json.dumps(payload))

    skip_underscore = {
        "shard_name": "_skip",
        "variant_to_input_drvs": {},
    }
    (tmp_path / "_skip.json").write_text(json.dumps(skip_underscore))

    skip_hidden = {
        "shard_name": ".hidden",
        "variant_to_input_drvs": {},
    }
    (tmp_path / ".hidden.json").write_text(json.dumps(skip_hidden))

    # Non-json files are also ignored.
    (tmp_path / "scratch.txt").write_text("ignored")

    outputs = read_shard_outputs(tmp_path)
    assert len(outputs) == 1
    assert outputs[0].shard_name == "real"


def test_read_shard_outputs_sorted_by_shard_name(tmp_path: pathlib.Path):
    for name in ("zeta", "alpha", "mu"):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(
                {"shard_name": name, "variant_to_input_drvs": {}}
            )
        )
    outputs = read_shard_outputs(tmp_path)
    assert [o.shard_name for o in outputs] == ["alpha", "mu", "zeta"]


def test_read_shard_outputs_missing_dir(tmp_path: pathlib.Path):
    nonexistent = tmp_path / "does-not-exist"
    assert read_shard_outputs(nonexistent) == []


def test_read_shard_outputs_rejects_malformed(tmp_path: pathlib.Path):
    (tmp_path / "bad.json").write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        read_shard_outputs(tmp_path)


# ---------------------------------------------------------------------------
# aggregate_input_drv_frequencies


def test_aggregate_counts_variant_once_per_drv():
    s1 = ShardOutput(
        shard_name="s1",
        variant_to_input_drvs={
            "v1": ["A", "B", "B"],  # B duplicated within v1
            "v2": ["B", "C"],
        },
    )
    freq = aggregate_input_drv_frequencies([s1])
    assert freq == {"A": 1, "B": 2, "C": 1}


def test_aggregate_across_multiple_shards():
    s1 = ShardOutput(
        shard_name="s1",
        variant_to_input_drvs={"v1": ["A"], "v2": ["A", "B"]},
    )
    s2 = ShardOutput(
        shard_name="s2",
        variant_to_input_drvs={"v3": ["B", "C"], "v4": ["C"]},
    )
    freq = aggregate_input_drv_frequencies([s1, s2])
    assert freq == {"A": 2, "B": 2, "C": 2}


def test_aggregate_empty():
    assert aggregate_input_drv_frequencies([]) == {}


# ---------------------------------------------------------------------------
# classify_input_drvs


def test_classify_basic():
    freq = {"A": 1, "B": 5, "C": 2}
    toolchains, common, incidental = classify_input_drvs(
        freq, toolchain_drvs=set(), common_threshold=2
    )
    assert toolchains == []
    assert common == ["B", "C"]
    assert incidental == ["A"]


def test_classify_with_external_toolchains():
    freq = {"A": 1, "B": 5, "C": 2}
    toolchains, common, incidental = classify_input_drvs(
        freq, toolchain_drvs={"D"}, common_threshold=2
    )
    # D is in toolchain_drvs but not in freq → still appears in toolchains.
    assert toolchains == ["D"]
    assert common == ["B", "C"]
    assert incidental == ["A"]


def test_classify_toolchain_overrides_threshold():
    freq = {"TOOL": 5, "B": 7}
    # common_threshold=100 — neither would reach the bar based on count.
    toolchains, common, incidental = classify_input_drvs(
        freq, toolchain_drvs={"TOOL"}, common_threshold=100
    )
    # TOOL is a toolchain so it's in toolchains regardless.
    assert toolchains == ["TOOL"]
    assert common == []
    # B is non-toolchain and below threshold.
    assert incidental == ["B"]


def test_classify_default_threshold_is_ten():
    freq = {f"D{i}": i for i in range(1, 12)}
    toolchains, common, incidental = classify_input_drvs(
        freq, toolchain_drvs=set()
    )
    assert toolchains == []
    assert common == ["D10", "D11"]
    assert set(incidental) == {f"D{i}" for i in range(1, 10)}
    assert incidental == sorted(incidental)


def test_classify_returns_sorted_lists():
    freq = {"z": 50, "a": 50, "m": 50}
    toolchains, common, incidental = classify_input_drvs(
        freq, toolchain_drvs={"y", "b"}, common_threshold=10
    )
    assert toolchains == sorted(toolchains)
    assert common == sorted(common)
    assert incidental == sorted(incidental)


# ---------------------------------------------------------------------------
# build_partition / read_partition_json round-trip


def test_build_partition_dedupes_and_sorts():
    variants = [
        _variant("hello", "x86_64", "O2"),
        _variant("hello", "x86_64", "O0"),
        # duplicate label should be deduped:
        _variant("hello", "x86_64", "O2"),
    ]
    p = build_partition(
        input_hash="deadbeef",
        variants=variants,
        toolchains=["B", "A", "B"],
        common_deps=["X", "Y", "X"],
    )
    assert p.version == 1
    assert p.input_hash == "deadbeef"
    assert p.toolchains == ("A", "B")
    assert p.common_deps == ("X", "Y")
    assert len(p.variants) == 2
    assert [v["label"] for v in p.variants] == sorted(
        v["label"] for v in p.variants
    )


def test_partition_json_roundtrip(tmp_path: pathlib.Path):
    variants = [
        _variant("hello", "x86_64", "O2", tier=1),
        _variant("sqlite", "aarch64", "O3", compiler_id="clang19", tier=2),
    ]
    p = build_partition(
        input_hash="abc123",
        variants=variants,
        toolchains=["/nix/store/tool-a.drv", "/nix/store/tool-b.drv"],
        common_deps=["/nix/store/dep1.drv"],
    )
    target = tmp_path / "partition.json"
    written = write_partition_json(target, p)
    assert written == target
    assert target.exists()

    on_disk = json.loads(target.read_text())
    assert on_disk["version"] == 1
    assert on_disk["input_hash"] == "abc123"
    assert on_disk["toolchains"] == [
        "/nix/store/tool-a.drv",
        "/nix/store/tool-b.drv",
    ]
    assert on_disk["common_deps"] == ["/nix/store/dep1.drv"]
    # Variants on disk only carry the documented schema fields.
    for variant_blob in on_disk["variants"]:
        assert set(variant_blob) == {
            "label",
            "drv",
            "variant_dir",
            "compiler_id",
            "tier",
        }

    loaded = read_partition_json(target)
    assert isinstance(loaded, Partition)
    assert loaded.version == 1
    assert loaded.input_hash == "abc123"
    assert loaded.toolchains == p.toolchains
    assert loaded.common_deps == p.common_deps
    assert len(loaded.variants) == len(p.variants)
    for original, restored in zip(p.variants, loaded.variants):
        for field in (
            "label",
            "drv",
            "variant_dir",
            "compiler_id",
            "tier",
        ):
            assert restored[field] == original[field]


def test_read_partition_json_rejects_unknown_version(
    tmp_path: pathlib.Path,
):
    target = tmp_path / "partition.json"
    target.write_text(
        json.dumps(
            {
                "version": 2,
                "input_hash": "x",
                "toolchains": [],
                "common_deps": [],
                "variants": [],
            }
        )
    )
    with pytest.raises(ValueError):
        read_partition_json(target)


def test_read_partition_json_rejects_missing_field(tmp_path: pathlib.Path):
    target = tmp_path / "partition.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "input_hash": "x",
                "toolchains": [],
                "common_deps": [],
                # variants missing
            }
        )
    )
    with pytest.raises(ValueError):
        read_partition_json(target)


def test_read_partition_json_rejects_bad_variant(tmp_path: pathlib.Path):
    target = tmp_path / "partition.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "input_hash": "x",
                "toolchains": [],
                "common_deps": [],
                "variants": [
                    {
                        "label": "hello",
                        "drv": "/nix/store/x.drv",
                        "variant_dir": "hello",
                        "compiler_id": "gcc15",
                        # tier missing
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError):
        read_partition_json(target)


def test_read_partition_json_rejects_non_object_top_level(
    tmp_path: pathlib.Path,
):
    target = tmp_path / "partition.json"
    target.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        read_partition_json(target)


def test_atomic_write_does_not_leave_tmp_files(tmp_path: pathlib.Path):
    out = ShardOutput(
        shard_name="x",
        variant_to_input_drvs={"v": ["/a.drv"]},
    )
    write_shard_output(tmp_path, out)
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []

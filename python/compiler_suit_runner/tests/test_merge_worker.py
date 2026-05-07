"""Tests for ``compiler_suit_runner.workers.merge_worker``.

Stdlib + pytest only; no real time elapses (``clock`` is injected for
deterministic ``duration_seconds``).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.partition import (
    PARTITION_VERSION,
    VariantSpec,
    read_partition_json,
)
from compiler_suit_runner.workers.merge_worker import (
    PHASE_1B_ITEM_CLASS,
    SKIP_LIST_VERSION,
    MergeWorkerEnv,
    MergeWorkerResult,
    collect_skip_list,
    merge_worker,
    parse_merge_manifest,
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


def _write_manifest(target: pathlib.Path, *, item_class: str = PHASE_1B_ITEM_CLASS) -> pathlib.Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"item_class": item_class}),
        encoding="utf-8",
    )
    return target


def _write_shard(
    raw_dir: pathlib.Path,
    shard_name: str,
    variant_to_input_drvs: dict[str, list[str]],
) -> pathlib.Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{shard_name}.json"
    target.write_text(
        json.dumps(
            {
                "shard_name": shard_name,
                "variant_to_input_drvs": variant_to_input_drvs,
            }
        ),
        encoding="utf-8",
    )
    return target


class FakeClock:
    """Deterministic clock that returns whatever time we set."""

    def __init__(self) -> None:
        self.values = [0.0, 1.5]
        self.calls = 0

    def __call__(self) -> float:
        idx = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[idx]


# ---------------------------------------------------------------------------
# parse_merge_manifest


def test_parse_merge_manifest_happy_path(tmp_path: pathlib.Path) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json")
    payload = parse_merge_manifest(manifest)
    assert payload["item_class"] == PHASE_1B_ITEM_CLASS


def test_parse_merge_manifest_tolerates_extra_fields(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "manifest.json"
    target.write_text(
        json.dumps(
            {
                "item_class": PHASE_1B_ITEM_CLASS,
                "extra": "ok",
                "size": 12345,
            }
        ),
        encoding="utf-8",
    )
    payload = parse_merge_manifest(target)
    assert payload["extra"] == "ok"
    assert payload["size"] == 12345


def test_parse_merge_manifest_rejects_wrong_item_class(tmp_path: pathlib.Path) -> None:
    manifest = _write_manifest(tmp_path / "manifest.json", item_class="phase1a_shard")
    with pytest.raises(ValueError, match="phase1b_merge"):
        parse_merge_manifest(manifest)


def test_parse_merge_manifest_rejects_missing_item_class(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps({"unrelated": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="phase1b_merge"):
        parse_merge_manifest(target)


def test_parse_merge_manifest_rejects_non_object(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        parse_merge_manifest(target)


def test_parse_merge_manifest_missing_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_merge_manifest(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# collect_skip_list — v1 contract is empty list


def test_collect_skip_list_returns_empty_list_for_v1() -> None:
    variants = (_variant("hello", "x86_64", "O0"), _variant("hello", "x86_64", "O2"))
    result = collect_skip_list(
        variants,
        classification_incidentals=frozenset({"/nix/store/xyz.drv"}),
    )
    assert result == []


def test_collect_skip_list_handles_empty_inputs() -> None:
    assert collect_skip_list((), classification_incidentals=frozenset()) == []


# ---------------------------------------------------------------------------
# merge_worker end-to-end


def test_merge_worker_end_to_end(tmp_path: pathlib.Path) -> None:
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json")

    variants = (
        _variant("hello", "x86_64", "O0"),
        _variant("hello", "x86_64", "O2"),
        _variant("hello", "aarch64", "O0"),
        _variant("hello", "aarch64", "O2"),
    )

    # A toolchain that appears in every variant's input list.
    toolchain_drv = "/nix/store/toolchain-gcc15.drv"
    # A "common" dep — appears in all 4 variants (>= threshold=2).
    common_drv = "/nix/store/common-zlib.drv"
    # An incidental — appears in just 1 variant (< threshold=2).
    incidental_drv = "/nix/store/incidental-only-once.drv"
    # Another common drv to exercise sorting + dedup.
    common_drv_2 = "/nix/store/common-libffi.drv"

    # Two shards covering the 4 variants. Frequencies (per distinct
    # variant): toolchain=4, common_zlib=4, common_libffi=3, incidental=1.
    _write_shard(
        raw_dir,
        "hello__x86_64",
        {
            variants[0]["label"]: [toolchain_drv, common_drv, common_drv_2, incidental_drv],
            variants[1]["label"]: [toolchain_drv, common_drv, common_drv_2],
        },
    )
    _write_shard(
        raw_dir,
        "hello__aarch64",
        {
            variants[2]["label"]: [toolchain_drv, common_drv, common_drv_2],
            variants[3]["label"]: [toolchain_drv, common_drv],
        },
    )

    fake_clock = FakeClock()
    fake_clock.values = [10.0, 12.5]

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-deadbeef",
        variants=variants,
        toolchain_drvs=frozenset({toolchain_drv}),
        common_threshold=2,
        clock=fake_clock,
    )

    result = merge_worker(manifest, env)

    # Result fields
    assert isinstance(result, MergeWorkerResult)
    assert result.error is None
    assert result.partition_path == out_dir / "partition.json"
    assert result.skip_list_path == out_dir / "skip_list.json"
    assert result.shard_count == 2
    assert result.variant_count == 4
    # 1 toolchain, 2 common deps (zlib + libffi), 1 incidental.
    assert result.toolchain_count == 1
    assert result.common_dep_count == 2
    assert result.incidental_count == 1
    # Two clock samples consumed: start=10.0, end=12.5 -> duration=2.5.
    assert result.duration_seconds == pytest.approx(2.5)

    # partition.json round-trip
    assert result.partition_path.exists()
    partition = read_partition_json(result.partition_path)
    assert partition.version == PARTITION_VERSION
    assert partition.input_hash == "sha256-deadbeef"
    assert toolchain_drv in partition.toolchains
    assert common_drv in partition.common_deps
    assert common_drv_2 in partition.common_deps
    assert incidental_drv not in partition.common_deps
    assert incidental_drv not in partition.toolchains
    # Variants present, sorted by label.
    assert tuple(v["label"] for v in partition.variants) == tuple(
        sorted(v["label"] for v in variants)
    )

    # skip_list.json — v1 always empty.
    assert result.skip_list_path.exists()
    skip_payload = json.loads(result.skip_list_path.read_text(encoding="utf-8"))
    assert skip_payload == {"version": SKIP_LIST_VERSION, "entries": []}

    # No leftover .tmp files.
    leftovers = list(out_dir.glob("*.tmp"))
    assert leftovers == []


def test_merge_worker_with_empty_raw_partition_dir(tmp_path: pathlib.Path) -> None:
    """No shard outputs => graceful empty merge."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json")

    variants = (_variant("hello", "x86_64", "O0"),)
    toolchain_drv = "/nix/store/toolchain-gcc15.drv"

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-empty",
        variants=variants,
        toolchain_drvs=frozenset({toolchain_drv}),
        common_threshold=2,
    )

    result = merge_worker(manifest, env)

    assert result.error is None
    assert result.shard_count == 0
    assert result.variant_count == 1
    # No frequencies => no observed toolchains. ``classify_input_drvs``
    # still returns the canonical toolchain set as the toolchain bucket
    # (sorted union with the empty observed set).
    assert result.toolchain_count == 1
    assert result.common_dep_count == 0
    assert result.incidental_count == 0

    partition = read_partition_json(result.partition_path)
    assert toolchain_drv in partition.toolchains
    assert partition.common_deps == ()
    assert tuple(v["label"] for v in partition.variants) == (variants[0]["label"],)

    skip_payload = json.loads(result.skip_list_path.read_text(encoding="utf-8"))
    assert skip_payload == {"version": SKIP_LIST_VERSION, "entries": []}


def test_merge_worker_with_nonexistent_raw_partition_dir(tmp_path: pathlib.Path) -> None:
    """Missing raw dir is treated as no-shards (read_shard_outputs returns [])."""
    raw_dir = tmp_path / "raw-missing"  # not created
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json")

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-nodir",
        variants=(),
        toolchain_drvs=frozenset(),
        common_threshold=2,
    )

    result = merge_worker(manifest, env)

    assert result.error is None
    assert result.shard_count == 0
    assert result.variant_count == 0
    assert result.toolchain_count == 0
    assert result.common_dep_count == 0


def test_merge_worker_rejects_wrong_manifest_class(tmp_path: pathlib.Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json", item_class="phase3_variant")

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-wrongclass",
        variants=(),
        toolchain_drvs=frozenset(),
    )

    with pytest.raises(ValueError, match="phase1b_merge"):
        merge_worker(manifest, env)

    # No partition.json should have been written before the failing
    # manifest check.
    assert not (out_dir / "partition.json").exists()


def test_merge_worker_malformed_shard_propagates(tmp_path: pathlib.Path) -> None:
    """``read_shard_outputs`` raises ValueError on malformed shards.

    Design choice: phase-1a shards are produced by our own worker into a
    private dir, so any malformed shard signals an internal bug and we
    fail loudly rather than silently dropping data. The merge worker
    surfaces the failure via the standard error path: the raised
    exception attaches a :class:`MergeWorkerResult` with ``error`` set.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json")

    # One well-formed shard.
    _write_shard(
        raw_dir,
        "hello__x86_64",
        {"hello-x86_64-gcc15-O0": ["/nix/store/foo.drv"]},
    )
    # One malformed shard (top-level array instead of object).
    bad = raw_dir / "broken.json"
    bad.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-bad",
        variants=(_variant("hello", "x86_64", "O0"),),
        toolchain_drvs=frozenset(),
    )

    with pytest.raises(ValueError):
        merge_worker(manifest, env)


def test_merge_worker_attaches_result_to_exception(tmp_path: pathlib.Path) -> None:
    """The re-raised exception carries a MergeWorkerResult with error set."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json", item_class="phase3_variant")

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-attach",
        variants=(),
        toolchain_drvs=frozenset(),
    )

    with pytest.raises(ValueError) as excinfo:
        merge_worker(manifest, env)

    attached = getattr(excinfo.value, "merge_worker_result", None)
    assert attached is not None
    assert attached.error is not None
    assert "phase1b_merge" in attached.error


def test_merge_worker_threshold_boundary(tmp_path: pathlib.Path) -> None:
    """A drv at exactly ``common_threshold`` is counted as common."""
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    manifest = _write_manifest(tmp_path / "manifest.json")

    variants = (
        _variant("hello", "x86_64", "O0"),
        _variant("hello", "x86_64", "O1"),
        _variant("hello", "x86_64", "O2"),
    )

    boundary_drv = "/nix/store/boundary.drv"
    below_drv = "/nix/store/below.drv"

    _write_shard(
        raw_dir,
        "hello__x86_64",
        {
            variants[0]["label"]: [boundary_drv, below_drv],
            variants[1]["label"]: [boundary_drv],
            variants[2]["label"]: [boundary_drv],
        },
    )

    env = MergeWorkerEnv(
        raw_partition_dir=raw_dir,
        partition_dir=out_dir,
        input_hash="sha256-threshold",
        variants=variants,
        toolchain_drvs=frozenset(),
        common_threshold=3,
    )

    result = merge_worker(manifest, env)
    assert result.error is None
    partition = read_partition_json(result.partition_path)
    assert boundary_drv in partition.common_deps
    assert below_drv not in partition.common_deps
    assert result.common_dep_count == 1
    assert result.incidental_count == 1

"""Pure utilities for the phase-1a/phase-1b partition pipeline.

This module is intentionally side-effect-free aside from the explicit
file-I/O helpers. Worker modules wrap these functions with subprocess
and threading concerns.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from collections.abc import Iterable, Mapping
from typing import TypedDict


PARTITION_VERSION = 1


class VariantSpec(TypedDict):
    """Static description of one variant the matrix exposes.

    ``label`` is the canonical full identifier
    (``<pkg>-<arch>-<compiler>-<opt>-<flags>-<hardening>``), used as
    a stable hash input. ``variant_dir`` is the per-variant subdir
    name (``<compiler>_<arch>_<opt>_<hash>``); each variant's ELFs
    land at ``dataset_dir/<pkg>/<variant_dir>/<elf>`` and the sidecar
    JSON at ``dataset_dir/<pkg>/<variant_dir>.json``. ``metadata_name``
    is the sidecar file name (``<compiler>_<arch>_<opt>_<hash>.json``).
    """

    label: str
    drv: str
    variant_dir: str
    metadata_name: str
    compiler_id: str
    compiler_family: str
    compiler_version: str
    optimization: str
    flag_set: str
    hardening: str
    sanitizer: str
    march: str
    tier: int
    pkg: str
    arch: str


# ---------------------------------------------------------------------------
# Sharding


@dataclasses.dataclass(frozen=True)
class Shard:
    """A (pkg, arch) slice of the variant matrix processed by one worker."""

    pkg: str
    arch: str
    variants: tuple[VariantSpec, ...]

    @property
    def name(self) -> str:
        """Filesystem-safe identifier ``f'{pkg}__{arch}'``."""
        return f"{self.pkg}__{self.arch}"


def split_into_shards(variants: Iterable[VariantSpec]) -> list[Shard]:
    """Group variants by ``(pkg, arch)`` into deterministic ``Shard``s.

    The returned list is sorted by ``(pkg, arch)``; each shard's
    ``variants`` tuple is sorted by ``label``.
    """
    buckets: dict[tuple[str, str], list[VariantSpec]] = {}
    for variant in variants:
        key = (variant["pkg"], variant["arch"])
        buckets.setdefault(key, []).append(variant)

    shards: list[Shard] = []
    for key in sorted(buckets):
        pkg, arch = key
        sorted_variants = tuple(
            sorted(buckets[key], key=lambda v: v["label"])
        )
        shards.append(Shard(pkg=pkg, arch=arch, variants=sorted_variants))
    return shards


# ---------------------------------------------------------------------------
# Phase-1a per-shard output


@dataclasses.dataclass
class ShardOutput:
    """Per-variant inputDrv sets emitted by a phase-1a worker."""

    shard_name: str
    variant_to_input_drvs: dict[str, list[str]]


def _atomic_write_json(target: pathlib.Path, payload: object) -> pathlib.Path:
    """Write ``payload`` as JSON to ``target`` atomically.

    Uses a sibling ``.tmp`` file plus ``os.replace``; fsyncs the file
    contents before the rename so an interrupted run does not leave a
    half-written payload.
    """
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    data = json.dumps(payload, sort_keys=True, indent=2)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def write_shard_output(
    target_dir: pathlib.Path, output: ShardOutput
) -> pathlib.Path:
    """Atomically write ``output`` to ``<target_dir>/<shard_name>.json``."""
    target_dir = pathlib.Path(target_dir)
    target = target_dir / f"{output.shard_name}.json"
    payload = {
        "shard_name": output.shard_name,
        "variant_to_input_drvs": {
            label: sorted(drvs)
            for label, drvs in output.variant_to_input_drvs.items()
        },
    }
    return _atomic_write_json(target, payload)


def read_shard_outputs(source_dir: pathlib.Path) -> list[ShardOutput]:
    """Load every non-hidden ``*.json`` file in ``source_dir``.

    Files whose name begins with ``_`` or ``.`` are skipped (these are
    reserved for sidecar or scratch state). The returned list is sorted
    by ``shard_name``.
    """
    source_dir = pathlib.Path(source_dir)
    if not source_dir.exists():
        return []

    outputs: list[ShardOutput] = []
    for entry in sorted(source_dir.iterdir()):
        if entry.suffix != ".json":
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        with open(entry, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError(
                f"shard output {entry} is not a JSON object"
            )
        shard_name = payload.get("shard_name")
        variant_map = payload.get("variant_to_input_drvs")
        if not isinstance(shard_name, str):
            raise ValueError(
                f"shard output {entry}: missing/invalid 'shard_name'"
            )
        if not isinstance(variant_map, dict):
            raise ValueError(
                f"shard output {entry}: missing/invalid"
                " 'variant_to_input_drvs'"
            )
        normalized: dict[str, list[str]] = {}
        for label, drvs in variant_map.items():
            if not isinstance(label, str) or not isinstance(drvs, list):
                raise ValueError(
                    f"shard output {entry}: malformed entry for {label!r}"
                )
            if not all(isinstance(d, str) for d in drvs):
                raise ValueError(
                    f"shard output {entry}: non-string drv in {label!r}"
                )
            normalized[label] = list(drvs)
        outputs.append(
            ShardOutput(
                shard_name=shard_name, variant_to_input_drvs=normalized
            )
        )
    outputs.sort(key=lambda o: o.shard_name)
    return outputs


# ---------------------------------------------------------------------------
# Aggregation + classification


def aggregate_input_drv_frequencies(
    shard_outputs: Iterable[ShardOutput],
) -> dict[str, int]:
    """Count, per inputDrv, how many distinct variants reference it.

    Duplicate references inside a single variant's drv list count once.
    """
    counts: dict[str, int] = {}
    for output in shard_outputs:
        for drvs in output.variant_to_input_drvs.values():
            for drv in set(drvs):
                counts[drv] = counts.get(drv, 0) + 1
    return counts


def classify_input_drvs(
    frequencies: Mapping[str, int],
    toolchain_drvs: set[str],
    *,
    common_threshold: int = 10,
) -> tuple[list[str], list[str], list[str]]:
    """Bucket inputDrvs into (toolchains, common_deps, incidental).

    * ``toolchains``: the canonical cross-compiler drvs from
      ``_crossToolchainsMeta``. They are *always* Phase 2 and therefore
      always returned regardless of frequency. The result preserves the
      sorted order of the union of ``toolchain_drvs`` with toolchain
      entries observed in ``frequencies``.
    * ``common_deps``: non-toolchain drvs whose frequency is at least
      ``common_threshold``. Sorted.
    * ``incidental``: everything else from ``frequencies``. Sorted.
    """
    toolchains_in_freq = {
        drv for drv in frequencies if drv in toolchain_drvs
    }
    toolchains = sorted(toolchain_drvs | toolchains_in_freq)

    common: list[str] = []
    incidental: list[str] = []
    for drv, count in frequencies.items():
        if drv in toolchain_drvs:
            continue
        if count >= common_threshold:
            common.append(drv)
        else:
            incidental.append(drv)
    common.sort()
    incidental.sort()
    return toolchains, common, incidental


# ---------------------------------------------------------------------------
# Final partition envelope


@dataclasses.dataclass(frozen=True)
class Partition:
    """The merged scheduling hint emitted by the phase-1b worker."""

    version: int
    input_hash: str
    toolchains: tuple[str, ...]
    common_deps: tuple[str, ...]
    variants: tuple[VariantSpec, ...]


def _dedupe_sorted(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(items)))


def build_partition(
    *,
    input_hash: str,
    variants: Iterable[VariantSpec],
    toolchains: Iterable[str],
    common_deps: Iterable[str],
) -> Partition:
    """Assemble a ``Partition`` (version=1) with sorted, deduped inputs."""
    seen_labels: set[str] = set()
    deduped_variants: list[VariantSpec] = []
    for variant in variants:
        label = variant["label"]
        if label in seen_labels:
            continue
        seen_labels.add(label)
        deduped_variants.append(variant)
    deduped_variants.sort(key=lambda v: v["label"])
    return Partition(
        version=PARTITION_VERSION,
        input_hash=input_hash,
        toolchains=_dedupe_sorted(toolchains),
        common_deps=_dedupe_sorted(common_deps),
        variants=tuple(deduped_variants),
    )


_VARIANT_FIELDS = (
    "label",
    "drv",
    "variant_dir",
    "compiler_id",
    "tier",
)


def _variant_to_jsonable(variant: VariantSpec) -> dict[str, object]:
    """Project a ``VariantSpec`` to the partition.json schema fields."""
    return {field: variant[field] for field in _VARIANT_FIELDS}


def write_partition_json(
    target: pathlib.Path, partition: Partition
) -> pathlib.Path:
    """Atomically write ``partition`` as JSON to ``target``."""
    payload = {
        "version": partition.version,
        "input_hash": partition.input_hash,
        "toolchains": list(partition.toolchains),
        "common_deps": list(partition.common_deps),
        "variants": [
            _variant_to_jsonable(v) for v in partition.variants
        ],
    }
    return _atomic_write_json(pathlib.Path(target), payload)


def read_partition_json(source: pathlib.Path) -> Partition:
    """Read a partition.json file written by :func:`write_partition_json`.

    Raises ``ValueError`` on any schema mismatch, including unsupported
    ``version`` values.
    """
    source = pathlib.Path(source)
    with open(source, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: top-level JSON must be an object")

    version = payload.get("version")
    if version != PARTITION_VERSION:
        raise ValueError(
            f"{source}: unsupported partition version {version!r};"
            f" expected {PARTITION_VERSION}"
        )

    input_hash = payload.get("input_hash")
    if not isinstance(input_hash, str):
        raise ValueError(f"{source}: missing/invalid 'input_hash'")

    def _string_tuple(key: str) -> tuple[str, ...]:
        raw = payload.get(key)
        if not isinstance(raw, list) or not all(
            isinstance(x, str) for x in raw
        ):
            raise ValueError(f"{source}: '{key}' must be a list of strings")
        return tuple(raw)

    toolchains = _string_tuple("toolchains")
    common_deps = _string_tuple("common_deps")

    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        raise ValueError(f"{source}: 'variants' must be a list")

    variants: list[VariantSpec] = []
    for index, raw in enumerate(raw_variants):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{source}: variants[{index}] is not an object"
            )
        for field in _VARIANT_FIELDS:
            if field not in raw:
                raise ValueError(
                    f"{source}: variants[{index}] missing field {field!r}"
                )
        if not isinstance(raw["tier"], int):
            raise ValueError(
                f"{source}: variants[{index}].tier must be an int"
            )
        for field in ("label", "drv", "variant_dir", "compiler_id"):
            if not isinstance(raw[field], str):
                raise ValueError(
                    f"{source}: variants[{index}].{field} must be a string"
                )
        # The on-disk schema deliberately omits pkg/arch — they can be
        # recovered from label or by re-running split_into_shards. For
        # in-memory round-tripping we accept missing values and default
        # to empty strings; downstream consumers that need pkg/arch
        # reconstruct them from the matrix metadata, not from
        # partition.json.
        variant: VariantSpec = {
            "label": raw["label"],
            "drv": raw["drv"],
            "variant_dir": raw["variant_dir"],
            "compiler_id": raw["compiler_id"],
            "tier": raw["tier"],
            "pkg": raw.get("pkg", "") if isinstance(raw.get("pkg", ""), str) else "",
            "arch": raw.get("arch", "") if isinstance(raw.get("arch", ""), str) else "",
        }
        variants.append(variant)

    return Partition(
        version=version,
        input_hash=input_hash,
        toolchains=toolchains,
        common_deps=common_deps,
        variants=tuple(variants),
    )

"""Pre-flight manifest generation for the compiler-suit runner.

The dynamic_batch framework discovers queue items by scanning a target
directory for manifest files. Two pieces of metadata travel with each
item:

* The JSON contents of the file describe the item's class (which worker
  should pick it up) and class-specific payload (pkg/arch/drv/...).
* The file's *apparent* on-disk size encodes the scheduling integer
  ``size = (phase_rank << 48) | (memory_bytes & ((1 << 48) - 1))``
  (see :mod:`compiler_suit_runner.memory_budget`). The framework reads
  this via ``os.stat().st_size`` to decide both phase ordering and
  per-item memory budget.

We therefore write the JSON header to the file, then ``os.ftruncate`` the
file to the encoded size. Because the JSON content is at most a few
hundred bytes and the encoded size is on the order of gigabytes, the
file is *sparse* — no actual disk usage is incurred for the trailing
zero-fill.

This module produces manifests for all four item classes in the plan's
"Updated rank table (final)":

* ``phase1a_partition`` — one per (pkg, arch) shard
* ``phase1a_barrier``   — ``num_workers`` sentinels for the phase-1a barrier
* ``phase1b_merge``     — exactly one merge item
* ``phase1b_barrier``   — ``num_workers`` sentinels
* ``phase2_toolchain``  — one per (arch, compiler_label) cross-toolchain
* ``phase2_common_dep`` — one per common host dep drv
* ``phase2_barrier``    — ``num_workers`` sentinels
* ``phase3_variant``    — one per matrix variant

Iteration order in the returned :class:`ManifestSet` follows the phase
sequence; the Rust scheduler re-sorts by ``size`` DESC anyway, but the
pre-sort order keeps debug logging readable.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from collections.abc import Iterable
from typing import Final, Literal

from compiler_suit_runner.memory_budget import (
    MEMORY_FLOOR_BYTES,
    PHASE_1A_BARRIER,
    PHASE_1A_PARTITION,
    PHASE_1B_BARRIER,
    PHASE_1B_MERGE,
    PHASE_2_BARRIER,
    PHASE_2_BUILD,
    PHASE_3_VARIANT,
    common_dep_memory_bytes,
    encode_size,
    merge_memory_bytes,
    partition_shard_memory_bytes,
    toolchain_memory_bytes,
    variant_memory_bytes,
)
from compiler_suit_runner.partition import Shard, VariantSpec, split_into_shards


# ---------------------------------------------------------------------------
# Types

ItemClass = Literal[
    "phase1a_partition",
    "phase1a_barrier",
    "phase1b_merge",
    "phase1b_barrier",
    "phase2_toolchain",
    "phase2_common_dep",
    "phase2_barrier",
    "phase3_variant",
]


# All known item classes, in the canonical iteration order. Used so
# ``ManifestSet.by_class`` can return an empty tuple for absent classes
# rather than raising ``KeyError``.
_ALL_ITEM_CLASSES: tuple[ItemClass, ...] = (
    "phase1a_partition",
    "phase1a_barrier",
    "phase1b_merge",
    "phase1b_barrier",
    "phase2_toolchain",
    "phase2_common_dep",
    "phase2_barrier",
    "phase3_variant",
)


@dataclasses.dataclass(frozen=True)
class ManifestHeader:
    """JSON-serialisable description of one queue item.

    ``size`` is the packed scheduling integer (see :mod:`memory_budget`).
    It must equal the on-disk apparent size of the manifest file —
    :func:`write_manifest` enforces this via ``os.ftruncate``, and
    :func:`read_manifest` verifies it on load.
    """

    item_class: ItemClass
    name: str
    size: int
    payload: dict


# ---------------------------------------------------------------------------
# Header constructors


def make_partition_shard_header(shard: Shard) -> ManifestHeader:
    """Build the phase-1a manifest for a (pkg, arch) shard.

    The payload carries enough information for the partition worker to
    re-discover the variant attribute paths it needs to ``nix derivation
    show``: pkg + arch select the matrix slice, and the variant list
    enumerates the labels/drvs that slice exposes.
    """
    payload = {
        "pkg": shard.pkg,
        "arch": shard.arch,
        "variants": [dict(v) for v in shard.variants],
    }
    return ManifestHeader(
        item_class="phase1a_partition",
        name=shard.name,
        size=encode_size(PHASE_1A_PARTITION, partition_shard_memory_bytes()),
        payload=payload,
    )


def make_partition_barrier_header(index: int, count: int) -> ManifestHeader:
    """Build the ``index``-th phase-1a barrier sentinel.

    ``count`` sentinels are emitted (one per worker) so every worker
    eventually pulls one and parks on the flag-file poll. The framework
    re-sorts by ``size`` DESC so all barriers of one rank get dispatched
    together; we still tag each sentinel with its index for log clarity.
    """
    if count <= 0:
        raise ValueError(f"barrier count must be positive, got {count}")
    if not 0 <= index < count:
        raise ValueError(
            f"barrier index {index} out of range for count {count}"
        )
    name = f"phase1a_barrier_{index:04d}_of_{count:04d}"
    return ManifestHeader(
        item_class="phase1a_barrier",
        name=name,
        size=encode_size(PHASE_1A_BARRIER, MEMORY_FLOOR_BYTES),
        payload={
            "flag_name": "phase1a_done",
            "index": index,
            "count": count,
        },
    )


def make_merge_header() -> ManifestHeader:
    """Build the singleton phase-1b merge manifest."""
    return ManifestHeader(
        item_class="phase1b_merge",
        name="phase1b_merge",
        size=encode_size(PHASE_1B_MERGE, merge_memory_bytes()),
        payload={},
    )


def make_merge_barrier_header(index: int, count: int) -> ManifestHeader:
    """Build the ``index``-th phase-1b barrier sentinel."""
    if count <= 0:
        raise ValueError(f"barrier count must be positive, got {count}")
    if not 0 <= index < count:
        raise ValueError(
            f"barrier index {index} out of range for count {count}"
        )
    name = f"phase1b_barrier_{index:04d}_of_{count:04d}"
    return ManifestHeader(
        item_class="phase1b_barrier",
        name=name,
        size=encode_size(PHASE_1B_BARRIER, MEMORY_FLOOR_BYTES),
        payload={
            "flag_name": "phase1b_done",
            "index": index,
            "count": count,
        },
    )


def make_toolchain_header(
    sys_name: str,
    arch: str,
    compiler_label: str,
    drv: str | None = None,
) -> ManifestHeader:
    """Build a phase-2 cross-toolchain manifest.

    The build worker resolves the toolchain via the
    ``_crossToolchainMap.<sys>.<arch>.<compiler_label>`` flake attribute.
    ``drv`` is optional — if the local pre-flight already evaluated the
    drvPath we can carry it through, else the worker re-evaluates.
    """
    payload: dict = {
        "sys": sys_name,
        "arch": arch,
        "compiler_label": compiler_label,
        "attr": f"_crossToolchainMap.{sys_name}.{arch}.{compiler_label}",
    }
    if drv is not None:
        payload["drv"] = drv
    return ManifestHeader(
        item_class="phase2_toolchain",
        name=f"toolchain__{arch}__{compiler_label}",
        size=encode_size(PHASE_2_BUILD, toolchain_memory_bytes()),
        payload=payload,
    )


def make_common_dep_header(drv: str, label: str) -> ManifestHeader:
    """Build a phase-2 common host-dep manifest.

    The worker uses ``attr`` directly with ``nix build`` (it is a raw
    drvPath, not a flake attribute path).
    """
    return ManifestHeader(
        item_class="phase2_common_dep",
        name=f"common_dep__{label}",
        size=encode_size(PHASE_2_BUILD, common_dep_memory_bytes()),
        payload={
            "drv": drv,
            "label": label,
            "attr": drv,
        },
    )


def make_phase2_barrier_header(index: int, count: int) -> ManifestHeader:
    """Build the ``index``-th phase-2 barrier sentinel."""
    if count <= 0:
        raise ValueError(f"barrier count must be positive, got {count}")
    if not 0 <= index < count:
        raise ValueError(
            f"barrier index {index} out of range for count {count}"
        )
    name = f"phase2_barrier_{index:04d}_of_{count:04d}"
    return ManifestHeader(
        item_class="phase2_barrier",
        name=name,
        size=encode_size(PHASE_2_BARRIER, MEMORY_FLOOR_BYTES),
        payload={
            "flag_name": "phase2_done",
            "index": index,
            "count": count,
        },
    )


def _label_to_attr_suffix(label: str) -> str:
    """Recover the flake attribute suffix for a variant label.

    ``flake.nix``'s ``_drvPaths`` and ``dataset`` outputs index variants
    by an attribute path of the form ``<sys>.<pkg>.<arch>.<suffix>``.
    Variant labels use the same suffix as their final dotted segment;
    we conservatively use the full label as the suffix here, which is
    what the matrix builder emits (see ``lib/matrix.nix``).
    """
    return label


def make_variant_header(
    variant: VariantSpec, sys_name: str
) -> ManifestHeader:
    """Build a phase-3 variant manifest.

    Memory budget is tier-aware (see :func:`memory_budget.variant_memory_bytes`).
    """
    pkg = variant["pkg"]
    arch = variant["arch"]
    label = variant["label"]
    suffix = _label_to_attr_suffix(label)
    payload = {
        "sys": sys_name,
        "pkg": pkg,
        "arch": arch,
        "label": label,
        "drv": variant["drv"],
        "tarball_name": variant["tarball_name"],
        "compiler_id": variant["compiler_id"],
        "tier": variant["tier"],
        "attr": f"dataset.{sys_name}.{pkg}.{arch}.{suffix}",
    }
    return ManifestHeader(
        item_class="phase3_variant",
        name=label,
        size=encode_size(PHASE_3_VARIANT, variant_memory_bytes(pkg)),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Manifest IO


def _header_to_jsonable(header: ManifestHeader) -> dict:
    return {
        "item_class": header.item_class,
        "name": header.name,
        "size": header.size,
        "payload": header.payload,
    }


def write_manifest(
    target_dir: pathlib.Path, header: ManifestHeader
) -> pathlib.Path:
    """Write ``header`` to ``<target_dir>/<header.name>.json``.

    The JSON content (a few hundred bytes) is written first, then the
    file is extended with ``os.ftruncate`` so its apparent size matches
    ``header.size``. The trailing region is sparse — no real disk usage.

    The write is atomic against concurrent readers: contents are placed
    in a sibling ``.tmp`` file (truncated to the encoded size before
    rename) and ``os.replace``\\ d into the final location.
    """
    target_dir = pathlib.Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{header.name}.json"
    tmp = target.with_suffix(target.suffix + ".tmp")

    data = json.dumps(_header_to_jsonable(header), sort_keys=True, indent=2)
    encoded = data.encode("utf-8")
    if len(encoded) > header.size:
        # Defensive: callers should pick budgets larger than any plausible
        # JSON header (we promise sub-kilobyte payloads). The framework's
        # st_size-based decoding cannot recover from a content overflow.
        raise ValueError(
            f"manifest JSON ({len(encoded)} bytes) exceeds encoded size"
            f" ({header.size}); pick a larger memory budget"
        )

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.ftruncate(fd, header.size)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return target


# A manifest's JSON content is sub-kilobyte; reading 64 KiB is
# generously safe and avoids ever loading the multi-petabyte sparse
# tail into memory.
_HEADER_READ_LIMIT_BYTES: Final[int] = 64 * 1024


def read_manifest(path: pathlib.Path) -> ManifestHeader:
    """Inverse of :func:`write_manifest`.

    Verifies that the file's apparent size matches the header's encoded
    size — a mismatch indicates corruption (e.g. a writer crashed before
    the ``ftruncate``, or an external tool rewrote the file without
    preserving its sparse extent).

    Only the leading ``_HEADER_READ_LIMIT_BYTES`` are read from disk; the
    trailing sparse zero-fill is never loaded into memory. The leading
    region contains the JSON document followed by zero-fill we strip
    before parsing.
    """
    path = pathlib.Path(path)
    stat = os.stat(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        head = os.read(fd, _HEADER_READ_LIMIT_BYTES)
    finally:
        os.close(fd)
    # Strip any trailing NULs left by ftruncate's zero-fill so json.loads
    # sees only the document.
    stripped = head.rstrip(b"\x00").decode("utf-8")
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: manifest JSON is not an object")

    for field in ("item_class", "name", "size", "payload"):
        if field not in parsed:
            raise ValueError(f"{path}: missing manifest field {field!r}")
    if not isinstance(parsed["size"], int):
        raise ValueError(f"{path}: 'size' must be an int")
    if not isinstance(parsed["payload"], dict):
        raise ValueError(f"{path}: 'payload' must be an object")
    if not isinstance(parsed["item_class"], str):
        raise ValueError(f"{path}: 'item_class' must be a string")
    if not isinstance(parsed["name"], str):
        raise ValueError(f"{path}: 'name' must be a string")

    if stat.st_size != parsed["size"]:
        raise ValueError(
            f"{path}: apparent size {stat.st_size} does not match"
            f" encoded size {parsed['size']}; manifest is corrupt"
        )

    return ManifestHeader(
        item_class=parsed["item_class"],  # type: ignore[arg-type]
        name=parsed["name"],
        size=parsed["size"],
        payload=parsed["payload"],
    )


# ---------------------------------------------------------------------------
# ManifestSet + emit_all_manifests


@dataclasses.dataclass(frozen=True)
class ManifestSet:
    """The full collection of manifests written by a pre-flight pass."""

    target_dir: pathlib.Path
    headers: tuple[ManifestHeader, ...]

    @property
    def by_class(self) -> dict[ItemClass, tuple[ManifestHeader, ...]]:
        """Group ``headers`` by ``item_class``.

        Every known :class:`ItemClass` key is present, defaulting to an
        empty tuple — callers can iterate without ``KeyError`` even on
        configurations that produce zero items in some class.
        """
        groups: dict[ItemClass, list[ManifestHeader]] = {
            cls: [] for cls in _ALL_ITEM_CLASSES
        }
        for header in self.headers:
            groups[header.item_class].append(header)
        return {cls: tuple(items) for cls, items in groups.items()}


def emit_all_manifests(
    *,
    target_dir: pathlib.Path,
    sys_name: str,
    variants: Iterable[VariantSpec],
    toolchain_specs: Iterable[tuple[str, str]],
    common_deps: Iterable[tuple[str, str]],
    num_workers: int,
) -> ManifestSet:
    """Produce one ManifestHeader per queue item; write each to disk.

    Ordering of the returned ``headers`` tuple is deterministic and
    follows the phase sequence: phase1a, phase1a_barrier, phase1b_merge,
    phase1b_barrier, phase2 (toolchains then common_deps), phase2_barrier,
    phase3 variants. The Rust scheduler re-sorts by ``size`` DESC at
    dispatch, so this in-memory order is purely for debug clarity.

    ``num_workers`` controls how many barrier sentinels are emitted at
    each barrier rank — one per worker, so all workers eventually pull a
    barrier item and synchronise on the flag file.
    """
    if num_workers < 1:
        raise ValueError(
            f"num_workers must be >= 1, got {num_workers}"
        )

    target_dir = pathlib.Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    variants_tuple = tuple(variants)
    shards = split_into_shards(variants_tuple)

    headers: list[ManifestHeader] = []

    # Phase 1a — one per shard.
    for shard in shards:
        headers.append(make_partition_shard_header(shard))

    # Phase 1a barrier — one sentinel per worker.
    for index in range(num_workers):
        headers.append(make_partition_barrier_header(index, num_workers))

    # Phase 1b — singleton merge.
    headers.append(make_merge_header())

    # Phase 1b barrier.
    for index in range(num_workers):
        headers.append(make_merge_barrier_header(index, num_workers))

    # Phase 2 — toolchains, then common deps.
    for arch, compiler_label in toolchain_specs:
        headers.append(
            make_toolchain_header(sys_name, arch, compiler_label)
        )
    for drv, label in common_deps:
        headers.append(make_common_dep_header(drv, label))

    # Phase 2 barrier.
    for index in range(num_workers):
        headers.append(make_phase2_barrier_header(index, num_workers))

    # Phase 3 — variants.
    for variant in variants_tuple:
        headers.append(make_variant_header(variant, sys_name))

    for header in headers:
        write_manifest(target_dir, header)

    return ManifestSet(
        target_dir=target_dir, headers=tuple(headers)
    )

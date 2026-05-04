"""Pre-flight manifest generation for the compiler-suit runner.

The dynamic_runner framework discovers queue items by scanning a target
directory for manifest files. Each manifest is a small JSON document
describing the item's class (which worker should pick it up) and a
class-specific payload (pkg/arch/drv/...).

Memory budgeting is disabled for this task: every ``size`` field is
``0`` and :class:`SuitTask.estimate_memory` returns a fixed 1-byte
constant, so the framework's resource scheduler treats all items as
zero-cost and packs purely by ``--jobs N`` worker count. Phase / type
ordering is owned by :class:`PhaseSpec.depends_on` declared on the task.

This module produces manifests for all five item classes in the plan's
phase sequence:

* ``phase1a_partition`` — one per (pkg, arch) shard
* ``phase1b_merge``     — exactly one merge item
* ``phase2_toolchain``  — one per (arch, compiler_label) cross-toolchain
* ``phase2_common_dep`` — one per common host dep drv
* ``phase3_variant``    — one per matrix variant

Iteration order in the returned :class:`ManifestSet` follows the phase
sequence.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from collections.abc import Iterable
from typing import Final, Literal

from compiler_suit_runner.partition import Shard, VariantSpec, split_into_shards


# ---------------------------------------------------------------------------
# Types

ItemClass = Literal[
    "phase1a_partition",
    "phase1b_merge",
    "phase2_toolchain",
    "phase2_common_dep",
    "phase3_variant",
]


# All known item classes, in the canonical iteration order. Used so
# ``ManifestSet.by_class`` can return an empty tuple for absent classes
# rather than raising ``KeyError``.
_ALL_ITEM_CLASSES: tuple[ItemClass, ...] = (
    "phase1a_partition",
    "phase1b_merge",
    "phase2_toolchain",
    "phase2_common_dep",
    "phase3_variant",
)


@dataclasses.dataclass(frozen=True)
class ManifestHeader:
    """JSON-serialisable description of one queue item.

    ``size`` is retained as a field for backward compatibility with the
    framework's :class:`TaskInfo.size` slot but is always 0 — memory
    budgeting is disabled for this task.
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
        size=0,
        payload=payload,
    )


def make_merge_header() -> ManifestHeader:
    """Build the singleton phase-1b merge manifest."""
    return ManifestHeader(
        item_class="phase1b_merge",
        name="phase1b_merge",
        size=0,
        payload={},
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
        size=0,
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
        size=0,
        payload={
            "drv": drv,
            "label": label,
            "attr": drv,
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
    """Build a phase-3 variant manifest."""
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
        size=0,
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

    The on-disk file is just the JSON document (a few hundred bytes).
    """
    target_dir = pathlib.Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{header.name}.json"
    tmp = target.with_suffix(target.suffix + ".tmp")

    data = json.dumps(_header_to_jsonable(header), sort_keys=True, indent=2)
    encoded = data.encode("utf-8")

    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    return target


# A manifest's JSON content is sub-kilobyte; reading 64 KiB is
# generously safe and tolerates legacy sparse-padded files (older
# write_manifest revisions ftruncate'd to a multi-GiB tail).
_HEADER_READ_LIMIT_BYTES: Final[int] = 64 * 1024


def read_manifest(path: pathlib.Path) -> ManifestHeader:
    """Inverse of :func:`write_manifest`.

    Reads up to ``_HEADER_READ_LIMIT_BYTES`` and strips any trailing
    NULs from legacy sparse-padded manifests before parsing.
    """
    path = pathlib.Path(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        head = os.read(fd, _HEADER_READ_LIMIT_BYTES)
    finally:
        os.close(fd)
    # Strip any trailing NULs from legacy sparse-padded manifests so
    # json.loads sees only the document; harmless for current
    # non-padded files (no NULs to strip).
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
    num_workers: int = 1,
) -> ManifestSet:
    """Produce one ManifestHeader per queue item; write each to disk.

    Ordering of the returned ``headers`` tuple is deterministic and
    follows the phase sequence: phase1a, phase1b_merge, phase2
    (toolchains then common_deps), phase3 variants. Phase ordering is
    enforced by the framework's :class:`PhaseSpec.depends_on` graph;
    no explicit barrier sentinels are emitted.

    ``num_workers`` is accepted for API compatibility with older
    callers; it is no longer used (the framework sizes its worker pool
    independently).
    """
    del num_workers  # accepted for compatibility; no longer used

    target_dir = pathlib.Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    variants_tuple = tuple(variants)
    shards = split_into_shards(variants_tuple)

    headers: list[ManifestHeader] = []

    # Phase 1a — one per shard.
    for shard in shards:
        headers.append(make_partition_shard_header(shard))

    # Phase 1b — singleton merge.
    headers.append(make_merge_header())

    # Phase 2 — toolchains, then common deps.
    for arch, compiler_label in toolchain_specs:
        headers.append(
            make_toolchain_header(sys_name, arch, compiler_label)
        )
    for drv, label in common_deps:
        headers.append(make_common_dep_header(drv, label))

    # Phase 3 — variants.
    for variant in variants_tuple:
        headers.append(make_variant_header(variant, sys_name))

    for header in headers:
        write_manifest(target_dir, header)

    return ManifestSet(
        target_dir=target_dir, headers=tuple(headers)
    )

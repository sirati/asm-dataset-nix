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

This module produces manifests for all known item classes in the plan's
phase sequence:

* ``phase0_eval``       — one per binary, distributed-eval Phase 0 task
* ``phase1a_partition`` — one per (pkg, arch) shard
* ``phase1b_merge``     — exactly one merge item
* ``phase2_toolchain``  — one per (arch, compiler_label) cross-toolchain
* ``phase2_common_dep`` — one per common host dep drv
* ``phase3_variant``    — one per matrix variant

Iteration order in the returned :class:`ManifestSet` follows the phase
sequence.

Stage taxonomy (see :func:`emit_all_manifests` ``stages`` kwarg) groups
item classes by submission lifecycle:

* ``"phase_minus1"`` — toolchain bootstrap tasks (``phase2_toolchain``)
* ``"phase0"``       — distributed eval tasks (``phase0_eval``)
* ``"phase1"``       — partition + merge + common-dep + variant build
  tasks (everything else; emitted by the primary at runtime via Q5
  ``primary.spawn_tasks`` once the distributed-eval path is wired)
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from collections.abc import Iterable
from typing import Literal, Optional

from compiler_suit_runner.partition import Shard, VariantSpec, split_into_shards


# ---------------------------------------------------------------------------
# Types

ItemClass = Literal[
    "phase0_eval",
    "phase1a_partition",
    "phase1b_merge",
    "phase2_toolchain",
    "phase2_toolchain_validate",
    "phase2_common_dep",
    "phase3_variant",
]


# All known item classes, in the canonical iteration order. Used so
# ``ManifestSet.by_class`` can return an empty tuple for absent classes
# rather than raising ``KeyError``.
_ALL_ITEM_CLASSES: tuple[ItemClass, ...] = (
    "phase0_eval",
    "phase1a_partition",
    "phase1b_merge",
    "phase2_toolchain",
    "phase2_toolchain_validate",
    "phase2_common_dep",
    "phase3_variant",
)


# Stage taxonomy — maps each lifecycle stage to the set of item classes
# it covers. Used by :func:`emit_all_manifests` to selectively emit
# only a subset of manifests when the submitter is only producing the
# Phase -1 + Phase 0 slice (Phase 1+ comes later via Q5 spawn_tasks).
Stage = Literal["phase_minus1", "phase0", "phase1"]

_STAGE_TO_CLASSES: dict[Stage, frozenset[ItemClass]] = {
    "phase_minus1": frozenset({
        "phase2_toolchain", "phase2_toolchain_validate",
    }),
    "phase0": frozenset({"phase0_eval"}),
    "phase1": frozenset({
        "phase1a_partition",
        "phase1b_merge",
        "phase2_common_dep",
        "phase3_variant",
    }),
}

_ALL_STAGES: tuple[Stage, ...] = ("phase_minus1", "phase0", "phase1")


@dataclasses.dataclass(frozen=True)
class ManifestHeader:
    """JSON-serialisable description of one queue item.

    ``size`` is retained as a field for backward compatibility with the
    framework's :class:`TaskInfo.size` slot but is always 0 — memory
    budgeting is disabled for this task.

    ``task_id`` is the framework's per-task identifier (Phase-1 of the
    task-deps API; required once the framework's PendingPool starts
    enforcing). Stable + unique across the run.

    ``task_depends_on`` is the tuple of ``task_id``s that must complete
    before this task is dispatchable. Empty means no deps (e.g.
    toolchains). Variants reference their toolchain's task_id so the
    scheduler can hold them until the toolchain build finishes.
    """

    item_class: ItemClass
    name: str
    size: int
    payload: dict
    task_id: str = ""
    task_depends_on: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Task-id helpers
#
# The framework's task-deps API (Phase 1: ``a1ebbaa``) carries
# ``task_id`` + ``task_depends_on`` per :class:`TaskInfo`. We mint
# ids that are stable (deterministic from the manifest's identity)
# and human-readable (no hashing) so log lines reference combos
# operators recognise. Charset is double-underscore-separated ASCII.


def toolchain_task_id(sys_name: str, arch: str, compiler_label: str) -> str:
    """Stable id for a phase-2 toolchain task."""
    return f"toolchain__{sys_name}__{arch}__{compiler_label}"


def common_dep_task_id(drv: str) -> str:
    """Stable id for a phase-2 common-dep task. Uses the drv's
    short hash (the ``hash-name`` segment of the store path) so two
    common deps with the same human-readable label but different
    derivations don't collide.
    """
    base = pathlib.Path(drv).name
    return f"common_dep__{base}"


def variant_task_id(variant: VariantSpec, sys_name: str) -> str:
    """Stable id for a phase-3 variant task. Uses the full variant
    label (already unique per dispatch — encodes pkg, arch, compiler,
    every flag axis) so it round-trips identifiably in logs.
    """
    return f"variant__{sys_name}__{variant['label']}"


def partition_task_id(shard: Shard) -> str:
    """Stable id for a phase-1a partition shard."""
    return f"partition__{shard.pkg}__{shard.arch}"


MERGE_TASK_ID = "merge__singleton"


def phase0_eval_task_id(binary: str) -> str:
    """Stable id for a phase 0 distributed-eval task. One task per
    binary (NOT per (binary, arch)) — see plan Part B.

    The eval worker, given the binary, walks every requested arch
    locally inside the single task and broadcasts every produced drv
    to its peers.
    """
    return f"phase0_eval__{binary}"


# ---------------------------------------------------------------------------
# Header constructors


def make_phase0_eval_header(
    binary: str,
    sys_name: str,
    archs: Iterable[str],
    suffixes: Iterable[str],
    *,
    variant_sample: Optional[int] = None,
    variant_seed: Optional[str] = None,
) -> ManifestHeader:
    """Build a phase 0 distributed-eval manifest.

    One task per binary. The eval worker (``workers/eval_worker.py``)
    runs ``nix-eval-jobs --flake .#dataset.<sys>.<binary>.<arch>`` for
    every arch in ``archs``, sampled per ``(variant_sample,
    variant_seed)``, collects the produced ``.drv`` paths, and
    broadcasts each drv to all peers via the
    ``/peer/path-broadcast-offer`` primitive.

    ``task_depends_on`` is left empty for now — once Phase -1
    toolchain bootstrap is wired, the phase 0 task should depend on
    every toolchain task whose outputs it needs in order to walk the
    flake's dataset attrs. TODO: reference Phase -1 toolchain task
    hashes once those tasks exist (see plan Part B step 3 of Phase
    -1).
    """
    archs_list = list(archs)
    suffixes_list = list(suffixes)
    payload: dict = {
        "binary": binary,
        "sys": sys_name,
        "archs": archs_list,
        "suffixes": suffixes_list,
        "attr": f"dataset.{sys_name}.{binary}",
    }
    if variant_sample is not None:
        payload["variant_sample"] = variant_sample
    if variant_seed is not None:
        payload["variant_seed"] = variant_seed
    return ManifestHeader(
        item_class="phase0_eval",
        name=f"phase0_eval__{binary}",
        size=0,
        payload=payload,
        task_id=phase0_eval_task_id(binary),
        task_depends_on=(),
    )


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
        task_id=partition_task_id(shard),
    )


def make_merge_header() -> ManifestHeader:
    """Build the singleton phase-1b merge manifest. Depends on every
    partition shard (in practice the framework's phase-level
    ``depends_on=("phase1a",)`` makes this redundant, but we set it
    explicitly so the dep graph is self-describing in the manifest)."""
    return ManifestHeader(
        item_class="phase1b_merge",
        name="phase1b_merge",
        size=0,
        payload={},
        task_id=MERGE_TASK_ID,
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
        task_id=toolchain_task_id(sys_name, arch, compiler_label),
    )


def make_toolchain_validate_header(
    sys_name: str,
    arch: str,
    compiler_label: str,
    drv: str,
    outpath: str | None = None,
) -> ManifestHeader:
    """Build a phase-2 toolchain *validate-only* manifest.

    Emitted instead of :func:`make_toolchain_header` when the dispatch
    runs with ``--allow-toolchain-build`` off (the default). The
    build_worker handler for this class fetches the toolchain from a
    peer (primary first per the placement map) instead of building
    from source; the primary is responsible for having every
    toolchain output already realised before dispatch.

    Payload mirrors :func:`make_toolchain_header` so dispatch code
    paths that key off the ``drv`` / ``attr`` / ``compiler_label``
    fields keep working; the differentiator is the ``item_class``
    string + the ``validate_only`` flag (set as a belt-and-braces
    marker for forward compatibility if we want to thread per-item
    behaviour through the validate path later). ``outpath`` is the
    realised store path of the drv's ``out`` output; when present
    the worker can skip ``nix derivation show`` and go straight to
    a path-info check.
    """
    payload: dict = {
        "sys": sys_name,
        "arch": arch,
        "compiler_label": compiler_label,
        "attr": f"_crossToolchainMap.{sys_name}.{arch}.{compiler_label}",
        "drv": drv,
        "validate_only": True,
    }
    if outpath is not None:
        payload["outpath"] = outpath
    return ManifestHeader(
        item_class="phase2_toolchain_validate",
        name=f"toolchain_validate__{arch}__{compiler_label}",
        size=0,
        payload=payload,
        task_id=toolchain_task_id(sys_name, arch, compiler_label),
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
        task_id=common_dep_task_id(drv),
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
    variant: VariantSpec,
    sys_name: str,
    *,
    input_drvs: Optional[frozenset[str]] = None,
    drv_outpaths: Optional[dict[str, str]] = None,
    preferred_secondaries: Optional[list[str]] = None,
) -> ManifestHeader:
    """Build a phase-3 variant manifest.

    ``task_depends_on`` references the corresponding toolchain task —
    when the framework's task-dep scheduler enforces (Phase 2 of the
    task-deps API), this variant won't be dispatched until its
    toolchain has been built/substituted, eliminating the worker-slot
    waste of variants spinning on nix-daemon's build-lock waiting for
    their toolchain to come online.

    ``input_drvs`` (when provided) carries the variant's transitive
    ``inputDrvs`` set — every drv path the variant build can read
    from the local store. ``drv_outpaths`` maps each drv to its
    realised ``outputs.out.path``. The pair is embedded in the
    variant payload so the secondary's build_worker can pre-fetch
    input deps from the cluster placement map before the actual
    ``nix build``. When either is absent, no pre-fetch list is
    emitted and the variant falls back to nix's native substituter
    resolution (cache.nixos.org + the peer federation).

    ``preferred_secondaries`` carries the K=3 scheduling-affinity
    hint: the list of secondary-ids known (at manifest-emission time)
    to hold this variant's toolchain. The framework scheduler reads
    it and prefers a candidate from the list when a free worker is
    available there (see plan: Framework Ask #2 — requires upstream
    support to take effect). Empty / ``None`` = no preference.
    """
    pkg = variant["pkg"]
    arch = variant["arch"]
    label = variant["label"]
    compiler_id = variant["compiler_id"]
    suffix = _label_to_attr_suffix(label)
    payload = {
        "sys": sys_name,
        "pkg": pkg,
        "arch": arch,
        "label": label,
        "drv": variant["drv"],
        "variant_dir": variant["variant_dir"],
        "metadata_name": variant["metadata_name"],
        "compiler_id": compiler_id,
        "compiler_family": variant.get("compiler_family", ""),
        "compiler_version": variant.get("compiler_version", ""),
        "optimization": variant.get("optimization", ""),
        "flag_set": variant.get("flag_set", ""),
        "hardening": variant.get("hardening", ""),
        "sanitizer": variant.get("sanitizer", ""),
        "march": variant.get("march", ""),
        "tier": variant["tier"],
        "attr": f"dataset.{sys_name}.{pkg}.{arch}.{suffix}",
    }
    if input_drvs and drv_outpaths:
        # Only ship the inputs that we actually have an outpath
        # mapping for; the rest stay implicit (worker falls back to
        # native substituter resolution). Sorted for deterministic
        # JSON output so manifest diffs stay readable across runs.
        kept_inputs = sorted(d for d in input_drvs if d in drv_outpaths)
        if kept_inputs:
            payload["input_drvs"] = kept_inputs
            payload["input_outpaths"] = {
                d: drv_outpaths[d] for d in kept_inputs
            }
    if preferred_secondaries:
        # Deterministic order so manifest diffs are stable; the
        # scheduler treats this as an unordered preference set anyway.
        payload["preferred_secondaries"] = sorted(preferred_secondaries)
    return ManifestHeader(
        item_class="phase3_variant",
        name=label,
        size=0,
        payload=payload,
        task_id=variant_task_id(variant, sys_name),
        task_depends_on=(toolchain_task_id(sys_name, arch, compiler_id),),
    )


# ---------------------------------------------------------------------------
# Manifest IO


def _header_to_jsonable(header: ManifestHeader) -> dict:
    out = {
        "item_class": header.item_class,
        "name": header.name,
        "size": header.size,
        "payload": header.payload,
    }
    # task_id / task_depends_on are emitted only when populated so
    # legacy manifests round-trip unchanged (older preflight outputs
    # without these fields parse cleanly via the read-side defaults).
    if header.task_id:
        out["task_id"] = header.task_id
    if header.task_depends_on:
        out["task_depends_on"] = list(header.task_depends_on)
    return out


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


def read_manifest(path: pathlib.Path) -> ManifestHeader:
    """Inverse of :func:`write_manifest`.

    Reads the whole file (phase0_eval manifests carry the full
    per-binary suffix list and can run into the megabytes) and strips
    any trailing NULs from legacy sparse-padded manifests before
    parsing.
    """
    path = pathlib.Path(path)
    head = path.read_bytes()
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

    # task_id / task_depends_on are optional on disk: legacy manifests
    # don't have them, and the framework's default-empty serde on the
    # wire makes that a no-op for older runs. Pre-validate types so a
    # malformed sidecar doesn't crash the secondary's manifest scan.
    raw_task_id = parsed.get("task_id", "")
    if not isinstance(raw_task_id, str):
        raise ValueError(f"{path}: 'task_id' must be a string")
    raw_deps = parsed.get("task_depends_on", [])
    if not isinstance(raw_deps, list) or not all(
        isinstance(d, str) for d in raw_deps
    ):
        raise ValueError(
            f"{path}: 'task_depends_on' must be a list of strings"
        )

    return ManifestHeader(
        item_class=parsed["item_class"],  # type: ignore[arg-type]
        name=parsed["name"],
        size=parsed["size"],
        payload=parsed["payload"],
        task_id=raw_task_id,
        task_depends_on=tuple(raw_deps),
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


def emit_phase0_eval_manifests(
    per_binary_metadata: dict[str, dict],
    *,
    sys_name: str,
) -> list[ManifestHeader]:
    """Build one phase 0 distributed-eval manifest header per binary.

    ``per_binary_metadata`` maps a binary name to a metadata dict of
    shape::

        {
            "archs": ["x86_64", "aarch64", ...],
            "suffixes": ["O0", "O2", ...],
            "variant_sample": 64,    # optional
            "variant_seed": "...",   # optional
        }

    This shape is what :func:`compiler_suit_runner.preflight
    .enumerate_variants` returns when invoked in
    ``defer_to_phase0=True`` mode (see plan Part B, ``preflight.py``
    split).

    Each emitted header has ``task_depends_on=()`` for now. Once
    Phase -1 toolchain bootstrap tasks exist, this should reference
    the relevant toolchain task ids — see TODO inline in
    :func:`make_phase0_eval_header`.

    The function returns the list of headers; the caller is
    responsible for writing them to disk (typically via
    :func:`emit_all_manifests(stages=["phase_minus1", "phase0"])`,
    which delegates here).
    """
    headers: list[ManifestHeader] = []
    # Sort binaries for deterministic iteration order so test
    # assertions and operator log lines are stable across runs.
    for binary in sorted(per_binary_metadata.keys()):
        meta = per_binary_metadata[binary]
        archs = meta.get("archs", ())
        suffixes = meta.get("suffixes", ())
        variant_sample = meta.get("variant_sample")
        variant_seed = meta.get("variant_seed")
        headers.append(
            make_phase0_eval_header(
                binary=binary,
                sys_name=sys_name,
                archs=archs,
                suffixes=suffixes,
                variant_sample=variant_sample,
                variant_seed=variant_seed,
            )
        )
    return headers


def emit_all_manifests(
    *,
    target_dir: pathlib.Path,
    sys_name: str,
    variants: Iterable[VariantSpec],
    toolchain_specs: Iterable[tuple[str, str]],
    common_deps: Iterable[tuple[str, str]],
    num_workers: int = 1,
    toolchain_drvs: Optional[dict[tuple[str, str], str]] = None,
    allow_toolchain_build: bool = False,
    per_variant_inputs: Optional[dict[str, frozenset[str]]] = None,
    drv_outpaths: Optional[dict[str, str]] = None,
    toolchain_outpath_placements: Optional[dict[str, list[str]]] = None,
    per_binary_metadata: Optional[dict[str, dict]] = None,
    stages: Optional[list[str]] = None,
) -> ManifestSet:
    """Produce one ManifestHeader per queue item; write each to disk.

    Ordering of the returned ``headers`` tuple is deterministic and
    follows the phase sequence: phase0_eval, phase1a, phase1b_merge,
    phase2 (toolchains then common_deps), phase3 variants. Phase
    ordering is enforced by the framework's
    :class:`PhaseSpec.depends_on` graph; no explicit barrier sentinels
    are emitted.

    ``allow_toolchain_build`` flips phase-2 toolchain emission between
    the legacy build-from-source ``phase2_toolchain`` class (True) and
    the new validate-only ``phase2_toolchain_validate`` class (False,
    the default for production dispatches).

    ``per_variant_inputs`` + ``drv_outpaths`` (both optional) carry
    the per-variant transitive ``inputDrvs`` sets and the global
    drv→outpath mapping. When both are present, each variant
    manifest's payload gets ``input_drvs`` + ``input_outpaths``
    fields so the secondary's build_worker can pre-fetch deps from
    the cluster placement map before the build.

    ``num_workers`` is accepted for API compatibility with older
    callers; it is no longer used (the framework sizes its worker pool
    independently).

    ``per_binary_metadata`` carries the per-binary input for the
    Phase 0 distributed-eval tasks (see
    :func:`emit_phase0_eval_manifests` for the shape). When None, no
    phase0_eval manifests are emitted regardless of ``stages``.

    ``stages`` selects which lifecycle stages to emit:

    * ``None`` (default, legacy): emit every class — used by callers
      that still run the monolithic submitter flow.
    * a list of values from ``{"phase_minus1", "phase0", "phase1"}``:
      emit only the classes whose stage is in the list. The new
      submit-time path (distributed-eval) passes
      ``stages=["phase_minus1", "phase0"]`` so that Phase 1+ tasks
      can be spawned at runtime by the primary via Q5
      ``primary.spawn_tasks`` instead.
    """
    del num_workers  # accepted for compatibility; no longer used

    target_dir = pathlib.Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Stale manifests from a prior dispatch in the same shared-fs would
    # land in PendingPool with whatever task_id they had on disk —
    # including the empty-string default that pre-dates Phase-1
    # plumbing — and the framework's duplicate-id check rejects them.
    # Keep underscore- and dot-prefixed files (``_meta.json``, etc.)
    # because :meth:`SuitTask.discover_items` already filters them out
    # of the dispatch.
    for stale in target_dir.iterdir():
        if not stale.is_file() or stale.suffix != ".json":
            continue
        if stale.name.startswith(("_", ".")):
            continue
        stale.unlink()

    # Resolve which item classes are in-scope for this emission.
    if stages is None:
        active_classes: frozenset[ItemClass] = frozenset(_ALL_ITEM_CLASSES)
    else:
        unknown = [s for s in stages if s not in _STAGE_TO_CLASSES]
        if unknown:
            raise ValueError(
                f"emit_all_manifests: unknown stage(s) {unknown!r}; "
                f"valid stages are {list(_STAGE_TO_CLASSES.keys())}"
            )
        merged: set[ItemClass] = set()
        for stage in stages:
            merged.update(_STAGE_TO_CLASSES[stage])  # type: ignore[index]
        active_classes = frozenset(merged)

    variants_tuple = tuple(variants)

    headers: list[ManifestHeader] = []

    # Phase 0 — distributed-eval tasks (one per binary). Emitted
    # only when ``per_binary_metadata`` is provided AND the phase0
    # stage is active; legacy callers don't pass either.
    if "phase0_eval" in active_classes and per_binary_metadata:
        headers.extend(
            emit_phase0_eval_manifests(
                per_binary_metadata, sys_name=sys_name
            )
        )

    # Phase 1a + Phase 1b are computed inline on the primary (job-list
    # creation belongs there — secondaries have empty /nix/stores and
    # can't walk drv graphs). The dispatch only ships phase 2 + 3
    # build manifests; ``common_deps`` arrives pre-classified from the
    # primary-side partition step (currently empty until that step is
    # implemented; phase 3 builds substitute their host deps directly
    # via the federated peer cache).

    # Phase 2 — toolchains, then common deps. Toolchain manifests
    # carry the realised drv path when available so build_worker on
    # the secondary builds via ``nix build <drv>^*`` (which can
    # substitute) instead of ``nix build <flake>#<attr>`` (which
    # would need flake.nix shipped to the secondary).
    tc_drvs = toolchain_drvs or {}
    outpaths_map = drv_outpaths or {}
    for arch, compiler_label in toolchain_specs:
        drv = tc_drvs.get((arch, compiler_label))
        if allow_toolchain_build or not drv:
            # Fall back to the build header when either:
            #  - the operator opted in via --allow-toolchain-build, or
            #  - we don't have a resolved drv path (validate-only
            #    can't fetch by-outpath without the drv → outpath
            #    mapping). The latter typically means
            #    eval_toolchain_drvs failed on the primary; logging
            #    here would be noisy because emit_all_manifests is
            #    also called from cached-preflight restoration. The
            #    CLI's primary-side toolchain check is the loud
            #    "no drv resolved" signal.
            if "phase2_toolchain" in active_classes:
                headers.append(
                    make_toolchain_header(sys_name, arch, compiler_label, drv=drv)
                )
        else:
            if "phase2_toolchain_validate" in active_classes:
                headers.append(
                    make_toolchain_validate_header(
                        sys_name, arch, compiler_label, drv,
                        outpath=outpaths_map.get(drv),
                    )
                )
    if "phase2_common_dep" in active_classes:
        for drv, label in common_deps:
            headers.append(make_common_dep_header(drv, label))

    # Phase 3 — variants. Inputs map keys by ``variant['label']``
    # (matches compute_partition_locally's per_variant dict).
    if "phase3_variant" in active_classes:
        inputs_by_label = per_variant_inputs or {}
        placements_by_outpath = toolchain_outpath_placements or {}
        for variant in variants_tuple:
            # K=3 scheduling affinity: look up the variant's toolchain
            # drv → outpath → placement holders. Empty list when the
            # toolchain hasn't been placed yet (typical at submit time).
            tc_drv = tc_drvs.get((variant["arch"], variant["compiler_id"]))
            preferred: Optional[list[str]] = None
            if tc_drv:
                tc_outpath = outpaths_map.get(tc_drv)
                if tc_outpath:
                    preferred = placements_by_outpath.get(tc_outpath) or None
            headers.append(
                make_variant_header(
                    variant, sys_name,
                    input_drvs=inputs_by_label.get(variant["label"]),
                    drv_outpaths=outpaths_map if outpaths_map else None,
                    preferred_secondaries=preferred,
                )
            )

    for header in headers:
        write_manifest(target_dir, header)

    return ManifestSet(
        target_dir=target_dir, headers=tuple(headers)
    )

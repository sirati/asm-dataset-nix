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

This module produces on-disk JSON manifests for the build-shaped item
classes only. The ``matrix_eval`` (phase 2) and ``dependency_graph``
(phase 3) phases are 100% JSON-free: nothing is written here and
nothing is read back by :meth:`SuitTask.discover_items` for them.
matrix_eval is exactly one task per binary and dependency_graph is
exactly ONE all-binaries task, both built directly in-memory in
:meth:`SuitTask.discover_items` from the preflight's per-binary
metadata (threaded onto ``SuitTaskConfig.per_binary_metadata``).
``make_matrix_eval_header`` remains as the in-memory constructor for
the matrix_eval TaskInfos; its output is never serialised here.

JSON-backed item classes:

* ``build_compilers``     — one per (arch, compiler_label) cross-toolchain
                            (optional; gated by ``--build-compilers``)
* ``toolchain_validate``  — one per (arch, compiler_label) when the
                            ``--debug-testbuild`` opt-in is set
* ``build_common_dep``    — one per common host dep drv
* ``build_variant``       — one per matrix variant

Iteration order in the returned :class:`ManifestSet` follows the phase
sequence.

Stage taxonomy (see :func:`emit_all_manifests` ``stages`` kwarg) groups
JSON-backed item classes by submission lifecycle:

* ``"build_compilers"`` — toolchain bootstrap tasks (``build_compilers``,
                          ``toolchain_validate``)
* ``"build"``           — common-dep + variant build tasks (everything
                          else; emitted by the primary at runtime via
                          ``primary.spawn_tasks`` from the
                          dependency_graph planner)
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal, Optional

from compiler_suit_runner.partition import VariantSpec


# ---------------------------------------------------------------------------
# Types


class Phase(StrEnum):
    """The four dynamic_runner phase ids, defined once.

    Single source of truth so phase names can't drift or be misspelled
    across ``_classify`` / ``_phase_specs`` / ``on_phase_end`` and the
    cross-phase ``TaskDep`` wiring. Members are plain strings (StrEnum),
    so they pass straight through to ``PhaseSpec(phase_id=...)`` /
    ``TaskDep(phase_id=...)`` and compare equal to the bare framework
    strings the runtime hands back.

    NOTE: a task's id should be phase-LOCAL — the phase is carried by
    the framework's ``(phase_id, task_id)`` identity, not embedded in
    the ``task_id`` string (see :func:`matrix_eval_task_id` and
    :func:`build_compilers_task_id`).
    """

    BUILD_COMPILERS = "build_compilers"
    MATRIX_EVAL = "matrix_eval"
    DEPENDENCY_GRAPH = "dependency_graph"
    BUILD = "build"


ItemClass = Literal[
    "matrix_eval",
    "dependency_graph",
    "build_compilers",
    "toolchain_validate",
    "build_common_dep",
    "build_variant",
]


# Item classes emitted as on-disk JSON manifests by
# :func:`emit_all_manifests`, in the canonical iteration order. Used so
# ``ManifestSet.by_class`` can return an empty tuple for absent classes
# rather than raising ``KeyError``.
#
# matrix_eval / dependency_graph are deliberately absent: those phases
# are JSON-free (their TaskInfos are built in-memory in
# :meth:`SuitTask.discover_items`), so emit_all_manifests never writes
# a manifest for them. ``make_matrix_eval_header`` still exists — it is
# used to build the matrix_eval TaskInfos in-memory — but its output is
# never serialised through this module.
_ALL_ITEM_CLASSES: tuple[ItemClass, ...] = (
    "build_compilers",
    "toolchain_validate",
    "build_common_dep",
    "build_variant",
)


# Stage taxonomy — maps each lifecycle stage to the set of item classes
# it covers. Used by :func:`emit_all_manifests` to selectively emit
# only a subset of manifests when the submitter is only producing the
# pre-dependency-graph slice (the ``build`` stage's tasks land later via
# ``primary.spawn_tasks``).
#
# matrix_eval (phase 2) and dependency_graph (phase 3) are NOT in this
# taxonomy: those phases are 100% JSON-free. Their TaskInfos are built
# directly in-memory by :meth:`SuitTask.discover_items` from the
# preflight's per-binary metadata (threaded onto ``SuitTaskConfig``),
# so :func:`emit_all_manifests` neither writes nor reads any manifest
# for them. This module's JSON machinery covers only build_compilers /
# toolchain_validate / build phases.
Stage = Literal[
    "build_compilers",
    "build",
]

_STAGE_TO_CLASSES: dict[Stage, frozenset[ItemClass]] = {
    "build_compilers": frozenset({
        "build_compilers", "toolchain_validate",
    }),
    "build": frozenset({
        "build_common_dep",
        "build_variant",
    }),
}

_ALL_STAGES: tuple[Stage, ...] = (
    "build_compilers",
    "build",
)


@dataclasses.dataclass(frozen=True)
class ManifestHeader:
    """JSON-serialisable description of one queue item.

    ``size`` is retained as a field for backward compatibility with the
    framework's :class:`TaskInfo.size` slot but is always 0 — memory
    budgeting is disabled for this task.

    ``task_id`` is the framework's per-task identifier (Phase-1 of the
    task-deps API; required once the framework's PendingPool starts
    enforcing). Stable + unique across the run.

    ``task_depends_on`` is the tuple of INTRA-phase ``task_id``s that
    must complete before this task is dispatchable. Bare strings resolve
    to the enclosing task's own phase at the framework's PyO3 boundary,
    so this field is for same-phase prerequisites only (e.g. a variant's
    ``build_common_dep`` siblings). Empty means no intra-phase deps.

    ``build_compilers_depends_on`` carries CROSS-phase prerequisites that
    live in :attr:`Phase.BUILD_COMPILERS` (a ``build_variant`` in
    :attr:`Phase.BUILD` naming its toolchain build). These are carried
    separately from ``task_depends_on`` because a dependency's full
    identity is ``(phase_id, task_id)``: a bare-string dep would resolve
    to the variant's own (BUILD) phase and be flagged a missing dep. The
    runner wraps each id here in a ``TaskDep(phase_id=BUILD_COMPILERS)``
    before handing it to the framework (see
    ``SuitTask._task_info_from_header``).

    ``priority_hint`` is a non-negative scheduling-priority bias (0 is
    the default neutral value; higher = earlier). The dependency_graph
    planner uses it to tag cross-arch / per-family meta
    ``build_common_dep`` tasks — the most-shared artefacts — so they
    surface ahead of per-arch variant builds. The field is emitted on
    disk only when non-zero so legacy manifests round-trip unchanged
    and the framework's wire-format remains free to ignore the hint.
    """

    item_class: ItemClass
    name: str
    size: int
    payload: dict
    task_id: str = ""
    task_depends_on: tuple[str, ...] = ()
    build_compilers_depends_on: tuple[str, ...] = ()
    priority_hint: int = 0


# ---------------------------------------------------------------------------
# Task-id helpers
#
# The framework's task-deps API (Phase 1: ``a1ebbaa``) carries
# ``task_id`` + ``task_depends_on`` per :class:`TaskInfo`. We mint
# ids that are stable (deterministic from the manifest's identity)
# and human-readable (no hashing) so log lines reference combos
# operators recognise. Charset is double-underscore-separated ASCII.


def build_compilers_task_id(sys_name: str, arch: str, compiler_label: str) -> str:
    """Phase-local id for a build_compilers task: the bare
    ``<sys>__<arch>__<compiler>`` tuple.

    One task per (sys, arch, compiler). The id does NOT embed the phase
    — the framework's ``(phase_id, task_id)`` identity carries it
    (``phase_id`` = :attr:`Phase.BUILD_COMPILERS`); a dependant in
    another phase (a ``build_variant`` in :attr:`Phase.BUILD`) names this
    task via ``TaskDep(task_id=..., phase_id=Phase.BUILD_COMPILERS)``,
    threaded through ``build_compilers_depends_on`` (see
    :class:`ManifestHeader`).

    Distinct from :func:`toolchain_validate_task_id` (which keeps its
    ``toolchain_validate__`` prefix) because both classes can fire on the
    same ``(arch, compiler_label)`` and live in different phases; the
    framework rejects duplicate task ids within a phase.
    """
    return f"{sys_name}__{arch}__{compiler_label}"


def toolchain_validate_task_id(sys_name: str, arch: str, compiler_label: str) -> str:
    """Stable id for a toolchain_validate task. Distinct from
    :func:`build_compilers_task_id` because both classes can fire on
    the same ``(arch, compiler_label)`` when the operator passes
    ``--build-compilers --debug-testbuild &lt;binary&gt;``; the framework
    rejects duplicate task ids.
    """
    return f"toolchain_validate__{sys_name}__{arch}__{compiler_label}"


def common_dep_task_id(drv: str) -> str:
    """Stable id for a build_common_dep task. Uses the drv's
    short hash (the ``hash-name`` segment of the store path) so two
    common deps with the same human-readable label but different
    derivations don't collide. The ``build_common_dep__`` prefix
    matches the post-rename phase4 task taxonomy (see
    :mod:`dependency_graph_planner` for the binary/arch-scoped variant
    used by the primary-side spawn path).
    """
    base = pathlib.Path(drv).name
    return f"build_common_dep__{base}"


def variant_task_id(variant: VariantSpec, sys_name: str) -> str:
    """Stable id for a build_variant task. Embeds the binary (``pkg``)
    in the id alongside ``sys_name`` and the full variant label so the
    namespacing matches the dependency_graph_planner's
    ``build_variant__<sys>__<binary>__<label>`` shape one-to-one. The
    label already encodes arch + compiler + every flag axis so the
    overall id is unique per dispatch.
    """
    return f"build_variant__{sys_name}__{variant['pkg']}__{variant['label']}"


def matrix_eval_task_id(binary: str) -> str:
    """Phase-local id for a matrix_eval task: the bare binary name.

    One task per binary (NOT per (binary, arch)). The id does NOT embed
    the phase — the framework's ``(phase_id, task_id)`` identity carries
    it (``phase_id`` = :attr:`Phase.MATRIX_EVAL`); a dependant in another
    phase names this task with
    ``TaskDep(task_id=binary, phase_id=Phase.MATRIX_EVAL)``. The id
    doubles as the ``predecessor_outputs`` key, so the dependency_graph
    worker reads the binary name straight off it.

    The eval worker, given the binary, walks every requested arch
    locally inside the single task and broadcasts every produced drv
    to its peers.
    """
    return binary


# The dependency_graph phase is a SINGLE all-binaries framework task
# (NOT one per binary): its id is a stable constant and it depends on
# every phase-2 ``matrix_eval__<binary>`` task. The worker is invoked
# once over all binaries and plans one descriptor list covering the
# whole run.
DEPENDENCY_GRAPH_TASK_ID = "dependency_graph"


# ---------------------------------------------------------------------------
# Header constructors


def make_matrix_eval_header(
    binary: str,
    sys_name: str,
    archs: Iterable[str],
    *,
    toolchain_aggregate_drv: str,
    variant_sample: Optional[int] = None,
    variant_seed: Optional[str] = None,
    toolchain_dedup: bool = True,
) -> ManifestHeader:
    """Build a matrix_eval (distributed-eval) :class:`ManifestHeader`.

    matrix_eval is JSON-free: this header is consumed in-memory by
    :meth:`SuitTask.discover_items` (one TaskInfo per binary) and is
    never serialised through :func:`write_manifest` / read back via
    :func:`read_manifest`.

    One task per binary. The eval worker (``workers/eval_worker.py``)
    runs ``nix-eval-jobs --flake .#dataset.<sys>.<binary>.<arch>`` for
    every arch in ``archs``, sampled per ``(variant_sample,
    variant_seed)``, collects the produced ``.drv`` paths, and
    broadcasts each drv to all peers via the
    ``/peer/path-broadcast-offer`` primitive.

    ``toolchain_aggregate_drv`` is the store path of the phase-1
    ``toolchains`` aggregate drv (one per dispatch, produced by
    pre-flight). It is required: every matrix_eval header carries
    it so eval_worker can ``appendContext`` it into the per-binary
    matrix aggregate without re-evaluating the toolchain attr set.
    Manifests built before this field was wired are rejected by
    schema validation, forcing a clean fresh preflight.

    ``toolchain_dedup`` (default True) is the rollback switch for
    toolchain dedup: when True the eval worker subtracts the toolchain
    closure from the exported per-binary archive (the toolchain is
    shipped once via the pre-flight ``toolchains.drv.archive``); when
    False the per-binary archive exports the full closure as before.

    ``task_depends_on`` is left empty for now — once the
    ``build_compilers`` stage is wired (gated by ``--build-compilers``),
    matrix_eval should depend on every build_compilers task whose
    outputs it needs in order to walk the flake's dataset attrs.
    """
    if not isinstance(toolchain_aggregate_drv, str) or not toolchain_aggregate_drv:
        raise ValueError(
            "make_matrix_eval_header: 'toolchain_aggregate_drv' must be"
            f" a non-empty string, got {toolchain_aggregate_drv!r}"
        )
    archs_list = list(archs)
    payload: dict = {
        "binary": binary,
        "sys": sys_name,
        "archs": archs_list,
        "attr": f"dataset.{sys_name}.{binary}",
        "toolchain_aggregate_drv": toolchain_aggregate_drv,
        # Toolchain-dedup feature flag (default ON). When True the eval
        # worker exports only ``requisites(matrix) − requisites(toolchain)``
        # into the per-binary archive; when False it exports the full
        # closure (today's behaviour). The toolchain archive is imported
        # first by every consumer regardless, so this is the sole rollback.
        "toolchain_dedup": bool(toolchain_dedup),
    }
    if variant_sample is not None:
        payload["variant_sample"] = variant_sample
    if variant_seed is not None:
        payload["variant_seed"] = variant_seed
    return ManifestHeader(
        item_class="matrix_eval",
        name=f"matrix_eval__{binary}",
        size=0,
        payload=payload,
        task_id=matrix_eval_task_id(binary),
        task_depends_on=(),
    )


def make_build_compilers_header(
    sys_name: str,
    arch: str,
    compiler_label: str,
    drv: str | None = None,
) -> ManifestHeader:
    """Build a build_compilers cross-toolchain manifest.

    The build_compilers worker resolves the toolchain via the
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
        item_class="build_compilers",
        name=f"build_compilers__{arch}__{compiler_label}",
        size=0,
        payload=payload,
        task_id=build_compilers_task_id(sys_name, arch, compiler_label),
    )


def make_toolchain_validate_header(
    sys_name: str,
    arch: str,
    compiler_label: str,
    drv: str,
    outpath: str | None = None,
) -> ManifestHeader:
    """Build a toolchain *validate-only* manifest.

    Emitted instead of :func:`make_build_compilers_header` when the
    dispatch runs with ``--build-compilers`` off (the default). The
    build_worker handler for this class fetches the toolchain from a
    peer (primary first per the placement map) instead of building
    from source; the primary is responsible for having every
    toolchain output already realised before dispatch.

    Payload mirrors :func:`make_build_compilers_header` so dispatch
    code paths that key off the ``drv`` / ``attr`` / ``compiler_label``
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
        item_class="toolchain_validate",
        name=f"toolchain_validate__{arch}__{compiler_label}",
        size=0,
        payload=payload,
        task_id=toolchain_validate_task_id(sys_name, arch, compiler_label),
    )


def make_build_common_dep_header(drv: str, label: str) -> ManifestHeader:
    """Build a build_common_dep (common host-dep) manifest.

    The worker uses ``attr`` directly with ``nix build`` (it is a raw
    drvPath, not a flake attribute path).
    """
    return ManifestHeader(
        item_class="build_common_dep",
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


def make_build_variant_header(
    variant: VariantSpec,
    sys_name: str,
    *,
    input_drvs: Optional[frozenset[str]] = None,
    drv_outpaths: Optional[dict[str, str]] = None,
    preferred_secondaries: Optional[list[str]] = None,
    toolchain_task_id: Optional[str] = None,
    toolchain_outpath: Optional[str] = None,
) -> ManifestHeader:
    """Build a build_variant manifest.

    ``build_compilers_depends_on`` references the corresponding
    toolchain task — a CROSS-phase prerequisite in
    :attr:`Phase.BUILD_COMPILERS` (the variant itself lives in
    :attr:`Phase.BUILD`). When the framework's task-dep scheduler
    enforces (Phase 2 of the task-deps API), this variant won't be
    dispatched until its toolchain has been built/substituted,
    eliminating the worker-slot waste of variants spinning on
    nix-daemon's build-lock waiting for their toolchain to come online.
    The id is routed through the dedicated cross-phase field (not the
    intra-phase ``task_depends_on``) so the runner can tag it with
    ``phase_id=Phase.BUILD_COMPILERS``; there are no intra-phase deps on
    a variant today, so ``task_depends_on`` stays empty.

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

    ``toolchain_outpath`` is the realized ``/nix/store/…`` output path
    for the variant's toolchain.  Workers use it to locate the
    per-toolchain delta archive (``toolchains.<id>.out.archive``) under
    ``matrix_eval_out_dir`` via
    :func:`preflight.toolchain_delta_archive_name`.  When absent (older
    manifests or toolchain not yet realized), workers fall back to
    substitution.
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
    if toolchain_outpath:
        # Workers use this to import the right per-toolchain delta archive
        # before building.  Omitted when the toolchain outpath is unknown
        # at manifest-emission time; workers fall back to substitution.
        payload["toolchain_outpath"] = toolchain_outpath
    return ManifestHeader(
        item_class="build_variant",
        name=label,
        size=0,
        payload=payload,
        task_id=variant_task_id(variant, sys_name),
        task_depends_on=(),
        build_compilers_depends_on=(
            (toolchain_task_id,) if toolchain_task_id else ()
        ),
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
    # task_id / task_depends_on / build_compilers_depends_on /
    # priority_hint are emitted only when populated so legacy manifests
    # round-trip unchanged (older preflight outputs without these fields
    # parse cleanly via the read-side defaults).
    if header.task_id:
        out["task_id"] = header.task_id
    if header.task_depends_on:
        out["task_depends_on"] = list(header.task_depends_on)
    if header.build_compilers_depends_on:
        out["build_compilers_depends_on"] = list(
            header.build_compilers_depends_on
        )
    if header.priority_hint:
        out["priority_hint"] = header.priority_hint
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

    Reads the whole file (matrix_eval manifests carry the full
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
    raw_bc_deps = parsed.get("build_compilers_depends_on", [])
    if not isinstance(raw_bc_deps, list) or not all(
        isinstance(d, str) for d in raw_bc_deps
    ):
        raise ValueError(
            f"{path}: 'build_compilers_depends_on' must be a list of strings"
        )
    raw_priority = parsed.get("priority_hint", 0)
    # ``bool`` is a subclass of ``int``; reject explicitly so a JSON
    # ``true`` doesn't sneak in as 1.
    if not isinstance(raw_priority, int) or isinstance(raw_priority, bool):
        raise ValueError(f"{path}: 'priority_hint' must be an int")
    # ``ManifestHeader`` documents priority_hint as non-negative
    # (higher = earlier scheduling bias); reject negatives here so
    # malformed sidecars surface at load time rather than silently
    # de-prioritising tasks downstream.
    if raw_priority < 0:
        raise ValueError(
            f"{path}: 'priority_hint' must be non-negative, "
            f"got {raw_priority}"
        )

    return ManifestHeader(
        item_class=parsed["item_class"],  # type: ignore[arg-type]
        name=parsed["name"],
        size=parsed["size"],
        payload=parsed["payload"],
        task_id=raw_task_id,
        task_depends_on=tuple(raw_deps),
        build_compilers_depends_on=tuple(raw_bc_deps),
        priority_hint=raw_priority,
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
    toolchain_drvs: Optional[dict[tuple[str, str], str]] = None,
    allow_toolchain_build: bool = False,
    per_variant_inputs: Optional[dict[str, frozenset[str]]] = None,
    drv_outpaths: Optional[dict[str, str]] = None,
    toolchain_outpath_placements: Optional[dict[str, list[str]]] = None,
    per_binary_metadata: Optional[dict[str, dict]] = None,
    stages: Optional[list[str]] = None,
) -> ManifestSet:
    """Produce one ManifestHeader per JSON-backed queue item; write
    each to disk.

    Ordering of the returned ``headers`` tuple is deterministic and
    follows the phase sequence: build_compilers / toolchain_validate,
    build_common_dep, build_variant. (matrix_eval / dependency_graph
    are JSON-free — see the module docstring.) Phase ordering is
    enforced by the framework's
    :class:`PhaseSpec.depends_on` graph; no explicit barrier sentinels
    are emitted.

    ``allow_toolchain_build`` flips toolchain emission between the
    build-compilers ``build_compilers`` class (True) and the
    validate-only ``toolchain_validate`` class (False, the default for
    production dispatches).

    ``per_variant_inputs`` + ``drv_outpaths`` (both optional) carry
    the per-variant transitive ``inputDrvs`` sets and the global
    drv→outpath mapping. When both are present, each variant
    manifest's payload gets ``input_drvs`` + ``input_outpaths``
    fields so the secondary's build_worker can pre-fetch deps from
    the cluster placement map before the build.

    ``num_workers`` is accepted for API compatibility with older
    callers; it is no longer used (the framework sizes its worker pool
    independently).

    ``per_binary_metadata`` is accepted for call-site compatibility but
    is no longer used here: matrix_eval (phase 2) and dependency_graph
    (phase 3) are JSON-free and built in-memory by
    :meth:`SuitTask.discover_items`.

    ``stages`` selects which lifecycle stages to emit:

    * ``None`` (default, legacy): emit every JSON-backed class — used
      by callers that still run the monolithic submitter flow.
    * a list of values from ``{"build_compilers", "build"}``: emit only
      the classes whose stage is in the list. The submit-time path
      passes ``stages=["build_compilers"]`` (when ``--build-compilers``
      is set) or ``stages=[]`` so that ``build`` tasks can be spawned
      at runtime by the primary via ``primary.spawn_tasks`` instead.
    """
    del num_workers  # accepted for compatibility; no longer used
    del per_binary_metadata  # JSON-free phases 2/3 built in discover_items

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

    # matrix_eval (phase 2) and dependency_graph (phase 3) are NOT
    # emitted here: those phases are JSON-free. Their TaskInfos are
    # built directly in-memory by :meth:`SuitTask.discover_items` from
    # the preflight's per-binary metadata threaded onto
    # ``SuitTaskConfig.per_binary_metadata`` — no manifest written, no
    # manifest read. ``per_binary_metadata`` is accepted here only for
    # call-site compatibility (older callers pass it through); it has
    # no effect on the on-disk manifests.

    # build_compilers / toolchain_validate, then build_common_dep.
    # Toolchain manifests carry the realised drv path when available
    # so build_worker on the secondary builds via ``nix build <drv>^*``
    # (which can substitute) instead of ``nix build <flake>#<attr>``
    # (which would need flake.nix shipped to the secondary).
    tc_drvs = toolchain_drvs or {}
    outpaths_map = drv_outpaths or {}
    for arch, compiler_label in toolchain_specs:
        drv = tc_drvs.get((arch, compiler_label))
        # build_compilers and toolchain_validate are independent
        # classes. Both can fire on the same (arch, compiler) when
        # both stages are active (e.g. --build-compilers
        # --debug-testbuild hello: build the toolchain fresh AND
        # validate it). Emit build_compilers when (a) the operator
        # opted in or (b) no drv was resolved (validate needs the
        # drv→outpath mapping). Emit toolchain_validate whenever a
        # drv is available.
        if "build_compilers" in active_classes and (
            allow_toolchain_build or not drv
        ):
            headers.append(
                make_build_compilers_header(
                    sys_name, arch, compiler_label, drv=drv,
                )
            )
        if "toolchain_validate" in active_classes and drv:
            headers.append(
                make_toolchain_validate_header(
                    sys_name, arch, compiler_label, drv,
                    outpath=outpaths_map.get(drv),
                )
            )
    if "build_common_dep" in active_classes:
        for drv, label in common_deps:
            headers.append(make_build_common_dep_header(drv, label))

    # build_variant — inputs map keys by ``variant['label']``.
    if "build_variant" in active_classes:
        inputs_by_label = per_variant_inputs or {}
        placements_by_outpath = toolchain_outpath_placements or {}
        for variant in variants_tuple:
            # K=3 scheduling affinity: look up the variant's toolchain
            # drv → outpath → placement holders. Empty list when the
            # toolchain hasn't been placed yet (typical at submit time).
            tc_drv = tc_drvs.get((variant["arch"], variant["compiler_id"]))
            tc_outpath: Optional[str] = None
            preferred: Optional[list[str]] = None
            if tc_drv:
                tc_outpath = outpaths_map.get(tc_drv)
                if tc_outpath:
                    preferred = placements_by_outpath.get(tc_outpath) or None
            # Depend on whichever toolchain class was emitted for the
            # same (arch, compiler). Prefer build_compilers (the
            # realising task) over toolchain_validate (sanity probe);
            # when neither stage is active the operator pre-staged the
            # toolchain and the variant has no submit-time dep to wait on.
            tc_task_id: Optional[str] = None
            if "build_compilers" in active_classes:
                tc_task_id = build_compilers_task_id(
                    sys_name, variant["arch"], variant["compiler_id"],
                )
            elif "toolchain_validate" in active_classes:
                tc_task_id = toolchain_validate_task_id(
                    sys_name, variant["arch"], variant["compiler_id"],
                )
            headers.append(
                make_build_variant_header(
                    variant, sys_name,
                    input_drvs=inputs_by_label.get(variant["label"]),
                    drv_outpaths=outpaths_map if outpaths_map else None,
                    preferred_secondaries=preferred,
                    toolchain_task_id=tc_task_id,
                    toolchain_outpath=tc_outpath,
                )
            )

    for header in headers:
        write_manifest(target_dir, header)

    return ManifestSet(
        target_dir=target_dir, headers=tuple(headers)
    )

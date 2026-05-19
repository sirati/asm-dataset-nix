"""Adapter from ``template_graph.streaming.plan_from_tree_streaming`` to
phase-4 task descriptors.

Replaces the legacy ``phase1_planner.py`` refcount-and-walk model with a
direct translation of the streaming planner's classified template graph.

The streaming planner emits, per ``(template_id, arch)`` cell:

  * a ``VariantArray`` whose ``variants`` lists the (compiler-opt)
    labels covered (e.g. ``["gcc15-O0", "gcc15-O2", ...]``);
  * a per-node hash row ``hashes[node_id][variant_index]`` carrying the
    ``(hash, name)`` ident of the drv that occupies that role in that
    variant;
  * a classification per node — ``"common_dep"`` or
    ``"variant_specific"`` — produced by the calibration-pair
    invariants pass.

Every ``common_dep`` node deduplicates one shared sub-derivation across
all the array's variants; we emit one ``build_common_dep`` descriptor
per such node. Every variant becomes one ``build_variant`` descriptor
whose ``depends_on`` references the common_dep task_ids it actually
covers plus the toolchain task_ids advertised by the caller.

Decoupling notes
----------------

This module emits **descriptors**, not pre-built
``manifest_gen.ManifestHeader`` instances, for two reasons:

  1. ``manifest_gen`` is mid-rename (``phase2_common_dep`` →
     ``build_common_dep`` and ``phase3_variant`` → ``build_variant``).
     Descriptors let the integration site convert in either taxonomy
     without churning this module twice.
  2. The descriptor shape matches the ``primary_handle.spawn_tasks``
     contract one-to-one, so the wiring layer is a trivial loop with no
     hidden mapping logic.

The module does NOT import ``template_graph`` at module load: the
caller passes the streaming result as a plain dict (the same shape
``plan_from_tree_streaming`` returns, but also tolerant of a
JSON-roundtripped form produced by ``dependency_graph_worker``'s
``_dependency_graph.json``). This keeps unit tests dependency-free
and lets the worker either pickle dataclasses or serialise to JSON
without dragging the adapter into either choice.
"""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional


__all__ = [
    "DependencyGraphCycleError",
    "Phase4Descriptor",
    "BinaryPlanInput",
    "convert_toolchain_drvs",
    "plan_phase4_for_binary",
    "plan_phase4_from_graph",
    "headers_from_descriptors",
    "load_descriptors_from_json",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DependencyGraphCycleError(Exception):
    """Raised when walking the streaming planner's template graph
    encounters a cycle.

    Nix drv graphs are DAGs by construction, so this is a defensive
    guard rather than an expected control flow path. Surfacing the
    cycle as a typed exception lets the watcher layer treat it as a
    hard, non-retryable error (the matrix output is corrupt and no
    amount of redispatch will fix it).
    """


# ---------------------------------------------------------------------------
# Public descriptor types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Phase4Descriptor:
    """One phase-4 task description in a manifest-gen-agnostic shape.

    ``kind`` is either ``"build_common_dep"`` or ``"build_variant"``;
    integration code maps these to the current ``manifest_gen``
    constructor names. ``payload`` carries the worker-visible
    arguments; ``depends_on`` is the tuple of task_ids whose completion
    gates this task.
    """

    kind: str
    task_id: str
    name: str
    payload: dict
    depends_on: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class BinaryPlanInput:
    """Per-binary inputs for :func:`plan_phase4_for_binary`.

    ``streaming_result`` is the dict returned by
    ``template_graph.streaming.plan_from_tree_streaming`` (or a
    JSON-roundtripped equivalent) for THIS binary's sum-drv.

    ``variant_lookup`` maps ``(arch, variant_label)`` — where
    ``variant_label`` is what the streaming planner stores in
    ``VariantArray.variants`` (e.g. ``"gcc15-O2"``) — to the full
    variant descriptor (a ``VariantSpec``-shaped Mapping). The
    descriptor supplies the variant's drv path, output directory
    metadata, compiler ids and so on — i.e. everything the build
    worker needs that isn't visible in the streaming graph itself.
    Missing labels are skipped with no error (caller may legitimately
    drop variants between graph generation and planning).

    ``toolchain_task_ids`` maps a toolchain drv identifier
    (``"<hash>-<name>"`` — the format returned by
    :func:`convert_toolchain_drvs`) to its phase-1 ``build_compilers``
    task_id. Variants whose template touches that toolchain get the
    id wired into their ``depends_on``. The streaming planner's
    ``toolchain_node_ids_per_template`` map identifies which template
    nodes are toolchains (the cowalk short-circuits their subtrees so
    ``arr.hashes`` rows stay empty); we resolve those node_ids to
    idents by matching the TemplateNode's role-name against
    ``out.toolchain_drvs`` entries. Idents not present in
    ``toolchain_task_ids`` (operator-provided toolchains with no
    framework task) are silently omitted. Empty mapping is fine
    (variants then have only common-dep deps).
    """

    binary: str
    streaming_result: Mapping[str, Any]
    variant_lookup: Mapping[tuple[str, str], Mapping[str, Any]]
    toolchain_task_ids: Mapping[str, str] = dataclasses.field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# Shape translation: (hash, name) tuples ↔ "<hash>-<name>" strings
# ---------------------------------------------------------------------------


def _coerce_ident(raw: Any) -> Optional[tuple[str, str]]:
    """Normalise a single toolchain ident entry to ``(hash, name)``.

    Accepts:
      * ``(hash, name)`` tuples (native streaming output);
      * ``[hash, name]`` lists (JSON-roundtripped form);
      * ``"<hash>-<name>"`` strings (legacy refcount form).

    Returns ``None`` for anything else so the caller can decide whether
    to log + skip vs raise.
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        h, n = raw
        if isinstance(h, str) and isinstance(n, str):
            return h, n
        return None
    if isinstance(raw, list) and len(raw) == 2:
        h, n = raw
        if isinstance(h, str) and isinstance(n, str):
            return h, n
        return None
    if isinstance(raw, str):
        # ``<hash>-<name>`` — split on first dash. The hash is a
        # nixbase32 32-char fixed-length prefix, so any earlier dash
        # would corrupt; rely on it being well-formed at the caller.
        if "-" in raw:
            h, n = raw.split("-", 1)
            return h, n
        return None
    return None


def _ident_to_str(ident: tuple[str, str]) -> str:
    """Join ``(hash, name)`` into the legacy ``"<hash>-<name>"`` shape."""
    return f"{ident[0]}-{ident[1]}"


def convert_toolchain_drvs(raw: Iterable[Any]) -> set[str]:
    """Translate the streaming planner's ``set[(hash, name)]`` shape
    into the legacy ``set[str]`` shape expected by
    ``manifest_gen``-era code.

    Entries that fail to coerce are dropped (the caller's contract is
    "best-effort conversion" — a malformed entry shouldn't sink the
    whole plan; the upstream invariant checker will already have
    surfaced the malformation elsewhere if it matters).
    """
    out: set[str] = set()
    for entry in raw:
        ident = _coerce_ident(entry)
        if ident is None:
            continue
        out.add(_ident_to_str(ident))
    return out


# ---------------------------------------------------------------------------
# Helpers: reading the streaming dict's dataclass-or-dict cells
# ---------------------------------------------------------------------------


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``obj.name`` (dataclass) or ``obj[name]`` (dict)."""
    if obj is None:
        return default
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return default


def _template_nodes(template: Any) -> list[Any]:
    """Return ``template.nodes`` as a sequence; accepts dataclass or dict."""
    nodes = _attr_or_key(template, "nodes", []) or []
    return list(nodes)


def _node_field(node: Any, name: str, default: Any = None) -> Any:
    return _attr_or_key(node, name, default)


def _variant_array_fields(arr: Any) -> tuple[int, str, list[str], list[list]]:
    """Return ``(template_id, arch, variants, hashes)`` from a
    VariantArray dataclass or its JSON dict form."""
    template_id = _attr_or_key(arr, "template_id", 0)
    arch = _attr_or_key(arr, "arch", "")
    variants = list(_attr_or_key(arr, "variants", []) or [])
    hashes_raw = _attr_or_key(arr, "hashes", []) or []
    hashes: list[list] = [list(row) if row is not None else [] for row in hashes_raw]
    return int(template_id), str(arch), variants, hashes


def _iter_variant_arrays(
    variant_arrays: Any,
) -> Iterable[tuple[tuple[int, str], Any]]:
    """Yield ``((template_id, arch), VariantArray)`` pairs.

    The streaming planner returns a dict keyed by ``(int, str)``
    tuples. JSON roundtripping can stringify those keys (e.g.
    ``"3|x86_64"``); we accept either by inspecting the key shape.
    """
    if isinstance(variant_arrays, Mapping):
        for key, arr in variant_arrays.items():
            if isinstance(key, tuple) and len(key) == 2:
                yield (int(key[0]), str(key[1])), arr
            elif isinstance(key, str) and "|" in key:
                tid_s, arch = key.split("|", 1)
                yield (int(tid_s), arch), arr
            else:
                # Caller passed an unparseable key — surface as
                # zero/empty so downstream still works deterministically
                # rather than crashing the whole plan over one bad
                # cell.
                yield (0, str(key)), arr


def _arch_indep_idents_for_binary(
    arch_indep_deps_raw: Any,
    binary: str,
) -> list[tuple[str, str]]:
    """Extract this binary's arch-indep idents from the streaming
    result's ``arch_indep_deps`` field.

    Streaming-native form is ``dict[str, set[(hash, name)]]``. After a
    JSON roundtrip both the outer set→list and inner tuple→list
    coercions apply, so we accept either shape and emit a typed
    ``list[(hash, name)]``. Entries that fail to coerce are dropped —
    same best-effort policy as :func:`convert_toolchain_drvs`.
    """
    if not isinstance(arch_indep_deps_raw, Mapping):
        return []
    bucket = arch_indep_deps_raw.get(binary)
    if bucket is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in bucket:
        ident = _coerce_ident(entry)
        if ident is None or ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
    return out


def _iter_classifications(
    raw: Any,
) -> Iterable[tuple[tuple[int, str], dict[int, str]]]:
    """Same key-shape tolerance as :func:`_iter_variant_arrays`,
    yielding ``((template_id, arch), {node_id: classification})``.

    Node-id keys may be int (native) or str (JSON-roundtripped); we
    coerce to int.
    """
    if not isinstance(raw, Mapping):
        return
    for key, inner in raw.items():
        if isinstance(key, tuple) and len(key) == 2:
            tid, arch = int(key[0]), str(key[1])
        elif isinstance(key, str) and "|" in key:
            tid_s, arch = key.split("|", 1)
            tid = int(tid_s)
        else:
            tid = 0
            arch = str(key)
        normalised: dict[int, str] = {}
        if isinstance(inner, Mapping):
            for nid, cls in inner.items():
                try:
                    normalised[int(nid)] = str(cls)
                except (TypeError, ValueError):
                    continue
        yield (tid, arch), normalised


def _coerce_toolchain_node_ids(raw: Any) -> dict[int, list[int]]:
    """Normalise ``toolchain_node_ids_per_template`` to ``{int: [int, ...]}``.

    The streaming planner emits this as ``dict[int, list[int]]``; a
    JSON roundtrip turns the outer keys into strings. We accept either
    shape and silently drop entries that won't coerce so a malformed
    snapshot never crashes the plan.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[int, list[int]] = {}
    for key, inner in raw.items():
        try:
            tid = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(inner, (list, tuple)):
            continue
        node_ids: list[int] = []
        for nid in inner:
            try:
                node_ids.append(int(nid))
            except (TypeError, ValueError):
                continue
        out[tid] = node_ids
    return out


def _toolchain_idents_by_name(raw: Any) -> dict[str, list[tuple[str, str]]]:
    """Index ``out.toolchain_drvs`` by drv ``name`` for fast role-lookup.

    The cowalk short-circuits toolchain subtrees so ``arr.hashes`` rows
    at toolchain node_ids are empty (E6). Instead, we resolve each
    toolchain TemplateNode's role to one or more ``(hash, name)`` idents
    by matching on the post-hash drv name carried in
    ``out.toolchain_drvs``. Multiple compiler versions can share a
    unified wrapper role (``wrapped-compiler-suit.drv``) so the map
    value is a LIST: every matching ident's task_id gets wired into
    each variant's ``depends_on``. Over-wiring is harmless (the
    variant waits on extra ``build_compilers__*`` tasks that would
    have been built anyway); under-wiring would break the build by
    starting a variant before its compiler is ready.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if not raw:
        return out
    for entry in raw:
        ident = _coerce_ident(entry)
        if ident is None:
            continue
        out.setdefault(ident[1], []).append(ident)
    return out


# ---------------------------------------------------------------------------
# Cycle-detection walk over the template graph
# ---------------------------------------------------------------------------


def _check_no_cycles(templates: Sequence[Any]) -> None:
    """Walk every template's ``nodes`` array via ``child_ids`` and raise
    :class:`DependencyGraphCycleError` if a back-edge to a node still
    on the current DFS stack is observed.

    Templates are conceptually DAGs in nix, but the streaming planner
    may register multiple template instances and a malformed input
    could in principle smuggle a cycle in. Cost is linear in
    ``sum(len(nodes))`` so the guard is cheap even at production
    matrix sizes.
    """
    for tmpl_id, template in enumerate(templates):
        nodes = _template_nodes(template)
        n = len(nodes)
        if n == 0:
            continue
        WHITE, GREY, BLACK = 0, 1, 2
        color = [WHITE] * n
        # Iterative DFS so deep templates don't blow the recursion
        # limit; the production matrix can have thousands of nodes
        # per template under stage-2 stdenv expansion.
        for start in range(n):
            if color[start] != WHITE:
                continue
            stack: list[tuple[int, list[int]]] = [
                (start, list(_node_field(nodes[start], "child_ids", []) or []))
            ]
            color[start] = GREY
            while stack:
                node_id, pending = stack[-1]
                if not pending:
                    color[node_id] = BLACK
                    stack.pop()
                    continue
                child = pending.pop()
                if not isinstance(child, int):
                    # Skip non-int child entries silently — the
                    # streaming planner never emits these but a
                    # malformed JSON roundtrip could.
                    continue
                if child < 0 or child >= n:
                    continue
                if color[child] == GREY:
                    raise DependencyGraphCycleError(
                        f"cycle detected in template #{tmpl_id} at "
                        f"node {child} (re-entered while still on the "
                        f"DFS stack starting at node {start})"
                    )
                if color[child] == BLACK:
                    continue
                color[child] = GREY
                stack.append((
                    child,
                    list(_node_field(nodes[child], "child_ids", []) or []),
                ))


# ---------------------------------------------------------------------------
# common-dep / variant descriptor minting
# ---------------------------------------------------------------------------


def _common_dep_task_id(ident_str: str) -> str:
    """Stable cross-binary task id for a shared dep.

    The task id is keyed solely on the ident (``"<hash>-<name>"``) so
    that two binaries whose templates touch the same shared sub-drv
    collapse onto ONE ``build_common_dep`` descriptor. Cross-binary
    descriptor dedup at emission time (see
    :func:`plan_phase4_from_graph`) folds the duplicates and unions
    their variants' ``depends_on`` entries.

    The ident already encodes (hash, name); the hash is a content-
    addressed nix-store prefix, so identical idents are guaranteed to
    refer to the same sub-derivation regardless of which binary
    template observed it. Architecture is implicit in the hash (a
    different arch produces a different nix-store hash for the same
    role) — explicitly prefixing arch would only hurt the cross-binary
    collapse the streaming planner just enabled.
    """
    return f"build_common_dep__{ident_str}"


def _arch_indep_task_id(binary: str, ident_str: str) -> str:
    """Per-binary task id for an ``arch_indep_deps`` ident.

    Arch-indep deps live one-level under the matrix wrapper and are
    shared by every variant of a binary (across opt-levels) but differ
    across binaries — so the task_id retains a ``binary`` prefix. The
    arch axis is intentionally elided (the dep is arch-independent by
    construction).
    """
    return f"build_common_dep__arch_indep__{binary}__{ident_str}"


def _variant_task_id(binary: str, sys_name: str, label: str) -> str:
    return f"build_variant__{sys_name}__{binary}__{label}"


def _load_source_terminal_predicate():
    """Lazy-load ``template_graph.parser.role._is_source_terminal_role``.

    Returns ``(predicate, role_extractor)`` callables. template_graph is
    the source of truth for arch-indep terminal patterns; this lazy
    import keeps adapter-only callers light. The import is required —
    if template_graph is unreachable the planner cannot correctly
    classify arch-indep deps, so we let ImportError propagate rather
    than silently emit tasks for every source tarball.
    """
    from template_graph.parser.role import (  # noqa: PLC0415
        _is_source_terminal_role,
        drv_role,
    )
    return _is_source_terminal_role, drv_role


def _common_dep_descriptor(
    *,
    binary: str,
    arch: str,
    sys_name: str,
    node_id: int,
    node_name: str,
    ident_str: str,
) -> Phase4Descriptor:
    """Build a ``build_common_dep`` descriptor for one shared-dep node."""
    task_id = _common_dep_task_id(ident_str)
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=task_id,
        name=f"build_common_dep__{binary}__{arch}__{node_name}",
        payload={
            "sys": sys_name,
            "binary": binary,
            "arch": arch,
            "node_name": node_name,
            "node_id": node_id,
            "ident": ident_str,
            # ``attr`` is the worker-facing input; for common-dep
            # builds we point at the ident-derived drv path (the build
            # worker reconstructs the full ``/nix/store/<ident>.drv``
            # prefix). Keeping the bare ident is forward-compatible
            # with both classic ``nix build <drv>`` and the
            # archive-import path used after matrix_eval.
            "attr": ident_str,
        },
        depends_on=(),
    )


def _arch_indep_descriptor(
    *,
    binary: str,
    sys_name: str,
    ident_str: str,
    node_name: str,
) -> Phase4Descriptor:
    """Build a ``build_common_dep`` descriptor for one arch-indep dep.

    Arch-indep deps come from ``OutputState.arch_indep_deps`` (depth-2
    matrix children that aren't variant entry-points). They are
    shared across every variant of THIS binary regardless of arch /
    compiler / opt-level. The descriptor's ``arch`` payload field is
    fixed to ``"arch_indep"`` so the worker can branch its build
    invocation; the task_id encodes the same axis for spawn-log
    grepping.
    """
    task_id = _arch_indep_task_id(binary, ident_str)
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=task_id,
        name=f"build_common_dep__arch_indep__{binary}__{node_name}",
        payload={
            "sys": sys_name,
            "binary": binary,
            "arch": "arch_indep",
            "node_name": node_name,
            "ident": ident_str,
            "attr": ident_str,
        },
        depends_on=(),
    )


def _variant_descriptor(
    *,
    binary: str,
    arch: str,
    sys_name: str,
    label: str,
    variant_spec: Mapping[str, Any],
    depends_on: Sequence[str],
) -> Phase4Descriptor:
    """Build a ``build_variant`` descriptor for one matrix variant.

    Payload mirrors the legacy ``make_variant_header`` shape so the
    integration site can hand-roll a ``ManifestHeader`` with no
    field-by-field reconstruction. We do NOT call ``manifest_gen`` at
    all — see module docstring for the decoupling rationale.
    """
    payload = {
        "sys": sys_name,
        "pkg": variant_spec.get("pkg", binary),
        "arch": arch,
        "label": label,
        "drv": variant_spec.get("drv", ""),
        "variant_dir": variant_spec.get("variant_dir", ""),
        "metadata_name": variant_spec.get("metadata_name", ""),
        "compiler_id": variant_spec.get("compiler_id", ""),
        "compiler_family": variant_spec.get("compiler_family", ""),
        "compiler_version": variant_spec.get("compiler_version", ""),
        "optimization": variant_spec.get("optimization", ""),
        "flag_set": variant_spec.get("flag_set", ""),
        "hardening": variant_spec.get("hardening", ""),
        "sanitizer": variant_spec.get("sanitizer", ""),
        "march": variant_spec.get("march", ""),
        "tier": variant_spec.get("tier", 0),
    }
    task_id = _variant_task_id(binary, sys_name, label)
    # Deterministic ordering of deps so observers comparing manifests
    # across runs see stable diffs.
    return Phase4Descriptor(
        kind="build_variant",
        task_id=task_id,
        name=f"build_variant__{binary}__{label}",
        payload=payload,
        depends_on=tuple(sorted(set(depends_on))),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def plan_phase4_for_binary(
    binary: str,
    streaming_result: Mapping[str, Any],
    variant_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    sys_name: str = "x86_64-linux",
    toolchain_task_ids: Mapping[str, str] = (),  # type: ignore[assignment]
) -> list[Phase4Descriptor]:
    """Translate one binary's streaming planner output into phase-4
    descriptors.

    Returns descriptors in a stable order: every
    ``build_common_dep`` first (sorted by ``(arch, node_name, ident)``),
    then every ``build_variant`` (sorted by ``(arch, label)``). This
    mirrors the legacy phase-1 planner's emit ordering so the framework
    matcher and any human reading the spawn log get a deterministic
    view.

    Raises :class:`DependencyGraphCycleError` if the streaming result's
    templates contain a cycle (defensive guard — see module docstring).

    ``toolchain_task_ids`` defaults to an empty mapping; explicit empty
    is allowed via ``{}``.
    """
    if not isinstance(toolchain_task_ids, Mapping):
        toolchain_task_ids = dict(toolchain_task_ids)  # type: ignore[arg-type]

    templates = list(streaming_result.get("templates", []) or [])
    variant_arrays = streaming_result.get("variant_arrays", {}) or {}
    classifications = streaming_result.get("common_deps_per_arch_template", {})
    toolchain_node_ids = _coerce_toolchain_node_ids(
        streaming_result.get("toolchain_node_ids_per_template", {})
    )
    toolchain_idents_by_name = _toolchain_idents_by_name(
        streaming_result.get("toolchain_drvs", set())
    )
    arch_indep_deps_raw = streaming_result.get("arch_indep_deps", {}) or {}

    _check_no_cycles(templates)

    classification_map: dict[tuple[int, str], dict[int, str]] = dict(
        _iter_classifications(classifications)
    )

    common_dep_descriptors: list[Phase4Descriptor] = []
    variant_descriptors: list[Phase4Descriptor] = []

    # ── Arch-indep deps: emit one build_common_dep per non-source-terminal
    # ident under THIS binary's bucket. Source-terminal idents (source
    # tarballs, fetchurl, builder scripts, patches, setup-hooks) are
    # arch-independent by role and resolve via the nix substituter at
    # build time — no task needed, the build worker fetches on demand.
    # Every variant of this binary depends on every arch-indep task_id
    # we emit (the dep gates the variant's own build worker so the
    # arch-indep artefact is materialised before any variant builds).
    arch_indep_descriptors: list[Phase4Descriptor] = []
    arch_indep_dep_task_ids: list[str] = []
    binary_indep_idents = _arch_indep_idents_for_binary(
        arch_indep_deps_raw, binary,
    )
    if binary_indep_idents:
        is_source_terminal, role_of = _load_source_terminal_predicate()
        for ident in sorted(binary_indep_idents):
            ident_str = _ident_to_str(ident)
            node_name = ident[1]
            if is_source_terminal(role_of(node_name)):
                # Cache substitutes; no task minted.
                continue
            descriptor = _arch_indep_descriptor(
                binary=binary,
                sys_name=sys_name,
                ident_str=ident_str,
                node_name=node_name,
            )
            arch_indep_descriptors.append(descriptor)
            arch_indep_dep_task_ids.append(descriptor.task_id)

    # Visit each (template_id, arch) cell, mint common-dep descriptors
    # for nodes classified as common_dep, then mint a variant
    # descriptor per label. depends_on for a variant accumulates the
    # ids of every common-dep cell that fires in this template +
    # every toolchain task_id resolved from
    # ``toolchain_node_ids_per_template`` (the cowalk short-circuits
    # toolchain subtrees so ``arr.hashes`` rows are empty for them;
    # we map node_id → role-name → ``toolchain_drvs`` idents →
    # caller-supplied ``toolchain_task_ids``).
    for (tmpl_id, arch), arr in _iter_variant_arrays(variant_arrays):
        if tmpl_id < 0 or tmpl_id >= len(templates):
            continue
        template = templates[tmpl_id]
        nodes = _template_nodes(template)
        _tid, _arch, variants, hashes = _variant_array_fields(arr)
        classes = classification_map.get((tmpl_id, arch), {})

        # Seed each variant's dep set with every arch-indep task we
        # minted for this binary. Arch-indep tasks gate every variant
        # of every arch — they materialise the per-binary fetched
        # source / patch artefacts the variant builds consume.
        per_variant_dep_ids: list[set[str]] = [
            set(arch_indep_dep_task_ids) for _ in variants
        ]

        # Resolve toolchain task_ids for this template once: every
        # variant in this cell gets the same set of toolchain deps
        # (we can't distinguish per-variant compilers within a
        # role-collapsed template node, so wire every ident matching
        # the role — see :func:`_toolchain_idents_by_name`).
        toolchain_task_id_set: set[str] = set()
        for tc_node_id in toolchain_node_ids.get(tmpl_id, []):
            if tc_node_id < 0 or tc_node_id >= len(nodes):
                continue
            node_name = str(_node_field(
                nodes[tc_node_id], "name", f"node_{tc_node_id}",
            ))
            for ident in toolchain_idents_by_name.get(node_name, ()):
                task_id = toolchain_task_ids.get(_ident_to_str(ident))
                if task_id:
                    toolchain_task_id_set.add(task_id)
        if toolchain_task_id_set:
            for deps in per_variant_dep_ids:
                deps.update(toolchain_task_id_set)

        tc_node_id_set = set(toolchain_node_ids.get(tmpl_id, []))
        for node_id, node in enumerate(nodes):
            if node_id in tc_node_id_set:
                # Toolchain node — wired via toolchain_node_ids above;
                # skip the common-dep / hashes path entirely.
                continue
            cls = classes.get(node_id)
            row = hashes[node_id] if node_id < len(hashes) else []
            if cls != "common_dep":
                # variant_specific (or unclassified) nodes are
                # subsumed into their variant's own build — no
                # dedicated common_dep task is minted.
                continue
            # All variants share the same hash at a common_dep node;
            # take the first non-None as the representative.
            representative: Optional[tuple[str, str]] = None
            for cell in row:
                ident = _coerce_ident(cell)
                if ident is not None:
                    representative = ident
                    break
            if representative is None:
                # Classified as common_dep but no hashes — skip; the
                # streaming planner's invariant checker should have
                # already raised if this is a real shape error.
                continue
            ident_str = _ident_to_str(representative)
            node_name = str(_node_field(node, "name", f"node_{node_id}"))
            descriptor = _common_dep_descriptor(
                binary=binary,
                arch=arch,
                sys_name=sys_name,
                node_id=node_id,
                node_name=node_name,
                ident_str=ident_str,
            )
            common_dep_descriptors.append(descriptor)
            for variant_idx in range(len(variants)):
                per_variant_dep_ids[variant_idx].add(descriptor.task_id)

        # Mint a variant descriptor per label. Variants without a
        # lookup entry are skipped — the caller's matrix may have
        # filtered them out between graph generation and planning.
        for variant_idx, label in enumerate(variants):
            spec = variant_lookup.get((arch, label))
            if spec is None:
                continue
            descriptor = _variant_descriptor(
                binary=binary,
                arch=arch,
                sys_name=sys_name,
                label=label,
                variant_spec=spec,
                depends_on=per_variant_dep_ids[variant_idx],
            )
            variant_descriptors.append(descriptor)

    common_dep_descriptors.sort(key=lambda d: (d.payload["arch"], d.payload["node_name"], d.payload["ident"]))
    arch_indep_descriptors.sort(key=lambda d: (d.payload["node_name"], d.payload["ident"]))
    variant_descriptors.sort(key=lambda d: (d.payload["arch"], d.payload["label"]))
    # Order: arch-indep deps first (they gate every variant), then per-
    # cell common_deps (which mostly gate intra-arch siblings), then
    # variants. Mirrors the spawn-order the framework prefers.
    return arch_indep_descriptors + common_dep_descriptors + variant_descriptors


def plan_phase4_from_graph(
    inputs: Iterable[BinaryPlanInput],
    *,
    sys_name: str = "x86_64-linux",
) -> list[Phase4Descriptor]:
    """Translate a sequence of per-binary streaming results into a
    single ordered phase-4 descriptor list.

    Each :class:`BinaryPlanInput` is processed independently via
    :func:`plan_phase4_for_binary`; the results are concatenated in
    binary-name order so the framework's spawn log is stable across
    runs. Cycle detection runs per-binary (the first cycle raises,
    later binaries are not visited).

    Cross-binary ``build_common_dep`` dedup: ``_common_dep_task_id``
    is keyed only on the shared-dep ident, so two binaries whose
    templates touch the same sub-drv produce the same task_id. We
    keep the FIRST descriptor seen for each task_id and drop later
    duplicates. ``build_variant`` descriptors from later binaries
    still reference the deduped task_id in their ``depends_on`` —
    that wiring is automatic since the variant builder reads the
    task_id off ``_common_dep_task_id`` directly. Stable ordering is
    enforced by sorting common-deps by ``task_id``.
    """
    common_dep_by_task_id: dict[str, Phase4Descriptor] = {}
    variant_descriptors: list[Phase4Descriptor] = []

    for inp in sorted(inputs, key=lambda i: i.binary):
        per_binary = plan_phase4_for_binary(
            inp.binary,
            inp.streaming_result,
            inp.variant_lookup,
            sys_name=sys_name,
            toolchain_task_ids=inp.toolchain_task_ids,
        )
        for d in per_binary:
            if d.kind == "build_common_dep":
                # First binary to emit this task_id wins; subsequent
                # binaries' duplicates are dropped because the task_id
                # already encodes the (content-addressed) ident.
                common_dep_by_task_id.setdefault(d.task_id, d)
            else:
                variant_descriptors.append(d)

    common_dep_descriptors = sorted(
        common_dep_by_task_id.values(), key=lambda d: d.task_id,
    )
    return common_dep_descriptors + variant_descriptors


# ---------------------------------------------------------------------------
# Convenience for tests + callers: derive a label key from a drv path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Descriptor → ManifestHeader glue (primary-side spawn path)
# ---------------------------------------------------------------------------


def load_descriptors_from_json(payload: Any) -> list[Phase4Descriptor]:
    """Re-tuple JSON-roundtripped Phase 4 descriptors.

    ``dependency_graph_worker.write_dependency_graph_json`` writes a
    dict ``{"phase4_descriptors": [<asdict-form>, ...]}``; this helper
    is the inverse for callers that read the file off disk and want a
    typed descriptor list back. Accepts either the wrapping dict or a
    bare descriptor list so test fixtures can pass either shape.

    Malformed entries (non-dict, missing ``kind`` / ``task_id``) are
    skipped with no error — callers are expected to validate against
    ``primary_handle.spawn_tasks`` results, which surface the
    semantic-level issues (duplicate hashes, unknown deps).
    """
    if isinstance(payload, Mapping):
        raw_list = payload.get("phase4_descriptors", [])
    else:
        raw_list = payload
    if not isinstance(raw_list, (list, tuple)):
        return []
    out: list[Phase4Descriptor] = []
    for entry in raw_list:
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        task_id = entry.get("task_id")
        name = entry.get("name")
        payload_dict = entry.get("payload")
        if not (
            isinstance(kind, str) and isinstance(task_id, str)
            and isinstance(name, str) and isinstance(payload_dict, Mapping)
        ):
            continue
        raw_deps = entry.get("depends_on") or ()
        if isinstance(raw_deps, (list, tuple)):
            deps_tuple = tuple(d for d in raw_deps if isinstance(d, str))
        else:
            deps_tuple = ()
        out.append(Phase4Descriptor(
            kind=kind,
            task_id=task_id,
            name=name,
            payload=dict(payload_dict),
            depends_on=deps_tuple,
        ))
    return out


def headers_from_descriptors(descriptors: Iterable[Phase4Descriptor]) -> list:
    """Translate :class:`Phase4Descriptor` records into
    :class:`manifest_gen.ManifestHeader` instances ready for
    ``primary_handle.spawn_tasks``.

    Late-imports ``manifest_gen`` so this module stays loadable in
    environments where ``manifest_gen`` is mid-rename or absent (unit
    tests for the planner that don't need the constructors).

    Per-descriptor mapping:

    * ``build_common_dep`` → :class:`ManifestHeader` with the planner's
      payload threaded straight through. The ``attr`` field is
      synthesised from the ident when the planner didn't carry one.
    * ``build_variant`` → :class:`ManifestHeader` with the variant
      payload threaded through; ``attr`` is reconstructed from
      ``dataset.<sys>.<pkg>.<arch>.<label>`` if missing so the
      downstream build worker can resolve it the same way the legacy
      submit-time path does.

    Unknown ``kind`` values are skipped with no error so a future
    descriptor type added upstream doesn't crash older primaries; the
    spawn-side log surfaces the gap as a "no headers spawned" line.
    """
    from compiler_suit_runner.manifest_gen import ManifestHeader  # noqa: PLC0415

    headers: list = []
    for d in descriptors:
        if d.kind == "build_common_dep":
            payload = dict(d.payload)
            payload.setdefault(
                "attr", payload.get("drv") or payload.get("ident", ""),
            )
            headers.append(ManifestHeader(
                item_class="build_common_dep",
                name=d.name,
                size=0,
                payload=payload,
                task_id=d.task_id,
                task_depends_on=tuple(d.depends_on),
            ))
            continue
        if d.kind == "build_variant":
            payload = dict(d.payload)
            sys_name = payload.get("sys", "")
            pkg = payload.get("pkg", "")
            arch = payload.get("arch", "")
            label = payload.get("label", "")
            if "attr" not in payload and sys_name and pkg and arch and label:
                payload["attr"] = f"dataset.{sys_name}.{pkg}.{arch}.{label}"
            headers.append(ManifestHeader(
                item_class="build_variant",
                name=d.name,
                size=0,
                payload=payload,
                task_id=d.task_id,
                task_depends_on=tuple(d.depends_on),
            ))
            continue
        # Unknown kind — skip silently; caller logs the gap.
    return headers


def variant_label_key(drv_path_or_name: str) -> str:
    """Strip the ``/nix/store/<hash>-`` prefix and ``.drv`` suffix.

    Useful for callers that have raw drv paths and want to build the
    ``(arch, label)`` lookup key without re-parsing the variant
    filename themselves. Mirrors the post-hash naming the streaming
    planner uses for ``VariantArray.variants``.
    """
    name = pathlib.Path(drv_path_or_name).name
    if name.endswith(".drv"):
        name = name[:-4]
    if "-" in name and len(name.split("-", 1)[0]) == 32:
        # Looks like a nix store basename; strip the leading hash.
        name = name.split("-", 1)[1]
    return name

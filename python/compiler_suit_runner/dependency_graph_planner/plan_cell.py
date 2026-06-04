"""Per-cell helpers for :func:`plan_total.plan_phase4_for_binary`.

A "cell" is one ``(template_id, arch)`` pair from the streaming
planner's output. The walk over cells lives in :mod:`.plan_total`;
this module just owns the building blocks each cell traversal calls.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .descriptors import (
    Phase4Descriptor,
    _arch_indep_descriptor,
    _common_dep_descriptor,
    _variant_descriptor,
)
from .shapes import (
    _arch_indep_idents_for_binary,
    _coerce_ident,
    _ident_to_str,
    _node_field,
    _template_nodes,
    _variant_array_fields,
)

# Match ``/nix/store/<hash>-`` so the remaining basename can be fed into
# ``parse_variant_path``. The hash segment is alphanumeric only, so a
# non-greedy match to the first ``-`` works for real nix paths (32-char
# base32 hash) and shorter test fixtures alike.
_STORE_HASH_PREFIX_RE = re.compile(r"^/nix/store/[^-]+-")


def _variant_toolchain_task_id(
    sys_name: str,
    arch: str,
    comp: str,
    known_task_ids: frozenset[str],
) -> Optional[str]:
    """Compose the bare ``<sys>__<arch>__<comp>`` build_compilers
    task_id for one variant's ``(arch, comp)`` and return it iff phase-1
    emitted it. Mirrors :func:`manifest_gen.build_compilers_task_id`
    (phase-local; the BUILD_COMPILERS phase is carried by the downstream
    cross-phase ``TaskDep``). ``None`` skips wiring for operator-provided
    toolchains (no task to depend on).
    """
    task_id = f"{sys_name}__{arch}__{comp}"
    return task_id if task_id in known_task_ids else None


def _variant_toolchain_dep(
    spec: Mapping[str, Any],
    sys_name: str,
    known_task_ids: frozenset[str],
) -> Optional[str]:
    """Per-variant toolchain dep: parse the variant drv path's
    ``(arch, comp)`` and compose its phase-1 task_id. Returns ``None``
    when the drv shape is unrecognised or the composed id isn't in
    ``known_task_ids`` -- best-effort enrichment, not a hard failure.
    """
    from template_graph.tree_walker import (  # noqa: PLC0415
        DEFAULT_ARCHS,
        TreeWalkError,
        parse_variant_path,
    )
    drv_path = spec.get("drv", "")
    if not isinstance(drv_path, str) or not drv_path:
        return None
    basename = _STORE_HASH_PREFIX_RE.sub("", drv_path)
    if basename == drv_path:
        return None  # no store prefix matched
    try:
        _binary, arch_v, comp, _opt = parse_variant_path(
            basename, archs=DEFAULT_ARCHS,
        )
    except TreeWalkError:
        return None
    return _variant_toolchain_task_id(
        sys_name, arch_v, comp, known_task_ids,
    )


def _load_source_terminal_predicate():
    """Lazy-load ``template_graph.parser.role._is_source_terminal_role``.

    Returns ``(predicate, role_extractor)`` callables. template_graph is
    the source of truth for arch-indep terminal patterns; this lazy
    import keeps adapter-only callers light. The import is required --
    if template_graph is unreachable the planner cannot correctly
    classify arch-indep deps, so we let ImportError propagate rather
    than silently emit tasks for every source tarball.
    """
    from template_graph.parser.role import (  # noqa: PLC0415
        _is_source_terminal_role,
        drv_role,
    )
    return _is_source_terminal_role, drv_role


def _resolve_arch_indep_descriptors(
    *,
    binary: str,
    sys_name: str,
    arch_indep_deps_raw: Any,
) -> tuple[list[Phase4Descriptor], list[str]]:
    """Emit one ``build_common_dep`` per non-source-terminal ident in
    THIS binary's ``arch_indep_deps`` bucket.

    Source-terminal idents (source tarballs, fetchurl, builder scripts,
    patches, setup-hooks) are arch-independent by role and resolve via
    the nix substituter at build time -- no task needed, the build
    worker fetches on demand. The returned task_ids gate every variant
    of this binary (the arch-indep artefact must be materialised before
    any variant builds).
    """
    binary_indep_idents = _arch_indep_idents_for_binary(
        arch_indep_deps_raw, binary,
    )
    if not binary_indep_idents:
        return [], []
    is_source_terminal, role_of = _load_source_terminal_predicate()
    descriptors: list[Phase4Descriptor] = []
    task_ids: list[str] = []
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
        descriptors.append(descriptor)
        task_ids.append(descriptor.task_id)
    return descriptors, task_ids


def _mint_common_dep_descriptors(
    *,
    binary: str,
    arch: str,
    sys_name: str,
    nodes: Sequence[Any],
    classes: Mapping[int, str],
    hashes: Sequence[Sequence[Any]],
    tc_node_id_set: set[int],
    meta_skip_idents: Optional[set[str]] = None,
) -> list[Phase4Descriptor]:
    """Mint one ``build_common_dep`` descriptor per common-dep node in
    this cell. Skips toolchain nodes (wired separately), unclassified
    nodes (subsumed into their variant's own build), and idents listed
    in ``meta_skip_idents`` (a meta-level task already covers them).
    """
    out: list[Phase4Descriptor] = []
    skip = meta_skip_idents or set()
    for node_id, node in enumerate(nodes):
        if node_id in tc_node_id_set:
            # Toolchain node -- wired via toolchain_node_ids elsewhere.
            continue
        cls = classes.get(node_id)
        if cls != "common_dep":
            # variant_specific (or unclassified) nodes are subsumed
            # into their variant's own build -- no dedicated
            # common_dep task is minted.
            continue
        row = hashes[node_id] if node_id < len(hashes) else []
        # All variants share the same hash at a common_dep node;
        # take the first non-None as the representative.
        representative: Optional[tuple[str, str]] = None
        for cell in row:
            ident = _coerce_ident(cell)
            if ident is not None:
                representative = ident
                break
        if representative is None:
            # Classified as common_dep but no hashes -- skip; the
            # streaming planner's invariant checker should have
            # already raised if this is a real shape error.
            continue
        ident_str = _ident_to_str(representative)
        if ident_str in skip:
            # A meta-level (cross_arch / family) task already covers
            # this ident; skip the per-cell duplicate.
            continue
        node_name = str(_node_field(node, "name", f"node_{node_id}"))
        out.append(_common_dep_descriptor(
            binary=binary,
            arch=arch,
            sys_name=sys_name,
            node_id=node_id,
            node_name=node_name,
            ident_str=ident_str,
        ))
    return out


def _plan_cell(
    *,
    binary: str,
    sys_name: str,
    tmpl_id: int,
    arch: str,
    arr: Any,
    templates: Sequence[Any],
    classification_map: Mapping[tuple[int, str], Mapping[int, str]],
    toolchain_node_ids: Mapping[int, Sequence[int]],
    toolchain_task_ids: Mapping[str, str],
    variant_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    arch_indep_dep_task_ids: Sequence[str],
    meta_extra_variant_deps: Optional[
        Mapping[tuple[str, str], set[str]]
    ] = None,
    meta_skip_idents: Optional[set[str]] = None,
    meta_toolchain_extras: Optional[
        Mapping[tuple[str, str], set[str]]
    ] = None,
) -> tuple[list[Phase4Descriptor], list[Phase4Descriptor]]:
    """Plan a single ``(tmpl_id, arch)`` cell, returning ``(common_dep
    descriptors, variant descriptors)`` for it.

    Variants without a lookup entry are skipped -- the caller's matrix
    may have filtered them out between graph generation and planning.

    The ``meta_*`` keyword arguments wire in the MetaTemplate post-pass
    (see :mod:`.plan_meta`): meta-level common_deps are merged into
    each variant's ``depends_on`` and their idents short-circuit the
    per-cell duplicate emission path.
    """
    if tmpl_id < 0 or tmpl_id >= len(templates):
        return [], []
    template = templates[tmpl_id]
    nodes = _template_nodes(template)
    _tid, _arch, variants, hashes = _variant_array_fields(arr)
    classes = classification_map.get((tmpl_id, arch), {})

    # Snapshot the set of phase-1 build_compilers task_ids (bare
    # ``<sys>__<arch>__<comp>``) once so per-variant wiring can
    # compose-then-check without rebuilding the lookup for every variant
    # in this cell.
    known_task_ids: frozenset[str] = frozenset(toolchain_task_ids.values())

    # Seed each variant's dep set with every arch-indep task we minted
    # for this binary. Arch-indep tasks gate every variant of every
    # arch -- they materialise the per-binary fetched source / patch
    # artefacts the variant builds consume.
    per_variant_dep_ids: list[set[str]] = [
        set(arch_indep_dep_task_ids) for _ in variants
    ]

    tc_node_id_set = set(toolchain_node_ids.get(tmpl_id, []))
    common_dep_descriptors = _mint_common_dep_descriptors(
        binary=binary,
        arch=arch,
        sys_name=sys_name,
        nodes=nodes,
        classes=classes,
        hashes=hashes,
        tc_node_id_set=tc_node_id_set,
        meta_skip_idents=meta_skip_idents,
    )
    # Every variant in this cell depends on every common-dep we just
    # minted (deduplicated by node_id -- one descriptor per common_dep
    # node).
    for descriptor in common_dep_descriptors:
        for deps in per_variant_dep_ids:
            deps.add(descriptor.task_id)

    # Mint a variant descriptor per label. Intra-phase deps go to
    # ``depends_on``; cross-phase toolchain deps accumulate SEPARATELY
    # into ``tc_deps`` (read ``(arch, comp)`` off the variant drv
    # basename, which the role-collapsed template node can't distinguish).
    variant_descriptors: list[Phase4Descriptor] = []
    for variant_idx, label in enumerate(variants):
        spec = variant_lookup.get((arch, label))
        if spec is None:
            continue
        deps = per_variant_dep_ids[variant_idx]
        if meta_extra_variant_deps:
            deps = deps | meta_extra_variant_deps.get((arch, label), set())
        tc_deps = set((meta_toolchain_extras or {}).get((arch, label), ()))
        tc_task_id = _variant_toolchain_dep(spec, sys_name, known_task_ids)
        if tc_task_id is not None:
            tc_deps.add(tc_task_id)
        variant_descriptors.append(_variant_descriptor(
            binary=binary,
            arch=arch,
            sys_name=sys_name,
            label=label,
            variant_spec=spec,
            depends_on=deps,
            build_compilers_depends_on=tc_deps,
        ))
    return common_dep_descriptors, variant_descriptors

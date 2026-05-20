"""Shared role-merging helpers for per-binary cross-arch projection.

The streaming planner produces one ``Template`` per ``(template_id,
arch)`` cell. In production the variant root role names embed the
``<binary>-<arch>-<comp>-<opt>-…-elf-folder`` axes, so ``drv_role``
does NOT strip the arch prefix — every ``(arch, binary)`` ends up with
its own template_id. Cross-arch sharing therefore can't be expressed
"per template_id"; it has to be expressed "per ``(role, enforce)`` key,
across all archs of one binary".

This module owns the canonical role-merge primitives both
``template_graph.cowalk.cross_arch.build_meta_templates`` and
``template_graph.dot.merge_binary.merge_binary_to_dot`` consume:

* :func:`collect_per_arch` — pick the (template, arch) cells whose
  root role names ``binary``. Accepts either an ``OutputState`` or the
  legacy ``StreamPlanner.finalize()`` result dict, so the merge_binary
  renderer doesn't have to change its public signature.
* :func:`build_merged_keymap` — fold each per-arch template into one
  ``(role, enforce)``-keyed map. The variant-root role is rewritten to
  ``{binary}-elf-folder.drv`` so the per-arch roots collapse onto one
  Key (their literal ``.name`` differs only in the embedded arch).
* :func:`canonical_walk_order` — DFS the merged keymap from the
  canonical root key so MetaTemplate positions are deterministic.

The class-priority rule (``variant_specific > common_dep > '?' >
toolchain``) and the variant-0 drv recording rule mirror what
``dot.merge_binary._build_merged_keymap`` used to do inline.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Union

from template_graph.graph import Template, VariantArray


# Key: (role, enforce). Plain nodes have enforce=None; template
# splits share a role but differ in enforce, kept as separate keys.
Key = tuple[str, Optional[tuple[str, Optional[str]]]]
# Per-key dict: arch -> (drv_path_v0_or_None, child_keys). The ident
# carries a ``bytes`` hash (planner-native after the str→bytes migration).
ArchCell = tuple[Optional[tuple[bytes, str]], list[Key]]
Merged = dict[Key, dict[str, ArchCell]]
# arch -> (template, common-dep classifications by node-id, variant array).
ByArch = dict[str, tuple[Template, dict[int, str], VariantArray]]


# Priority order for class merging across archs. Higher wins.
_CLASS_PRIORITY: dict[str, int] = {
    "variant_specific": 3,
    "common_dep": 2,
    "?": 1,
    "toolchain": 0,
}


def collect_per_arch(
    source: Union[Mapping[str, Any], "object"],
    binary: str,
) -> ByArch:
    """Pick every ``(template, arch)`` cell whose root names ``binary``.

    ``source`` may be either an ``OutputState`` instance (the shape
    used by :func:`build_meta_templates`) or a ``StreamPlanner.finalize()``
    result dict (the shape used by :func:`merge_binary_to_dot`). Both
    expose ``variant_arrays``, ``templates``, and a per-cell
    classifications map under different attribute / key names.

    Matches only on the ROOT node: a template "belongs to" ``binary``
    iff its entry-point role starts with ``{binary}-`` and contains
    ``-elf-folder``. Templates that merely *depend* on ``binary`` are
    excluded.

    Returns an empty dict when no matching cells exist (the caller
    decides whether that's an error or a no-op).
    """
    if isinstance(source, Mapping):
        variant_arrays = source["variant_arrays"]
        templates = source["templates"]
        classifications = source["common_deps_per_arch_template"]
    else:
        variant_arrays = source.variant_arrays
        templates = source.templates
        classifications = source.classifications
    by_arch: ByArch = {}
    for (tid, arch), arr in variant_arrays.items():
        tmpl = templates[tid]
        root = tmpl.nodes[tmpl.root_id]
        if not (
            root.name.startswith(f"{binary}-")
            and "-elf-folder" in root.name
        ):
            continue
        classes = classifications.get((tid, arch), {})
        by_arch[arch] = (tmpl, classes, arr)
    return by_arch


def build_merged_keymap(
    by_arch: ByArch, canonical_root_role: str,
) -> tuple[Merged, dict[Key, str], dict[Key, bool]]:
    """Fold each per-arch template into the ``(role, enforce)``-keyed map.

    Per arch we record the variant-0 drv-path (only for common-dep
    nodes — variant-specific drvs are deliberately not surfaced) and
    the child role keys. The class priority ``variant_specific >
    common_dep > '?' > toolchain`` means any arch seeing the role as
    variant-specific dominates the merged view: a role one arch sees
    as variant-specific cannot be promoted to a shared meta-template
    common_dep, even if other archs see it as common_dep.

    The root node of each per-arch template is keyed at
    ``(canonical_root_role, None)`` so per-arch roots collapse onto a
    single Key despite each arch's literal root name embedding the
    arch axis.
    """
    merged: Merged = {}
    key_class: dict[Key, str] = {}
    key_optional: dict[Key, bool] = {}
    for arch, (tmpl, classes, arr) in by_arch.items():
        for nid, node in enumerate(tmpl.nodes):
            if nid == tmpl.root_id:
                role = canonical_root_role
                enforce = None
            else:
                role = node.name
                enforce = node.enforce
            k: Key = (role, enforce)
            cls = (
                "toolchain" if node.is_toolchain
                else classes.get(nid, "?")
            )
            existing = key_class.get(k)
            if existing is None or (
                _CLASS_PRIORITY.get(cls, 0)
                > _CLASS_PRIORITY.get(existing, 0)
            ):
                key_class[k] = cls
            key_optional[k] = key_optional.get(k, False) or node.optional
            drv = None
            if cls == "common_dep" and arr.hashes[nid]:
                drv = arr.hashes[nid][0]
            child_keys: list[Key] = [
                (tmpl.nodes[c].name, tmpl.nodes[c].enforce)
                for c in node.child_ids
            ]
            merged.setdefault(k, {})[arch] = (drv, child_keys)
    return merged, key_class, key_optional


def canonical_walk_order(
    merged: Merged, canonical_root_key: Key,
) -> tuple[Key, ...]:
    """DFS the merged keymap from the canonical root, returning each
    Key in first-visit pre-order.

    Children at each Key are the deduplicated union of child-key lists
    across archs, ordered by first appearance (which is itself stable
    because the per-arch order matches the in-template child-id order
    plus the deterministic arch iteration order of ``by_arch``). Keys
    unreachable from the root (would only happen for malformed input)
    are appended at the end in their insertion order so nothing is
    silently dropped.
    """
    order: list[Key] = []
    visited: set[Key] = set()
    stack: list[Key] = [canonical_root_key]
    while stack:
        k = stack.pop()
        if k in visited or k not in merged:
            continue
        visited.add(k)
        order.append(k)
        # Stable union of child Keys across archs (first-seen order).
        seen_children: set[Key] = set()
        ordered_children: list[Key] = []
        for _arch, (_drv, children) in merged[k].items():
            for c in children:
                if c not in seen_children:
                    seen_children.add(c)
                    ordered_children.append(c)
        # Reverse so DFS pops left-to-right.
        for c in reversed(ordered_children):
            if c not in visited:
                stack.append(c)
    # Append unreachable keys (defensive — shouldn't happen normally).
    for k in merged:
        if k not in visited:
            order.append(k)
    return tuple(order)


__all__ = [
    "Key",
    "ArchCell",
    "Merged",
    "ByArch",
    "collect_per_arch",
    "build_merged_keymap",
    "canonical_walk_order",
]

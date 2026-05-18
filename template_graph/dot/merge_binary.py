"""Per-binary merged Graphviz DOT renderer.

Overlays every (template, arch) pair belonging to one binary into one
DOT graph. Nodes are keyed by ``(role, enforce)`` so template splits
stay visible. Common-dep nodes are coloured by their A/B/C/D cross-arch
sharing pattern; see :func:`merge_binary_to_dot` for the taxonomy.
The classifier ``_classify_cross_arch_sharing`` is imported lazily
from :mod:`template_graph.streaming` (it moves out in a later phase).
"""

from __future__ import annotations

from typing import Optional

from template_graph.graph import Template, VariantArray

from .template import _enforce_label


# (role-name, enforce). Plain nodes have enforce=None; template
# splits share a role but differ in enforce, kept as separate boxes.
Key = tuple[str, Optional[tuple[str, Optional[str]]]]
# Per-key dict: arch -> (drv_path_v0_or_None, child_keys).
ArchCell = tuple[Optional[tuple[str, str]], list[Key]]
Merged = dict[Key, dict[str, ArchCell]]
# arch -> (template, common-dep classifications by node-id, variant array)
ByArch = dict[str, tuple[Template, dict[int, str], VariantArray]]


def _collect_per_arch(result: dict, binary: str) -> ByArch:
    """Pick every (template, arch) whose root belongs to ``binary``.

    Match only on the ROOT node — a template "belongs to" binary X
    iff its entry-point is X's elf-folder. Otherwise binaries that
    *depend* on X (e.g. vips dep'ing on libxml2) get wrongly counted.
    """
    by_arch: ByArch = {}
    for (tid, arch), arr in result["variant_arrays"].items():
        tmpl = result["templates"][tid]
        root = tmpl.nodes[tmpl.root_id]
        if not (
            root.name.startswith(f"{binary}-")
            and "-elf-folder" in root.name
        ):
            continue
        classes = result["common_deps_per_arch_template"].get(
            (tid, arch), {}
        )
        by_arch[arch] = (tmpl, classes, arr)
    if not by_arch:
        raise ValueError(f"binary {binary!r} not found in result")
    return by_arch


def _build_merged_keymap(
    by_arch: ByArch, canonical_root_role: str,
) -> tuple[Merged, dict[Key, str], dict[Key, bool]]:
    """Fold each per-arch template into the (role, enforce)-keyed map.

    Per arch we record the variant-0 drv-path (only for common-dep
    nodes — variant-specific drvs are deliberately not surfaced) and
    the child role keys. Class priority: variant_specific > common_dep
    > '?' > toolchain — any arch seeing the role as variant-specific
    dominates the merged view.
    """
    merged: Merged = {}
    key_class: dict[Key, str] = {}
    key_optional: dict[Key, bool] = {}
    order = {"variant_specific": 3, "common_dep": 2, "?": 1, "toolchain": 0}
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
            if existing is None or order.get(cls, 0) > order.get(existing, 0):
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


def _resolve_visible(
    merged: Merged,
    key_class: dict[Key, str],
    canonical_root_key: Key,
    collapse_common_deps: bool,
) -> set[Key]:
    """Pick which keys to render.

    With ``collapse_common_deps``, only render keys reachable from
    the root without descending through a common_dep node (the
    common dep itself is shown; its subtree is hidden).
    """
    if not collapse_common_deps:
        return set(merged)
    visible: set[Key] = set()
    stack: list[Key] = [canonical_root_key]
    while stack:
        r = stack.pop()
        if r in visible or r not in merged:
            continue
        visible.add(r)
        if key_class.get(r) == "common_dep":
            continue
        for _arch, (_drv, children) in merged[r].items():
            stack.extend(children)
    return visible


def _missing_arch_tag(
    present: set[str], absent: set[str], all_archs: list[str]
) -> str:
    """Compact human-readable summary of which archs lack this role."""
    if len(present) == 1:
        only = next(iter(present))
        return "native-only" if only == "x86_64" else f"only-{only}"
    if "x86_64" in absent and len(present) == len(all_archs) - 1:
        return "cross-only"
    if len(absent) == 1:
        missing_one = next(iter(absent))
        return (
            "no-native" if missing_one == "x86_64"
            else f"no-{missing_one}"
        )
    if "x86_64" in absent:
        return (
            f"no-x86_64,{','.join(sorted(absent - {'x86_64'}))}"
            if len(absent) <= 3
            else f"no x86_64 (+{len(absent) - 1} cross)"
        )
    if len(absent) <= 3:
        return f"-{','.join(sorted(absent))}"
    return f"in {','.join(sorted(present))}"


def _node_fill_and_sharing(
    cls: str,
    archs_dict: dict[str, ArchCell],
) -> tuple[str, Optional[str]]:
    """Fill colour + optional A/B/C/D sharing tag for one node."""
    # Lazy import — streaming.py re-exports merge_binary_to_dot.
    from template_graph.streaming import _classify_cross_arch_sharing

    if cls == "toolchain":
        return "lightgray", None
    if cls == "variant_specific":
        return "lightcoral", None
    if cls == "common_dep":
        drvs = {a: d for a, d in
                ((a, archs_dict[a][0]) for a in archs_dict)
                if d is not None}
        if not drvs:
            return "white", None
        cat = _classify_cross_arch_sharing(drvs)
        fill = {"A": "orange", "B": "yellow",
                "C": "cyan", "D": "palegreen"}[cat]
        return fill, cat
    return "white", None


def _render_nodes(
    merged: Merged,
    key_class: dict[Key, str],
    key_optional: dict[Key, bool],
    visible: set[Key],
    all_archs: list[str],
    key_to_id: dict[Key, int],
) -> list[str]:
    """Emit one DOT node line per visible key.

    Dashed border when the role is missing from some archs or is
    optional. Label suffix encodes sharing class, optional marker,
    enforce constraint, and arch-coverage tag.
    """
    lines: list[str] = []
    for k, archs_dict in merged.items():
        if k not in visible:
            continue
        role, enforce = k
        fill, sharing = _node_fill_and_sharing(key_class[k], archs_dict)
        missing = len(archs_dict) < len(all_archs)
        is_optional = key_optional.get(k, False)
        style = "filled,dashed" if (missing or is_optional) else "filled"
        suffix = f" [{sharing}]" if sharing else ""
        if is_optional:
            suffix = "*" + suffix
        suffix += _enforce_label(enforce)
        if missing:
            present = set(archs_dict)
            absent = set(all_archs) - present
            tag = _missing_arch_tag(present, absent, all_archs)
            miss_suffix = f"  ({len(present)}/{len(all_archs)} • {tag})"
        else:
            miss_suffix = ""
        safe_name = role.replace('"', '\\"')
        lines.append(
            f'  n{key_to_id[k]} '
            f'[label="{safe_name}{suffix}{miss_suffix}", '
            f'fillcolor={fill}, style="{style}"];'
        )
    return lines


def _render_edges(
    merged: Merged,
    visible: set[Key],
    key_class: dict[Key, str],
    collapse_common_deps: bool,
    key_to_id: dict[Key, int],
) -> list[str]:
    """Emit DOT edges as the union of children across archs."""
    lines: list[str] = []
    for k, archs_dict in merged.items():
        if k not in visible:
            continue
        if collapse_common_deps and key_class.get(k) == "common_dep":
            continue
        edges: set[Key] = set()
        for _arch, (_drv, children) in archs_dict.items():
            for c in children:
                edges.add(c)
        for c in edges:
            if c in key_to_id:
                lines.append(f"  n{key_to_id[k]} -> n{key_to_id[c]};")
    return lines


def merge_binary_to_dot(
    result: dict,
    binary: str,
    *,
    label: Optional[str] = None,
    collapse_common_deps: bool = False,
) -> str:
    """Build a per-binary merged DOT across all (template, arch).

    Nodes are keyed by role (the version-and-triple-stripped name
    already on each template node's ``.name``). For each role we look
    up the drv-path in ``arr.hashes[node][0]`` of every arch's
    calibration variant and classify the cross-arch sharing pattern:

      A — different drv per arch
      B — same drv within family, different across families
          (x86: i686+x86_64; arm: aarch64+armv7l-hf+armv7l-sf;
           mips: mips64el+mipsel; power: ppc32+ppc64; riscv: riscv64)
      C — partial sharing that crosses family boundaries
      D — same drv across every arch that has the role

    Dashed border if some archs of the matrix don't surface the role.
    """
    by_arch = _collect_per_arch(result, binary)
    all_archs = sorted(by_arch)
    canonical_root_role = f"{binary}-elf-folder.drv"
    canonical_root_key: Key = (canonical_root_role, None)
    merged, key_class, key_optional = _build_merged_keymap(
        by_arch, canonical_root_role)
    visible = _resolve_visible(
        merged, key_class, canonical_root_key, collapse_common_deps)
    key_to_id: dict[Key, int] = {
        k: i for i, k in enumerate(k2 for k2 in merged if k2 in visible)}
    safe_label = (label or f"merged_{binary}").replace('"', '\\"')
    lines: list[str] = [
        f'digraph "{safe_label}" {{',
        "  rankdir=LR;",
        "  node [shape=box, style=filled, fontname=monospace];",
    ]
    lines.extend(_render_nodes(
        merged, key_class, key_optional, visible, all_archs, key_to_id))
    lines.extend(_render_edges(
        merged, visible, key_class, collapse_common_deps, key_to_id))
    lines.append("}")
    return "\n".join(lines)


def save_binary_merged_dot(
    result: dict, binary: str, out_path: str
) -> None:
    with open(out_path, "w") as f:
        f.write(merge_binary_to_dot(result, binary))
        f.write("\n")

"""Per-binary merged Graphviz DOT renderer.

Overlays every (template, arch) pair belonging to one binary into one
DOT graph. Nodes are keyed by ``(role, enforce)`` so template splits
stay visible. Common-dep nodes are coloured by their A/B/C/D cross-arch
sharing pattern; see :func:`merge_binary_to_dot` for the taxonomy.

The role-merging primitives (collect-per-arch, build-merged-keymap)
live in :mod:`template_graph.cowalk._role_merge` and are shared with
``build_meta_templates``. The cross-arch sharing letter is read off
the binary's pre-computed ``MetaTemplate`` (placed under
``result["meta_templates"]`` by :func:`template_graph.streaming.finalize`),
so this module never runs the classifier itself.
"""

from __future__ import annotations

from typing import Optional

from template_graph.cowalk._role_merge import (
    ArchCell,
    ByArch,
    Key,
    Merged,
    build_merged_keymap,
    collect_per_arch,
)
from template_graph.graph import MetaTemplate

from .template import _enforce_label


def _build_letter_lookup(
    meta_template: Optional[MetaTemplate],
) -> dict[Key, Optional[str]]:
    """Index a binary's ``MetaTemplate`` by ``(role, enforce)`` Key.

    Returns ``{key -> A/B/C/D-letter-or-None}``. Each entry's letter
    is ``None`` when the MetaTemplate marked the position
    ``variant_specific`` — which is exactly when the renderer should
    NOT emit a sharing label. Empty dict when no MetaTemplate is
    available (caller falls back to "no shared idents → white"
    rendering for any Key it doesn't find).
    """
    if meta_template is None:
        return {}
    out: dict[Key, Optional[str]] = {}
    for role, enforce, letter in zip(
        meta_template.role_at_node,
        meta_template.enforce_at_node,
        meta_template.class_letter_at_node,
    ):
        out[(role, enforce)] = letter
    return out


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
    key: Key,
    letter_by_key: dict[Key, Optional[str]],
) -> tuple[str, Optional[str]]:
    """Fill colour + optional A/B/C/D sharing tag for one node.

    Looks the letter up by ``(role, enforce)`` Key in the binary's
    pre-computed MetaTemplate via ``letter_by_key``; never re-runs
    the cross-arch classifier here. When the MetaTemplate has no
    entry for the Key (or the position is ``variant_specific``) the
    node is rendered white with no sharing label.
    """
    if cls == "toolchain":
        return "lightgray", None
    if cls == "variant_specific":
        return "lightcoral", None
    if cls == "common_dep":
        # Skip nodes where no arch has a variant-0 ident — the legacy
        # renderer collapsed these to "white", we preserve that.
        has_any_drv = any(
            archs_dict[a][0] is not None for a in archs_dict
        )
        if not has_any_drv:
            return "white", None
        letter = letter_by_key.get(key)
        if letter is None:
            return "white", None
        fill = {"A": "orange", "B": "yellow",
                "C": "cyan", "D": "palegreen"}[letter]
        return fill, letter
    return "white", None


def _render_nodes(
    merged: Merged,
    key_class: dict[Key, str],
    key_optional: dict[Key, bool],
    visible: set[Key],
    all_archs: list[str],
    key_to_id: dict[Key, int],
    letter_by_key: dict[Key, Optional[str]],
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
        fill, sharing = _node_fill_and_sharing(
            key_class[k], archs_dict, k, letter_by_key,
        )
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


def _pick_meta_template(
    result: dict, binary: str,
) -> Optional[MetaTemplate]:
    """Return the binary's MetaTemplate from ``result``, or ``None``.

    Result dicts produced by ``StreamPlanner.finalize`` since Phase 4.3
    carry ``meta_templates: dict[binary, list[MetaTemplate]]``. Older
    callers may pass a result without this key (back-compat), in which
    case the renderer falls back to "no shared letter known" rendering
    — every common-dep node renders white. New callers should ensure
    ``meta_templates`` is populated.
    """
    raw = result.get("meta_templates")
    if not raw:
        return None
    metas = raw.get(binary)
    if not metas:
        return None
    return metas[0]


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
    The A/B/C/D letter comes from the binary's pre-computed
    ``MetaTemplate`` in ``result["meta_templates"][binary]``; this
    function never runs the classifier itself.
    """
    by_arch: ByArch = collect_per_arch(result, binary)
    if not by_arch:
        raise ValueError(f"binary {binary!r} not found in result")
    all_archs = sorted(by_arch)
    canonical_root_role = f"{binary}-elf-folder.drv"
    canonical_root_key: Key = (canonical_root_role, None)
    merged, key_class, key_optional = build_merged_keymap(
        by_arch, canonical_root_role)
    visible = _resolve_visible(
        merged, key_class, canonical_root_key, collapse_common_deps)
    key_to_id: dict[Key, int] = {
        k: i for i, k in enumerate(k2 for k2 in merged if k2 in visible)}
    letter_by_key = _build_letter_lookup(_pick_meta_template(result, binary))
    safe_label = (label or f"merged_{binary}").replace('"', '\\"')
    lines: list[str] = [
        f'digraph "{safe_label}" {{',
        "  rankdir=LR;",
        "  node [shape=box, style=filled, fontname=monospace];",
    ]
    lines.extend(_render_nodes(
        merged, key_class, key_optional, visible, all_archs, key_to_id,
        letter_by_key,
    ))
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

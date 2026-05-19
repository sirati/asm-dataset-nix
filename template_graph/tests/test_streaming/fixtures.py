"""Synthetic ``nix-store --query --tree`` line builders for unit tests.

The streaming planner consumes lines exactly as printed by
``nix-store --query --tree``. Each line carries a depth (encoded as
indent + connector segments, 4 chars each) and a store-path. The
``_parse_line`` parser only cares about:
  * indent / connector segment count (depth),
  * the 32-char base32 hash following ``/nix/store/``,
  * the post-hash basename,
  * presence of the ``[...]`` backref suffix.

The helpers here produce that exact shape from a Python dict-tree
description. They never call into nix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from template_graph.streaming import StreamPlanner


# 32 base32 chars. The streaming planner doesn't verify content; what
# matters is that hashes are distinct across nodes that should differ
# AND consistent across DAG-revisits of the same logical drv.
_HEX = "0123456789abcdefghijklmnopqrstuv"


def make_hash(seed: int) -> str:
    """Deterministic 32-char hash from an integer seed."""
    out = []
    s = seed
    for _ in range(32):
        out.append(_HEX[s & 0x1f])
        s = (s * 1103515245 + 12345) & 0xffffffff
    # Salt with the seed so seeds 0/1 still produce distinguishable
    # 32-char strings.
    return f"{seed:08d}".rjust(8, "0") + "".join(out)[:24]


@dataclass
class Node:
    """A synthetic drv node for tree construction.

    Each Node is a (hash, name) pair plus children. ``hash`` is unique
    per logical drv; if you reference the SAME hash twice the second
    occurrence will render as a backref ``[...]`` (nix-store's DAG
    deduplication convention).
    """

    hash: str
    name: str
    children: list["Node"] = field(default_factory=list)


_VARIANT_BASELINE_INNER = "baseline-default-san-off-march-default"
_VARIANT_SUFFIX_TAIL = f"-{_VARIANT_BASELINE_INNER}-elf-folder.drv"


def variant_label(
    binary: str, arch: str, comp: str = "gcc15", opt: str = "O0",
    *, inner: str = _VARIANT_BASELINE_INNER,
) -> str:
    """Sidecar-form variant label produced by the streaming planner
    (``<binary>__<arch>__<comp>-<opt>-<flag>-<hardening>-san-<san>-march-<march>``).
    Pass ``inner`` to override the default baseline suffix axes.
    """
    return f"{binary}__{arch}__{comp}-{opt}-{inner}"


def variant_name(
    binary: str, arch: str, comp: str = "gcc15", opt: str = "O0",
    *, inner: str = _VARIANT_BASELINE_INNER,
) -> str:
    """Produce a drv name that parse_variant_path accepts."""
    return f"{binary}-{arch}-{comp}-{opt}-{inner}-elf-folder.drv"


def render_tree(root: Node) -> str:
    """Render a Node tree to the exact ``nix-store --query --tree``
    text format. Depth-first; siblings get ``├───`` except the last
    which gets ``└───``. Backrefs (second+ occurrence of the same
    hash) get the ``[...]`` suffix.
    """
    seen: set[str] = set()
    lines: list[str] = []

    # Root line — no indent.
    lines.append(f"/nix/store/{root.hash}-{root.name}")
    seen.add(root.hash)

    # ``prefix`` is the accumulated left-of-connector indentation for
    # each child. nix-store uses ``│   `` for "ancestor has more
    # siblings" and ``    `` for "ancestor is last".
    def _walk(node: Node, prefix: str) -> None:
        kids = node.children
        for i, child in enumerate(kids):
            last = (i == len(kids) - 1)
            connector = "└───" if last else "├───"
            is_backref = child.hash in seen
            suffix = " [...]" if is_backref else ""
            line = (
                f"{prefix}{connector}/nix/store/"
                f"{child.hash}-{child.name}{suffix}"
            )
            lines.append(line)
            if is_backref:
                continue
            seen.add(child.hash)
            extension = "    " if last else "│   "
            _walk(child, prefix + extension)

    _walk(root, "")
    return "\n".join(lines)


def feed(planner: StreamPlanner, tree_text: str) -> None:
    """Feed every line of a rendered tree into a StreamPlanner."""
    for line in tree_text.splitlines():
        planner.feed_line(line)


def simple_variant(
    binary: str,
    arch: str,
    *,
    seed_base: int,
    comp: str = "gcc15",
    opt: str = "O0",
    inner: str = _VARIANT_BASELINE_INNER,
    children: Optional[list[Node]] = None,
) -> Node:
    """Build a variant-root Node with the canonical entry-point name.
    Children default to empty; pass explicit child Nodes to extend.
    """
    return Node(
        hash=make_hash(seed_base),
        name=variant_name(binary, arch, comp=comp, opt=opt, inner=inner),
        children=list(children or []),
    )

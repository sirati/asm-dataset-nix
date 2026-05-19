"""MetaTemplate: typed projection of cross-arch sharing.

A ``MetaTemplate`` is the role-merged "skeleton" several arch-local
templates share once their ``(role, enforce)`` keys are folded across
archs. Where one ``Template`` lives inside a single arch, one
``MetaTemplate`` spans every arch that holds a template for one binary
in the matrix.

The construction folds per-arch templates by their ``(role, enforce)``
key (with the variant-root role canonicalised to
``{binary}-elf-folder.drv`` so per-arch roots collapse onto one
position) — see :func:`template_graph.cowalk.cross_arch.build_meta_templates`
and :mod:`template_graph.cowalk._role_merge`. This is a deliberate
semantic shift from the early Phase 4.2 prototype: in production the
per-arch root names embed the arch axis so each arch's template_id is
unique. Per-binary role-merging is the only correct way to project
cross-arch sharing in that setting.

Cross-arch classification values
--------------------------------

Each entry of :attr:`cross_arch_classification` is one of:

``"cross_arch_common_dep"``
    Mapped from class **D** of
    :func:`template_graph.streaming._classify_cross_arch_sharing`.
    Every arch covered at this position realises the role with the
    same underlying drv (one ``(hash, name)`` tuple). The corresponding
    :attr:`drv_per_node` entry is a single ``(hash, name)`` tuple.

``"family_common_dep"``
    Mapped from class **B**. Archs realise the role with one drv per
    arch *family* (e.g. all ``arm`` archs share a drv, all ``x86`` archs
    share another drv). The corresponding :attr:`drv_per_node` entry is
    a mapping ``family -> (hash, name)``.

``"uni_arch_common_dep"``
    Mapped from classes **A** and **C**. Either every arch has its own
    distinct drv (class A — including the single-arch presence case)
    or the partition is more interleaved than "one drv per family"
    (class C). The corresponding :attr:`drv_per_node` entry is a
    mapping ``arch -> (hash, name)``.

``"variant_specific"``
    The role is folded into the per-variant build payload rather than
    pulled up into the meta-template's common skeleton. Either no arch
    has a non-``None`` ident at this position, or the per-arch class
    priority (``variant_specific > common_dep > '?' > toolchain``) was
    won by ``variant_specific``. The corresponding :attr:`drv_per_node`
    entry is ``None``.

Field semantics
---------------

All tuple-shaped fields are indexed by *node position* in the canonical
walk order produced by
:func:`template_graph.cowalk._role_merge.canonical_walk_order` — DFS
from the canonical root Key across the role-merged keymap.

The :attr:`class_letter_at_node` field preserves the original A/B/C/D
letter so the DOT renderer (``template_graph.dot.merge_binary``) can
distinguish class A from class C in node labels without re-running the
classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class MetaTemplate:
    """Cross-arch projection over one binary's arch-local templates.

    One MetaTemplate is built per binary. Each position covers one
    ``(role, enforce)`` key from the role-merged keymap; per-position
    fields record the agreed role name, the optional enforce constraint,
    the cross-arch classification, and the drv ident(s) realising the
    role across the covered archs. See the module docstring for the
    four classification values and their drv shapes.
    """

    # Roles in canonical walk order (DFS from the canonical root Key
    # across the role-merged keymap). Length = number of distinct
    # ``(role, enforce)`` keys observed across the binary's per-arch
    # templates. The variant-root position holds the canonicalised
    # role ``{binary}-elf-folder.drv`` so per-arch roots collapse.
    role_at_node: tuple[str, ...] = field(default_factory=tuple)

    # Per-position enforce constraint (``TemplateNode.enforce``). When
    # a template node is split by ``_split_dag_revisit`` two positions
    # share the same role but differ in enforce. ``None`` for plain
    # (unsplit) positions.
    enforce_at_node: tuple[
        Optional[tuple[str, Optional[str]]], ...
    ] = field(default_factory=tuple)

    # Per-arch template id covering this binary. Maps an arch short
    # key (matching ``lib/architectures.nix``) to the integer template
    # id whose nodes contribute to this meta-template's role-merged
    # positions.
    template_id_per_arch: Mapping[str, int] = field(default_factory=dict)

    # Role-level classification across the covered archs. Length must
    # equal ``len(role_at_node)``. Allowed string values:
    #   "cross_arch_common_dep" — A/B/C/D classification class "D"
    #   "family_common_dep"     — class B
    #   "uni_arch_common_dep"   — class A or C
    #   "variant_specific"      — folded into the variant build
    # See the module docstring for the full mapping.
    cross_arch_classification: tuple[str, ...] = field(default_factory=tuple)

    # Original A/B/C/D class letter from
    # ``_classify_cross_arch_sharing``. ``None`` for variant_specific
    # positions. Preserves the A-vs-C distinction the dot renderer
    # surfaces in its node label suffix; the
    # ``cross_arch_classification`` enum collapses them.
    class_letter_at_node: tuple[Optional[str], ...] = field(
        default_factory=tuple
    )

    # Resolved drv ident(s) for each role. Length must equal
    # ``len(role_at_node)``. Per-entry shape depends on the matching
    # ``cross_arch_classification`` value:
    #   "cross_arch_common_dep" -> single (hash, name) tuple
    #   "family_common_dep"     -> Mapping[family, (hash, name)]
    #   "uni_arch_common_dep"   -> Mapping[arch,   (hash, name)]
    #   "variant_specific"      -> None
    drv_per_node: tuple[Any, ...] = field(default_factory=tuple)

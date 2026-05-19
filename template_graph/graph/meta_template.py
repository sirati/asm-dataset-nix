"""MetaTemplate: typed projection of cross-arch sharing.

A ``MetaTemplate`` is the structural "skeleton" that several arch-local
templates share once their node lists have been aligned by the
recursive-walk order used in ``_shape_equal``
(:mod:`template_graph.graph.template`). Where one ``Template`` lives
inside a single arch, one ``MetaTemplate`` spans several archs whose
arch-local templates have the same shape and the same role names at
every aligned node position.

The dataclass mirrors what is currently computed on the fly inside
``_classify_cross_arch_sharing`` (in ``template_graph.streaming._helpers``)
and consumed by ``template_graph.dot.merge_binary``: for each role
position, what is its cross-arch classification and which concrete drv
ident(s) realise it.

Cross-arch classification values
--------------------------------

Each entry of :attr:`cross_arch_classification` is one of:

``"cross_arch_common_dep"``
    Mapped from class **D** in ``_classify_cross_arch_sharing``. Every
    arch covered by this meta-template realises the role with the same
    underlying drv (one ``(hash, name)`` tuple). The corresponding
    :attr:`drv_per_node` entry is a single ``(hash, name)`` tuple.

``"family_common_dep"``
    Mapped from class **B**. Archs realise the role with one drv per
    arch *family* (e.g. all ``arm`` archs share a drv, all ``x86`` archs
    share another drv). The corresponding :attr:`drv_per_node` entry is
    a mapping ``family -> (hash, name)``.

``"uni_arch_common_dep"``
    Mapped from classes **A** and **C**. Either every arch has its own
    distinct drv (class A) or the partition is more interleaved than
    "one drv per family" (class C). In both cases the natural carrier
    is per-arch. The corresponding :attr:`drv_per_node` entry is a
    mapping ``arch -> (hash, name)``.

``"variant_specific"``
    The role is folded into the per-variant build payload rather than
    pulled up into the meta-template's common skeleton: there is no
    single drv that all variants covered by this meta-template share at
    this role. The corresponding :attr:`drv_per_node` entry is ``None``.

Field semantics
---------------

All four tuple-shaped fields are indexed by *node position* in the
canonical recursive-walk order produced by ``_shape_equal`` — that is,
position ``i`` refers to "the role each arch-local template visits at
step ``i`` of the recursive walk from the root".

Construction invariant
----------------------

Because every covered ``(template_id, arch)`` cell passed ``_shape_equal``,
the role name at each aligned position must agree across all archs.
:attr:`role_at_node` records that single agreed name per position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class MetaTemplate:
    """Cross-arch projection over a set of shape-equal arch-local templates.

    Holds the agreed role name per node, the per-arch template id, and
    the per-node cross-arch classification together with whatever drv
    ident(s) realise each role across the covered archs. See the module
    docstring for the four classification values and their drv shapes.
    """

    # Roles in canonical recursive-walk order. Length = number of nodes
    # covered by this meta-template. All covered (template_id, arch)
    # cells produced the same role at each position because they passed
    # ``_shape_equal`` (template_graph.graph.template).
    role_at_node: tuple[str, ...] = field(default_factory=tuple)

    # Per-arch template id covering this meta-template: maps an arch
    # short key (matching ``lib/architectures.nix``) to the integer
    # template id (index into the per-arch template list) whose nodes
    # are aligned by ``role_at_node``.
    template_id_per_arch: Mapping[str, int] = field(default_factory=dict)

    # Role-level classification across the covered archs. Length must
    # equal ``len(role_at_node)``. Allowed string values:
    #   "cross_arch_common_dep" — A/B/C/D classification class "D"
    #   "family_common_dep"     — class B
    #   "uni_arch_common_dep"   — class A or C
    #   "variant_specific"      — folded into the variant build
    # See the module docstring for the full mapping.
    cross_arch_classification: tuple[str, ...] = field(default_factory=tuple)

    # Resolved drv ident(s) for each role. Length must equal
    # ``len(role_at_node)``. Per-entry shape depends on the matching
    # ``cross_arch_classification`` value:
    #   "cross_arch_common_dep" -> single (hash, name) tuple
    #   "family_common_dep"     -> Mapping[family, (hash, name)]
    #   "uni_arch_common_dep"   -> Mapping[arch,   (hash, name)]
    #   "variant_specific"      -> None
    drv_per_node: tuple[Any, ...] = field(default_factory=tuple)

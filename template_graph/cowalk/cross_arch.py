"""Per-binary cross-arch ``MetaTemplate`` construction.

For each binary in a finalised ``OutputState`` this module emits ONE
``MetaTemplate`` covering every arch whose calibration variant landed
under that binary. Construction walks the role-merged keymap from
:mod:`template_graph.cowalk._role_merge`: each ``(role, enforce)`` Key
becomes one position in the MetaTemplate, classified against the
per-arch calibration idents (column 0 of every covered
``VariantArray.hashes``).

Why role-merging instead of per-template-id grouping
----------------------------------------------------

The early Phase 4.2 prototype grouped per-arch templates by shared
``template_id``. In production the variant root role embeds
``<binary>-<arch>-<comp>-<opt>-…`` so ``drv_role`` doesn't strip the
arch axis — every ``(arch, binary)`` ends up with its own template_id
and no template_id is shared across archs. The merge_binary DOT
renderer already worked around this by collapsing per-arch roots onto
``{binary}-elf-folder.drv`` and keying nodes by ``(role, enforce)``;
this module lifts that approach into the canonical MetaTemplate
construction so cross-arch projection works in production, not only on
synthetic single-arch fixtures.

Classification mapping
----------------------

The A/B/C/D classifier (:func:`template_graph.streaming._classify_cross_arch_sharing`)
runs unchanged on the per-arch variant-0 idents at each position. We
project the result onto ``MetaTemplate.cross_arch_classification``:

  ``cross_arch_common_dep`` — class **D** (same ident across archs)
  ``family_common_dep``     — class **B** (one ident per arch family)
  ``uni_arch_common_dep``   — class **A** or **C** (per-arch / mixed)
  ``variant_specific``      — at least one arch flagged the role
                              variant_specific, or no arch has a
                              non-``None`` ident here.

The original A/B/C/D letter is also stored in
``class_letter_at_node`` so consumers (DOT renderer) can preserve the
A-vs-C distinction without recomputing the classifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Optional

from template_graph.cowalk._role_merge import (
    Key,
    Merged,
    build_merged_keymap,
    canonical_walk_order,
    collect_per_arch,
)
from template_graph.graph import MetaTemplate
from template_graph.streaming._helpers import (
    _ARCH_FAMILIES,
    _classify_cross_arch_sharing,
)

if TYPE_CHECKING:
    from template_graph.streaming.state import OutputState


# A/B/C/D → MetaTemplate.cross_arch_classification value.
_CLASS_NAME: dict[str, str] = {
    "A": "uni_arch_common_dep",
    "B": "family_common_dep",
    "C": "uni_arch_common_dep",
    "D": "cross_arch_common_dep",
}


def _by_family(
    arch_to_drv: Mapping[str, tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Collapse ``arch_to_drv`` to one ident per arch family.

    Pre-condition: the A/B/C/D classifier already returned ``B``, which
    guarantees every family is realised by a single ident across its
    archs. Used to shape ``drv_per_node`` for ``family_common_dep``.
    """
    out: dict[str, tuple[str, str]] = {}
    for arch, ident in arch_to_drv.items():
        fam = _ARCH_FAMILIES.get(arch, "other")
        out[fam] = ident
    return out


def _drv_value_for_class(
    letter: str, arch_to_drv: Mapping[str, tuple[str, str]],
) -> object:
    """Shape ``drv_per_node`` payload per the A/B/C/D class letter."""
    if letter == "D":
        # Class D: identical ident across all covered archs.
        return next(iter(arch_to_drv.values()))
    if letter == "B":
        return _by_family(arch_to_drv)
    # A or C: keep per-arch idents.
    return dict(arch_to_drv)


def _classify_position(
    arch_to_drv: dict[str, tuple[str, str]],
    is_variant_specific: bool,
) -> tuple[str, Optional[str], object]:
    """Return ``(classification, class_letter, drv_value)`` for one role.

    ``class_letter`` is the original A/B/C/D letter (``None`` for
    ``variant_specific``). ``drv_value`` matches
    ``MetaTemplate.drv_per_node`` shape:

      * ``cross_arch_common_dep`` -> single ``(hash, name)`` tuple
      * ``family_common_dep``     -> ``{family -> (hash, name)}``
      * ``uni_arch_common_dep``   -> ``{arch   -> (hash, name)}``
      * ``variant_specific``      -> ``None``
    """
    if is_variant_specific or not arch_to_drv:
        return "variant_specific", None, None
    letter = _classify_cross_arch_sharing(arch_to_drv)
    return _CLASS_NAME[letter], letter, _drv_value_for_class(
        letter, arch_to_drv
    )


def _arch_idents_at_key(
    merged: Merged, k: Key,
) -> dict[str, tuple[str, str]]:
    """Read each arch's variant-0 ident at Key ``k``.

    The role-merged keymap stores the variant-0 drv (or ``None`` for
    non-common_dep cells) under ``merged[k][arch][0]``. Drop archs
    whose cell is ``None`` so the classifier only sees archs that
    actually realise the role at variant 0.
    """
    out: dict[str, tuple[str, str]] = {}
    for arch, (drv, _children) in merged[k].items():
        if drv is None:
            continue
        out[arch] = drv
    return out


def _build_meta_template_for_binary(
    binary: str,
    by_arch_template_id: Mapping[str, int],
    merged: Merged,
    key_class: Mapping[Key, str],
    order: tuple[Key, ...],
) -> MetaTemplate:
    """Project one binary's role-merged keymap into a ``MetaTemplate``.

    Walks ``order`` Keys, classifying each by its per-arch variant-0
    idents (or marking ``variant_specific`` when the merged class
    already lost to that priority). Result is a single MetaTemplate
    spanning every arch covered for ``binary``.
    """
    roles: list[str] = []
    enforces: list[Optional[tuple[str, Optional[str]]]] = []
    classifications: list[str] = []
    letters: list[Optional[str]] = []
    drvs: list[object] = []
    for k in order:
        role, enforce = k
        roles.append(role)
        enforces.append(enforce)
        merged_cls = key_class.get(k, "?")
        arch_to_drv = _arch_idents_at_key(merged, k)
        is_variant_specific = (
            merged_cls == "variant_specific" or merged_cls == "toolchain"
        )
        # Toolchain rows never record idents anyway (cowalk's
        # ``_record`` short-circuits on ``is_toolchain``) so the empty
        # arch_to_drv would already classify variant_specific; we make
        # it explicit for clarity. ``?`` rows fall through to the
        # classifier, which will return ``A`` for single-arch presence
        # or the appropriate letter for multi-arch.
        cls, letter, drv = _classify_position(
            arch_to_drv, is_variant_specific,
        )
        classifications.append(cls)
        letters.append(letter)
        drvs.append(drv)
    # Deterministic arch ordering (test invariant + readability).
    template_id_per_arch = {
        arch: tid for arch, tid in sorted(by_arch_template_id.items())
    }
    return MetaTemplate(
        role_at_node=tuple(roles),
        enforce_at_node=tuple(enforces),
        template_id_per_arch=template_id_per_arch,
        cross_arch_classification=tuple(classifications),
        class_letter_at_node=tuple(letters),
        drv_per_node=tuple(drvs),
    )


def build_meta_templates(
    out: "OutputState", binary: str,
) -> list[MetaTemplate]:
    """Build the per-binary ``MetaTemplate`` list.

    Returns a list with at most one ``MetaTemplate`` — one per binary
    covering every arch that holds a template for it. Returns ``[]``
    when no per-arch template's root names ``binary`` (the binary
    isn't present in this output state).

    Construction:

      1. Pick per-arch cells whose root role names ``binary``
         (:func:`template_graph.cowalk._role_merge.collect_per_arch`).
      2. Fold them into a ``(role, enforce)``-keyed merged keymap with
         the variant-root role canonicalised to
         ``{binary}-elf-folder.drv``.
      3. DFS the keymap from the canonical root Key to get a
         deterministic position order.
      4. Classify each position against per-arch variant-0 idents.

    Does not consult ``out.toolchain_drvs`` directly — toolchain
    template nodes are detected via ``TemplateNode.is_toolchain``
    inside :func:`build_merged_keymap`, which is sufficient for
    cross-arch sharing projection.
    """
    by_arch = collect_per_arch(out, binary)
    if not by_arch:
        return []
    canonical_root_role = f"{binary}-elf-folder.drv"
    merged, key_class, _key_optional = build_merged_keymap(
        by_arch, canonical_root_role,
    )
    canonical_root_key: Key = (canonical_root_role, None)
    order = canonical_walk_order(merged, canonical_root_key)
    by_arch_template_id = {
        arch: arr.template_id for arch, (_tmpl, _cls, arr) in by_arch.items()
    }
    return [_build_meta_template_for_binary(
        binary, by_arch_template_id, merged, key_class, order,
    )]


__all__ = ["build_meta_templates"]

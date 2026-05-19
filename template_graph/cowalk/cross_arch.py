"""Cross-arch MetaTemplate construction (Phase 4.2 — plan §E1).

For each binary in a finalized ``OutputState`` this module emits one
``MetaTemplate`` per ``template_id`` shared by more than one arch.
Each MetaTemplate covers all archs whose calibration variants landed
on that template_id, projecting the per-arch ``arr.hashes[nid][0]``
calibration idents into a per-position cross-arch classification:

  * ``cross_arch_common_dep`` — every arch has the same ident at
    this position (A/B/C/D class **D**).
  * ``family_common_dep``    — idents agree within arch family but
    differ across families (class **B**).
  * ``uni_arch_common_dep``  — idents differ per arch (class A) or
    are mixed across family boundaries (class **C**).
  * ``variant_specific``     — at least one arch's calibration
    classified this node as variant_specific, or no arch has a
    non-``None`` ident at this position.

The A/B/C/D classifier itself lives in
``template_graph.streaming._helpers`` and is consumed verbatim by
``template_graph.dot.merge_binary`` for visualisation. Phase 4.3 will
switch the DOT renderer to consume MetaTemplate; once that lands the
copy in ``streaming/_helpers.py`` can be retired in favour of this
module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from template_graph.graph import (
    MetaTemplate,
    Template,
    VariantArray,
    _shape_equal,
)
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


def _collect_binary_cells(
    out: OutputState, binary: str,
) -> dict[int, dict[str, VariantArray]]:
    """Group ``(template_id, arch) -> VariantArray`` cells by template_id
    for templates whose root role names this binary.

    Matches ``dot.merge_binary._collect_per_arch``: a template "belongs
    to" ``binary`` iff its root node's name starts with ``{binary}-``
    and contains ``-elf-folder``. Only template_ids that appear in
    more than one arch within this binary's matrix are returned —
    single-arch template_ids have nothing to share cross-arch.
    """
    grouped: dict[int, dict[str, VariantArray]] = {}
    for (tid, arch), arr in out.variant_arrays.items():
        tmpl = out.templates[tid]
        root = tmpl.nodes[tmpl.root_id]
        if not (
            root.name.startswith(f"{binary}-")
            and "-elf-folder" in root.name
        ):
            continue
        grouped.setdefault(tid, {})[arch] = arr
    return {tid: per_arch for tid, per_arch in grouped.items()
            if len(per_arch) > 1}


def _canonical_node_order(template: Template) -> tuple[int, ...]:
    """Return the canonical recursive-walk node order over ``template``.

    Uses ``_shape_equal(template, template)`` so the produced order
    matches the alignment ``MetaTemplate`` documents. Self-alignment
    yields identity ``(nid, nid)`` pairs in the recursive-walk
    pre-order from the root; we project that down to the ``a``-side
    node ids.
    """
    alignment = _shape_equal(template, template)
    assert alignment is not None, (
        "self-alignment of a template should always succeed"
    )
    return tuple(an for (an, _bn) in alignment.node_pairs)


def _arch_to_drv_at_node(
    per_arch: Mapping[str, VariantArray], nid: int,
) -> dict[str, tuple[str, str]]:
    """Read each arch's calibration ident at ``nid``.

    Drops archs whose ``arr.hashes[nid][0]`` is ``None`` (variant 0
    didn't cover this node) or whose row is empty. Returns a plain
    ``arch -> (hash, name)`` dict suitable for
    ``_classify_cross_arch_sharing``.
    """
    out: dict[str, tuple[str, str]] = {}
    for arch, arr in per_arch.items():
        if not arr.hashes[nid]:
            continue
        ident = arr.hashes[nid][0]
        if ident is None:
            continue
        out[arch] = ident
    return out


def _any_arch_variant_specific(
    per_arch_classifications: Mapping[str, dict[int, str]], nid: int,
) -> bool:
    """True iff any covered arch's calibration-pair classifier marked
    ``nid`` as ``variant_specific``.

    Mirrors ``dot.merge_binary._build_merged_keymap``'s priority rule
    (``variant_specific > common_dep > '?' > toolchain``): a node that
    one arch sees as variant-specific cannot be promoted to a shared
    meta-template entry, even if other archs see it as common_dep.
    """
    for classes in per_arch_classifications.values():
        if classes.get(nid) == "variant_specific":
            return True
    return False


def _by_family(
    arch_to_drv: Mapping[str, tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Collapse ``arch_to_drv`` to one ident per arch family.

    Used for the ``family_common_dep`` drv shape. Pre-condition: the
    A/B/C/D classifier already returned ``B``, which guarantees every
    family is realised by a single ident across its archs.
    """
    out: dict[str, tuple[str, str]] = {}
    for arch, ident in arch_to_drv.items():
        fam = _ARCH_FAMILIES.get(arch, "other")
        # Class B invariant: all archs in a family share the same ident.
        out[fam] = ident
    return out


def _classify_position(
    arch_to_drv: dict[str, tuple[str, str]],
    is_variant_specific: bool,
) -> tuple[str, object]:
    """Return ``(classification, drv_per_node_value)`` for one role.

    ``drv_per_node_value`` shape follows ``MetaTemplate.drv_per_node``:

      * ``cross_arch_common_dep`` -> single ``(hash, name)`` tuple
      * ``family_common_dep``     -> ``{family -> (hash, name)}``
      * ``uni_arch_common_dep``   -> ``{arch   -> (hash, name)}``
      * ``variant_specific``      -> ``None``
    """
    if is_variant_specific or not arch_to_drv:
        return "variant_specific", None
    cat = _classify_cross_arch_sharing(arch_to_drv)
    classification = _CLASS_NAME[cat]
    if classification == "cross_arch_common_dep":
        # Class D: identical ident across all covered archs.
        drv = next(iter(arch_to_drv.values()))
        return classification, drv
    if classification == "family_common_dep":
        return classification, _by_family(arch_to_drv)
    # uni_arch_common_dep: keep per-arch idents.
    return classification, dict(arch_to_drv)


def _build_one_meta_template(
    template: Template,
    per_arch: Mapping[str, VariantArray],
    per_arch_classifications: Mapping[str, dict[int, str]],
) -> MetaTemplate:
    """Project one shared-tid group into a ``MetaTemplate``."""
    order = _canonical_node_order(template)
    roles: list[str] = []
    classifications: list[str] = []
    drvs: list[object] = []
    for nid in order:
        roles.append(template.nodes[nid].name)
        arch_to_drv = _arch_to_drv_at_node(per_arch, nid)
        is_variant_specific = _any_arch_variant_specific(
            per_arch_classifications, nid
        )
        cls, drv = _classify_position(arch_to_drv, is_variant_specific)
        classifications.append(cls)
        drvs.append(drv)
    # Deterministic arch ordering (test invariant + readability).
    template_id_per_arch = {
        arch: arr.template_id for arch, arr in sorted(per_arch.items())
    }
    return MetaTemplate(
        role_at_node=tuple(roles),
        template_id_per_arch=template_id_per_arch,
        cross_arch_classification=tuple(classifications),
        drv_per_node=tuple(drvs),
    )


def build_meta_templates(
    out: OutputState, binary: str,
) -> list[MetaTemplate]:
    """Build the per-binary ``MetaTemplate`` list (plan §E1).

    For each ``template_id`` shared by more than one arch within
    ``binary``'s matrix, walk the shared template in canonical
    recursive-walk order, classify each role-position against the
    per-arch calibration idents (column 0 of every covered
    ``VariantArray.hashes``), and emit one ``MetaTemplate``. Result is
    ordered by ``template_id`` so the list is deterministic.

    Does not consult ``out.toolchain_drvs`` or
    ``Template.nodes[*].is_toolchain``: those drive task-emission
    short-circuits in Phase 4.3 (plan §E2). This module's contract is
    limited to the cross-arch sharing projection.
    """
    grouped = _collect_binary_cells(out, binary)
    meta_templates: list[MetaTemplate] = []
    for tid in sorted(grouped):
        per_arch = grouped[tid]
        per_arch_cls: dict[str, dict[int, str]] = {
            arch: out.classifications.get((tid, arch), {})
            for arch in per_arch
        }
        meta_templates.append(
            _build_one_meta_template(
                out.templates[tid], per_arch, per_arch_cls
            )
        )
    return meta_templates


__all__ = ["build_meta_templates"]

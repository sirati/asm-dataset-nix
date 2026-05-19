"""Free-function helpers used across the streaming subpackage.

These are pure utilities with no streaming-state dependency:
``drv_name_full`` (drv-name passthrough) and the cross-arch sharing
classifier consumed by ``template_graph.dot.merge_binary``.

The arch-triple table, triple/version extractors, and the
revisit-diff classifier moved to ``template_graph.cowalk._helpers``
(those are cowalk-internal — see that module's docstring). The
streaming package re-exports them for back-compat.
"""

from __future__ import annotations


def drv_name_full(name: str) -> str:
    """Identity passthrough for the post-hash drv name. Kept as a
    function so callers can switch in another extractor."""
    return name


_ARCH_FAMILIES: dict[str, str] = {
    "x86_64": "x86",
    "i686": "x86",
    "aarch64": "arm",
    "armv7l-hf": "arm",
    "armv7l-sf": "arm",
    "mips64el": "mips",
    "mipsel": "mips",
    "ppc32": "power",
    "ppc64": "power",
    "riscv64": "riscv",
}


def _classify_cross_arch_sharing(
    arch_to_drv: dict[str, str],
) -> str:
    """Returns one of 'A','B','C','D'. See merge_binary_to_dot doc."""
    # Single-arch presence is by definition not "shared across archs",
    # even though the unique-set is trivially of size 1. Treat as A.
    if len(arch_to_drv) <= 1:
        return "A"
    unique = set(arch_to_drv.values())
    if len(unique) == 1:
        return "D"
    if len(unique) == len(arch_to_drv):
        return "A"
    # Each drv → set of archs that have it.
    by_drv: dict[str, frozenset[str]] = {}
    for arch, drv in arch_to_drv.items():
        by_drv.setdefault(drv, set()).add(arch)
    by_drv = {d: frozenset(a) for d, a in by_drv.items()}
    # For each drv's arch-set, is it exactly one whole family
    # (or a single arch from a family with no other family members
    # present)? "Different per family" = every group is a single
    # complete family. Anything more interleaved = "mixed".
    all_families_present: dict[str, set[str]] = {}
    for arch in arch_to_drv:
        fam = _ARCH_FAMILIES.get(arch, "other")
        all_families_present.setdefault(fam, set()).add(arch)
    family_clean = True
    for archs in by_drv.values():
        fams = {_ARCH_FAMILIES.get(a, "other") for a in archs}
        if len(fams) > 1:
            family_clean = False
            break
        # All archs in this group come from one family. Check that
        # ALL archs from that family which are present in our
        # observation set are in this group — otherwise the family
        # is split across drvs (mixed).
        fam = next(iter(fams))
        if archs != all_families_present[fam]:
            family_clean = False
            break
    return "B" if family_clean else "C"

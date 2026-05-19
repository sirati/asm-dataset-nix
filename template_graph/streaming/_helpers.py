"""Free-function helpers used across the streaming subpackage.

These are pure utilities with no streaming-state dependency:
``drv_name_full`` (drv-name passthrough), arch-to-triple table,
triple/version extractors, the revisit-diff classifier (used by
cowalk), and the cross-arch sharing classifier consumed by
``template_graph.dot.merge_binary``.
"""

from __future__ import annotations

import re
from typing import Optional

from template_graph.parser.role import _TRIPLE_RE


def drv_name_full(name: str) -> str:
    """Identity passthrough for the post-hash drv name. Kept as a
    function so callers can switch in another extractor."""
    return name


# Map our short arch keys (matching lib/architectures.nix) to the
# canonical Nix target triple. x86_64 is native — no triple shows up
# in drv names.
_ARCH_TO_TRIPLE: dict[str, Optional[str]] = {
    "x86_64":    None,
    "i686":      "i686-unknown-linux-gnu",
    "aarch64":   "aarch64-unknown-linux-gnu",
    "armv7l-hf": "armv7l-unknown-linux-gnueabihf",
    "armv7l-sf": "armv7l-unknown-linux-gnueabi",
    "mipsel":    "mipsel-unknown-linux-gnu",
    "mips64el":  "mips64el-unknown-linux-gnuabin32",
    "ppc32":     "powerpc-unknown-linux-gnu",
    "ppc64":     "powerpc64-unknown-linux-gnuabielfv2",
    "riscv64":   "riscv64-unknown-linux-gnu",
}


def _extract_triple(name: str) -> Optional[str]:
    """Return the embedded target triple (no leading/trailing - or _)
    or None if the name has none."""
    base = name[:-4] if name.endswith(".drv") else name
    m = _TRIPLE_RE.search(base)
    return m.group(0).strip("-_") if m else None


def _extract_version(name: str) -> str:
    """Triple-strip the name, then return the first -<digits>[.<digits>...]
    sequence, or '' if none."""
    base = name[:-4] if name.endswith(".drv") else name
    no_triple = _TRIPLE_RE.sub("", base)
    m = re.search(r"-(\d[\d.]*[a-z]?\d*)", no_triple)
    return m.group(1) if m else ""


def _classify_revisit_diff(
    stored: tuple[str, str],
    observed: tuple[str, str],
) -> Optional[tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]]]:
    """Decide whether two distinct (hash, name) values at the same DAG
    position differ purely by target-triple or purely by version. Returns
    (diff_kind, stored_enforce, observed_enforce) or None for any other
    kind of difference (caller should fall through to the violation log).
    """
    sn, on = stored[1], observed[1]
    s_tri = _extract_triple(sn)
    o_tri = _extract_triple(on)
    s_ver = _extract_version(sn)
    o_ver = _extract_version(on)
    if s_tri != o_tri and s_ver == o_ver:
        return ("triple", ("triple", s_tri), ("triple", o_tri))
    if s_tri == o_tri and s_ver != o_ver:
        return ("version", ("version", s_ver), ("version", o_ver))
    return None


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

"""Free-function helpers consumed by the cowalk algorithm.

Lives in ``template_graph.cowalk`` because the only runtime callers
are ``cowalk_variant`` (``_ARCH_TO_TRIPLE`` for triple-enforce
collapse, ``_classify_revisit_diff`` for DAG-revisit splitting); the
streaming subpackage previously hosted these and back-imported into
cowalk, violating the parser -> graph -> cowalk -> streaming layering.

``template_graph.streaming.__init__`` keeps a re-export shim so the
external surface ``from template_graph.streaming import _ARCH_TO_TRIPLE``
(and the three siblings) still resolves.
"""

from __future__ import annotations

import re
from typing import Optional

from template_graph.parser.role import _TRIPLE_RE


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

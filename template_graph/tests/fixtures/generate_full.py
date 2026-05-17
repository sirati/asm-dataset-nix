#!/usr/bin/env python3
"""Generate a LARGE sum drv covering many binaries / all (arch, comp) combos.

Pulls:
  - 2 binaries from each of the 14 categories defined in lib/packages.nix
    (first two valid entries in high → med → low order, per category).
  - All (arch, compiler) pairs from ``_crossToolchainMap.x86_64-linux``.
  - 2 opt levels per (binary, arch, compiler): O0 + O2, with
    ``baseline-default-san-off-march-default`` as the rest of the
    suffix.

Many (binary, arch, compiler) combos are gated as invalid by the
matrix (old compilers vs new archs, etc.). We pass
``tolerate_missing=True`` so ``builtins.tryEval`` silently drops the
failing attrs at nix-instantiation time.

Output: ``full_sum_drv.txt`` (single drv path).

Run from the project root::

    python3 template_graph/tests/fixtures/generate_full.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from template_graph.make_sum_drv import make_sum_drv  # noqa: E402


SYS = "x86_64-linux"
OPTS = ["O0", "O2"]
SUFFIX_TEMPLATE = "{comp}-{opt}-baseline-default-san-off-march-default"

FIX_DIR = Path(__file__).parent
OUT_FILE = FIX_DIR / "full_sum_drv.txt"

PACKAGES_NIX = PROJECT_ROOT / "lib" / "packages.nix"


# ---------------------------------------------------------------------------
# Parse lib/packages.nix for the 14 categories + 2 binaries each.
# ---------------------------------------------------------------------------


_TOP_CATS = {
    "smoke", "smallLibs", "gnuUtils", "parsers", "codecs", "crypto",
    "serialization", "math", "networking", "interpreters", "system",
    "binaryAnalysis", "cliTools", "misc",
}


def _pick_two_per_category() -> dict[str, list[str]]:
    text = PACKAGES_NIX.read_text()
    out: dict[str, list[str]] = {}
    for m in re.finditer(
        r"^\s+(\w+) = \{\s*\n(.*?)^\s*\};", text, re.M | re.S
    ):
        cat = m.group(1)
        if cat not in _TOP_CATS:
            continue
        body = m.group(2)
        picks: list[str] = []
        for section in ("high", "med", "low"):
            sm = re.search(rf"{section} = \[(.*?)\];", body, re.S)
            if not sm:
                continue
            for pm in re.finditer(r'\(pkg "([\w\-]+)"', sm.group(1)):
                picks.append(pm.group(1))
                if len(picks) == 2:
                    break
            if len(picks) == 2:
                break
        if picks:
            out[cat] = picks
    return out


# ---------------------------------------------------------------------------
# Enumerate (arch, compiler) pairs from _crossToolchainMap.
# ---------------------------------------------------------------------------


def _toolchain_pairs() -> list[tuple[str, str]]:
    proc = subprocess.run(
        [
            "nix", "eval", "--json",
            f".#_crossToolchainMap.{SYS}",
            "--apply",
            "m: builtins.mapAttrs (a: v: builtins.attrNames v) m",
        ],
        capture_output=True, check=True,
    )
    m = json.loads(proc.stdout.decode("utf-8"))
    pairs: list[tuple[str, str]] = []
    for arch in sorted(m):
        for comp in sorted(m[arch]):
            pairs.append((arch, comp))
    return pairs


# ---------------------------------------------------------------------------
# Build attr lists.
# ---------------------------------------------------------------------------


def _toolchain_attrs(pairs: list[tuple[str, str]]) -> list[str]:
    return [
        f'outputs._crossToolchainMap.{SYS}.{arch}."{comp}"'
        for arch, comp in pairs
    ]


def _variant_attrs(
    binary: str, pairs: list[tuple[str, str]]
) -> list[str]:
    out = []
    for arch, comp in pairs:
        for opt in OPTS:
            suffix = SUFFIX_TEMPLATE.format(comp=comp, opt=opt)
            out.append(
                f'outputs.dataset.{SYS}.{binary}.{arch}."{suffix}"'
            )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    cats = _pick_two_per_category()
    sys.stderr.write(f"# {sum(len(v) for v in cats.values())} binaries across "
                     f"{len(cats)} categories\n")
    for cat, bins in sorted(cats.items()):
        sys.stderr.write(f"#   {cat:18s} {bins}\n")

    pairs = _toolchain_pairs()
    sys.stderr.write(f"# {len(pairs)} (arch, compiler) pairs\n")

    binaries = [b for bs in cats.values() for b in bs]
    n_variants = len(binaries) * len(pairs) * len(OPTS)
    sys.stderr.write(
        f"# enumerating {n_variants} variant attrs "
        f"({len(binaries)} bins × {len(pairs)} (arch,comp) × {len(OPTS)} opts)\n"
    )

    toolchains = _toolchain_attrs(pairs)
    matrices: dict[str, list[str]] = {}
    for binary in binaries:
        matrices[f"matrix-{binary}"] = _variant_attrs(binary, pairs)

    flake_ref = f"git+file://{PROJECT_ROOT}"
    bash_attr = f"inputs.nixpkgs.legacyPackages.{SYS}.bash"

    sys.stderr.write("# running nix-instantiate (this can take a few minutes)...\n")
    drv = make_sum_drv(
        flake_ref=flake_ref,
        bash_attr=bash_attr,
        toolchain_attrs=toolchains,
        matrices=matrices,
        root_name="sum-root",
        toolchains_name="toolchains",
        system=SYS,
        tolerate_missing=True,
    )

    OUT_FILE.write_text(
        "# Generated by template_graph/tests/fixtures/generate_full.py\n"
        f"# {len(binaries)} binaries × {len(pairs)} (arch,comp) × {len(OPTS)} opts "
        f"(invalid combos dropped by tryEval at instantiation time).\n"
        f"{drv}\n"
    )
    sys.stderr.write(f"# wrote {OUT_FILE}\n")
    print(drv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

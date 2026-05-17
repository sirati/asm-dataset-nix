#!/usr/bin/env python3
"""Generate a sum drv covering the ENTIRE (arch × compiler) matrix.

Pipeline:
1. Enumerate all (binary, arch, comp) tuples from packages.nix (2 per
   category × 14 categories = 28 binaries) and ``_crossToolchainMap``
   (317 valid pairs).
2. For each (binary, arch, comp, opt), spawn ``nix eval --raw
   .drvPath`` in parallel (default 16 workers). Each subprocess
   catches its own errors — including the attribute-missing class
   that ``tryEval`` can't catch.
3. Cache pass/fail results in ``.probe_cache.json`` so re-runs are
   instant (~seconds) for tuples we've already seen.
4. Build the sum drv from the surviving set with ``make_sum_drv``.

Output: ``full_filtered_sum_drv.txt``.
"""

from __future__ import annotations

import concurrent.futures as cf
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
SUFFIX = "{comp}-{opt}-baseline-default-san-off-march-default"
WORKERS = 16

FIX_DIR = Path(__file__).parent
CACHE_FILE = FIX_DIR / ".probe_cache.json"
OUT_FILE = FIX_DIR / "full_filtered_sum_drv.txt"

PACKAGES_NIX = PROJECT_ROOT / "lib" / "packages.nix"

# 14 top-level category names in lib/packages.nix
_TOP_CATS = {
    "smoke", "smallLibs", "gnuUtils", "parsers", "codecs", "crypto",
    "serialization", "math", "networking", "interpreters", "system",
    "binaryAnalysis", "cliTools", "misc",
}


def pick_two_per_category() -> list[str]:
    text = PACKAGES_NIX.read_text()
    out: list[str] = []
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
            # Second arg of (pkg "<attr>" "<label>") — the LABEL is
            # what the dataset attrset is keyed by.
            for pm in re.finditer(
                r'\(pkg "[\w\-]+" "([\w\-]+)"', sm.group(1)
            ):
                picks.append(pm.group(1))
                if len(picks) == 2:
                    break
            if len(picks) == 2:
                break
        out.extend(picks)
    return out


def toolchain_pairs() -> list[tuple[str, str]]:
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
    return [(arch, comp) for arch in sorted(m) for comp in sorted(m[arch])]


def valid_meta_combos(binary: str) -> set[tuple[str, str, str]]:
    """Per-(binary) intersection of matrix availability with our chosen
    suffix shape. Returns (arch, comp, opt) tuples that the matrix
    actually exposes for ``binary`` — eliminates the 'flake does not
    provide attribute' probe class.
    """
    # Match e.g. "gcc15-O0-baseline-default-san-off-march-default" and
    # capture (comp, opt).
    apply_expr = (
        'm: builtins.mapAttrs (arch: suffixes:\n'
        '  builtins.filter (s: builtins.match '
        '".*-(O0|O2)-baseline-default-san-off-march-default" s != null)\n'
        '  (builtins.attrNames suffixes)) m'
    )
    proc = subprocess.run(
        [
            "nix", "eval", "--json", f".#_meta.{SYS}.{binary}",
            "--apply", apply_expr,
            "--extra-experimental-features", "flakes",
        ],
        capture_output=True, check=True,
    )
    raw = json.loads(proc.stdout.decode("utf-8"))
    valid: set[tuple[str, str, str]] = set()
    for arch, sfxs in raw.items():
        for s in sfxs:
            # "<comp>-<opt>-..." — opt is "O0" or "O2".
            m = re.match(r"^(.+?)-(O[02])-", s)
            if m:
                valid.add((arch, m.group(1), m.group(2)))
    return valid


def probe_one(args: tuple[str, str, str, str]) -> tuple[tuple, bool]:
    binary, arch, comp, opt = args
    suffix = SUFFIX.format(comp=comp, opt=opt)
    attr = (
        f'.#dataset.{SYS}.{binary}.{arch}."{suffix}".drvPath'
    )
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--raw", attr],
        capture_output=True, check=False,
    )
    ok = (
        proc.returncode == 0
        and proc.stdout.decode("utf-8", "replace").strip()
            .endswith(".drv")
    )
    return ((binary, arch, comp, opt), ok)


def main() -> int:
    binaries = pick_two_per_category()
    pairs = toolchain_pairs()
    sys.stderr.write(
        f"# {len(binaries)} binaries × {len(pairs)} (arch,comp) × "
        f"{len(OPTS)} opts (full cartesian = "
        f"{len(binaries) * len(pairs) * len(OPTS)} attrs)\n"
        "# pre-filtering via _meta (per binary)...\n"
    )
    # Per-binary, ask the matrix which (arch, comp, opt) tuples it
    # actually exposes. Eliminates the "flake does not provide
    # attribute" probe class (~25% of the cartesian product).
    by_binary_valid: dict[str, set[tuple[str, str, str]]] = {}
    for b in binaries:
        try:
            by_binary_valid[b] = valid_meta_combos(b)
            sys.stderr.write(
                f"#   {b}: {len(by_binary_valid[b])} valid combos\n"
            )
        except subprocess.CalledProcessError as e:
            sys.stderr.write(
                f"#   {b}: _meta query failed; skipping binary\n"
                f"#     stderr: {e.stderr.decode('utf-8', 'replace')[:200]}\n"
            )
            by_binary_valid[b] = set()
    tuples = [
        (b, a, c, o)
        for b in binaries
        for (a, c, o) in by_binary_valid.get(b, set())
    ]
    sys.stderr.write(f"# after _meta filter: {len(tuples)} attrs to probe\n")

    cache: dict[str, bool] = {}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())
        sys.stderr.write(f"# loaded cache: {len(cache)} entries\n")

    def key(t):
        return "/".join(t)

    todo = [t for t in tuples if key(t) not in cache]
    sys.stderr.write(
        f"# {len(todo)} attrs to probe ({len(tuples) - len(todo)} cached)\n"
    )

    n_done = 0
    last_flush = 0
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for tup, ok in ex.map(probe_one, todo, chunksize=8):
            cache[key(tup)] = ok
            n_done += 1
            if n_done % 200 == 0:
                pct = n_done * 100 // len(todo) if todo else 100
                sys.stderr.write(f"# probed {n_done}/{len(todo)} ({pct}%)\n")
            # Flush cache every 500 probes so an interrupt doesn't lose work.
            if n_done - last_flush >= 500:
                CACHE_FILE.write_text(json.dumps(cache))
                last_flush = n_done

    CACHE_FILE.write_text(json.dumps(cache))

    n_pass = sum(1 for v in cache.values() if v)
    n_fail = sum(1 for v in cache.values() if not v)
    sys.stderr.write(f"# probe done: {n_pass} pass / {n_fail} fail\n")

    # Build attr lists from the passing combos.
    toolchain_attrs = [
        f'outputs._crossToolchainMap.{SYS}.{a}."{c}"' for a, c in pairs
    ]
    matrices: dict[str, list[str]] = {}
    for b in binaries:
        attrs = []
        for a, c in pairs:
            for o in OPTS:
                if cache.get(key((b, a, c, o))):
                    sfx = SUFFIX.format(comp=c, opt=o)
                    attrs.append(
                        f'outputs.dataset.{SYS}.{b}.{a}."{sfx}"'
                    )
        if attrs:
            matrices[f"matrix-{b}"] = attrs
        else:
            sys.stderr.write(f"# WARNING: {b} has zero passing variants\n")
    sys.stderr.write(
        f"# building sum drv: {sum(len(v) for v in matrices.values())} variants "
        f"across {len(matrices)} matrices\n"
    )

    drv = make_sum_drv(
        flake_ref=f"git+file://{PROJECT_ROOT}",
        bash_attr=f"inputs.nixpkgs.legacyPackages.{SYS}.bash",
        toolchain_attrs=toolchain_attrs,
        matrices=matrices,
        root_name="sum-root",
        toolchains_name="toolchains",
        system=SYS,
        tolerate_missing=True,
    )

    OUT_FILE.write_text(
        f"# Generated by template_graph/tests/fixtures/generate_full_filtered.py\n"
        f"# Full matrix probed; {n_pass}/{len(tuples)} attrs included.\n"
        f"# {len(binaries)} binaries: {', '.join(binaries)}\n"
        f"# {len(pairs)} (arch, compiler) pairs across {len(set(a for a, _ in pairs))} archs.\n"
        f"# 2 opts: {', '.join(OPTS)}.\n"
        f"{drv}\n"
    )
    sys.stderr.write(f"# wrote {OUT_FILE}\n")
    print(drv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

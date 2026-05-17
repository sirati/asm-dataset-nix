#!/usr/bin/env python3
"""Probe drvPath eval for dcraw / quickjs / wasm3.

These three packages are marked insecure upstream; once
`permittedInsecurePackages` allows them, they should evaluate just
like any other matrix entry. Cache goes to a SEPARATE file so the
existing `.probe_cache_remaining.json` is preserved.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYS = "x86_64-linux"
OPT = "O0"
SUFFIX = "{comp}-{opt}-baseline-default-san-off-march-default"
WORKERS = 16
BINS = ["dcraw", "quickjs", "wasm3"]

FIX_DIR = Path(__file__).parent
OUT_CACHE = FIX_DIR / ".probe_cache_insecure.json"
SUMMARY_FILE = FIX_DIR / "insecure_probe_summary.txt"


def toolchain_pairs() -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--json",
         f".#_crossToolchainMap.{SYS}",
         "--apply",
         "m: builtins.mapAttrs (a: v: builtins.attrNames v) m"],
        capture_output=True, check=True,
    )
    m = json.loads(proc.stdout.decode("utf-8"))
    return [(arch, comp) for arch in sorted(m) for comp in sorted(m[arch])]


def valid_meta_combos(binary: str) -> set[tuple[str, str]]:
    apply_expr = (
        f'm: builtins.mapAttrs (arch: suffixes:\n'
        f'  builtins.filter (s: builtins.match '
        f'".*-{OPT}-baseline-default-san-off-march-default" s != null)\n'
        f'  (builtins.attrNames suffixes)) m'
    )
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--json", f".#_meta.{SYS}.{binary}",
         "--apply", apply_expr],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return set()
    raw = json.loads(proc.stdout.decode("utf-8"))
    valid: set[tuple[str, str]] = set()
    for arch, sfxs in raw.items():
        for s in sfxs:
            m = re.match(rf"^(.+?)-{OPT}-", s)
            if m:
                valid.add((arch, m.group(1)))
    return valid


def probe_one(args):
    binary, arch, comp = args
    suffix = SUFFIX.format(comp=comp, opt=OPT)
    attr = f'.#dataset.{SYS}.{binary}.{arch}."{suffix}".drvPath'
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--raw", attr],
        capture_output=True, check=False,
    )
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    ok = proc.returncode == 0 and stdout.strip().endswith(".drv")
    err = ""
    if not ok:
        m = re.search(r"^\s*error: (.+?)$", stderr, re.M)
        err = (m.group(1).strip()[:200] if m else "unknown")
    return ((binary, arch, comp), ok, err)


def main() -> int:
    pairs = toolchain_pairs()
    sys.stderr.write(f"# {len(pairs)} (arch,comp) pairs total\n")
    by_bin = {b: valid_meta_combos(b) for b in BINS}
    tuples = [(b, a, c) for b in BINS for (a, c) in sorted(by_bin[b])]
    sys.stderr.write(f"# {len(tuples)} attrs to probe across {len(BINS)} bins\n")

    cache: dict[str, bool] = {}
    errs: dict[str, str] = {}
    n = 0
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for tup, ok, err in ex.map(probe_one, tuples, chunksize=4):
            k = f"{tup[0]}/{tup[1]}/{tup[2]}"
            cache[k] = ok
            if not ok and err:
                errs[k] = err
            n += 1
            if n % 200 == 0:
                sys.stderr.write(f"# probed {n}/{len(tuples)}\n")

    OUT_CACHE.write_text(json.dumps({"results": cache, "errors": errs}))
    n_pass = sum(1 for v in cache.values() if v)
    n_fail = sum(1 for v in cache.values() if not v)

    lines = [
        f"# insecure-binaries probe summary",
        f"# {n_pass} pass / {n_fail} fail",
        "",
    ]
    per_bin = {b: {"pass": 0, "fail": 0} for b in BINS}
    for k, v in cache.items():
        b = k.split("/", 1)[0]
        per_bin[b]["pass" if v else "fail"] += 1
    for b in BINS:
        c = per_bin[b]
        lines.append(f"{b:10s}  pass={c['pass']:4d}  fail={c['fail']:4d}")
    if errs:
        from collections import Counter
        lines.append("")
        lines.append("# top error classes:")
        for err, n in Counter(errs.values()).most_common(10):
            lines.append(f"  {n:4d}  {err}")
    SUMMARY_FILE.write_text("\n".join(lines) + "\n")
    sys.stderr.write(f"# wrote {OUT_CACHE} and {SUMMARY_FILE}\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

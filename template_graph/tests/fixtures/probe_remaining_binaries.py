#!/usr/bin/env python3
"""Probe drvPath eval for every binary NOT in the curated 28-bin fixture.

Reads:
- `_meta.x86_64-linux` to enumerate ALL 245 binaries the matrix exposes.
- ``.probe_cache.json`` (from generate_full_filtered.py) to identify
  which 28 binaries were already covered. Skips those.

For the remaining ~217 binaries:
- Probe across all 317 (arch, compiler) pairs, with ONE opt level
  (O0). Per-binary _meta pre-filter so we don't probe combos the
  matrix doesn't expose.

Output:
- ``.probe_cache_remaining.json`` — separate cache file; does NOT
  touch ``.probe_cache.json``.
- Stdout summary: pass/fail per binary + the top error classes.

Run from project root, in background::

    nohup python3 template_graph/tests/fixtures/probe_remaining_binaries.py &
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

SYS = "x86_64-linux"
OPT = "O0"
SUFFIX = "{comp}-{opt}-baseline-default-san-off-march-default"
WORKERS = 16

FIX_DIR = Path(__file__).parent
EXISTING_CACHE = FIX_DIR / ".probe_cache.json"
OUT_CACHE = FIX_DIR / ".probe_cache_remaining.json"
SUMMARY_FILE = FIX_DIR / "remaining_probe_summary.txt"


def all_binaries() -> list[str]:
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--json", f".#_meta.{SYS}",
         "--apply", "m: builtins.attrNames m"],
        capture_output=True, check=True,
    )
    return json.loads(proc.stdout.decode("utf-8"))


def already_probed_binaries(existing_cache_path: Path) -> set[str]:
    if not existing_cache_path.exists():
        return set()
    cache = json.loads(existing_cache_path.read_text())
    return {k.split("/", 1)[0] for k in cache.keys()}


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
    """Per-(binary, arch) valid (arch, comp) tuples for our suffix shape, opt=O0."""
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


def probe_one(args: tuple[str, str, str]) -> tuple[tuple, bool, str]:
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
    # Extract the most useful error line for classification.
    err_class = ""
    if not ok:
        m = re.search(r"^\s*error: (.+?)$", stderr, re.M)
        if m:
            err_class = m.group(1).strip()[:200]
        elif "function 'anonymous lambda' called with unexpected argument" in stderr:
            err_class = "stdenvNoCC (function doesn't take stdenv)"
    return ((binary, arch, comp), ok, err_class)


def main() -> int:
    bins_all = all_binaries()
    bins_done = already_probed_binaries(EXISTING_CACHE)
    bins_todo = sorted(set(bins_all) - bins_done)
    sys.stderr.write(
        f"# {len(bins_all)} total binaries; {len(bins_done)} already "
        f"probed → {len(bins_todo)} to probe\n"
    )

    pairs = toolchain_pairs()

    # Per-binary, query _meta to know which (arch, comp) combos actually exist.
    by_binary_valid: dict[str, set[tuple[str, str]]] = {}
    for i, b in enumerate(bins_todo, 1):
        by_binary_valid[b] = valid_meta_combos(b)
        if i % 20 == 0 or i == len(bins_todo):
            sys.stderr.write(
                f"# _meta filter {i}/{len(bins_todo)}\n"
            )

    cache: dict[str, bool] = {}
    cache_errors: dict[str, str] = {}
    if OUT_CACHE.exists():
        existing = json.loads(OUT_CACHE.read_text())
        cache = existing.get("results", existing if isinstance(existing, dict) else {})
        cache_errors = existing.get("errors", {}) if isinstance(existing, dict) and "results" in existing else {}
        sys.stderr.write(f"# resuming with {len(cache)} cached entries\n")

    tuples = [
        (b, a, c)
        for b in bins_todo
        for (a, c) in by_binary_valid[b]
        if f"{b}/{a}/{c}" not in cache
    ]
    sys.stderr.write(f"# probing {len(tuples)} attrs across {WORKERS} workers\n")

    key = lambda t: f"{t[0]}/{t[1]}/{t[2]}"
    n_done = 0
    last_flush = 0
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for tup, ok, err in ex.map(probe_one, tuples, chunksize=8):
            cache[key(tup)] = ok
            if not ok and err:
                cache_errors[key(tup)] = err
            n_done += 1
            if n_done % 500 == 0:
                pct = n_done * 100 // len(tuples) if tuples else 100
                sys.stderr.write(f"# probed {n_done}/{len(tuples)} ({pct}%)\n")
            if n_done - last_flush >= 1000:
                OUT_CACHE.write_text(json.dumps({"results": cache, "errors": cache_errors}))
                last_flush = n_done

    OUT_CACHE.write_text(json.dumps({"results": cache, "errors": cache_errors}))
    sys.stderr.write(
        f"# done: cached {len(cache)} entries; "
        f"pass={sum(1 for v in cache.values() if v)} "
        f"fail={sum(1 for v in cache.values() if not v)}\n"
    )

    # Per-binary summary.
    by_bin: dict[str, dict[str, int]] = {}
    for k, v in cache.items():
        b = k.split("/", 1)[0]
        by_bin.setdefault(b, {"pass": 0, "fail": 0})
        by_bin[b]["pass" if v else "fail"] += 1

    err_classes = Counter(cache_errors.values())

    lines = [
        f"# probe_remaining_binaries.py summary",
        f"# {len(bins_todo)} binaries probed (excluding the 28 already covered).",
        f"# opt level: {OPT}",
        f"# {sum(1 for v in cache.values() if v)} pass / {sum(1 for v in cache.values() if not v)} fail",
        "",
        f"# === per-binary (sorted by pass count) ===",
    ]
    for b, c in sorted(by_bin.items(), key=lambda x: -x[1]["pass"]):
        lines.append(f"{b:30s}  pass={c['pass']:4d}  fail={c['fail']:4d}")
    lines.append("")
    lines.append(f"# === top error classes ===")
    for err, n in err_classes.most_common(20):
        lines.append(f"  {n:4d}  {err}")
    SUMMARY_FILE.write_text("\n".join(lines) + "\n")
    sys.stderr.write(f"# wrote {SUMMARY_FILE}\n")
    print("\n".join(lines[:6]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

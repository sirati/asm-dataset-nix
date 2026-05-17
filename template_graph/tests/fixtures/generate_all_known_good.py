#!/usr/bin/env python3
"""Build a sum-drv containing every (binary, arch, compiler, opt) cell
that is known-good across the three probe caches, plus re-probe the
previously-failed cells the matrix still surfaces.

Inputs (read-only):
    .probe_cache.json            — first batch, 28 binaries × (O0,O2)
    .probe_cache_remaining.json  — remaining 217 binaries × O0 only
    .probe_cache_insecure.json   — 3 insecure binaries × O0 only

The cache key shapes differ:
    binary/arch/comp/opt   (first batch — 4 segments)
    binary/arch/comp       (remaining + insecure — 3 segments; opt fixed to O0)

Output:
    all_known_good_sum_drv.txt   — generated drv path + manifest header
    .probe_cache_combined.json   — re-probe results merged with originals

Run from project root::

    python3 template_graph/tests/fixtures/generate_all_known_good.py
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from template_graph.make_sum_drv import make_sum_drv  # noqa: E402

SYS = "x86_64-linux"
SUFFIX = "{comp}-{opt}-baseline-default-san-off-march-default"
SUFFIX_RE = re.compile(r"^(.+?)-(O[0-9])-baseline-default-san-off-march-default$")
WORKERS = 16

FIX_DIR = Path(__file__).parent
CACHE_FIRST = FIX_DIR / ".probe_cache.json"
CACHE_REMAINING = FIX_DIR / ".probe_cache_remaining.json"
CACHE_INSECURE = FIX_DIR / ".probe_cache_insecure.json"
OUT_COMBINED = FIX_DIR / ".probe_cache_combined.json"
OUT_FILE = FIX_DIR / "all_known_good_sum_drv.txt"


def _unwrap(cache: dict) -> dict:
    return cache.get("results", cache) if isinstance(cache, dict) else {}


def _normalize_key(key: str) -> tuple[str, str, str, str]:
    """Return canonical (binary, arch, comp, opt) — assume O0 when missing."""
    parts = key.split("/")
    if len(parts) == 4:
        return tuple(parts)  # type: ignore[return-value]
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2], "O0")
    raise ValueError(f"unrecognised cache key shape: {key!r}")


def load_all_caches() -> dict[tuple[str, str, str, str], bool]:
    out: dict[tuple[str, str, str, str], bool] = {}
    for path in (CACHE_FIRST, CACHE_REMAINING, CACHE_INSECURE):
        if not path.exists():
            sys.stderr.write(f"# missing cache: {path}\n")
            continue
        raw = json.loads(path.read_text())
        results = _unwrap(raw)
        for k, v in results.items():
            out[_normalize_key(k)] = bool(v)
        sys.stderr.write(f"# loaded {len(results)} entries from {path.name}\n")
    return out


def valid_meta_combos(binary: str) -> set[tuple[str, str, str]]:
    """Per-binary set of (arch, comp, opt) the matrix currently surfaces.

    Filters on suffix shape ``*-(O0|O2)-baseline-default-san-off-march-default``;
    if you grow this script to other opts, widen the regex.
    """
    apply_expr = (
        "m: builtins.mapAttrs (arch: suffixes:\n"
        "  builtins.filter (s: builtins.match "
        '".*-(O0|O2)-baseline-default-san-off-march-default" s != null)\n'
        "  (builtins.attrNames suffixes)) m"
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
    out: set[tuple[str, str, str]] = set()
    for arch, sfxs in raw.items():
        for s in sfxs:
            m = SUFFIX_RE.match(s)
            if m:
                out.add((arch, m.group(1), m.group(2)))
    return out


def probe_one(args: tuple[str, str, str, str]) -> tuple[tuple, bool, str]:
    binary, arch, comp, opt = args
    suffix = SUFFIX.format(comp=comp, opt=opt)
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
    return ((binary, arch, comp, opt), ok, err)


def toolchain_pairs() -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["nix", "eval", "--extra-experimental-features", "flakes",
         "--impure", "--json", f".#_crossToolchainMap.{SYS}",
         "--apply",
         "m: builtins.mapAttrs (a: v: builtins.attrNames v) m"],
        capture_output=True, check=True,
    )
    m = json.loads(proc.stdout.decode("utf-8"))
    return [(arch, comp) for arch in sorted(m) for comp in sorted(m[arch])]


def main() -> int:
    combined = load_all_caches()
    sys.stderr.write(f"# {len(combined)} total cache entries loaded\n")

    binaries = sorted({k[0] for k in combined})
    sys.stderr.write(f"# {len(binaries)} distinct binaries in caches\n")

    # Per-binary _meta filter (single nix eval per binary, run in parallel)
    sys.stderr.write(
        f"# computing per-binary _meta gate ({len(binaries)} binaries, "
        f"{WORKERS} workers)...\n"
    )
    by_binary_valid: dict[str, set[tuple[str, str, str]]] = {}
    done = 0
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for b, combos in zip(
            binaries, ex.map(valid_meta_combos, binaries, chunksize=4)
        ):
            by_binary_valid[b] = combos
            done += 1
            if done % 20 == 0 or done == len(binaries):
                sys.stderr.write(f"#   _meta {done}/{len(binaries)}\n")

    # Re-probe candidates: cached as False AND still in the matrix.
    todo: list[tuple[str, str, str, str]] = []
    for key, ok in combined.items():
        if ok:
            continue
        b, a, c, o = key
        if (a, c, o) in by_binary_valid.get(b, set()):
            todo.append(key)
    sys.stderr.write(
        f"# {len(todo)} previously-failed entries still in matrix — re-probing\n"
    )

    if todo:
        n_done = 0
        recovered = 0
        with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
            for key, ok, _err in ex.map(probe_one, todo, chunksize=8):
                if ok and not combined[key]:
                    recovered += 1
                combined[key] = ok
                n_done += 1
                if n_done % 200 == 0:
                    pct = n_done * 100 // len(todo)
                    sys.stderr.write(
                        f"#   reprobe {n_done}/{len(todo)} ({pct}%) — "
                        f"recovered={recovered}\n"
                    )
        sys.stderr.write(
            f"# reprobe done: {recovered} previously-failing entries now pass\n"
        )

    OUT_COMBINED.write_text(json.dumps({
        "results": {f"{b}/{a}/{c}/{o}": v for (b, a, c, o), v in combined.items()},
    }))
    sys.stderr.write(f"# wrote {OUT_COMBINED}\n")

    pairs = toolchain_pairs()
    toolchain_attrs = [
        f'outputs._crossToolchainMap.{SYS}.{a}."{c}"' for a, c in pairs
    ]

    # Group passing cells per binary; drop binaries with zero passing cells.
    by_binary: dict[str, list[str]] = defaultdict(list)
    for (b, a, c, o), ok in combined.items():
        if not ok:
            continue
        # Defense in depth: only emit if the cell is currently in the matrix.
        if (a, c, o) not in by_binary_valid.get(b, set()):
            continue
        sfx = SUFFIX.format(comp=c, opt=o)
        by_binary[b].append(f'outputs.dataset.{SYS}.{b}.{a}."{sfx}"')

    matrices: dict[str, list[str]] = {
        f"matrix-{b}": attrs for b, attrs in sorted(by_binary.items()) if attrs
    }
    n_total = sum(len(v) for v in matrices.values())
    sys.stderr.write(
        f"# sum drv: {n_total} variants across {len(matrices)} binaries\n"
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
        f"# Generated by template_graph/tests/fixtures/generate_all_known_good.py\n"
        f"# {n_total} variants across {len(matrices)} binaries.\n"
        f"# {len(pairs)} (arch, compiler) pairs.\n"
        f"# Sources: first batch (O0+O2), remaining (O0), insecure (O0); "
        f"failures re-probed against current matrix.\n"
        f"{drv}\n"
    )
    sys.stderr.write(f"# wrote {OUT_FILE}\n")
    print(drv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

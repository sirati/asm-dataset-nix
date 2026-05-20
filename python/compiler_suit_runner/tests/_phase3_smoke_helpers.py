"""Nix-introspection helpers for the Phase-3 live-matrix smoke test.

Extracted out of ``test_phase3_smoke.py`` to keep the test module
within the 300-LOC cap. Only imported by ``test_phase3_smoke.py``;
no production callers.

Drives ``nix eval --json`` against the worktree flake to discover
compilers / archs / (arch, comp) pairs, composes flake-attr lists for
the matrix and toolchain map, assembles the sum-drv via
``template_graph.make_sum_drv``, walks the ``nix-store --query --tree``
output to build the variant lookup, and runs the streaming planner +
``plan_phase4_for_binary``. The assertion contract lives in the
test module so semantics stay co-located with the test function.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


# Configuration knobs (kept here so the test module just references them).
# _meta decides which suffixes exist per arch; no local guesswork.
DEFAULT_SMOKE_ARCHS: tuple[str, ...] = ("x86_64", "aarch64")
SYS_NAME = "x86_64-linux"
BINARY = "hello"
FLAKE_BASH_ATTR = f"inputs.nixpkgs.legacyPackages.{SYS_NAME}.bash"
SUFFIX_MATCH_PATTERN = (
    ".*-(baseline-default|noinline-default)-san-off-march-default"
)
STORE_HASH_RE = re.compile(r"^/nix/store/[^-]+-")


# --- nix eval / nix-store wrappers -----------------------------------------


def _nix_eval_json(attr: str, *, root: Path, apply: str | None = None) -> object:
    """Single ``nix eval --json`` against the worktree flake."""
    argv: list[str] = [
        "nix", "eval",
        "--extra-experimental-features", "nix-command flakes",
        "--json",
        f"{root}#{attr}",
    ]
    if apply is not None:
        argv += ["--apply", apply]
    proc = subprocess.run(argv, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix eval --json {attr} failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return json.loads(proc.stdout.decode("utf-8", errors="replace"))


def query_tree(sum_drv: str) -> str:
    """``nix-store --query --tree <sum_drv>`` → decoded text."""
    proc = subprocess.run(
        ["nix-store", "--query", "--tree", sum_drv],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix-store --query --tree failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return proc.stdout.decode("utf-8", errors="replace")


# --- Discovery -------------------------------------------------------------


def resolved_smoke_archs(discovered: list[str]) -> list[str]:
    """``ASM_PHASE3_SMOKE_ARCHS`` resolver. ``all`` → every discovered
    arch; comma-list → intersection with ``discovered``; unset →
    ``DEFAULT_SMOKE_ARCHS``."""
    raw = os.environ.get("ASM_PHASE3_SMOKE_ARCHS", "").strip()
    if not raw:
        return [a for a in DEFAULT_SMOKE_ARCHS if a in discovered]
    if raw == "all":
        return list(discovered)
    wanted = [tok.strip() for tok in raw.split(",") if tok.strip()]
    return [a for a in wanted if a in discovered]


def discover_compilers(root: Path) -> list[str]:
    """``_debug.compilers`` → flat sorted list of compiler labels."""
    payload = _nix_eval_json(f"_debug.{SYS_NAME}.compilers", root=root)
    assert isinstance(payload, dict)
    return sorted({
        entry["label"]
        for family in ("gcc", "clang")
        for entry in payload.get(family, [])
        if isinstance(entry, dict) and isinstance(entry.get("label"), str)
    })


def discover_archs(root: Path) -> list[str]:
    """``_debug.targets`` → list of arch labels."""
    raw = _nix_eval_json(f"_debug.{SYS_NAME}.targets", root=root)
    assert isinstance(raw, list) and raw
    return [a for a in raw if isinstance(a, str)]


def discover_compilers_per_arch(root: Path) -> dict[str, list[str]]:
    """``_crossToolchainsMeta`` → ``{arch: [comp_label, ...]}`` (every
    (arch, compiler) pair the matrix considers valid)."""
    raw = _nix_eval_json(f"_crossToolchainsMeta.{SYS_NAME}", root=root)
    assert isinstance(raw, dict)
    return {
        arch: sorted({
            entry["compiler"]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("compiler"), str)
        })
        for arch, entries in raw.items()
        if isinstance(entries, list)
    }


def _enumerate_valid_suffixes_for_arch(
    *, arch: str, root: Path,
) -> list[str]:
    """``_meta.<sys>.<binary>.<arch>`` → suffixes matching the inner-axis
    pattern (string list — ``--apply`` keeps JSON small)."""
    apply_expr = (
        f'm: builtins.filter '
        f'(s: builtins.match "{SUFFIX_MATCH_PATTERN}" s != null) '
        f'(builtins.attrNames m)'
    )
    raw = _nix_eval_json(
        f"_meta.{SYS_NAME}.{BINARY}.{arch}",
        root=root, apply=apply_expr,
    )
    if not isinstance(raw, list):
        raise RuntimeError(
            f"_meta enumeration for {arch} returned non-list: "
            f"{type(raw).__name__}"
        )
    return [s for s in raw if isinstance(s, str)]


# --- Attr-list composition -------------------------------------------------


def matrix_attrs_for(smoke_archs: list[str], root: Path) -> list[str]:
    """Compose ``dataset.<sys>.<binary>.<arch>.<suffix>`` attrs for every
    arch in ``smoke_archs``, sourcing the valid suffix list from ``_meta``."""
    out: list[str] = []
    for arch in smoke_archs:
        for suffix in _enumerate_valid_suffixes_for_arch(arch=arch, root=root):
            out.append(f"dataset.{SYS_NAME}.{BINARY}.{arch}.{suffix}")
    return out


def toolchain_attrs_for(
    smoke_archs: list[str], compilers_per_arch: dict[str, list[str]],
) -> list[str]:
    """``_crossToolchainMap.<sys>.<arch>.<comp>`` attrs for every valid
    (arch, compiler) pair in the smoke archs."""
    return [
        f"_crossToolchainMap.{SYS_NAME}.{arch}.{comp}"
        for arch in smoke_archs
        for comp in compilers_per_arch.get(arch, [])
    ]


def toolchain_task_ids_for_combos(
    compilers_per_arch: dict[str, list[str]],
) -> dict[str, str]:
    """``ident -> build_compilers__*`` map. ``plan_cell._variant_toolchain_dep``
    only reads ``set(values)``, so synthetic idents are fine here."""
    return {
        f"__synth__{arch}__{comp}":
            f"build_compilers__{SYS_NAME}__{arch}__{comp}"
        for arch, comps in compilers_per_arch.items()
        for comp in comps
    }


# --- Tree walking — variant lookup -----------------------------------------


def extract_store_path(raw_line: str, variant_suffix: str) -> str | None:
    """Return the ``/nix/store/...`` segment of ``raw_line`` iff the line
    is a matrix-depth2 variant root, else ``None``."""
    stripped = raw_line.rstrip()
    if not stripped.endswith(variant_suffix):
        return None
    if stripped.endswith(" [...]"):
        stripped = stripped[: -len(" [...]")]
        if not stripped.endswith(variant_suffix):
            return None
    store_idx = stripped.rfind("/nix/store/")
    if store_idx < 0:
        return None
    return stripped[store_idx:]


def build_variant_lookup(tree_text: str) -> dict[tuple[str, str], dict]:
    """Scan the tree text for matrix-depth2 entries; compose
    ``(arch, label) -> spec`` the way streaming's ``cur_label`` does.
    Mirrors ``archive.derive_variant_lookup_from_drvs`` but reads the
    tree directly so the test doesn't need a ``nix-store --import``."""
    from template_graph.tree_walker import (  # noqa: PLC0415
        VARIANT_SUFFIX, parse_variant_path,
    )
    lookup: dict[tuple[str, str], dict] = {}
    for raw_line in tree_text.splitlines():
        store_path = extract_store_path(raw_line, VARIANT_SUFFIX)
        if store_path is None:
            continue
        basename = STORE_HASH_RE.sub("", store_path)
        if basename == store_path:
            continue
        try:
            binary, arch, comp, opt = parse_variant_path(basename)
        except Exception:
            continue
        if binary != BINARY:
            continue
        head_len = len(binary) + 1 + len(arch) + 1
        suffix = basename[head_len : -len(VARIANT_SUFFIX)]
        label = f"{binary}__{arch}__{suffix}"
        lookup[(arch, label)] = {
            "drv": store_path,
            "arch": arch,
            "label": label,
            "suffix": suffix,
            "compiler_id": comp,
            "optimization": opt,
            "pkg": BINARY,
        }
    return lookup


# --- Sum-drv + planner stages ----------------------------------------------


def build_sum_drv(
    *,
    root: Path,
    smoke_archs: list[str],
    compilers_per_arch: dict[str, list[str]],
) -> str:
    """Stage 1: build flake-attr lists + assemble the sum-drv via the
    flake-form ``make_sum_drv``. Returns the sum-root drv path."""
    from template_graph.make_sum_drv import make_sum_drv  # noqa: PLC0415

    matrix_attrs = matrix_attrs_for(smoke_archs, root)
    toolchain_attrs = toolchain_attrs_for(smoke_archs, compilers_per_arch)
    assert matrix_attrs, "matrix_attrs empty after smoke-arch filtering"
    assert toolchain_attrs, "toolchain_attrs empty — no valid (arch, comp)"

    sum_drv = make_sum_drv(
        flake_ref=f"git+file://{root}",
        bash_attr=FLAKE_BASH_ATTR,
        toolchain_attrs=toolchain_attrs,
        matrices={f"matrix-{BINARY}": matrix_attrs},
        root_name="phase3-smoke-sum-root",
        toolchains_name="toolchains",
        system=SYS_NAME,
        tolerate_missing=False,
    )
    assert sum_drv.endswith(".drv"), f"unexpected sum-drv path: {sum_drv}"
    return sum_drv


def plan_from_tree(
    tree_text: str,
    *,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
):
    """Stage 2: stream-parse + plan. Returns ``(descriptors, streaming_result)``."""
    from template_graph.streaming import (  # noqa: PLC0415
        plan_from_tree_streaming,
    )
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        plan_phase4_for_binary,
    )

    streaming_result = plan_from_tree_streaming(tree_text, lax=True)
    descriptors = plan_phase4_for_binary(
        BINARY,
        streaming_result,
        variant_lookup,
        sys_name=SYS_NAME,
        toolchain_task_ids=toolchain_task_ids,
    )
    return descriptors, streaming_result

"""Nix-introspection helpers for the Phase-3 live-matrix smoke test.

Mirrors the production code path end-to-end:

* phase 1: ``toolchains`` aggregate via
  :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths` from
  toolchain leaf drvs discovered by ONE
  ``nix-eval-jobs --force-recurse`` over ``_crossToolchainMap.<sys>``;
* phase 2: ``matrix-<binary>`` aggregate the same way from variant
  leaf drvs discovered by ONE ``nix-eval-jobs --select`` over
  ``dataset.<sys>.<binary>``;
* phase 3: :func:`make_sum_drv_from_paths` over the pre-built
  aggregates, then ``nix-store --query --tree`` + streaming planner.

ZERO per-leaf ``nix eval --apply`` calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from compiler_suit_runner.workers.eval_worker import sample_suffix_attrs


DEFAULT_SMOKE_ARCHS: tuple[str, ...] = ("x86_64", "aarch64")
SYS_NAME = "x86_64-linux"
BINARY = "hello"
FLAKE_BASH_ATTR = f"inputs.nixpkgs.legacyPackages.{SYS_NAME}.bash"
SUFFIX_MATCH_PATTERN = (
    ".*-(baseline-default|noinline-default)-san-off-march-default"
)
STORE_HASH_RE = re.compile(r"^/nix/store/[^-]+-")
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def _nix_eval_json(attr: str, *, root: Path, apply: str | None = None) -> object:
    """One ``nix eval --json``; tiny scalar/list reads only."""
    argv = [
        "nix", "eval",
        "--extra-experimental-features", "nix-command flakes",
        "--json", f"{root}#{attr}",
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


def _run_jobs(argv: list[str]) -> list[dict]:
    proc = subprocess.run(argv, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix-eval-jobs failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    out: list[dict] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _validate(tok: str, *, kind: str) -> None:
    if not _SAFE.match(tok):
        raise RuntimeError(f"phase3-smoke: unsafe {kind} {tok!r}")


def _depth2(entries: list[dict], *, archs: set[str] | None = None):
    """Yield ``(a, b, drv)`` from depth-2 JSONL leaves."""
    for entry in entries:
        ap = entry.get("attrPath")
        drv = entry.get("drvPath")
        if (
            isinstance(ap, list) and len(ap) == 2
            and isinstance(ap[0], str) and isinstance(ap[1], str)
            and isinstance(drv, str) and drv.endswith(".drv")
            and (archs is None or ap[0] in archs)
        ):
            yield ap[0], ap[1], drv


def resolved_smoke_archs(discovered: list[str]) -> list[str]:
    """Resolve ``ASM_PHASE3_SMOKE_ARCHS`` against the discovered archs."""
    raw = os.environ.get("ASM_PHASE3_SMOKE_ARCHS", "").strip()
    if not raw:
        return [a for a in DEFAULT_SMOKE_ARCHS if a in discovered]
    if raw == "all":
        return list(discovered)
    wanted = [t.strip() for t in raw.split(",") if t.strip()]
    return [a for a in wanted if a in discovered]


def discover_archs(*, root: Path) -> list[str]:
    """``_debug.targets`` → list of arch labels."""
    raw = _nix_eval_json(f"_debug.{SYS_NAME}.targets", root=root)
    assert isinstance(raw, list) and raw
    return [a for a in raw if isinstance(a, str)]


def eval_bash_drv_path(root: Path) -> str:
    """ONE nix-eval --json for bash's store outPath."""
    payload = _nix_eval_json(FLAKE_BASH_ATTR, root=root)
    assert isinstance(payload, str) and payload.startswith("/nix/store/")
    return payload


def _eval_toolchain_pairs(*, root: Path, archs: list[str]):
    """ONE ``nix-eval-jobs --force-recurse`` walk; (arch, comp, drv) list."""
    for arch in archs:
        _validate(arch, kind="arch")
    entries = _run_jobs([
        "nix-eval-jobs",
        "--flake", f"{root}#_crossToolchainMap.{SYS_NAME}",
        "--force-recurse", "--workers", "8",
    ])
    return list(_depth2(entries, archs=set(archs)))


def eval_toolchain_leaves(*, root: Path, archs: list[str]) -> list[str]:
    return sorted({d for _a, _c, d in _eval_toolchain_pairs(root=root, archs=archs)})


def discover_compilers_per_arch_from_leaves(
    *, root: Path, archs: list[str],
) -> dict[str, list[str]]:
    """``{arch: [comp, ...]}`` from the same bulk eval-jobs walk."""
    per: dict[str, set[str]] = {a: set() for a in archs}
    for arch, comp, _drv in _eval_toolchain_pairs(root=root, archs=archs):
        per[arch].add(comp)
    return {arch: sorted(per[arch]) for arch in archs}


def _meta_suffixes_for_arch(*, arch: str, root: Path) -> dict[str, dict]:
    apply_expr = (
        f'm: builtins.listToAttrs (builtins.map '
        f'(s: {{ name = s; value = m.${{s}}; }}) '
        f'(builtins.filter '
        f'(s: builtins.match "{SUFFIX_MATCH_PATTERN}" s != null) '
        f'(builtins.attrNames m)))'
    )
    raw = _nix_eval_json(
        f"_meta.{SYS_NAME}.{BINARY}.{arch}", root=root, apply=apply_expr,
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"_meta {arch} returned non-dict: {type(raw).__name__}")
    return raw


def eval_sampled_matrix_leaves(
    *,
    root: Path,
    binary: str,
    smoke_archs: list[str],
    compilers_per_arch: dict[str, list[str]],  # noqa: ARG001 — API symmetry
    sample_size: int,
    sample_seed: str,
) -> list[str]:
    """ONE bulk ``nix-eval-jobs --select`` over ``dataset.<sys>.<binary>``."""
    for arch in smoke_archs:
        _validate(arch, kind="arch")
    sampled_by_arch: dict[str, list[str]] = {}
    for arch in smoke_archs:
        meta = _meta_suffixes_for_arch(arch=arch, root=root)
        chosen = sample_suffix_attrs(
            meta, arch=arch, sample_size=sample_size, seed=sample_seed,
        )
        if chosen:
            sampled_by_arch[arch] = sorted(chosen.keys())
    if not sampled_by_arch:
        return []
    for suffixes in sampled_by_arch.values():
        for s in suffixes:
            _validate(s, kind="suffix")
    arch_blocks = [
        f'"{a}" = {{ ' + " ".join(f'"{s}" = null;' for s in sampled_by_arch[a]) + ' };'
        for a in sorted(sampled_by_arch)
    ]
    select_expr = (
        f"m: let filter = {{ {' '.join(arch_blocks)} }}; in "
        "builtins.mapAttrs (a: v: builtins.intersectAttrs filter.${a} v) "
        "(builtins.intersectAttrs filter m)"
    )
    entries = _run_jobs([
        "nix-eval-jobs",
        "--flake", f"{root}#dataset.{SYS_NAME}.{binary}",
        "--select", select_expr,
        "--force-recurse", "--workers", "8",
    ])
    return sorted({d for _a, _s, d in _depth2(entries)})


def build_toolchain_aggregate(leaves: list[str], *, sys_name: str) -> str:
    """Phase-1 mirror: single ``toolchains`` wrapper drv."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    return make_wrapper_drv_from_paths(
        drvs=sorted(leaves), name="toolchains", system=sys_name,
    )


def build_matrix_aggregate(
    toolchain_agg: str, leaves: list[str], *, binary: str, sys_name: str,
) -> str:
    """Phase-2 mirror: ``matrix-<binary>`` wrapper drv (toolchain + leaves)."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    return make_wrapper_drv_from_paths(
        drvs=[toolchain_agg, *sorted(leaves)],
        name=f"matrix-{binary}", system=sys_name,
    )


def build_sum_drv_from_aggregates(
    toolchain_agg: str,
    matrix_aggs: dict[str, str],
    *,
    bash_path: str,
    sys_name: str,
) -> str:
    """Phase-3 mirror: ONE ``make_sum_drv_from_paths`` call."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_sum_drv_from_paths,
    )
    return make_sum_drv_from_paths(
        bash_path=bash_path,
        toolchain_drvs=[toolchain_agg],
        matrix_drvs={f"matrix-{b}": [agg] for b, agg in matrix_aggs.items()},
        system=sys_name,
    )


def toolchain_task_ids_for_combos(
    compilers_per_arch: dict[str, list[str]],
) -> dict[str, str]:
    """``ident -> build_compilers__*`` map (synthetic ident keys)."""
    return {
        f"__synth__{arch}__{comp}":
            f"build_compilers__{SYS_NAME}__{arch}__{comp}"
        for arch, comps in compilers_per_arch.items()
        for comp in comps
    }


def extract_store_path(raw_line: str, variant_suffix: str) -> str | None:
    """``/nix/store/...`` segment iff the line is a matrix-depth2 root."""
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
    """``(arch, label) -> spec`` mirroring streaming's ``cur_label``."""
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
            "drv": store_path, "arch": arch, "label": label,
            "suffix": suffix, "compiler_id": comp,
            "optimization": opt, "pkg": BINARY,
        }
    return lookup


def plan_from_tree(
    tree_text: str,
    *,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
):
    """Stream-parse + plan. Returns ``(descriptors, streaming_result)``."""
    from template_graph.streaming import (  # noqa: PLC0415
        plan_from_tree_streaming,
    )
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        plan_phase4_for_binary,
    )
    streaming_result = plan_from_tree_streaming(tree_text, lax=True)
    descriptors = plan_phase4_for_binary(
        BINARY, streaming_result, variant_lookup,
        sys_name=SYS_NAME, toolchain_task_ids=toolchain_task_ids,
    )
    return descriptors, streaming_result

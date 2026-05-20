"""Nix-introspection helpers for the Phase-3 live-matrix smoke test.

Extracted out of ``test_phase3_smoke.py`` to keep the test module
within the 300-LOC cap. Only imported by ``test_phase3_smoke.py``
and by ad-hoc dot-generation scripts; no production callers.

**Design rule (load-bearing):** every nix evaluation done by these
helpers MUST come from a single bulk evaluator pass —
``nix-eval-jobs`` over an attr subtree, or ``nix eval --json``
over an attr that already returns the entire result. Per-leaf or
per-(arch, suffix) flake-attr evaluation is forbidden because it
re-evaluates the flake closure per leaf and runs in minutes on
real matrices instead of seconds.

The flake-form ``template_graph.make_sum_drv.make_sum_drv`` (which
takes flake-attr paths and re-evaluates the entire closure per
leaf) MUST NOT be imported here — only the path-form
``make_sum_drv_from_paths``, which uses ``builtins.appendContext``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


# Configuration knobs.
DEFAULT_SMOKE_ARCHS: tuple[str, ...] = ("x86_64", "aarch64")
SYS_NAME = "x86_64-linux"
BINARY = "hello"
SUFFIX_MATCH_PATTERN = (
    ".*-(baseline-default|noinline-default)-san-off-march-default"
)
STORE_HASH_RE = re.compile(r"^/nix/store/[^-]+-")


# --- nix process wrappers --------------------------------------------------


def _run(argv: list[str], *, err_label: str) -> bytes:
    proc = subprocess.run(argv, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{err_label} failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()[:800]
        )
    return proc.stdout


def query_tree(sum_drv: str) -> str:
    """``nix-store --query --tree <sum_drv>`` → decoded text. One nix call."""
    return _run(
        ["nix-store", "--query", "--tree", sum_drv],
        err_label="nix-store --query --tree",
    ).decode("utf-8", errors="replace")


# --- bulk evaluators (the ONLY way to source drv paths) --------------------


def _nix_eval_jobs(
    *, root: Path, attr: str, select_expr: str | None = None,
) -> list[dict]:
    """ONE ``nix-eval-jobs`` pass over ``{root}#{attr}``. Returns a list
    of parsed JSON entries (each with at minimum ``attr`` and ``drvPath``).
    """
    argv = [
        "nix-eval-jobs",
        "--flake", f"{root}#{attr}",
        "--max-jobs", "1",
        # ``_crossToolchainMap`` and ``dataset`` subtrees don't carry
        # ``recurseForDerivations`` markers, so nix-eval-jobs would
        # stop at the first attrset by default. ``--force-recurse``
        # makes the single-call bulk eval actually visit every leaf.
        "--force-recurse",
    ]
    if select_expr is not None:
        argv += ["--select", select_expr]
    out = _run(argv, err_label=f"nix-eval-jobs {attr}")
    entries: list[dict] = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("drvPath"):
            entries.append(entry)
    return entries


def _select_attrnames(names: list[str]) -> str:
    """Compose a ``--select`` expression that filters the parent attrset
    to a given list of top-level names (e.g. archs or binaries)."""
    keys = " ".join(f'"{n}" = null;' for n in names)
    return f"m: builtins.intersectAttrs {{ {keys} }} m"


def _select_archs_and_inner_suffixes(archs: list[str], pattern: str) -> str:
    """``--select`` expression for ``dataset.<sys>.<binary>``: keeps only
    the given archs and, within each arch, only the suffixes matching
    ``pattern``. ONE nix-eval-jobs call walks the resulting subtree."""
    arch_filter = " ".join(f'"{a}" = null;' for a in archs)
    return (
        f"m: builtins.mapAttrs "
        f"(_: archset: builtins.intersectAttrs (builtins.listToAttrs "
        f"(map (s: {{ name = s; value = null; }}) "
        f"(builtins.filter (s: builtins.match {json.dumps(pattern)} s != null) "
        f"(builtins.attrNames archset)))) archset) "
        f"(builtins.intersectAttrs {{ {arch_filter} }} m)"
    )


def eval_bash_drv_path(*, root: Path) -> str:
    """ONE ``nix eval --raw`` call. The bash drv is needed by
    sum_drv.nix to embed as the builder; an arbitrary store path works
    so long as it's the same bash that the production sum-drv uses."""
    out = _run(
        [
            "nix", "eval", "--raw",
            "--extra-experimental-features", "nix-command flakes",
            "--impure", "--expr",
            f'(builtins.getFlake "{root}").inputs.nixpkgs'
            f".legacyPackages.{SYS_NAME}.bash.drvPath",
        ],
        err_label="nix eval bash drvPath",
    )
    path = out.decode("utf-8", errors="replace").strip()
    assert path.startswith("/nix/store/") and path.endswith(".drv"), (
        f"unexpected bash drv path: {path!r}"
    )
    return path


def eval_matrix_drvs_for_binary(
    *, root: Path, binary: str, archs: list[str],
    suffix_pattern: str = SUFFIX_MATCH_PATTERN,
) -> list[str]:
    """ONE ``nix-eval-jobs`` pass over ``dataset.<sys>.<binary>`` — returns
    every variant ``.drv`` path for the requested archs whose suffix
    matches ``suffix_pattern``. NEVER walks the attr tree leaf-by-leaf."""
    entries = _nix_eval_jobs(
        root=root,
        attr=f"dataset.{SYS_NAME}.{binary}",
        select_expr=_select_archs_and_inner_suffixes(archs, suffix_pattern),
    )
    return [entry["drvPath"] for entry in entries]


def eval_toolchain_drvs(*, root: Path, archs: list[str]) -> list[str]:
    """ONE ``nix-eval-jobs`` pass over ``_crossToolchainMap.<sys>`` —
    returns every compiler wrapper ``.drv`` path for the requested archs."""
    entries = _nix_eval_jobs(
        root=root,
        attr=f"_crossToolchainMap.{SYS_NAME}",
        select_expr=_select_attrnames(archs),
    )
    return [entry["drvPath"] for entry in entries]


def discover_archs(*, root: Path) -> list[str]:
    """ONE ``nix eval --json`` for ``_debug.<sys>.targets``."""
    out = _run(
        [
            "nix", "eval",
            "--extra-experimental-features", "nix-command flakes",
            "--json", f"{root}#_debug.{SYS_NAME}.targets",
        ],
        err_label=f"nix eval _debug.{SYS_NAME}.targets",
    )
    raw = json.loads(out.decode("utf-8", errors="replace"))
    assert isinstance(raw, list) and raw, "_debug.targets returned empty list"
    return [a for a in raw if isinstance(a, str)]


def resolved_smoke_archs(discovered: list[str]) -> list[str]:
    """``ASM_PHASE3_SMOKE_ARCHS`` resolver. ``all`` → every discovered
    arch; comma-list → intersection with ``discovered``; unset →
    ``DEFAULT_SMOKE_ARCHS`` filtered against ``discovered``."""
    raw = os.environ.get("ASM_PHASE3_SMOKE_ARCHS", "").strip()
    if not raw:
        return [a for a in DEFAULT_SMOKE_ARCHS if a in discovered]
    if raw == "all":
        return list(discovered)
    wanted = [tok.strip() for tok in raw.split(",") if tok.strip()]
    return [a for a in wanted if a in discovered]


def toolchain_task_ids_for_drvs(
    toolchain_drvs: list[str],
) -> dict[str, str]:
    """Compose ``{ident -> build_compilers__*}`` for every toolchain leaf.
    ``plan_cell._variant_toolchain_dep`` only reads ``set(values)``, so
    the ident keys can be the drv basenames themselves."""
    from template_graph.tree_walker import (  # noqa: PLC0415
        _COMP_RE,
    )
    out: dict[str, str] = {}
    for drv in toolchain_drvs:
        basename = STORE_HASH_RE.sub("", drv)
        # Extract arch + compiler from the cross-compiler wrapper basename,
        # e.g. ``aarch64-unknown-linux-gnu-clang-wrapper-10.0.1.drv``.
        # We split on ``-wrapper-`` and parse the leading triple +
        # compiler label.
        if "-wrapper-" not in basename:
            continue
        head, _, _tail = basename.partition("-wrapper-")
        # ``head`` looks like ``aarch64-unknown-linux-gnu-clang`` or
        # ``x86_64-unknown-linux-gnu-gcc``.
        mc = _COMP_RE.search(head + "-O0")  # synthesise opt to anchor regex
        if mc is None:
            continue
        comp = mc.group(1)
        arch = head[: -(len(comp) + 1)].split("-", 1)[0]
        # Note: the triple's first segment IS the arch for the variant
        # drv-name shape (e.g. ``aarch64-unknown-linux-gnu`` → arch
        # ``aarch64``). For native (no cross) toolchains, the head is
        # short — fall back to ``x86_64``.
        if not arch:
            arch = "x86_64"
        out[basename] = f"build_compilers__{SYS_NAME}__{arch}__{comp}"
    return out


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


def build_variant_lookup(
    tree_text: str, *, binaries: list[str] | None = None,
) -> dict[tuple[str, str], dict]:
    """Scan tree text for matrix-depth2 entries; compose ``(arch, label) ->
    spec`` the way streaming's ``cur_label`` does. ``binaries`` filters
    to specific binary names (default: any)."""
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
        if binaries is not None and binary not in binaries:
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
            "pkg": binary,
        }
    return lookup


# --- Sum-drv + planner stages ----------------------------------------------


def build_sum_drv_from_eval(
    *,
    bash_drv_path: str,
    toolchain_drvs: list[str],
    matrix_drvs: dict[str, list[str]],
    root_name: str = "phase3-smoke-sum-root",
) -> str:
    """ONE ``nix-instantiate`` call: ``make_sum_drv_from_paths`` over
    drv paths sourced from bulk ``nix-eval-jobs`` passes. NEVER calls
    the flake-eval ``make_sum_drv`` (which re-evaluates the flake
    closure per leaf and is forbidden here)."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_sum_drv_from_paths,
    )
    return make_sum_drv_from_paths(
        bash_path=bash_drv_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs,
        root_name=root_name,
        toolchains_name="toolchains",
        system=SYS_NAME,
    )


def plan_from_tree(
    tree_text: str,
    *,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
    binary: str = BINARY,
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
        binary,
        streaming_result,
        variant_lookup,
        sys_name=SYS_NAME,
        toolchain_task_ids=toolchain_task_ids,
    )
    return descriptors, streaming_result

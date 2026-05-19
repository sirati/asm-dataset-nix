"""Live-matrix end-to-end smoke test for the Phase-3 streaming planner.

Complements the hand-built ``phase3_debug_cases`` corpus (sibling D.3)
by exercising the FULL discoverable compiler / arch matrix through real
``nix-instantiate``. Scope:

  * 1 binary (``hello``), x86_64-linux primary, all 7 opt levels;
  * all compilers from ``_debug.compilers`` + all archs from
    ``_debug.targets`` (overrideable via ``ASM_PHASE3_SMOKE_ARCHS``;
    default: ``x86_64`` + ``aarch64``);
  * two inner-axis combos per ``(comp, opt)`` cell (``baseline-default``
    + ``noinline-default``, both ``san-off`` ``march-default``) so the
    streaming planner's calibration pair fires.

Validation contract (mirrors ``test_phase3_debug_cases.py``):

  * ``len(descriptors) > 0``, ``len(build_variants) > 0``;
  * every ``build_variant`` carries a ``build_compilers__*`` task_id in
    ``depends_on`` (regression-pin for plan_cell._variant_toolchain_dep);
  * descriptor task_ids unique across the plan;
  * ``counters["source_terminal_skipped"] >= 1``;
  * ``len(violations) == 0`` in lax mode.

Marked ``@pytest.mark.nix``; excluded from the default ``pytest``
invocation. Run explicitly:

    pytest -m nix python/compiler_suit_runner/tests/test_phase3_smoke.py -v

Skips cleanly when ``nix-instantiate`` isn't on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.nix


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------

# _meta decides which suffixes exist per arch (see
# _enumerate_valid_suffixes_for_arch); no local guesswork.
_DEFAULT_SMOKE_ARCHS: tuple[str, ...] = ("x86_64", "aarch64")
_SYS_NAME = "x86_64-linux"
_BINARY = "hello"
_FLAKE_BASH_ATTR = f"inputs.nixpkgs.legacyPackages.{_SYS_NAME}.bash"
_SUFFIX_MATCH_PATTERN = (
    ".*-(baseline-default|noinline-default)-san-off-march-default"
)
_STORE_HASH_RE = re.compile(r"^/nix/store/[^-]+-")


# ---------------------------------------------------------------------------
# Helpers (each does one thing; the test body just orchestrates)
# ---------------------------------------------------------------------------


def _flake_root() -> Path:
    """Repo root — same ``parents[3]`` walk used by ``conftest.py``."""
    return Path(__file__).resolve().parents[3]


def _flake_ref(root: Path) -> str:
    """``git+file://`` URI accepted by ``builtins.getFlake``."""
    return f"git+file://{root}"


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


def _resolved_smoke_archs(discovered: list[str]) -> list[str]:
    """``ASM_PHASE3_SMOKE_ARCHS`` resolver. ``all`` → every discovered
    arch; comma-list → intersection with ``discovered``; unset →
    ``_DEFAULT_SMOKE_ARCHS``."""
    raw = os.environ.get("ASM_PHASE3_SMOKE_ARCHS", "").strip()
    if not raw:
        return [a for a in _DEFAULT_SMOKE_ARCHS if a in discovered]
    if raw == "all":
        return list(discovered)
    wanted = [tok.strip() for tok in raw.split(",") if tok.strip()]
    return [a for a in wanted if a in discovered]


def _discover_compilers(root: Path) -> list[str]:
    """``_debug.compilers`` → flat sorted list of compiler labels."""
    payload = _nix_eval_json(f"_debug.{_SYS_NAME}.compilers", root=root)
    assert isinstance(payload, dict)
    return sorted({
        entry["label"]
        for family in ("gcc", "clang")
        for entry in payload.get(family, [])
        if isinstance(entry, dict) and isinstance(entry.get("label"), str)
    })


def _discover_archs(root: Path) -> list[str]:
    """``_debug.targets`` → list of arch labels."""
    raw = _nix_eval_json(f"_debug.{_SYS_NAME}.targets", root=root)
    assert isinstance(raw, list) and raw
    return [a for a in raw if isinstance(a, str)]


def _discover_compilers_per_arch(root: Path) -> dict[str, list[str]]:
    """``_crossToolchainsMeta`` → ``{arch: [comp_label, ...]}`` (every
    (arch, compiler) pair the matrix considers valid)."""
    raw = _nix_eval_json(f"_crossToolchainsMeta.{_SYS_NAME}", root=root)
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
        f'(s: builtins.match "{_SUFFIX_MATCH_PATTERN}" s != null) '
        f'(builtins.attrNames m)'
    )
    raw = _nix_eval_json(
        f"_meta.{_SYS_NAME}.{_BINARY}.{arch}",
        root=root, apply=apply_expr,
    )
    if not isinstance(raw, list):
        raise RuntimeError(
            f"_meta enumeration for {arch} returned non-list: "
            f"{type(raw).__name__}"
        )
    return [s for s in raw if isinstance(s, str)]


def _matrix_attrs_for(smoke_archs: list[str], root: Path) -> list[str]:
    """Compose ``dataset.<sys>.<binary>.<arch>.<suffix>`` attrs for every
    arch in ``smoke_archs``, sourcing the valid suffix list from ``_meta``."""
    out: list[str] = []
    for arch in smoke_archs:
        for suffix in _enumerate_valid_suffixes_for_arch(arch=arch, root=root):
            out.append(f"dataset.{_SYS_NAME}.{_BINARY}.{arch}.{suffix}")
    return out


def _toolchain_attrs_for(
    smoke_archs: list[str], compilers_per_arch: dict[str, list[str]],
) -> list[str]:
    """``_crossToolchainMap.<sys>.<arch>.<comp>`` attrs for every valid
    (arch, compiler) pair in the smoke archs."""
    return [
        f"_crossToolchainMap.{_SYS_NAME}.{arch}.{comp}"
        for arch in smoke_archs
        for comp in compilers_per_arch.get(arch, [])
    ]


def _query_tree(sum_drv: str) -> str:
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


def _build_variant_lookup(tree_text: str) -> dict[tuple[str, str], dict]:
    """Scan the tree text for matrix-depth2 entries; compose
    ``(arch, label) -> spec`` the way streaming's ``cur_label`` does.
    Mirrors ``archive.derive_variant_lookup_from_drvs`` but reads the
    tree directly so the test doesn't need a ``nix-store --import``."""
    from template_graph.tree_walker import (  # noqa: PLC0415
        VARIANT_SUFFIX, parse_variant_path,
    )
    lookup: dict[tuple[str, str], dict] = {}
    for raw_line in tree_text.splitlines():
        store_path = _extract_store_path(raw_line, VARIANT_SUFFIX)
        if store_path is None:
            continue
        basename = _STORE_HASH_RE.sub("", store_path)
        if basename == store_path:
            continue
        try:
            binary, arch, comp, opt = parse_variant_path(basename)
        except Exception:
            continue
        if binary != _BINARY:
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
            "pkg": _BINARY,
        }
    return lookup


def _extract_store_path(raw_line: str, variant_suffix: str) -> str | None:
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


def _toolchain_task_ids_for_combos(
    compilers_per_arch: dict[str, list[str]],
) -> dict[str, str]:
    """``ident -> build_compilers__*`` map. ``plan_cell._variant_toolchain_dep``
    only reads ``set(values)``, so synthetic idents are fine here."""
    return {
        f"__synth__{arch}__{comp}":
            f"build_compilers__{_SYS_NAME}__{arch}__{comp}"
        for arch, comps in compilers_per_arch.items()
        for comp in comps
    }


def _skip_unless_nix_available() -> None:
    """Skip cleanly when the required nix binaries aren't on PATH."""
    for tool in ("nix-instantiate", "nix-store", "nix"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not in PATH")
    if not (_flake_root() / "flake.nix").is_file():
        pytest.skip(f"no flake.nix at expected root {_flake_root()}")


def _build_sum_drv(
    *,
    root: Path,
    smoke_archs: list[str],
    compilers_per_arch: dict[str, list[str]],
) -> str:
    """Stage 1: build flake-attr lists + assemble the sum-drv via the
    flake-form ``make_sum_drv``. Returns the sum-root drv path."""
    from template_graph.make_sum_drv import make_sum_drv  # noqa: PLC0415

    matrix_attrs = _matrix_attrs_for(smoke_archs, root)
    toolchain_attrs = _toolchain_attrs_for(smoke_archs, compilers_per_arch)
    assert matrix_attrs, "matrix_attrs empty after smoke-arch filtering"
    assert toolchain_attrs, "toolchain_attrs empty — no valid (arch, comp)"

    sum_drv = make_sum_drv(
        flake_ref=_flake_ref(root),
        bash_attr=_FLAKE_BASH_ATTR,
        toolchain_attrs=toolchain_attrs,
        matrices={f"matrix-{_BINARY}": matrix_attrs},
        root_name="phase3-smoke-sum-root",
        toolchains_name="toolchains",
        system=_SYS_NAME,
        tolerate_missing=False,
    )
    assert sum_drv.endswith(".drv"), f"unexpected sum-drv path: {sum_drv}"
    return sum_drv


def _plan_from_tree(
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
        _BINARY,
        streaming_result,
        variant_lookup,
        sys_name=_SYS_NAME,
        toolchain_task_ids=toolchain_task_ids,
    )
    return descriptors, streaming_result


def _assert_descriptors_contract(
    descriptors, streaming_result,
) -> None:
    """Stage 3: invariants the live matrix is expected to satisfy."""
    from compiler_suit_runner.workers.dependency_graph_worker.counters import (  # noqa: PLC0415
        compute_dependency_graph_counters,
    )
    assert len(descriptors) > 0, "no descriptors emitted"

    build_variant_descs = [
        d for d in descriptors if d.kind == "build_variant"
    ]
    assert len(build_variant_descs) > 0, (
        "no build_variant descriptors — variant_lookup never matched"
    )

    task_ids = [d.task_id for d in descriptors]
    assert len(task_ids) == len(set(task_ids)), (
        "duplicate task_ids in plan: "
        + repr([t for t in task_ids if task_ids.count(t) > 1][:5])
    )

    missing_tc = [
        d for d in build_variant_descs
        if not any(dep.startswith("build_compilers__") for dep in d.depends_on)
    ]
    assert not missing_tc, (
        f"{len(missing_tc)} build_variant descriptor(s) lack a "
        f"build_compilers__ dep; first 3: "
        + repr([(d.task_id, d.depends_on) for d in missing_tc[:3]])
    )

    counters = compute_dependency_graph_counters(
        streaming_result=streaming_result,
        descriptors=descriptors,
        binaries=[_BINARY],
    )
    assert counters["source_terminal_skipped"] >= 1, (
        "expected at least one source_terminal_skipped — real trees "
        "always carry tarball / patch deps"
    )

    violations = list(streaming_result.get("violations", []) or [])
    assert len(violations) == 0, (
        f"streaming planner recorded {len(violations)} violation(s) "
        f"in lax mode; first 3: {violations[:3]}"
    )


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_phase3_smoke_live_matrix():
    """End-to-end probe: live matrix → streaming planner →
    ``plan_phase4_for_binary``. Stage helpers narrow failures."""
    _skip_unless_nix_available()
    root = _flake_root()

    discovered_compilers = _discover_compilers(root)
    assert discovered_compilers, "no compilers discovered from _debug.compilers"

    discovered_archs = _discover_archs(root)
    compilers_per_arch_all = _discover_compilers_per_arch(root)
    smoke_archs = _resolved_smoke_archs(discovered_archs)
    assert smoke_archs, (
        f"no smoke archs resolved from "
        f"ASM_PHASE3_SMOKE_ARCHS={os.environ.get('ASM_PHASE3_SMOKE_ARCHS')!r}; "
        f"discovered={discovered_archs}"
    )
    compilers_per_arch = {
        arch: compilers_per_arch_all.get(arch, []) for arch in smoke_archs
    }

    sum_drv = _build_sum_drv(
        root=root,
        smoke_archs=smoke_archs,
        compilers_per_arch=compilers_per_arch,
    )
    tree_text = _query_tree(sum_drv)
    assert tree_text, "empty tree text from nix-store --query --tree"

    variant_lookup = _build_variant_lookup(tree_text)
    assert variant_lookup, (
        "variant_lookup empty — no matrix-depth2 entries parsed from tree"
    )
    toolchain_task_ids = _toolchain_task_ids_for_combos(compilers_per_arch)
    assert toolchain_task_ids, "toolchain_task_ids empty"

    descriptors, streaming_result = _plan_from_tree(
        tree_text,
        variant_lookup=variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    _assert_descriptors_contract(descriptors, streaming_result)

"""Local pre-flight: enumerate the variant matrix via ``nix eval``.

The runner needs the full superset of (pkg, arch, variant_suffix) tuples
*before* it submits anything, because the dynamic_runner framework's
queue is fixed at coord.run() time (see plan §"How to inject Phase 2 +
Phase 3 items into the queue", option A). This module implements the
local pre-flight: it shells out to ``nix eval`` against this repo's
flake to read

* ``.#_meta.<sys>``  — instant; pure metadata only.
* ``.#_drvPaths.<sys>`` — slow; forces drv instantiation.
* ``.#_crossToolchainsMeta.<sys>`` — instant.

and assembles a :class:`PreflightResult` that downstream code (CLI,
manifest_gen) can feed to ``emit_all_manifests``.

Subprocess invocation is dependency-injected via ``run_subprocess`` so
unit tests stay hermetic — the real flake never has to be evaluated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
import subprocess
from collections.abc import Callable
from typing import Optional

from compiler_suit_runner.partition import VariantSpec


def _short_dataset_name(
    *, compiler_id: str, arch: str, optimization: str, full_label: str
) -> str:
    """Return the short filesystem identifier for a variant.

    Format: ``<compiler>_<arch>_<opt>_<8-hex-hash>``. The hash is
    deterministic (sha256 of the full variant label), so the same
    variant always maps to the same filename — re-running with new
    flag dimensions added to the matrix doesn't shift previously-built
    tarballs around. Filesystem-unsafe characters are scrubbed from
    the leading fields (compilers like ``gcc-old-4_4`` keep their
    hyphens / underscores; nothing else is currently problematic).
    """
    digest = hashlib.sha256(full_label.encode("utf-8")).hexdigest()[:8]
    safe = re.compile(r"[^A-Za-z0-9._-]+")
    parts = [
        safe.sub("-", compiler_id) or "unknown-compiler",
        safe.sub("-", arch) or "unknown-arch",
        safe.sub("-", optimization) or "unknown-opt",
        digest,
    ]
    return "_".join(parts)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PreflightResult:
    """The outputs of one pre-flight pass.

    ``variants`` is the full superset that ``emit_all_manifests`` will
    materialise into phase-3 manifests (filtered by the user's
    ``--packages`` / ``--archs`` flags upstream).

    ``toolchain_specs`` enumerates every ``(arch, compiler_label)`` pair
    the matrix considers a valid cross-toolchain target — these become
    phase-2 toolchain manifests.

    ``common_dep_drvs`` is left empty by this module: the populating
    logic lives in phase 1b's merge worker (see
    :mod:`compiler_suit_runner.workers.merge_worker`). It exists as a
    field here so callers can pass a complete :class:`PreflightResult`
    around regardless of whether the merge has run yet.

    ``toolchain_drvs`` is the canonical set of nix drv paths for every
    matrix variant's drv. The phase-1b classifier intersects this with
    its frequency map to identify "this is a toolchain build, hoist it
    to phase 2" vs "this is a common host dep we should pre-build".
    For now we expose it as a frozenset so the merge worker can consume
    it without reconstructing the set itself.
    """

    sys_name: str
    variants: tuple[VariantSpec, ...]
    toolchain_specs: tuple[tuple[str, str], ...]
    common_dep_drvs: tuple[tuple[str, str], ...]
    toolchain_drvs: frozenset[str]


# ---------------------------------------------------------------------------
# Subprocess injection
# ---------------------------------------------------------------------------


# ``run_subprocess`` accepts argv (list[str]) and returns a tuple of
# (stdout_bytes, stderr_bytes, returncode).
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# nix eval helper
# ---------------------------------------------------------------------------


def run_nix_eval(
    flake_ref: str,
    attr: str,
    *,
    raw: bool = False,
    run_subprocess: Optional[RunSubprocess] = None,
):
    """Invoke ``nix eval`` on a single attribute.

    By default returns the parsed JSON result. With ``raw=True`` the
    function returns the stdout as a :class:`str` (useful for
    ``--raw`` outputs like single drv paths).

    Raises :class:`RuntimeError` on non-zero exit; the stderr is
    embedded in the exception message so callers can surface it.
    """
    runner = run_subprocess or _default_run_subprocess

    argv: list[str] = [
        "nix",
        "eval",
        "--extra-experimental-features",
        "nix-command flakes",
    ]
    if raw:
        argv.append("--raw")
    else:
        argv.append("--json")
    argv.append(f"{flake_ref}#{attr}")

    stdout, stderr, rc = runner(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"nix eval {flake_ref}#{attr} failed (rc={rc}): {decoded_err}"
        )

    if raw:
        return stdout.decode("utf-8", errors="replace")

    text = stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nix eval {flake_ref}#{attr} returned invalid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Variant enumeration
# ---------------------------------------------------------------------------


def _tier_from_pkg(pkg: str) -> int:
    """Coarse tier classification carried as variant metadata.

    Tier 1 = small (hello, busybox); tier 3 = large (coreutils, gawk);
    tier 2 = everything else. Used as a payload hint only; the matrix
    no longer uses it for memory budgeting.
    """
    if pkg in ("hello", "busybox"):
        return 1
    if pkg in ("coreutils", "gawk"):
        return 3
    return 2


def _build_variant_spec(
    *,
    pkg: str,
    arch: str,
    suffix: str,
    meta_entry: dict,
    drv_path: str,
) -> VariantSpec:
    """Assemble one :class:`VariantSpec` from the meta + drvPaths slices.

    ``tarball_name`` and ``metadata_name`` are short-form file names
    (``<compiler>_<arch>_<opt>_<hash>``) — keeps the filesystem layout
    legible even when the matrix grows extra axes (sanitizer, march,
    individual hardening flags). The full parameter set is written to
    the sidecar JSON during phase-3 build.
    """
    label = meta_entry.get("variantLabel", suffix)
    compiler_id = meta_entry.get("compiler", "")
    optimization = meta_entry.get("optimization", "")
    short = _short_dataset_name(
        compiler_id=compiler_id,
        arch=arch,
        optimization=optimization,
        full_label=label,
    )
    return {
        "label": label,
        "drv": drv_path,
        "tarball_name": f"{short}.tar.zst",
        "metadata_name": f"{short}.json",
        "compiler_id": compiler_id,
        "compiler_family": meta_entry.get("compilerFamily", ""),
        "compiler_version": meta_entry.get("compilerVersion", ""),
        "optimization": optimization,
        "flag_set": meta_entry.get("flags", ""),
        "hardening": meta_entry.get("hardening", ""),
        "tier": _tier_from_pkg(pkg),
        "pkg": pkg,
        "arch": arch,
    }


def _sample_suffix_attrs(
    suffix_attrs: dict,
    *,
    arch: str,
    sample_size: int,
    seed: str,
) -> dict:
    """Down-sample ``{suffix: meta_entry}`` to ``sample_size`` per
    ``(compiler, optimization)`` group, seeded deterministically.

    The seed for each group is ``f"{seed}:{compiler}:{arch}:{opt}"`` so
    changing the operator-supplied seed reshuffles every group, while
    holding the seed fixed gives identical samples across runs (skip-
    existing then guarantees we don't rebuild).

    Suffixes lacking ``compiler`` / ``optimization`` in their meta entry
    are passed through unchanged (we can't group them).
    """
    if sample_size <= 0:
        return suffix_attrs
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    passthrough: dict[str, dict] = {}
    for suffix, meta_entry in suffix_attrs.items():
        if not isinstance(meta_entry, dict):
            passthrough[suffix] = meta_entry
            continue
        compiler = meta_entry.get("compiler")
        opt = meta_entry.get("optimization")
        if not isinstance(compiler, str) or not isinstance(opt, str):
            passthrough[suffix] = meta_entry
            continue
        groups.setdefault((compiler, opt), []).append((suffix, meta_entry))

    sampled: dict[str, dict] = dict(passthrough)
    for (compiler, opt), candidates in groups.items():
        candidates.sort(key=lambda kv: kv[0])
        rng = random.Random(f"{seed}:{compiler}:{arch}:{opt}")
        chosen = rng.sample(candidates, min(sample_size, len(candidates)))
        for suffix, meta_entry in chosen:
            sampled[suffix] = meta_entry
    return sampled


def enumerate_variants(
    flake_ref: str,
    sys_name: str,
    *,
    packages: Optional[list[str]] = None,
    archs: Optional[list[str]] = None,
    sample_size: int = 0,
    sample_seed: str = "42",
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[tuple[VariantSpec, ...], frozenset[str]]:
    """Enumerate every ``(pkg, arch, suffix)`` variant the matrix exposes.

    Filters by ``packages`` and ``archs`` if provided (each is an inclusion
    list — None means "all").

    When ``sample_size > 0`` each ``(compiler, arch, optimization)`` group
    is down-sampled to that many ``(flag, hardening)`` combinations using
    a deterministic seeded RNG keyed on ``sample_seed`` plus the group
    identity — so changing the seed reshuffles every group. The slow
    ``_drvPaths`` eval is still scoped per ``(pkg, arch)``; sampling
    happens against the meta layer (instant) before any drv lookups.

    Returns ``(variants, toolchain_drvs)`` where ``toolchain_drvs`` is the
    set of nix drv paths corresponding to the variants. (The plan calls
    this the canonical set; phase 1b uses it to filter "toolchain" from
    "common host dep" classification.)
    """
    meta = run_nix_eval(
        flake_ref,
        f"_meta.{sys_name}",
        run_subprocess=run_subprocess,
    )

    if not isinstance(meta, dict):
        raise RuntimeError(
            f"_meta.{sys_name} is not a JSON object (got {type(meta).__name__})"
        )

    pkg_filter = set(packages) if packages else None
    arch_filter = set(archs) if archs else None

    # Scope the (slow, drv-instantiating) `_drvPaths` eval to just the
    # (pkg, arch) combos the operator actually asked for. The full-matrix
    # eval `_drvPaths.<sys>` touches every (compiler, arch) cell — including
    # broken combos like gcc5+mips64el (nixpkgs-18.03 lacks
    # platform.kernelArch for the triple) whose errors raise from
    # `derivationStrict` and cannot be caught by `tryEval`. When the user
    # filters with --packages / --archs, evaluating per-(pkg, arch) keeps
    # the eval inside the requested scope, so unrelated broken cells stay
    # untouched. With no filter we have to evaluate the whole system —
    # callers asking for "everything" implicitly accept the broken-cell risk.
    full_drvs: dict | None = None
    if pkg_filter is None and arch_filter is None:
        full_drvs = run_nix_eval(
            flake_ref,
            f"_drvPaths.{sys_name}",
            run_subprocess=run_subprocess,
        )
        if not isinstance(full_drvs, dict):
            raise RuntimeError(
                f"_drvPaths.{sys_name} is not a JSON object "
                f"(got {type(full_drvs).__name__})"
            )

    variants: list[VariantSpec] = []
    drv_set: set[str] = set()

    for pkg, arch_attrs in sorted(meta.items()):
        if pkg_filter is not None and pkg not in pkg_filter:
            continue
        if not isinstance(arch_attrs, dict):
            continue
        for arch, suffix_attrs in sorted(arch_attrs.items()):
            if arch_filter is not None and arch not in arch_filter:
                continue
            if not isinstance(suffix_attrs, dict):
                continue
            if sample_size > 0:
                suffix_attrs = _sample_suffix_attrs(
                    suffix_attrs,
                    arch=arch,
                    sample_size=sample_size,
                    seed=sample_seed,
                )
            if full_drvs is None:
                drvs_arch = run_nix_eval(
                    flake_ref,
                    f"_drvPaths.{sys_name}.{pkg}.{arch}",
                    run_subprocess=run_subprocess,
                )
            else:
                drvs_pkg = full_drvs.get(pkg)
                drvs_arch = (
                    drvs_pkg.get(arch) if isinstance(drvs_pkg, dict) else None
                )
            if not isinstance(drvs_arch, dict):
                continue
            for suffix, meta_entry in sorted(suffix_attrs.items()):
                if not isinstance(meta_entry, dict):
                    continue
                drv = drvs_arch.get(suffix)
                if not isinstance(drv, str) or not drv:
                    continue
                variant = _build_variant_spec(
                    pkg=pkg,
                    arch=arch,
                    suffix=suffix,
                    meta_entry=meta_entry,
                    drv_path=drv,
                )
                variants.append(variant)
                drv_set.add(drv)

    return tuple(variants), frozenset(drv_set)


# ---------------------------------------------------------------------------
# Toolchain enumeration
# ---------------------------------------------------------------------------


def enumerate_toolchains(
    flake_ref: str,
    sys_name: str,
    archs: Optional[list[str]] = None,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[tuple[str, str], ...]:
    """Read ``.#_crossToolchainsMeta.<sys>`` and return the (arch, compiler)
    pair list.

    Filters by ``archs`` if provided. The returned list is sorted by
    ``(arch, compiler_label)`` for reproducible manifest ordering.
    """
    meta = run_nix_eval(
        flake_ref,
        f"_crossToolchainsMeta.{sys_name}",
        run_subprocess=run_subprocess,
    )
    if not isinstance(meta, dict):
        raise RuntimeError(
            f"_crossToolchainsMeta.{sys_name} is not a JSON object"
            f" (got {type(meta).__name__})"
        )

    arch_filter = set(archs) if archs else None
    pairs: list[tuple[str, str]] = []
    for arch, comps in sorted(meta.items()):
        if arch_filter is not None and arch not in arch_filter:
            continue
        if not isinstance(comps, list):
            continue
        for entry in comps:
            if not isinstance(entry, dict):
                continue
            label = entry.get("compiler")
            if isinstance(label, str) and label:
                pairs.append((arch, label))
    pairs.sort()
    return tuple(pairs)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def preflight(
    flake_ref: str,
    sys_name: str,
    *,
    packages: Optional[list[str]] = None,
    archs: Optional[list[str]] = None,
    sample_size: int = 0,
    sample_seed: str = "42",
    run_subprocess: Optional[RunSubprocess] = None,
) -> PreflightResult:
    """Composite call: variants + toolchains.

    See :func:`enumerate_variants` for the meaning of ``sample_size`` /
    ``sample_seed``. ``common_dep_drvs`` is left empty here — phase 1b's
    merge worker populates it on the cluster. The phase-2 manifests
    therefore only cover toolchains until the merge runs.
    """
    variants, toolchain_drvs = enumerate_variants(
        flake_ref,
        sys_name,
        packages=packages,
        archs=archs,
        sample_size=sample_size,
        sample_seed=sample_seed,
        run_subprocess=run_subprocess,
    )
    toolchain_specs = enumerate_toolchains(
        flake_ref,
        sys_name,
        archs=archs,
        run_subprocess=run_subprocess,
    )
    return PreflightResult(
        sys_name=sys_name,
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_dep_drvs=(),
        toolchain_drvs=toolchain_drvs,
    )


def filter_existing_variants(
    variants: tuple[VariantSpec, ...],
    *,
    dataset_dir,
) -> tuple[tuple[VariantSpec, ...], int]:
    """Drop variants whose tarball already lives in ``dataset_dir``.

    Returns ``(remaining_variants, skipped_count)``. The output dir
    layout is flat — phase-3 build_worker writes
    ``<dataset_dir>/<tarball_name>`` directly — so a single existence
    check per variant is enough.
    """
    import pathlib

    dataset_dir = pathlib.Path(dataset_dir)
    if not dataset_dir.is_dir():
        return variants, 0
    remaining: list[VariantSpec] = []
    skipped = 0
    for variant in variants:
        tarball = dataset_dir / variant["tarball_name"]
        if tarball.exists():
            skipped += 1
        else:
            remaining.append(variant)
    return tuple(remaining), skipped

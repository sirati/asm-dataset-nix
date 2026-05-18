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
import shutil
import subprocess
from collections.abc import Callable, Iterable
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
    logic lives downstream (legacy partition/merge workers were
    removed; the replacement dependency_graph planner will populate
    this field once it lands). It exists as a field here so callers
    can pass a complete :class:`PreflightResult` around regardless of
    whether the downstream classifier has run yet.

    ``toolchain_drvs`` is the canonical set of nix drv paths for every
    matrix variant's drv. The downstream classifier intersects this
    with its frequency map to identify "this is a toolchain build,
    hoist it to phase 2" vs "this is a common host dep we should
    pre-build". For now we expose it as a frozenset so the consumer
    can use it without reconstructing the set itself.
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


def _eval_drv_paths_for_suffixes(
    flake_ref: str,
    sys_name: str,
    pkg: str,
    arch: str,
    suffixes: list[str],
    *,
    workers: int = 8,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, str]:
    """Evaluate drv paths for a specific list of suffixes in parallel.

    Uses ``nix-eval-jobs`` (a parallel multi-attr nix evaluator) over
    ``dataset.<sys>.<pkg>.<arch>`` with a ``--select`` lambda that
    projects to just the sampled suffixes. ``nix-eval-jobs`` spawns
    ``workers`` parallel evaluator threads sharing one process, so
    each compiler's cross-toolchain closure gets walked once and
    drv instantiation parallelises across compilers.

    Output is JSONL — one line per attr with ``drvPath``. We collect
    them into a ``{suffix: drv_path}`` dict.

    Suffix names come from the matrix and are made of
    ``[A-Za-z0-9._-]``; we validate before splicing into the
    --select lambda so future schema drift surfaces loudly instead
    of letting an injected nix fragment slip through.

    Falls back to the inline ``nix eval --apply`` path if
    ``nix-eval-jobs`` isn't on PATH (e.g. in the test harness with a
    stubbed ``run_subprocess``).
    """
    runner = run_subprocess or _default_run_subprocess
    safe = re.compile(r"^[A-Za-z0-9._-]+$")
    for s in suffixes:
        if not safe.match(s):
            raise RuntimeError(
                f"unexpected character in matrix suffix {s!r}"
                " — refusing to splice into nix-eval-jobs --select"
            )

    if shutil.which("nix-eval-jobs") is None:
        return _eval_drv_paths_for_suffixes_fallback(
            flake_ref, sys_name, pkg, arch, suffixes, run_subprocess=runner,
        )

    # Build the --select lambda. Form:
    #   m: builtins.intersectAttrs { S1 = null; S2 = null; ... } m
    keys = " ".join(f'"{s}" = null;' for s in suffixes)
    select_expr = f"m: builtins.intersectAttrs {{ {keys} }} m"

    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{flake_ref}#dataset.{sys_name}.{pkg}.{arch}",
        "--select",
        select_expr,
        "--workers",
        str(max(1, workers)),
    ]
    stdout, stderr, rc = runner(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"nix-eval-jobs {flake_ref}#dataset.{sys_name}.{pkg}.{arch}"
            f" failed (rc={rc}): {decoded_err}"
        )
    drvs: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # nix-eval-jobs interleaves status/error lines; ignore
            # anything that isn't a valid JSON object.
            continue
        if not isinstance(entry, dict):
            continue
        attr = entry.get("attr")
        drv = entry.get("drvPath")
        if isinstance(attr, str) and isinstance(drv, str) and drv:
            drvs[attr] = drv
    return drvs


def _eval_drv_paths_for_suffixes_fallback(
    flake_ref: str,
    sys_name: str,
    pkg: str,
    arch: str,
    suffixes: list[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, str]:
    """Single-threaded fallback used when ``nix-eval-jobs`` isn't on
    PATH (mostly: the test harness with a stubbed ``run_subprocess``).

    Form:  ``nix eval --apply 'm: { "S1" = m."S1"; ... }' .#_drvPaths.<...>``
    """
    runner = run_subprocess or _default_run_subprocess
    body = "; ".join(f'"{s}" = m."{s}"' for s in suffixes)
    apply_expr = f"m: {{ {body}; }}"
    argv: list[str] = [
        "nix",
        "eval",
        "--extra-experimental-features",
        "nix-command flakes",
        "--json",
        f"{flake_ref}#_drvPaths.{sys_name}.{pkg}.{arch}",
        "--apply",
        apply_expr,
    ]
    stdout, stderr, rc = runner(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"nix eval (apply fallback) {flake_ref}#_drvPaths.{sys_name}.{pkg}.{arch}"
            f" failed (rc={rc}): {decoded_err}"
        )
    parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"nix eval (apply fallback) returned non-object: {type(parsed).__name__}"
        )
    return parsed  # type: ignore[return-value]


def run_nix_eval(
    flake_ref: str,
    attr: str,
    *,
    raw: bool = False,
    apply: Optional[str] = None,
    run_subprocess: Optional[RunSubprocess] = None,
):
    """Invoke ``nix eval`` on a single attribute.

    By default returns the parsed JSON result. With ``raw=True`` the
    function returns the stdout as a :class:`str` (useful for
    ``--raw`` outputs like single drv paths). With ``apply`` set, the
    given lambda is applied to the evaluated value (``--apply``); use
    this to project a large attrset down to just the keys / shapes
    needed, which avoids forcing the full lazy tree.

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
    if apply is not None:
        argv.extend(["--apply", apply])

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

    ``variant_dir`` is the per-variant subdir name
    (``<compiler>_<arch>_<opt>_<hash>``) under ``dataset/<pkg>/``;
    each variant's ELFs land at ``<variant_dir>/<elf>`` and the
    sidecar JSON sits beside it as ``<variant_dir>.json``.
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
        "variant_dir": short,
        "metadata_name": f"{short}.json",
        "compiler_id": compiler_id,
        "compiler_family": meta_entry.get("compilerFamily", ""),
        "compiler_version": meta_entry.get("compilerVersion", ""),
        "optimization": optimization,
        "flag_set": meta_entry.get("flags", ""),
        "hardening": meta_entry.get("hardening", ""),
        "sanitizer": meta_entry.get("sanitizer", ""),
        "march": meta_entry.get("march", ""),
        "tier": _tier_from_pkg(pkg),
        "pkg": pkg,
        "arch": arch,
    }


def _parse_compiler_major(version: str) -> Optional[int]:
    """Best-effort parse of a compiler version string's major number.

    Accepts ``"15.2.0"`` (returns 15), ``"4.6"`` (returns 4),
    ``"3.4.1-rc2"`` (returns 3). Returns ``None`` on anything that
    doesn't start with a digit.
    """
    m = re.match(r"^(\d+)", version)
    return int(m.group(1)) if m is not None else None


def _parse_compiler_minor(version: str) -> int:
    """Best-effort parse of the minor version (0 if absent)."""
    m = re.match(r"^\d+\.(\d+)", version)
    return int(m.group(1)) if m is not None else 0


# --- Primitives ----------------------------------------------------------
#
# Every rule below is one of three shapes:
#   1. unconditional flag-vs-flag conflict (compiler-independent)
#   2. flag rejected on legacy-nixpkgs compilers (any value of that flag)
#   3. flag value rejected on legacy-nixpkgs compilers (subset of values)
#
# To extend: add to ``_LEGACY_BROKEN_*`` or ``_FLAG_CONFLICTS`` below.
# Don't write new if/return blocks unless the rule genuinely doesn't
# fit any of these shapes — keeping the surface tiny is what makes the
# filter audit-able.


# Compiler-version cutoffs for "this is from a legacy nixpkgs input"
# (15.09 / 18.03 / 22.11 / 23.11 / 24.05). Modern unstable ships
# clang ≥ 18 and gcc ≥ 13 with cc-wrapper, binutils, and runtime libs
# correctly wired up for both native and cross targets.
_LEGACY_CLANG_MAX = 18  # exclusive
_LEGACY_GCC_MAX = 13    # exclusive


def _is_legacy_compiler(family: str, major: Optional[int]) -> bool:
    """True if (family, major) corresponds to a legacy-nixpkgs compiler.

    Used as a uniform precondition for every "feature X doesn't work
    with legacy compilers" rule. Pinning the cutoff in one place means
    bumping nixpkgs revisions only requires editing two constants.
    """
    if major is None:
        return False
    if family == "clang" and major < _LEGACY_CLANG_MAX:
        return True
    if family == "gcc" and major < _LEGACY_GCC_MAX:
        return True
    return False


# Flag values that legacy-nixpkgs cc-wrappers / binutils don't support.
# Each entry maps the value to the missing facility — only used to
# render a helpful error message.

# values of meta_entry["flags"]
_LEGACY_BROKEN_FLAG_SET = {
    "lto":       "LTO-plugin-aware ar/ranlib",
    "ltothin":   "LTO-plugin-aware ar/ranlib",
    "staticpie": "static-pie crt files (rcrt1.o)",
}
# values of meta_entry["hardening"]
_LEGACY_BROKEN_HARDENING = {
    "pie": "Scrt1.o (PIE crt)",
    "cet": "binutils support for Intel CET (-fcf-protection=full) ELF notes",
}
# values of meta_entry["march"]
_LEGACY_BROKEN_MARCH = {
    "march-v2": "crt baseline (psABI v2)",
    "march-v3": "crt baseline (psABI v3)",
    "march-v4": "crt baseline (psABI v4)",
}


# Compiler-independent flag-vs-flag conflicts. Each entry is a
# predicate over the meta_entry that returns a reason string when
# the combination is known-bad, or None when the entry is fine.
def _conflict_sanitizer_o0(meta: dict) -> Optional[str]:
    if meta.get("sanitizer", "san-off") != "san-off" and meta.get("optimization") == "O0":
        return "sanitizer requires -O1+; configure fails on -O0"
    return None


def _conflict_fastmath_san_undefined(meta: dict) -> Optional[str]:
    # ``-ffast-math`` lets the compiler assume ops UBSan instruments
    # don't happen → false positives or link errors. Hits both via
    # the dedicated ``fastmath`` flag set and via ``-Ofast`` which
    # implies ``-ffast-math``.
    if meta.get("sanitizer") != "san-undefined":
        return None
    if meta.get("optimization") == "Ofast":
        return "Ofast implies -ffast-math which conflicts with san-undefined"
    if meta.get("flags") == "fastmath":
        return "fastmath flag set implies -ffast-math which conflicts with san-undefined"
    return None


_FLAG_CONFLICTS = (
    _conflict_sanitizer_o0,
    _conflict_fastmath_san_undefined,
)


def is_known_bad_combo(meta_entry: dict) -> Optional[str]:
    """Return a non-empty reason string if ``meta_entry`` describes a
    combination known to fail at build time, or ``None`` if the combo
    is plausible.

    Filtered combos never enter the variant matrix; this avoids
    wasting a worker slot, a dispatch round-trip, and several minutes
    of nix evaluation on combinations that always fail the configure
    executability test (or fail mid-link).

    Two rule families:

    1. **Flag-vs-flag conflicts** (``_FLAG_CONFLICTS``): combinations
       that fail regardless of which compiler is in play. Examples:
       sanitizer + ``-O0``, ``-ffast-math`` + ``san-undefined``.
    2. **Legacy-compiler-rejects-flag**: a flag value that requires
       runtime/crt/binutils support legacy-nixpkgs cc-wrappers don't
       provide. Examples: LTO, static-pie, PIE hardening, CET, modern
       x86_64-vN march levels. Filtered for clang < 18 / gcc < 13.

    Adding a new rule typically means adding to one of the
    ``_LEGACY_BROKEN_*`` dicts above — not writing new branches here.
    """
    # Flag-vs-flag conflicts (compiler-independent).
    for predicate in _FLAG_CONFLICTS:
        reason = predicate(meta_entry)
        if reason is not None:
            return reason

    family = meta_entry.get("compilerFamily", "")
    major = _parse_compiler_major(meta_entry.get("compilerVersion", ""))
    if not _is_legacy_compiler(family, major):
        return None

    # Sanitizer + legacy-compiler. Always bad regardless of which
    # sanitizer (libasan / libubsan / libtsan / libmsan all come from
    # the same legacy stdenv that doesn't wire them up).
    sanitizer = meta_entry.get("sanitizer", "san-off")
    if sanitizer != "san-off":
        return (
            f"{family} {major} sourced from legacy nixpkgs cannot locate "
            f"the {sanitizer} runtime library; configure fails to link"
        )

    # Legacy-broken flag values, looked up by axis.
    flag_set = meta_entry.get("flags", "")
    if flag_set in _LEGACY_BROKEN_FLAG_SET:
        return (
            f"{family} {major} sourced from legacy nixpkgs lacks "
            f"{_LEGACY_BROKEN_FLAG_SET[flag_set]}"
        )

    hardening = meta_entry.get("hardening", "")
    if hardening in _LEGACY_BROKEN_HARDENING:
        return (
            f"{family} {major} sourced from legacy nixpkgs lacks "
            f"{_LEGACY_BROKEN_HARDENING[hardening]}"
        )

    march = meta_entry.get("march", "march-default")
    if march in _LEGACY_BROKEN_MARCH:
        return (
            f"{family} {major} sourced from legacy nixpkgs cannot link "
            f"{march}: missing {_LEGACY_BROKEN_MARCH[march]}"
        )

    # Per-(arch, compiler-version) broken combos — the wrapper builds,
    # ABI flags are right, hardening is stripped, but the variant
    # build still fails for compiler-internal reasons (no
    # integrated-as for that target+version, autoconf-vs-direct
    # divergence, etc.). Lib/architectures.nix's brokenClangVersions
    # is the source of truth on the Nix side; Python mirrors it
    # via compiler_suit_runner.compiler_flag_support so the runner
    # never enumerates these even if the Nix matrix gate is
    # accidentally bypassed.
    arch = meta_entry.get("arch", "")
    minor = _parse_compiler_minor(meta_entry.get("compilerVersion", ""))
    if family in ("gcc", "clang") and arch and major:
        try:
            from compiler_suit_runner.compiler_flag_support import is_known_broken
            reason = is_known_broken(arch, family, major, minor)
            if reason is not None:
                return (
                    f"{family} {major}.{minor} on {arch}: known-broken — {reason}"
                )
        except Exception:  # noqa: BLE001 — module-import safety
            pass

    return None


# ---------------------------------------------------------------------------
# (pkg, arch) availability gate — pure consumer of the nix-side gate
# ---------------------------------------------------------------------------
#
# Counterpart to ``pkgIsBuildableForTarget`` in ``lib/matrix.nix``. The nix
# matrix returns an empty attrset for any (pkg, arch) cell whose underlying
# nixpkgs derivation declares itself unavailable on the requested host
# platform (``meta.available = false`` — combines
# ``meta.platforms``/``meta.badPlatforms`` with ``meta.broken`` and
# nixpkgs' insecure-allowlist check). Cells excluded for stdenv reasons
# (e.g. nanopb-only-stdenvNoCC packages on archs where the override
# wrapper isn't applicable) also come back empty.
#
# Rather than mirror the platform metadata in a separate Python table
# (which would drift), this helper asks the nix matrix directly: a
# (pkg, arch) is "supported" iff ``_meta.<sys>.<pkg>.<arch>`` has any
# suffix attrs at all. The result is cached per (flake_ref, sys_name,
# pkg, arch) so a sampler can probe thousands of tuples without
# re-shelling out.


_PKG_SUPPORTS_ARCH_CACHE: dict[tuple[str, str, str, str], bool] = {}


def pkg_supports_arch(
    pkg: str,
    arch: str,
    *,
    flake_ref: str = ".",
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
) -> bool:
    """Return ``True`` iff the matrix exposes any variants for ``(pkg, arch)``.

    Mirrors the nix-side ``pkgIsBuildableForTarget`` gate (see
    ``lib/matrix.nix``) by querying ``_meta.<sys>.<pkg>.<arch>`` and
    checking whether its attribute-name list is non-empty. Cells that
    nix dropped — because the underlying package's ``meta.available``
    is ``false`` on that platform, or the package doesn't take a
    ``stdenv`` argument — return ``False``.

    Results are memoised so a Python sampler can call this once per
    (pkg, arch) tuple without re-shelling out to nix. Cache key
    includes ``flake_ref`` and ``sys_name`` so callers querying
    different flakes / systems stay isolated.

    Failures (no such pkg attr, nix eval crashes, missing flake) all
    map to ``False`` — the caller's contract is "drop the tuple", and
    a missing/broken cell is operationally equivalent to "unsupported".
    """
    key = (flake_ref, sys_name, pkg, arch)
    cached = _PKG_SUPPORTS_ARCH_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        attr_names = run_nix_eval(
            flake_ref,
            f"_meta.{sys_name}.{pkg}.{arch}",
            run_subprocess=run_subprocess,
            apply="m: builtins.attrNames m",
        )
    except RuntimeError:
        _PKG_SUPPORTS_ARCH_CACHE[key] = False
        return False

    supported = isinstance(attr_names, list) and len(attr_names) > 0
    _PKG_SUPPORTS_ARCH_CACHE[key] = supported
    return supported


def supported_archs_for_pkg(
    pkg: str,
    *,
    flake_ref: str = ".",
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[str, ...]:
    """Return every arch label for which ``pkg`` has at least one variant.

    Batched companion to :func:`pkg_supports_arch`: one ``nix eval``
    asks for the per-arch attribute-name counts in a single call, then
    populates the per-(pkg, arch) cache with the result so subsequent
    point queries hit the cache.

    Returns ``()`` if the pkg doesn't appear in ``_meta`` at all (eg
    typo or pkg not in ``lib/packages.nix``); the per-(pkg, arch) cache
    entries are still written so repeated calls don't re-shell out.
    """
    try:
        per_arch_counts = run_nix_eval(
            flake_ref,
            f"_meta.{sys_name}.{pkg}",
            run_subprocess=run_subprocess,
            apply="m: builtins.mapAttrs (_: a: builtins.length (builtins.attrNames a)) m",
        )
    except RuntimeError:
        return ()
    if not isinstance(per_arch_counts, dict):
        return ()
    supported: list[str] = []
    for arch, count in per_arch_counts.items():
        if not isinstance(arch, str) or not isinstance(count, int):
            continue
        is_supported = count > 0
        _PKG_SUPPORTS_ARCH_CACHE[(flake_ref, sys_name, pkg, arch)] = is_supported
        if is_supported:
            supported.append(arch)
    supported.sort()
    return tuple(supported)


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
) -> dict[str, dict]:
    """Enumerate every ``(pkg, arch, suffix)`` variant the matrix exposes,
    returning per-binary metadata that the matrix_eval worker uses to
    drive its own ``nix-eval-jobs`` invocation.

    Filters by ``packages`` and ``archs`` if provided (each is an inclusion
    list — None means "all").

    ``sample_size`` / ``sample_seed`` are echoed back in the per-binary
    metadata so the matrix_eval worker on a secondary re-applies the
    deterministic sample with the same seed — the submitter never needs
    to know the sampled subset and the slow drv-instantiation never runs
    on the submit host. Suffixes are filtered against the support table
    and the known-bad-combo list *before* being emitted.

    The return shape is::

        {
          <pkg>: {
            "archs": [arch, ...],
            "suffixes_by_arch": {arch: [suffix, ...], ...},
            "sample_size": int,
            "sample_seed": str,
            "tier": int,
          },
          ...
        }
    """
    pkg_filter = set(packages) if packages else None
    arch_filter = set(archs) if archs else None

    # Scope the ``_meta`` eval the same way ``_drvPaths`` is scoped
    # below: when the operator asks for a subset, only force the
    # requested cells. The full ``_meta.<sys>`` eval iterates every
    # (pkg, arch, suffix) combo into JSON; with ~250 packages × 6
    # archs × ~280 combos per cell that's ~420k entries and tips
    # nix into a multi-minute eval that the daemon eventually
    # interrupts. Per-(pkg, arch) eval keeps each call ~instant.
    meta: dict = {}
    if pkg_filter is None or arch_filter is None:
        # Need a list of all (pkg, arch) labels first. ``builtins.attrNames``
        # is cheap because it doesn't force values.
        full_meta_keys = run_nix_eval(
            flake_ref,
            f"_meta.{sys_name}",
            run_subprocess=run_subprocess,
            apply="m: builtins.mapAttrs (_: a: builtins.attrNames a) m",
        )
        if not isinstance(full_meta_keys, dict):
            raise RuntimeError(
                f"_meta.{sys_name} is not a JSON object "
                f"(got {type(full_meta_keys).__name__})"
            )
        pkg_arch_pairs = [
            (pkg, arch)
            for pkg, archs_list in full_meta_keys.items()
            if pkg_filter is None or pkg in pkg_filter
            for arch in (archs_list if isinstance(archs_list, list) else [])
            if arch_filter is None or arch in arch_filter
        ]
    else:
        pkg_arch_pairs = [
            (pkg, arch) for pkg in pkg_filter for arch in arch_filter
        ]

    for pkg, arch in pkg_arch_pairs:
        try:
            cell = run_nix_eval(
                flake_ref,
                f"_meta.{sys_name}.{pkg}.{arch}",
                run_subprocess=run_subprocess,
            )
        except RuntimeError:
            # Cell may not exist (e.g. arch not configured for this
            # package); skip silently.
            continue
        if not isinstance(cell, dict):
            continue
        meta.setdefault(pkg, {})[arch] = cell

    # We have the per-(pkg, arch) meta but skip the drv-instantiation
    # step entirely. The matrix_eval worker on a secondary does the
    # slow ``nix-eval-jobs`` work itself, seeded with the same
    # ``sample_seed`` so the resulting variant set is deterministic
    # without the submitter ever forcing drv paths.
    from compiler_suit_runner.support_table import (  # noqa: PLC0415
        is_supported,
        load_support_table,
    )
    support = load_support_table()
    out: dict[str, dict] = {}
    for pkg, arch_attrs in sorted(meta.items()):
        if pkg_filter is not None and pkg not in pkg_filter:
            continue
        if not isinstance(arch_attrs, dict):
            continue
        per_arch: dict[str, list[str]] = {}
        for arch, suffix_attrs in sorted(arch_attrs.items()):
            if arch_filter is not None and arch not in arch_filter:
                continue
            if not isinstance(suffix_attrs, dict):
                continue
            kept_map: dict[str, dict] = {}
            for suffix, meta_entry in sorted(suffix_attrs.items()):
                if not isinstance(meta_entry, dict):
                    continue
                if not is_supported(
                    support, meta_entry.get("compiler", ""), arch
                ):
                    continue
                if is_known_bad_combo(meta_entry):
                    continue
                kept_map[suffix] = meta_entry
            # Pre-apply sampling here so manifests stay small enough
            # to transit the framework's ClusterMutation wire (a 8 MB
            # suffix list for 150 k+ variants causes the SSH tunnel
            # to reset before InitialAssignment arrives). The eval
            # worker then uses the already-sampled list directly
            # (variant_sample=None in payload → no re-sampling step).
            if sample_size > 0 and sample_seed and kept_map:
                kept_map = _sample_suffix_attrs(
                    kept_map,
                    arch=arch,
                    sample_size=sample_size,
                    seed=sample_seed,
                )
            if kept_map:
                per_arch[arch] = sorted(kept_map.keys())
        if per_arch:
            out[pkg] = {
                "archs": sorted(per_arch.keys()),
                "suffixes_by_arch": per_arch,
                # Signal to eval_worker that no re-sampling is needed:
                # suffixes are already the final sampled subset.
                # eval_worker skips the _meta lookup + re-sampling
                # step when sample_size is falsy.
                "sample_size": 0,
                "sample_seed": sample_seed,
                "tier": _tier_from_pkg(pkg),
            }
    return out


# ---------------------------------------------------------------------------
# Toolchain enumeration
# ---------------------------------------------------------------------------


def eval_toolchain_drvs(
    flake_ref: str,
    sys_name: str,
    pairs: tuple[tuple[str, str], ...],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[tuple[str, str], str]:
    """Evaluate the drv path for each ``(arch, compiler)`` toolchain.

    Without this, phase-2 toolchain manifests would only carry the
    flake attribute path (``_crossToolchainMap.<sys>.<arch>.<compiler>``)
    and the build_worker on a SLURM secondary would try to resolve it
    against ``flake_ref="."`` which is the container's working dir
    (no flake.nix shipped to the secondary). The drv path lets the
    worker substitute / build via ``nix build <drv>^*`` instead.

    Returns a dict ``(arch, compiler) -> drv_path``; entries with
    failed evals are silently dropped (the manifest then falls back
    to flake-attr resolution, which will fail loudly on the secondary
    and surface in the build-failure log).
    """
    if not pairs:
        return {}
    runner = run_subprocess or _default_run_subprocess

    # ``nix-eval-jobs`` walks each (arch, compiler) leaf independently
    # with per-entry isolation: a single broken combo (e.g. gcc5 +
    # mips64el throws ``cross-compiler not available in nixpkgs-18.03``)
    # produces an "error" line for that one entry while every other
    # entry still yields its drvPath. The previous one-giant-``nix
    # eval --apply`` call had the opposite shape — one bad combo
    # crashed the whole evaluation and returned 0/N resolved.
    #
    # ``--force-recurse`` makes nix-eval-jobs descend through the
    # nested ``arch.compiler`` levels of ``_crossToolchainMap.<sys>``;
    # without it the walker would stop at ``arch`` and never see the
    # individual compiler entries.
    if shutil.which("nix-eval-jobs") is None:
        return _eval_toolchain_drvs_fallback(
            flake_ref, sys_name, pairs, run_subprocess=runner
        )

    requested = {(arch, compiler) for arch, compiler in pairs}
    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{flake_ref}#_crossToolchainMap.{sys_name}",
        "--force-recurse",
        "--workers",
        "16",
    ]
    stdout, _stderr, rc = runner(argv)
    if rc != 0:
        # nix-eval-jobs returns rc!=0 only on catastrophic failures
        # (no flake found, etc.). Per-entry errors are reported on
        # stdout as JSONL ``{"attr": ..., "error": ...}`` lines and
        # don't affect rc. Fallback to the inline path so callers
        # still get something.
        return _eval_toolchain_drvs_fallback(
            flake_ref, sys_name, pairs, run_subprocess=runner
        )

    out: dict[tuple[str, str], str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        attr_path = entry.get("attrPath")
        drv = entry.get("drvPath")
        if (
            isinstance(attr_path, list)
            and len(attr_path) == 2
            and isinstance(drv, str)
            and drv.endswith(".drv")
        ):
            pair = (attr_path[0], attr_path[1])
            if pair in requested:
                out[pair] = drv
    return out


def _eval_toolchain_drvs_fallback(
    flake_ref: str,
    sys_name: str,
    pairs: tuple[tuple[str, str], ...],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[tuple[str, str], str]:
    """One ``nix eval`` per (arch, compiler) — used when
    ``nix-eval-jobs`` isn't on PATH (test harness, hermetic
    environments). Slower than the parallel evaluator but still
    per-entry isolated.
    """
    runner = run_subprocess or _default_run_subprocess
    safe = re.compile(r"^[A-Za-z0-9._-]+$")
    out: dict[tuple[str, str], str] = {}
    for arch, compiler in pairs:
        if not safe.match(arch) or not safe.match(compiler):
            continue
        argv = [
            "nix",
            "eval",
            "--extra-experimental-features",
            "nix-command flakes",
            "--raw",
            f"{flake_ref}#_crossToolchainMap.{sys_name}.{arch}.{compiler}.drvPath",
        ]
        stdout, _stderr, rc = runner(argv)
        if rc != 0:
            continue
        drv = stdout.decode("utf-8", errors="replace").strip()
        if drv.endswith(".drv"):
            out[(arch, compiler)] = drv
    return out


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

    # Filter combos by table.md before they enter the toolchain-specs
    # list: combos marked FAIL or n/a would otherwise be dispatched to
    # secondaries as phase-2 toolchain tasks, fail at build time
    # (because their drvPath couldn't be evaluated either, see
    # ``eval_toolchain_drvs``), and consume worker slots producing
    # nothing.
    from compiler_suit_runner.support_table import (  # noqa: PLC0415
        is_supported,
        load_support_table,
    )
    support = load_support_table()

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
            if not isinstance(label, str) or not label:
                continue
            if not is_supported(support, label, arch):
                continue
            pairs.append((arch, label))
    pairs.sort()
    return tuple(pairs)


def enumerate_toolchains_only(
    flake_ref: str,
    sys_name: str,
    *,
    archs: Optional[list[str]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[tuple[tuple[str, str], ...], dict[tuple[str, str], str]]:
    """Submitter-side toolchain-only preflight.

    Resolves the (arch, compiler) toolchain set and their drv paths
    without touching any variant matrix data. This is the fast surface
    Phase -1 bootstrap uses to seed the cluster with toolchain drvs
    before matrix_eval tasks fire on secondaries.

    Returns ``(pairs, drv_paths)`` where ``pairs`` is the sorted
    ``(arch, compiler)`` tuple list and ``drv_paths`` maps each pair to
    its resolved ``.drv`` path. Entries with failed drv evaluation are
    dropped from ``drv_paths`` but stay in ``pairs`` so the caller can
    decide what to do with them (e.g. log a warning, fall back to
    flake-attr resolution downstream).
    """
    pairs = enumerate_toolchains(
        flake_ref, sys_name, archs=archs, run_subprocess=run_subprocess,
    )
    if not pairs:
        return pairs, {}
    drv_paths = eval_toolchain_drvs(
        flake_ref, sys_name, pairs, run_subprocess=run_subprocess,
    )
    return pairs, drv_paths


# ---------------------------------------------------------------------------
# Batched ``nix path-info`` helper
# ---------------------------------------------------------------------------


def path_info_batch(
    drvs: list[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, str]:
    """Resolve outpaths for many ``.drv`` paths in **one** subprocess call.

    Calls ``nix path-info <drv1> <drv2> ... <drvN> --json`` once and
    parses the JSON result into a ``{drv: outpath}`` dict. Drvs whose
    outpath isn't realised locally are *absent* from the returned dict
    (callers should treat the missing key as "not in local store").
    Empty input returns ``{}`` without spawning nix.

    This replaces the legacy N-subprocess-calls loop (one
    ``nix path-info`` per drv) which had per-fork startup overhead in
    the hundreds-of-milliseconds range — a few minutes wall on 300+
    toolchains. Batching collapses that to a single nix invocation.

    Both nix's ``--json`` output shapes are handled:

    - Modern (nix ≥ 2.19): a JSON object keyed by the path argument
      (drv path) with the output paths nested inside.
    - Legacy: a JSON array of entries each carrying a ``"path"`` field
      pointing at the output and a separate ``"valid"`` flag.

    The function uses the queried drv path's ``^*`` form so nix resolves
    the drv's outputs rather than treating the drv itself as the path.
    """
    drvs = [d for d in drvs if isinstance(d, str) and d.endswith(".drv")]
    if not drvs:
        return {}
    runner = run_subprocess or _default_run_subprocess
    # The ``^*`` suffix tells ``nix path-info`` to resolve every output
    # of the derivation (e.g. ``out``, ``lib``, ``bin``). Without it the
    # subcommand looks for the drv path itself in the store, which
    # always exists when the drv was written, defeating the existence
    # probe semantics callers want.
    argv: list[str] = [
        "nix",
        "path-info",
        "--extra-experimental-features",
        "nix-command flakes",
        "--json",
    ]
    argv.extend(f"{d}^*" for d in drvs)
    stdout, _stderr, rc = runner(argv)
    if rc != 0:
        return {}
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}

    out: dict[str, str] = {}
    # Modern shape: {drv_or_output_path: {...}, ...}
    if isinstance(payload, dict):
        for key, entry in payload.items():
            if not isinstance(key, str):
                continue
            # ``key`` is typically the output path (not the drv); the
            # drv-of-origin lives in ``deriver`` on each entry.
            if isinstance(entry, dict):
                deriver = entry.get("deriver")
                if isinstance(deriver, str) and deriver.endswith(".drv"):
                    out[deriver] = key
                    continue
            # Fallback: the key itself is the drv (some nix variants).
            if key.endswith(".drv"):
                out[key] = key
    elif isinstance(payload, list):
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            valid = entry.get("valid", True)
            if not valid:
                continue
            path = entry.get("path")
            deriver = entry.get("deriver")
            if isinstance(deriver, str) and isinstance(path, str):
                out[deriver] = path
    return out


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
    """Composite call: toolchain enumeration only.

    Since distributed-eval is the only supported mode, the submitter
    no longer instantiates variant drvs locally — matrix_eval workers
    on secondaries do that work. The returned :class:`PreflightResult`
    therefore carries an empty ``variants`` tuple and an empty
    ``toolchain_drvs`` set; only ``toolchain_specs`` (the
    ``(arch, compiler)`` pairs the matrix considers valid) is populated.
    ``common_dep_drvs`` is left empty here — the cluster's eval workers
    populate it dynamically at runtime.

    ``sample_size`` / ``sample_seed`` / ``packages`` / ``archs`` are
    accepted for API compatibility (cli's ``preflight`` debug subcommand
    forwards them) but are not consulted by this composite — they apply
    only to per-binary metadata that the matrix_eval worker generates.
    Use :func:`enumerate_variants` directly to inspect the per-binary
    metadata shape on the submitter.
    """
    del packages, sample_size, sample_seed  # accepted for API stability
    toolchain_specs = enumerate_toolchains(
        flake_ref,
        sys_name,
        archs=archs,
        run_subprocess=run_subprocess,
    )
    return PreflightResult(
        sys_name=sys_name,
        variants=(),
        toolchain_specs=toolchain_specs,
        common_dep_drvs=(),
        toolchain_drvs=frozenset(),
    )


def filter_existing_variants(
    variants: tuple[VariantSpec, ...],
    *,
    dataset_dir,
) -> tuple[tuple[VariantSpec, ...], int]:
    """Drop variants whose sidecar already records the same ``label``.

    Returns ``(remaining_variants, skipped_count)``. The output layout
    is per-package: phase-3 build_worker writes the ELF folder + sidecar
    JSON at ``<dataset_dir>/<pkg>/<variant_dir>/<elf>`` and
    ``<dataset_dir>/<pkg>/<variant_dir>.json``.

    Match is on the ``label`` field of each sidecar, NOT the filename.
    The filename is a sha256-derived short hash of label; matching the
    hash works as long as the hash function never changes, but reading
    ``label`` directly is the canonical precise-flags identifier and
    survives short-name refactors. A bonus consequence: a variant
    written under a different short-name scheme is still recognised as
    "we already have this combo", so we never rebuild what's already
    on disk.

    Sidecars per pkg are scanned at most once each (O(existing+todo)
    rather than per-variant ``stat``).
    """
    import pathlib

    dataset_dir = pathlib.Path(dataset_dir)
    if not dataset_dir.is_dir():
        return variants, 0

    # Lazy: only scan a pkg's subdir once we encounter a variant
    # claiming that pkg. Variants without ``pkg`` fall back to the
    # legacy flat-layout scan in ``dataset_dir`` itself.
    labels_by_pkg: dict[str, set[str]] = {}

    def _labels_for(pkg_dir: pathlib.Path) -> set[str]:
        seen: set[str] = set()
        try:
            entries = list(pkg_dir.iterdir())
        except OSError:
            return seen
        for entry in entries:
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                with entry.open("rb") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            label = data.get("label") if isinstance(data, dict) else None
            if isinstance(label, str) and label:
                seen.add(label)
        return seen

    remaining: list[VariantSpec] = []
    skipped = 0
    for variant in variants:
        pkg = variant.get("pkg") if isinstance(variant, dict) else None
        label = variant.get("label") if isinstance(variant, dict) else None
        if not isinstance(label, str) or not label:
            remaining.append(variant)
            continue
        # Cache lookup; fill the cache the first time we see each pkg.
        cache_key = pkg if isinstance(pkg, str) and pkg else ""
        if cache_key not in labels_by_pkg:
            scan_dir = (
                dataset_dir / pkg
                if isinstance(pkg, str) and pkg
                else dataset_dir
            )
            labels_by_pkg[cache_key] = _labels_for(scan_dir)
        if label in labels_by_pkg[cache_key]:
            skipped += 1
        else:
            remaining.append(variant)
    return tuple(remaining), skipped


# ---------------------------------------------------------------------------
# Local toolchain validation + realisation
# ---------------------------------------------------------------------------


class PreflightError(RuntimeError):
    """Raised when preflight detects a state the dispatch cannot recover
    from on its own — e.g. missing toolchains while remote-build is off.

    Subclass of :class:`RuntimeError` so callers that already catch the
    broader exception keep working; the dedicated type lets the CLI
    surface a clean ``error:`` line without a traceback for known
    failure modes.
    """


def check_toolchains_locally(
    toolchain_drvs: frozenset[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> frozenset[str]:
    """Return the subset of toolchain drvs not yet realised locally.

    For each drv path in ``toolchain_drvs``, runs
    ``nix path-info <drv>^*`` (one invocation per drv) and treats a
    non-zero exit as "outputs are missing". ``^*`` expands to every
    output of the drv, so a partially-realised toolchain (e.g. ``lib``
    present but ``out`` missing) is correctly flagged as missing.

    Returns a :class:`frozenset` so the caller can compare cheaply
    against the full toolchain set without re-allocating.
    """
    runner = run_subprocess or _default_run_subprocess
    missing: set[str] = set()
    for drv in toolchain_drvs:
        if not drv:
            continue
        cmd = [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "path-info",
            f"{drv}^*",
        ]
        _stdout, _stderr, rc = runner(cmd)
        if rc != 0:
            missing.add(drv)
    return frozenset(missing)


def query_initial_toolchain_placement(
    toolchain_drvs: Iterable[str],
    drv_outpaths: dict[str, str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, list[str]]:
    """Return the initial placement seed for each toolchain outpath.

    For every outpath the submitter's local nix store already has (it
    built or substituted the toolchain ahead of dispatch), seed
    ``["submitter"]`` as the first holder. For outpaths the submitter
    lacks, seed ``[]`` — no preference until the first secondary
    fetches via cascade.

    Seeding ``"submitter"`` lets the variant's first scheduling
    decision (Q2 wire-in's static ``preferred_secondaries`` channel)
    pick a secondary that can fetch the toolchain from the submitter's
    harmonia listener immediately, instead of waiting for a remote
    holder to appear.

    The local-store presence probe is a single batched
    ``nix-store --check-validity --print-invalid <outpaths...>`` call:
    that subcommand returns rc=0 and prints the invalid (i.e.
    not-locally-realised) subset to stdout. The complement of the
    invalid set is "locally present" — we seed ``["submitter"]`` for
    those and ``[]`` for the rest.

    The returned dict's keys are the outpaths derived by looking up
    each drv in *drv_outpaths*. Drvs missing from ``drv_outpaths`` (or
    mapped to a falsy outpath) are silently dropped — the caller knows
    their drv→outpath table was incomplete and the omission is
    equivalent to "no placement record for that toolchain".

    Empty input dict → empty output dict, no subprocess call.

    ``run_subprocess`` is the same injection seam every preflight
    helper exposes: tests stub it; production code lets it default to
    :func:`_default_run_subprocess`.
    """
    outpaths: list[str] = []
    seen: set[str] = set()
    for drv in toolchain_drvs:
        if not isinstance(drv, str) or not drv:
            continue
        op = drv_outpaths.get(drv)
        if not isinstance(op, str) or not op:
            continue
        if op in seen:
            continue
        seen.add(op)
        outpaths.append(op)

    if not outpaths:
        return {}

    runner = run_subprocess or _default_run_subprocess
    # ``nix-store --check-validity --print-invalid`` is the canonical
    # batched local-store presence probe: rc=0 always (unless the
    # subcommand itself is unavailable), stdout = newline-delimited
    # list of the queried paths that are NOT valid in the local store.
    # Complement → locally present → submitter is a valid initial
    # holder for the path.
    argv: list[str] = [
        "nix-store",
        "--check-validity",
        "--print-invalid",
        *outpaths,
    ]
    stdout, _stderr, rc = runner(argv)
    if rc != 0:
        # Probe itself failed (nix-store missing, daemon down, etc.).
        # Conservative fallback: nobody known to hold any outpath yet;
        # cascade will populate placement later.
        return {op: [] for op in outpaths}

    invalid: set[str] = {
        line.strip()
        for line in stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    }
    return {
        op: ([] if op in invalid else ["submitter"])
        for op in outpaths
    }


def build_toolchains_locally(
    toolchain_drvs: frozenset[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> None:
    """Realise every toolchain drv in the local nix store.

    Calls ``nix build <drv>^* --no-link`` per drv (sequentially —
    nix handles internal parallelism). On the first non-zero exit,
    raises :class:`PreflightError` with the stderr snippet so the
    operator can see why the build aborted.

    ``--no-link`` avoids polluting the cwd with ``result`` symlinks;
    the build artefacts land in the store regardless and the
    placement gossip will be written from the first secondary that
    pulls them.
    """
    runner = run_subprocess or _default_run_subprocess
    for drv in sorted(toolchain_drvs):
        if not drv:
            continue
        cmd = [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "build",
            f"{drv}^*",
            "--no-link",
        ]
        _stdout, stderr, rc = runner(cmd)
        if rc != 0:
            decoded = stderr.decode("utf-8", errors="replace").strip()
            raise PreflightError(
                f"local toolchain build failed for {drv} (rc={rc}): "
                f"{decoded[-1000:]}"
            )

"""Local pre-flight: discover packages + cross toolchains via ``nix eval``.

The submitter only enumerates the cheap, top-level shape of the matrix
locally: the package list under ``dataset.<sys>`` (via ``attrNames``)
and the leaf drv paths of ``_crossToolchainMap.<sys>`` (via
``nix-eval-jobs``, with a single-call fallback). Per-suffix variant
enumeration — the expensive part — runs on the cluster inside the
matrix_eval worker, which evaluates ``_meta.<sys>.<pkg>.<arch>`` one
arch at a time via ``_eval_meta_for_arch`` and applies
``is_known_bad_combo`` there.

The output is a :class:`PreflightResult` that downstream code (CLI,
manifest_gen) feeds to ``emit_all_manifests``.

Subprocess invocation is dependency-injected via ``run_subprocess`` so
unit tests stay hermetic — the real flake never has to be evaluated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
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

    ``toolchain_aggregate_drv`` is the ``toolchains`` wrapper drv path
    built by :func:`enumerate_toolchains_only` from the sorted leaf
    toolchain drv list. It collapses N per-compiler leaves into one
    handle the downstream phases (sum-drv assembly, dependency_graph
    planner) consume directly. Empty string when no leaves resolved.
    """

    sys_name: str
    variants: tuple[VariantSpec, ...]
    toolchain_specs: tuple[tuple[str, str], ...]
    common_dep_drvs: tuple[tuple[str, str], ...]
    toolchain_drvs: frozenset[str]
    toolchain_aggregate_drv: str = ""


# ---------------------------------------------------------------------------
# Subprocess injection
# ---------------------------------------------------------------------------


# ``run_subprocess`` accepts argv (list[str]) plus an optional
# ``input`` kwarg of bytes piped to stdin, and returns a tuple of
# (stdout_bytes, stderr_bytes, returncode). The ``input`` kwarg
# mirrors the signature in :mod:`workers.eval_worker` so the same
# fakes can be reused; preflight callers never pass ``input=`` and
# the kwarg is ignored when ``None``.
RunSubprocess = Callable[..., tuple[bytes, bytes, int]]


def _default_run_subprocess(
    argv: list[str],
    *,
    input: Optional[bytes] = None,
) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
        input=input,
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
    """Enumerate every ``(pkg, arch)`` cell the matrix exposes.

    The submitter only discovers the package list and echoes the
    operator's arch filter. Per-suffix enumeration AND the
    ``is_supported`` + ``is_known_bad_combo`` filter run on the
    cluster inside the matrix_eval worker (one
    ``_meta.<sys>.<binary>.<arch>`` call per arch via
    ``_eval_meta_for_arch``). ``nix-eval-jobs`` silently swallows any
    combos the predicate misses.

    Return shape::

        {
          <pkg>: {
            "archs": [arch, ...],
            "sample_size": int,
            "sample_seed": str,
            "tier": int,
          },
          ...
        }
    """
    pkg_filter = set(packages) if packages else None
    arch_filter = set(archs) if archs else None

    all_pkgs_raw = run_nix_eval(
        flake_ref,
        f"dataset.{sys_name}",
        run_subprocess=run_subprocess,
        apply="m: builtins.attrNames m",
    )
    if not isinstance(all_pkgs_raw, list):
        raise RuntimeError(
            f"dataset.{sys_name} attrNames is not a JSON array "
            f"(got {type(all_pkgs_raw).__name__})"
        )
    all_pkgs = [p for p in all_pkgs_raw if isinstance(p, str)]

    if arch_filter is None:
        per_pkg_archs_raw = run_nix_eval(
            flake_ref,
            f"dataset.{sys_name}",
            run_subprocess=run_subprocess,
            apply="m: builtins.mapAttrs (_: a: builtins.attrNames a) m",
        )
        if not isinstance(per_pkg_archs_raw, dict):
            raise RuntimeError(
                f"dataset.{sys_name} per-pkg attrNames is not a JSON object "
                f"(got {type(per_pkg_archs_raw).__name__})"
            )
    else:
        per_pkg_archs_raw = None

    out: dict[str, dict] = {}
    for pkg in sorted(all_pkgs):
        if pkg_filter is not None and pkg not in pkg_filter:
            continue
        if arch_filter is not None:
            pkg_archs = sorted(arch_filter)
        else:
            raw_archs = per_pkg_archs_raw.get(pkg) if per_pkg_archs_raw else None
            if not isinstance(raw_archs, list):
                continue
            pkg_archs = sorted(a for a in raw_archs if isinstance(a, str))
        if not pkg_archs:
            continue
        out[pkg] = {
            "archs": pkg_archs,
            "sample_size": int(sample_size or 0),
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
    sys_name: str = "x86_64-linux",
    *,
    archs: Optional[list[str]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[
    tuple[tuple[str, str], ...],
    dict[tuple[str, str], str],
    str,
]:
    """Submitter-side toolchain-only preflight.

    Resolves the (arch, compiler) toolchain set and their drv paths
    without touching any variant matrix data, then assembles the leaf
    drv paths into a single ``toolchains`` aggregate wrapper drv via
    :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths`.
    This is the fast surface Phase -1 bootstrap uses to seed the
    cluster with toolchain drvs before matrix_eval tasks fire on
    secondaries; downstream phases consume the aggregate as a single
    handle instead of N leaves.

    Returns ``(pairs, drv_paths, aggregate_drv)`` where:

    - ``pairs`` is the sorted ``(arch, compiler)`` tuple list.
    - ``drv_paths`` maps each pair to its resolved ``.drv`` path.
      Entries with failed drv evaluation are dropped from ``drv_paths``
      but stay in ``pairs`` so the caller can decide what to do with
      them (e.g. log a warning, fall back to flake-attr resolution
      downstream).
    - ``aggregate_drv`` is the ``toolchains`` wrapper drv path built
      from ``sorted(drv_paths.values())`` — sorted for determinism so
      the same leaf set always yields the same wrapper-drv hash. When
      no leaves resolve, the aggregate is an empty string.

    ``sys_name`` defaults to ``"x86_64-linux"`` for callers that do
    not yet thread a system literal; the submitter CLI passes its own
    ``--system`` value through (the ``dependency_graph_worker`` CLI
    accepts ``--sys-name`` as a back-compat alias).
    """
    pairs = enumerate_toolchains(
        flake_ref, sys_name, archs=archs, run_subprocess=run_subprocess,
    )
    if not pairs:
        return pairs, {}, ""
    drv_paths = eval_toolchain_drvs(
        flake_ref, sys_name, pairs, run_subprocess=run_subprocess,
    )
    leaf_drvs = sorted(d for d in drv_paths.values() if d)
    if not leaf_drvs:
        return pairs, drv_paths, ""
    # Imported lazily so the import cost is paid only when the
    # submitter actually builds the aggregate (the toolchain-only
    # composite is the sole call site).
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    aggregate_drv = make_wrapper_drv_from_paths(
        drvs=leaf_drvs,
        name="toolchains",
        system=sys_name,
    )
    return pairs, drv_paths, aggregate_drv


# ---------------------------------------------------------------------------
# Toolchain archive export (toolchain dedup pre-flight step)
# ---------------------------------------------------------------------------


TOOLCHAIN_ARCHIVE_NAME = "toolchains.drv.archive"
TOOLCHAIN_COMMON_ARCHIVE_NAME = "toolchains.common.archive"


def toolchain_id_for_outpath(outpath: str) -> str:
    """Derive a stable, filesystem-safe id from a realized toolchain out-path.

    The Nix store-hash is the first ``-``-delimited token of the basename
    (e.g. ``/nix/store/abc123xyz-gcc-14.2.0`` → ``"abc123xyz"``).  This
    is the canonical 32-char base-32 hash component that uniquely
    identifies the derivation output — it is stable across re-runs with
    the same inputs, always filesystem-safe (``[a-z0-9]``), and short
    enough to compose cleanly into archive filenames.

    The same derivation is used by workers to compute the per-toolchain
    archive name from ``payload["toolchain_outpath"]``:
    ``toolchains.<toolchain_id_for_outpath(outpath)>.out.archive``.

    Raises :class:`ValueError` for paths that don't look like
    ``/nix/store/<hash>-<name>`` (empty string, no ``-`` in basename).
    """
    basename = outpath.rsplit("/", 1)[-1]
    parts = basename.split("-", 1)
    if not parts[0]:
        raise ValueError(
            f"toolchain_id_for_outpath: cannot extract store hash from {outpath!r}"
        )
    return parts[0]


def toolchain_delta_archive_name(outpath: str) -> str:
    """Return the per-toolchain delta archive filename for ``outpath``.

    Format: ``toolchains.<id>.out.archive`` where ``<id>`` is derived via
    :func:`toolchain_id_for_outpath`.  Workers use this to find the
    archive on the shared mount at
    ``<matrix_eval_out_dir>/<name>``.
    """
    return f"toolchains.{toolchain_id_for_outpath(outpath)}.out.archive"


@dataclasses.dataclass
class ToolchainSplit:
    """Result of :func:`compute_toolchain_split`.

    ``common_paths`` is the intersection of all toolchain closures —
    the shared runtime (glibc, libgcc, …).  It forms a self-contained
    importable closure (by the downward-closed intersection property).

    ``delta_paths`` maps each toolchain out-path to the tuple of store
    paths in its closure that are NOT in ``common_paths``.  Each delta
    references paths already in ``common_paths``, so a worker imports
    COMMON first then each delta to reconstruct the full toolchain.
    """

    common_paths: frozenset[str]
    delta_paths: dict[str, tuple[str, ...]]


def compute_toolchain_split(
    toolchain_out_paths: list[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> ToolchainSplit:
    """Compute COMMON + per-toolchain deltas from realized out-paths.

    For each toolchain out-path ``Ti``:
      closure(Ti) = ``nix-store --query --requisites Ti``

    COMMON = intersection of all closure(Ti).  By the downward-closed
    property of Nix closures the intersection is itself a complete
    closure (all deps of every COMMON path are also in COMMON) so it
    is directly importable.

    delta(Ti) = closure(Ti) − COMMON.  Each delta imports cleanly after
    COMMON is present because every dependency of a delta path is
    either in COMMON or in the same delta.

    Raises :class:`RuntimeError` if any ``nix-store --query
    --requisites`` call fails (a bad out-path would produce a useless
    archive so callers must surface the error).
    """
    if not toolchain_out_paths:
        raise RuntimeError(
            "compute_toolchain_split: no toolchain out-paths supplied"
        )
    runner = run_subprocess or _default_run_subprocess
    # Keep each closure as the ORDERED requisites list: ``nix-store
    # --query --requisites`` emits paths in topological (dependency-first)
    # order, which ``nix-store --import`` REQUIRES — a path's references
    # must already be registered when it is imported. The delta archives
    # are exported with ``export_closure_exact`` (no requisites re-query),
    # so we must preserve this order here; sorting alphabetically would
    # break intra-delta references at import time.
    closures: dict[str, list[str]] = {}
    for outpath in toolchain_out_paths:
        stdout, stderr, rc = runner(
            ["nix-store", "--query", "--requisites", outpath]
        )
        if rc != 0:
            raise RuntimeError(
                f"compute_toolchain_split: requisites query failed for "
                f"{outpath!r}: "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        lines = [l for l in stdout.decode("utf-8", errors="replace").splitlines() if l.strip()]
        if not lines:
            raise RuntimeError(
                f"compute_toolchain_split: requisites query returned no paths "
                f"for {outpath!r}"
            )
        closures[outpath] = lines

    # Intersection = COMMON (order-agnostic; the common archive re-queries
    # requisites at export so its own topological order is recovered).
    common: frozenset[str] = frozenset(next(iter(closures.values())))
    for lines in closures.values():
        common = common & frozenset(lines)

    # delta = closure − COMMON per toolchain, PRESERVING topological order.
    delta_paths: dict[str, tuple[str, ...]] = {
        outpath: tuple(p for p in lines if p not in common)
        for outpath, lines in closures.items()
    }
    return ToolchainSplit(common_paths=common, delta_paths=delta_paths)


def export_toolchain_archive(
    toolchain_aggregate_drv: str,
    out_dir: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> pathlib.Path:
    """Export the toolchain aggregate's closure into ``toolchains.drv.archive``.

    The submit pre-flight produces ONE toolchain archive per dispatch so
    the per-binary ``matrix-<binary>.drv.archive`` exports can subtract
    the toolchain closure and ship only the binary-specific diff. The
    landing file is ``<out_dir>/toolchains.drv.archive``; consumers
    (dependency_graph_worker / build_worker) import it FIRST, then each
    per-binary diff archive.

    The seed is the toolchain aggregate ``.drv`` path itself — the same
    closure space as the matrix aggregate (which carries the toolchain
    aggregate as inputDrv #0), so ``requisites(seed)`` here is exactly
    the set the matrix export subtracts on the worker.

    Delegates to :func:`workers.build_compilers_worker.export_closure`
    (requisites → ``nix-store --export`` → atomic ``.tmp`` + ``os.replace``).
    The helper is imported lazily so the submit pre-flight only pays the
    cross-worker import cost when toolchain dedup is actually exercised.

    Returns the written archive path. Raises :class:`RuntimeError` on
    any export failure (a missing toolchain archive makes every diff
    archive un-importable, so the caller must fail fast).
    """
    if not toolchain_aggregate_drv:
        raise RuntimeError(
            "export_toolchain_archive: empty toolchain_aggregate_drv"
        )
    # Lazy import: the cross-worker dependency is only needed when the
    # submitter actually exports the toolchain archive (mirrors the
    # lazy ``make_sum_drv`` import in ``enumerate_toolchains_only``).
    from compiler_suit_runner.workers.build_compilers_worker import (  # noqa: PLC0415
        export_closure,
    )

    archive_path = out_dir / TOOLCHAIN_ARCHIVE_NAME
    ok, req_stderr, exp_stderr = export_closure(
        archive_path,
        [toolchain_aggregate_drv],
        run_subprocess=run_subprocess,
    )
    if not ok:
        raise RuntimeError(
            "export_toolchain_archive: nix-store export of "
            f"{toolchain_aggregate_drv!r} failed: requisites_stderr="
            + req_stderr.decode("utf-8", errors="replace").strip()
            + " export_stderr="
            + exp_stderr.decode("utf-8", errors="replace").strip()
        )
    return archive_path


def export_toolchain_split(
    split: ToolchainSplit,
    out_dir: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, pathlib.Path]:
    """Export COMMON + per-toolchain delta archives from a :class:`ToolchainSplit`.

    Writes:
      * ``<out_dir>/toolchains.common.archive`` — the shared path set.
      * ``<out_dir>/toolchains.<id>.out.archive`` for each toolchain,
        containing only the delta paths (i.e. NOT requisites-expanded
        again — the split already has the exact path sets).

    The common archive uses :func:`export_closure` (with requisites
    expansion) so the correct topological order is preserved by nix-store.
    Each delta archive uses :func:`export_closure_exact` — a direct
    ``nix-store --export`` of the pre-computed exact path set — to avoid
    re-pulling COMMON paths back in.

    Returns a ``{archive_name: path}`` dict for every archive written.
    Raises :class:`RuntimeError` on any export failure; callers treat
    this as a soft failure (warn + skip upload).

    Note: the planned evolution is to overlap these uploads with build
    dispatch (setup-task kind in the framework), so workers can start
    building as each per-toolchain archive lands rather than waiting for
    all uploads to finish.  For now the upload remains a submit-time
    step before dispatch.
    """
    from compiler_suit_runner.workers.build_compilers_worker import (  # noqa: PLC0415
        export_closure,
        export_closure_exact,
    )

    written: dict[str, pathlib.Path] = {}

    # Common archive: use the full requisites path so nix-store --export
    # emits paths in a valid topological (dependency-first) order.
    common_sorted = sorted(split.common_paths)
    common_archive = out_dir / TOOLCHAIN_COMMON_ARCHIVE_NAME
    ok, req_stderr, exp_stderr = export_closure(
        common_archive,
        common_sorted,
        run_subprocess=run_subprocess,
    )
    if not ok:
        raise RuntimeError(
            "export_toolchain_split: failed to export common archive: "
            "requisites_stderr="
            + req_stderr.decode("utf-8", errors="replace").strip()
            + " export_stderr="
            + exp_stderr.decode("utf-8", errors="replace").strip()
        )
    written[TOOLCHAIN_COMMON_ARCHIVE_NAME] = common_archive

    # Per-toolchain delta archives: exact paths only (no requisites re-expansion).
    for outpath, delta in split.delta_paths.items():
        if not delta:
            # Toolchain's closure is entirely COMMON — no delta to ship.
            continue
        name = toolchain_delta_archive_name(outpath)
        archive = out_dir / name
        ok, exp_stderr = export_closure_exact(
            archive,
            list(delta),
            run_subprocess=run_subprocess,
        )
        if not ok:
            raise RuntimeError(
                f"export_toolchain_split: failed to export delta archive "
                f"for {outpath!r} ({name}): export_stderr="
                + exp_stderr.decode("utf-8", errors="replace").strip()
            )
        written[name] = archive

    return written


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
    """Return the subset of toolchain drvs whose ``out`` is not realised
    locally.

    For each drv path in ``toolchain_drvs``, runs
    ``nix path-info <drv>^out`` (one invocation per drv) and treats a
    non-zero exit as "the ``out`` output is neither locally realised
    nor available via a configured substituter".

    ``^out`` (not ``^*``) is the right probe for cluster dispatch
    readiness: secondaries fetch the toolchain's ``out`` via harmonia
    federation (from the submitter or cache.nixos.org), and that is
    the only output a variant build needs at compile time. Auxiliary
    outputs (``info``, ``man``, ``debug``) are documentation / debug
    data and may legitimately exist only in the binary cache, never
    locally — requiring them locally would force the operator to
    pre-fetch every doc tarball before dispatch, which is not the
    documented operator workflow.

    Substituter availability is intentionally accepted as "present"
    because the cluster's secondaries can fetch via the same
    substituter the submitter consults — so if ``nix path-info``
    succeeds on the submitter (locally or via cache) the cluster will
    succeed too. The only signal we want to gate on is "neither
    local nor reachable via any configured substituter", which is the
    actual failure mode for cluster dispatch.

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
            f"{drv}^out",
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

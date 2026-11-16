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
    *, compiler_id: str, arch: str, optimization: str, full_label: str,
    package_identity: Optional[str] = None, variant_drv: Optional[str] = None,
) -> str:
    """Return the short filesystem identifier for a variant.

    Format: ``<compiler>_<arch>_<opt>_<hash>``. New release records use
    16 hex digits over label, package identity and actual derivation;
    legacy records retain their 8-digit label hash. The same
    variant always maps to the same filename — re-running with new
    flag dimensions added to the matrix doesn't shift previously-built
    tarballs around. Filesystem-unsafe characters are scrubbed from
    the leading fields (compilers like ``gcc-old-4_4`` keep their
    hyphens / underscores; nothing else is currently problematic).
    """
    identity = json.dumps([full_label, package_identity, variant_drv], separators=(",", ":")) if package_identity else full_label
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16 if package_identity else 8]
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
    no_eval_cache: bool = False,
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
    if no_eval_cache:
        argv.append("--no-eval-cache")
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


# ---------------------------------------------------------------------------
# Deterministic-invalid variant exclusions (confirmed from failure logs,
# cross-checked against 12,421 actual successes — only combos with zero
# successes are listed).
#
# Rule A: bzip2 + clang 3.{4-8} + arch != x86_64
#   clang 3.x mis-parses the BZ_EXTERN ``__attribute__`` form in bzlib.h
#   for cross targets. Fails at every compile attempt; fixed in clang 4.
#   x86_64 uses a different wrapper path that survives.
#
# Rule C1: xz + clang 3.9 (all arches)
#   xz configure passes -Werror; clang 3.9 aborts on a CFLAGS-ordering
#   edge case that mis-detects the compiler. Fails deterministically.
#
# Rule C2: xz + stackclash hardening + clang 11..17 + specific cross arches
#   Same xz -Werror abort as C1 but triggered by clang 11-17 emitting a
#   stack-clash-protection diagnostic on specific cross targets.
#
# Rule D2/E: staticpie + arch in {ppc64,ppc32,mipsel,mips64el,armv7l-hf}
#   Cross sysroot for these arches lacks rcrt1.o / Scrt1.o.
#   i686 is EXCLUDED — cross-check found lz4+i686+staticpie succeeds.
#
# Rule D3: zerocallregs hardening + arch in {ppc64,armv7l-hf,mipsel,mips64el}
#          AND family=clang
#   Clang's -fzero-call-used-regs=used-gpr fails to link on these cross
#   arches. GCC zerocallregs works there — rule is clang-only.
# ---------------------------------------------------------------------------
_BZIP2_CLANG3_BAD_MINORS: frozenset[int] = frozenset({4, 5, 6, 7, 8})
_STATICPIE_CROSS_BROKEN_ARCHES: frozenset[str] = frozenset({
    "ppc64", "ppc32", "mipsel", "mips64el", "armv7l-hf",
})
_ZEROCALLREGS_CROSS_BROKEN_ARCHES: frozenset[str] = frozenset({
    "ppc64", "armv7l-hf", "mipsel", "mips64el",
})
_XZ_STACKCLASH_CLANG_BROKEN_ARCHES: frozenset[str] = frozenset({
    "mipsel", "mips64el", "riscv64", "ppc64", "armv7l-hf",
})


def _conflict_bzip2_clang3x_non_x86_64(meta: dict) -> Optional[str]:
    """Rule A: bzip2 + clang 3.{4..8} + arch != x86_64.

    clang 3.x mis-parses the BZ_EXTERN ``__attribute__`` form in bzlib.h
    for cross targets. Fails at every compile attempt; fixed in clang 4.
    """
    if meta.get("package") != "bzip2":
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major != 3:
        return None
    minor = _parse_compiler_minor(meta.get("compilerVersion", ""))
    if minor not in _BZIP2_CLANG3_BAD_MINORS:
        return None
    arch = meta.get("arch", "")
    if arch == "x86_64":
        return None
    return (
        f"bzip2 BZ_EXTERN __attribute__ parse error: "
        f"clang 3.{minor} on {arch} mis-parses bzlib.h attribute syntax"
    )


def _conflict_xz_clang3_9(meta: dict) -> Optional[str]:
    """Rule C1: xz + clang 3.9 (all arches).

    xz's configure passes -Werror and clang 3.9 aborts on a CFLAGS
    ordering edge case that causes configure to mis-detect the compiler.
    Fails deterministically across every arch.
    """
    if meta.get("package") != "xz":
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    minor = _parse_compiler_minor(meta.get("compilerVersion", ""))
    if (major, minor) != (3, 9):
        return None
    return (
        "xz configure -Werror abort: clang 3.9 triggers a CFLAGS-ordering "
        "false-positive in xz's configure Werror check"
    )


def _conflict_xz_stackclash_clang_cross(meta: dict) -> Optional[str]:
    """Rule C2: xz + hardening=stackclash + clang 11..17 + cross arch.

    Same xz -Werror abort as C1 but triggered by clang 11-17 emitting a
    stack-clash-protection diagnostic that xz's configure treats as fatal.
    Only affects cross targets where the stack probing code path differs.
    """
    if meta.get("package") != "xz":
        return None
    if meta.get("hardening") != "stackclash":
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major is None or not (11 <= major <= 17):
        return None
    arch = meta.get("arch", "")
    if arch not in _XZ_STACKCLASH_CLANG_BROKEN_ARCHES:
        return None
    return (
        f"xz configure -Werror abort: clang {major} -fstack-clash-protection "
        f"emits a diagnostic xz configure treats as fatal on {arch}"
    )


def _conflict_staticpie_cross_sysroot_arches(meta: dict) -> Optional[str]:
    """Rule D2/E: staticpie + cross sysroot arches lacking rcrt1.o/Scrt1.o.

    The cross sysroot for ppc64/ppc32/mipsel/mips64el/armv7l-hf lacks
    rcrt1.o / Scrt1.o (the static-PIE CRT files). Arch-structural — no
    compiler version can fix it. i686 is excluded: cross-check found
    lz4+i686+staticpie builds successfully (109+ successes).
    """
    if meta.get("flags") != "staticpie":
        return None
    arch = meta.get("arch", "")
    if arch not in _STATICPIE_CROSS_BROKEN_ARCHES:
        return None
    return (
        f"static-PIE requires rcrt1.o/Scrt1.o; cross sysroot for {arch} "
        f"lacks these CRT files — -static-pie link always fails"
    )


def _conflict_zerocallregs_clang_cross_arches(meta: dict) -> Optional[str]:
    """Rule D3: hardening=zerocallregs + clang + cross arches.

    Clang's -fzero-call-used-regs=used-gpr fails to link on cross arches
    ppc64/armv7l-hf/mipsel/mips64el. GCC handles zerocallregs correctly
    on these arches — rule is clang-only. Cross-check confirmed gcc12+
    zerocallregs succeeds on mipsel and bzip2+mips64el+gcc18+zerocallregs
    builds fine.
    """
    if meta.get("hardening") != "zerocallregs":
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    arch = meta.get("arch", "")
    if arch not in _ZEROCALLREGS_CROSS_BROKEN_ARCHES:
        return None
    return (
        f"clang -fzero-call-used-regs=used-gpr fails to link on {arch}: "
        f"cross-linker relocation pattern rejected (gcc works there)"
    )


# ---------------------------------------------------------------------------
# New-package exclusions (zstd, brotli, simdjson, dav1d, libpng, pcre2).
#
# Rule N1: old clang (3.4/3.5/3.6) for ALL 6 new packages.
#   2014–2016 compilers are fundamentally incompatible with 2023 sources:
#   linker-detection failures, GLIBC ABI gaps, build-system rejections.
#   clang 3.7+ works. Min-clang-3.7 for these packages.
#
# Rule N2: simdjson requires C++17 → gcc < 7 and clang < 5 cannot compile it.
#   gcc 7 and clang 5 are the first releases with full C++17 support.
#
# Rule N3: staticpie for ALL 6 new packages.
#   All 6 output shared libraries (.so). ``-static-pie`` injects rcrt1.o
#   startup which defines _start and requires ``main()``; linking rcrt1.o
#   into a shared library fails with ``undefined reference to 'main'``.
#
# Rule N4: san-address, san-thread, san-memory for ALL 6 new packages.
#   Instrumented conftest/helper binaries cannot execute in the nix build
#   sandbox (ASLR lockdown → ``cannot run C compiled programs``).
#   san-undefined is left in (conservative — it passed or was inconclusive).
#
# Rule N5: nopic + clang ≥ 15 or gcc ≥ 15 for ALL 6 new packages.
#   Modern toolchains default to PIE; ``-fno-PIC`` objects under default-PIE
#   linker emit ``relocation R_X86_64_32 can not be used when making a PIE
#   object``. Gate nopic OFF for clang ≥ 15 / gcc ≥ 15 for these packages.
#
# Rule N6: brotli + Ofast/fastmath + clang 7/8/9.
#   glibc ≥ 2.31 removed ``__log2_finite`` and friends (finite-math aliases).
#   clang 7–9 emits calls to those removed symbols under -ffast-math /
#   -Ofast → ``undefined reference to '__log2_finite'`` at link time.
# ---------------------------------------------------------------------------

_NEW_PACKAGES: frozenset[str] = frozenset({
    "zstd", "brotli", "simdjson", "dav1d", "libpng", "pcre2",
})

# C++17-capable minimums. clang 5's C++17 is incomplete (a default-member-
# initializer DR fails simdjson's ``document() = default``); clang 6 is the
# real floor (run_20260615_073008: simdjson clang5 0/14, clang6 14/14).
_CXX17_MIN_GCC_MAJOR = 7
_CXX17_MIN_CLANG_MAJOR = 6


def _conflict_new_pkgs_old_clang3_early(meta: dict) -> Optional[str]:
    """Rule N1: clang 3.4/3.5/3.6 + any of the 6 new packages.

    2014–2016 compilers are fundamentally incompatible with 2023 C/C++ sources:
    linker-detection routines, GLIBC ABI expectations, and build-system probes
    all break. clang 3.7+ works for these packages.
    """
    if meta.get("package") not in _NEW_PACKAGES:
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major != 3:
        return None
    minor = _parse_compiler_minor(meta.get("compilerVersion", ""))
    if minor >= 7:
        return None
    return (
        f"clang 3.{minor} (2014–2016) is fundamentally incompatible with "
        f"{meta.get('package')} (2023 source): linker detection, GLIBC ABI, "
        f"build-system probes all fail; clang 3.7+ works"
    )


def _conflict_simdjson_pre_cxx17_compiler(meta: dict) -> Optional[str]:
    """Rule N2: simdjson requires C++17 → gcc < 7 or clang < 5.

    simdjson uses C++17 features (structured bindings, ``if constexpr``,
    ``std::string_view``, etc.) unconditionally. gcc 7 and clang 5 are the
    first releases with full C++17 support; anything older aborts at the
    first C++17 construct.
    """
    if meta.get("package") != "simdjson":
        return None
    family = meta.get("compilerFamily", "")
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major is None:
        return None
    if family == "gcc" and major < _CXX17_MIN_GCC_MAJOR:
        return (
            f"simdjson requires C++17; gcc {major} predates full C++17 support "
            f"(minimum: gcc {_CXX17_MIN_GCC_MAJOR})"
        )
    if family == "clang" and major < _CXX17_MIN_CLANG_MAJOR:
        return (
            f"simdjson requires C++17; clang {major} predates full C++17 support "
            f"(minimum: clang {_CXX17_MIN_CLANG_MAJOR})"
        )
    return None


def _conflict_new_pkgs_staticpie(meta: dict) -> Optional[str]:
    """Rule N3: staticpie + any of the 6 new packages (all output shared libs).

    -static-pie injects rcrt1.o (the static-PIE CRT startup) which pulls in
    ``_start`` and requires ``main()``. Linking rcrt1.o into a shared library
    fails with ``undefined reference to 'main'`` — structurally impossible.
    """
    if meta.get("package") not in _NEW_PACKAGES:
        return None
    if meta.get("flags") != "staticpie":
        return None
    return (
        f"{meta.get('package')} builds a shared library; -static-pie (rcrt1.o) "
        f"requires main() and cannot link into a .so"
    )


def _conflict_new_pkgs_sandbox_sanitizers(meta: dict) -> Optional[str]:
    """Rule N4: san-address/san-thread/san-memory + any of the 6 new packages.

    The nix build sandbox locks down ASLR; instrumented conftest/helper
    binaries (``AC_TRY_RUN``, cmake ``try_run``) cannot execute and the build
    aborts with ``Executables created by c compiler are not runnable`` or
    ``cannot run C compiled programs``.  san-undefined is left in (conservative
    — it does not require execute-at-configure-time and passed in testing).
    """
    if meta.get("package") not in _NEW_PACKAGES:
        return None
    san = meta.get("sanitizer", "san-off")
    if san not in ("san-address", "san-thread", "san-memory"):
        return None
    return (
        f"{meta.get('package')} + {san}: instrumented binaries cannot execute "
        f"in the nix build sandbox (ASLR lockdown); configure probe fails"
    )


_NOPIC_MIN_MODERN_GCC = 15
_NOPIC_MIN_MODERN_CLANG = 15


def _conflict_new_pkgs_nopic_modern_pie_toolchain(meta: dict) -> Optional[str]:
    """Rule N5: nopic + clang ≥ 15 / gcc ≥ 15 + any of the 6 new packages.

    Modern toolchains (clang 15+, gcc 15+) enable PIE by default and the
    linker enforces it for executable components. ``-fno-PIC`` objects under
    a PIE-default linker produce ``relocation R_X86_64_32 can not be used
    when making a PIE object``. These packages have executable components;
    nopic is safe only on older toolchains that don't default to PIE.
    """
    if meta.get("package") not in _NEW_PACKAGES:
        return None
    if meta.get("flags") != "nopic":
        return None
    family = meta.get("compilerFamily", "")
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major is None:
        return None
    if family == "gcc" and major >= _NOPIC_MIN_MODERN_GCC:
        return (
            f"{meta.get('package')} nopic + gcc {major}: modern PIE-default "
            f"linker rejects R_X86_64_32 relocations in PIE output"
        )
    if family == "clang" and major >= _NOPIC_MIN_MODERN_CLANG:
        return (
            f"{meta.get('package')} nopic + clang {major}: modern PIE-default "
            f"linker rejects R_X86_64_32 relocations in PIE output"
        )
    return None


_FASTMATH_FINITE_PACKAGES: frozenset[str] = frozenset({"brotli", "libpng"})
_FASTMATH_FINITE_CLANG_BAD_MAJORS: frozenset[int] = frozenset({7, 8, 9})


def _conflict_fastmath_finite_clang7_9(meta: dict) -> Optional[str]:
    """Rule N6: {brotli, libpng} + Ofast/fastmath + clang 7/8/9.

    glibc ≥ 2.31 (2020) removed ``__log2_finite`` / ``__pow_finite`` and the
    other ``__*_finite`` finite-math aliases. clang 7–9 emits calls to those
    symbols under ``-ffast-math`` / ``-Ofast`` (``-ffinite-math-only`` enables
    them) → ``undefined reference to '__pow_finite'`` at link. brotli (log2)
    and libpng (pow) both trip it; clang 10+ uses the glibc-version-guarded
    symbols. Proven for libpng (run_20260615_073008: clang7-9+Ofast/fastmath
    0 ok/10 fail; clang5/6 + clang10+ ok; non-fastmath clang7-9 ok). zstd's
    paramgrill uses log() too but is parked pending a stock-build run.
    """
    if meta.get("package") not in _FASTMATH_FINITE_PACKAGES:
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major not in _FASTMATH_FINITE_CLANG_BAD_MAJORS:
        return None
    opt = meta.get("optimization", "")
    flags = meta.get("flags", "")
    if opt != "Ofast" and flags != "fastmath":
        return None
    trigger = "Ofast" if opt == "Ofast" else "fastmath"
    return (
        f"{meta.get('package')} + {trigger} + clang {major}: glibc ≥ 2.31 "
        f"removed __*_finite; clang {major} emits calls under -ffast-math"
    )


def _conflict_dav1d_old_gcc_atomics(meta: dict) -> Optional[str]:
    """Rule N7: dav1d + gcc 4.<7 — meson aborts "Atomics not supported".

    dav1d 1.5 requires C11 atomics (``stdatomic.h``); gcc 4.4–4.6 lack it and
    meson's GCC-style-atomics fallback check also fails. gcc 4.8/4.9 BUILD FINE
    (run_20260615_073008: gcc4.4/4.5/4.6 = 0 ok/32 fail; gcc4.8/4.9 = 24/24 ok),
    so the bound is strictly ``< 4.7``. gcc4.7 is absent from the sample → left
    IN (not over-excluded).
    """
    if meta.get("package") != "dav1d":
        return None
    if meta.get("compilerFamily") != "gcc":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major != 4:
        return None
    minor = _parse_compiler_minor(meta.get("compilerVersion", ""))
    if minor >= 7:
        return None
    return (
        f"dav1d needs C11 atomics (stdatomic.h); gcc 4.{minor} lacks it → "
        f"meson 'Atomics not supported' (gcc4.8+ build fine)"
    )


def _conflict_dav1d_san_undefined_clang(meta: dict) -> Optional[str]:
    """Rule N8: dav1d + san-undefined + clang (ubsan runtime unlinked in .so).

    dav1d builds ``libdav1d.so`` via meson ``shared_library``. Under clang +
    ``-fsanitize=undefined`` meson instruments the objects but does NOT
    auto-link the ubsan runtime into the shared library → many
    ``undefined reference to '__ubsan_handle_*'`` at link. CLANG-ONLY: gcc +
    dav1d + san-undefined (without nopic) SUCCEEDS — a compiler-agnostic rule
    would wrongly drop those (run_20260615_073008: clang18-22 = 0 ok/28 fail;
    gcc13-15 = 12/12 ok). clang<18 absent from the sample but the meson
    behaviour is clang-wide.
    """
    if meta.get("package") != "dav1d":
        return None
    if meta.get("compilerFamily") != "clang":
        return None
    if meta.get("sanitizer") != "san-undefined":
        return None
    return (
        "dav1d builds libdav1d.so; clang's meson shared_library does not "
        "auto-link the ubsan runtime → undefined __ubsan_handle_* (gcc links it)"
    )


_NOPIC_SAN_UNDEFINED_MIN_GCC = 13


def _conflict_nopic_san_undefined_modern_gcc(meta: dict) -> Optional[str]:
    """Rule N9: nopic + san-undefined + gcc ≥ 13 for the new packages.

    The ``nopic`` (-fno-PIC) × ``san-undefined`` combination fails to link
    under gcc's modern PIE-default toolchain (run_20260615_073008:
    nopic+san-undefined+gcc13/14 = 12 failures across the new packages;
    nopic+san-off builds fine at gcc≤12). Distinct from N5 (nopic+gcc≥15 for
    ANY sanitizer) — this catches the gcc13/14 case which N5's ≥15 threshold
    misses. The proposed blanket "nopic+gcc≥13" was rejected: nopic+san-off at
    gcc13/14 was never tested, so it stays unproven; this rule only excludes
    the proven nopic×ubsan failure. gcc≤12 left IN (not over-excluded).
    """
    if meta.get("package") not in _NEW_PACKAGES:
        return None
    if meta.get("flags") != "nopic":
        return None
    if meta.get("sanitizer") != "san-undefined":
        return None
    if meta.get("compilerFamily") != "gcc":
        return None
    major = _parse_compiler_major(meta.get("compilerVersion", ""))
    if major is None or major < _NOPIC_SAN_UNDEFINED_MIN_GCC:
        return None
    return (
        f"{meta.get('package')} nopic + san-undefined + gcc {major}: nopic×ubsan "
        f"under modern PIE-default gcc fails to link (gcc≤12 + nopic ok)"
    )


_FLAG_CONFLICTS = (
    _conflict_sanitizer_o0,
    _conflict_fastmath_san_undefined,
    # Binary-scoped intrinsic failures (confirmed deterministic from logs):
    _conflict_bzip2_clang3x_non_x86_64,        # Rule A
    _conflict_xz_clang3_9,                      # Rule C1
    _conflict_xz_stackclash_clang_cross,        # Rule C2
    # Arch-structural cross-toolchain failures:
    _conflict_staticpie_cross_sysroot_arches,   # Rule D2/E (i686 excluded)
    _conflict_zerocallregs_clang_cross_arches,  # Rule D3 (clang-only)
    # New-package exclusions (zstd, brotli, simdjson, dav1d, libpng, pcre2):
    _conflict_new_pkgs_old_clang3_early,        # Rule N1: clang 3.4/3.5/3.6
    _conflict_simdjson_pre_cxx17_compiler,      # Rule N2: simdjson C++17 min
    _conflict_new_pkgs_staticpie,               # Rule N3: staticpie+shared-lib
    _conflict_new_pkgs_sandbox_sanitizers,      # Rule N4: san-address/thread/memory
    _conflict_new_pkgs_nopic_modern_pie_toolchain,  # Rule N5: nopic+modern-PIE
    _conflict_fastmath_finite_clang7_9,         # Rule N6: {brotli,libpng}+fastmath+clang7-9
    _conflict_dav1d_old_gcc_atomics,            # Rule N7: dav1d+gcc4.<7 (C11 atomics)
    _conflict_dav1d_san_undefined_clang,        # Rule N8: dav1d+san-undefined+clang
    _conflict_nopic_san_undefined_modern_gcc,   # Rule N9: nopic+san-undefined+gcc≥13
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
    # Package rules are curated by family, including explicit releases. A
    # package ID is not an upstream package name and must not be reparsed.
    if meta_entry.get("packageFamily"):
        meta_entry = {**meta_entry, "package": meta_entry["packageFamily"]}
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


def resolve_package_selection(
    flake_ref: str, sys_name: str, *, packages=None, package_versions=None,
    package_ids=None, all_package_versions=False, archs=None, run_subprocess=None,
) -> dict[str, dict]:
    """Read a cheap index, then resolve provenance for the selected families."""
    from .package_selection import nix_string, select_package_ids, validate_release

    if packages is not None and not packages:
        raise ValueError("package selection is empty")
    if archs is not None and not archs:
        raise ValueError("target architecture selection is empty")
    if all_package_versions and (package_versions or package_ids):
        raise ValueError("--all-package-versions cannot be combined with explicit release selections")
    attr = f"_packageCatalog.{sys_name}"
    index = run_nix_eval(
        flake_ref, attr, run_subprocess=run_subprocess,
        apply="c: builtins.mapAttrs (_: p: { inherit (p) packageFamily packageId isDefault; }) c",
    )
    if not isinstance(index, dict) or not index:
        raise ValueError("package catalog index is empty or malformed")
    known = {entry["packageFamily"] for entry in index.values()}
    requested = set(packages or ())
    implied = set()
    for spec in package_versions or ():
        family, sep, version = spec.partition("=")
        if not sep or not family or not version:
            raise ValueError(f"expected FAMILY=EXACT_VERSION, got {spec!r}")
        implied.add(family)
    for key in package_ids or ():
        if key not in index:
            raise ValueError(f"unknown package ID: {key}")
        implied.add(index[key]["packageFamily"])
    unknown = (requested | implied) - known
    if unknown:
        raise ValueError(f"unknown package families: {', '.join(sorted(unknown))}")
    if packages is not None and implied - requested:
        raise ValueError("release selections conflict with --packages: " + ", ".join(sorted(implied - requested)))
    wanted = requested if packages is not None else implied or known
    candidates = {key: entry for key, entry in index.items() if entry["packageFamily"] in wanted}
    # Exact version matching needs only native versions and recipe IDs, not
    # sources or availability of every release on every architecture.
    version_families = {spec.partition("=")[0] for spec in package_versions or ()}
    version_keys = {key: None for key, entry in candidates.items() if entry["packageFamily"] in version_families}
    if version_keys:
        versions = run_nix_eval(
            flake_ref, attr, run_subprocess=run_subprocess,
            apply=f"c: builtins.mapAttrs (_: p: {{ inherit (p) packageFamily packageId isDefault packageVersion recipeId; }}) "
                  f"(builtins.intersectAttrs (builtins.fromJSON {nix_string(json.dumps(version_keys))}) c)",
        )
        if not isinstance(versions, dict) or set(versions) != set(version_keys):
            raise ValueError("version catalog does not match its index")
        candidates.update(versions)
    ids = select_package_ids(candidates, families=packages, versions=package_versions,
                             package_ids=package_ids, all_versions=all_package_versions)
    keys = dict.fromkeys(ids)
    projection = f"builtins.intersectAttrs (builtins.fromJSON {nix_string(json.dumps(keys))}) c"
    if archs is not None:
        targets = nix_string(json.dumps(dict.fromkeys(sorted(set(archs)))))
        projection = f"builtins.mapAttrs (_: p: p // {{ availability = builtins.intersectAttrs (builtins.fromJSON {targets}) p.availability; }}) ({projection})"
    details = run_nix_eval(flake_ref, attr, run_subprocess=run_subprocess, apply=f"c: {projection}")
    if not isinstance(details, dict) or set(details) != set(keys):
        raise ValueError("resolved package catalog does not match its index")
    result = {key: details[key] for key in ids}
    for entry in result.values():
        if any(state["available"] for state in entry.get("availability", {}).values()):
            validate_release(entry)
    return result


def enumerate_variants(
    flake_ref: str,
    sys_name: str,
    *,
    packages: Optional[list[str]] = None,
    package_versions: Optional[list[str]] = None,
    package_ids: Optional[list[str]] = None,
    all_package_versions: bool = False,
    selected_catalog: Optional[dict[str, dict]] = None,
    archs: Optional[list[str]] = None,
    sample_size: int = 0,
    sample_seed: str = "42",
    run_subprocess: Optional[RunSubprocess] = None,
    compiler_capture: bool = False,
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
    from .package_selection import release_metadata

    catalog = selected_catalog if selected_catalog is not None else resolve_package_selection(
        flake_ref, sys_name, packages=packages, package_versions=package_versions,
        package_ids=package_ids, all_package_versions=all_package_versions,
        archs=archs, run_subprocess=run_subprocess,
    )
    arch_filter = set(archs) if archs else None
    out: dict[str, dict] = {}
    for pkg, entry in sorted(catalog.items()):
        availability = entry["availability"]
        requested_archs = arch_filter if arch_filter is not None else set(availability)
        unknown_archs = requested_archs - set(availability)
        if unknown_archs:
            raise ValueError(f"unknown target architectures: {', '.join(sorted(unknown_archs))}")
        pkg_archs = sorted(a for a in requested_archs if availability[a]["available"])
        out[pkg] = {
            **release_metadata(entry),
            "archs": pkg_archs,
            "skipped_targets": {a: availability[a]["reason"] for a in sorted(requested_archs) if not availability[a]["available"]},
            "sample_size": int(sample_size or 0),
            "sample_seed": sample_seed,
            "tier": _tier_from_pkg(entry["packageFamily"]),
        }
        if entry.get("variantInventorySchema") == 1:
            out[pkg]["variant_inventory_schema"] = 1
            out[pkg]["variant_inventory_identity"] = entry["variantInventoryIdentity"]
    if not any(entry["archs"] for entry in out.values()):
        raise ValueError(f"no available package/target cells: {out}")
    if compiler_capture:
        for metadata in out.values():
            metadata["compiler_capture"] = True
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
    compiler_capture: bool = False,
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
    toolchain_root="_capturedCrossToolchainMap" if compiler_capture else "_crossToolchainMap"
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
            flake_ref, sys_name, pairs, run_subprocess=runner,
            **({"compiler_capture":True} if compiler_capture else {})
        )

    requested = {(arch, compiler) for arch, compiler in pairs}
    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{flake_ref}#{toolchain_root}.{sys_name}",
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
            flake_ref, sys_name, pairs, run_subprocess=runner,
            **({"compiler_capture":True} if compiler_capture else {})
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
    compiler_capture: bool = False,
) -> dict[tuple[str, str], str]:
    """One ``nix eval`` per (arch, compiler) — used when
    ``nix-eval-jobs`` isn't on PATH (test harness, hermetic
    environments). Slower than the parallel evaluator but still
    per-entry isolated.
    """
    toolchain_root="_capturedCrossToolchainMap" if compiler_capture else "_crossToolchainMap"
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
            f"{flake_ref}#{toolchain_root}.{sys_name}.{arch}.{compiler}.drvPath",
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
        load_support_table_for_flake,
    )
    support = load_support_table_for_flake(flake_ref)

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
    compiler_capture: bool = False,
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
        **({"compiler_capture":True} if compiler_capture else {}),
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
BUILD_DEPS_ARCHIVE_NAME = "build_deps.out.archive"


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


# Subdirectory under ``matrix_eval_out_dir`` where ``build_common_dep``
# tasks publish their realised OUTPUT closure archive
# (``common-<id>.out.archive``).  Kept in its OWN subdir so the
# per-binary ``discover_archives`` matrix glob (``matrix-*.drv.archive``
# at the top level) never picks these up, and so an operator can
# ``ls _matrix_eval/_common_deps/`` to see every shared-dep archive.
COMMON_DEPS_SUBDIR = "_common_deps"


def common_dep_id_for_ident(ident_str: str) -> str:
    """Derive a stable, filesystem-safe id for a common-dep ident.

    A ``build_common_dep`` descriptor is keyed on the drv ident
    ``"<hash>-<name>"`` (the streaming planner's
    :func:`dependency_graph_planner._ident_to_str` shape).  The Nix
    store-hash is the first ``-``-delimited token; it is content-
    addressed (stable across re-runs with the same inputs), always
    filesystem-safe (``[a-z0-9]``), and short enough to compose cleanly
    into an archive filename and a gate task_id.

    The id is derivable on BOTH sides of the affine-gate handoff:

    * the spawn/wiring side knows the ``build_common_dep__<ident_str>``
      task_id (so it can strip the prefix and call this), and
    * the build worker knows ``payload["ident"]`` (the same
      ``ident_str``) when it publishes the archive after the build.

    Deriving from the drv ident (NOT the realised output path) is what
    keeps the two sides in agreement: the worker does not know the gate
    id at wire-time, and the wiring side does not know the realised
    output hash — only the drv ident is common to both.

    Raises :class:`ValueError` for an ident with no extractable hash
    (empty string, or a leading-dash form).
    """
    # Tolerate a bare ``.drv`` suffix on the ident name half; the hash
    # token (before the first dash) is unaffected by it.
    head = ident_str.split("-", 1)[0]
    if not head:
        raise ValueError(
            f"common_dep_id_for_ident: cannot extract store hash from "
            f"{ident_str!r}"
        )
    return head


def common_dep_archive_name(ident_str: str) -> str:
    """Return the published archive filename for a common-dep ident.

    Format: ``common-<id>.out.archive`` where ``<id>`` is derived via
    :func:`common_dep_id_for_ident`.  The build worker writes this under
    ``<matrix_eval_out_dir>/<COMMON_DEPS_SUBDIR>/<name>``; the per-machine
    affine import gate reads it from the same location.
    """
    return f"common-{common_dep_id_for_ident(ident_str)}.out.archive"


@dataclasses.dataclass
class ToolchainSplit:
    """Result of :func:`compute_toolchain_split`.

    ``common_paths`` is the set of store paths that appear in at least
    two toolchain closures — the shared runtime (glibc, libgcc, …).
    Using freq>=2 (rather than the strict ALL-toolchains intersection)
    avoids the multi-era collapse: glibc-2.26-era toolchains share zero
    paths with glibc-2.42-era toolchains, so a strict intersection
    would be empty and COMMON would carry nothing.  Paths appearing in
    >=2 closures naturally collapse to the era-shared dependencies.

    ``delta_paths`` maps each toolchain out-path to the tuple of store
    paths in its closure that are NOT in ``common_paths``.  Each delta
    imports cleanly after COMMON is present because every dependency of
    a delta path is either in COMMON or in the same delta.

    Upload size: COMMON + Σ|delta_i| = |union of all closures|
    (each store path appears in COMMON or exactly one delta), so total
    upload ≈ union size with no blowup.
    """

    common_paths: frozenset[str]
    delta_paths: dict[str, tuple[str, ...]]


def _toolchain_build_closure(
    outpath: str, runner: RunSubprocess
) -> Optional[list[str]]:
    """Return the toolchain's BUILD-time closure (topologically ordered), or
    ``None`` if the drv can't be resolved (caller falls back to the OUTPUT
    closure).

    A build node must IMPORT everything needed to BUILD with the cross-
    stdenv — the setup hooks, ``patchelf``, ``pkg-config``, ``make-binary-
    wrapper-hook``, the bootstrap — none of which appear in the stdenv
    OUTPUT's runtime requisites (~82 paths) but all of which are in the
    drv's ``--include-outputs`` build closure (~hundreds of paths). Shipping
    only the output closure left the node trying to BUILD the missing build-
    time paths offline ("Cannot build patchelf/setup-hook/...drv").

    Steps: resolve the realised outpath's deriver, take that drv's
    ``--include-outputs`` build closure OUTPUT paths, then re-query
    ``--requisites`` over those outputs so the exported list is runtime-
    closed AND dependency-first ordered (import requires a path's references
    to be registered first; ``export_closure_exact`` does not re-query
    order, so it must be correct here).
    """
    der_out, _der_err, der_rc = runner(
        ["nix-store", "--query", "--deriver", outpath]
    )
    if der_rc != 0:
        return None
    drvs = [
        l for l in der_out.decode("utf-8", errors="replace").splitlines()
        if l.strip()
    ]
    drv = drvs[0] if drvs else ""
    if not drv.endswith(".drv") or "unknown-deriver" in drv:
        return None
    bc_out, _bc_err, bc_rc = runner(
        ["nix-store", "--query", "--requisites", "--include-outputs", drv]
    )
    if bc_rc != 0:
        return None
    outputs = [
        l for l in bc_out.decode("utf-8", errors="replace").splitlines()
        if l.strip() and not l.endswith(".drv")
    ]
    if not outputs:
        return None
    req_out, req_err, req_rc = runner(
        ["nix-store", "--query", "--requisites", *outputs]
    )
    if req_rc != 0:
        # FAIL LOUD (do NOT silently fall back to the output closure): the
        # build-closure OUTPUTS exist as derivations but at least one is
        # UNREALIZED in the local store, so requisites cannot be resolved.
        # Silently returning None here drops the toolchain's bootstrap (xgcc,
        # bootstrap-stage*, binutils-wrapper, …) from the shipped archive and
        # every substituter-free build then fails offline with "import did not
        # land". The producer MUST realise + gc-root the full build closure
        # (see build_toolchains_locally) before the split runs; if it didn't,
        # abort rather than upload a silently-incomplete archive.
        raise RuntimeError(
            "_toolchain_build_closure: build-closure requisites query failed "
            f"for {drv} — an --include-outputs path is UNREALIZED (the toolchain "
            "build closure was not realised+gc-rooted before the split, so the "
            "archive would silently omit the bootstrap). rc=%d: %s" % (
                req_rc, req_err.decode("utf-8", errors="replace").strip()[-800:],
            )
        )
    return [
        l for l in req_out.decode("utf-8", errors="replace").splitlines()
        if l.strip()
    ]


def compute_toolchain_split(
    toolchain_out_paths: list[str],
    *,
    baseline_paths: Optional[frozenset[str]] = None,
    force_common_outpaths: Optional[list[str]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
) -> ToolchainSplit:
    """Compute COMMON + per-toolchain deltas from realized out-paths.

    ``baseline_paths`` (the native std-compiler stdenv's build closure,
    present on EVERY build node via the image) is subtracted from each
    toolchain's build closure before the common/delta split, so only the
    cross-specific delta ships (the user directive: "only include binaries
    not in the normal stdenv with the std compiler"). This shrinks the
    common archive massively (the native bootstrap is the bulk and is
    already resident). A toolchain whose closure is ENTIRELY baseline (e.g.
    the x86_64-native toolchain) yields an empty delta and is skipped — the
    build worker must tolerate its absent per-toolchain archive (all-baseline
    ⇒ already resident).

    For each toolchain out-path ``Ti``:
      closure(Ti) = ``nix-store --query --requisites Ti``

    COMMON = {p : freq(p) >= 2} — paths appearing in at least two
    toolchain closures.  This avoids the multi-era collapse: the strict
    ALL-closures intersection of a matrix spanning glibc-2.26 and
    glibc-2.42 toolchains is empty; the freq>=2 definition keeps all
    intra-era shared deps in COMMON.

    delta(Ti) = closure(Ti) − COMMON.  Each delta imports cleanly after
    COMMON is present.

    Upload property: COMMON + Σ|delta_i| == |union| (each path is in
    COMMON or exactly one delta) — total upload ≈ union size, no blowup.

    FAIL LOUD on incompleteness: if a toolchain's build closure can be
    resolved (deriver known) but an ``--include-outputs`` path is UNREALIZED,
    :func:`_toolchain_build_closure` raises and that propagates here — the
    split aborts rather than silently shipping the output-only closure (which
    would omit the bootstrap). The producer must realise+gc-root the full
    build closure first (:func:`build_toolchains_locally`). Only the benign
    cases (invalid/non-store outpath, or an entirely-baseline toolchain) are
    skipped with a warning; a legacy stdenv with an unknown deriver falls back
    to its already-resident OUTPUT closure. Raises :class:`RuntimeError` if NO
    toolchain produces a valid closure, on any unrealized build closure, or if
    a requisites query hard-fails with a non-zero rc.
    """
    if not toolchain_out_paths:
        raise RuntimeError(
            "compute_toolchain_split: no toolchain out-paths supplied"
        )
    runner = run_subprocess or _default_run_subprocess
    baseline = baseline_paths or frozenset()
    # Keep each closure as the ORDERED requisites list: ``nix-store
    # --query --requisites`` emits paths in topological (dependency-first)
    # order, which ``nix-store --import`` REQUIRES — a path's references
    # must already be registered when it is imported. The delta archives
    # are exported with ``export_closure_exact`` (no requisites re-query),
    # so we must preserve this order here; sorting alphabetically would
    # break intra-delta references at import time.
    import logging as _logging  # noqa: PLC0415
    _split_log = _logging.getLogger("compiler_suit_runner.preflight.split")

    closures: dict[str, list[str]] = {}
    for outpath in toolchain_out_paths:
        if not isinstance(outpath, str) or not outpath.startswith("/"):
            _split_log.warning(
                "compute_toolchain_split: skipping unrealized/invalid "
                "outpath %r",
                outpath,
            )
            continue
        # Prefer the toolchain's full BUILD-time closure (setup hooks,
        # patchelf, pkg-config, bootstrap), not just its OUTPUT runtime
        # closure — the build node imports this to BUILD with the cross-
        # stdenv offline. Falls back to the output closure when the drv is
        # unresolved (legacy/already-resident stdenvs).
        lines = _toolchain_build_closure(outpath, runner)
        if lines is None:
            stdout, stderr, rc = runner(
                ["nix-store", "--query", "--requisites", outpath]
            )
            if rc != 0:
                _split_log.warning(
                    "compute_toolchain_split: requisites query failed for "
                    "%r (rc=%d): %s — skipping",
                    outpath, rc,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
                continue
            lines = [l for l in stdout.decode("utf-8", errors="replace").splitlines() if l.strip()]
        if baseline:
            # Subtract the native std-compiler baseline (resident on every
            # node via the image): ship only the cross-specific delta. Order
            # is preserved (filter keeps the dependency-first sequence).
            kept = [l for l in lines if l not in baseline]
            if not kept:
                _split_log.info(
                    "compute_toolchain_split: %r is entirely in the native "
                    "baseline (%d paths) — no cross-specific delta to ship "
                    "(build worker must tolerate the absent per-toolchain "
                    "archive)",
                    outpath, len(lines),
                )
                continue
            lines = kept
        if not lines:
            _split_log.warning(
                "compute_toolchain_split: closure query returned no "
                "paths for %r — skipping",
                outpath,
            )
            continue
        closures[outpath] = lines

    if not closures:
        raise RuntimeError(
            "compute_toolchain_split: no toolchain produced a valid "
            "closure (all paths unrealized or requisites queries failed)"
        )

    # FORCE-COMMON (gap#2): the native build-host stdenv's build closure must
    # land on EVERY build node via the always-imported common gate — a cross
    # variant's depsBuildBuild needs it to run configure/make on the build
    # host, but it is NOT part of any per-compiler cross toolchain's closure,
    # so it would otherwise be freq=1 → routed to an undeclared delta gate →
    # never imported. We force its whole --include-outputs build closure into
    # COMMON unconditionally (minus baseline). Same fail-loud contract as the
    # toolchain closures (unrealized → raise, never ship partial).
    force_common: set[str] = set()
    for outpath in force_common_outpaths or ():
        if not isinstance(outpath, str) or not outpath.startswith("/"):
            continue
        lines = _toolchain_build_closure(outpath, runner)
        if lines is None:
            stdout, stderr, rc = runner(
                ["nix-store", "--query", "--requisites", outpath]
            )
            if rc != 0:
                raise RuntimeError(
                    "compute_toolchain_split: force-common requisites query "
                    "failed for %r (rc=%d): %s" % (
                        outpath, rc,
                        stderr.decode("utf-8", errors="replace").strip(),
                    )
                )
            lines = [
                l for l in stdout.decode("utf-8", errors="replace").splitlines()
                if l.strip()
            ]
        if baseline:
            lines = [l for l in lines if l not in baseline]
        force_common.update(lines)

    # freq(p) = number of toolchains whose closure contains p.
    # COMMON = {p : freq(p) >= 2} so paths shared by at least two
    # toolchains (any era) form the common archive.  A single-toolchain
    # run makes COMMON the full closure (freq >= 1 iff >= 2 out of 1 is
    # vacuously true — but for N=1 we fall back to the full closure so
    # the single-toolchain case works: delta = empty, common = full).
    if len(closures) == 1:
        only_paths = next(iter(closures.values()))
        common: frozenset[str] = frozenset(only_paths) | frozenset(force_common)
    else:
        import collections as _collections  # noqa: PLC0415
        freq: dict[str, int] = _collections.Counter(
            p for paths in closures.values() for p in paths
        )
        common = frozenset(
            p for p, count in freq.items() if count >= 2
        ) | frozenset(force_common)

    # delta = closure − COMMON per toolchain, PRESERVING topological order.
    delta_paths: dict[str, tuple[str, ...]] = {
        outpath: tuple(p for p in lines if p not in common)
        for outpath, lines in closures.items()
    }
    return ToolchainSplit(common_paths=common, delta_paths=delta_paths)


def eval_native_build_host_stdenvs(
    flake_ref: str,
    sys_name: str,
    archs: Iterable[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[list[str], list[str]]:
    """Eval the NATIVE build-host stdenv (``crossToolchains.<sys>.<arch>.stdenv``)
    drv + outpath for each target arch, deduplicated.

    A cross variant's ``depsBuildBuild`` needs the native build-platform stdenv
    to run configure/make on the build host; substituter-free, that stdenv's
    FULL build closure (xgcc, bootstrap-stage*, binutils-wrapper, bash,
    patchelf, …) must be resident on every build node. That closure is NOT part
    of any per-compiler cross toolchain's closure, so the submitter must realise
    it and force-ship it into the always-imported common archive. The native
    stdenv is the build platform's own stdenv (identical across cross targets),
    so we eval per-arch and dedup → a multi-arch run ships it once.

    Returns ``(drv_paths, out_paths)`` (sorted, deduplicated). An arch whose
    attr is missing or fails to eval is skipped with a warning rather than
    aborting — a genuinely-absent native closure surfaces downstream as the
    split's fail-loud guard or the build worker's substituter-free assertion.
    """
    import logging as _logging  # noqa: PLC0415
    _log = _logging.getLogger(
        "compiler_suit_runner.preflight.native_stdenv"
    )
    runner = run_subprocess or _default_run_subprocess
    drvs: set[str] = set()
    outs: set[str] = set()
    for arch in sorted({a for a in archs if a}):
        attr = f"{flake_ref}#crossToolchains.{sys_name}.{arch}.stdenv"
        d_out, _d_err, d_rc = runner([
            "nix", "--extra-experimental-features", "nix-command flakes",
            "eval", "--raw", f"{attr}.drvPath",
        ])
        o_out, _o_err, o_rc = runner([
            "nix", "--extra-experimental-features", "nix-command flakes",
            "eval", "--raw", f"{attr}.outPath",
        ])
        if d_rc != 0 or o_rc != 0:
            _log.warning(
                "native build-host stdenv eval failed for arch %s "
                "(drv_rc=%d out_rc=%d)", arch, d_rc, o_rc,
            )
            continue
        d = d_out.decode("utf-8", errors="replace").strip()
        o = o_out.decode("utf-8", errors="replace").strip()
        if d.endswith(".drv") and o.startswith("/nix/store/"):
            drvs.add(d)
            outs.add(o)
        else:
            _log.warning(
                "native build-host stdenv eval for arch %s returned "
                "unexpected paths (drv=%r out=%r)", arch, d, o,
            )
    return sorted(drvs), sorted(outs)


def eval_build_host_tools(
    flake_ref: str,
    sys_name: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Eval the elf-folder build-host tools aggregate
    (``_buildHostTools.<sys>`` = file/findutils/gawk) drv + outpath.

    Every cross variant's elf-folder wrapper (lib/mkBinaryFolder.nix) runs
    ``file -bL`` / find / awk on the BUILD host to collect ELF binaries — so
    their output closure must be resident on every build node for a
    substituter-free build, but they are in no toolchain/stdenv closure. The
    submitter realises this aggregate (materialising the tool outputs) and
    force-ships its closure into import_common.

    Returns ``(drv, outpath)`` or ``(None, None)`` if the attr is missing /
    fails to eval (the build worker's assertion surfaces a genuinely-absent
    tool downstream).
    """
    import logging as _logging  # noqa: PLC0415
    _log = _logging.getLogger(
        "compiler_suit_runner.preflight.build_host_tools"
    )
    runner = run_subprocess or _default_run_subprocess
    attr = f"{flake_ref}#_buildHostTools.{sys_name}"
    d_out, _d_err, d_rc = runner([
        "nix", "--extra-experimental-features", "nix-command flakes",
        "eval", "--raw", f"{attr}.drvPath",
    ])
    o_out, _o_err, o_rc = runner([
        "nix", "--extra-experimental-features", "nix-command flakes",
        "eval", "--raw", f"{attr}.outPath",
    ])
    if d_rc != 0 or o_rc != 0:
        _log.warning(
            "build-host tools eval failed (drv_rc=%d out_rc=%d)", d_rc, o_rc,
        )
        return None, None
    d = d_out.decode("utf-8", errors="replace").strip()
    o = o_out.decode("utf-8", errors="replace").strip()
    if d.endswith(".drv") and o.startswith("/nix/store/"):
        return d, o
    _log.warning(
        "build-host tools eval returned unexpected paths (drv=%r out=%r)",
        d, o,
    )
    return None, None


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
    """Skip only a complete publication of the exact release and derivation."""
    import pathlib
    from .publication import variant_complete
    dataset_dir = pathlib.Path(dataset_dir)
    remaining: list[VariantSpec] = []
    skipped = 0
    for variant in variants:
        pkg = variant.get("pkg") if isinstance(variant, dict) else None
        if not (isinstance(pkg, str) and pkg):
            complete = False
        elif variant.get("compiler_capture") is not None:
            # Captured variants publish under their own contract.
            from compiler_suit_runner.capture import captured_variant_complete
            complete = captured_variant_complete(
                dataset_dir / pkg / variant["variant_dir"],
                label=variant.get("label"), drv=variant.get("drv"),
                contract=variant["compiler_capture"], expected=variant,
            )
        else:
            complete = variant_complete(dataset_dir / pkg, variant)
        if complete:
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
    aggregate_drv: Optional[str] = None,  # noqa: ARG001 - accepted, unused
    gcroot_dir: Optional[str] = None,
    deep_realise_drvs: frozenset[str] = frozenset(),
    run_subprocess: Optional[RunSubprocess] = None,
) -> None:
    """Realise AND gc-root every toolchain's FULL build closure so the split
    upload ships a COMPLETE substituter-free archive.

    A substituter-free build node must import the toolchain's entire BUILD
    closure — the bootstrap (xgcc, bootstrap-stage*, binutils-wrapper, bash,
    patchelf, setup hooks) — none of which appears in the stdenv OUTPUT runtime
    closure. ``nix build <drv>^*`` only materialises the FINAL stdenv outputs;
    worse, with substituters enabled on the submitter it FETCHES those final
    outputs directly and never realises the bootstrap at all. The bootstrap
    outputs are then absent when :func:`compute_toolchain_split` snapshots the
    ``--include-outputs`` closure → the requisites re-query fails → (pre-fix)
    the split silently fell back to the ~82-path output closure → every worker
    failed offline with "import did not land" for ~114 missing bootstrap paths.

    So this, in order:
      1. builds each toolchain's final outputs (``nix build <drv>^*``),
      1.5. for ``deep_realise_drvs`` (the native build-host stdenv), realises
         the FULL drv-input closure so build-time-only intermediates (xgcc,
         bootstrap-stage*) — which ``--include-outputs`` omits while unrealised
         — actually exist before the snapshot,
      2. computes the union of every toolchain's ``--include-outputs`` build-
         closure OUTPUT paths,
      3. REALISES that union (substituting the bootstrap from the submitter's
         caches — the producer is allowed substituters) so every build-closure
         output is present, and
      4. GC-ROOTS the realised union (indirect roots under ``gcroot_dir``) so it
         survives until the split snapshots it ("after rebuilding the compiler
         stuff you must add a gc root, otherwise it's collected again").

    Sizing note: the complete cross-aarch64 build closure is ~34GB / ~1344
    paths (measured) — NOT the ~99GB seen earlier, which was an all-arch over-
    realisation (rooting every -man/-debug/-doc + intermediate). One arch fits
    comfortably; the image-resident baseline is subtracted at split time.

    Realise + root are batched (256 paths/call) to bound argv length and avoid
    the per-path rooting hang. Raises :class:`PreflightError` on the first
    non-zero exit at any step (fail loud — never proceed to ship a partial
    closure).
    """
    runner = run_subprocess or _default_run_subprocess
    _BATCH = 256
    drvs = sorted(d for d in toolchain_drvs if d)
    if not drvs:
        return

    # 1. Build each toolchain's final outputs (resolves derivers; nix handles
    #    internal parallelism per drv).
    for drv in drvs:
        _stdout, stderr, rc = runner([
            "nix", "--extra-experimental-features", "nix-command flakes",
            "build", f"{drv}^*", "--no-link",
        ])
        if rc != 0:
            decoded = stderr.decode("utf-8", errors="replace").strip()
            raise PreflightError(
                f"local toolchain build failed for {drv} (rc={rc}): "
                f"{decoded[-1000:]}"
            )

    # 1.5. DEEP-REALISE the FULL drv-input closure for deep_realise_drvs (the
    #      native build-host stdenv). ``nix build <drv>^*`` / whole-output
    #      substitution leaves BUILD-TIME-ONLY intermediates (xgcc,
    #      bootstrap-stage*) UNREALISED, and ``--include-outputs`` lists only
    #      EXISTING outputs — so the snapshot would still miss them. Realising
    #      the input-derivation closure materialises every build-closure output
    #      (cheap for the native stdenv: standard cached nixpkgs). We do NOT do
    #      this for the cross toolchains — their build-time-only intermediates
    #      are uncached (would rebuild from source) and variants don't need
    #      them; only the native build-host bootstrap is required at a cross
    #      variant's depsBuildBuild.
    for drv in sorted(d for d in deep_realise_drvs if d):
        out, err, rc = runner([
            "nix-store", "--query", "--requisites", drv,
        ])
        if rc != 0:
            decoded = err.decode("utf-8", errors="replace").strip()
            raise PreflightError(
                f"native build-host closure query failed for {drv} "
                f"(rc={rc}): {decoded[-1000:]}"
            )
        input_drvs = sorted({
            ln.strip()
            for ln in out.decode("utf-8", errors="replace").splitlines()
            if ln.strip().endswith(".drv")
        })
        for i in range(0, len(input_drvs), _BATCH):
            batch = input_drvs[i:i + _BATCH]
            _o, stderr, rc = runner(["nix-store", "--realise", *batch])
            if rc != 0:
                decoded = stderr.decode("utf-8", errors="replace").strip()
                raise PreflightError(
                    "native build-host closure realise failed "
                    f"(rc={rc}): {decoded[-1000:]}"
                )

    # 2. Union of --include-outputs build-closure OUTPUT paths across all
    #    toolchains (the drv files are shipped separately via the drv archive).
    bc_outputs: set[str] = set()
    for drv in drvs:
        out, err, rc = runner([
            "nix-store", "--query", "--requisites", "--include-outputs", drv,
        ])
        if rc != 0:
            decoded = err.decode("utf-8", errors="replace").strip()
            raise PreflightError(
                f"toolchain build-closure query failed for {drv} (rc={rc}): "
                f"{decoded[-1000:]}"
            )
        for line in out.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.endswith(".drv"):
                bc_outputs.add(line)
    if not bc_outputs:
        return
    ordered = sorted(bc_outputs)

    # 3 + 4. Realise the union (substitute the bootstrap) and gc-root it in one
    #    batched pass. A fresh gcroot dir each call keeps this run's roots
    #    bounded (no unbounded accumulation across submits). ``--add-root``
    #    with N paths creates <link>, <link>-2, <link>-3 … all registered as
    #    indirect gcroots, so one call roots a whole batch.
    root_base: Optional[pathlib.Path] = None
    if gcroot_dir:
        root_base = pathlib.Path(gcroot_dir)
        if root_base.exists():
            shutil.rmtree(root_base, ignore_errors=True)
        root_base.mkdir(parents=True, exist_ok=True)

    for i in range(0, len(ordered), _BATCH):
        batch = ordered[i:i + _BATCH]
        cmd = ["nix-store", "--realise"]
        if root_base is not None:
            cmd += [
                "--add-root", str(root_base / f"tc-build-closure-{i // _BATCH}"),
                "--indirect",
            ]
        cmd += batch
        _stdout, stderr, rc = runner(cmd)
        if rc != 0:
            decoded = stderr.decode("utf-8", errors="replace").strip()
            raise PreflightError(
                "local toolchain build-closure realise/root failed "
                f"(rc={rc}): {decoded[-1000:]}"
            )


# ---------------------------------------------------------------------------
# Build-deps output closure (build_deps_local feature)
# ---------------------------------------------------------------------------


def compute_build_deps_closure(
    input_outpaths: list[str],
    *,
    toolchain_paths_to_subtract: Optional[frozenset[str]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[list[str], list[str]]:
    """Compute the ordered requisites closure of ``input_outpaths``, minus
    any paths already shipped by toolchain archives.

    ``input_outpaths`` are the realised OUTPUT paths of every build-input
    derivation that ``nix build variant.drv^*`` needs locally before it can
    build (e.g. glibc, libgcc, gcc, stdenv, bash, coreutils, …).  These
    must already be realised in the local nix store (caller is responsible
    for substituting/realising them first).

    ``toolchain_paths_to_subtract`` is the union of every store path already
    shipped by the toolchain archives (``toolchains.common.archive`` +
    every ``toolchains.<id>.out.archive``).  Subtracting them avoids
    re-shipping data the build workers already imported.

    Returns ``(ordered_build_deps_paths, uncovered_paths)`` where:

    * ``ordered_build_deps_paths`` is the topologically-ordered list of
      store paths to export into :data:`BUILD_DEPS_ARCHIVE_NAME`.  Topological
      order is preserved from the ``nix-store --query --requisites`` output so
      ``nix-store --import`` can register them in dependency order.
    * ``uncovered_paths`` is the list of seed outpaths that either were not
      realised locally (not present in the requisites output) or were silently
      absent from the query result — the caller uses this for the completeness
      gate.

    Raises :class:`RuntimeError` if ``nix-store --query --requisites`` exits
    non-zero on any seed path.  An empty ``input_outpaths`` list returns two
    empty lists immediately.
    """
    import logging as _logging  # noqa: PLC0415
    _log = _logging.getLogger("compiler_suit_runner.preflight.build_deps")

    if not input_outpaths:
        return [], []

    runner = run_subprocess or _default_run_subprocess
    subtract: frozenset[str] = toolchain_paths_to_subtract or frozenset()

    # Run a single batched requisites query across all seed paths.
    req_stdout, req_stderr, req_rc = runner(
        ["nix-store", "--query", "--requisites", *input_outpaths]
    )
    if req_rc != 0:
        raise RuntimeError(
            "compute_build_deps_closure: nix-store --query --requisites "
            "failed (rc=%d): %s" % (
                req_rc,
                req_stderr.decode("utf-8", errors="replace").strip(),
            )
        )

    all_requisites: list[str] = [
        ln for ln in (
            raw.strip()
            for raw in req_stdout.decode("utf-8", errors="replace").splitlines()
        )
        if ln
    ]
    req_set: frozenset[str] = frozenset(all_requisites)

    # Identify seed outpaths absent from the requisites output (not
    # realised locally) so the caller can fail closed.
    uncovered: list[str] = [
        op for op in input_outpaths
        if op and op not in req_set
    ]
    if uncovered:
        _log.warning(
            "compute_build_deps_closure: %d input outpath(s) not found in "
            "requisites output (not locally realised): %s",
            len(uncovered),
            ", ".join(uncovered[:5]) + (" …" if len(uncovered) > 5 else ""),
        )

    # Filter out paths already shipped by toolchain archives, preserving
    # topological order (nix-store --query --requisites guarantees
    # dependency-first ordering).
    build_deps_paths: list[str] = [
        p for p in all_requisites
        if p not in subtract
    ]
    return build_deps_paths, uncovered


def collect_toolchain_archive_paths(
    toolchain_out_paths: list[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> frozenset[str]:
    """Return the union of requisites closures for all toolchain out-paths.

    Used by the build_deps_local feature to compute the set of paths that
    are already covered by the toolchain archives so they can be subtracted
    from the build-deps closure to avoid duplication.

    Silently skips out-paths whose requisites query fails (one bad toolchain
    does not zero the subtraction set).  Returns an empty frozenset if no
    toolchain produces a valid requisites list.
    """
    import logging as _logging  # noqa: PLC0415
    _log = _logging.getLogger("compiler_suit_runner.preflight.build_deps")
    runner = run_subprocess or _default_run_subprocess
    all_paths: set[str] = set()
    for outpath in toolchain_out_paths:
        if not outpath or not outpath.startswith("/"):
            continue
        stdout, stderr, rc = runner(
            ["nix-store", "--query", "--requisites", outpath]
        )
        if rc != 0:
            _log.warning(
                "collect_toolchain_archive_paths: requisites failed for "
                "%r (rc=%d): %s — skipping",
                outpath, rc,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            continue
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                all_paths.add(line)
    return frozenset(all_paths)


def export_build_deps_archive(
    build_deps_paths: list[str],
    out_dir: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> pathlib.Path:
    """Export ``build_deps_paths`` as :data:`BUILD_DEPS_ARCHIVE_NAME`.

    Writes the archive atomically via ``.tmp`` + ``os.replace``.
    The paths MUST be in topological order (dependency-first) as produced
    by :func:`compute_build_deps_closure`.

    Returns the written archive path.  Raises :class:`RuntimeError` on
    export failure.
    """
    from compiler_suit_runner.workers.build_compilers_worker import (  # noqa: PLC0415
        export_closure_exact,
    )
    archive_path = out_dir / BUILD_DEPS_ARCHIVE_NAME
    if not build_deps_paths:
        raise RuntimeError(
            "export_build_deps_archive: no paths to export — "
            "build_deps closure is empty after subtracting toolchain paths"
        )
    ok, exp_stderr = export_closure_exact(
        archive_path,
        build_deps_paths,
        run_subprocess=run_subprocess,
    )
    if not ok:
        raise RuntimeError(
            "export_build_deps_archive: nix-store --export failed: "
            + exp_stderr.decode("utf-8", errors="replace").strip()
        )
    return archive_path

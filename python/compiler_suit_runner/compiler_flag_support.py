"""Compiler-flag support tables — single source of truth for the runner.

When preflight enumerates variants, some `(compiler, version, flag)`
combinations are known to fail at build time because the compiler
version predates the flag. The matrix builder in `lib/matrix.nix`
already filters by `minGccVersion` / `minClangVersion` hints in
`lib/flags.nix` — this module mirrors that knowledge on the Python
side so the dispatch runner can:

1. Strip flags from `NIX_HARDENING_ENABLE` for compiler versions
   that don't support them (mirrors `lib/old-compilers.nix`'s
   `getClangUnsupportedHardeningFlags`).
2. Report at preflight time which `(arch, compiler, hardening)`
   combinations would silently fail vs which would dispatch.
3. Force mandatory per-(arch, compiler) flags into the wrapper for
   pairs where the modern glibc/libgcc + old compiler hybrid only
   links with a specific ABI (e.g. `-mabi=n32` for
   `mips64el-...-gnuabin32 + clang <= 7`).

Data sources:
- `docs/clang-flag-support-matrix.md` (web-sourced from LLVM
  release notes + cross-checked against in-store binaries)
- `docs/gcc-flag-support-matrix.md` (web-sourced from
  gcc.gnu.org/onlinedocs Option-Summary pages)
- `lib/flags.nix` (the project's own minGccVersion/minClangVersion
  guards, confirmed accurate or conservative by the subagent runs)

Both .md files are the authoritative reference; this module is the
machine-readable mirror. **When bumping nixpkgs, regenerate the
matrices via the subagent prompts in `docs/franken-toolchain-anatomy.md`
and update the tables below.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CompilerFamily = Literal["gcc", "clang"]


@dataclass(frozen=True)
class VersionGate:
    """`(family, major, minor)` triple ordered for ``<`` comparison."""

    family: CompilerFamily
    major: int
    minor: int

    def at_least(self, other_major: int, other_minor: int = 0) -> bool:
        """True if this version is >= `(other_major, other_minor)`."""
        return (self.major, self.minor) >= (other_major, other_minor)


# ---------------------------------------------------------------------------
# Hardening flags + the compiler versions that introduced them
# ---------------------------------------------------------------------------
#
# Keys are nixpkgs cc-wrapper hardening-flag NAMES (the ones the wrapper's
# add-hardening.sh reads from `NIX_HARDENING_ENABLE`); values are the
# minimum `(major, minor)` of each family that accepts the underlying
# compiler flag.
#
# When a compiler is older than the introduction version, the corresponding
# hardening name must be added to ``hardeningUnsupportedFlags`` so the
# cc-wrapper strips it before invoking the compiler. Otherwise the wrapper
# passes the flag through and the compiler errors with "unknown argument",
# autoconf's compile test fails, and the whole variant build dies with
# "C compiler cannot create executables" — a silent class of bug that
# previously cost us 9 of the 16 wrapper-builds-but-fails entries in
# `table.md`.
_HARDENING_INTRODUCED: dict[str, dict[CompilerFamily, tuple[int, int]]] = {
    "stackprotector": {
        # -fstack-protector-strong
        "gcc": (4, 8),  # Option-Summary 4.8
        "clang": (3, 5),  # LLVM 3.5 release notes
    },
    "stackclashprotection": {
        # -fstack-clash-protection
        "gcc": (8, 0),  # GCC 8
        "clang": (11, 0),  # LLVM 11 release notes
    },
    "zerocallusedregs": {
        # -fzero-call-used-regs=used-gpr
        "gcc": (11, 0),  # GCC 11
        "clang": (15, 0),  # confirmed via binary tests
    },
    "strictflexarrays1": {
        # -fstrict-flex-arrays=1 — the flag nixpkgs adds by default
        # circa nixpkgs-unstable 2024+. Stripping it was the fix
        # that recovered mips64el+clang3_4..clang7 variants from
        # autoconf-fails-with-cannot-create-executables.
        "gcc": (13, 0),  # GCC 13
        "clang": (16, 0),  # LLVM 16
    },
    "strictflexarrays3": {
        # -fstrict-flex-arrays=3
        "gcc": (13, 0),
        "clang": (16, 0),
    },
    "cet": {
        # -fcf-protection=full (Intel CET — x86 only)
        "gcc": (8, 0),
        "clang": (7, 0),
    },
    "branchprotection": {
        # -mbranch-protection=standard (aarch64 BTI+PAC)
        "gcc": (9, 0),
        "clang": (8, 0),
    },
    "staticpie": {
        # -static-pie (linker + driver)
        "gcc": (8, 0),
        "clang": (9, 0),
    },
}


def unsupported_hardening_flags(
    family: CompilerFamily, major: int, minor: int
) -> frozenset[str]:
    """Return the cc-wrapper hardening flag names this `(family, version)`
    pair does NOT support.

    Pass the result as ``hardeningUnsupportedFlags`` (nix wrapper attr)
    OR as the set to subtract from ``NIX_HARDENING_ENABLE`` when
    composing wrapper environments.
    """
    out: set[str] = set()
    for name, intro in _HARDENING_INTRODUCED.items():
        intro_major, intro_minor = intro.get(family, (0, 0))
        if (major, minor) < (intro_major, intro_minor):
            out.add(name)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Flag-set / sanitizer / opt flags — first version that accepts them
# ---------------------------------------------------------------------------
#
# These mirror ``minGccVersion`` / ``minClangVersion`` guards in
# `lib/flags.nix`. The matrix builder already drops variants whose
# (compiler, flag) combination predates these versions, but the Python
# runner can use this map for additional preflight diagnostics.
_FLAG_INTRODUCED: dict[str, dict[CompilerFamily, tuple[int, int]]] = {
    # opt levels: all supported by every version in scope
    # flag sets that aren't universal:
    "lto": {  # -flto
        "gcc": (4, 5),  # actually 4.5; flags.nix guards at 4.6 (safe)
        "clang": (3, 4),
    },
    "ltothin": {  # -flto=thin
        "gcc": (10, 0),  # GCC 10 added thinlto
        "clang": (3, 8),  # driver-accepted 3.8; functional 3.9
    },
    "march-x86_64-v2": {
        "gcc": (11, 0),
        "clang": (12, 0),
    },
    "march-x86_64-v3": {
        "gcc": (11, 0),
        "clang": (12, 0),
    },
    "march-x86_64-v4": {
        "gcc": (11, 0),
        "clang": (12, 0),
    },
    "sanitize-address": {
        "gcc": (4, 8),
        "clang": (3, 4),
    },
    "sanitize-thread": {
        "gcc": (4, 8),
        "clang": (3, 4),
    },
    "sanitize-undefined": {
        "gcc": (4, 9),
        "clang": (3, 4),
    },
    "sanitize-memory": {
        # gcc has no msan
        "clang": (3, 4),
    },
    "fortify3": {
        # -D_FORTIFY_SOURCE=3 — preprocessor accepts everywhere
        # but effective only on gcc 12+ / clang 14+ with glibc 2.35+
        "gcc": (12, 0),
        "clang": (14, 0),
    },
}


def flag_supported(
    family: CompilerFamily, major: int, minor: int, flag_key: str
) -> bool:
    """Return True iff this `(family, version)` supports ``flag_key``.

    ``flag_key`` is a nix-style flag-set / sanitizer / hardening
    identifier (e.g. ``"lto"``, ``"sanitize-memory"``,
    ``"stackprotector"``). Unknown keys default to ``True`` — only
    keys with explicit version gates here are tracked.
    """
    table = _FLAG_INTRODUCED.get(flag_key) or _HARDENING_INTRODUCED.get(flag_key)
    if table is None:
        return True
    intro = table.get(family)
    if intro is None:
        return False  # family has no support at all (e.g. gcc + msan)
    return (major, minor) >= intro


# ---------------------------------------------------------------------------
# Mandatory per-(arch, compiler) flags
# ---------------------------------------------------------------------------
#
# Hybrid franken-toolchains (old compiler binary + modern binutils/libgcc/
# glibc) sometimes need explicit ABI selection to make codegen match the
# runtime libraries the modern toolchain provides.
#
# Mirrors `lib/old-compilers.nix:abiFlagsFor`. Kept here so a Python
# consumer (preflight, manifest-emission) can surface "this variant will
# pass extra ABI flags" without re-evaluating the nix expression.
_MANDATORY_ABI_FLAGS: list[tuple[str, CompilerFamily, int, int, tuple[str, ...]]] = [
    # (arch_label, family, max_major, max_minor, flags)
    # mips64el-...-gnuabin32: modern libgcc is N32; clang <= 7 may
    # default to N64 or O32 under this triple. Force N32 codegen.
    ("mips64el", "clang", 7, 999, ("-mabi=n32",)),
    # riscv64-...-gnu: modern glibc is double-float lp64d; clang 9
    # may default to soft-float. Force lp64d + rv64gc march.
    ("riscv64", "clang", 9, 0, ("-mabi=lp64d", "-march=rv64gc")),
]


def mandatory_flags_for(
    arch: str, family: CompilerFamily, major: int, minor: int
) -> tuple[str, ...]:
    """Return additional flags that MUST be passed to make this
    (arch, family, version) combination produce working binaries.

    Empty tuple if no override is needed. Multiple flags concatenate
    in the order they appear.
    """
    for arch_label, fam, max_major, max_minor, flags in _MANDATORY_ABI_FLAGS:
        if arch_label != arch or fam != family:
            continue
        if (major, minor) <= (max_major, max_minor):
            return flags
    return ()


# ---------------------------------------------------------------------------
# Known unrecoverable (arch, compiler) combinations
# ---------------------------------------------------------------------------
#
# Even with all flag-strips and ABI-mandatory overrides applied, some
# combinations still fail to build a hello binary. Documented here so
# preflight can surface them as "skip — known broken" rather than
# letting the dispatch waste a worker on them.
#
# Each entry: (arch, family, max_major, max_minor, reason).
_KNOWN_BROKEN: list[tuple[str, CompilerFamily, int, int, str]] = [
    # ppc64 + clang 3.4 / 3.5: clang's integrated-as for ppc64 was
    # incomplete in 3.x; the driver falls back to external `as` which
    # isn't on the wrapper's PATH for this triple.
    ("ppc64", "clang", 3, 5, "clang3.x lacks ppc64 integrated-as"),
    # aarch64 + clang 3.5: manually compiles fine but variant build
    # fails in some autoconf-specific configuration we haven't
    # narrowed down (probably an interaction with hardening flags
    # not enumerated above).
    ("aarch64", "clang", 3, 5, "variant-build vs manual-compile divergence"),
    # mips64el + clang 3.x: see comment on KNOWN_BROKEN — abi=n32
    # fix recovered clang3_4-7, but if any new clang3.x cross-target
    # fails, add it here.
]


def is_known_broken(
    arch: str, family: CompilerFamily, major: int, minor: int
) -> str | None:
    """Return a reason string if this combination is known to fail,
    None otherwise."""
    for arch_label, fam, max_major, max_minor, reason in _KNOWN_BROKEN:
        if arch_label != arch or fam != family:
            continue
        if (major, minor) <= (max_major, max_minor):
            return reason
    return None

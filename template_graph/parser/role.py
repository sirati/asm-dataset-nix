"""Role extraction for matrix-variant template alignment.

The post-hash drv name carries variant-specific axes (target triple,
package version) that vary across the matrix expansion. The *role* is
the variant-axis-stripped form used as a positional key when matching
the same template position across variants.

Public surface:
    drv_role(name) -> str
        Variant-axis-stripped name (target triples and version segments
        removed; compiler-wrapper roles collapse to a unified token).
    _is_stdenv_role(role) -> bool
        Whether ``role`` denotes a stdenv-bearing drv (matrix-variant
        terminal: subtree is siphoned for separate stdenv template
        construction rather than recursed into).
    _is_compiler_wrapper_role(role) -> bool
        Whether ``role`` denotes a compiler-wrapper drv. All wrappers
        collapse to ``_UNIFIED_COMPILER_WRAPPER_ROLE`` so a node with
        both gcc-wrapper and clang-wrapper children groups as one slot.

The supporting regexes (``_TRIPLE_RE``, ``_EXT_RE`` etc.) are exported
for use by other modules that need to recognise the same axes (e.g.
DAG-revisit diff classifiers in ``streaming``).
"""

from __future__ import annotations

import re


# Target triple/quad components. Order is canonical
# <arch>-<vendor>-<os>-<abi>; the separator between components must be
# consistent (here we accept all-dash or all-underscore — nixpkgs uses
# dashes, but the latter shape appears in some upstream toolchain
# names). Component vocabulary follows Rust/LLVM target naming.
_TRIPLE_ARCH = (
    r"aarch64(?:_be)?|"
    r"arm(?:eb)?|"
    r"armv[1-9](?:[a-z]+)?|"
    r"avr|"
    r"bpf(?:el|eb)|"
    r"hexagon|"
    r"i[3-6]86|"
    r"loongarch64|"
    r"m68k|"
    r"mips(?:64)?(?:el)?|"
    r"mipsisa(?:32|64)r6(?:el)?|"
    r"msp430|"
    r"nvptx(?:64)?|"
    r"or1k|"
    r"powerpc(?:64)?(?:le)?|"
    r"riscv(?:32|64)|"
    r"s390x?|"
    r"sparc(?:64|el|v9)?|"
    r"thumbv[1-9][a-z]*|"
    r"wasm(?:32|64)|"
    r"x86_64"
)
_TRIPLE_VENDOR = (
    r"unknown|pc|apple|ibm|none|sun|nvidia|"
    r"nintendo|fortanix|esp|sony"
)
_TRIPLE_OS = (
    r"linux|darwin|ios|macos|tvos|watchos|"
    r"freebsd|openbsd|netbsd|dragonfly|"
    r"windows|redox|fuchsia|illumos|solaris|"
    r"haiku|hermit|hurd|l4re|nto|wasi|"
    r"vxworks|espidf|mingw32|none"
)
_TRIPLE_ABI = (
    r"gnu(?:eabi(?:hf)?|abin32|abi64|abielfv[12](?:qb)?)?|"
    r"musl(?:eabi(?:hf)?)?|"
    r"msvc(?:llvm)?|"
    r"eabi(?:hf)?|"
    r"elf|ilp32|sgx|cuda|"
    r"newlib|uclibc(?:eabi(?:hf)?)?|"
    r"ohos"
)
# Nodes whose role-name matches this pattern are treated as terminals
# during matrix-variant template construction: no descent, no hash
# recording, but their raw-tree subtrees are siphoned off for separate
# stdenv template construction later. Setup-hook divergence (e.g.
# stage-2 stdenv pulling in ``separate-debug-info.sh`` while stage-1
# doesn't) lives inside stdenv and shouldn't pollute matrix templates.
# Role pattern for stdenv derivations only (post-hash, post-version,
# .drv-bearing). Anchors to the *whole* role so false positives like
# ``source-stdenv.sh`` (script, not drv) and
# ``bootstrap-stage3-gcc-wrapper.drv`` (gcc-wrapper, not stdenv) are
# rejected.
_STDENV_RE = re.compile(
    r"^(?:bootstrap-stage(?:[0-9]+|-xgcc)-)?"
    r"stdenv(?:-linux(?:-boot)?)?\.drv$"
)


def _is_stdenv_role(role: str) -> bool:
    return _STDENV_RE.search(role) is not None


# Compiler-wrapper roles all collapse to a single unified
# role so a node with both gcc-wrapper and clang-wrapper children
# (e.g. netpbm's CC_FOR_BUILD pattern) groups as one slot. The role
# also short-circuits to is_toolchain at allocation time.
_UNIFIED_COMPILER_WRAPPER_ROLE = "wrapped-compiler-suit.drv"
_COMPILER_WRAPPER_ROLE_RE = re.compile(
    r"^(?:gcc|clang|cc|tcc|icc|gccgo|ldc)-wrapper\.drv$"
)


def _is_compiler_wrapper_role(role: str) -> bool:
    return (
        role == _UNIFIED_COMPILER_WRAPPER_ROLE
        or _COMPILER_WRAPPER_ROLE_RE.match(role) is not None
    )


# Source-terminal roles: arch-independent build inputs (source tarballs,
# fetched archives, builder scripts, patches, setup-hook shell snippets)
# that always resolve via cache rather than per-variant compilation.
# Recognising them by role-pattern lets the streaming planner skip
# entering them as template nodes (no per-variant cell tracking) and
# emit no ``build_common_dep`` task downstream (cache substitutes them).
#
# All checks are pure string-pattern on the role name as produced by
# ``drv_role``. NO nix call.
_SOURCE_TERMINAL_RES: tuple[re.Pattern[str], ...] = (
    # ``*-source`` (post-drv-suffix) — git/fetchgit checkouts, etc.
    re.compile(r"-source(?:\.drv)?$"),
    # Archive tarballs of the common compressions. ``drv_role`` retains
    # extensions up to 2 levels so the role ends with ``.tar.<ext>(.drv)``.
    re.compile(
        r"\.tar\.(?:gz|xz|bz2|bz|zst|lz|lzma|Z|7z|lz4)(?:\.drv)?$"
    ),
    # ``fetchurl-…`` (and the ``builtins.fetchurl`` family) drv names.
    re.compile(r"(?:^|-)fetchurl(?:-|\.drv$|$)"),
    # Builder scripts inlined as separate drvs (uncommon but real).
    # ``(?:^|-)`` so a bare ``builder.sh`` (no dash prefix) is matched too,
    # not only ``<pkg>-builder.sh``.
    re.compile(r"(?:^|-)builder\.(?:sh|pl)(?:\.drv)?$"),
    # Patch files referenced as drv inputs.
    re.compile(r"\.patch(?:\.drv)?$"),
    # Setup-hook shell snippets.
    re.compile(r"-setup-hook(?:\.sh)?(?:\.drv)?$"),
)


def _is_source_terminal_role(role: str) -> bool:
    """Whether ``role`` denotes an arch-independent source-terminal:
    source tarball, fetched archive, builder script, patch, or
    setup-hook. These don't need per-variant template tracking and
    don't get ``build_common_dep`` tasks emitted (cache substitutes
    them); they're still recorded in per-binary ``arch_indep_deps``
    for diagnostic counting."""
    return any(rx.search(role) is not None for rx in _SOURCE_TERMINAL_RES)


_TRIPLE_RE = re.compile(
    rf"(?:^|-)(?:{_TRIPLE_ARCH})-(?:{_TRIPLE_VENDOR})"
    rf"-(?:{_TRIPLE_OS})-(?:{_TRIPLE_ABI})"
    rf"|(?:^|_)(?:{_TRIPLE_ARCH})_(?:{_TRIPLE_VENDOR})"
    rf"_(?:{_TRIPLE_OS})_(?:{_TRIPLE_ABI})"
)


_QUALIFIER_RE = re.compile(
    r"^(unstable|rc\d*|pre\d*|p\d+|alpha\d*|beta\d*|dev\d*)$"
)
_EXT_RE = re.compile(
    r"\.(?P<e1>[a-z][a-z0-9]*)(?:\.(?P<e2>[a-z0-9]+))?$"
)


def _strip_version(body: str) -> str:
    m = re.search(r"-\d", body)
    if not m:
        return body
    start = m.start()
    i = start + 1
    while i < len(body):
        c = body[i]
        if c.isdigit() or c == ".":
            i += 1
            continue
        if c == "-":
            j = i + 1
            while j < len(body) and body[j] not in "-.":
                j += 1
            chunk = body[i + 1 : j]
            if chunk.isdigit() or _QUALIFIER_RE.match(chunk):
                i = j
                continue
            break
        if c.isalpha() and i + 1 < len(body) and body[i + 1].isdigit():
            j = i + 1
            while j < len(body) and body[j].isdigit():
                j += 1
            i = j
            continue
        break
    return body[:start] + body[i:]


def drv_role(name: str) -> str:
    """Variant-axis-stripped name used for matching the same template
    position across variants. ``name`` is the post-hash store-path
    name (as produced by ``_parse_line``).

    Strips: target triples, version segments. Retains extensions up to
    2 levels, optional ``.<digits>`` and ``.drv``.
    """
    base = name
    has_drv = base.endswith(".drv")
    if has_drv:
        base = base[:-4]
    digit_suffix = ""
    m = re.search(r"\.(\d+)$", base)
    if m and re.search(r"\.[a-z][a-z0-9]*$", base[: m.start()]):
        digit_suffix = m.group(0)
        base = base[: m.start()]
    ext = ""
    m = _EXT_RE.search(base)
    if m and not m.group("e1").isdigit():
        ext = m.group(0)
        base = base[: m.start()]
    body = _TRIPLE_RE.sub("", base)
    body = _strip_version(body)
    body = re.sub(r"-{2,}", "-", body).strip("-")
    suffix = ext + digit_suffix + (".drv" if has_drv else "")
    role = body + suffix
    if _COMPILER_WRAPPER_ROLE_RE.match(role):
        return _UNIFIED_COMPILER_WRAPPER_ROLE
    return role

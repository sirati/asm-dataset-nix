"""Project-specific name extractor for the asm-dataset-nix matrix.

Hooks into the algorithm's ``name_extractor`` slot. The matrix bakes
variant axes into the top-level drv name (``hello-<arch>-<compiler>-
<opt>-<flags>-<hardening>-<sanitizer>-<march>-elf-folder``) and uses
cross-compilation target triples as infixes deep in the closure
(e.g. ``hello-variant-aarch64-unknown-linux-gnu-2.12.3``). To make
different variants land on the SAME template-node names we strip
those axes here.

The toolchain wrappers are canonicalised:
``gcc-wrapper`` / ``clang-wrapper`` → ``cc-wrapper`` so a gcc15 and
clang21 variant of the same arch share one template position for the
compiler.

Pure-string transforms; no nix call.
"""

from __future__ import annotations


# Longest first so "ppc64" beats "ppc32" and "mips64el" beats "mipsel".
_ARCH_PREFIXES = [
    "x86_64",
    "aarch64",
    "armv7l",
    "ppc64",
    "ppc32",
    "mips64el",
    "mipsel",
    "riscv64",
    "i686",
]


def _strip_store_prefix(name: str) -> str:
    if name.endswith(".drv"):
        name = name[:-4]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
        if "-" in name:
            name = name.split("-", 1)[1]
    return name


def _canonicalise_toolchain_family(name: str) -> str:
    # Collapse gcc-wrapper / clang-wrapper into one family AND drop
    # the wrapper version (15.2.0 / 21.1.8 / ...). Version-strip is
    # NEEDED here so gcc15 and clang21 variants of the same arch
    # share one template position for the compiler; we deliberately
    # don't strip versions on regular packages because bootstrap
    # chains carry distinct same-name same-version drvs at different
    # stages — collapsing them would trip false DAG-revisit asserts.
    for family in ("gcc-wrapper", "clang-wrapper"):
        idx = name.find(family)
        if idx >= 0:
            head = name[:idx]
            return head + "cc-wrapper"
    return name


# Source archive / patch suffixes. If a drv name ends in one of these
# it's a fetched source, NOT a built output — we tag it ``-source`` so
# e.g. ``bash-5.3.tar.gz`` doesn't collide with the built ``bash-5.3p9``
# at the same template position.
_SOURCE_SUFFIXES = (
    ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar.lz",
    ".tar", ".tgz", ".tbz", ".tbz2",
    ".zip", ".patch", ".diff",
)


def _strip_source_suffix(name: str) -> tuple[str, bool]:
    for sfx in _SOURCE_SUFFIXES:
        if name.endswith(sfx):
            return name[: -len(sfx)], True
    return name, False


def hello_name_extractor(drv_record_or_name) -> str:
    """Strip variant axes + canonicalise toolchain family.

    Examples (drv path → returned name):

        hello-x86_64-gcc15-O0-...-elf-folder.drv  -> hello
        hello-variant-2.12.3.drv                  -> hello-variant
        hello-variant-aarch64-...-2.12.3.drv      -> hello-variant
        gcc-wrapper-15.2.0.drv                    -> cc-wrapper
        clang-wrapper-21.1.8.drv                  -> cc-wrapper
        aarch64-...-gcc-wrapper-15.2.0.drv        -> aarch64-unknown-linux-gnu-cc-wrapper
        stdenv-linux.drv                          -> stdenv-linux
    """
    if isinstance(drv_record_or_name, dict):
        raw = drv_record_or_name.get("name", "")
    else:
        raw = drv_record_or_name
    if not isinstance(raw, str) or not raw:
        return ""
    name = _strip_store_prefix(raw)
    name, is_source = _strip_source_suffix(name)
    src_tag = "-source" if is_source else ""
    name = _canonicalise_toolchain_family(name)
    # Defensive: if the name is exactly "<arch>__<suffix>" (a label,
    # not a drv), don't molest it.
    if "__" in name and "/" not in raw:
        return name + src_tag
    # Strip any "-<arch>-..." infix (variant or cross-compile target).
    # This collapses the matrix's variant-suffixed elf-folder root
    # (``hello-x86_64-gcc15-...``) AND cross-compiled inner drvs
    # (``hello-variant-aarch64-...-2.12.3``) onto stable names.
    #
    # IMPORTANT: we do NOT strip version digits here. Bootstrap
    # chains contain multiple versions of the same package
    # (``bash-5.3`` from bootstrap-tools vs ``bash-5.3p9`` from the
    # final stdenv) — collapsing them would merge two genuinely
    # different graph positions and trip the DAG-revisit assert.
    for arch in _ARCH_PREFIXES:
        marker = f"-{arch}-"
        idx = name.find(marker)
        if idx > 0:
            return name[:idx] + src_tag
    return name + src_tag

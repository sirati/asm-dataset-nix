"""Line-level helpers for parsing ``nix-store --query --tree`` output.

The streaming planner (``template_graph.streaming``) consumes this
output line-by-line; this module supplies the per-line decoder
(``_parse_line``), the variant-filename parser
(``parse_variant_path``), the shared regexes / constants for the
sum-tree shape (``_MATRIX_RE``, ``_TOOLCHAINS_RE``,
``VARIANT_SUFFIX``), and the error type (``TreeWalkError``).

The two-phase walker (``walk`` + ``plan_from_tree`` +
``bucket_variants``) has been retired in favour of the single-pass
streaming planner — no full tree buffer, no follow-up
``nix derivation show`` traffic.
"""

from __future__ import annotations

import re
from typing import Iterator

BACKREF_SUFFIX = " [...]"
# Each indentation level is a 4-character segment: '│   ', '    ',
# '├───', '└───'. Codepoint counts match Python string indices because
# the box-drawing chars are single BMP codepoints.
_SEG_LEN = 4
_INDENT_SEGS = ("│   ", "    ")
_CONNECTOR_SEGS = ("├───", "└───")

# Variant filename suffix from `lib/mkVariant.nix`. Stable enough to be
# the matrix-level entry-point marker.
VARIANT_SUFFIX = "-elf-folder.drv"
# Regexes match the post-hash drv_name (no hash prefix anymore).
_MATRIX_RE = re.compile(r"^matrix-(?P<binary>[a-zA-Z0-9_+\-.]+)\.drv$")
_TOOLCHAINS_RE = re.compile(r"^toolchains\.drv$")


class TreeWalkError(RuntimeError):
    """Raised when the input tree violates an ordering / shape assumption."""


_STORE_PREFIX = "/nix/store/"
_STORE_PREFIX_LEN = len(_STORE_PREFIX)
_HASH_LEN = 32  # nix base32 hash is always 32 chars + dash


def _parse_line(line: str) -> tuple[int, bytes, str, bool]:
    """Return ``(depth, drv_hash, drv_name, is_backref)``. Root line → depth 0.

    ``drv_hash`` is the 32-byte ASCII base32 store-path hash; ``drv_name``
    is the post-hash basename string. The ``/nix/store/`` prefix is
    implied. For lines that don't start with the store prefix (only the
    root in practice), ``drv_hash=b""`` and ``drv_name`` carries the raw
    rest.
    """
    i = 0
    depth = 0
    while i + _SEG_LEN <= len(line):
        seg = line[i : i + _SEG_LEN]
        if seg in _INDENT_SEGS:
            depth += 1
            i += _SEG_LEN
            continue
        if seg in _CONNECTOR_SEGS:
            depth += 1
            i += _SEG_LEN
            break
        break
    rest = line[i:]
    is_backref = rest.endswith(BACKREF_SUFFIX)
    if is_backref:
        rest = rest[: -len(BACKREF_SUFFIX)]
    if rest.startswith(_STORE_PREFIX):
        body_start = _STORE_PREFIX_LEN
        drv_hash = rest[body_start : body_start + _HASH_LEN].encode("ascii")
        drv_name = rest[body_start + _HASH_LEN + 1 :]
    else:
        drv_hash = b""
        drv_name = rest
    return depth, drv_hash, drv_name, is_backref


_PREFIX = b"/nix/store/"
_PREFIX_LEN = 11
_BACKREF = b" [...]"
_BACKREF_LEN = 6


def parse_line_bytes(line: bytes) -> tuple[int, bytes, str, bool]:
    """Decode one ``nix-store --query --tree`` line into
    ``(depth, drv_hash, drv_name, is_backref)``.

    ``drv_hash`` is the raw 32-byte base32 ASCII hash; ``drv_name``
    is the decoded post-hash basename. Raises ``ValueError`` on
    malformed indent (mis-aligned segments or missing connector).
    """
    is_backref = line.endswith(_BACKREF)
    end = len(line) - _BACKREF_LEN if is_backref else len(line)
    offset = line.find(_PREFIX, 0, end)
    if offset < 0:
        raise ValueError(f"no /nix/store/ in line: {line!r}")
    e2_count = line.count(b"\xe2", 0, offset)
    if (offset - 2 * e2_count) & 0b11:
        raise ValueError(
            f"indent bytes={offset} e2={e2_count} not divisible "
            f"into 4-codepoint segments: {line!r}"
        )
    if offset > 0 and e2_count < 4:
        raise ValueError(
            f"indent has no connector (e2={e2_count} < 4): {line!r}"
        )
    depth = (offset - 2 * e2_count) >> 2
    h = offset + _PREFIX_LEN
    return (
        depth,
        line[h:h + _HASH_LEN],
        line[h + _HASH_LEN + 1:end].decode("ascii"),
        is_backref,
    )


# ─── Variant filename → (binary, arch, comp, opt) ───────────────────

# Architectures from lib/architectures.nix. Longest-first so that
# `armv7l-hf` matches before `armv7l` would (defensive — current
# architectures.nix doesn't have a bare `armv7l`, but keeping the rule
# means renames don't silently mis-parse).
DEFAULT_ARCHS = (
    "armv7l-hf", "armv7l-sf",
    "mips64el", "mipsel",
    "aarch64", "x86_64", "riscv64",
    "ppc32", "ppc64",
    "i686",
)

_VARIANT_RE = re.compile(
    # ``<rest>-<flag_set>-<hardening...>-san-<sanitizer>-march-<march>-elf-folder.drv``
    #
    # ``rest`` is anchored to end on the opt-level token
    # (``-O0``…``-Ofast``, ``-Odefault``, ``-Os``, ``-Og``, ``-Oz``)
    # so the variable-width hardening field on the right doesn't slurp
    # the opt into it. The original pattern required the baseline
    # quadruple (``baseline-default-san-off-march-default``) verbatim,
    # which tripped TreeWalkError on every non-baseline sampled
    # variant. ``parse_variant_path`` only returns
    # ``(binary, arch, comp, opt)`` — flag_set / hardening / sanitizer
    # / march never reach the caller — so the inner axes use ``.*?``
    # between ``-<flag_set>-`` and ``-san-`` to absorb the (possibly
    # multi-token) hardening value.
    r"^(?P<rest>.+?-(?:O[0-9]|Os|Og|Oz|Ofast|Odefault))"
    r"-(?P<flag_set>[A-Za-z0-9]+)"
    r"-(?P<hardening>.*?)"
    r"-san-(?P<sanitizer>[A-Za-z0-9_+]+)"
    r"-march-(?P<march>[A-Za-z0-9_+]+)"
    r"-elf-folder\.drv$"
)
_OPT_RE = re.compile(r"-(O[0-9]|Os|Og|Oz|Ofast|Odefault)$")
_COMP_RE = re.compile(r"-((?:gcc|clang)[0-9]+(?:_[0-9]+)?)$")


def parse_variant_path(
    drv_name: str, *, archs: tuple[str, ...] = DEFAULT_ARCHS
) -> tuple[str, str, str, str]:
    """``<bin>-<arch>-<comp>-<opt>-...-elf-folder.drv`` → ``(binary,
    arch, comp, opt)``. ``drv_name`` is the post-hash store-path name
    as produced by ``_parse_line``. Raises on shape mismatch.
    """
    fn = drv_name
    m = _VARIANT_RE.match(fn)
    if m is None:
        raise TreeWalkError(f"unrecognised variant filename: {fn}")
    rest = m.group("rest")
    mo = _OPT_RE.search(rest)
    if mo is None:
        raise TreeWalkError(f"no opt suffix in variant rest: {rest!r}")
    opt = mo.group(1)
    rest = rest[: mo.start()]
    mc = _COMP_RE.search(rest)
    if mc is None:
        raise TreeWalkError(f"no compiler suffix in variant rest: {rest!r}")
    comp = mc.group(1)
    rest = rest[: mc.start()]
    # Match arch by suffix (longest-first).
    for arch in sorted(archs, key=lambda a: -len(a)):
        suffix = "-" + arch
        if rest.endswith(suffix):
            binary = rest[: -len(suffix)]
            if not binary:
                raise TreeWalkError(f"empty binary after arch strip: {fn}")
            return binary, arch, comp, opt
    raise TreeWalkError(
        f"no known arch matches variant rest {rest!r}; "
        f"tried {sorted(archs)}"
    )


def drv_tree_stream(stream) -> Iterator[tuple[int, bytes, str, bool]]:
    """Iterate parsed tuples from a byte stream of
    ``nix-store --query --tree`` output. Stops at producer EOF.
    Strips trailing ``\n`` cheaply.
    """
    _parse = parse_line_bytes
    for raw in stream:
        if raw[-1] == 0x0a:
            raw = raw[:-1]
        yield _parse(raw)

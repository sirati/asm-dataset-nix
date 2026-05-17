"""Fast extraction of (toolchain_drvs, arch_indep_deps, variants_per_matrix)
from ``nix-store --query --tree <sum-root>`` output.

Pre-condition (asserted): the toolchains wrapper is the first direct
child of the sum-root, and every matrix wrapper references the
toolchains wrapper too. That refcount boost (root + N matrices) makes
nix-store sort toolchains to the top of the printed tree. The
algorithm collapses if toolchains shows up after any matrix — we abort
with a structured error rather than silently miscategorise.

Walk strategy:
- Within the **toolchain** subtree (depth ≥ 1): every non-backref store
  path goes into ``toolchain_drvs``. Sources (``.sh``, ``.bash``,
  patches, ...) included — they're toolchain build inputs.
- Within a **matrix** subtree, only depth=2 (immediate children of
  ``matrix-<binary>.drv``) is inspected:
  - ``*-elf-folder.drv`` → variant entry point (handed to the
    template-graph cowalker for per-arch DAG dedup).
  - Any other non-backref drv → arch-independent shared dep for that
    binary (e.g. a source tarball used by every variant).
  - Backrefs (``[...]``) are skipped; their content lives in the
    toolchain set or in an earlier matrix's arch-indep set.
- Depth > 2 inside a matrix is ignored — the cowalk re-streams that
  via ``nix derivation show`` once per variant template.

Inputs/outputs are pure data; this module does not invoke nix.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

BACKREF_SUFFIX = " [...]"
# Each indentation level is a 4-character segment: '│   ', '    ',
# '├───', '└───'. Codepoint counts match Python string indices because
# the box-drawing chars are single BMP codepoints.
_SEG_LEN = 4
_INDENT_SEGS = ("│   ", "    ")
_CONNECTOR_SEGS = ("├───", "└───")

# Variant filename suffix from `lib/mkVariant.nix`. Stable enough to be
# the matrix-level entry-point marker.
_VARIANT_SUFFIX = "-elf-folder.drv"
# Regexes match the post-hash drv_name (no hash prefix anymore).
_MATRIX_RE = re.compile(r"^matrix-(?P<binary>[a-zA-Z0-9_+\-.]+)\.drv$")
_TOOLCHAINS_RE = re.compile(r"^toolchains\.drv$")


@dataclass
class TreeWalkResult:
    toolchain_drvs: set[tuple[str, str]] = field(default_factory=set)
    arch_indep_deps: set[tuple[str, str]] = field(default_factory=set)
    variants_per_matrix: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )

    def summary(self) -> str:
        n_var = sum(len(v) for v in self.variants_per_matrix.values())
        return (
            f"toolchain_drvs={len(self.toolchain_drvs)} "
            f"arch_indep_deps={len(self.arch_indep_deps)} "
            f"variants={n_var} across {len(self.variants_per_matrix)} binaries"
        )


class TreeWalkError(RuntimeError):
    """Raised when the input tree violates an ordering / shape assumption."""


_FIX_TOOLCHAIN_ORDER = """\
This walker depends on `nix-store --query --tree` listing the toolchains
wrapper before any matrix wrapper. The tree printer sorts each node's
children by reference count (descending), so toolchains must be
referenced MORE TIMES than any single matrix wrapper.

The intended shape is set by template_graph/sum_drv.nix's mkWrapper:
each matrix wrapper lists the toolchains wrapper among its refs, e.g.

    matrixDrvs = map (m: mkWrapper m.name ([ toolchainsDrv ] ++ m.drvs))
                     matrices;

That gives toolchains refcount = 1 (root) + N (one per matrix), while
each matrix has refcount 1 (root). Toolchains wins the sort and
opens first.

If you see this error:
  - You rebuilt make_sum_drv.py output but matrix wrappers came from a
    PRIOR generation that predates the toolchains-in-every-matrix
    change → re-run make_sum_drv on the same input set so the matrix
    .drvs themselves embed the toolchains ref.
  - You stitched a sum-root via builtins.appendContext directly from
    on-disk matrix .drvs that lack the toolchains ref → rebuild each
    matrix wrapper too, using appendContext to inject the toolchains
    drv-output context into the matrix's inputs.
  - You produced matrix wrappers in some other shape → either replicate
    the toolchains-in-every-matrix structure, or do not use this
    walker; fall back to template_graph.core.plan_phase1_graph with an
    explicit toolchain_drvs set from your own enumeration.
"""


def _ordering_violation_msg(*, kind: str, line: int, offender: str) -> str:
    if kind == "late-toolchain":
        return (
            f"toolchains.drv must precede every matrix-*.drv in the tree, "
            f"but at line {line} we encountered {offender} after a matrix "
            f"wrapper had already opened.\n\n" + _FIX_TOOLCHAIN_ORDER
        )
    if kind == "early-matrix":
        return (
            f"{offender} appears at line {line} before any toolchains.drv. "
            f"The toolchain subtree must be the first direct child of the "
            f"sum-root.\n\n" + _FIX_TOOLCHAIN_ORDER
        )
    return f"tree ordering violation ({kind}) at line {line}: {offender}"


_STORE_PREFIX = "/nix/store/"
_STORE_PREFIX_LEN = len(_STORE_PREFIX)
_HASH_LEN = 32  # nix base32 hash is always 32 chars + dash


def _parse_line(line: str) -> tuple[int, str, str, bool]:
    """Return ``(depth, drv_hash, drv_name, is_backref)``. Root line → depth 0.

    ``drv_hash`` is the 32-char store-path hash; ``drv_name`` is the
    post-hash basename. The ``/nix/store/`` prefix is implied. For
    lines that don't start with the store prefix (only the root in
    practice), ``drv_hash=""`` and ``drv_name`` carries the raw rest.
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
        drv_hash = rest[body_start : body_start + _HASH_LEN]
        drv_name = rest[body_start + _HASH_LEN + 1 :]
    else:
        drv_hash = ""
        drv_name = rest
    return depth, drv_hash, drv_name, is_backref


def walk(tree_text: str) -> TreeWalkResult:
    """Single-pass walk of the full tree output."""
    result = TreeWalkResult()
    lines = tree_text.splitlines()
    if not lines:
        raise TreeWalkError("empty tree output")

    # section ∈ {None, "toolchain", "matrix:<binary>", "other"}
    section: Optional[str] = None
    saw_toolchain = False
    saw_matrix = False

    for idx, line in enumerate(lines):
        if idx == 0:
            # root line — skipped, no depth/indent
            continue
        depth, drv_hash, drv_name, is_backref = _parse_line(line)
        ident = (drv_hash, drv_name)

        if depth == 1:
            # New direct child of the sum-root — pick the section.
            if _TOOLCHAINS_RE.match(drv_name):
                if saw_matrix:
                    raise TreeWalkError(
                        _ordering_violation_msg(
                            kind="late-toolchain",
                            line=idx,
                            offender=drv_name,
                        )
                    )
                section = "toolchain"
                saw_toolchain = True
                if not is_backref:
                    result.toolchain_drvs.add(ident)
                continue
            m = _MATRIX_RE.match(drv_name)
            if m is not None:
                if not saw_toolchain:
                    raise TreeWalkError(
                        _ordering_violation_msg(
                            kind="early-matrix",
                            line=idx,
                            offender=drv_name,
                        )
                    )
                binary = m.group("binary")
                section = f"matrix:{binary}"
                saw_matrix = True
                result.variants_per_matrix.setdefault(binary, [])
                continue
            section = "other"
            continue

        # depth > 1 — process per current section.
        if section == "toolchain":
            if not is_backref:
                result.toolchain_drvs.add(ident)
        elif section is not None and section.startswith("matrix:"):
            if depth != 2:
                continue  # cowalk handles the variant interiors
            binary = section[len("matrix:") :]
            if drv_name.endswith(_VARIANT_SUFFIX):
                if not is_backref:
                    result.variants_per_matrix[binary].append(ident)
                else:
                    raise TreeWalkError(
                        f"variant {drv_name} at line {idx} is a backref; "
                        f"a variant should occur exactly once in the tree"
                    )
            else:
                if not is_backref:
                    result.arch_indep_deps.add(ident)
        # "other" section → ignore

    if not saw_toolchain:
        raise TreeWalkError("no toolchains.drv subtree encountered")
    return result


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
    r"^(?P<rest>.+?)"
    r"-baseline-default-san-off-march-default-elf-folder\.drv$"
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


# ─── Orchestrator: tree → plan_phase1_graph input ───────────────────

def plan_from_tree(
    tree_text: str,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
    get_record=None,
    logger=None,
    binary_filter=None,  # optional: callable(binary_name) -> bool
):
    """End-to-end glue: parse the tree, bucket variants, hand into
    ``template_graph.core.plan_phase1_graph`` with toolchain set and
    arch-indep deps wired in as terminals.

    ``binary_filter`` lets you run the algorithm on a subset (e.g. just
    ``hello`` and ``busybox``) — the full 81K-variant pass will take
    many hours of ``nix derivation show`` traffic.
    """
    from template_graph.core import plan_phase1_graph
    from template_graph.drv_io import read_drv_record

    if get_record is None:
        get_record = read_drv_record

    walked = walk(tree_text)
    buckets = bucket_variants(walked, archs=archs)

    if binary_filter is not None:
        buckets = {
            k: v for k, v in buckets.items() if binary_filter(k[0])
        }

    # Toolchain set from the tree is exhaustive — pass straight in.
    # Arch-indep per-matrix shared deps become non_recursing terminals
    # so the cowalk doesn't descend into them (same role as toolchain
    # nodes from the algorithm's perspective: known terminals that
    # don't need template-shape comparison).
    non_recursing = set(walked.arch_indep_deps)
    if logger is not None:
        n_v = sum(len(v) for v in buckets.values())
        logger(
            f"plan_from_tree: {n_v} variants in {len(buckets)} (binary, arch) "
            f"buckets, toolchain_drvs={len(walked.toolchain_drvs)}, "
            f"non_recursing={len(non_recursing)}"
        )
    kwargs = {
        "variants_by_binary_arch": buckets,
        "toolchain_drvs": walked.toolchain_drvs,
        "get_record": get_record,
        "non_recursing_drvs": non_recursing,
    }
    if logger is not None:
        kwargs["logger"] = logger
    return plan_phase1_graph(**kwargs)


def bucket_variants(
    walked: TreeWalkResult,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
) -> dict[tuple[str, str], list[tuple[str, tuple[str, str]]]]:
    """Reshape walker output into ``plan_phase1_graph``'s expected
    ``{(binary, arch): [(label, (hash, name)), ...]}`` dict. Label is
    ``f"{comp}-{opt}"`` — stable within a (binary, arch) bucket and
    descriptive enough for cowalk error messages.
    """
    out: dict[tuple[str, str], list[tuple[str, tuple[str, str]]]] = {}
    for binary, idents in walked.variants_per_matrix.items():
        for ident in idents:
            _, drv_name = ident
            b, arch, comp, opt = parse_variant_path(drv_name, archs=archs)
            if b != binary:
                raise TreeWalkError(
                    f"variant {ident!r} parsed as binary={b} but "
                    f"tree-walked under matrix-{binary}; tree shape "
                    f"inconsistent"
                )
            label = f"{comp}-{opt}"
            out.setdefault((binary, arch), []).append((label, ident))
    return out

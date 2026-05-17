"""Single-pass streaming planner over `nix-store --query --tree` output.

Replaces the two-phase (walk → plan_phase1_graph) approach: we never
call ``nix derivation show`` because every drv's immediate inputs are
already present in the tree (as direct children at depth+1). The
algorithm walks the output line-by-line, opens a per-variant RawTree
buffer when it enters a matrix variant subtree, and as soon as ANY
arch accumulates two raw trees we build the template from that
calibration pair, drain that arch's buffer into the new variant array,
and continue streaming. Subsequent variants of the same arch
stream directly into the array (no further buffering).

Reused from ``template_graph.core``:
    Template, TemplateNode, VariantArray,
    find_or_register_template,
    TemplateGraphAssertError.

Public surface:
    StreamPlanner — instantiate, feed lines via .feed_line(), call .finalize().
    plan_from_tree_streaming(tree_text, *, archs=...) -> dict
        Same return shape as ``core.plan_phase1_graph``:
            { templates, variant_arrays, placement,
              common_deps_per_arch_template,
              toolchain_drvs, arch_indep_deps }
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from template_graph.core import (
    Template,
    TemplateNode,
    VariantArray,
    TemplateGraphAssertError,
    find_or_register_template,
)
from template_graph.tree_walker import (
    DEFAULT_ARCHS,
    TreeWalkError,
    _MATRIX_RE,
    _TOOLCHAINS_RE,
    _VARIANT_SUFFIX,
    parse_variant_path,
    _parse_line,
)


def drv_name_full(name: str) -> str:
    """Identity passthrough for the post-hash drv name. Kept as a
    function so callers can switch in another extractor."""
    return name


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


_TRIPLE_RE = re.compile(
    rf"(?:^|-)(?:{_TRIPLE_ARCH})-(?:{_TRIPLE_VENDOR})"
    rf"-(?:{_TRIPLE_OS})-(?:{_TRIPLE_ABI})"
    rf"|(?:^|_)(?:{_TRIPLE_ARCH})_(?:{_TRIPLE_VENDOR})"
    rf"_(?:{_TRIPLE_OS})_(?:{_TRIPLE_ABI})"
)

# Map our short arch keys (matching lib/architectures.nix) to the
# canonical Nix target triple. x86_64 is native — no triple shows up
# in drv names.
_ARCH_TO_TRIPLE: dict[str, Optional[str]] = {
    "x86_64":    None,
    "i686":      "i686-unknown-linux-gnu",
    "aarch64":   "aarch64-unknown-linux-gnu",
    "armv7l-hf": "armv7l-unknown-linux-gnueabihf",
    "armv7l-sf": "armv7l-unknown-linux-gnueabi",
    "mipsel":    "mipsel-unknown-linux-gnu",
    "mips64el":  "mips64el-unknown-linux-gnuabin32",
    "ppc32":     "powerpc-unknown-linux-gnu",
    "ppc64":     "powerpc64-unknown-linux-gnuabielfv2",
    "riscv64":   "riscv64-unknown-linux-gnu",
}


def _extract_triple(name: str) -> Optional[str]:
    """Return the embedded target triple (no leading/trailing - or _)
    or None if the name has none."""
    base = name[:-4] if name.endswith(".drv") else name
    m = _TRIPLE_RE.search(base)
    return m.group(0).strip("-_") if m else None


def _extract_version(name: str) -> str:
    """Triple-strip the name, then return the first -<digits>[.<digits>...]
    sequence, or '' if none."""
    base = name[:-4] if name.endswith(".drv") else name
    no_triple = _TRIPLE_RE.sub("", base)
    m = re.search(r"-(\d[\d.]*[a-z]?\d*)", no_triple)
    return m.group(1) if m else ""


def _classify_revisit_diff(
    stored: tuple[str, str],
    observed: tuple[str, str],
) -> Optional[tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]]]:
    """Decide whether two distinct (hash, name) values at the same DAG
    position differ purely by target-triple or purely by version. Returns
    (diff_kind, stored_enforce, observed_enforce) or None for any other
    kind of difference (caller should fall through to the violation log).
    """
    sn, on = stored[1], observed[1]
    s_tri = _extract_triple(sn)
    o_tri = _extract_triple(on)
    s_ver = _extract_version(sn)
    o_ver = _extract_version(on)
    if s_tri != o_tri and s_ver == o_ver:
        return ("triple", ("triple", s_tri), ("triple", o_tri))
    if s_tri == o_tri and s_ver != o_ver:
        return ("version", ("version", s_ver), ("version", o_ver))
    return None
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


# ── RawTree: in-memory per-variant subtree ───────────────────────────


@dataclass
class RawTreeNode:
    """A raw-tree drv reference. ``(hash, name)`` is the canonical
    identity — the ``/nix/store/`` prefix is implied and never stored.
    Read ``rn.ident`` for set/dict keys; read ``rn.name`` for
    name-extraction (role / package name).
    """
    hash: str
    name: str
    is_backref: bool
    depth: int
    children: list["RawTreeNode"] = field(default_factory=list)

    @property
    def ident(self) -> tuple[str, str]:
        return (self.hash, self.name)


# ── Streaming planner ────────────────────────────────────────────────


class StreamPlanner:
    def __init__(
        self,
        *,
        archs: tuple[str, ...] = DEFAULT_ARCHS,
        name_extractor=drv_role,
        logger=None,
        lax: bool = False,
    ):
        self.archs = archs
        self.name_extractor = name_extractor
        self.logger = logger or (lambda _msg: None)
        # Survey mode: never raise on shape inconsistencies; collect
        # them in ``violations`` and best-effort proceed.
        self.lax = lax
        self.violations: list[dict] = []

        # ── outputs (same shape as plan_phase1_graph) ──
        self.templates: list[Template] = []
        self.variant_arrays: dict[tuple[int, str], VariantArray] = {}
        self.placement: dict[str, tuple[int, str, int]] = {}
        self.classifications: dict[tuple[int, str], dict[int, str]] = {}
        # All identity-bearing collections use ``(hash, name)`` tuples.
        # The ``/nix/store/`` prefix is implied; never reconstructed.
        self.toolchain_drvs: set[tuple[str, str]] = set()
        # Per-binary arch-indep deps surfaced at matrix depth 2.
        self.arch_indep_deps: dict[str, set[tuple[str, str]]] = {}
        # Stdenv raw subtrees, siphoned during template construction
        # and cowalk. Keyed by the stdenv root ``(hash, name)`` so a
        # stdenv referenced from many variants captures once.
        self.stdenv_subtrees: dict[tuple[str, str], dict] = {}
        # Scratch context passed from _build_and_drain_arch / cowalk
        # callers into the inner _alloc/_visit closures, so stdenv
        # captures know which (matrix, arch, label) produced them.
        self._building_arch: Optional[str] = None
        self._building_label_pair: tuple[str, str] = ("", "")

        # ── tree-walk state ──
        self.section: Optional[str] = None
        self.matrix_binary: Optional[str] = None
        self._saw_toolchain = False
        self._saw_matrix = False

        # ── per-matrix state (reset on matrix transition) ──
        self.pending_raw_trees: dict[str, list[tuple[str, RawTreeNode]]] = {}
        self.arch_template_id: dict[str, int] = {}
        # Drvs encountered (non-backref) in this matrix's variants that
        # haven't been placed into any bucket. Should drain to empty by
        # end-of-matrix; non-empty surfaces a missing edge case.
        self.unclassified_nodes: set[tuple[str, str]] = set()

        # ── currently-buffering variant raw tree (if any) ──
        self._cur_root: Optional[RawTreeNode] = None
        self._cur_arch: Optional[str] = None
        self._cur_label: Optional[str] = None
        self._cur_drv: Optional[str] = None
        self._cur_stack: list[RawTreeNode] = []
        # ``(hash, name)`` → RawTreeNode within the variant currently
        # being built. Collapses re-encountered idents onto the
        # existing node (DAG via nix's [...] back-refs).
        self._cur_path_to_node: dict[tuple[str, str], RawTreeNode] = {}

    # ── lax-mode violation recording ──

    def _record(self, kind: str, **details) -> None:
        entry = {"kind": kind, **details}
        if self.matrix_binary is not None:
            entry.setdefault("matrix", self.matrix_binary)
        self.violations.append(entry)

    # ── public API ──

    def feed_line(self, line: str) -> None:
        depth, drv_hash, drv_name, is_backref = _parse_line(line)
        # Root line — no further work; the calling driver dispatches
        # the line index, we just need to stay consistent.
        if depth == 0:
            return

        # If we were inside a variant raw tree and depth drops back to
        # at or below 2 (the matrix-direct-child level), finalise.
        if self._cur_root is not None and depth <= 2:
            self._finalise_current_variant()

        if depth == 1:
            self._on_depth1(drv_hash, drv_name, is_backref)
            return

        # Below depth 1: process per section.
        if self.section == "toolchain":
            if not is_backref:
                self.toolchain_drvs.add((drv_hash, drv_name))
            return

        if self.section and self.section.startswith("matrix:"):
            self._on_matrix_inner(depth, drv_hash, drv_name, is_backref)
            return

        # "other" section (e.g. bash builder ref) — ignore.

    def finalize(self) -> dict:
        """Drain the last variant and the last matrix's pending buffers."""
        if self._cur_root is not None:
            self._finalise_current_variant()
        self._close_current_matrix()
        return {
            "templates": self.templates,
            "variant_arrays": self.variant_arrays,
            "placement": self.placement,
            "common_deps_per_arch_template": self.classifications,
            "toolchain_drvs": self.toolchain_drvs,
            "arch_indep_deps": self.arch_indep_deps,
            "stdenv_subtrees": self.stdenv_subtrees,
        }

    # ── depth-1 section transitions ──

    def _on_depth1(
        self, drv_hash: str, drv_name: str, is_backref: bool
    ) -> None:
        if _TOOLCHAINS_RE.match(drv_name):
            if self._saw_matrix:
                raise TreeWalkError(
                    f"toolchains.drv appeared after a matrix opened. "
                    f"The streaming planner relies on toolchains being "
                    f"the first depth-1 child (highest refcount). "
                    f"Ensure sum_drv.nix's mkWrapper includes the "
                    f"toolchains wrapper as a ref in every matrix."
                )
            self.section = "toolchain"
            self._saw_toolchain = True
            if not is_backref:
                self.toolchain_drvs.add((drv_hash, drv_name))
            return
        m = _MATRIX_RE.match(drv_name)
        if m is not None:
            if not self._saw_toolchain:
                raise TreeWalkError(
                    f"{drv_name} appeared before any toolchains.drv. "
                    f"Toolchains must sort first under the sum-root. "
                    f"See sum_drv.nix's mkWrapper for the matrix-side "
                    f"toolchains ref that drives the refcount sort."
                )
            self._close_current_matrix()
            self.matrix_binary = m.group("binary")
            self.section = f"matrix:{self.matrix_binary}"
            self._saw_matrix = True
            self.pending_raw_trees = {}
            self.arch_template_id = {}
            self.unclassified_nodes = set()
            self.arch_indep_deps.setdefault(self.matrix_binary, set())
            return
        # Some other depth-1 (bash builder reference, etc.).
        self.section = "other"

    # ── matrix-inner depth handling ──

    def _on_matrix_inner(
        self,
        depth: int,
        drv_hash: str,
        drv_name: str,
        is_backref: bool,
    ) -> None:
        ident = (drv_hash, drv_name)
        if depth == 2:
            if drv_name.endswith(_VARIANT_SUFFIX):
                if is_backref:
                    raise TreeWalkError(
                        f"variant entry-point {drv_name} at matrix depth 2 "
                        f"is a backref; each variant should occur exactly "
                        f"once in the tree"
                    )
                binary, arch, comp, opt = parse_variant_path(
                    drv_name, archs=self.archs
                )
                if binary != self.matrix_binary:
                    raise TreeWalkError(
                        f"variant {drv_name!r} parses as binary={binary!r} "
                        f"but tree-walked under matrix-{self.matrix_binary!r}"
                    )
                root = RawTreeNode(
                    hash=drv_hash, name=drv_name,
                    is_backref=False, depth=2,
                )
                self._cur_root = root
                self._cur_arch = arch
                self._cur_label = f"{comp}-{opt}"
                self._cur_drv = ident
                self._cur_stack = [root]
                self._cur_path_to_node = {ident: root}
            else:
                if not is_backref:
                    self.arch_indep_deps[self.matrix_binary].add(ident)
            return

        # depth > 2: inside the current variant's subtree
        if self._cur_root is None:
            raise TreeWalkError(
                f"depth-{depth} line under matrix-{self.matrix_binary} "
                f"with no active variant subtree (ident={ident!r})"
            )
        while self._cur_stack and self._cur_stack[-1].depth >= depth:
            self._cur_stack.pop()
        if not self._cur_stack:
            raise TreeWalkError(
                f"raw-tree splice failed at depth {depth} for ident={ident!r}"
            )
        parent = self._cur_stack[-1]
        existing = self._cur_path_to_node.get(ident)
        if existing is not None:
            parent.children.append(existing)
            return
        node = RawTreeNode(
            hash=drv_hash, name=drv_name,
            is_backref=is_backref, depth=depth,
        )
        parent.children.append(node)
        self._cur_path_to_node[ident] = node
        if not is_backref:
            self._cur_stack.append(node)
            if ident not in self.toolchain_drvs:
                self.unclassified_nodes.add(ident)

    # ── variant raw-tree completion ──

    def _finalise_current_variant(self) -> None:
        root = self._cur_root
        arch = self._cur_arch
        label = self._cur_label
        assert root is not None and arch is not None
        # Reset buffer pointers BEFORE doing the heavy work so that
        # any recursive calls (shouldn't happen, but defence) don't
        # confuse state.
        self._cur_root = None
        self._cur_arch = None
        self._cur_label = None
        self._cur_drv = None
        self._cur_stack = []
        self._cur_path_to_node = {}
        if arch in self.arch_template_id:
            # Template exists — stream-cowalk this variant immediately.
            tmpl_id = self.arch_template_id[arch]
            self._cowalk_into_arr(tmpl_id, arch, label, root)
        else:
            self.pending_raw_trees.setdefault(arch, []).append((label, root))
            if len(self.pending_raw_trees[arch]) == 2:
                self._build_and_drain_arch(arch)

    # ── template construction from calibration pair ──

    def _build_and_drain_arch(self, arch: str) -> None:
        pair = self.pending_raw_trees[arch]
        assert len(pair) == 2, (
            f"calibration pair must have exactly 2 raw trees for {arch}; "
            f"got {len(pair)}"
        )
        (label0, tree0), (label1, tree1) = pair
        self._building_arch = arch
        self._building_label_pair = (label0, label1)
        candidate = self._build_template(tree0, tree1, label0, label1)
        tmpl_id, _was_new = find_or_register_template(
            self.templates, candidate
        )
        self.arch_template_id[arch] = tmpl_id
        template = self.templates[tmpl_id]
        arr = VariantArray(
            template_id=tmpl_id,
            arch=arch,
            variants=[],
            hashes=[[] for _ in template.nodes],
        )
        self.variant_arrays[(tmpl_id, arch)] = arr
        # Drain the calibration pair into the new VariantArray (these
        # are the only entries for THIS arch; other archs keep their
        # singletons buffered).
        self._cowalk_into_arr(tmpl_id, arch, label0, tree0, _arr=arr)
        self._cowalk_into_arr(tmpl_id, arch, label1, tree1, _arr=arr)
        self.pending_raw_trees[arch] = []
        # Classify on the calibration pair (variants 0 and 1).
        self.classifications[(tmpl_id, arch)] = self._classify_pair(
            arr, template
        )

    def _build_template(
        self,
        tree0: RawTreeNode,
        tree1: RawTreeNode,
        label0: str,
        label1: str,
    ) -> Template:
        """Parallel-walk two raw trees. Identity during construction is
        the pair ``(t0.drv_path, t1.drv_path)``: re-encountering the
        same pair (DAG join in both raw trees at once) links back to
        the existing template node. Distinct hash-paths — even with
        identical post-hash names — get distinct template nodes.
        """
        template = Template(
            nodes=[],
            name_to_id={},
            root_id=0,
            template_built_from=[label0, label1],
        )
        # pair_to_id key shapes:
        #  - pair walk:       ((h0, n0), (h1, n1))
        #  - one-sided t0:    ((h, n), None)
        #  - one-sided t1:    (None, (h, n))
        pair_to_id: dict = {}

        def _alloc(t0: RawTreeNode, t1: RawTreeNode) -> tuple[int, bool]:
            key = (t0.ident, t1.ident)
            if key in pair_to_id:
                return pair_to_id[key], False
            tnode = self._make_template_node(
                [t0, t1],
                optional=False,
                label_slots=list(self._building_label_pair),
            )
            nid = len(template.nodes)
            template.nodes.append(tnode)
            pair_to_id[key] = nid
            return nid, True

        def _walk_single(rn: RawTreeNode, is_t0_side: bool) -> int:
            """Walk a raw subtree present on only one calibration side.
            Every template node it allocates is optional. Identity is
            keyed ``(ident, None)`` or ``(None, ident)`` so it never
            collides with pair-walked nodes."""
            slot = self._building_label_pair[0 if is_t0_side else 1]

            def _alloc(r: RawTreeNode) -> tuple[int, bool]:
                key = (r.ident, None) if is_t0_side else (None, r.ident)
                if key in pair_to_id:
                    return pair_to_id[key], False
                tnode = self._make_template_node(
                    [r], optional=True, label_slots=[slot],
                )
                nid_ = len(template.nodes)
                template.nodes.append(tnode)
                pair_to_id[key] = nid_
                return nid_, True

            return self._walk_one_sided_subtree(rn, template, _alloc)

        def _walk(t0: RawTreeNode, t1: RawTreeNode) -> int:
            nid, fresh = _alloc(t0, t1)
            if not t0.is_backref:
                self.unclassified_nodes.discard(t0.ident)
            if not t1.is_backref:
                self.unclassified_nodes.discard(t1.ident)
            if not fresh:
                return nid
            node = template.nodes[nid]
            if node.is_toolchain:
                self._discard_subtree(t0)
                self._discard_subtree(t1)
                return nid
            if t0.ident == t1.ident:
                ref = t0 if t0.children else t1
                for c in ref.children:
                    node.child_ids.append(_walk(c, c))
                return nid
            t0_by_name: dict[str, list[RawTreeNode]] = {}
            for c in t0.children:
                t0_by_name.setdefault(
                    self.name_extractor(c.name), []
                ).append(c)
            t1_by_name: dict[str, list[RawTreeNode]] = {}
            for c in t1.children:
                t1_by_name.setdefault(
                    self.name_extractor(c.name), []
                ).append(c)
            common = set(t0_by_name) & set(t1_by_name)
            only_t0 = set(t0_by_name) - common
            only_t1 = set(t1_by_name) - common
            # One-sided children become optional subtrees attached to
            # the parent. They cease to be a "violation" — they are an
            # *expected* phenomenon (stdenv splices setup-hooks based
            # on compiler choice, etc.).
            for name in sorted(only_t0):
                for c0 in t0_by_name[name]:
                    node.child_ids.append(_walk_single(c0, is_t0_side=True))
            for name in sorted(only_t1):
                for c1 in t1_by_name[name]:
                    node.child_ids.append(_walk_single(c1, is_t0_side=False))
            for name in sorted(common):
                t0_lst = sorted(t0_by_name[name], key=lambda r: r.ident)
                t1_lst = sorted(t1_by_name[name], key=lambda r: r.ident)
                if len(t0_lst) != len(t1_lst):
                    if not self.lax:
                        raise TreeWalkError(
                            f"calibration pair same-name child count "
                            f"mismatch at node {node.name!r}, name "
                            f"{name!r}: t0={len(t0_lst)} vs t1={len(t1_lst)}"
                        )
                    self._record(
                        "calibration-same-name-count-mismatch",
                        node_role=node.name,
                        child_name=name,
                        t0_count=len(t0_lst),
                        t1_count=len(t1_lst),
                        labels=list(template.template_built_from),
                    )
                n = min(len(t0_lst), len(t1_lst))
                for c0, c1 in zip(t0_lst[:n], t1_lst[:n]):
                    node.child_ids.append(_walk(c0, c1))
            return nid

        _walk(tree0, tree1)
        return template

    # ── cowalk one raw tree into a VariantArray ──

    def _cowalk_into_arr(
        self,
        tmpl_id: int,
        arch: str,
        label: str,
        tree: RawTreeNode,
        *,
        _arr: Optional[VariantArray] = None,
    ) -> None:
        template = self.templates[tmpl_id]
        arr = _arr if _arr is not None else self.variant_arrays[(tmpl_id, arch)]
        v_pos = len(arr.variants)
        arr.variants.append(label)
        for row in arr.hashes:
            row.append(None)
        for n in template.nodes:
            n.visit_flag = False
        self.placement[tree.ident] = (tmpl_id, arch, v_pos)

        def _resolve_enforce(
            enf: tuple[str, Optional[str]],
        ) -> tuple[str, Optional[str]]:
            if enf[0] == "triple":
                target = _ARCH_TO_TRIPLE.get(arch)
                if enf[1] is not None and enf[1] == target:
                    return ("this-target", None)
            return enf

        def _split_dag_revisit(
            parent_nid: int,
            cid: int,
            observed_ident: tuple[str, str],
            diff: tuple[str, tuple[str, Optional[str]], tuple[str, Optional[str]]],
        ) -> int:
            orig = template.nodes[cid]
            _diff_kind, stored_enf, observed_enf = diff
            if orig.enforce is None:
                orig.enforce = _resolve_enforce(stored_enf)
            new_node = TemplateNode(
                name=orig.name,
                child_ids=list(orig.child_ids),
                is_toolchain=orig.is_toolchain,
                visit_flag=True,
                optional=orig.optional,
                enforce=_resolve_enforce(observed_enf),
            )
            new_nid = len(template.nodes)
            template.nodes.append(new_node)
            # All variant_arrays of this template gain a row. Copy the
            # original's row so previously-cowalked variants (which
            # agreed on both DAG paths) keep their value for the new
            # node too — only divergent variants will overwrite later.
            for (tid_x, _ax), other_arr in self.variant_arrays.items():
                if tid_x == tmpl_id:
                    other_arr.hashes.append(list(other_arr.hashes[cid]))
            arr.hashes[new_nid][v_pos] = observed_ident
            parent = template.nodes[parent_nid]
            idx = parent.child_ids.index(cid)
            parent.child_ids[idx] = new_nid
            return new_nid

        def _visit_children(self_nid: int, t_node: RawTreeNode) -> None:
            node = template.nodes[self_nid]
            template_by_name: dict[str, list[int]] = {}
            for cid in node.child_ids:
                template_by_name.setdefault(
                    template.nodes[cid].name, []
                ).append(cid)
            actual_by_name: dict[str, list[RawTreeNode]] = {}
            for c in t_node.children:
                actual_by_name.setdefault(
                    self.name_extractor(c.name), []
                ).append(c)
            common = set(template_by_name) & set(actual_by_name)
            only_tmpl = set(template_by_name) - common
            only_actual = set(actual_by_name) - common
            promoted: list[str] = []
            for name in sorted(only_tmpl):
                for cid in template_by_name[name]:
                    if not template.nodes[cid].optional:
                        template.nodes[cid].optional = True
                        promoted.append(name)
            if promoted:
                self._record(
                    "cowalk-promoted-to-optional",
                    node_role=node.name,
                    label=label,
                    promoted_children=promoted,
                )
            if only_actual:
                extended: list[str] = []
                for name in sorted(only_actual):
                    for c in actual_by_name[name]:
                        self._extend_template_with_subtree(
                            tmpl_id, arch, v_pos, self_nid, c,
                        )
                    extended.append(name)
                self._record(
                    "cowalk-template-extended",
                    node_role=node.name,
                    label=label,
                    new_children=extended,
                )
            for name in sorted(common):
                cids = template_by_name[name]
                actuals = sorted(
                    actual_by_name[name], key=lambda r: r.ident
                )
                if len(actuals) != len(cids):
                    if not self.lax:
                        raise TemplateGraphAssertError(
                            kind="child-count-mismatch",
                            message=(
                                f"variant {label!r} at node #{self_nid} "
                                f"({node.name!r}): name {name!r} "
                                f"template={len(cids)} actual="
                                f"{len(actuals)}"
                            ),
                            template_built_from=template.template_built_from,
                            failing_variant=label,
                            node_name=node.name,
                        )
                    self._record(
                        "cowalk-child-count-mismatch",
                        node_role=node.name,
                        label=label,
                        child_name=name,
                        template_count=len(cids),
                        actual_count=len(actuals),
                    )
                n = min(len(cids), len(actuals))
                for cid, c in zip(cids[:n], actuals[:n]):
                    _visit(cid, c, self_nid)

        def _visit(
            nid: int,
            t_node: RawTreeNode,
            parent_nid: Optional[int],
        ) -> None:
            node = template.nodes[nid]
            if not t_node.is_backref:
                self.unclassified_nodes.discard(t_node.ident)
            if node.visit_flag:
                if not node.is_toolchain:
                    stored = arr.hashes[nid][v_pos]
                    if stored is not None and stored != t_node.ident:
                        diff = _classify_revisit_diff(stored, t_node.ident)
                        if diff is not None and parent_nid is not None:
                            new_nid = _split_dag_revisit(
                                parent_nid, nid, t_node.ident, diff,
                            )
                            self._record(
                                "cowalk-dag-revisit-split",
                                node_role=node.name,
                                label=label,
                                diff_kind=diff[0],
                                stored=stored,
                                observed=t_node.ident,
                            )
                            if not t_node.is_backref:
                                _visit_children(new_nid, t_node)
                            return
                        if not self.lax:
                            raise TemplateGraphAssertError(
                                kind="dag-revisit-hash-mismatch",
                                message=(
                                    f"DAG revisit at node #{nid} "
                                    f"({node.name!r}) observed drv differs"
                                ),
                                template_built_from=template.template_built_from,
                                failing_variant=label,
                                node_name=node.name,
                                details={
                                    "stored": stored,
                                    "observed": t_node.ident,
                                },
                            )
                        self._record(
                            "cowalk-dag-revisit-hash-mismatch",
                            node_role=node.name,
                            label=label,
                            stored=stored,
                            observed=t_node.ident,
                        )
                return
            node.visit_flag = True
            if node.is_toolchain:
                if _is_stdenv_role(node.name):
                    self.stdenv_subtrees.setdefault(t_node.ident, {
                        "first_seen_in": {
                            "matrix": self.matrix_binary,
                            "arch": arch,
                            "label": label,
                        },
                        "root": t_node,
                    })
                self._discard_subtree(t_node)
                return
            arr.hashes[nid][v_pos] = t_node.ident
            if t_node.is_backref:
                return
            _visit_children(nid, t_node)

        # Root pairing is by template.root_id ↔ raw tree root —
        # don't name-match here, because the variant entry-point's
        # extracted name embeds comp+opt and so differs across
        # variants of the same (binary, arch).
        _visit(template.root_id, tree, parent_nid=None)

    # ── shared template-node construction ──

    def _make_template_node(
        self,
        raw_nodes: list[RawTreeNode],
        *,
        optional: bool,
        label_slots: list[str],
        arch: Optional[str] = None,
    ) -> TemplateNode:
        """Build a TemplateNode from one or two raw nodes. Handles
        stdenv capture (siphoning the subtree into ``stdenv_subtrees``)
        and toolchain classification (stdenv role, compiler-wrapper
        role, or drv-path membership in ``toolchain_drvs``).
        """
        name = self.name_extractor(raw_nodes[0].name)
        is_stdenv = _is_stdenv_role(name)
        eff_arch = arch if arch is not None else self._building_arch
        if is_stdenv:
            seen: set[tuple[str, str]] = set()
            for rn, slot in zip(raw_nodes, label_slots):
                if rn.ident in seen:
                    continue
                seen.add(rn.ident)
                self.stdenv_subtrees.setdefault(rn.ident, {
                    "first_seen_in": {
                        "matrix": self.matrix_binary,
                        "arch": eff_arch,
                        "label": slot,
                    },
                    "root": rn,
                })
        is_toolchain = (
            is_stdenv
            or _is_compiler_wrapper_role(name)
            or any(rn.ident in self.toolchain_drvs for rn in raw_nodes)
        )
        return TemplateNode(
            name=name,
            child_ids=[],
            is_toolchain=is_toolchain,
            visit_flag=False,
            optional=optional,
        )

    def _walk_one_sided_subtree(
        self,
        rn: RawTreeNode,
        template: Template,
        alloc_fn,  # (RawTreeNode) -> (nid, fresh)
        post_fresh=None,  # optional (nid, rn) -> None for hash-record etc
    ) -> int:
        """Walk a raw subtree from a single calibration side / variant,
        recursing through children. ``alloc_fn`` returns ``(nid,
        fresh)`` and is responsible for any dedup. ``post_fresh`` runs
        after fresh allocation for callers that need to record hashes.
        """
        nid, fresh = alloc_fn(rn)
        if not rn.is_backref:
            self.unclassified_nodes.discard(rn.ident)
        if not fresh:
            return nid
        node = template.nodes[nid]
        if post_fresh is not None:
            post_fresh(nid, rn)
        if node.is_toolchain:
            self._discard_subtree(rn)
            return nid
        for c in rn.children:
            cid = self._walk_one_sided_subtree(
                c, template, alloc_fn, post_fresh
            )
            if cid not in node.child_ids:
                node.child_ids.append(cid)
        return nid

    # ── template extension during cowalk ──

    def _extend_template_with_subtree(
        self,
        tmpl_id: int,
        arch: str,
        v_pos: int,
        parent_nid: int,
        raw_root: RawTreeNode,
    ) -> int:
        """Walk a variant's raw subtree the calibration pair didn't
        cover. Allocates optional template nodes (DAG-merging within
        this single walk), grows every ``VariantArray`` of this
        template, records the variant's hashes, then attaches the new
        subtree's root to ``parent_nid``'s ``child_ids``.
        """
        template = self.templates[tmpl_id]
        arr = self.variant_arrays[(tmpl_id, arch)]
        label = arr.variants[v_pos]
        local_path_to_nid: dict[tuple[str, str], int] = {}

        def _alloc(rn: RawTreeNode) -> tuple[int, bool]:
            if rn.ident in local_path_to_nid:
                return local_path_to_nid[rn.ident], False
            tnode = self._make_template_node(
                [rn], optional=True, label_slots=[label], arch=arch,
            )
            nid = len(template.nodes)
            template.nodes.append(tnode)
            local_path_to_nid[rn.ident] = nid
            # Every variant_array for this template gets a None row.
            for (tid_x, _ax), other_arr in self.variant_arrays.items():
                if tid_x == tmpl_id:
                    other_arr.hashes.append(
                        [None] * len(other_arr.variants)
                    )
            return nid, True

        def _record(nid: int, rn: RawTreeNode) -> None:
            if not template.nodes[nid].is_toolchain:
                arr.hashes[nid][v_pos] = rn.ident

        new_nid = self._walk_one_sided_subtree(
            raw_root, template, _alloc, post_fresh=_record
        )
        if new_nid not in template.nodes[parent_nid].child_ids:
            template.nodes[parent_nid].child_ids.append(new_nid)
        return new_nid

    # ── toolchain-child detection (for parent-level filtering) ──

    def _is_toolchain_child(self, raw_node: RawTreeNode) -> bool:
        """Classify a raw-tree child as toolchain *without* allocating a
        template node. Mirrors the is_toolchain logic at _alloc time."""
        name = self.name_extractor(raw_node.name)
        return (
            _is_stdenv_role(name)
            or _is_compiler_wrapper_role(name)
            or raw_node.ident in self.toolchain_drvs
        )

    # ── discard a raw subtree from unclassified ──

    def _discard_subtree(self, t_node: RawTreeNode) -> None:
        """When we determine a raw-tree node is toolchain-internal,
        every drv reachable from it is also toolchain-internal —
        anything not pre-listed in toolchain_drvs is just incidental
        build noise. Discard the whole subtree from unclassified."""
        stack = [t_node]
        while stack:
            n = stack.pop()
            if not n.is_backref:
                self.unclassified_nodes.discard(n.ident)
            stack.extend(n.children)

    # ── classification on calibration pair ──

    def _classify_pair(
        self, arr: VariantArray, template: Template
    ) -> dict[int, str]:
        """At this point arr.hashes has exactly two columns (variants
        0 and 1). For each non-toolchain node: equal → common_dep,
        differing → variant_specific.

        Subsequent variants of this arch will be checked incrementally
        by ``_cowalk_into_arr`` via ``assert_classification_after_cowalk``
        — but for now the simple end-of-arch check in core's
        ``assert_arch_invariants`` will do once a final pass is needed.
        """
        out: dict[int, str] = {}
        for nid, node in enumerate(template.nodes):
            if node.is_toolchain:
                continue
            h0 = arr.hashes[nid][0]
            h1 = arr.hashes[nid][1] if len(arr.hashes[nid]) > 1 else None
            if h0 is None:
                # Variant 0 didn't reach this node during cowalk. If
                # not already optional, promote it now — same handling
                # the cowalk-time path uses for "required but absent
                # in some variant". Downstream merged render uses
                # whichever non-None hash exists.
                if not node.optional:
                    node.optional = True
                out[nid] = "common_dep"
                continue
            if h1 is None:
                # Single-variant calibration (rare; happens via
                # _close_current_matrix when an arch had only one
                # variant). Mark as common_dep.
                out[nid] = "common_dep"
                continue
            out[nid] = "common_dep" if h0 == h1 else "variant_specific"
        return out

    # ── end-of-matrix cleanup ──

    def _close_current_matrix(self) -> None:
        if self.matrix_binary is None:
            return
        # Singletons: archs that only saw one variant. Build a
        # single-variant template from each.
        for arch, pending in list(self.pending_raw_trees.items()):
            if not pending:
                continue
            if len(pending) >= 2:
                # Pair (and any extras would have been drained at the
                # moment the 2nd arrived). Safety net.
                self._build_and_drain_arch(arch)
            else:
                label, tree = pending[0]
                self._building_arch = arch
                template = self._build_template_singleton(tree, label)
                tmpl_id, _ = find_or_register_template(
                    self.templates, template
                )
                self.arch_template_id[arch] = tmpl_id
                arr = VariantArray(
                    template_id=tmpl_id,
                    arch=arch,
                    variants=[],
                    hashes=[[] for _ in self.templates[tmpl_id].nodes],
                )
                self.variant_arrays[(tmpl_id, arch)] = arr
                self._cowalk_into_arr(tmpl_id, arch, label, tree)
                self.classifications[(tmpl_id, arch)] = self._classify_pair(
                    arr, self.templates[tmpl_id]
                )
                self.pending_raw_trees[arch] = []
        if self.unclassified_nodes:
            sample = sorted(self.unclassified_nodes)[:5]
            if not self.lax:
                raise TreeWalkError(
                    f"matrix-{self.matrix_binary} ended with "
                    f"{len(self.unclassified_nodes)} drvs still in "
                    f"unclassified_nodes — algorithm gap. "
                    f"First 5: {sample}"
                )
            self._record(
                "unclassified-at-matrix-end",
                count=len(self.unclassified_nodes),
                sample=sample,
            )
            self.unclassified_nodes = set()

    def _build_template_singleton(
        self, tree: RawTreeNode, label: str
    ) -> Template:
        """Single-variant arch → mirror its structure, all nodes
        become common_dep candidates (no calibration possible)."""
        template = Template(
            nodes=[], name_to_id={}, root_id=0,
            template_built_from=[label],
        )

        def _alloc(tn: RawTreeNode) -> int:
            name = self.name_extractor(tn.name)
            is_stdenv = _is_stdenv_role(name)
            if is_stdenv:
                self.stdenv_subtrees.setdefault(tn.ident, {
                    "first_seen_in": {
                        "matrix": self.matrix_binary,
                        "arch": self._building_arch,
                        "label": label,
                    },
                    "root": tn,
                })
            is_toolchain = (
                is_stdenv
                or _is_compiler_wrapper_role(name)
                or tn.ident in self.toolchain_drvs
            )
            if name in template.name_to_id:
                return template.name_to_id[name]
            node = TemplateNode(
                name=name,
                child_ids=[],
                is_toolchain=is_toolchain,
                visit_flag=False,
            )
            nid = len(template.nodes)
            template.nodes.append(node)
            template.name_to_id[name] = nid
            return nid

        visited: set[int] = set()

        def _walk(tn: RawTreeNode) -> int:
            nid = _alloc(tn)
            if not tn.is_backref:
                self.unclassified_nodes.discard(tn.ident)
            if nid in visited:
                return nid
            visited.add(nid)
            if template.nodes[nid].is_toolchain:
                self._discard_subtree(tn)
                return nid
            for c in tn.children:
                cid = _walk(c)
                if cid not in template.nodes[nid].child_ids:
                    template.nodes[nid].child_ids.append(cid)
            return nid

        _walk(tree)
        return template


# ── convenience entry point ──


def _enforce_label(enforce: Optional[tuple[str, Optional[str]]]) -> str:
    """One-line render of an enforce tuple for use in DOT labels."""
    if enforce is None:
        return ""
    kind, val = enforce
    if kind == "this-target":
        return " ⟨this-target⟩"
    if kind == "triple":
        return " ⟨native⟩" if val is None else f" ⟨{val}⟩"
    if kind == "version":
        return f" ⟨v{val}⟩"
    return f" ⟨{kind}:{val}⟩"


def template_to_dot(
    template: Template,
    classifications: Optional[dict] = None,
    *,
    label: str = "template",
) -> str:
    """Render a Template as Graphviz DOT.

    Colours: toolchain = lightgray, common_dep = palegreen,
    variant_specific = lightcoral, unclassified = white. Open the
    output with ``xdot``, ``dot -Tsvg``, or any DOT viewer.
    """
    classifications = classifications or {}
    lines: list[str] = []
    safe_label = label.replace('"', '\\"')
    lines.append(f'digraph "{safe_label}" {{')
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=filled, fontname=monospace];")
    for nid, node in enumerate(template.nodes):
        if node.is_toolchain:
            fill = "lightgray"
        else:
            cls = classifications.get(nid)
            fill = {
                "common_dep": "palegreen",
                "variant_specific": "lightcoral",
            }.get(cls, "white")
        safe_name = node.name.replace('"', '\\"')
        style = "filled,dashed" if node.optional else "filled"
        suffix = " *" if node.optional else ""
        suffix += _enforce_label(node.enforce)
        lines.append(
            f'  n{nid} [label="{safe_name}{suffix}", '
            f'fillcolor={fill}, style="{style}"];'
        )
    for nid, node in enumerate(template.nodes):
        for cid in node.child_ids:
            lines.append(f"  n{nid} -> n{cid};")
    lines.append("}")
    return "\n".join(lines)


_ARCH_FAMILIES: dict[str, str] = {
    "x86_64": "x86",
    "i686": "x86",
    "aarch64": "arm",
    "armv7l-hf": "arm",
    "armv7l-sf": "arm",
    "mips64el": "mips",
    "mipsel": "mips",
    "ppc32": "power",
    "ppc64": "power",
    "riscv64": "riscv",
}


def _classify_cross_arch_sharing(
    arch_to_drv: dict[str, str],
) -> str:
    """Returns one of 'A','B','C','D'. See merge_binary_to_dot doc."""
    # Single-arch presence is by definition not "shared across archs",
    # even though the unique-set is trivially of size 1. Treat as A.
    if len(arch_to_drv) <= 1:
        return "A"
    unique = set(arch_to_drv.values())
    if len(unique) == 1:
        return "D"
    if len(unique) == len(arch_to_drv):
        return "A"
    # Each drv → set of archs that have it.
    by_drv: dict[str, frozenset[str]] = {}
    for arch, drv in arch_to_drv.items():
        by_drv.setdefault(drv, set()).add(arch)
    by_drv = {d: frozenset(a) for d, a in by_drv.items()}
    # For each drv's arch-set, is it exactly one whole family
    # (or a single arch from a family with no other family members
    # present)? "Different per family" = every group is a single
    # complete family. Anything more interleaved = "mixed".
    all_families_present: dict[str, set[str]] = {}
    for arch in arch_to_drv:
        fam = _ARCH_FAMILIES.get(arch, "other")
        all_families_present.setdefault(fam, set()).add(arch)
    family_clean = True
    for archs in by_drv.values():
        fams = {_ARCH_FAMILIES.get(a, "other") for a in archs}
        if len(fams) > 1:
            family_clean = False
            break
        # All archs in this group come from one family. Check that
        # ALL archs from that family which are present in our
        # observation set are in this group — otherwise the family
        # is split across drvs (mixed).
        fam = next(iter(fams))
        if archs != all_families_present[fam]:
            family_clean = False
            break
    return "B" if family_clean else "C"


def merge_binary_to_dot(
    result: dict,
    binary: str,
    *,
    label: Optional[str] = None,
    collapse_common_deps: bool = False,
) -> str:
    """Build a per-binary merged DOT across all (template, arch).

    Nodes are keyed by role (the version-and-triple-stripped name
    that's already the template node's ``.name``). For each role we
    look up the drv-path stored in arr.hashes[node][0] of every arch's
    calibration variant and classify the sharing pattern:

      A — different drv per arch
      B — same drv within family, different across families
          (x86: i686+x86_64;
           arm: aarch64+armv7l-hf+armv7l-sf;
           mips: mips64el+mipsel; power: ppc32+ppc64; riscv: riscv64)
      C — partial sharing that crosses family boundaries
      D — same drv across every arch that has the role

    Plus a dashed border if not every arch in the binary's matrix
    surfaces the role.
    """
    by_arch: dict[str, tuple[Template, dict[int, str], VariantArray]] = {}
    for (tid, arch), arr in result["variant_arrays"].items():
        tmpl = result["templates"][tid]
        # Match only on the ROOT node — a template "belongs to" binary
        # X iff its entry-point is X's elf-folder. Otherwise binaries
        # that *depend* on X (e.g. vips dep'ing on libxml2) would be
        # wrongly counted as X templates.
        root = tmpl.nodes[tmpl.root_id]
        if not (
            root.name.startswith(f"{binary}-")
            and "-elf-folder" in root.name
        ):
            continue
        classes = result["common_deps_per_arch_template"].get(
            (tid, arch), {}
        )
        by_arch[arch] = (tmpl, classes, arr)
    if not by_arch:
        raise ValueError(f"binary {binary!r} not found in result")
    all_archs = sorted(by_arch)

    canonical_root_role = f"{binary}-elf-folder.drv"
    # Key = (role, enforce). Split template nodes share a role but
    # differ in enforce — keep them as separate render boxes so the
    # split is visible. Plain (un-split) nodes have enforce=None.
    Key = tuple[str, Optional[tuple[str, Optional[str]]]]
    canonical_root_key: Key = (canonical_root_role, None)
    # key -> { arch: (drv_path_v0_or_None, child_keys) }
    merged: dict[Key, dict[str, tuple[Optional[tuple[str, str]], list[Key]]]] = {}
    key_class: dict[Key, str] = {}
    key_optional: dict[Key, bool] = {}
    for arch, (tmpl, classes, arr) in by_arch.items():
        for nid, node in enumerate(tmpl.nodes):
            if nid == tmpl.root_id:
                role = canonical_root_role
                enforce = None
            else:
                role = node.name
                enforce = node.enforce
            k: Key = (role, enforce)
            cls = (
                "toolchain" if node.is_toolchain
                else classes.get(nid, "?")
            )
            existing = key_class.get(k)
            order = {
                "variant_specific": 3,
                "common_dep": 2,
                "?": 1,
                "toolchain": 0,
            }
            if existing is None or order.get(cls, 0) > order.get(existing, 0):
                key_class[k] = cls
            key_optional[k] = key_optional.get(k, False) or node.optional
            drv = None
            if cls == "common_dep" and arr.hashes[nid]:
                drv = arr.hashes[nid][0]
            child_keys: list[Key] = [
                (tmpl.nodes[c].name, tmpl.nodes[c].enforce)
                for c in node.child_ids
            ]
            merged.setdefault(k, {})[arch] = (drv, child_keys)

    # When collapsing, only render keys reachable from the root
    # without descending through a common_dep node.
    visible: set[Key]
    if collapse_common_deps:
        visible = set()
        stack: list[Key] = [canonical_root_key]
        while stack:
            r = stack.pop()
            if r in visible or r not in merged:
                continue
            visible.add(r)
            if key_class.get(r) == "common_dep":
                continue
            for arch, (_drv, children) in merged[r].items():
                stack.extend(children)
    else:
        visible = set(merged)

    # Render
    lines: list[str] = []
    safe_label = (label or f"merged_{binary}").replace('"', '\\"')
    lines.append(f'digraph "{safe_label}" {{')
    lines.append("  rankdir=LR;")
    lines.append("  node [shape=box, style=filled, fontname=monospace];")

    key_to_id: dict[Key, int] = {
        k: i for i, k in enumerate(k2 for k2 in merged if k2 in visible)
    }
    for k, archs_dict in merged.items():
        if k not in visible:
            continue
        role, enforce = k
        cls = key_class[k]
        if cls == "toolchain":
            fill = "lightgray"
            sharing = None
        elif cls == "variant_specific":
            fill = "lightcoral"
            sharing = None
        elif cls == "common_dep":
            drvs = {a: d for a, d in
                    ((a, archs_dict[a][0]) for a in archs_dict)
                    if d is not None}
            if not drvs:
                fill = "white"
                sharing = None
            else:
                cat = _classify_cross_arch_sharing(drvs)
                fill = {"A": "orange", "B": "yellow",
                        "C": "cyan", "D": "palegreen"}[cat]
                sharing = cat
        else:
            fill = "white"
            sharing = None
        missing = len(archs_dict) < len(all_archs)
        is_optional = key_optional.get(k, False)
        style = (
            "filled,dashed" if (missing or is_optional) else "filled"
        )
        suffix = f" [{sharing}]" if sharing else ""
        if is_optional:
            suffix = "*" + suffix
        suffix += _enforce_label(enforce)
        if missing:
            present = set(archs_dict)
            absent = set(all_archs) - present
            if len(present) == 1:
                only = next(iter(present))
                tag = "native-only" if only == "x86_64" else f"only-{only}"
            elif "x86_64" in absent and len(present) == len(all_archs) - 1:
                tag = "cross-only"
            elif len(absent) == 1:
                missing_one = next(iter(absent))
                tag = (
                    "no-native" if missing_one == "x86_64"
                    else f"no-{missing_one}"
                )
            elif "x86_64" in absent:
                tag = (
                    f"no-x86_64,{','.join(sorted(absent - {'x86_64'}))}"
                    if len(absent) <= 3
                    else f"no x86_64 (+{len(absent) - 1} cross)"
                )
            elif len(absent) <= 3:
                tag = f"-{','.join(sorted(absent))}"
            else:
                tag = f"in {','.join(sorted(present))}"
            miss_suffix = (
                f"  ({len(present)}/{len(all_archs)} • {tag})"
            )
        else:
            miss_suffix = ""
        safe_name = role.replace('"', '\\"')
        lines.append(
            f'  n{key_to_id[k]} '
            f'[label="{safe_name}{suffix}{miss_suffix}", '
            f'fillcolor={fill}, style="{style}"];'
        )
    # Edges: union of children across archs.
    for k, archs_dict in merged.items():
        if k not in visible:
            continue
        if collapse_common_deps and key_class.get(k) == "common_dep":
            continue
        edges: set[Key] = set()
        for arch, (_drv, children) in archs_dict.items():
            for c in children:
                edges.add(c)
        for c in edges:
            if c in key_to_id:
                lines.append(f"  n{key_to_id[k]} -> n{key_to_id[c]};")
    lines.append("}")
    return "\n".join(lines)


def save_binary_merged_dot(
    result: dict, binary: str, out_path: str
) -> None:
    with open(out_path, "w") as f:
        f.write(merge_binary_to_dot(result, binary))
        f.write("\n")


def save_template_dot(
    template: Template,
    classifications: Optional[dict],
    out_path: str,
    *,
    label: str = "template",
) -> None:
    with open(out_path, "w") as f:
        f.write(template_to_dot(template, classifications, label=label))
        f.write("\n")


def plan_from_tree_streaming(
    tree_text: str,
    *,
    archs: tuple[str, ...] = DEFAULT_ARCHS,
    name_extractor=drv_role,
    logger=None,
    lax: bool = False,
) -> dict:
    planner = StreamPlanner(
        archs=archs,
        name_extractor=name_extractor,
        logger=logger,
        lax=lax,
    )
    for line in tree_text.splitlines():
        planner.feed_line(line)
    result = planner.finalize()
    result["violations"] = planner.violations
    return result

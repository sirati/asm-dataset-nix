"""Template-graph algorithm: build templates, cowalk variants, classify.

Algorithm (Part B, Phase 1 corrected, plan
docs/dynrunner_refactor_findings_2f30920.md):

  1. Build a Template from one variant of a (binary, arch) by walking
     its drv closure ONE NODE AT A TIME. Nodes are keyed by package
     NAME (versions stripped). Toolchain drvs are terminals.
  2. Reuse an existing template by structural shape match if possible,
     else register a new one.
  3. For every variant of (binary, arch), cowalk template ↔ actual
     drv graph in lockstep, placing each non-toolchain node's drv
     hash into ``arr.hashes[node_id][variant_index]``. Within-variant
     DAG revisits assert hash equality at the already-stored slot.
  4. At end of arch group: non-toolchain LEAVES must carry all-equal
     hashes (→ per-arch common dep). Non-toolchain INTERNALS must
     carry all-distinct hashes (→ variant-specific). Any violation
     raises ``TemplateGraphAssertError`` carrying the variant labels
     that locked the template plus the failing-variant label.

Streaming contract: the algorithm never builds a closure dict. It
takes a callable ``get_record(drv_path) -> {"name", "inputDrvs"}``
that fetches ONE drv at a time. Each record is used in a single
visit and then dropped. Children of a node are sorted by the
package name embedded in their drv path (the store-path basename),
which means sorting does NOT require an extra ``get_record`` call
per child.
"""

from __future__ import annotations

from typing import Callable, Optional

from template_graph.graph.template import (
    Template,
    TemplateGraphAssertError,
    TemplateNode,
    _shape_equal,
    find_or_register_template,
)
from template_graph.graph.variant_array import VariantArray

__all__ = [
    "Template",
    "TemplateNode",
    "VariantArray",
    "TemplateGraphAssertError",
    "_shape_equal",
    "find_or_register_template",
    "Logger",
    "GetRecord",
    "NameExtractor",
    "derivation_package_name",
    "build_template_from_closure",
    "cowalk_and_index",
    "assert_arch_invariants",
    "plan_phase1_graph",
]


# ---------------------------------------------------------------------------
# Logger seam
# ---------------------------------------------------------------------------


Logger = Callable[[str], None]


def _noop_logger(_msg: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Package-name extraction
# ---------------------------------------------------------------------------


def derivation_package_name(drv_record_or_name) -> str:
    """Strip hash + version from a drv path / basename / record name.

    Examples::

        "/nix/store/abc-hello-2.12.drv" -> "hello"
        "gcc-wrapper-13.4.0"            -> "gcc-wrapper"
        "stdenv-linux"                  -> "stdenv-linux"   # no version
        {"name": "openssl-3.0.7"}       -> "openssl"
    """
    if isinstance(drv_record_or_name, dict):
        raw = drv_record_or_name.get("name", "")
    else:
        raw = drv_record_or_name
    if not isinstance(raw, str):
        return ""
    name = raw
    if name.endswith(".drv"):
        name = name[:-4]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
        if "-" in name:
            # Store-path basename: "<32-base32-hash>-<rest>". Strip hash.
            name = name.split("-", 1)[1]
    for i in range(len(name) - 1):
        if name[i] == "-" and name[i + 1].isdigit():
            return name[:i]
    return name


# ---------------------------------------------------------------------------
# Build template from a single variant
# ---------------------------------------------------------------------------


GetRecord = Callable[[str], dict]
NameExtractor = Callable[[str], str]


def build_template_from_closure(
    root_drv: str,
    get_record: GetRecord,
    toolchain_drvs: set,
    *,
    logger: Logger = _noop_logger,
    built_from_label: str = "<unknown>",
    name_extractor: NameExtractor = derivation_package_name,
    non_recursing_drvs: set | None = None,
) -> Template:
    """Walk root_drv DFS, fetching one record at a time; return Template.

    ``name_extractor(drv_path) -> name`` is the project hook that strips
    variant-axis suffixes (e.g. ``-x86_64-gcc15-O0-...``) and
    canonicalises toolchain-family names (e.g. ``gcc-wrapper``/
    ``clang-wrapper`` → ``cc-wrapper``) so that different variants of
    the same logical position get the SAME template-node name.

    ``non_recursing_drvs`` is the set of drv paths to treat as
    template terminals (no recursion). ``toolchain_drvs`` is the
    subset that additionally skips per-variant hash storage —
    toolchains are identified externally by (arch, compiler). Other
    terminals (build-side stdenv, native bash/findutils, ...) DO
    store hashes; the invariant pass classifies them as common deps
    when their hashes are constant across variants. Defaults to
    ``toolchain_drvs`` when not supplied.
    """
    if non_recursing_drvs is None:
        non_recursing_drvs = toolchain_drvs
    nodes: list[TemplateNode] = []
    name_to_id: dict[str, int] = {}

    def _alloc(name: str, is_toolchain: bool) -> int:
        nid = len(nodes)
        nodes.append(
            TemplateNode(name=name, child_ids=[], is_toolchain=is_toolchain)
        )
        name_to_id[name] = nid
        return nid

    def _visit(drv_path: str) -> int:
        # Derive the package name from the drv path directly — saves
        # one get_record() per child during sorting at the parent.
        name = name_extractor(drv_path)
        if not name:
            raise TemplateGraphAssertError(
                kind="empty-package-name",
                message=f"drv {drv_path!r} has no derivable package name",
                template_built_from=[built_from_label],
            )
        if name in name_to_id:
            existing = name_to_id[name]
            logger(
                f"[build] revisit name={name!r} -> existing node #{existing}"
            )
            return existing
        is_toolchain = drv_path in toolchain_drvs
        nid = _alloc(name, is_toolchain)
        logger(
            f"[build] alloc #{nid} name={name!r} toolchain={is_toolchain} "
            f"drv={drv_path}"
        )
        if drv_path in non_recursing_drvs:
            # Template terminal — do not descend. (is_toolchain
            # already captures the storage-skip behaviour; other
            # terminals will store hashes and be classified by the
            # end-of-arch invariant pass.)
            return nid
        record = get_record(drv_path)
        input_drvs = record.get("inputDrvs") or {}
        if not isinstance(input_drvs, dict):
            input_drvs = {}
        children_sorted = sorted(
            input_drvs.keys(),
            key=lambda c: (name_extractor(c), c),
        )
        # Discard record before recursing so we don't pile up records
        # on the Python call stack — only the algorithm's TemplateNode
        # data structures persist.
        del record
        for child_drv in children_sorted:
            cid = _visit(child_drv)
            nodes[nid].child_ids.append(cid)
        return nid

    root_id = _visit(root_drv)
    return Template(
        nodes=nodes,
        name_to_id=name_to_id,
        root_id=root_id,
        template_built_from=[built_from_label],
    )


# ---------------------------------------------------------------------------
# Cowalk
# ---------------------------------------------------------------------------


def cowalk_and_index(
    template: Template,
    drv_path: str,
    get_record: GetRecord,
    arr: VariantArray,
    variant_index: int,
    toolchain_drvs: set,
    current_variant_label: str,
    *,
    logger: Logger = _noop_logger,
    name_extractor: NameExtractor = derivation_package_name,
    non_recursing_drvs: set | None = None,
) -> None:
    """Walk template ↔ actual drv graph in lockstep, indexing hashes.

    Caller must have:
      - reset every template node's ``visit_flag`` to False,
      - grown each ``arr.hashes`` row by one ``None`` at ``variant_index``,
      - appended ``current_variant_label`` to ``arr.variants``.
    """
    if non_recursing_drvs is None:
        non_recursing_drvs = toolchain_drvs

    def _visit(node_id: int, drv: str) -> None:
        node = template.nodes[node_id]
        if node.visit_flag:
            logger(
                f"[cowalk] revisit node #{node_id} name={node.name!r} drv={drv}"
            )
            if not node.is_toolchain:
                stored = arr.hashes[node_id][variant_index]
                if stored != drv:
                    raise TemplateGraphAssertError(
                        kind="dag-revisit-hash-mismatch",
                        message=(
                            f"on DAG revisit to node #{node_id} "
                            f"({node.name!r}), observed drv differs from "
                            f"earlier visit in the same variant"
                        ),
                        template_built_from=template.template_built_from,
                        failing_variant=current_variant_label,
                        node_name=node.name,
                        details={"stored": stored, "observed": drv},
                    )
            return

        node.visit_flag = True
        if node.is_toolchain:
            logger(
                f"[cowalk] skip-toolchain node #{node_id} name={node.name!r} "
                f"drv={drv}"
            )
            return
        logger(
            f"[cowalk] index node #{node_id} variant={variant_index} "
            f"name={node.name!r} hash={drv}"
        )
        arr.hashes[node_id][variant_index] = drv

        # If the actual drv is in the non-recurse set, this is a
        # leaf in the template — don't descend further. Template
        # MUST have child_ids=[] here; we sanity-check below.
        if drv in non_recursing_drvs:
            if node.child_ids:
                raise TemplateGraphAssertError(
                    kind="terminal-shape-mismatch",
                    message=(
                        f"actual drv {drv!r} is non-recursing but "
                        f"template node #{node_id} ({node.name!r}) "
                        f"has {len(node.child_ids)} child(ren)"
                    ),
                    template_built_from=template.template_built_from,
                    failing_variant=current_variant_label,
                    node_name=node.name,
                )
            return
        record = get_record(drv)
        input_drvs = record.get("inputDrvs") or {}
        if not isinstance(input_drvs, dict):
            input_drvs = {}
        # Sort actual children by package name (from drv path) to match
        # the template's child ordering. No extra get_record() needed.
        actual_by_name: dict[str, list[str]] = {}
        for child_drv in input_drvs.keys():
            cname = name_extractor(child_drv)
            actual_by_name.setdefault(cname, []).append(child_drv)
        actual_names_sorted = sorted(actual_by_name)
        template_child_names = [
            template.nodes[cid].name for cid in node.child_ids
        ]
        template_unique_sorted = sorted(set(template_child_names))
        # Discard record + input_drvs map now — we have everything we need.
        del record, input_drvs

        if actual_names_sorted != template_unique_sorted:
            raise TemplateGraphAssertError(
                kind="child-name-mismatch",
                message=(
                    f"at node #{node_id} ({node.name!r}): actual drv "
                    f"children differ from template shape"
                ),
                template_built_from=template.template_built_from,
                failing_variant=current_variant_label,
                node_name=node.name,
                details={
                    "template_children": template_child_names,
                    "actual_children": actual_names_sorted,
                    "drv": drv,
                },
            )

        for cid in node.child_ids:
            cname = template.nodes[cid].name
            actual_drvs = actual_by_name[cname]
            if len(actual_drvs) != 1:
                raise TemplateGraphAssertError(
                    kind="multi-drv-same-name",
                    message=(
                        f"at node #{node_id} ({node.name!r}), child name "
                        f"{cname!r} appears in actual closure "
                        f"{len(actual_drvs)} times"
                    ),
                    template_built_from=template.template_built_from,
                    failing_variant=current_variant_label,
                    node_name=node.name,
                    details={"child_name": cname, "drvs": actual_drvs},
                )
            _visit(cid, actual_drvs[0])

    _visit(template.root_id, drv_path)


# ---------------------------------------------------------------------------
# End-of-arch invariants
# ---------------------------------------------------------------------------


def assert_arch_invariants(
    arr: VariantArray, template: Template
) -> dict[int, str]:
    """Classify each non-toolchain node by comparing the calibration
    pair (variants 0 and 1), then assert every subsequent variant
    matches the classification.

    Rules:
      hashes[node][0] == hashes[node][1]  → common_dep
      hashes[node][0] != hashes[node][1]  → variant_specific

    After classification:
      common_dep nodes:        hashes[node][k] must equal hashes[node][0]
                               for every k > 1 (any divergence is a
                               template-shape escape, hard error).
      variant_specific nodes:  hashes[node][k] must not equal
                               hashes[node][j] for any j < k (two
                               variants accidentally sharing a hash
                               that's supposed to vary is also a hard
                               error).

    Single-variant arch groups skip classification and mark every node
    as common_dep — with one observation, every hash is trivially
    "constant".
    """
    classification: dict[int, str] = {}
    n_variants = len(arr.variants)
    for node_id, node in enumerate(template.nodes):
        if node.is_toolchain:
            continue
        hashes = arr.hashes[node_id]
        if any(h is None for h in hashes):
            missing = [
                arr.variants[i] for i, h in enumerate(hashes) if h is None
            ]
            raise TemplateGraphAssertError(
                kind="missing-hash-after-cowalk",
                message=(
                    f"node #{node_id} ({node.name!r}) was not reached "
                    f"during cowalk in {len(missing)} variant(s)"
                ),
                template_built_from=template.template_built_from,
                node_name=node.name,
                details={"missing_variants": missing},
            )
        if n_variants < 2:
            # Nothing to compare against; treat as common_dep.
            classification[node_id] = "common_dep"
            continue
        is_common = hashes[0] == hashes[1]
        if is_common:
            classification[node_id] = "common_dep"
            for k in range(2, n_variants):
                if hashes[k] != hashes[0]:
                    raise TemplateGraphAssertError(
                        kind="common-dep-divergent",
                        message=(
                            f"node #{node_id} ({node.name!r}) was "
                            f"classified as common_dep by the "
                            f"calibration pair ({arr.variants[0]!r}, "
                            f"{arr.variants[1]!r}) but variant "
                            f"{arr.variants[k]!r} (index {k}) has a "
                            f"divergent hash"
                        ),
                        template_built_from=template.template_built_from,
                        node_name=node.name,
                        details={
                            "calibration_hash": hashes[0],
                            "divergent_variant": arr.variants[k],
                            "divergent_hash": hashes[k],
                        },
                    )
        else:
            classification[node_id] = "variant_specific"
            seen: dict[str, str] = {hashes[0]: arr.variants[0],
                                    hashes[1]: arr.variants[1]}
            for k in range(2, n_variants):
                h = hashes[k]
                if h in seen:
                    raise TemplateGraphAssertError(
                        kind="variant-specific-collision",
                        message=(
                            f"node #{node_id} ({node.name!r}) was "
                            f"classified as variant_specific by the "
                            f"calibration pair ({arr.variants[0]!r}, "
                            f"{arr.variants[1]!r}) but variants "
                            f"{seen[h]!r} and {arr.variants[k]!r} "
                            f"share a hash"
                        ),
                        template_built_from=template.template_built_from,
                        node_name=node.name,
                        details={
                            "collision_hash": h,
                            "first_variant": seen[h],
                            "colliding_variant": arr.variants[k],
                        },
                    )
                seen[h] = arr.variants[k]
    return classification


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def plan_phase1_graph(
    variants_by_binary_arch: dict,
    toolchain_drvs: set,
    *,
    get_record: GetRecord,
    logger: Logger = _noop_logger,
    name_extractor: NameExtractor = derivation_package_name,
    non_recursing_drvs: set | None = None,
) -> dict:
    """Run the algorithm end-to-end (streaming per-drv fetches)."""
    templates: list[Template] = []
    variant_arrays: dict[tuple[int, str], VariantArray] = {}
    placement: dict[str, tuple[int, str, int]] = {}
    classifications: dict[tuple[int, str], dict[int, str]] = {}

    for (binary, arch), variants in variants_by_binary_arch.items():
        if not variants:
            continue
        first_label, first_drv = variants[0]
        candidate = build_template_from_closure(
            root_drv=first_drv,
            get_record=get_record,
            toolchain_drvs=toolchain_drvs,
            logger=logger,
            built_from_label=first_label,
            name_extractor=name_extractor,
            non_recursing_drvs=non_recursing_drvs,
        )
        tmpl_id, was_new = find_or_register_template(templates, candidate)
        template = templates[tmpl_id]

        arr_key = (tmpl_id, arch)
        if arr_key not in variant_arrays:
            variant_arrays[arr_key] = VariantArray(
                template_id=tmpl_id,
                arch=arch,
                variants=[],
                hashes=[[] for _ in template.nodes],
            )
        arr = variant_arrays[arr_key]

        for vidx, (label, drv) in enumerate(variants):
            v_pos = len(arr.variants)
            arr.variants.append(label)
            for row in arr.hashes:
                row.append(None)
            for n in template.nodes:
                n.visit_flag = False
            cowalk_and_index(
                template=template,
                drv_path=drv,
                get_record=get_record,
                arr=arr,
                variant_index=v_pos,
                toolchain_drvs=toolchain_drvs,
                current_variant_label=label,
                logger=logger,
                name_extractor=name_extractor,
                non_recursing_drvs=non_recursing_drvs,
            )
            placement[drv] = (tmpl_id, arch, v_pos)
            if (
                was_new
                and vidx == 1
                and len(template.template_built_from) == 1
            ):
                template.template_built_from.append(label)

        classifications[arr_key] = assert_arch_invariants(arr, template)

    return {
        "templates": templates,
        "variant_arrays": variant_arrays,
        "placement": placement,
        "common_deps_per_arch_template": classifications,
    }

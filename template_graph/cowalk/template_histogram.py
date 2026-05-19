"""Cross-binary template shape histogram (Phase 4 diagnostic).

Risk-register #3 ("Cross-binary template dedup surfaces shape variation
we didn't expect") asks for a diagnostic that groups per-arch templates
by their canonical structural shape so we can verify two-binary smoke
runs produce sensible cross-binary template counts before relying on
the dedup.

The histogram groups every ``(template_id, arch)`` cell present in an
``OutputState.variant_arrays`` by a shape signature derived from a
canonical pre-order walk of the template. The variant-root role is
collapsed to a placeholder (each per-arch root embeds the
``<binary>-<arch>-...`` axis, so literal-name shape equality would
falsely separate per-arch and per-binary instances of one structural
shape) and each node contributes its role, enforce-tag, and
toolchain/optional flags plus the child-count to the signature. The
result is a hash that two structurally-equivalent templates share
regardless of which binary or arch produced them.

This module is purely diagnostic — no production code consumes the
histogram. The intended use is Phase 5 development: invoke
:func:`template_shape_histogram` on a finalised planner state and read
``multi_arch_count`` / ``multi_binary_count`` to sanity-check whether
the cross-binary dedup is producing the sharing pattern the planner
design expects.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Union

from template_graph.graph import Template

if TYPE_CHECKING:
    from template_graph.streaming.state import OutputState


# Placeholder role used in the shape signature for any node that names
# the variant-root (matches ``-elf-folder.drv`` suffix). Collapsing this
# is what makes two structurally-equal templates from different archs
# or different binaries hash to the same shape signature.
_ROOT_ROLE_PLACEHOLDER = "<ROOT>"


@dataclass(frozen=True)
class TemplateShapeHistogram:
    """Diagnostic histogram of template shapes across binaries + archs.

    ``by_shape`` maps each shape signature to a per-shape breakdown.
    Each breakdown is a dict with three keys:

      * ``"per_arch"``   — ``arch -> count`` (how many ``(template_id,
        arch)`` cells of this shape were seen for that arch).
      * ``"per_binary"`` — ``binary -> count`` (the same count, split
        by binary instead).
      * ``"total"``      — total ``(template_id, arch)`` cells of this
        shape across every arch + binary.

    Top-level counts:

      * ``total``               — sum of every per-shape ``total``;
        equals ``len(out.variant_arrays)`` for inputs whose every cell
        has a recognisable variant-root.
      * ``arch_specific_count`` — shapes seen in exactly one arch.
      * ``multi_arch_count``    — shapes seen in two or more archs
        (cross-arch sharing candidates).
      * ``multi_binary_count``  — shapes seen across two or more
        binaries (cross-binary sharing candidates).
    """

    by_shape: dict[str, dict[str, object]]
    total: int
    arch_specific_count: int
    multi_arch_count: int
    multi_binary_count: int


def _canonical_role(role: str) -> str:
    """Collapse a variant-root role onto a placeholder.

    The variant-root role embeds ``<binary>-<arch>-...-elf-folder.drv``;
    keeping it literal would split every per-arch + per-binary instance
    of one structural shape into its own histogram bucket. Every other
    role is returned unchanged.
    """
    if role.endswith("-elf-folder.drv"):
        return _ROOT_ROLE_PLACEHOLDER
    return role


def _shape_signature(template: Template) -> str:
    """Hash a template's canonical pre-order walk into a stable string.

    Walks ``template`` from ``root_id`` in DFS pre-order, child-index
    ordered (matching :func:`_shape_equal`'s recursion). Each node
    contributes ``(canonical_role, enforce, is_toolchain, optional,
    num_children)``. DAG-revisits emit a back-reference token so two
    different DAG shapes with the same per-node payload don't collide.
    """
    parts: list[str] = []
    visited: dict[int, int] = {}

    def _visit(nid: int) -> None:
        if nid in visited:
            parts.append(f"BACK:{visited[nid]}")
            return
        idx = len(visited)
        visited[nid] = idx
        node = template.nodes[nid]
        role = _canonical_role(node.name)
        parts.append(
            f"N:{role}|e={node.enforce!r}|tc={int(node.is_toolchain)}"
            f"|opt={int(node.optional)}|c={len(node.child_ids)}"
        )
        for c in node.child_ids:
            _visit(c)
        parts.append("UP")

    _visit(template.root_id)
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _binary_of(template: Template) -> str:
    """Extract the binary name from a template's root role.

    Variant-root role is ``<binary>-<arch>-<comp>-<opt>-...-elf-folder.drv``;
    the binary is the first ``-``-delimited segment. Returns
    ``"<unknown>"`` when the root role doesn't carry the elf-folder
    suffix (e.g. singleton templates synthesised from non-variant trees
    in tests).
    """
    root_name = template.nodes[template.root_id].name
    if not root_name.endswith("-elf-folder.drv"):
        return "<unknown>"
    first_dash = root_name.find("-")
    if first_dash <= 0:
        return "<unknown>"
    return root_name[:first_dash]


def template_shape_histogram(
    source: Union["OutputState", Mapping[str, Any]],
) -> TemplateShapeHistogram:
    """Compute a per-shape histogram across every ``(template_id, arch)``
    cell in ``source``'s variant arrays.

    Accepts either an ``OutputState`` instance OR a
    ``StreamPlanner.finalize()`` result dict (mirroring the dual-shape
    contract of :func:`template_graph.cowalk._role_merge.collect_per_arch`).
    CLI callers usually only have the dict; tests usually pass the
    ``OutputState`` directly.

    Two templates produce the same shape signature iff they have the
    same canonical structural shape: same node count, same per-node
    ``(canonical_role, enforce, is_toolchain, optional, child_count)``
    in the same pre-order DFS walk, same DAG-revisit pattern. The
    variant-root role is canonicalised to a placeholder so cross-arch
    and cross-binary instances of one shape land in the same bucket.

    No production code consumes the returned histogram; it's intended
    for Phase-5 sanity checks via a CLI hook or ad-hoc invocation.
    """
    if isinstance(source, Mapping):
        variant_arrays = source["variant_arrays"]
        templates = source["templates"]
    else:
        variant_arrays = source.variant_arrays
        templates = source.templates
    buckets: dict[str, dict[str, dict[str, int]]] = {}
    total = 0
    for (tid, arch), _arr in variant_arrays.items():
        template = templates[tid]
        sig = _shape_signature(template)
        binary = _binary_of(template)
        slot = buckets.setdefault(
            sig, {"per_arch": {}, "per_binary": {}},
        )
        slot["per_arch"][arch] = slot["per_arch"].get(arch, 0) + 1
        slot["per_binary"][binary] = slot["per_binary"].get(binary, 0) + 1
        total += 1
    by_shape: dict[str, dict[str, object]] = {}
    arch_specific = 0
    multi_arch = 0
    multi_binary = 0
    for sig, slot in buckets.items():
        cells_total = sum(slot["per_arch"].values())
        by_shape[sig] = {
            "per_arch": dict(slot["per_arch"]),
            "per_binary": dict(slot["per_binary"]),
            "total": cells_total,
        }
        if len(slot["per_arch"]) >= 2:
            multi_arch += 1
        else:
            arch_specific += 1
        if len(slot["per_binary"]) >= 2:
            multi_binary += 1
    return TemplateShapeHistogram(
        by_shape=by_shape,
        total=total,
        arch_specific_count=arch_specific,
        multi_arch_count=multi_arch,
        multi_binary_count=multi_binary,
    )


def format_histogram(hist: TemplateShapeHistogram) -> str:
    """Render a human-readable summary of the histogram.

    Used by the CLI ``--histogram`` flag (when wired) and developer
    REPL sessions. Sorts shapes by descending total cell-count so the
    most-shared shapes appear first; truncates the signature to its
    first 12 hex chars for compactness.
    """
    lines = [
        f"total cells:         {hist.total}",
        f"distinct shapes:     {len(hist.by_shape)}",
        f"arch_specific:       {hist.arch_specific_count}",
        f"multi_arch:          {hist.multi_arch_count}",
        f"multi_binary:        {hist.multi_binary_count}",
        "",
        "shape  total  archs  binaries",
    ]
    items = sorted(
        hist.by_shape.items(),
        key=lambda kv: (-int(kv[1]["total"]), kv[0]),
    )
    for sig, slot in items:
        archs = ",".join(sorted(slot["per_arch"]))
        binaries = ",".join(sorted(slot["per_binary"]))
        lines.append(
            f"{sig[:12]}  {slot['total']:>5}  {archs}  {binaries}"
        )
    return "\n".join(lines)


__all__ = [
    "TemplateShapeHistogram",
    "template_shape_histogram",
    "format_histogram",
]

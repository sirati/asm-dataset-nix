"""Per-binary slicing helpers for the streaming-planner result.

Extracted from :mod:`.plan` so the driver module stays under the
300-LOC cap (audit 6 — pre-extraction plan.py was 319 LOC). The
helpers are private (underscore prefix) and consumed exclusively by
``_plan_total_impl`` in :mod:`.plan`; they are not re-exported from
the package ``__init__``.

The contract: given a single streaming_result dict spanning multiple
binaries, derive a per-binary view that contains only the
``variant_arrays`` / ``common_deps_per_arch_template`` entries owned
by that binary. ``templates`` and the toolchain / arch-indep maps are
passed through unchanged so per-cell ``templates[tid]`` lookups still
resolve.
"""

from __future__ import annotations

from typing import Any, Mapping


def _binary_of_template(template: Any) -> str:
    """Return the binary name encoded in a template's root role.

    Variant-root role is ``<binary>-<arch>-<comp>-<opt>-...-elf-folder.drv``;
    the binary is the first ``-``-delimited segment. Returns
    ``"<unknown>"`` when the root role doesn't carry the elf-folder
    suffix (singleton templates synthesised from non-variant trees
    in tests).

    Re-implemented locally rather than imported from
    ``template_graph.cowalk.template_histogram._binary_of`` to keep
    this module's import surface tight (the histogram module pulls in
    hashlib + the full template graph machinery for an unrelated
    diagnostic).
    """
    nodes = getattr(template, "nodes", None)
    if nodes is None and isinstance(template, Mapping):
        nodes = template.get("nodes")
    if not nodes:
        return "<unknown>"
    root_id = getattr(template, "root_id", None)
    if root_id is None and isinstance(template, Mapping):
        root_id = template.get("root_id", 0)
    if not isinstance(root_id, int) or root_id < 0 or root_id >= len(nodes):
        return "<unknown>"
    root = nodes[root_id]
    root_name = getattr(root, "name", None)
    if root_name is None and isinstance(root, Mapping):
        root_name = root.get("name", "")
    if not isinstance(root_name, str):
        return "<unknown>"
    if not root_name.endswith("-elf-folder.drv"):
        return "<unknown>"
    first_dash = root_name.find("-")
    if first_dash <= 0:
        return "<unknown>"
    return root_name[:first_dash]


def _group_templates_by_binary(
    templates: list[Any],
) -> dict[str, set[int]]:
    """Map ``binary -> {template_id, ...}`` by inspecting each template's
    root role.
    """
    grouped: dict[str, set[int]] = {}
    for tid, template in enumerate(templates):
        binary = _binary_of_template(template)
        grouped.setdefault(binary, set()).add(tid)
    return grouped


def _slice_streaming_result(
    streaming_result: Mapping[str, Any],
    owned_template_ids: set[int],
) -> dict:
    """Return a streaming_result restricted to ``owned_template_ids``.

    Only ``variant_arrays`` and ``common_deps_per_arch_template`` need
    per-binary filtering — :func:`plan_phase4_for_binary` iterates
    ``variant_arrays`` and uses the classification map keyed by the
    same ``(tmpl_id, arch)`` pairs; the toolchain task_id wiring path
    reads each cell's ``hashes`` row. ``templates`` is passed through
    unchanged so per-cell ``templates[tid]`` lookups still resolve.
    """
    variant_arrays = streaming_result.get("variant_arrays", {}) or {}
    classifications = streaming_result.get(
        "common_deps_per_arch_template", {}
    ) or {}
    sliced_variant_arrays = {
        key: arr
        for key, arr in variant_arrays.items()
        if _key_template_id(key) in owned_template_ids
    }
    sliced_classifications = {
        key: cls
        for key, cls in classifications.items()
        if _key_template_id(key) in owned_template_ids
    }
    return {
        "templates": list(streaming_result.get("templates", []) or []),
        "variant_arrays": sliced_variant_arrays,
        "common_deps_per_arch_template": sliced_classifications,
        "toolchain_drvs": streaming_result.get("toolchain_drvs", set()),
        "arch_indep_deps": streaming_result.get("arch_indep_deps", {}),
        # Keyed by template_id; pass through unchanged because the
        # adapter looks up per-cell tmpl_id and templates list is also
        # un-sliced (cells for non-owned templates are filtered above).
        "toolchain_node_ids_per_template": streaming_result.get(
            "toolchain_node_ids_per_template", {}
        ),
        # Per-binary keyed; the adapter reads only ``[binary]`` so the
        # entire dict is fine to pass through unsliced (Phase 5.4).
        "meta_templates": streaming_result.get("meta_templates", {}),
    }


def _key_template_id(key: Any) -> int:
    """Extract the ``template_id`` from a variant_arrays / classification
    key. The streaming planner uses ``(int, str)`` tuples natively but
    JSON-roundtripped data may use ``"<tid>|<arch>"`` strings.
    """
    if isinstance(key, tuple) and len(key) == 2:
        try:
            return int(key[0])
        except (TypeError, ValueError):
            return -1
    if isinstance(key, str) and "|" in key:
        tid_s, _ = key.split("|", 1)
        try:
            return int(tid_s)
        except ValueError:
            return -1
    return -1

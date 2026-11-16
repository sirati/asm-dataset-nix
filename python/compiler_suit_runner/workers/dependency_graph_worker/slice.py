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


def _group_templates_by_binary(
    variant_arrays: Mapping[Any, Any],
) -> dict[str, set[int]]:
    """Map ``binary -> {template_id, ...}`` from the variant arrays.

    Each array records the matrix it was built under
    (``VariantArray.binary``). Reading that instead of parsing the
    template's root drv name keeps dashed binaries such as ``c-ares``
    and ``zlib-ng`` apart from ``c`` and ``zlib``. Arrays without a
    binary group under ``"<unknown>"``.
    """
    grouped: dict[str, set[int]] = {}
    for key, arr in variant_arrays.items():
        binary = getattr(arr, "binary", None)
        if binary is None and isinstance(arr, Mapping):
            binary = arr.get("binary")
        grouped.setdefault(binary or "<unknown>", set()).add(
            _key_template_id(key)
        )
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

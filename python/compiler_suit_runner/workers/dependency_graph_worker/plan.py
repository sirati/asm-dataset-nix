"""Streaming-planner driver: tree text → phase-4 descriptors.

Two entry points:

  * :func:`plan_binary` — single-binary path kept for back-compat with
    existing tests that monkey-patch it.
  * :func:`plan_total` — multi-binary path used by ``run_dependency_graph_task``
    after the per-binary loop collapse. Runs the streaming planner ONCE
    on a multi-binary sum-drv tree and partitions the result per binary
    before feeding :func:`plan_phase4_from_graph`.
"""

from __future__ import annotations

from typing import Any, Mapping


__all__ = [
    "plan_binary",
    "plan_total",
]


def plan_binary(
    *,
    binary: str,
    tree_text: str,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
) -> list[Any]:
    """Run the streaming planner + dependency_graph_planner adapter
    against ``tree_text`` for one binary.

    Returns the list of :class:`Phase4Descriptor` records. Raises
    :class:`DependencyGraphCycleError` (from the adapter) on cycle
    detection; the caller logs + propagates.
    """
    from template_graph.streaming import plan_from_tree_streaming  # noqa: PLC0415
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
        plan_phase4_from_graph,
    )

    streaming_result = plan_from_tree_streaming(tree_text)
    inp = BinaryPlanInput(
        binary=binary,
        streaming_result=streaming_result,
        variant_lookup=variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    return plan_phase4_from_graph([inp], sys_name=sys_name)


def plan_total(
    *,
    tree_text: str,
    binaries: list[str],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
) -> list[Any]:
    """Run ONE streaming pass against ``tree_text`` and emit a single
    flat phase-4 descriptor list spanning all binaries.

    ``binaries`` is the deterministic ordered list of binary names that
    the caller assembled into the multi-binary sum-drv. ``variant_lookups``
    maps each binary to its own ``(arch, label) -> variant_spec`` dict.

    The streaming planner runs ONCE; cross-binary template dedup fires
    within the single :class:`StreamPlanner` instance (its ``templates``
    + ``find_or_register_template`` collapse equivalent shapes, and the
    finalize pass keys ``meta_templates`` / ``arch_indep_deps`` per
    binary). We then partition ``streaming_result["variant_arrays"]``
    + ``["common_deps_per_arch_template"]`` per binary by inspecting
    each template's root role (``<binary>-...-elf-folder.drv``) so each
    binary's :class:`BinaryPlanInput` only sees its own variants.

    Raises :class:`DependencyGraphCycleError` on cycle detection.
    """
    from template_graph.streaming import plan_from_tree_streaming  # noqa: PLC0415
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
        plan_phase4_from_graph,
    )

    streaming_result = plan_from_tree_streaming(tree_text)

    # Group templates by the binary their root role encodes. Each
    # binary's BinaryPlanInput consumes a sliced streaming_result
    # carrying ONLY the templates / variant_arrays / classifications
    # that belong to that binary; toolchain_drvs and arch_indep_deps
    # are shared (the planner adapter only reads variant_arrays +
    # classifications + templates from the per-binary slice).
    templates = list(streaming_result.get("templates", []) or [])
    binary_to_template_ids = _group_templates_by_binary(templates)

    inputs: list[BinaryPlanInput] = []
    for binary in binaries:
        owned_ids = binary_to_template_ids.get(binary, set())
        sliced = _slice_streaming_result(streaming_result, owned_ids)
        inputs.append(BinaryPlanInput(
            binary=binary,
            streaming_result=sliced,
            variant_lookup=variant_lookups.get(binary, {}),
            toolchain_task_ids=toolchain_task_ids,
        ))

    return plan_phase4_from_graph(inputs, sys_name=sys_name)


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

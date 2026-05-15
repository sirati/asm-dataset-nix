"""Phase 1 task-graph planner.

This module runs on the promoted primary after Phase 0 quiesces (every
``phase0_eval_<binary>`` task ``Completed``). Its job is to translate
the Phase 0 manifests + per-variant drv graphs into the Phase 1 task
set: one task per common dep + one task per variant, with
``task_depends_on`` wired so the framework's scheduler holds variants
until their toolchain and common-dep dependencies finish.

Algorithm (see plan ``lively-beaming-summit.md`` Part B):

1. Load every variant's drv path from the Phase 0 manifest set.
2. Walk each variant drv's ``inputDrvs`` once via
   :func:`compiler_suit_runner.drv_graph.read_input_drvs`. The walk is
   shallow (direct inputs only) — refcount semantics target one-hop
   shared deps; transitive deps are handled implicitly because nix
   builds substitute everything reachable from the realised closure.
3. Refcount every input drv across all variants.
4. Common deps = ``{drv | refcount[drv] >= 2}``. Note: refcount >= 2
   (NOT >= 10) — the threshold's purpose is "share whenever the same
   drv appears in more than one variant".
5. Emit :class:`ManifestHeader` records: one ``phase2_common_dep`` per
   common dep, then one ``phase3_variant`` per variant with
   ``task_depends_on`` wired to whichever common-dep + toolchain
   task_ids are reachable from its direct inputs.
6. Pass the full header list to the framework via the injected
   ``spawn_tasks`` callback (which will be the Q5 ``primary.spawn_tasks``
   API once that lands in the framework; for now it is a callable
   parameter so unit tests inject mocks and production wiring can stub
   it to the legacy local-graph path until Q5 ships).

Cycle handling: drv graphs are DAGs by nix's invariant, but we defend
against the impossible case with an explicit visited set. A cycle is
surfaced as :class:`Phase1PlannerError`.
"""

from __future__ import annotations

import collections
import json
import pathlib
from collections.abc import Callable, Iterable, Mapping
from typing import Optional

from compiler_suit_runner import drv_graph
from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    common_dep_task_id,
    make_common_dep_header,
    make_variant_header,
)
from compiler_suit_runner.partition import VariantSpec


class Phase1PlannerError(RuntimeError):
    """Raised when the Phase 1 planner cannot synthesise the task graph.

    Currently this is only the cycle-detection escape hatch; future
    failure modes (e.g. orphaned variant whose toolchain is missing
    from ``toolchain_task_ids``) would surface here too.
    """


# Subprocess-injected drv-graph reader. Default delegates to
# :func:`drv_graph.read_input_drvs`; tests override via the ``reader``
# parameter on :func:`plan_phase1`.
DrvInputsReader = Callable[[str], set[str]]


def _default_reader(drv_path: str) -> set[str]:
    return drv_graph.read_input_drvs(drv_path)


# ---------------------------------------------------------------------------
# Phase 0 manifest IO
# ---------------------------------------------------------------------------


def read_phase0_manifests(out_dir: pathlib.Path) -> dict[str, dict]:
    """Read every ``out/<binary>/_phase0/manifest.json`` under ``out_dir``.

    Returns a mapping from ``binary`` (the manifest's ``binary`` field)
    to the parsed manifest dict. Missing or malformed manifests are
    skipped silently — the caller already has the Phase 0 task results
    via the framework's task_completed hook; this read is a defensive
    reconstruction path for resume scenarios.

    The expected on-disk layout is::

        out/<binary>/_phase0/manifest.json

    matching the resume marker described in the plan's "Resume
    support" section.
    """
    out_dir = pathlib.Path(out_dir)
    manifests: dict[str, dict] = {}
    if not out_dir.is_dir():
        return manifests

    # Glob walks the layout exactly so peer-side scratch dirs that
    # happen to live alongside ``out`` don't get pulled in.
    for manifest_path in sorted(out_dir.glob("*/_phase0/manifest.json")):
        try:
            text = manifest_path.read_text(encoding="utf-8")
            parsed = json.loads(text)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        binary = parsed.get("binary")
        if not isinstance(binary, str) or not binary:
            continue
        manifests[binary] = parsed
    return manifests


# ---------------------------------------------------------------------------
# Graph walking
# ---------------------------------------------------------------------------


def _walk_drv_subgraph(
    root_drv: str,
    *,
    reader: DrvInputsReader,
    memo: dict[str, set[str]],
) -> None:
    """Walk the full ``inputDrvs`` subgraph rooted at ``root_drv``.

    Populates ``memo`` so that ``memo[drv]`` is the set of direct
    inputs of ``drv`` for every drv reachable from ``root_drv``. Each
    distinct drv is read exactly once across the lifetime of ``memo``
    (the cache is shared across variants so two variants pointing at
    the same root drv only trigger one read, and a deep input shared
    between two variant subgraphs is also read only once).

    Cycle detection is iterative-DFS with a ``visiting`` stack; if a
    drv re-enters while still on the stack we raise
    :class:`Phase1PlannerError`. Drv graphs are DAGs by nix's
    invariant, so this is a defensive guard against malformed inputs
    (e.g. a manually-crafted .drv used during testing).
    """
    # Iterative DFS so deep graphs don't blow the recursion limit.
    # Stack entries: (drv, iterator-of-pending-children). We push a
    # drv with its child iterator, then drain children one at a time.
    if root_drv in memo:
        return
    visiting: set[str] = set()
    stack: list[tuple[str, list[str]]] = []

    def _push(drv: str) -> None:
        if drv in memo:
            return
        if drv in visiting:
            raise Phase1PlannerError("cycle in drv graph")
        visiting.add(drv)
        inputs = reader(drv)
        memo[drv] = set(inputs)
        stack.append((drv, list(inputs)))

    _push(root_drv)
    while stack:
        drv, pending = stack[-1]
        if not pending:
            visiting.discard(drv)
            stack.pop()
            continue
        child = pending.pop()
        # Order matters: ``visiting`` check FIRST. A back-edge to a
        # drv still on the DFS stack is the cycle signal, but that
        # drv is already in ``memo`` (we memoise before recursing
        # so a deep DAG of repeated subgraphs collapses to O(N))
        # — so the memo skip would mask the cycle if we checked
        # memo first.
        if child in visiting:
            raise Phase1PlannerError("cycle in drv graph")
        if child in memo:
            continue
        _push(child)


# ---------------------------------------------------------------------------
# Plan entry point
# ---------------------------------------------------------------------------


def plan_phase1(
    phase0_manifests: Mapping[str, Mapping],
    toolchain_task_ids: Mapping[str, str],
    spawn_tasks: Callable[[list[ManifestHeader]], None],
    *,
    sys_name: str = "x86_64-linux",
    reader: Optional[DrvInputsReader] = None,
) -> dict:
    """Build the Phase 1 task graph and hand it to ``spawn_tasks``.

    Parameters
    ----------
    phase0_manifests
        Mapping from binary name to the Phase 0 manifest dict. Each
        manifest must carry ``binary`` and a ``variants`` list whose
        entries are :class:`VariantSpec`-shaped dicts (at minimum
        ``label`` + ``drv``; other fields are forwarded to
        :func:`make_variant_header`).
    toolchain_task_ids
        Mapping from a toolchain drv path (the same identifier the
        variant's ``inputDrvs`` will reference) to the Phase -1
        toolchain task_id. Variants whose drv inputs include such a
        toolchain get the toolchain's task_id wired into
        ``task_depends_on``.
    spawn_tasks
        Callback that will be the framework's Q5
        ``primary.spawn_tasks`` API once it ships. Called exactly once
        with the full list of headers (common deps first, then
        variants — matches the iteration order in
        :func:`compiler_suit_runner.manifest_gen.emit_all_manifests`).
    sys_name
        Build system name (forwarded to
        :func:`make_variant_header`). Default matches the project's
        primary target.
    reader
        Injection seam for the drv reader. Production code uses
        :func:`drv_graph.read_input_drvs`; tests inject a mock.

    Returns
    -------
    dict
        Summary with three keys:
        ``common_deps`` (count of phase2_common_dep headers emitted),
        ``variants`` (count of phase3_variant headers emitted), and
        ``drv_graph_size`` (number of distinct drvs read — useful for
        sanity-checking the memo hit rate).
    """
    if reader is None:
        reader = _default_reader

    # 1+2. Walk each variant's drv subgraph. ``memo[d]`` is the set
    #      of direct ``inputDrvs`` of ``d`` for every drv reached. The
    #      walk is transitive (for cycle detection) but refcount below
    #      only looks at each variant's *direct* input set — the plan
    #      defines "common dep" as a drv listed by >=2 variants
    #      directly, not "appears anywhere in >=2 variants' closures".
    memo: dict[str, set[str]] = {}
    variant_inputs: dict[str, set[str]] = {}
    variant_records: list[tuple[str, VariantSpec]] = []

    for _binary, manifest in phase0_manifests.items():
        raw_variants = manifest.get("variants") if isinstance(manifest, Mapping) else None
        if not isinstance(raw_variants, Iterable):
            continue
        for variant in raw_variants:
            if not isinstance(variant, Mapping):
                continue
            drv_path = variant.get("drv")
            if not isinstance(drv_path, str) or not drv_path:
                continue
            _walk_drv_subgraph(drv_path, reader=reader, memo=memo)
            variant_inputs[drv_path] = memo[drv_path]
            # Preserve as a plain dict so make_variant_header can
            # subscript it; we cast to VariantSpec only at the type
            # level (runtime check would be over-eager — eval_worker
            # may emit additional fields).
            variant_records.append((drv_path, variant))  # type: ignore[arg-type]

    # 3. Refcount inputs across variants. Counter handles the "drv
    #    referenced once per variant in which it appears" semantics
    #    out of the box.
    refcount: collections.Counter[str] = collections.Counter()
    for inputs in variant_inputs.values():
        for dep in inputs:
            refcount[dep] += 1

    # 4. Common deps = refcount >= 2. (Plan: threshold is NOT 10.)
    #    Toolchain drvs are emitted as their own Phase -1 tasks
    #    (referenced via ``toolchain_task_ids``) so we exclude them
    #    here even if they happen to refcount >= 2 — otherwise we'd
    #    double-emit the toolchain as both phase2_toolchain and
    #    phase2_common_dep.
    toolchain_drv_set = set(toolchain_task_ids.keys())
    common_deps = sorted(
        drv
        for drv, n in refcount.items()
        if n >= 2 and drv not in toolchain_drv_set
    )

    # 5. Emit headers — common deps first, then variants. Sorting
    #    keeps the output deterministic so test assertions and
    #    log lines are stable.
    headers: list[ManifestHeader] = []
    for dep_drv in common_deps:
        # The drv path's basename (minus the .drv suffix) is a
        # human-recognisable label for log output; common_dep_task_id
        # already uses the same basename for id stability.
        label = pathlib.Path(dep_drv).stem
        headers.append(make_common_dep_header(dep_drv, label))

    common_dep_set = set(common_deps)
    for variant_drv, variant in sorted(variant_records, key=lambda v: v[0]):
        header = make_variant_header(variant, sys_name)
        # Recompute task_depends_on: include this variant's
        # common-dep deps + toolchain deps. make_variant_header sets a
        # single-toolchain default that we override here because Phase
        # 1's view of the dep set is richer (it has the drv-graph
        # walk; the static manifest_gen helper does not).
        deps: list[str] = []
        seen_deps: set[str] = set()
        for input_drv in sorted(variant_inputs.get(variant_drv, set())):
            if input_drv in common_dep_set:
                dep_id = common_dep_task_id(input_drv)
            elif input_drv in toolchain_task_ids:
                dep_id = toolchain_task_ids[input_drv]
            else:
                continue
            if dep_id not in seen_deps:
                seen_deps.add(dep_id)
                deps.append(dep_id)
        # Replace the header's default task_depends_on with the
        # planner-computed set. ManifestHeader is frozen so we
        # rebuild via dataclasses.replace-equivalent constructor.
        headers.append(
            ManifestHeader(
                item_class=header.item_class,
                name=header.name,
                size=header.size,
                payload=header.payload,
                task_id=header.task_id,
                task_depends_on=tuple(deps),
            )
        )

    # 6. Hand to the framework. Exactly one call per plan_phase1
    #    invocation — Q5's API expects atomic submission of a phase's
    #    task set so the matcher sees it as a coherent batch.
    spawn_tasks(headers)

    return {
        "common_deps": len(common_deps),
        "variants": len(variant_records),
        "drv_graph_size": len(memo),
    }

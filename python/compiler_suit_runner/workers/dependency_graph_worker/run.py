"""Top-level driver: archive walk → multi-binary sum-drv → one streaming
pass → descriptors.

Phase 5.2 collapse: every archive contributes ONE matrix wrapper to a
single sum-drv; the streaming planner runs ONCE over the resulting
tree so cross-binary template dedup fires automatically inside the
single :class:`StreamPlanner` instance. A single descriptor list is
produced spanning all binaries and streamed to the primary as batched
custom messages via ``task.send_message`` (the
:mod:`compiler_suit_runner.streamed_spawn` wire codec); a
human-readable ``_dependency_graph_summary.txt`` is written for
operator inspection.
"""

from __future__ import annotations

import collections
import logging
import pathlib
import time
from collections.abc import Callable, Sequence
from typing import Any, Optional, Union

from . import archive as _archive
from . import output as _output
from . import summary as _summary
from ...streamed_spawn import (
    SPAWN_TOPIC,
    SUMMARY_TOPIC,
    SpawnBatchEncoder,
)
from .errors import DependencyGraphResult, DependencyGraphWorkerError
from .subproc import RunSubprocess, default_run_subprocess


_LOG = logging.getLogger(__name__)


__all__ = [
    "run_dependency_graph_task",
]


def run_dependency_graph_task(
    *,
    task: Any,
    matrix_eval_out_dir: pathlib.Path,
    bash_path: str,
    toolchain_aggregate_drv: str,
    binary: Optional[str] = None,
    matrix_drv: Optional[str] = None,
    matrix_drvs: Optional[dict[str, str]] = None,
    toolchain_task_ids: Optional[dict[str, str]] = None,
    toolchain_outpaths_map: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
    build_deps_local: bool = False,
) -> DependencyGraphResult:
    """Assemble one sum-drv spanning ALL binaries' pre-built aggregate
    drvs and produce a single Phase 4 descriptor list (plus the
    ``_dependency_graph_summary.txt`` companion).

    Single all-binaries dispatch: the framework runs ONE
    dependency_graph task over every binary, fed each binary's
    ``matrix_aggregate_drv`` via the corresponding matrix_eval
    predecessor task's keyed outputs. The streaming planner runs ONCE
    over the combined tree so cross-binary template dedup fires inside
    the single :class:`StreamPlanner` instance.

      1. import every ``matrix-<binary>.drv.archive`` so the closures
         (and therefore the leaves each aggregate drv references) are
         materialised in the local store — required for the
         ``nix-store --query --tree`` walk further down;
      2. derive each binary's ``variant_lookup`` from its matrix
         aggregate via :func:`archive.derive_variant_lookup_from_aggregate`
         (D.1a);
      3. wrap every binary's aggregate drv in the multi-binary
         ``matrix_drvs`` mapping and call :func:`build_sum_drv_multi`,
         which in turn calls
         :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
         (the sole ``nix-instantiate`` in this phase);
      4. ``plan_from_drv_tree`` ONCE — the planner streams
         ``nix-store --query --tree`` tuples directly from
         :func:`stream_drv_tree`;
      5. one phase-4 descriptor list emitted spanning all binaries.

    Inputs: pass either ``matrix_drvs`` (a ``{binary: matrix_drv}``
    mapping, the all-binaries dispatch path) OR the single-binary
    ``binary`` + ``matrix_drv`` pair (back-compat / ad-hoc CLI path).
    ``toolchain_aggregate_drv`` MUST be non-empty — phase 3 cannot run
    without the producer's toolchain output.

    ``toolchain_outpaths_map`` is an optional ``{"<arch>/<comp>": outpath}``
    dict threaded from the submitter's pre-flight (the same drv→outpath
    table used to build the split archives). When present, every
    ``build_variant`` descriptor's payload carries ``toolchain_outpath``
    so the build worker can import the correct per-toolchain delta archive
    without a separate payload injection step. Absent map → empty string
    in every variant payload (build worker falls back to substitution,
    but with the mandatory split that is a hard-fail — always supply it
    when the split archives are uploaded).

    ``task`` is the framework task handle and is REQUIRED: the planned
    descriptors are streamed to the primary as batched custom messages
    via ``task.send_message`` (the Wave-1 streamed-spawn handoff, see
    :mod:`compiler_suit_runner.streamed_spawn`) — spawn batches on
    :data:`~compiler_suit_runner.streamed_spawn.SPAWN_TOPIC`, then
    exactly one summary on
    :data:`~compiler_suit_runner.streamed_spawn.SUMMARY_TOPIC` (an
    empty plan sends ONLY the total=0 summary). ``send_message``
    failures propagate: a partially-streamed plan must FAIL the task
    rather than limp to a clean exit.

    Per-binary failures raise :class:`DependencyGraphWorkerError`
    tagged with the binary + stage; the caller's main loop translates
    that into a non-zero exit code.
    """
    if not hasattr(task, "send_message"):
        raise RuntimeError(
            "run_dependency_graph_task: task object has no send_message();"
            " the streamed-spawn handoff hard-requires the dynrunner"
            " Wave-1 custom-message API (Task.send_message) — the"
            " framework pin is too old (or a non-Task object was"
            " passed). Refusing to plan without a streaming channel."
        )
    if not toolchain_aggregate_drv:
        raise ValueError(
            "run_dependency_graph_task: toolchain_aggregate_drv is "
            "empty; phase 3 requires the producer's toolchain wrapper "
            "drv path (see preflight._build_toolchains_aggregate_drv)."
        )

    # Normalise the two input shapes into a single ``{binary:
    # matrix_drv}`` mapping. The all-binaries dispatch passes
    # ``matrix_drvs``; the legacy / CLI path passes ``binary`` +
    # ``matrix_drv``.
    drv_by_binary: dict[str, str]
    if matrix_drvs is not None:
        if binary is not None or matrix_drv is not None:
            raise ValueError(
                "run_dependency_graph_task: pass either matrix_drvs OR "
                "the single-binary binary/matrix_drv pair, not both."
            )
        drv_by_binary = dict(matrix_drvs)
        if not drv_by_binary:
            raise ValueError(
                "run_dependency_graph_task: matrix_drvs is empty; phase "
                "3 requires at least one matrix-<binary> aggregate drv "
                "(see workers/build_worker dep_graph dispatch)."
            )
        for b, d in drv_by_binary.items():
            if not b:
                raise ValueError(
                    "run_dependency_graph_task: matrix_drvs contains an "
                    "empty binary name."
                )
            if not d:
                raise ValueError(
                    "run_dependency_graph_task: matrix_drvs[%r] is empty; "
                    "phase 3 requires each binary's matrix-<binary> "
                    "aggregate drv." % b
                )
    else:
        if not binary:
            raise ValueError(
                "run_dependency_graph_task: binary is empty; phase 3 "
                "requires the binary name this run plans."
            )
        if not matrix_drv:
            raise ValueError(
                "run_dependency_graph_task: matrix_drv is empty; phase 3 "
                "requires the matrix-<binary> aggregate drv (see "
                "workers/build_worker dep_graph dispatch)."
            )
        drv_by_binary = {binary: matrix_drv}

    clock_fn = clock or time.monotonic
    start = clock_fn()

    archives = _archive.discover_archives(matrix_eval_out_dir)
    if not archives:
        return _empty_result(
            task=task,
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})

    # Step 1: import the shared toolchain archive FIRST (toolchain-first),
    # then every per-binary archive. With toolchain dedup the per-binary
    # archives are diffs against the toolchain closure, so the toolchain
    # must be resident locally before they import. The leaves the
    # aggregate drv references must all be present before the
    # `nix-store --query --tree` walk further down.
    _import_toolchain_archive_or_raise(
        matrix_eval_out_dir=matrix_eval_out_dir, runner=runner,
    )
    _import_all_archives(archives=archives, runner=runner)

    # Step 2: derive the variant lookup for every binary from its
    # matrix aggregate drv (D.1a's helper). Binaries whose aggregate
    # yields an empty lookup are skipped (the path-form sum-drv helper
    # forbids zero-variant matrix wrappers).
    variant_lookups, plannable_binaries = (
        _derive_variant_lookups(
            drv_by_binary=drv_by_binary,
            toolchain_outpaths_map=toolchain_outpaths_map or {},
        )
    )

    if not plannable_binaries:
        return _empty_result(
            task=task,
            matrix_eval_out_dir=matrix_eval_out_dir,
            duration=max(0.0, clock_fn() - start),
        )

    # Step 3: wrap each binary's aggregate drv in a length-1 list keyed
    # by ``matrix-<binary>`` for the path-form sum-drv helper
    # (post-Phase-A.2 invariant).
    toolchain_drvs: list[str] = [toolchain_aggregate_drv]
    matrix_drvs_for_sum: dict[str, list[str]] = {
        f"matrix-{b}": [drv_by_binary[b]]
        for b in plannable_binaries
    }

    descriptors, counters, violation_entries = _plan_all_binaries(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs_for_sum,
        binaries=plannable_binaries,
        sys_name=sys_name,
        variant_lookups=variant_lookups,
        tc_ids=tc_ids,
    )

    _summary.emit_summary_log(
        binaries=plannable_binaries,
        counters=counters,
    )
    if counters.get("violations", 0) > 0:
        _summary.emit_violations_log(violation_entries)

    _stream_descriptors(task=task, descriptors=descriptors)

    # Step 6 (build_deps_local only): compute + export the build-deps output
    # closure archive so build workers can pre-load all variant build-input
    # paths before ``nix build`` fires.  Runs AFTER streaming so a failure
    # here does not silently suppress the spawn batches.  Raises on any
    # error — the dep_graph task must FAIL loudly if the archive cannot be
    # produced so the build tasks never start without their pre-load.
    if build_deps_local:
        _produce_build_deps_archive(
            descriptors=descriptors,
            variant_lookups=variant_lookups,
            matrix_eval_out_dir=matrix_eval_out_dir,
            runner=runner,
        )

    out_path = _write_outputs(
        matrix_eval_out_dir=matrix_eval_out_dir,
        descriptors=descriptors,
        binaries=plannable_binaries,
        counters=counters,
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=len(plannable_binaries),
        descriptor_count=len(descriptors),
        duration_seconds=max(0.0, clock_fn() - start),
        templates=counters.get("templates", 0),
        meta_templates=counters.get("meta_templates", 0),
        variants=counters.get("variants", 0),
        common_deps_cross_arch=counters.get("common_deps_cross_arch", 0),
        common_deps_family=counters.get("common_deps_family", 0),
        common_deps_uni_arch=counters.get("common_deps_uni_arch", 0),
        common_deps_arch_indep=counters.get("common_deps_arch_indep", 0),
        source_terminal_skipped=counters.get("source_terminal_skipped", 0),
        toolchain_wired=counters.get("toolchain_wired", 0),
        stdenv_subtrees=counters.get("stdenv_subtrees", 0),
        violations=counters.get("violations", 0),
    )


def _empty_result(
    *,
    task: Any,
    matrix_eval_out_dir: pathlib.Path,
    duration: float,
) -> DependencyGraphResult:
    """Write an empty summary + return a zero-counter result.

    Used on the two short-circuit paths (no archives discovered and no
    plannable binaries after lookup derivation) so the operator-facing
    ``_dependency_graph_summary.txt`` is well-formed even when there is
    nothing to plan. The streamed-spawn handoff still runs: an empty
    plan sends NO spawn batches and exactly one total=0 summary so the
    primary's reconciliation barrier sees a complete (empty) stream
    instead of silence.
    """
    _stream_descriptors(task=task, descriptors=[])
    out_path = _write_outputs(
        matrix_eval_out_dir=matrix_eval_out_dir,
        descriptors=[],
        binaries=[],
        counters={},
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=0,
        descriptor_count=0,
        duration_seconds=duration,
    )


def _stream_descriptors(
    *,
    task: Any,
    descriptors: Sequence[Any],
) -> None:
    """Stream the planned descriptors to the primary as batched custom
    messages (the Wave-1 streamed-spawn handoff).

    Encodes via :class:`SpawnBatchEncoder` in planner order (the
    planner mints common_deps before the variants that depend on them,
    so the stream is dependency-safe), sending every full batch on
    :data:`SPAWN_TOPIC`, the flush remainder (if any) on the same
    topic, then exactly one summary on :data:`SUMMARY_TOPIC` carrying
    the authoritative totals plus per-kind descriptor counts. An empty
    ``descriptors`` sends no batch messages and a total=0 summary.

    ``task.send_message`` failures (and encoder ValueErrors) propagate
    — a partially-streamed plan must fail the task loudly; the
    primary's reconciliation barrier catches the missing summary, but
    failing fast here is the first line of defence.
    """
    encoder = SpawnBatchEncoder()
    streamed = 0

    def _send_batch(message: bytes) -> None:
        nonlocal streamed
        task.send_message(SPAWN_TOPIC, message)
        _LOG.info(
            "streamed spawn batch %d (%d descriptors, %d bytes)",
            encoder.batches_emitted - 1,
            encoder.descriptors_emitted - streamed,
            len(message),
        )
        streamed = encoder.descriptors_emitted

    for descriptor in descriptors:
        message = encoder.add(descriptor)
        if message is not None:
            _send_batch(message)
    remainder = encoder.flush()
    if remainder is not None:
        _send_batch(remainder)

    per_kind_counters = collections.Counter(
        getattr(d, "kind", "<unknown>") for d in descriptors
    )
    task.send_message(
        SUMMARY_TOPIC, encoder.encode_summary(dict(per_kind_counters)),
    )
    _LOG.info(
        "streamed spawn done: %d descriptors in %d batches; summary sent",
        encoder.descriptors_emitted,
        encoder.batches_emitted,
    )


def _import_toolchain_archive_or_raise(
    *,
    matrix_eval_out_dir: pathlib.Path,
    runner: RunSubprocess,
) -> None:
    """Import the shared ``toolchains.drv.archive`` (toolchain-first).

    Toolchain-dedup pre-flight writes ONE ``toolchains.drv.archive``
    carrying the whole compiler-toolchain closure; the per-binary
    ``matrix-<binary>.drv.archive`` files are diffs against it. This
    archive MUST import before any per-binary archive or those diffs
    cannot resolve. Fatal (:class:`DependencyGraphWorkerError`, stage
    ``"toolchain_import"``) if the archive is missing or zero-byte —
    every diff archive would be un-importable otherwise.
    """
    archive = _archive.toolchain_archive_path(matrix_eval_out_dir)
    try:
        size = archive.stat().st_size
    except OSError:
        size = -1
    if size <= 0:
        raise DependencyGraphWorkerError(
            binary="<toolchain>", stage="toolchain_import",
            message=(
                "toolchains.drv.archive missing or zero-byte at "
                f"{archive} (size={size}); the per-binary diff archives "
                "are un-importable without it — was the submit "
                "pre-flight toolchain export skipped?"
            ),
        )
    ok, err, _imported = _archive.import_archive(archive, run_subprocess=runner)
    if not ok:
        raise DependencyGraphWorkerError(
            binary="<toolchain>", stage="toolchain_import",
            message=(
                "nix-store --import of toolchains.drv.archive failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
        )


def _import_all_archives(
    *,
    archives: list[pathlib.Path],
    runner: RunSubprocess,
) -> None:
    """Import every archive so the leaves the aggregate drv references
    are present in the local store.

    Post-D.1b: the import is still required (the streaming planner's
    ``nix-store --query --tree`` walk needs the closure resident
    locally), but we no longer derive kept drvs from the import
    stdout — D.1a's ``derive_variant_lookup_from_aggregate`` owns the
    variant_lookup, and the matrix aggregate drv replaces the leaf
    list passed to the sum-drv helper. The stdout from
    ``nix-store --import`` is therefore discarded; we only care about
    side effects (closure materialisation) and the success rc.
    """
    for archive in archives:
        # A zero-byte per-binary archive means the binary was fully gated
        # (all archs filtered out) — there is nothing to import. Skip it
        # (not an error); ``nix-store --import`` on empty input would be a
        # harmless no-op anyway, but skipping avoids the spurious call.
        try:
            if archive.stat().st_size == 0:
                continue
        except OSError:
            pass
        _import_archive_or_raise(
            archive=archive, runner=runner,
            binary=_archive.binary_from_archive_name(archive),
        )


def _derive_variant_lookups(
    *,
    drv_by_binary: dict[str, str],
    toolchain_outpaths_map: dict[str, str],
) -> tuple[dict[str, dict[tuple[str, str], dict]], list[str]]:
    """Build the per-binary ``variant_lookup`` map + plannable-binary
    list from every binary's matrix aggregate drv path.

    Calls :func:`archive.derive_variant_lookup_from_aggregate` (D.1a)
    on each binary's aggregate. A binary whose helper yields an empty
    lookup is silently skipped — ``make_sum_drv_from_paths`` forbids
    zero-variant matrices, and an empty lookup means the cluster
    produced no plannable variants for that binary in this run. The
    returned ``plannable_binaries`` list is sorted and contains only
    the binaries with a non-empty lookup.

    ``toolchain_outpaths_map`` is passed through to
    :func:`archive.derive_variant_lookup_from_aggregate` so each
    variant spec carries ``toolchain_outpath`` for the build worker's
    per-toolchain delta archive import.

    Raises :class:`DependencyGraphWorkerError` tagged with the offending
    binary + stage ``"variant_lookup"`` if the helper itself raises
    (e.g. malformed aggregate, missing closure entries).
    """
    lookups: dict[str, dict[tuple[str, str], dict]] = {}
    plannable: list[str] = []
    for binary in sorted(drv_by_binary):
        matrix_drv = drv_by_binary[binary]
        try:
            lookup = _archive.derive_variant_lookup_from_aggregate(
                matrix_drv,
                toolchain_outpaths_map=toolchain_outpaths_map,
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyGraphWorkerError(
                binary=binary, stage="variant_lookup",
                message=(
                    "derive_variant_lookup_from_aggregate failed for "
                    f"{matrix_drv!r}: {exc}"
                ),
                cause=exc,
            ) from exc
        if not lookup:
            continue
        lookups[binary] = lookup
        plannable.append(binary)
    return lookups, plannable


def _import_archive_or_raise(
    *,
    archive: pathlib.Path,
    runner: RunSubprocess,
    binary: str,
) -> list[str]:
    """``nix-store --import`` the archive, surfacing the stdout paths.

    Raises :class:`DependencyGraphWorkerError` (stage ``"import"``)
    on failure so the worker driver can convert that to a non-zero
    exit. On success returns the parsed list of imported store
    paths.
    """
    ok, err, imported_paths = _archive.import_archive(
        archive, run_subprocess=runner,
    )
    if not ok:
        raise DependencyGraphWorkerError(
            binary=binary, stage="import",
            message=(
                "nix-store --import failed: "
                + err.decode("utf-8", errors="replace").strip()
            ),
        )
    return imported_paths


def _plan_all_binaries(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    matrix_drvs: dict[str, list[str]],
    binaries: list[str],
    sys_name: str,
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    tc_ids: dict[str, str],
) -> tuple[list[Any], dict[str, int], list[dict]]:
    """Build the multi-binary sum-drv, stream-plan it, and produce
    ``(descriptors, counters, violation_entries)`` spanning every
    plannable binary.

    Stages are surfaced via :class:`DependencyGraphWorkerError` tagged
    with ``binary="<all>"`` for the sum-drv / tree-query / plan steps.
    The planner pulls its own ``nix-store --query --tree`` stream via
    :func:`stream_drv_tree` so this helper no longer needs the
    subprocess-runner injection. ``build_sum_drv_multi`` and
    ``plan_total`` are resolved through the package namespace so test
    monkeypatches are honoured; see :func:`summary.invoke_planner`
    for the planner-call shim.
    """
    import importlib  # noqa: PLC0415
    _pkg = importlib.import_module(__package__)

    try:
        sum_drv = _pkg.build_sum_drv_multi(
            bash_path=bash_path,
            toolchain_drvs=toolchain_drvs,
            matrix_drvs=matrix_drvs,
            system=sys_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary="<all>", stage="sum_drv",
            message=f"sum-drv assembly failed: {exc}",
            cause=exc,
        ) from exc

    try:
        return _summary.invoke_planner(
            pkg=_pkg,
            sum_drv=sum_drv,
            binaries=binaries,
            variant_lookups=variant_lookups,
            tc_ids=tc_ids,
            sys_name=sys_name,
        )
    except RuntimeError as exc:
        # ``stream_drv_tree`` raises ``RuntimeError`` on a non-zero
        # ``nix-store --query --tree`` exit; keep the historical
        # ``stage="query_tree"`` label so operator-side error
        # introspection stays the same.
        raise DependencyGraphWorkerError(
            binary="<all>", stage="query_tree",
            message=str(exc), cause=exc,
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise DependencyGraphWorkerError(
            binary="<all>", stage="plan",
            message=f"plan_phase4 failed: {exc}",
            cause=exc,
        ) from exc


def _build_summary(
    *,
    descriptors: Sequence[Any],
    binaries: Sequence[str],
    counters: dict[str, int],
) -> dict[str, Union[int, float, str]]:
    """Build the ``summary`` dict serialised to
    ``_dependency_graph_summary.txt``.

    Combines the planner-emitted counters (templates / meta_templates /
    common_deps_* / violations / etc.) with descriptor-derived
    aggregates (per-kind counts, per-priority_hint counts, binary list).
    """
    summary: dict[str, Union[int, float, str]] = dict(counters)
    summary["binary_count"] = len(binaries)
    summary["descriptor_count"] = len(descriptors)
    summary["binaries"] = "/".join(binaries) if binaries else "<none>"

    by_kind: collections.Counter = collections.Counter(
        getattr(d, "kind", "<unknown>") for d in descriptors
    )
    for kind, count in by_kind.items():
        summary[f"descriptors_by_kind.{kind}"] = count

    by_priority: collections.Counter = collections.Counter(
        getattr(d, "priority_hint", 0) for d in descriptors
    )
    for hint, count in by_priority.items():
        summary[f"descriptors_by_priority_hint.{hint}"] = count

    return summary


def _write_outputs(
    *,
    matrix_eval_out_dir: pathlib.Path,
    descriptors: Sequence[Any],
    binaries: Sequence[str],
    counters: dict[str, int],
) -> pathlib.Path:
    """Write the human-readable summary text atomically and return its
    path (the canonical ``output_path`` reported in
    :class:`DependencyGraphResult`).
    """
    summary = _build_summary(
        descriptors=descriptors, binaries=binaries, counters=counters,
    )
    summary_path = matrix_eval_out_dir / _output.DEPENDENCY_GRAPH_SUMMARY
    _output.write_phase4_summary_text(
        summary=summary, out_path=summary_path,
    )
    return summary_path


# ---------------------------------------------------------------------------
# Build-deps output closure (build_deps_local feature)
# ---------------------------------------------------------------------------


def _produce_build_deps_archive(
    *,
    descriptors: Sequence[Any],
    variant_lookups: dict[str, dict[tuple[str, str], dict]],
    matrix_eval_out_dir: pathlib.Path,
    runner: RunSubprocess,
) -> None:
    """Compute and export the build-deps output closure archive.

    Collects the unique set of variant drv paths from ``descriptors``,
    then for each variant drv runs ``nix-store --query --references`` to
    get its INPUT drvs, realises/substitutes the INPUT drv OUTPUTS (those
    are the build-input realised outputs ``nix build`` needs locally),
    computes ``nix-store --query --requisites`` over those output paths,
    subtracts the toolchain closure already shipped by the toolchain
    archives, and exports the remainder as ``build_deps.out.archive`` in
    ``matrix_eval_out_dir``.

    The COMPLETENESS GATE (fail-closed): after the export, verifies that
    every variant-drv input output path is covered by either the
    build_deps archive or the toolchain closure.  Any uncovered path would
    still be substituted at build time — that is the problem this feature
    solves.  A non-empty uncovered set raises :class:`RuntimeError` naming
    the offending paths so the operator can investigate and abort rather
    than silently running with partial pre-loading.

    Raises :class:`RuntimeError` on any failure (failed subprocess,
    unrealised inputs, export error, completeness gate violation).  The
    caller MUST let this propagate to fail the dep_graph task loudly.
    """
    import logging as _logging  # noqa: PLC0415
    _log = _logging.getLogger(
        "compiler_suit_runner.dependency_graph_worker.build_deps"
    )

    from compiler_suit_runner import preflight as _preflight  # noqa: PLC0415

    # 1. Collect the unique variant drv paths from the planned descriptors.
    variant_drvs: set[str] = set()
    for d in descriptors:
        if getattr(d, "kind", None) == "build_variant":
            drv = d.payload.get("drv") if hasattr(d, "payload") else None
            if drv and isinstance(drv, str):
                variant_drvs.add(drv)
    # Also pull from variant_lookups (covers cases where descriptors use a
    # spec dict rather than a Phase4Descriptor with .payload).
    for binary_lookup in variant_lookups.values():
        for spec in binary_lookup.values():
            drv = spec.get("drv") if isinstance(spec, dict) else None
            if drv and isinstance(drv, str):
                variant_drvs.add(drv)

    if not variant_drvs:
        _log.warning(
            "_produce_build_deps_archive: no variant drvs found in "
            "descriptors/lookups — skipping build_deps archive production"
        )
        return

    _log.info(
        "_produce_build_deps_archive: collecting input drvs for %d "
        "unique variant drvs",
        len(variant_drvs),
    )

    # 2. For each variant drv, get its INPUT drvs via
    #    ``nix-store --query --references``.  Filter to .drv paths only
    #    (source files are also references but are not derivations and
    #    have no "output" to realise).
    all_input_drvs: set[str] = set()
    _variant_pkg_drvs_skipped = 0
    for variant_drv in sorted(variant_drvs):
        stdout, stderr, rc = runner(
            ["nix-store", "--query", "--references", variant_drv]
        )
        if rc != 0:
            raise RuntimeError(
                "_produce_build_deps_archive: nix-store --query --references "
                f"failed for {variant_drv!r} (rc={rc}): "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            ref = line.strip()
            if not ref.endswith(".drv"):
                continue
            # Exclude the variant PACKAGE drv (``<pkg>-variant-<arch>-…``).
            # mkBinaryFolder.nix interpolates the variant package's outputs
            # into the elf-folder build script, so the variant package drv is
            # an inputDrv of the elf-folder drv we just queried. Its OUTPUT
            # (``…-<pkg>-variant-<arch>-…``) is exactly what the build phase
            # PRODUCES — it is not pre-fetchable from any substituter, so it
            # must never enter the realise/closure set (including it caused
            # the build_deps ``nix-store --realise`` hard gap). The
            # ``-variant-`` infix is set unconditionally by
            # ``pname = "${pkg.attr}-variant"`` (mkVariant.nix) and never
            # appears in a genuine build input.
            if "-variant-" in ref.rsplit("/", 1)[-1]:
                _variant_pkg_drvs_skipped += 1
                continue
            all_input_drvs.add(ref)

    if _variant_pkg_drvs_skipped:
        _log.info(
            "_produce_build_deps_archive: excluded %d variant-package drv(s) "
            "from the build-deps input set (their outputs are produced by the "
            "build phase, not pre-fetchable)",
            _variant_pkg_drvs_skipped,
        )

    if not all_input_drvs:
        _log.warning(
            "_produce_build_deps_archive: no input drvs found across "
            "%d variant drvs — skipping",
            len(variant_drvs),
        )
        return

    _log.info(
        "_produce_build_deps_archive: %d unique input drvs; resolving "
        "output paths",
        len(all_input_drvs),
    )

    # 3. Resolve the OUTPUT path of each input drv via
    #    ``nix-store --query --outputs``.  Note: this reads the .drv file
    #    and always returns the output path regardless of whether the path
    #    is realised in the store; a non-zero rc here means the .drv itself
    #    is missing from the local store (import failure), which is fatal.
    input_outpaths: list[str] = []
    for input_drv in sorted(all_input_drvs):
        stdout, stderr, rc = runner(
            ["nix-store", "--query", "--outputs", input_drv]
        )
        if rc != 0:
            raise RuntimeError(
                "_produce_build_deps_archive: nix-store --query --outputs "
                f"failed for {input_drv!r} (rc={rc}) — the .drv is not "
                "resident in the local store (import archive missing/corrupt?): "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            op = line.strip()
            if op:
                input_outpaths.append(op)

    if not input_outpaths:
        _log.warning(
            "_produce_build_deps_archive: no input outpaths resolved — "
            "build_deps archive would be empty; skipping"
        )
        return

    # 3b. REALISE the input output paths before compute_build_deps_closure.
    #     ``--query --requisites`` requires the paths to be valid in the
    #     store and export also needs them realised.  The dep_graph worker
    #     only holds the drv graph (imported drv archives), NOT the built
    #     outputs; we must substitute them from the configured substituters
    #     (cache.nixos.org etc.) first.  Build is attempted only when
    #     substitution is unavailable.
    #
    #     This realises the build-input closure on the dep_graph worker node
    #     (one-time).  It may add noticeable dep_graph-phase latency for
    #     large closures but is required for correctness.
    _log.info(
        "_produce_build_deps_archive: realising %d input outpath(s) via "
        "nix-store --realise (may add dep_graph-phase latency)",
        len(input_outpaths),
    )
    stdout, stderr, rc = runner(["nix-store", "--realise"] + input_outpaths)
    if rc != 0:
        failed_hint = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "_produce_build_deps_archive: nix-store --realise failed "
            f"(rc={rc}) for {len(input_outpaths)} input outpath(s) — "
            "one or more build inputs cannot be substituted or built.  "
            "This is a hard gap: the build_deps feature cannot pre-load "
            "paths that are neither in the store nor substitutable.  "
            f"First 5 paths: {', '.join(input_outpaths[:5])}"
            + (" …" if len(input_outpaths) > 5 else "")
            + (f"\nnix-store stderr: {failed_hint}" if failed_hint else "")
        )
    _log.info(
        "_produce_build_deps_archive: all %d input outpath(s) realised",
        len(input_outpaths),
    )

    # 4. Compute the toolchain closure to subtract (paths already shipped
    #    by toolchains.common.archive + toolchains.<id>.out.archive).
    #    Toolchain outpaths are embedded in the variant specs as
    #    ``toolchain_outpath``; use them as seeds for
    #    collect_toolchain_archive_paths.
    _log.info(
        "_produce_build_deps_archive: computing toolchain closure to subtract"
    )
    tc_subtract: frozenset[str] = frozenset()
    tc_outpaths_from_lookups: set[str] = set()
    for binary_lookup in variant_lookups.values():
        for spec in binary_lookup.values():
            tc_op = spec.get("toolchain_outpath") if isinstance(spec, dict) else None
            if tc_op and isinstance(tc_op, str):
                tc_outpaths_from_lookups.add(tc_op)

    if tc_outpaths_from_lookups:
        tc_subtract = _preflight.collect_toolchain_archive_paths(
            list(tc_outpaths_from_lookups),
            run_subprocess=runner,
        )
        _log.info(
            "_produce_build_deps_archive: toolchain closure has %d paths "
            "(to subtract from build_deps)",
            len(tc_subtract),
        )
    else:
        _log.warning(
            "_produce_build_deps_archive: no toolchain outpaths found in "
            "variant_lookups; build_deps archive will include toolchain paths"
        )

    # 5. Compute build_deps closure = requisites(input_outpaths) − toolchain.
    build_deps_paths, uncovered = _preflight.compute_build_deps_closure(
        input_outpaths,
        toolchain_paths_to_subtract=tc_subtract,
        run_subprocess=runner,
    )

    # COMPLETENESS GATE (fail-closed): any uncovered input outpath would
    # still be substituted at build time — that is EXACTLY the problem this
    # feature prevents.  Abort with a clear error listing the offenders.
    if uncovered:
        raise RuntimeError(
            "_produce_build_deps_archive: COMPLETENESS GATE FAILED — "
            "%d input outpath(s) not found in local store requisites; "
            "they would still be substituted at build time even with "
            "build_deps_local=True.  Offending paths (first 10): %s"
            % (
                len(uncovered),
                ", ".join(uncovered[:10])
                + (" …" if len(uncovered) > 10 else ""),
            )
        )

    if not build_deps_paths:
        _log.info(
            "_produce_build_deps_archive: build_deps closure is empty after "
            "subtracting toolchain paths — all build inputs already covered "
            "by toolchain archives; skipping archive production"
        )
        return

    # 6. Export build_deps.out.archive atomically.
    _log.info(
        "_produce_build_deps_archive: exporting %d build-deps paths "
        "to %s/%s",
        len(build_deps_paths), matrix_eval_out_dir,
        _preflight.BUILD_DEPS_ARCHIVE_NAME,
    )
    archive_path = _preflight.export_build_deps_archive(
        build_deps_paths,
        matrix_eval_out_dir,
        run_subprocess=runner,
    )
    _log.info(
        "_produce_build_deps_archive: wrote %s (%d bytes)",
        archive_path, archive_path.stat().st_size,
    )

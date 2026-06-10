"""The single ``TaskDefinition`` that orchestrates the compiler-suit run.

The dynamic_runner framework exposes phases as first-class
:class:`PhaseSpec` (with declared dependencies) and per-phase task
types as :class:`TaskTypeSpec`. :class:`SuitTask` implements that
Protocol under the new phase taxonomy:

* ``matrix_eval`` (phase 2 in the plan) — distributed eval workers, one
  task per binary; produces ``matrix-<binary>.drv.archive`` per-binary
  closures plus the kept-drv set the dependency_graph worker walks.
* ``build_compilers`` (phase 1, optional) — distributed cross-toolchain
  build workers; one task per (arch, compiler). PhaseSpec is always
  registered so the framework's dispatch surface is uniform; the CLI
  gate (``--build-compilers``) decides whether any manifests are
  actually emitted.
* ``dependency_graph`` (phase 3) — a SINGLE framework task
  (``task_id="dependency_graph"``) over ALL binaries;
  ``task_depends_on`` is every phase-2 matrix_eval task (whose
  task_id is the bare binary name — phase-local, the MATRIX_EVAL
  phase disambiguates), so the CRDT activates it only after every
  matrix_eval has applied. The worker is invoked ONCE: it gathers
  each binary's matrix_aggregate drv from the corresponding
  matrix_eval predecessor's keyed outputs (``task.
  predecessor_outputs[<binary>]["matrix_aggregate_drv"]["value"]``,
  published by the upstream matrix_eval task via
  ``Task.publish_string``),
  imports every per-binary matrix-aggregate drv archive, and runs the
  streaming planner ONCE over the combined tree, producing one
  descriptor list covering all binaries. matrix_eval and
  dependency_graph are JSON-free: their TaskInfos are built in-memory
  by :meth:`discover_items` from ``config.per_binary_metadata`` — no
  manifest written, none read.
* ``build`` (phase 4) — distributed ``build_common_dep`` +
  ``build_variant`` workers; ``toolchain_validate`` shares the same
  dispatch (rarely emitted, gated by ``--debug-testbuild``). The build
  phase is spawned at runtime by the primary from the descriptor
  batches the single dependency_graph task STREAMS as custom messages
  (:mod:`compiler_suit_runner.streamed_spawn`): the worker's messages
  reach the secondary's :meth:`SuitTask.worker_message_listener`,
  which forwards them verbatim to the primary as IMPORTANT messages;
  the primary's :meth:`SuitTask.custom_message_handler` decodes each
  batch and spawns it incrementally, and
  :meth:`SuitTask.on_phase_end("dependency_graph")` is a pure
  reconciliation barrier against the worker's terminal summary.

Responsibilities:

1. **Topology** (:meth:`get_phases`) declares the ``matrix_eval``,
   ``build_compilers``, ``dependency_graph`` and ``build`` framework
   phases. All four are first-class PhaseSpecs; the dispatch graph
   between them is encoded as ``depends_on`` tuples so the framework
   schedules them in topological order.
2. **Item discovery** (:meth:`discover_items`) builds the phase-2
   (matrix_eval) + phase-3 (dependency_graph) TaskInfos DIRECTLY
   in-memory from ``config.per_binary_metadata`` (JSON-free) and
   additionally scans the manifest directory written by
   :mod:`compiler_suit_runner.manifest_gen` for the build-shaped
   phases, classifying each by ``item_class`` to
   ``(phase_id, type_id, affinity_id)``.
3. **Per-type plumbing** (:meth:`estimate_memory`,
   :meth:`build_worker_command_args`,
   :meth:`get_output_filename_pattern`) wires manifests onto the
   subprocess workers.
4. **Lifecycle hooks** (:meth:`on_run_start`, :meth:`on_run_end`,
   :meth:`on_phase_start`, :meth:`on_phase_end`) own setup / teardown
   of the peer-cache machinery (signing key, peer announcement,
   :class:`PeerListWatcher`, optional Cachix uploader, optional
   harmonia subprocess).

The legacy in-process dispatch surface (``find_binaries`` /
``dispatch_binary`` / private worker-routing) is preserved as an
escape hatch the single-process CLI still uses for tests; new
deployments go through the framework's worker protocol.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import threading
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any, Optional

from compiler_suit_runner.cachix_uploader import (
    CachixUploader,
    UploaderConfig,
)
from compiler_suit_runner.holding_matcher import (
    UNFULFILLABLE_REASON_TEMPLATE,
    matcher as fulfillability_matcher,
)
from compiler_suit_runner.manifest_gen import (
    DEPENDENCY_GRAPH_TASK_ID,
    ManifestHeader,
    Phase,
    make_matrix_eval_header,
    matrix_eval_task_id,
    read_manifest,
)
from compiler_suit_runner.peer_cache import (
    SUBSTITUTERS_FILENAME,
    substituters_filename_for,
    HarmoniaProcess,
    PathPlacementWatcher,
    PeerInfo,
    PeerListWatcher,
    SigningKey,
    announce_self,
    generate_signing_key,
    withdraw_self,
)
from compiler_suit_runner import peer_paths
from compiler_suit_runner.peer_push import (
    PUSH_PORT_OFFSET,
    PeerPushServer,
    fan_out_announce,
    fan_out_withdraw,
    push_port_for,
)
from compiler_suit_runner.peer_replication import (
    DEFAULT_REPLICATION_K,
    BroadcastReceiver,
    ReplicationContext,
    ReplicationReceiver,
    ReplicationRepairWorker,
    ReplicationSender,
)
from compiler_suit_runner import peer_paths_fetch
from compiler_suit_runner.workers.build_worker import (
    BuildWorkerEnv,
    build_worker,
)


__all__ = [
    "SuitTaskConfig",
    "SuitTask",
    "UNFULFILLABLE_REASON_TEMPLATE",
    "fulfillability_matcher",
]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _push_url_to_substituter_url(push_url: str) -> Optional[str]:
    """Translate ``http://host:<push_port>`` → ``http://host:<harmonia_port>``.

    The broadcast protocol gossips push URLs (port = harmonia_port +
    PUSH_PORT_OFFSET) because that's the port the receiver listens on
    for the ``/peer/path-broadcast-offer`` POST. The fetch side, in
    contrast, needs the harmonia substituter URL. Both run on the
    same host, so we just subtract PUSH_PORT_OFFSET from the port.

    Returns ``None`` if the URL doesn't parse as ``http://host:port``
    or the port-arithmetic produces a non-positive number; callers
    fall back to using the raw URL (which ``nix copy --from`` will
    reject, returning False — the safe default).
    """
    if not push_url or not push_url.startswith(("http://", "https://")):
        return None
    try:
        scheme, _, rest = push_url.partition("://")
        # rest looks like ``host:port[/...]``; we don't preserve any
        # path suffix because nix copy --from is base-URL only.
        host_port, _, _ = rest.partition("/")
        host, _, port_s = host_port.rpartition(":")
        if not host or not port_s:
            return None
        push_port = int(port_s)
    except (TypeError, ValueError):
        return None
    harmonia_port = push_port - PUSH_PORT_OFFSET
    if harmonia_port <= 0:
        return None
    return f"{scheme}://{host}:{harmonia_port}"


# Item classes that route through the build worker (post-rename).
_BUILD_DISPATCH_CLASSES: frozenset[str] = frozenset(
    {"toolchain_validate", "build_common_dep", "build_variant"}
)


def _classify(header: ManifestHeader) -> tuple[str, str, Optional[str]]:
    """Map a :class:`ManifestHeader` to ``(phase_id, type_id, affinity_id)``.

    The mapping is the single source of truth for which dynamic_runner
    phase / type / affinity bucket each consumer ``item_class`` lands
    in. ``affinity_id`` is non-None for the compiler-bound types
    (``build_compilers``, ``toolchain_validate`` and ``build_variant``)
    so the framework co-locates a compiler build and the variants that
    depend on it onto the same worker for kernel page-cache reuse.
    """
    item_class = header.item_class
    if item_class == "matrix_eval":
        # Distributed eval: one task per binary. Pinned by binary so
        # we get a stable affinity bucket per package (handy for log
        # grepping; framework just treats it as a tag).
        binary = header.payload.get("binary", "?")
        return (Phase.MATRIX_EVAL, "eval", binary)
    if item_class == "dependency_graph":
        # Exactly ONE all-binaries dependency_graph task; it depends on
        # every phase-2 matrix_eval task (one per binary). The worker is
        # invoked once over all binaries: it imports every
        # matrix-<binary> archive, gathers each binary's
        # matrix_aggregate_drv from the corresponding matrix_eval
        # predecessor's keyed outputs (the predecessor task_id IS the
        # bare binary, so ``task.predecessor_outputs[<binary>]
        # ["matrix_aggregate_drv"]["value"]``, published via
        # ``Task.publish_string``), and runs the streaming planner ONCE
        # over the whole run. There is no single binary, so
        # ``affinity_id`` is None.
        return (Phase.DEPENDENCY_GRAPH, "dep_graph", None)
    if item_class == "build_compilers":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return (Phase.BUILD_COMPILERS, "build_compilers", f"{compiler}-{arch}")
    # All remaining build-shaped tasks share the single ``build``
    # phase. Nix's daemon serializes shared dependencies via its
    # build lock, so toolchain validates / common deps land before
    # their dependent variants without explicit per-class phase
    # ordering.
    if item_class == "toolchain_validate":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return (Phase.BUILD, "toolchain_validate", f"{compiler}-{arch}")
    if item_class == "build_common_dep":
        return (Phase.BUILD, "common_dep", None)
    if item_class == "build_variant":
        # Treat an empty compiler_id the same as a missing one so the
        # affinity bucket never degrades to a leading-dash form like
        # ``"-aarch64"`` (which mis-buckets every compiler-less variant
        # together and breaks page-cache co-location with its toolchain).
        compiler = header.payload.get("compiler_id") or "?"
        arch = header.payload.get("arch") or "?"
        return (Phase.BUILD, "variant", f"{compiler}-{arch}")
    raise ValueError(f"unknown item_class {item_class!r}")


def _make_task_info(
    path: pathlib.Path,
    size: int,
    *,
    phase_id: str,
    type_id: str,
    affinity_id: Optional[str],
    payload: dict,
    task_id: str = "",
    task_depends_on: tuple[str, ...] = (),
):
    """Return a framework-compatible :class:`TaskInfo`, falling back to a stub.

    When :mod:`dynamic_runner` is importable we use the real
    :class:`TaskInfo`; otherwise we synthesise an attribute-compatible
    stub via :class:`types.SimpleNamespace` so unit tests run without
    the framework on ``sys.path``.

    ``task_id`` + ``task_depends_on`` are populated when the manifest
    has them (Phase 1 of the framework's task-deps API,
    ``a1ebbaa``); empty defaults so legacy manifests / older
    framework builds round-trip cleanly. Entries may be bare strings
    (intra-phase) or :class:`TaskDep` (cross-phase, phase-tagged by the
    caller that knows the prerequisite's phase).
    """
    try:
        from dynamic_runner._shared import (  # type: ignore[import-not-found]
            BinaryIdentifier,
            TaskInfo,
        )
    except Exception:  # noqa: BLE001 — framework absent
        return SimpleNamespace(
            path=path,
            size=size,
            phase_id=phase_id,
            type_id=type_id,
            affinity_id=affinity_id,
            payload=payload,
            task_id=task_id,
            task_depends_on=task_depends_on,
        )

    identifier = BinaryIdentifier(
        binary_name=path.name,
        platform="manifest",
        compiler="manifest",
        version="0",
        opt_level="manifest",
    )
    # ``TaskInfo`` gained ``task_id`` + ``task_depends_on`` in
    # framework commit a1ebbaa. Older framework builds don't have
    # the kwargs; fall back to constructing without them so a stale
    # pin doesn't fail-import.
    try:
        return TaskInfo(
            path=path,
            size=size,
            identifier=identifier,
            phase_id=phase_id,
            type_id=type_id,
            affinity_id=affinity_id,
            payload=dict(payload),
            task_id=task_id,
            task_depends_on=task_depends_on,
        )
    except TypeError:
        return TaskInfo(
            path=path,
            size=size,
            identifier=identifier,
            phase_id=phase_id,
            type_id=type_id,
            affinity_id=affinity_id,
            payload=dict(payload),
        )


def _make_task_dep(task_id: str, *, phase_id: str = "", inherit_outputs: bool = False):
    """Return a framework :class:`TaskDep`, or an attribute-compatible stub.

    A dependency's full identity is ``(phase_id, task_id)``. At the
    framework's PyO3 boundary a BARE-STRING dep resolves to the
    ENCLOSING task's phase — correct only for an intra-phase
    prerequisite. A CROSS-phase prerequisite must name its phase
    explicitly via ``TaskDep(phase_id=...)`` or the primary marks the
    declaring task ``invalid_task`` ("missing dep") at ingest (e.g. the
    all-binaries ``dependency_graph`` task depending on each MATRIX_EVAL
    ``<binary>`` task, or a ``build_variant`` in BUILD depending on its
    BUILD_COMPILERS ``<sys>__<arch>__<comp>`` toolchain). The dependent
    task then never runs and its downstream phase spawns ZERO tasks.

    Falls back to a :class:`types.SimpleNamespace` stub when
    :mod:`dynamic_runner` is absent so unit tests run without the
    framework on ``sys.path`` (mirrors :func:`_make_task_info`).
    """
    try:
        from dynamic_runner._shared import TaskDep  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — framework absent
        return SimpleNamespace(
            task_id=task_id,
            phase_id=phase_id,
            inherit_outputs=inherit_outputs,
        )
    # ``TaskDep`` gained the cross-phase ``phase_id`` kwarg in the
    # primary-coordinator-unification. Older framework pins reject it;
    # fall back to an attribute-compatible stub carrying the phase so
    # consumers (and unit tests) still see it. The live dispatch runs
    # the current wheel where the real TaskDep supports phase_id. Mirrors
    # the ``except TypeError`` forward-compat fallback in
    # :func:`_make_task_info`.
    try:
        return TaskDep(
            task_id=task_id,
            phase_id=phase_id,
            inherit_outputs=inherit_outputs,
        )
    except TypeError:
        return SimpleNamespace(
            task_id=task_id,
            phase_id=phase_id,
            inherit_outputs=inherit_outputs,
        )


def _phase_specs(*, build_max_concurrent: Optional[int]):
    """Build the framework :class:`PhaseSpec` tuple for this run.

    Implemented as a function (rather than a module-level constant) so
    importing this module never fails when ``dynamic_runner`` is
    absent — the framework's :class:`PhaseSpec` and :class:`TaskTypeSpec`
    are imported lazily here and only matter at run time.

    ``build_max_concurrent`` (when set) caps the global in-flight count
    for the build-heavy types (build_compilers, common_dep, variant).
    ``None`` leaves all types unconstrained.

    The phase graph declared here:

    * ``build_compilers`` — distributed cross-toolchain build workers.
      Always registered; emits zero tasks unless ``--build-compilers``
      gates manifests at submit time.
    * ``matrix_eval`` — distributed nix-eval-jobs workers; depends on
      ``build_compilers`` so toolchain outputs are available before
      eval walks the dataset attrs.
    * ``build`` — distributed common_dep + variant workers (plus the
      rarely-emitted toolchain_validate type); depends on
      ``matrix_eval`` because the kept-drv set + dependency_graph plan
      come out of that phase.

    The ``dependency_graph`` step (phase 3 in the plan) runs as a
    first-class framework PhaseSpec task dispatched by the runner; it
    plans the phase-4 descriptor list and STREAMS it as
    :mod:`compiler_suit_runner.streamed_spawn` batch messages while it
    plans. Each batch is relayed (secondary →) primary where
    :meth:`SuitTask.custom_message_handler` translates descriptors to
    :class:`ManifestHeader` instances via
    :func:`dependency_graph_planner.headers_from_descriptors`,
    converts each to a framework ``TaskInfo`` via
    :func:`_header_to_task_info`, and hands them to
    ``primary_handle.spawn_tasks`` for the ``build`` phase;
    :meth:`SuitTask.on_phase_end` only reconciles the spawned count
    against the worker's terminal summary. The spawn fan-out stays
    primary-affined because "task creation can only be done by the
    manager (primary)" is still a framework invariant.
    """
    from dynamic_runner.task_protocol import (  # type: ignore[import-not-found]
        PhaseSpec,
        TaskTypeSpec,
    )

    # Memory budgeting is disabled for this task: every TaskTypeSpec
    # uses the default ``estimator_attr = "estimate_memory"`` which
    # resolves to :meth:`SuitTask.estimate_memory` and returns a
    # 1-byte constant. The framework's resource scheduler then treats
    # all items as zero-cost; concurrency is bounded by ``--jobs N``
    # plus the optional per-type ``max_concurrent`` cap below.
    build_kwargs: dict = {}
    if build_max_concurrent is not None:
        build_kwargs["max_concurrent"] = build_max_concurrent

    # ``toolchain_validate`` is uncapped: the work is a path-info
    # probe + at most one ``nix copy`` per item, so the build-heavy
    # cap (which targets nix-build oversubscription) doesn't apply.
    # Keeping it uncapped also avoids starving phase-4 variants
    # behind the validate phase when the same cap is configured low
    # for compile-throttling.
    return (
        PhaseSpec(
            phase_id=Phase.BUILD_COMPILERS,
            types=(
                TaskTypeSpec(
                    type_id="build_compilers",
                    # Unified entry: the framework picks ONE
                    # ``worker_module`` for the whole secondary pool
                    # (the first registered one wins), so every task
                    # type funnels through ``build_worker.main``.
                    # Its ``handle`` closure sniffs ``task.payload``
                    # and dispatches matrix_eval to the eval worker,
                    # build_compilers to the build_compilers worker,
                    # and everything else to the build path.
                    worker_module="compiler_suit_runner.workers.build_worker",
                    **build_kwargs,
                ),
            ),
        ),
        PhaseSpec(
            phase_id=Phase.MATRIX_EVAL,
            depends_on=(Phase.BUILD_COMPILERS,),
            types=(
                TaskTypeSpec(
                    type_id="eval",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id=Phase.DEPENDENCY_GRAPH,
            depends_on=(Phase.MATRIX_EVAL,),
            types=(
                TaskTypeSpec(
                    type_id="dep_graph",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id=Phase.BUILD,
            depends_on=(Phase.DEPENDENCY_GRAPH,),
            types=(
                TaskTypeSpec(
                    type_id="toolchain_validate",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
                TaskTypeSpec(
                    type_id="common_dep",
                    worker_module="compiler_suit_runner.workers.build_worker",
                    **build_kwargs,
                ),
                TaskTypeSpec(
                    type_id="variant",
                    worker_module="compiler_suit_runner.workers.build_worker",
                    **build_kwargs,
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Q4 peer-lifecycle listener
# ---------------------------------------------------------------------------


class _PeerLifecycleListener:
    """Q4 peer-lifecycle bridge: framework → consumer.

    Constructed once in :meth:`SuitTask.on_run_start` and threaded to the
    ``RustPrimaryCoordinator(..., peer_lifecycle_listener=)`` kwarg. The
    framework calls the listener's ``on_peer_added`` /
    ``on_peer_removed`` methods on its own dispatch thread; we route
    each event into the appropriate consumer subsystem (placement
    gossip for observer additions, :class:`ReplicationRepairWorker` for
    removals) and swallow any exception so the framework does not
    disable the listener on a single hiccup.

    Both methods are duck-typed by the framework: if a method is
    missing, the framework logs + skips the event. We define both so
    the framework can deliver both shapes; either swallows on listener
    error.

    ``on_peer_added(secondary_id, is_observer)``:

    * ``is_observer=True``: the joiner is a late-attaching observer
      that has announced toolchain holdings. We mirror those holdings
      into the placement gossip so the local
      :class:`PathPlacementWatcher` snapshot reflects the observer's
      contribution. Observer's holdings are delivered via the
      framework's observer-late-joiner channel; today the placement
      gossip plumbing is the existing
      :func:`peer_paths.record_self_has` path, scoped by observer id.
    * ``is_observer=False``: NoOp. The existing K=3 peer-set watcher
      already handles regular-secondary additions for cascade.

    ``on_peer_removed(secondary_id, cause)``:

    * ``cause`` is a dict ``{"kind": str, "reason": str | None}``.
      Possible kinds: ``"keepalive_miss"`` (a sustained ~2 min outage),
      ``"fatal_error"``.
    * Every cause kind triggers a standard
      :meth:`ReplicationRepairWorker.on_peer_removed` repair sweep —
      the cause does not change the repair logic.
    * For ``"fatal_error"``, the ``cause['reason']`` string is woven
      into any subsequent ``fail_permanent`` reason via the repair
      worker's existing
      :meth:`ReplicationRepairWorker._maybe_mark_unfulfillable` path
      (best-effort: when ``mark_task_unfulfillable`` is bound the
      reason carries forward).
    """

    def __init__(
        self,
        *,
        repair_worker: Optional[Any],
        placement_record_observer_callable: Optional[
            Callable[[str, str], None]
        ] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._repair = repair_worker
        self._record_observer = placement_record_observer_callable
        self._logger = logger or logging.getLogger(__name__)
        # Track which secondaries the framework last reported as
        # observers so on_peer_removed can route observer-side
        # cleanup if needed. Defensive: a removed peer the framework
        # never announced is still routed through the repair worker.
        self._observer_peers: set[str] = set()
        self._lock = threading.Lock()

    # ── Framework entry points ─────────────────────────────────────────

    def on_peer_added(
        self,
        secondary_id: str,
        is_observer: bool,
    ) -> None:
        """Handle a peer-added notification."""
        try:
            if not secondary_id:
                return
            if not is_observer:
                # Regular secondary additions are already handled by
                # the existing K=3 peer-set watcher for cascade. We
                # don't double-fire here.
                self._logger.debug(
                    "_PeerLifecycleListener: peer added %s"
                    " (non-observer); cascade owned by peer-set watcher",
                    secondary_id,
                )
                return
            with self._lock:
                self._observer_peers.add(secondary_id)
            self._logger.info(
                "_PeerLifecycleListener: observer peer added %s;"
                " registering holdings in placement gossip",
                secondary_id,
            )
            if self._record_observer is not None:
                try:
                    self._record_observer(secondary_id, "")
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "_PeerLifecycleListener: observer placement"
                        " registration raised for %s",
                        secondary_id,
                    )
        except Exception:  # noqa: BLE001 — never raise out
            self._logger.exception(
                "_PeerLifecycleListener.on_peer_added swallowed"
                " unexpected exception"
            )

    def on_peer_removed(
        self,
        secondary_id: str,
        cause: dict,
    ) -> None:
        """Handle a peer-removed notification."""
        try:
            if not secondary_id:
                return
            kind = ""
            reason: Optional[str] = None
            if isinstance(cause, dict):
                kind = str(cause.get("kind") or "")
                raw_reason = cause.get("reason")
                if isinstance(raw_reason, str):
                    reason = raw_reason
            with self._lock:
                self._observer_peers.discard(secondary_id)
            self._logger.info(
                "_PeerLifecycleListener: peer removed %s (kind=%s,"
                " reason=%s); routing to repair worker",
                secondary_id, kind, reason,
            )
            if self._repair is None:
                return
            # The repair worker's on_peer_removed accepts a free-form
            # reason string. For fatal_error we forward the wire
            # reason so any downstream fail_permanent reason can pick
            # it up via the repair path.
            forwarded_reason = (
                reason if (kind == "fatal_error" and reason) else kind
            )
            try:
                self._repair.on_peer_removed(
                    secondary_id, forwarded_reason or "",
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "_PeerLifecycleListener: repair worker"
                    " on_peer_removed raised for %s",
                    secondary_id,
                )
        except Exception:  # noqa: BLE001 — never raise out
            self._logger.exception(
                "_PeerLifecycleListener.on_peer_removed swallowed"
                " unexpected exception"
            )


# ---------------------------------------------------------------------------
# dependency_graph → build header conversion
# ---------------------------------------------------------------------------


def _header_depends_on(header: ManifestHeader) -> tuple:
    """Assemble a header's full ``task_depends_on`` tuple.

    Intra-phase deps in ``header.task_depends_on`` pass through as bare
    strings (the framework resolves them to the enclosing phase).
    Cross-phase toolchain ids in ``header.build_compilers_depends_on``
    are each wrapped in a phase-tagged ``TaskDep`` so they resolve to
    :attr:`Phase.BUILD_COMPILERS` rather than the declaring task's own
    phase. Shared by the spawn-side :func:`_header_to_task_info` and the
    in-memory :meth:`SuitTask._task_info_from_header`.
    """
    toolchain_deps = tuple(
        _make_task_dep(tc_id, phase_id=Phase.BUILD_COMPILERS)
        for tc_id in header.build_compilers_depends_on
    )
    return tuple(header.task_depends_on) + toolchain_deps


def _header_to_task_info(header: ManifestHeader, *, disable_task_deps: bool = False):
    """Convert one :class:`ManifestHeader` directly into a framework
    ``TaskInfo``. Mirrors the call shape used by
    :func:`_make_task_info` (the disk-round-trip path used by
    :meth:`SuitTask.discover_items`) so spawn-side and discover-side
    items differ only by source, not by encoding.

    The ``payload`` carried on the resulting TaskInfo is the same
    header_dict shape ``discover_items`` emits, so downstream
    workers see a uniform payload regardless of whether the item
    came from preflight or from Phase 1 planning.

    Cross-phase toolchain deps (``build_compilers_depends_on``) are
    phase-tagged via :func:`_header_depends_on`; this is the live
    spawn path (``on_phase_end`` → ``headers_from_descriptors``) that
    emits each ``build_variant``'s BUILD_COMPILERS toolchain edge. When
    ``disable_task_deps`` is set the dep tuple is dropped entirely —
    parity with the disk loop and :meth:`SuitTask._task_info_from_header`
    (the caller passes ``self.config.disable_task_deps``).
    """
    phase_id, type_id, affinity_id = _classify(header)
    header_dict = {
        "item_class": header.item_class,
        "name": header.name,
        "size": header.size,
        "payload": dict(header.payload),
    }
    # ManifestHeader carries a synthetic "path" only by name; the
    # framework treats path as an opaque tag for non-file-based
    # tasks (SuitTask.uses_file_based_items = False), so we just
    # use the header name as the path component.
    return _make_task_info(
        pathlib.Path(f"{header.name}.json"),
        header.size,
        phase_id=phase_id,
        type_id=type_id,
        affinity_id=affinity_id,
        payload=header_dict,
        task_id=header.task_id or "",
        task_depends_on=() if disable_task_deps else _header_depends_on(header),
    )


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SuitTaskConfig:
    """Static, frozen configuration for one runner invocation.

    The CLI builds this from argparse output; tests build it directly.
    All filesystem paths are absolute; relative paths would break the
    secondary's working-directory assumptions.
    """

    flake_ref: str
    sys_name: str
    shared_fs: pathlib.Path
    manifest_dir: pathlib.Path
    dataset_dir: pathlib.Path
    peers_dir: pathlib.Path
    run_id: str
    secondary_id: str
    hostname: str
    harmonia_port: int = 5000
    enable_harmonia: bool = True
    cachix_cache: Optional[str] = None
    cachix_token_file: Optional[pathlib.Path] = None
    # Opt-in: also spawn sshd in each container so an operator can
    # ssh in for live debugging while compilation work is running.
    # See :mod:`compiler_suit_runner.ssh_debug`. Off by default —
    # the auth surface (a sshd on a high port + the baked
    # authorized_keys) is opt-in even though the image always ships
    # the bits.
    enable_ssh_debug: bool = False
    ssh_debug_port: int = 22222
    # Global concurrency cap on the three build-heavy task types
    # (toolchain, common_dep, variant). ``None`` = unconstrained;
    # a positive int caps in-flight items of those types across the
    # whole cluster. Useful for compile-throttling: each variant
    # build forks ``nix build`` which itself spawns N parallel
    # compiler invocations, so unbounded concurrency × workers
    # quickly oversubscribes the underlying secondaries.
    build_max_concurrent: Optional[int] = None
    # Workaround flag: when True, ``discover_items`` strips
    # ``task_depends_on`` from every emitted :class:`TaskInfo`. Set this
    # to bypass a dynamic_runner post-promotion bug where the secondary
    # rebuilds its PendingPool from cluster_state after the primary
    # promotes it but never seeds in-flight task_ids into the
    # extend()-validator's ``known`` set, causing UnknownTaskDep on any
    # variant whose toolchain dep is currently dispatched. Safe because
    # nix's drv graph + harmonia federation handle toolchain-before-
    # variant ordering at the daemon build-lock level — we never needed
    # framework-level deps for correctness, they were belt-and-suspenders.
    disable_task_deps: bool = False
    # Policy flag for remote toolchain builds. Default ``False`` means
    # secondaries only VALIDATE toolchains (path-info + targeted
    # ``nix copy --from`` against the placement map), never build them
    # — missing toolchains have to be realised on the primary before
    # dispatch and surfaced as a :class:`PreflightError`. Flipping to
    # ``True`` restores the legacy behaviour (every secondary may
    # rebuild any toolchain whose outputs aren't in its local store);
    # use this only when the primary's local store is a stale cache
    # and the slowdown of remote toolchain builds is acceptable.
    allow_toolchain_build: bool = False
    # K=3 toolchain replication knobs. ``replication_k`` is the
    # target holder count cluster-wide; default 3. Set to 1 to
    # disable the K=3 cascade (every toolchain held by only its
    # initial fetcher); set to 0 to disable replication entirely
    # (no cascade, no repair). ``allow_observer_as_holder`` controls
    # whether a late-attaching observer counts toward K when wired
    # (Framework Ask #3); without observer support the flag has no
    # effect.
    replication_k: int = 3
    allow_observer_as_holder: bool = True
    # Per-task budget for the framework's auto-reinject of permanently
    # Unfulfillable tasks (triggered by ``HoldingMatcher`` and by
    # explicit ``PrimaryHandle.reinject_task`` calls). ``None`` =
    # unbounded; matches the framework default. A non-negative int
    # caps how many reinjects a single task hash may absorb before
    # the framework refuses further reinjects on that task. Kept
    # distinct from ``--retry-max-passes`` (the Recoverable-failure
    # retry budget); see ``feedback_state_machine_semantics.md``.
    unfulfillable_reinject_max_per_task: Optional[int] = None
    # Shared bind-mounted path where matrix_eval workers write their
    # per-binary ``matrix-<binary>.drv.archive`` closures. The archive
    # is the watcher's quiesce signal; there is no resume short-
    # circuit (in-run second attempts are failure restarts where the
    # cached on-disk archive cannot be trusted, so the worker always
    # re-runs). Submitter side: ``<shared_fs>/dataset/_matrix_eval``;
    # secondary container side: ``/app/out-network/_matrix_eval``
    # (same physical dir via the framework's --output bind mount).
    matrix_eval_out_dir: Optional[pathlib.Path] = None

    # SSH gateway URL (``ssh://user@host[:port]``) used by the
    # matrix_eval quiesce watcher to mirror per-binary outputs back
    # from ``<slurm_root_folder>/out/`` into the submitter-local
    # ``<dataset_dir>/_matrix_eval/`` between matrix_eval-quiesce and
    # phase-3 spawn. ``None`` (the default) means the watcher skips
    # the mirror step — useful for in-process tests, local single-host
    # smokes, and the legacy submitter-side-only flow.
    gateway_url: Optional[str] = None
    # Gateway-side root containing ``out/``, ``image_bin/``, ``log/``
    # subdirs. Required (together with ``gateway_url``) for the
    # matrix_eval output-mirror step. CLI populates from
    # ``--slurm-root-folder``; tests leave ``None``.
    slurm_root_folder: Optional[str] = None

    # Per-binary preflight metadata for the JSON-free phase-2
    # (matrix_eval) and phase-3 (dependency_graph) TaskInfos. Maps each
    # binary name to a metadata dict of shape::
    #
    #     {
    #         "archs": ["x86_64", "aarch64", ...],
    #         "toolchain_aggregate_drv": "/nix/store/...-toolchains.drv",
    #         "variant_sample": 64,    # optional
    #         "variant_seed": "...",   # optional
    #     }
    #
    # This is the shape :func:`compiler_suit_runner.preflight
    # .enumerate_variants` produces (post-flattening in cli.py). It is
    # threaded onto the config at submit time so
    # :meth:`SuitTask.discover_items` can build the matrix_eval (one
    # TaskInfo per binary) and the single dependency_graph TaskInfo
    # DIRECTLY in-memory — no JSON manifest written, none read. ``None``
    # / empty means no phase-2/3 tasks are discovered (e.g. the
    # secondary-container config, where discover_items is never called).
    per_binary_metadata: Optional[dict[str, dict]] = None


# ---------------------------------------------------------------------------
# SuitTask
# ---------------------------------------------------------------------------


class SuitTask:
    """The single dynamic_runner :class:`TaskDefinition` for the run."""

    # Items are JSON manifests on the shared FS — workers resolve
    # ``TaskInfo.path`` themselves against ``config.manifest_dir`` and
    # the framework should NOT stat / hash / stage them. The path
    # propagates to the worker as an opaque identifier over the comm
    # fd. Skips the primary-side ``queue_initial_staging`` content-hash
    # pass entirely (was 30+ min on a full matrix dispatch).
    uses_file_based_items: bool = False

    def __init__(
        self,
        config: SuitTaskConfig,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._logger = logger or logging.getLogger(__name__)

        # Peer-cache state, lazily populated by ``on_run_start``.
        self._signing_key: Optional[SigningKey] = None
        self._peer_watcher: Optional[PeerListWatcher] = None
        self._peer_nix_conf_watcher: Optional[Any] = None
        self._placement_watcher: Optional[PathPlacementWatcher] = None
        self._cachix_uploader: Optional[CachixUploader] = None
        self._harmonia: Optional[HarmoniaProcess] = None
        self._push_server: Optional[PeerPushServer] = None
        # K=3 replication coordination (consumer-side; not a framework
        # task — runs parallel-async to the worker pool).
        self._replication_sender: Optional[ReplicationSender] = None
        self._replication_receiver: Optional[ReplicationReceiver] = None
        self._replication_repair: Optional[ReplicationRepairWorker] = None
        # matrix_eval drv broadcast consumer (mirrors ReplicationReceiver
        # but for the all-peers flood-fill protocol). Without this the
        # ``/peer/path-broadcast-offer`` endpoint sees deduped offers
        # land on a no-op callback that returns False, so the cluster
        # never actually fetches the broadcast drvs.
        self._broadcast_receiver: Optional[BroadcastReceiver] = None
        # Q4 peer-lifecycle listener (constructed by on_run_start).
        # Held on ``self`` so callers wiring it onto the framework's
        # ``RustPrimaryCoordinator(..., peer_lifecycle_listener=)``
        # kwarg can fetch it after on_run_start returns.
        self._peer_lifecycle_listener: Optional[_PeerLifecycleListener] = None
        # Q3 fulfillability matcher: a module-level callable, surfaced
        # as an attribute so callers wiring the
        # ``RustPrimaryCoordinator(..., fulfillability_matcher=)`` kwarg
        # don't need to re-import.
        self._fulfillability_matcher: Optional[Callable[..., bool]] = (
            fulfillability_matcher
        )
        # Q1 outpath → task_hash_hex lookup. Built at on_run_start by
        # scanning toolchain manifest headers (each carries an
        # ``outpath`` payload field set by preflight + the task_id
        # from manifest_gen). The framework's
        # ``ReplicationContext.lookup_task_hash_for_outpath`` callable
        # binds to this dict's ``.get`` so a missing outpath returns
        # None and the matcher / repair worker degrade safely.
        self._outpath_to_task_hash: dict[str, str] = {}
        # Mutable handle to the framework's ``PrimaryHandle`` set by
        # :meth:`wire_primary_handle`. ``ReplicationContext`` callables
        # built in :meth:`on_run_start` close over ``self`` and route
        # through this attribute on every invocation; until the caller
        # binds it the wrappers log + degrade to the legacy NFS-poll
        # fallback path.
        self._primary_handle: Optional[Any] = None
        # Streamed dependency_graph → build spawn bookkeeping
        # (primary-local; see custom_message_handler / on_phase_end).
        # ``_streamed_spawned_count`` accumulates the TaskInfos handed
        # to ``spawn_tasks`` across spawn_batch messages;
        # ``_streamed_expected_total`` / ``_streamed_summary_counters``
        # / ``_streamed_summary_batches`` record the worker's terminal
        # summary so ``on_phase_end("dependency_graph")`` can act as a
        # reconciliation barrier.
        self._streamed_spawned_count: int = 0
        self._streamed_expected_total: Optional[int] = None
        self._streamed_summary_counters: Optional[dict] = None
        self._streamed_summary_batches: Optional[int] = None
        self._setup_done: bool = False
        self._setup_lock = threading.Lock()

    # ── Framework dispatch kwargs (post-b839a2a) ───────────────────────
    #
    # The framework's `_dispatch_local`, `_dispatch_multi_computer_local`,
    # and `_dispatch_slurm` helpers (post-`b839a2a`) duck-type these
    # attributes off the task object via
    # `getattr(task, "fulfillability_matcher", None)` /
    # `getattr(task, "peer_lifecycle_listener", None)` and thread them
    # into `RustPrimaryCoordinator(fulfillability_matcher=…,
    # peer_lifecycle_listener=…)`. The underscore-prefixed attributes
    # remain the internal source-of-truth; these properties are just the
    # contracted public surface.

    @property
    def fulfillability_matcher(self) -> Optional[Callable[..., bool]]:
        return self._fulfillability_matcher

    @property
    def peer_lifecycle_listener(self) -> Optional["_PeerLifecycleListener"]:
        return self._peer_lifecycle_listener

    # ── Worker-function injection seams (used by tests) ────────────────

    @property
    def _build_worker(self):
        return build_worker

    def _substituters_file_path(self) -> Optional[pathlib.Path]:
        if self.config.peers_dir is None:
            return None
        # Per-secondary substituters file so concurrent writers from
        # different secondaries don't clobber each other's
        # self-excluded views (each secondary's peer list naturally
        # differs from every other's).
        return self.config.peers_dir / substituters_filename_for(
            self.config.secondary_id
        )

    # ── Topology ───────────────────────────────────────────────────────

    def get_phases(self):
        """Return the four-phase :class:`PhaseSpec` graph."""
        return _phase_specs(
            build_max_concurrent=self.config.build_max_concurrent,
        )

    # ── Item discovery ─────────────────────────────────────────────────

    def discover_items(
        self,
        source_dir: Optional[pathlib.Path] = None,
        args: Optional[Namespace] = None,
    ) -> Iterable:
        """Yield the run's :class:`TaskInfo` items.

        Phase-2 (matrix_eval) and phase-3 (dependency_graph) items are
        built DIRECTLY in-memory from ``config.per_binary_metadata`` —
        no JSON manifest is written or read for them. Phase-2 yields one
        matrix_eval TaskInfo per binary; phase-3 yields exactly ONE
        dependency_graph TaskInfo (``task_id="dependency_graph"``)
        depending on every matrix_eval task id. The build-shaped phases
        (build_compilers / toolchain_validate / build) are still scanned
        from the JSON manifest dir written by preflight.

        ``source_dir`` / ``args`` are accepted for protocol compatibility
        but explicitly ignored: the framework passes ``args.source``
        (the run's shared-fs root, not the ``manifests/`` subdir), while
        the canonical manifest directory is owned by
        :class:`SuitTaskConfig` and was written by preflight on the
        primary. Honouring ``source_dir`` here would make the framework
        list shared-fs/ instead of shared-fs/manifests/ and silently
        return zero items (the dispatch then falls into the framework's
        ``test/job-submission mode`` and never builds anything).
        """
        del source_dir, args

        # Phase 2 + 3: JSON-free, built in-memory from the preflight's
        # per-binary metadata threaded onto the config.
        yield from self._discover_matrix_eval_and_dep_graph_items()

        # Phases 1 + 4: build-shaped JSON manifests on the shared FS.
        target = self.config.manifest_dir
        try:
            entries = sorted(target.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return

        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.suffix != ".json":
                continue
            try:
                header = read_manifest(entry)
            except Exception as exc:  # noqa: BLE001 — corrupt manifest
                self._logger.warning(
                    "discover_items: skipping unreadable manifest %s: %s",
                    entry,
                    exc,
                )
                continue
            # Phase 2/3 are JSON-free and already yielded in-memory
            # above; any matrix_eval / dependency_graph JSON on disk is
            # stale from a pre-rip-out run and must be ignored so we
            # don't double-emit (or resurrect the per-binary
            # dependency_graph race).
            if header.item_class in ("matrix_eval", "dependency_graph"):
                self._logger.warning(
                    "discover_items: ignoring stale %s manifest %s"
                    " (phase 2/3 are JSON-free)",
                    header.item_class, entry,
                )
                continue
            try:
                phase_id, type_id, affinity_id = _classify(header)
            except ValueError as exc:
                self._logger.warning(
                    "discover_items: %s — skipping %s",
                    exc,
                    entry,
                )
                continue
            # Ship the full ManifestHeader as TaskInfo.payload — with
            # FR-3 the framework propagates this dict through the wire
            # to the worker, so workers never need to read the manifest
            # file. ``path`` is just a synthetic identifier (the
            # manifest filename) used for logging / dedup.
            header_dict = {
                "item_class": header.item_class,
                "name": header.name,
                "size": header.size,
                "payload": dict(header.payload),
            }
            # Intra-phase deps pass through bare; cross-phase toolchain
            # deps are phase-tagged (and the whole set is dropped when
            # disable_task_deps is set) by the shared helper.
            task_depends_on = self._header_task_depends_on(header)
            yield _make_task_info(
                pathlib.Path(entry.name),
                header.size,
                phase_id=phase_id,
                type_id=type_id,
                affinity_id=affinity_id,
                payload=header_dict,
                task_id=header.task_id,
                task_depends_on=task_depends_on,
            )

    def _discover_matrix_eval_and_dep_graph_items(self) -> Iterable:
        """Build the phase-2 (matrix_eval) + phase-3 (dependency_graph)
        TaskInfos in-memory from ``config.per_binary_metadata``.

        No JSON manifest is written or read for either phase. Yields:

        * one matrix_eval TaskInfo per binary (``task_id =
          matrix_eval__<binary>``, no deps), and
        * exactly ONE dependency_graph TaskInfo (``task_id =
          "dependency_graph"``) whose ``task_depends_on`` is the tuple
          of every matrix_eval task id in the run.

        When ``per_binary_metadata`` is empty / ``None`` (e.g. the
        secondary-container config) this yields nothing.
        """
        per_binary = self.config.per_binary_metadata or {}
        if not per_binary:
            return

        sys_name = self.config.sys_name
        # Deterministic order: sorted binaries so task ids + the
        # dependency_graph deps tuple are stable across runs.
        binaries = sorted(per_binary.keys())

        matrix_eval_ids: list[str] = []
        toolchain_aggregate_drv: Optional[str] = None
        for binary in binaries:
            meta = per_binary[binary]
            if not isinstance(meta, dict):
                continue
            tc_agg = meta.get("toolchain_aggregate_drv")
            if not tc_agg:
                # Mirror make_matrix_eval_header's contract: a missing
                # aggregate is a preflight schema mismatch. Skip the
                # binary (and warn) rather than crashing discovery.
                self._logger.warning(
                    "discover_items: binary %r metadata missing"
                    " 'toolchain_aggregate_drv'; skipping its phase-2/3"
                    " tasks",
                    binary,
                )
                continue
            if toolchain_aggregate_drv is None:
                toolchain_aggregate_drv = tc_agg
            header = make_matrix_eval_header(
                binary=binary,
                sys_name=sys_name,
                archs=meta.get("archs", ()),
                toolchain_aggregate_drv=tc_agg,
                variant_sample=meta.get("variant_sample"),
                variant_seed=meta.get("variant_seed"),
                # Default ON when absent so legacy metadata (pre-dedup)
                # behaves as full-dedup; the cli.py path always sets it.
                toolchain_dedup=meta.get("toolchain_dedup", True),
            )
            matrix_eval_ids.append(header.task_id or matrix_eval_task_id(binary))
            yield self._task_info_from_header(header)

        # Single all-binaries dependency_graph task depending on every
        # matrix_eval task id. Skip entirely when no matrix_eval task
        # was emitted (no plannable binary / no aggregate resolved).
        if not matrix_eval_ids or toolchain_aggregate_drv is None:
            return
        dep_graph_header = ManifestHeader(
            item_class="dependency_graph",
            name=DEPENDENCY_GRAPH_TASK_ID,
            size=0,
            payload={
                "sys": sys_name,
                "toolchain_aggregate_drv": toolchain_aggregate_drv,
            },
            task_id=DEPENDENCY_GRAPH_TASK_ID,
            # The matrix_eval prerequisites (task_id = bare binary) are
            # declared in the MATRIX_EVAL phase — a different phase than
            # this task — so configure each as a cross-phase
            # TaskDep(phase_id=...). A bare string would resolve to this
            # task's own (DEPENDENCY_GRAPH) phase and be flagged a
            # missing dep.
            task_depends_on=tuple(
                _make_task_dep(mid, phase_id=Phase.MATRIX_EVAL)
                for mid in matrix_eval_ids
            ),
        )
        yield self._task_info_from_header(dep_graph_header)

    def _task_info_from_header(self, header: ManifestHeader):
        """Build a framework :class:`TaskInfo` directly from an
        in-memory :class:`ManifestHeader` (no disk round-trip).

        Mirrors the disk path's payload shape so the build_worker sees
        a uniform header_dict regardless of source. Honours
        ``disable_task_deps`` the same way the disk loop does.

        ``task_depends_on`` is assembled as the union of (a) the
        intra-phase deps in ``header.task_depends_on`` — bare strings
        that the framework resolves to this task's own phase — and (b)
        the cross-phase toolchain deps in
        ``header.build_compilers_depends_on``, each explicitly wrapped in
        a ``TaskDep(phase_id=Phase.BUILD_COMPILERS)``. ``_make_task_info``
        forwards the tuple verbatim and does NOT phase-tag bare strings,
        so the explicit wrap MUST happen here (same as the
        matrix_eval→dependency_graph edge in :meth:`discover_items`).
        """
        phase_id, type_id, affinity_id = _classify(header)
        header_dict = {
            "item_class": header.item_class,
            "name": header.name,
            "size": header.size,
            "payload": dict(header.payload),
        }
        task_depends_on = self._header_task_depends_on(header)
        return _make_task_info(
            pathlib.Path(f"{header.name}.json"),
            header.size,
            phase_id=phase_id,
            type_id=type_id,
            affinity_id=affinity_id,
            payload=header_dict,
            task_id=header.task_id,
            task_depends_on=task_depends_on,
        )

    def _header_task_depends_on(self, header: ManifestHeader) -> tuple:
        """Resolve a header's full ``task_depends_on`` tuple, honouring
        ``disable_task_deps``.

        Returns ``()`` when ``disable_task_deps`` is set. Otherwise
        delegates to :func:`_header_depends_on`, which passes intra-phase
        deps through as bare strings and wraps every cross-phase
        toolchain id in a phase-tagged ``TaskDep`` so it resolves to
        :attr:`Phase.BUILD_COMPILERS` rather than the variant's own
        (BUILD) phase.
        """
        if self.config.disable_task_deps:
            return ()
        return _header_depends_on(header)

    # ── Memory estimator (disabled) ───────────────────────────────────

    def estimate_memory(self, item) -> int:  # noqa: ARG002 — all items same
        """Return a 1-byte constant — memory budgeting is disabled.

        Empirical variance dominates any per-item memory model on this
        matrix, so we don't try to model it. Returning a tiny constant
        makes the framework's resource scheduler treat all items as
        zero-cost; concurrency is then bounded purely by ``--jobs N``.
        """
        return 1

    # ── Worker plumbing ────────────────────────────────────────────────

    def add_task_arguments(self, parser: ArgumentParser) -> None:
        """No task-specific argparse args; the runner CLI owns the parser."""
        return None

    def build_worker_command_args(
        self,
        type_id: str,
        args: Namespace,
        source_dir: pathlib.Path,
        output_dir: pathlib.Path,
        skip_existing: bool,
    ) -> list[str]:
        """Build the per-type argv the framework appends to ``python -m``.

        The framework spawns
        ``python -m <TaskTypeSpec.worker_module>`` and tacks on the
        argv we return here. Per-manifest config is passed as JSON
        files / paths under the shared FS so workers don't need to
        re-derive them.
        """
        del args, source_dir, output_dir, skip_existing  # unused
        common = [
            "--manifest-dir",
            str(self.config.manifest_dir),
        ]
        if type_id in {
            "dep_graph",
            "eval", "build_compilers", "toolchain_validate",
            "common_dep", "variant",
        }:
            argv = common + [
                "--flake-ref",
                self.config.flake_ref,
                "--dataset-output-dir",
                str(self.config.dataset_dir),
            ]
            substituters = self._substituters_file_path()
            if substituters is not None:
                argv += ["--substituters-file", str(substituters)]
            # Cluster placement-map plumbing. ``shared_fs`` + the
            # secondary identity let the build worker subprocess read
            # ``peers/_paths_*.jsonl`` directly (no in-process watcher
            # available across the framework's fork boundary) and write
            # back its own placement records. The signing public-key
            # authenticates the ``path-have`` push fan-out. For the
            # ``eval`` type (matrix_eval tasks), ``shared_fs`` is
            # additionally required by :func:`build_worker.main`'s
            # BroadcastSender init — the eval worker refuses to run
            # without it and raises NonRecoverableError immediately.
            if self.config.shared_fs is not None:
                argv += ["--shared-fs", str(self.config.shared_fs)]
            if self.config.secondary_id:
                argv += ["--secondary-id", self.config.secondary_id]
            if self._signing_key is not None:
                argv += [
                    "--signing-public-key", self._signing_key.public_key,
                ]
            # matrix_eval marker dir: bind-mount-visible path so the
            # eval worker writes its matrix-<binary>.drv.archive there and the
            # dep_graph worker (PH-A) imports it from the same location.
            # The matrix_aggregate drv is threaded out-of-band via
            # ``Task.publish_string`` / predecessor_outputs, not on
            # disk. Build types (variant / common_dep / toolchain_validate)
            # also need it now: with toolchain dedup the build prelude
            # imports ``toolchains.drv.archive`` from this dir before the
            # per-binary diff archive (toolchain-first), so a build-only
            # secondary that never ran eval/dep_graph still gets the path.
            if (
                type_id in {
                    "eval", "dep_graph",
                    "variant", "common_dep", "toolchain_validate",
                }
                and self.config.matrix_eval_out_dir is not None
            ):
                argv += [
                    "--matrix-eval-out-dir",
                    str(self.config.matrix_eval_out_dir),
                ]
            return argv
        return common

    def get_output_filename_pattern(self, type_id, item) -> str:
        """Identity pattern — workers write their own output names."""
        del type_id  # unused
        return getattr(item, "path", item).name if hasattr(
            item, "path"
        ) else str(item)

    # ── Lifecycle hooks ────────────────────────────────────────────────

    def on_run_start(
        self,
        source_dir: Optional[pathlib.Path] = None,
        output_dir: Optional[pathlib.Path] = None,
        args: Optional[Namespace] = None,
        primary_handle: Optional[Any] = None,
    ) -> None:
        """Bring up peer-cache state. Idempotent.

        ``primary_handle`` is the in-flight runtime control surface
        the framework's modern dispatcher (post-``5fa212c``) passes via
        kwarg. Captured onto ``self._primary_handle`` for the
        primary-affined control-plane wrappers; the streamed
        ``dependency_graph`` → ``build`` spawn itself receives its
        handle per-message via :meth:`custom_message_handler`. When
        the kwarg is absent (legacy callers, single-process tests)
        :meth:`on_phase_end` logs a warning and skips the
        dependency_graph reconciliation.
        """
        del source_dir, args, output_dir  # unused
        with self._setup_lock:
            if self._setup_done:
                # Late-binding the handle on a re-entry is harmless;
                # :meth:`on_phase_end` reads ``self._primary_handle``
                # through the SuitTask reference, so a flip after
                # construction still takes effect for the later
                # reconciliation gate.
                if primary_handle is not None:
                    self._primary_handle = primary_handle
                return
            if primary_handle is not None:
                self._primary_handle = primary_handle

            # 1. Signing key — idempotent on shared FS.
            try:
                self._signing_key = generate_signing_key(
                    self.config.shared_fs, self.config.run_id
                )
            except FileNotFoundError as exc:
                self._logger.warning(
                    "on_run_start: signing key generation skipped"
                    " (nix CLI not found): %s",
                    exc,
                )
                self._signing_key = None
            except Exception:  # noqa: BLE001 — never raise out
                self._logger.exception(
                    "on_run_start: signing key generation failed"
                )
                self._signing_key = None

            public_key = (
                self._signing_key.public_key
                if self._signing_key is not None
                else ""
            )
            my_info = PeerInfo(
                secondary_id=self.config.secondary_id,
                hostname=self.config.hostname,
                port=self.config.harmonia_port,
                public_key=public_key,
            )

            # 2. Watcher (start FIRST so its initial _refresh populates
            # the live peer set before we announce/push). The push tick
            # is relaxed to 60 s once push is enabled — push collapses
            # the typical announce → discover latency to one HTTP RTT,
            # leaving polling as a join-race / lost-packet safety net.
            push_enabled = bool(public_key)
            try:
                self._peer_watcher = PeerListWatcher(
                    shared_fs=self.config.shared_fs,
                    exclude_id=self.config.secondary_id,
                    tick_seconds=60.0 if push_enabled else 5.0,
                )
                self._peer_watcher.start()
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: PeerListWatcher failed to start"
                )
                self._peer_watcher = None

            # 2a-bis. PathPlacementWatcher — same NFS-gossip + push-wake
            # shape as PeerListWatcher, but aggregating per-secondary
            # ``_paths_*.jsonl`` placement records. Workers query it
            # before every fetch decision; the polling tick is the
            # safety net for missed pushes.
            try:
                self._placement_watcher = PathPlacementWatcher(
                    shared_fs=self.config.shared_fs,
                    tick_seconds=60.0 if push_enabled else 5.0,
                )
                self._placement_watcher.start()
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: PathPlacementWatcher failed to start"
                )
                self._placement_watcher = None

            # 2b-pre. K=3 replication coordination plane.
            #
            # NOT a framework task — runs parallel-async to task/worker
            # management. The receiver's handler callbacks need to be
            # bound at PeerPushServer construction time (the server
            # snapshots its callbacks into a generated handler class),
            # so the replication classes have to be ready BEFORE the
            # push server is constructed.
            #
            # Repair-on-death has two signal sources:
            #
            #   1. (primary, when shipped) Framework peer-removed hook —
            #      sub-tick latency, see plan: Framework Ask #4.
            #      Wired via ``ReplicationRepairWorker.on_peer_removed``.
            #   2. (fallback) PathPlacementWatcher diff callback —
            #      tick-bounded, kicks in if the framework hook is not
            #      yet present in this dynrunner version.
            if (
                push_enabled
                and self._peer_watcher is not None
                and self._placement_watcher is not None
                and self._signing_key is not None
            ):
                try:
                    peer_watcher = self._peer_watcher
                    placement_watcher = self._placement_watcher
                    # Build the outpath→task_hash_hex lookup from the
                    # toolchain manifests on disk. Best-effort: any
                    # manifest read failure logs + degrades; the
                    # matcher will simply return False for that
                    # outpath until the dict is populated.
                    self._outpath_to_task_hash = (
                        self._build_outpath_to_task_hash_lookup()
                    )
                    repl_ctx = ReplicationContext(
                        my_secondary_id=self.config.secondary_id,
                        our_pubkey=public_key,
                        shared_fs=self.config.shared_fs,
                        get_peers=lambda: list(peer_watcher.peers),
                        get_placements=lambda: placement_watcher.snapshot(),
                        replication_k=getattr(
                            self.config, "replication_k",
                            DEFAULT_REPLICATION_K,
                        ),
                        # Q1 + Q2 PrimaryHandle bindings. Each wrapper
                        # closes over ``self`` and routes through
                        # ``self._primary_handle`` on every call, so a
                        # later ``wire_primary_handle`` flip is visible
                        # without rebuilding the (frozen) context.
                        mark_task_unfulfillable=self._mark_task_unfulfillable,
                        reinject_task=self._reinject_task,
                        update_preferred_secondaries=(
                            self._update_preferred_secondaries
                        ),
                        lookup_task_hash_for_outpath=(
                            self._outpath_to_task_hash.get
                        ),
                    )
                    self._replication_sender = ReplicationSender(repl_ctx)
                    self._replication_receiver = ReplicationReceiver(
                        repl_ctx, self._replication_sender,
                    )
                    self._replication_repair = ReplicationRepairWorker(
                        repl_ctx, self._replication_sender,
                    )
                    # Backstop wiring: placement-diff drives repair when
                    # the Q4 framework hook is absent OR loses a race.
                    # When both fire for the same removal the diff log
                    # surfaces which path caught it first; see
                    # ``ReplicationRepairWorker.on_diff``.
                    placement_watcher.register_diff_callback(
                        self._replication_repair.on_diff,
                    )
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: ReplicationSender/Receiver/Repair "
                        "init failed; K=3 coordination disabled"
                    )
                    self._replication_sender = None
                    self._replication_receiver = None
                    self._replication_repair = None

                # Q4 peer-lifecycle listener. Constructed after the
                # repair worker so the listener can route on_peer_removed
                # straight to it. Held on ``self`` so the dispatch
                # site (cli.py / RustPrimaryCoordinator kwargs) can
                # fetch + pass it via ``peer_lifecycle_listener=``.
                try:
                    self._peer_lifecycle_listener = _PeerLifecycleListener(
                        repair_worker=self._replication_repair,
                        placement_record_observer_callable=(
                            self._record_observer_holdings
                        ),
                        logger=self._logger,
                    )
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: _PeerLifecycleListener init failed;"
                        " Q4 hook will not be exposed"
                    )
                    self._peer_lifecycle_listener = None

                # matrix_eval broadcast consumer (path-broadcast-offer).
                # Parallel to the K=3 receiver above but for the
                # all-peers flood-fill protocol: every secondary that
                # accepts a broadcast both fetches the drv from the
                # originator and re-fans the offer to the rest of
                # the cluster (minus self + origin). Constructed here
                # so its ``on_broadcast_offer`` method can be passed
                # to the PeerPushServer below.
                try:
                    self_sid = self.config.secondary_id

                    def _broadcast_peer_url_map() -> dict[str, str]:
                        # Snapshot: peer_id -> push URL for every
                        # currently-known peer, including self (the
                        # receiver subtracts self + origin at fan-out
                        # time, per the broadcast contract).
                        return {
                            p.secondary_id: (
                                f"http://{p.hostname}:"
                                f"{push_port_for(p.port)}"
                            )
                            for p in peer_watcher.peers
                        }

                    def _broadcast_fetch(
                        path: str, origin_push_url: str,
                    ) -> bool:
                        # The provider hands us push URLs; nix copy
                        # needs the harmonia substituter URL. Same
                        # host, port - PUSH_PORT_OFFSET. Falling back
                        # to the raw URL on a parse failure is safe:
                        # nix copy will just error and we return
                        # False.
                        substituter_url = (
                            _push_url_to_substituter_url(
                                origin_push_url,
                            )
                            or origin_push_url
                        )
                        return peer_paths_fetch.nix_copy_from_url(
                            path, substituter_url,
                        )

                    self._broadcast_receiver = BroadcastReceiver(
                        self_peer_id=self_sid,
                        peer_url_provider=_broadcast_peer_url_map,
                        is_path_locally_valid=(
                            peer_paths_fetch.is_path_locally_valid
                        ),
                        fetch_path_from_peer=_broadcast_fetch,
                        our_pubkey=public_key,
                        shared_fs=self.config.shared_fs,
                    )
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: BroadcastReceiver init failed;"
                        " matrix_eval drv flood-fill consumer disabled"
                    )
                    self._broadcast_receiver = None

            # 2b. Push server (listens BEFORE we announce so any peer
            # that pushes us in response sees a ready listener). Keyed
            # off ``public_key``: without the cluster signing key we
            # have no auth token, so we degrade to polling-only.
            if push_enabled and self._peer_watcher is not None:
                try:
                    refresh_cb = self._peer_watcher.request_refresh
                    placement_watcher = self._placement_watcher
                    placement_refresh = (
                        placement_watcher.request_refresh
                        if placement_watcher is not None
                        else (lambda: None)
                    )
                    receiver = self._replication_receiver
                    sender = self._replication_sender
                    broadcast_receiver = self._broadcast_receiver

                    def _on_path_have(record: dict) -> None:
                        placement_refresh()
                        # Notify the sender so any of OUR in-flight
                        # offers to this peer settle and convergence
                        # detection (path-cancel fan-out) can fire.
                        if sender is not None:
                            holder = record.get("secondary_id", "")
                            outpath = record.get("outpath", "")
                            if holder and outpath:
                                try:
                                    sender.on_path_have(holder, outpath)
                                except Exception:  # noqa: BLE001
                                    self._logger.exception(
                                        "ReplicationSender.on_path_have raised"
                                    )

                    def _on_path_offer(record: dict) -> None:
                        if receiver is not None:
                            receiver.on_offer(record)

                    def _on_path_accept(record: dict) -> None:
                        if sender is not None:
                            sender.on_accept(
                                record.get("from_secondary_id", ""),
                                record.get("outpath", ""),
                            )

                    def _on_path_reject(record: dict) -> None:
                        if sender is not None:
                            sender.on_reject(
                                record.get("from_secondary_id", ""),
                                record.get("outpath", ""),
                                record.get("reason", ""),
                            )

                    def _on_path_cancel(record: dict) -> None:
                        if receiver is not None:
                            receiver.on_cancel(record)

                    # Broadcast-receive placement gossip: when the
                    # consumer accepts a ``/peer/path-broadcast-offer``
                    # (the drv has been fetched into our local store),
                    # publish a holder record under
                    # ``item_class="matrix_eval_drv"`` so the
                    # placement-map watcher on every other secondary
                    # learns we hold it. Mirrors the K=3 receiver's
                    # post-fetch ``record_self_has`` call but with the
                    # matrix_eval item-class so matrix_eval drv holders
                    # are distinguishable from toolchain / variant
                    # holders.
                    _record_broadcast_self_has = (
                        self._make_broadcast_record_self_has(
                            peer_watcher, public_key,
                        )
                    )

                    def _on_broadcast_offer(
                        path: str,
                        size: int,
                        origin_peer_id: str,
                        broadcast_id: str,
                        hop_count: int,
                    ) -> bool:
                        # Without a wired BroadcastReceiver we cannot
                        # actually fetch the drv; reject so the
                        # originator's placement accounting is honest.
                        if broadcast_receiver is None:
                            return False
                        return broadcast_receiver.on_broadcast_offer(
                            path, size, origin_peer_id,
                            broadcast_id, hop_count,
                        )

                    self._push_server = PeerPushServer(
                        bind_host="0.0.0.0",
                        port=push_port_for(self.config.harmonia_port),
                        expected_pubkey=public_key,
                        on_announce=lambda _info: refresh_cb(),
                        on_withdraw=lambda _sid: refresh_cb(),
                        on_path_have=_on_path_have,
                        on_path_gone=lambda _rec: placement_refresh(),
                        on_path_offer=_on_path_offer,
                        on_path_accept=_on_path_accept,
                        on_path_reject=_on_path_reject,
                        on_path_cancel=_on_path_cancel,
                        record_broadcast_self_has=_record_broadcast_self_has,
                        on_broadcast_offer=_on_broadcast_offer,
                    )
                    self._push_server.start()
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: PeerPushServer failed to start"
                    )
                    self._push_server = None

            # 3. Announce self (writes peers/<id>.json), then push the
            # announce to currently-known peers so they re-read without
            # waiting for their next 60 s tick.
            try:
                announce_self(self.config.shared_fs, my_info)
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: announce_self failed"
                )
            if (
                push_enabled
                and self._peer_watcher is not None
                and self._push_server is not None
            ):
                try:
                    sent = fan_out_announce(
                        self._peer_watcher.peers, my_info, public_key,
                    )
                    self._logger.debug(
                        "on_run_start: announce push sent to %d peer(s)",
                        sent,
                    )
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: fan_out_announce failed"
                    )

            # 4. Cachix uploader (optional).
            if self.config.cachix_cache and self.config.cachix_token_file:
                try:
                    cfg = UploaderConfig(
                        cache_name=self.config.cachix_cache,
                        auth_token_file=self.config.cachix_token_file,
                    )
                    self._cachix_uploader = CachixUploader(cfg)
                    self._cachix_uploader.start()
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: CachixUploader failed to start"
                    )
                    self._cachix_uploader = None

            # 5. Harmonia (optional). Requires nix-daemon (harmonia 3.x
            # talks to the daemon over its socket for store queries).
            if self.config.enable_harmonia and self._signing_key is not None:
                # Land the per-secondary aux-process logs (nix-daemon,
                # harmonia) INSIDE this secondary's own subdir alongside
                # the framework's per-role logs
                # (<log-network>/<id>/{primary,secondary,worker_0}.log),
                # instead of at the top level with the id baked into the
                # filename. The per-id DIRECTORY keeps each node's path
                # unique (no shared inode across concurrent writers)
                # without an id-scoped filename.
                sec_log_dir = (
                    pathlib.Path("/app/log-network")
                    / self.config.secondary_id
                )
                try:
                    sec_log_dir.mkdir(parents=True, exist_ok=True)
                except OSError:  # noqa: BLE001 — best-effort; the
                    # framework already creates <id>/ for its own logs.
                    pass
                try:
                    # Start nix-daemon FIRST. Idempotent — no-op if a
                    # daemon socket already exists. Container's
                    # nix.conf is in single-user mode so no nixbld*
                    # group is needed. Log goes on the gateway-
                    # readable mount alongside harmonia's so we can
                    # diagnose worker connection-reset issues from
                    # the host after the container exits and its
                    # /tmp gets nuked.
                    from .peer_cache import start_nix_daemon
                    daemon_log = sec_log_dir / "nix-daemon.log"
                    start_nix_daemon(daemon_log)
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: nix-daemon start failed"
                        " (harmonia will likely 500 on store queries)"
                    )
                try:
                    # Log goes on the gateway-readable mount inside this
                    # secondary's subdir (operators read it from the
                    # gateway; other secondaries never write it). TOML
                    # stays container-local under the default runtime_dir.
                    log_path = sec_log_dir / "harmonia.log"
                    self._harmonia = HarmoniaProcess(
                        bind_addr=f"0.0.0.0:{self.config.harmonia_port}",
                        signing_key_path=self._signing_key.secret_path,
                        log_path=log_path,
                    )
                    self._harmonia.start()
                except FileNotFoundError as exc:
                    self._logger.info(
                        "on_run_start: harmonia binary not found,"
                        " continuing without local cache server: %s",
                        exc,
                    )
                    self._harmonia = None
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: harmonia failed to start"
                    )
                    self._harmonia = None

            # 6. PeerNixConfWatcher — translate the live peer set
            # into ``/etc/nix/peer.conf`` so the image's baseline
            # ``/etc/nix/nix.conf`` (which `!include`s peer.conf)
            # picks up substituters + trusted-public-keys for every
            # other secondary. Lets a worker's ``nix-store --realise``
            # transparently fetch built paths from peer harmonias
            # without per-build CLI flags.
            if self._peer_watcher is not None:
                try:
                    from .peer_cache import PeerNixConfWatcher
                    self._peer_nix_conf_watcher = PeerNixConfWatcher(
                        self._peer_watcher
                    )
                    self._peer_nix_conf_watcher.start()
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: PeerNixConfWatcher start failed"
                    )

            # 7. ssh_debug (opt-in) — spawn sshd as a detached session
            # leader on ``config.ssh_debug_port`` and drop a ready
            # marker on the gateway-readable mount. Survives this
            # ``on_run_start`` call's death so even if the secondary
            # restarts, the operator's session keeps working.
            if self.config.enable_ssh_debug:
                try:
                    from .ssh_debug import (
                        publish_ready_marker,
                        start_sshd_detached,
                    )
                    sshd_pid = start_sshd_detached(
                        port=self.config.ssh_debug_port,
                    )
                    if sshd_pid is not None or os.path.exists(
                        "/tmp/ssh-debug/sshd.pid"
                    ):
                        publish_ready_marker(
                            self.config.hostname,
                            self.config.ssh_debug_port,
                        )
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: ssh_debug start failed"
                    )

            self._setup_done = True

    def on_run_end(self, success: bool = True) -> None:
        """Reverse :meth:`on_run_start`. Idempotent and best-effort."""
        del success  # informational only
        with self._setup_lock:
            if self._peer_nix_conf_watcher is not None:
                try:
                    self._peer_nix_conf_watcher.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: PeerNixConfWatcher stop failed"
                    )
                finally:
                    self._peer_nix_conf_watcher = None

            # Push withdraw BEFORE we drop the file or stop the watcher
            # — we still have access to the live peer list and the
            # signing public-key for the auth header.
            if (
                self._push_server is not None
                and self._peer_watcher is not None
                and self._signing_key is not None
            ):
                try:
                    sent = fan_out_withdraw(
                        self._peer_watcher.peers,
                        self.config.secondary_id,
                        self._signing_key.public_key,
                    )
                    self._logger.debug(
                        "on_run_end: withdraw push sent to %d peer(s)", sent,
                    )
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: fan_out_withdraw failed"
                    )

            # Stop the replication coordination plane before the push
            # server: cancels any pending offer timers cleanly so they
            # don't try to push through a torn-down server.
            if self._replication_sender is not None:
                try:
                    self._replication_sender.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: ReplicationSender stop failed"
                    )
                finally:
                    self._replication_sender = None
            if self._replication_receiver is not None:
                try:
                    self._replication_receiver.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: ReplicationReceiver stop failed"
                    )
                finally:
                    self._replication_receiver = None
            self._replication_repair = None
            if self._broadcast_receiver is not None:
                try:
                    self._broadcast_receiver.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: BroadcastReceiver stop failed"
                    )
                finally:
                    self._broadcast_receiver = None

            if self._push_server is not None:
                try:
                    self._push_server.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: PeerPushServer stop failed"
                    )
                finally:
                    self._push_server = None

            if self._harmonia is not None:
                try:
                    self._harmonia.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: harmonia stop failed"
                    )
                finally:
                    self._harmonia = None

            if self._cachix_uploader is not None:
                try:
                    self._cachix_uploader.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: CachixUploader stop failed"
                    )
                finally:
                    self._cachix_uploader = None

            if self._peer_watcher is not None:
                try:
                    self._peer_watcher.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: PeerListWatcher stop failed"
                    )
                finally:
                    self._peer_watcher = None

            if self._placement_watcher is not None:
                try:
                    self._placement_watcher.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "on_run_end: PathPlacementWatcher stop failed"
                    )
                finally:
                    self._placement_watcher = None

            try:
                # Also unlinks ``peers/_paths_<sid>.jsonl`` so peers'
                # placement watchers drop our entries on next tick.
                withdraw_self(
                    self.config.shared_fs, self.config.secondary_id
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "on_run_end: withdraw_self failed"
                )

            self._signing_key = None
            self._setup_done = False

    # ── Q1 + Q2 PrimaryHandle wrappers ───────────────────────────────

    def _mark_task_unfulfillable(
        self, task_hash_hex: str, reason: str,
    ) -> None:
        """Wrap ``primary_handle.fail_permanent`` for ReplicationContext.

        Converts the consumer-side hex string into the ``bytes`` the
        Rust API expects. Logs + degrades when the handle is unbound
        or the framework method is missing/raises so the cluster's
        legacy NFS-poll fallback keeps working.
        """
        handle = self._primary_handle
        if handle is None:
            self._logger.debug(
                "mark_task_unfulfillable: no primary_handle bound;"
                " task_hash=%s reason=%r", task_hash_hex, reason,
            )
            return
        fail_permanent = getattr(handle, "fail_permanent", None)
        if fail_permanent is None:
            self._logger.warning(
                "mark_task_unfulfillable: primary_handle exposes no"
                " fail_permanent; framework pin is too old"
            )
            return
        try:
            task_hash_bytes = bytes.fromhex(task_hash_hex)
        except ValueError:
            self._logger.warning(
                "mark_task_unfulfillable: task_hash_hex=%r is not"
                " hex-decodable; skipping",
                task_hash_hex,
            )
            return
        try:
            fail_permanent(task_hash_bytes, "Unfulfillable", reason)
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "mark_task_unfulfillable: primary_handle"
                ".fail_permanent raised for %s",
                task_hash_hex,
            )

    def _reinject_task(self, task_hash_hex: str) -> None:
        """Wrap ``primary_handle.reinject_task`` for ReplicationContext."""
        handle = self._primary_handle
        if handle is None:
            self._logger.debug(
                "reinject_task: no primary_handle bound; task_hash=%s",
                task_hash_hex,
            )
            return
        reinject = getattr(handle, "reinject_task", None)
        if reinject is None:
            self._logger.warning(
                "reinject_task: primary_handle exposes no"
                " reinject_task; framework pin is too old"
            )
            return
        try:
            task_hash_bytes = bytes.fromhex(task_hash_hex)
        except ValueError:
            self._logger.warning(
                "reinject_task: task_hash_hex=%r is not hex-decodable",
                task_hash_hex,
            )
            return
        try:
            reinject(task_hash_bytes)
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "reinject_task: primary_handle.reinject_task raised"
                " for %s",
                task_hash_hex,
            )

    def _update_preferred_secondaries(
        self, task_hash_hex: str, secondaries: list[str],
    ) -> None:
        """Wrap ``primary_handle.update_preferred_secondaries``."""
        handle = self._primary_handle
        if handle is None:
            self._logger.debug(
                "update_preferred_secondaries: no primary_handle"
                " bound; task_hash=%s len=%d",
                task_hash_hex, len(secondaries),
            )
            return
        update = getattr(
            handle, "update_preferred_secondaries", None,
        )
        if update is None:
            self._logger.warning(
                "update_preferred_secondaries: primary_handle exposes"
                " no update_preferred_secondaries; framework pin too old"
            )
            return
        try:
            task_hash_bytes = bytes.fromhex(task_hash_hex)
        except ValueError:
            self._logger.warning(
                "update_preferred_secondaries: task_hash_hex=%r is"
                " not hex-decodable",
                task_hash_hex,
            )
            return
        try:
            update(task_hash_bytes, list(secondaries))
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "update_preferred_secondaries: primary_handle"
                ".update_preferred_secondaries raised for %s",
                task_hash_hex,
            )

    def _record_observer_holdings(
        self, observer_id: str, _placeholder: str,
    ) -> None:
        """Hook invoked by :class:`_PeerLifecycleListener` when an
        observer joins. Today an emergency placeholder: the observer
        announces its holdings through the framework's mesh-announce
        channel, which the local placement watcher picks up on its
        next tick. We log so the operator can correlate the lifecycle
        event with placement-map changes; mid-run hydration via
        ``record_self_has`` would require the framework to expose the
        observer's holdings list to the listener, which is not part
        of the current Q4 wire contract.
        """
        del _placeholder
        self._logger.info(
            "observer peer registered in placement gossip: %s"
            " (waiting on observer's own announce-on-PrimaryChanged)",
            observer_id,
        )

    def _build_outpath_to_task_hash_lookup(self) -> dict[str, str]:
        """Scan toolchain manifests and build the outpath → task_hash dict.

        For each ``build_compilers`` / ``toolchain_validate`` manifest
        with both an ``outpath`` payload field and a ``task_id``,
        compute the task_hash via ``dynamic_runner.compute_task_hash``
        (when available) and record the mapping. Without the framework,
        fall back to the manifest's ``task_id`` so tests still resolve
        a non-empty identifier (the framework would refuse it as a
        wrong-shape hash; that's the degraded path the wrappers
        already log).

        Best-effort: any per-manifest read error logs + skips. Returns
        an empty dict if no manifests exist (matrix_eval-only run
        before toolchain manifests are emitted).
        """
        result: dict[str, str] = {}
        try:
            entries = sorted(self.config.manifest_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return result

        try:
            from dynamic_runner import (  # type: ignore[import-not-found]
                compute_task_hash,
            )
        except Exception:  # noqa: BLE001 — framework absent
            compute_task_hash = None  # type: ignore[assignment]

        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.startswith((".", "_")):
                continue
            if entry.suffix != ".json":
                continue
            try:
                header = read_manifest(entry)
            except Exception:  # noqa: BLE001 — log + skip
                self._logger.debug(
                    "outpath lookup: skipping unreadable %s", entry,
                )
                continue
            if header.item_class not in (
                "build_compilers",
                "toolchain_validate",
            ):
                continue
            outpath = header.payload.get("outpath")
            if not isinstance(outpath, str) or not outpath:
                continue
            task_id = header.task_id
            if not task_id:
                continue
            task_hash_hex: Optional[str] = None
            if compute_task_hash is not None:
                # Build a minimal TaskInfo-compatible object so the
                # framework can compute the hash. We rely on the
                # framework's hashing being a function of task_id +
                # payload identity; if the API rejects our duck-typed
                # object we fall through to the task_id fallback.
                try:
                    phase_id = (
                        Phase.BUILD_COMPILERS
                        if header.item_class == "build_compilers"
                        else Phase.BUILD
                    )
                    type_id = (
                        "build_compilers"
                        if header.item_class == "build_compilers"
                        else "toolchain_validate"
                    )
                    task_info = _make_task_info(
                        path=pathlib.Path(entry.name),
                        size=header.size,
                        phase_id=phase_id,
                        type_id=type_id,
                        affinity_id=None,
                        payload=dict(header.payload),
                        task_id=task_id,
                        task_depends_on=tuple(header.task_depends_on),
                    )
                    task_hash_hex = compute_task_hash(task_info)
                except Exception:  # noqa: BLE001
                    self._logger.debug(
                        "outpath lookup: compute_task_hash raised"
                        " for %s; falling back to task_id",
                        entry.name,
                    )
                    task_hash_hex = None
            if not task_hash_hex:
                task_hash_hex = task_id
            result[outpath] = task_hash_hex
        return result

    # ── Q1+Q2+Q3+Q4 public wire-in entrypoint ──────────────────────────

    def wire_primary_handle(self, primary_handle: Any) -> None:
        """Bind framework PrimaryHandle bindings; call before run().

        Performs the Q1+Q2+Q3+Q4 wire-in:

        * Applies the configured
          ``--unfulfillable-reinject-max-per-task`` cap on the handle
          via :meth:`apply_unfulfillable_reinject_cap`.
        * Binds the handle onto the late-bound wrappers (Q1
          fail_permanent + reinject_task, Q2
          update_preferred_secondaries) so subsequent
          ``ReplicationContext`` callable invocations reach the Rust
          control plane.

        The Q3 ``fulfillability_matcher`` and Q4
        ``peer_lifecycle_listener`` are NOT wired by this method —
        they are kwargs on ``RustPrimaryCoordinator`` construction and
        must be passed by whoever instantiates the coordinator. Read
        them off this task instance:

        * ``task._fulfillability_matcher`` — the matcher callable
        * ``task._peer_lifecycle_listener`` — the listener object

        Idempotent: re-calling with a different handle just rebinds.
        """
        self._primary_handle = primary_handle
        try:
            self.apply_unfulfillable_reinject_cap(primary_handle)
        except Exception:  # noqa: BLE001
            self._logger.exception(
                "wire_primary_handle: apply_unfulfillable_reinject_cap"
                " raised"
            )

    def apply_unfulfillable_reinject_cap(self, primary_handle: Any) -> None:
        """Forward the configured reinject cap onto a primary handle.

        Must be called BEFORE ``primary_handle.run()`` starts: the
        framework freezes the per-task budget cell when ``run()``
        flips ``run_started``. When the config field is ``None`` we
        skip the setter call entirely so the framework keeps its
        own default (unbounded). The call is wrapped in a guarded
        try/except so a framework version that doesn't expose
        ``set_unfulfillable_reinject_max_per_task`` simply logs +
        continues instead of aborting the run.
        """
        cap = self.config.unfulfillable_reinject_max_per_task
        if cap is None:
            return
        setter = getattr(
            primary_handle,
            "set_unfulfillable_reinject_max_per_task",
            None,
        )
        if setter is None:
            self._logger.warning(
                "primary_handle has no "
                "set_unfulfillable_reinject_max_per_task; "
                "framework default (unbounded) remains in effect"
            )
            return
        try:
            setter(cap)
        except Exception:  # noqa: BLE001 — never raise out
            self._logger.exception(
                "set_unfulfillable_reinject_max_per_task(%d) failed",
                cap,
            )

    def on_phase_start(self, phase_id: str) -> None:
        self._logger.info("phase %s starting", phase_id)
        if phase_id == Phase.DEPENDENCY_GRAPH:
            # Fresh stream per run: a run-level restart or a reused
            # SuitTask instance restreams the whole plan, so counters
            # inherited from a previous run would poison the
            # on_phase_end reconciliation barrier. Reset them here,
            # before the worker can send its first spawn_batch. (The
            # framework fires on_phase_start once per phase per run; a
            # WITHIN-run dependency_graph task retry restreams without
            # a reset and trips the barrier loudly — preferable to
            # risking duplicate spawns.)
            self._streamed_spawned_count = 0
            self._streamed_expected_total = None
            self._streamed_summary_counters = None
            self._streamed_summary_batches = None

    def on_phase_end(
        self,
        phase_id: str,
        completed: int = 0,
        failed: int = 0,
        *,
        # Still supplied by the framework's call shape; retained for
        # compatibility and intentionally unused since the published-
        # pickle handoff was replaced by the streamed-spawn messages.
        phase_outputs: Optional[dict] = None,
    ) -> None:
        self._logger.info(
            "phase %s ended: %d completed, %d failed",
            phase_id,
            completed,
            failed,
        )
        if phase_id != Phase.DEPENDENCY_GRAPH:
            return
        if self._primary_handle is None:
            # Secondary, or a framework-pin gap where the handle was
            # never delivered to on_run_start. Task creation is a
            # primary-only operation, so there is nothing to do here.
            self._logger.warning(
                "on_phase_end(\"dependency_graph\"): no primary_handle"
                " (secondary or framework-pin gap); cannot spawn build"
                " phase"
            )
            return

        # Reconciliation barrier ONLY. The build tasks were already
        # spawned incrementally by :meth:`custom_message_handler` as
        # the dependency_graph worker streamed its descriptor batches;
        # here we just verify the spawned count against the worker's
        # authoritative terminal summary so a lost batch (or a missing
        # summary) fails loudly instead of silently under-spawning.
        #
        # KNOWN Wave-1 limitation: these counts are primary-local. A
        # mid-stream failover replays only the still-Unhandled messages
        # into a promoted primary whose counters start fresh, so this
        # reconciliation can false-alarm after failover. Accepted for
        # now — loud beats silent.
        if self._streamed_expected_total is None:
            raise RuntimeError(
                "dependency_graph handoff incomplete: no summary"
                " message received"
                f" (spawned={self._streamed_spawned_count})"
            )
        if self._streamed_spawned_count != self._streamed_expected_total:
            raise RuntimeError(
                "dependency_graph handoff mismatch:"
                f" spawned={self._streamed_spawned_count}"
                f" != total={self._streamed_expected_total}"
                f" (counters={self._streamed_summary_counters})"
            )
        self._logger.info(
            "dependency_graph handoff reconciled:"
            " spawned == total == %d (batches=%s, counters=%s)",
            self._streamed_spawned_count,
            self._streamed_summary_batches,
            self._streamed_summary_counters,
        )

    # ── Streamed dependency_graph → build spawn transport ──────────────

    def worker_message_listener(
        self,
        worker_id: int,
        type_id: str,
        topic: str,
        data: bytes,
        secondary_handle: Any,
    ) -> None:
        """Secondary-side relay for worker custom messages.

        Duck-typed framework hook: invoked on the SECONDARY when a
        worker subprocess sends a custom message. Streamed-spawn
        traffic (the dependency_graph worker's
        :data:`streamed_spawn.SPAWN_TOPIC` batches and its terminal
        :data:`streamed_spawn.SUMMARY_TOPIC`) is forwarded VERBATIM to
        the primary as an IMPORTANT message — decode-free, so a
        malformed payload surfaces on the primary via the framework's
        terminal-Failed handler-raise path instead of killing the relay.
        Any other topic is ignored at debug level: the relay must
        never poison unrelated traffic.
        """
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            SPAWN_TOPIC,
            SUMMARY_TOPIC,
        )

        if topic in (SPAWN_TOPIC, SUMMARY_TOPIC):
            self._logger.info(
                "worker_message_listener: forwarding %s to primary"
                " (worker_id=%d, %d bytes)",
                topic, worker_id, len(data),
            )
            secondary_handle.send_to_primary(topic, data, important=True)
            return
        self._logger.debug(
            "worker_message_listener: ignoring topic %r"
            " (worker_id=%d, type_id=%s, %d bytes)",
            topic, worker_id, type_id, len(data),
        )

    def custom_message_handler(
        self,
        origin: str,
        topic: str,
        data: bytes,
        important: bool,
        primary_handle: Any,
    ) -> None:
        """Primary-side consumer of the streamed-spawn messages.

        Duck-typed framework hook: invoked ON THE PRIMARY for each
        relayed custom message. ``spawn_batch`` messages are decoded
        (:func:`streamed_spawn.decode_spawn_message`), translated
        descriptor → :class:`ManifestHeader` → ``TaskInfo``, and
        handed to ``primary_handle.spawn_tasks`` immediately; the
        terminal ``summary`` records the authoritative totals for the
        :meth:`on_phase_end` reconciliation barrier.

        Raising is treated by the framework as a USER ERROR: the
        message goes terminally Failed on the FIRST raise (no retry),
        with a structured ERROR (origin/seq/topic/exception) in the
        primary log, and any partially-queued effect from the raising
        handler is discarded (all-or-nothing). The framework invokes
        custom-message handlers serially (never concurrently), so the
        ``_streamed_*`` counters are deliberately unguarded — no lock.
        The missing spawns are
        then caught loudly by the :meth:`on_phase_end` reconciliation
        barrier. That is the intended failure chain, so this method
        does NOT catch: ``ValueError`` (malformed payload / unknown
        topic) and ``RuntimeError`` (no usable primary_handle,
        conflicting summary) propagate by design. Handler effects and
        the Handled mark commit as ONE atomic CRDT frame, so a
        replayed/duplicate spawn-effect cannot double-land.
        """
        del important  # relay always marks these important
        from compiler_suit_runner.streamed_spawn import (  # noqa: PLC0415
            SPAWN_TOPIC,
            SUMMARY_TOPIC,
            decode_spawn_message,
        )

        if topic not in (SPAWN_TOPIC, SUMMARY_TOPIC):
            raise ValueError(
                f"custom_message_handler: unknown topic {topic!r}"
                f" (origin={origin}); refusing to mark an unrecognised"
                " important message handled"
            )
        msg = decode_spawn_message(data)  # ValueError propagates
        if msg["kind"] == "summary":
            self._handle_streamed_summary(origin, msg)
        else:
            self._handle_streamed_spawn_batch(origin, msg, primary_handle)

    def _handle_streamed_spawn_batch(
        self, origin: str, msg: dict, primary_handle: Any,
    ) -> None:
        """Spawn one decoded ``spawn_batch`` onto ``primary_handle``."""
        # Late import keeps planner machinery off the import path in
        # single-process tests that never reach phase 3.
        from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
            headers_from_descriptors,
        )

        if primary_handle is None or not hasattr(
            primary_handle, "spawn_tasks"
        ):
            # Must stay Unhandled (replayed to a future primary with a
            # real handle) rather than silently dropping a batch.
            raise RuntimeError(
                "custom_message_handler: no usable primary_handle"
                f" (got {type(primary_handle).__name__}); cannot spawn"
                f" spawn_batch seq={msg['seq']} from {origin}"
            )
        headers = headers_from_descriptors(msg["descriptors"])
        task_infos = [
            _header_to_task_info(
                header,
                disable_task_deps=self.config.disable_task_deps,
            )
            for header in headers
        ]
        self._logger.info(
            "custom_message_handler: spawn_batch seq=%d from %s;"
            " spawning %d task(s)",
            msg["seq"], origin, len(task_infos),
        )
        errors = primary_handle.spawn_tasks(task_infos) or []
        self._log_spawn_errors(errors, headers)
        self._streamed_spawned_count += len(task_infos)

    def _handle_streamed_summary(self, origin: str, msg: dict) -> None:
        """Record (or reconcile a redelivery of) the terminal summary."""
        total = msg["total"]
        batches = msg["batches"]
        counters = msg["counters"]
        if self._streamed_expected_total is not None:
            if (
                self._streamed_expected_total,
                self._streamed_summary_batches,
                self._streamed_summary_counters,
            ) == (total, batches, counters):
                # Framework redelivery edge (e.g. replay after
                # promotion): an identical duplicate is harmless.
                self._logger.info(
                    "custom_message_handler: duplicate identical"
                    " dependency_graph summary from %s (total=%d);"
                    " ignoring",
                    origin, total,
                )
                return
            raise RuntimeError(
                "conflicting dependency_graph summary from"
                f" {origin}: recorded"
                f" total={self._streamed_expected_total}"
                f" batches={self._streamed_summary_batches}"
                f" counters={self._streamed_summary_counters};"
                f" new total={total} batches={batches}"
                f" counters={counters}"
            )
        self._streamed_expected_total = total
        self._streamed_summary_batches = batches
        self._streamed_summary_counters = dict(counters)
        self._logger.info(
            "custom_message_handler: dependency_graph summary from %s:"
            " total=%d batches=%d counters=%s",
            origin, total, batches, counters,
        )

    def _log_spawn_errors(
        self, errors: Iterable, headers: list[ManifestHeader],
    ) -> None:
        """Port of the old watcher's per-error WARN diagnostics.

        ``primary_handle.spawn_tasks`` returns a list of
        ``(idx, error_dict)`` pairs (empty = all spawned cleanly).
        Each entry is logged at WARN with severity-specific context:

        * ``duplicate_task_hash`` — the planner re-emitted a hash the
          framework already holds (the framework does NOT silently
          re-spawn Failed / Unfulfillable entries on duplicate hash).
        * ``unknown_dependency`` — the graph builder produced a dep
          that doesn't exist (real bug); logged with ``dep_task_id``.
        * any other ``kind`` — logged with the raw dict.
        """
        for idx_err in errors:
            try:
                idx, err = idx_err
            except (TypeError, ValueError):
                self._logger.warning(
                    "spawn_tasks: malformed spawn error entry %r;"
                    " skipping",
                    idx_err,
                )
                continue
            kind = err.get("kind") if isinstance(err, dict) else ""
            task_hash = err.get("task_hash") if isinstance(err, dict) else ""
            offending = headers[idx] if 0 <= idx < len(headers) else None
            offending_name = offending.name if offending is not None else "?"
            if kind == "duplicate_task_hash":
                self._logger.warning(
                    "spawn_tasks: duplicate task_hash for"
                    " header %s (idx=%d task_hash=%s)",
                    offending_name, idx, task_hash,
                )
            elif kind == "unknown_dependency":
                dep_task_id = (
                    err.get("dep_task_id") if isinstance(err, dict) else ""
                )
                self._logger.warning(
                    "spawn_tasks: unknown dependency for"
                    " header %s (idx=%d task_hash=%s dep_task_id=%s)",
                    offending_name, idx, task_hash, dep_task_id,
                )
            else:
                self._logger.warning(
                    "spawn_tasks: unrecognised error kind"
                    " for header %s (idx=%d err=%r)",
                    offending_name, idx, err,
                )

    # ── Broadcast-receive placement gossip ────────────────────────────

    def _make_broadcast_record_self_has(
        self,
        peer_watcher: PeerListWatcher,
        public_key: str,
    ) -> Any:
        """Return a callable suitable for ``PeerPushServer.record_broadcast_self_has``.

        The returned function calls :func:`peer_paths.record_self_has`
        with ``item_class=ITEM_CLASS_MATRIX_EVAL_DRV`` and the live
        peer list from *peer_watcher*. Extracted as a method so the
        callable assembly is unit-testable without spinning up the
        full :meth:`on_run_start` lifecycle.

        Exceptions raised by ``record_self_has`` are caught + logged
        (gossip is best-effort; a failed write must not propagate into
        the handshake response back to the originator).
        """
        my_sid = self.config.secondary_id
        shared_fs = self.config.shared_fs
        peer_watcher_ref = peer_watcher
        bound_pubkey = str(public_key)

        def _record(path: str) -> None:
            try:
                peer_paths.record_self_has(
                    shared_fs,
                    my_secondary_id=my_sid,
                    outpath=path,
                    drv_path=path,
                    item_class=peer_paths.ITEM_CLASS_MATRIX_EVAL_DRV,
                    peers=list(peer_watcher_ref.peers),
                    our_pubkey=bound_pubkey,
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "broadcast record_self_has failed for %s", path,
                )

        return _record

    # ==================================================================
    # Legacy in-process dispatch surface
    #
    # The single-process CLI still uses these to drive a hermetic test
    # of the pipeline without spinning up the framework's worker pool.
    # New deployments go through the framework's get_phases /
    # discover_items / per-worker subprocess machinery.
    # ==================================================================

    def setup_peer_cache(self) -> None:
        """Legacy alias for :meth:`on_run_start`."""
        self.on_run_start()

    def teardown(self) -> None:
        """Legacy alias for :meth:`on_run_end`."""
        self.on_run_end(True)

    def find_binaries(self, input_dir: Optional[pathlib.Path] = None) -> list:
        """Legacy in-process discovery — wraps :meth:`discover_items`."""
        return list(self.discover_items(source_dir=input_dir))

    def dispatch_binary(
        self,
        binary_info,
        output_dir: Optional[pathlib.Path] = None,
        **kwargs: Any,
    ) -> None:
        """Read ``binary_info.path`` and route to the right worker.

        Legacy entry point used by the single-process CLI; new
        deployments dispatch through the framework's per-worker
        subprocess factory.
        """
        del output_dir, kwargs
        path = pathlib.Path(getattr(binary_info, "path"))
        try:
            header = read_manifest(path)
        except Exception as exc:  # noqa: BLE001 — log + degrade
            self._logger.exception(
                "dispatch_binary: failed to read manifest %s: %s", path, exc
            )
            return

        try:
            self._dispatch_item(header.item_class, path, header)
        except Exception:  # noqa: BLE001 — never raise out
            self._logger.exception(
                "dispatch_binary: worker raised for %s (%s)",
                path,
                header.item_class,
            )

    def _dispatch_item(
        self,
        item_class: str,
        path: pathlib.Path,
        header: ManifestHeader,
    ) -> None:
        """Route to the right worker. May raise; caller swallows."""
        del header  # current workers re-read the manifest themselves
        if item_class in _BUILD_DISPATCH_CLASSES:
            env = BuildWorkerEnv(
                flake_ref=self.config.flake_ref,
                dataset_output_dir=self.config.dataset_dir,
                substituters_file=self._substituters_file_path(),
                shared_fs=self.config.shared_fs,
                secondary_id=self.config.secondary_id,
                placement_watcher=self._placement_watcher,
                peer_watcher=self._peer_watcher,
                signing_public_key=(
                    self._signing_key.public_key
                    if self._signing_key is not None
                    else ""
                ),
            )
            self._build_worker(path, env)
            return

        raise ValueError(
            f"unknown item_class {item_class!r} in manifest {path}"
        )

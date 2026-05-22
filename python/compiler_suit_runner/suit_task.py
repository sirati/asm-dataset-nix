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
* ``dependency_graph`` (phase 3) — framework task, one per binary;
  ``task_depends_on=[matrix_eval__<binary>]`` so the CRDT activates
  each dep_graph task atomically with the matching matrix_eval
  TaskCompleted apply. The worker reads the matrix_aggregate drv
  path from ``task.predecessor_outputs[matrix_eval__<binary>]
  ["matrix_aggregate_drv"]["value"]`` (published by the upstream
  matrix_eval task via ``Task.publish_string``), imports the
  per-binary matrix-aggregate drv archive, runs the streaming planner, and emits
  per-(compiler, arch) sidecar manifests at
  ``<matrix_eval_out_dir>/_manifests/`` for the placeholder
  build_variant tasks to consume.
* ``build`` (phase 4) — distributed ``build_common_dep`` +
  ``build_variant`` workers; ``toolchain_validate`` shares the same
  dispatch (rarely emitted, gated by ``--debug-testbuild``).
  ``build_variant`` tasks are declared at submit time as K-sized
  placeholders behind ``dependency_graph__<binary>``; the worker
  resolves each placeholder via its per-cell sidecar slot.

Responsibilities:

1. **Topology** (:meth:`get_phases`) declares the ``matrix_eval``,
   ``build_compilers``, ``dependency_graph`` and ``build`` framework
   phases. All four are first-class PhaseSpecs; the dispatch graph
   between them is encoded as ``depends_on`` tuples so the framework
   schedules them in topological order.
2. **Item discovery** (:meth:`discover_items`) scans the manifest
   directory written by :mod:`compiler_suit_runner.manifest_gen` and
   yields one :class:`TaskInfo` per manifest, classifying each by
   ``item_class`` to ``(phase_id, type_id, affinity_id)``.
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
import json
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
    ManifestHeader,
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
        return ("matrix_eval", "eval", binary)
    if item_class == "dependency_graph":
        # One task per binary; depends on matrix_eval__<binary>. The
        # worker imports that binary's matrix-aggregate drv archive + reads the matrix
        # aggregate drv from ``task.predecessor_outputs[
        # matrix_eval__<binary>]["matrix_aggregate_drv"]["value"]``
        # (published via ``Task.publish_string``), runs the streaming
        # planner, and writes per-(compiler, arch) sidecar manifests
        # for the placeholder build_variant tasks (see plan:
        # placeholder-pattern-restructure PH-A). Affinity = binary so
        # the framework keeps the import closure warm on the worker
        # that just finished matrix_eval.
        binary = header.payload.get("binary", "?")
        return ("dependency_graph", "dep_graph", binary)
    if item_class == "build_compilers":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return ("build_compilers", "build_compilers", f"{compiler}-{arch}")
    # All remaining build-shaped tasks share the single ``build``
    # phase. Nix's daemon serializes shared dependencies via its
    # build lock, so toolchain validates / common deps land before
    # their dependent variants without explicit per-class phase
    # ordering.
    if item_class == "toolchain_validate":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return ("build", "toolchain_validate", f"{compiler}-{arch}")
    if item_class == "build_common_dep":
        return ("build", "common_dep", None)
    if item_class == "build_variant":
        compiler = header.payload.get("compiler_id", "?")
        arch = header.payload.get("arch", "?")
        return ("build", "variant", f"{compiler}-{arch}")
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
    framework builds round-trip cleanly.
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
    first-class framework PhaseSpec task dispatched by the runner;
    the worker writes ``_dependency_graph.pkl`` under the matrix_eval
    output directory. :meth:`SuitTask.on_phase_end` then reads that
    pickle when ``phase_id == "dependency_graph"``, translates the
    descriptor list to :class:`ManifestHeader` instances via
    :func:`dependency_graph_planner.headers_from_descriptors`, and
    hands them to :meth:`_MatrixEvalQuiesceWatcher._spawn_tasks`
    which in turn drives ``primary_handle.spawn_tasks`` for the
    ``build`` phase. The spawn fan-out stays primary-affined because
    "task creation can only be done by the manager (primary)" is
    still a framework invariant.
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
            phase_id="build_compilers",
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
            phase_id="matrix_eval",
            depends_on=("build_compilers",),
            types=(
                TaskTypeSpec(
                    type_id="eval",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id="dependency_graph",
            depends_on=("matrix_eval",),
            types=(
                TaskTypeSpec(
                    type_id="dep_graph",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id="build",
            depends_on=("dependency_graph",),
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
      Possible kinds: ``"keepalive_miss"``, ``"mass_death_escalation"``,
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
# matrix_eval quiesce watcher
# ---------------------------------------------------------------------------


class _MatrixEvalQuiesceWatcher:
    """Wait for every ``matrix_eval__<binary>`` task to complete.

    The framework drives phase-3 via its own ``dependency_graph``
    PhaseSpec; this watcher only records matrix_eval task completions
    so :meth:`SuitTask.on_phase_end` can observe quiesce. The spawn
    primitives :meth:`_spawn_tasks` / :meth:`_header_to_task_info`
    are invoked from :meth:`SuitTask.on_phase_end` after the framework
    reports phase-3 quiesce.

    Calls to :meth:`on_task_completed` are guarded by an internal lock
    so concurrent completions from a worker pool are safe.
    """

    def __init__(
        self,
        expected_task_ids: Iterable[str],
        out_dir: pathlib.Path,
        *,
        logger: Optional[logging.Logger] = None,
        primary_handle: Optional[Any] = None,
    ) -> None:
        self._expected: frozenset[str] = frozenset(expected_task_ids)
        self._completed: set[str] = set()
        self._out_dir = pathlib.Path(out_dir)
        self._primary_handle = primary_handle
        self._lock = threading.Lock()
        self._logger = logger or logging.getLogger(__name__)

    # ── Public read-only inspection (used by tests) ────────────────────

    @property
    def expected(self) -> frozenset[str]:
        return self._expected

    @property
    def completed(self) -> frozenset[str]:
        return frozenset(self._completed)

    # ── Event entry point ──────────────────────────────────────────────

    def on_task_completed(
        self, task_id: str, result: Any = None
    ) -> None:
        """Bookkeeping-only: record one matrix_eval task's completion.

        No-op when:

        * ``task_id`` is empty.
        * ``task_id`` is not in the expected set (the watcher coexists
          with other listeners on the same hook surface).
        * The task_id has already been marked complete (idempotent).
        """
        if not task_id:
            return
        with self._lock:
            if task_id not in self._expected:
                return
            if task_id in self._completed:
                return
            self._completed.add(task_id)

    # ── spawn_tasks dispatch ──────────────────────────────────────────

    def _dump_dependency_graph(
        self, headers: list[ManifestHeader],
    ) -> pathlib.Path:
        """Serialise ``headers`` to ``_dependency_graph_headers.json``
        (the descriptors-as-headers companion file) for offline
        inspection.

        The dependency_graph worker writes the authoritative descriptor
        payload to ``_dependency_graph.pkl`` (pickle, hard cutover).
        This helper emits a parallel ManifestHeader-equivalent JSON
        view under a distinct name so operators eyeballing the spawn
        output see what the framework will consume, without colliding
        with the pickle path.

        Best-effort: an OSError is logged + swallowed so the spawn
        path can still proceed.
        """
        self._out_dir.mkdir(parents=True, exist_ok=True)
        graph_path = self._out_dir / "_dependency_graph_headers.json"
        serialised = [
            {
                "item_class": h.item_class,
                "name": h.name,
                "size": h.size,
                "payload": h.payload,
                "task_id": h.task_id,
                "task_depends_on": list(h.task_depends_on),
            }
            for h in headers
        ]
        try:
            graph_path.write_text(
                json.dumps(serialised, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            self._logger.exception(
                "_MatrixEvalQuiesceWatcher: failed writing %s",
                graph_path,
            )
        return graph_path

    def _spawn_tasks(
        self, headers: list[ManifestHeader]
    ) -> None:
        """Spawn-dispatch path.

        Always overwrites ``_dependency_graph_headers.json`` with the
        ManifestHeader-shaped view of the descriptor list (useful for
        offline inspection / resume). When ``self._primary_handle``
        is bound:

        1. Convert each :class:`ManifestHeader` into a framework
           ``TaskInfo`` via :meth:`_header_to_task_info`.
        2. Call ``primary_handle.spawn_tasks(task_infos)``.
        3. Log spawn count + error count. Per-error log severity:
           - ``duplicate_task_hash`` → WARN (we expect a fresh
             content-hash for every header; a duplicate means we hit
             the framework's idempotent-respawn guard for a hash that
             we believed was new).
           - ``unknown_dependency`` → WARN with the offending
             ``task_hash`` + ``dep_task_id`` (graph builder produced a
             dep that doesn't exist — real bug).
           - any other ``kind`` → WARN with the raw dict.

        When ``self._primary_handle`` is None (single-process tests,
        framework-pin gap) the JSON dump is the only side effect; an
        INFO log explains the degradation.
        """
        graph_path = self._dump_dependency_graph(headers)

        if self._primary_handle is None:
            self._logger.info(
                "_MatrixEvalQuiesceWatcher: primary_handle unbound;"
                " wrote %s (build phase falls back to JSON-only)",
                graph_path,
            )
            return

        task_infos: list = []
        for header in headers:
            try:
                task_infos.append(self._header_to_task_info(header))
            except Exception:  # noqa: BLE001 — log + skip
                self._logger.exception(
                    "_MatrixEvalQuiesceWatcher: header→TaskInfo"
                    " conversion failed for %s (task_id=%s); skipping",
                    header.name,
                    header.task_id,
                )

        if not task_infos:
            self._logger.warning(
                "_MatrixEvalQuiesceWatcher: no valid TaskInfo entries"
                " to spawn (received %d header(s)); wrote %s",
                len(headers),
                graph_path,
            )
            return

        try:
            errors = self._primary_handle.spawn_tasks(task_infos)
        except Exception:  # noqa: BLE001 — log + degrade
            self._logger.exception(
                "_MatrixEvalQuiesceWatcher: primary_handle.spawn_tasks"
                " raised for %d task(s); build phase may stall",
                len(task_infos),
            )
            return

        errors_list = list(errors) if errors is not None else []
        self._logger.info(
            "_MatrixEvalQuiesceWatcher: spawn_tasks dispatched %d"
            " task(s) with %d error(s); wrote %s",
            len(task_infos),
            len(errors_list),
            graph_path,
        )
        for idx_err in errors_list:
            try:
                idx, err = idx_err
            except (TypeError, ValueError):
                self._logger.warning(
                    "_MatrixEvalQuiesceWatcher: malformed spawn error"
                    " entry %r; skipping",
                    idx_err,
                )
                continue
            kind = err.get("kind") if isinstance(err, dict) else ""
            task_hash = err.get("task_hash") if isinstance(err, dict) else ""
            offending = (
                headers[idx] if 0 <= idx < len(headers) else None
            )
            offending_name = offending.name if offending is not None else "?"
            if kind == "duplicate_task_hash":
                # Plan builder produced a hash collision with the
                # ledger. This is a real signal: the framework does
                # NOT silently re-spawn Failed / Unfulfillable entries
                # on duplicate hash, so a hit here means the planner
                # re-emitted a hash we'd already submitted (e.g. on a
                # planner retry). WARN so operators see it.
                self._logger.warning(
                    "_MatrixEvalQuiesceWatcher: spawn_tasks duplicate"
                    " task_hash for header %s (idx=%d task_hash=%s)",
                    offending_name, idx, task_hash,
                )
            elif kind == "unknown_dependency":
                dep_task_id = (
                    err.get("dep_task_id")
                    if isinstance(err, dict)
                    else ""
                )
                self._logger.warning(
                    "_MatrixEvalQuiesceWatcher: spawn_tasks unknown"
                    " dependency for header %s (idx=%d"
                    " task_hash=%s dep_task_id=%s)",
                    offending_name, idx, task_hash, dep_task_id,
                )
            else:
                self._logger.warning(
                    "_MatrixEvalQuiesceWatcher: spawn_tasks unrecognised"
                    " error kind for header %s (idx=%d err=%r)",
                    offending_name, idx, err,
                )

    def _header_to_task_info(self, header: ManifestHeader):
        """Convert one :class:`ManifestHeader` directly into a framework
        ``TaskInfo``. Mirrors the call shape used by
        :func:`_make_task_info` (the disk-round-trip path used by
        :meth:`SuitTask.discover_items`) so spawn-side and discover-side
        items differ only by source, not by encoding.

        The ``payload`` carried on the resulting TaskInfo is the same
        header_dict shape ``discover_items`` emits, so downstream
        workers see a uniform payload regardless of whether the item
        came from preflight or from Phase 1 planning.
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
            task_depends_on=tuple(header.task_depends_on),
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
        # matrix_eval → build quiesce watcher. Populated by
        # on_run_start when matrix_eval items are present in the
        # manifest dir. Reference held so the listener doesn't get
        # GC'd; framework task-completion events drive its
        # on_task_completed method.
        self._matrix_eval_watcher: Optional[_MatrixEvalQuiesceWatcher] = None
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

    @property
    def task_completed_listener(
        self,
    ) -> Callable[[Optional[str], bool, Optional[str]], None]:
        """Return the duck-typed callable the framework picks off
        ``getattr(task, "task_completed_listener", None)``.

        The framework invokes the returned callable on every
        ``TaskCompleted`` / ``TaskFailed`` apply with
        ``(task_id, success, error_kind)``. We forward the event into
        ``self._matrix_eval_watcher.on_task_completed`` when the
        watcher is active AND the task_id is in its expected set;
        non-matching ids (e.g. toolchain completions) and absent-watcher
        conditions are NoOps. The watcher itself is already idempotent
        on duplicate fires, so the dispatch can be invoked freely.

        Exceptions are swallowed so a buggy consumer-side listener can
        not stall the framework's apply path.
        """
        def _dispatch(
            task_id: Optional[str],
            success: bool,
            error_kind: Optional[str],
        ) -> None:
            del success, error_kind  # not consumed by the watcher today
            try:
                watcher = self._matrix_eval_watcher
                if watcher is None or not task_id:
                    return
                if task_id not in watcher.expected:
                    return
                watcher.on_task_completed(task_id)
            except Exception:  # noqa: BLE001 — never raise out
                self._logger.exception(
                    "task_completed_listener: dispatch raised for"
                    " task_id=%s",
                    task_id,
                )
        return _dispatch

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
        """Yield one :class:`TaskInfo` per manifest in the manifest dir.

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
            task_depends_on = (
                () if self.config.disable_task_deps else header.task_depends_on
            )
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
            # disk. Other types (toolchain_validate / common_dep /
            # variant) ignore the flag harmlessly.
            if (
                type_id in {"eval", "dep_graph"}
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
        kwarg. Captured onto ``self._primary_handle`` so subsequent
        matrix_eval → build dispatch via
        ``_MatrixEvalQuiesceWatcher`` can drive
        ``primary_handle.spawn_tasks(...)``. When the kwarg is absent
        (legacy callers, single-process tests) the watcher degrades to
        the JSON-only fallback.
        """
        del source_dir, args  # unused (output_dir consumed below)
        with self._setup_lock:
            if self._setup_done:
                # Late-binding the handle on a re-entry is harmless;
                # the watcher reads ``self._primary_handle`` through
                # the SuitTask reference, so a flip after construction
                # still takes effect for any later spawn fire.
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
                    daemon_log = (
                        pathlib.Path("/app/log-network")
                        / f"nix-daemon-{self.config.secondary_id}.log"
                    )
                    start_nix_daemon(daemon_log)
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: nix-daemon start failed"
                        " (harmonia will likely 500 on store queries)"
                    )
                try:
                    # Log goes on the gateway-readable mount under a
                    # secondary-id-scoped filename (operators read it
                    # from the gateway; other secondaries never write
                    # it). TOML stays container-local under the
                    # default runtime_dir.
                    log_path = (
                        pathlib.Path("/app/log-network")
                        / f"harmonia-{self.config.secondary_id}.log"
                    )
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

            # 6b. matrix_eval → build quiesce watcher.
            #
            # Scan the manifest dir for matrix_eval items. If any
            # exist, spin up a _MatrixEvalQuiesceWatcher whose
            # on_task_completed method is the framework's
            # task-completion hook target. The watcher's
            # ``_spawn_tasks`` is invoked from
            # :meth:`SuitTask.on_phase_end` when the framework signals
            # the ``dependency_graph`` phase has ended; that handler
            # reads the worker-written pickle and fans out the
            # resulting headers into ``primary_handle.spawn_tasks``
            # for the ``build`` phase.
            #
            # We register the watcher best-effort against whatever
            # task-completion surface the framework currently exposes
            # — the consumer drives it directly when no hook is
            # available. The reference is held on ``self`` so it is
            # not garbage-collected mid-run.
            try:
                self._matrix_eval_watcher = self._build_matrix_eval_watcher(
                    output_dir=output_dir,
                )
                if self._matrix_eval_watcher is not None:
                    self._register_matrix_eval_watcher(
                        self._matrix_eval_watcher,
                    )
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: _MatrixEvalQuiesceWatcher setup"
                    " failed; build phase will not auto-fire"
                )
                self._matrix_eval_watcher = None

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

            # Drop the matrix_eval watcher reference; if it never
            # fired (e.g. run aborted mid-matrix_eval) it just gets
            # GC'd. No cleanup work — the watcher owns no threads,
            # files, or sockets of its own.
            self._matrix_eval_watcher = None
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
                        "build_compilers"
                        if header.item_class == "build_compilers"
                        else "build"
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

    def on_phase_end(
        self, phase_id: str, completed: int = 0, failed: int = 0
    ) -> None:
        self._logger.info(
            "phase %s ended: %d completed, %d failed",
            phase_id,
            completed,
            failed,
        )
        if (
            phase_id == "dependency_graph"
            and self._matrix_eval_watcher is not None
        ):
            # Late imports keep planner machinery off the import path
            # in single-process tests that never reach phase 3.
            from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
                headers_from_descriptors,
                load_phase4_descriptors,
            )
            from compiler_suit_runner.workers.dependency_graph_worker.output import (  # noqa: PLC0415
                DEPENDENCY_GRAPH_PICKLE,
            )

            out_dir = self._matrix_eval_watcher._out_dir
            pickle_path = out_dir / DEPENDENCY_GRAPH_PICKLE
            try:
                descriptors, _summary = load_phase4_descriptors(
                    pickle_path
                )
                headers = headers_from_descriptors(descriptors)
                self._logger.info(
                    "on_phase_end(\"dependency_graph\"): loaded %d "
                    "phase-4 descriptors; spawning %d tasks",
                    len(descriptors), len(headers),
                )
                self._matrix_eval_watcher._spawn_tasks(headers)
            except Exception:  # noqa: BLE001 — log + degrade
                self._logger.exception(
                    "on_phase_end: dependency_graph spawn failed at %s",
                    pickle_path,
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

    # ── matrix_eval watcher wiring ────────────────────────────────────

    def _build_matrix_eval_watcher(
        self,
        *,
        output_dir: Optional[pathlib.Path],
    ) -> Optional[_MatrixEvalQuiesceWatcher]:
        """Scan the manifest dir and return a watcher if matrix_eval is active.

        Returns ``None`` (no watcher) when no ``matrix_eval`` manifest
        is present — there's nothing to wait for so the build phase
        can dispatch immediately when the framework gets to it.

        ``config.matrix_eval_out_dir`` (when set) is used as the
        archive root; it overrides ``output_dir`` and the legacy
        ``shared_fs/'out'`` fallback. ``output_dir`` is the
        framework-supplied per-run output directory used only when
        ``matrix_eval_out_dir`` is absent.
        """
        manifest_dir = self.config.manifest_dir
        try:
            entries = sorted(manifest_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return None

        expected_ids: set[str] = set()

        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.startswith((".", "_")):
                continue
            if entry.suffix != ".json":
                continue
            try:
                header = read_manifest(entry)
            except Exception:  # noqa: BLE001 — corrupt manifest
                continue
            if header.item_class == "matrix_eval":
                binary = header.payload.get("binary", "")
                if isinstance(binary, str) and binary:
                    expected_ids.add(
                        header.task_id or matrix_eval_task_id(binary)
                    )

        if not expected_ids:
            return None

        resolved_out_dir = (
            self.config.matrix_eval_out_dir
            if self.config.matrix_eval_out_dir is not None
            else (
                pathlib.Path(output_dir)
                if output_dir is not None
                else self.config.shared_fs / "out"
            )
        )

        return _MatrixEvalQuiesceWatcher(
            expected_task_ids=expected_ids,
            out_dir=resolved_out_dir,
            primary_handle=self._primary_handle,
            logger=self._logger,
        )

    def _register_matrix_eval_watcher(
        self, watcher: _MatrixEvalQuiesceWatcher
    ) -> None:
        """Best-effort wire the watcher onto the framework's hook surface.

        The framework's task-completion event seam is still in flight.
        Try a few duck-typed registration surfaces in priority order;
        if none exist the watcher remains callable from the consumer's
        own dispatch loop (legacy single-process CLI drives it
        directly). Either way we hold the reference on ``self`` so it
        isn't GC'd.
        """
        try:
            from dynamic_runner import run as dynrunner_run  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 — framework absent
            self._logger.debug(
                "_register_matrix_eval_watcher: dynamic_runner.run not"
                " importable; watcher attached to SuitTask only"
            )
            return

        for attr in ("register_task_completed_listener",
                     "add_task_completed_listener",
                     "on_task_completed"):
            hook = getattr(dynrunner_run, attr, None)
            if callable(hook):
                try:
                    hook(watcher.on_task_completed)
                    self._logger.info(
                        "_register_matrix_eval_watcher: wired via"
                        " dynamic_runner.run.%s",
                        attr,
                    )
                    return
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "_register_matrix_eval_watcher: %s raised", attr,
                    )
        self._logger.info(
            "_register_matrix_eval_watcher: framework task_completed"
            " hook not found; watcher reachable via"
            " SuitTask._matrix_eval_watcher.on_task_completed"
        )

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

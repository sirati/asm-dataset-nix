"""The single ``TaskDefinition`` that orchestrates the compiler-suit run.

The dynamic_runner framework now exposes phases as first-class
:class:`PhaseSpec` (with declared dependencies) and per-phase task
types as :class:`TaskTypeSpec`. :class:`SuitTask` implements that
Protocol:

1. **Topology** (:meth:`get_phases`) declares the four phases
   ``phase1a → phase1b → phase2 → phase3`` and their per-phase
   :class:`TaskTypeSpec`\\ s; each type binds a worker module and a
   per-type memory estimator.
2. **Item discovery** (:meth:`discover_items`) scans the manifest
   directory written by :mod:`compiler_suit_runner.manifest_gen` and
   yields one :class:`TaskInfo` per manifest, classifying each by
   ``item_class`` to ``(phase_id, type_id, affinity_id)``.
3. **Per-type plumbing** (:meth:`estimate_memory` plus per-type
   estimators, :meth:`build_worker_command_args`,
   :meth:`get_output_filename_pattern`) wires manifests onto the
   subprocess workers added in 8.4.
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
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, Optional

from compiler_suit_runner.cachix_uploader import (
    CachixUploader,
    UploaderConfig,
)
from compiler_suit_runner import phase1_planner
from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    phase0_eval_task_id,
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
from compiler_suit_runner.workers.merge_worker import (
    MergeWorkerEnv,
    merge_worker,
)
from compiler_suit_runner.workers.partition_worker import (
    WorkerEnv as PartitionWorkerEnv,
    partition_worker,
)


__all__ = [
    "SuitTaskConfig",
    "SuitTask",
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


# Item classes that route through the build worker.
_PHASE2_BUILD_CLASSES: frozenset[str] = frozenset(
    {"phase2_toolchain", "phase2_toolchain_validate", "phase2_common_dep"}
)


def _classify(header: ManifestHeader) -> tuple[str, str, Optional[str]]:
    """Map a :class:`ManifestHeader` to ``(phase_id, type_id, affinity_id)``.

    The mapping is the single source of truth for which dynamic_runner
    phase / type / affinity bucket each consumer ``item_class`` lands
    in. ``affinity_id`` is non-None for the two compiler-bound types
    (phase-2 toolchains and phase-3 variants) so the framework
    co-locates a toolchain build and the variants that depend on it
    onto the same worker for kernel page-cache reuse.
    """
    item_class = header.item_class
    if item_class == "phase0_eval":
        # Phase 0 distributed-eval: one task per binary. Pinned by
        # binary so we get a stable affinity bucket per package
        # (handy for log grepping; framework just treats it as a
        # tag).
        binary = header.payload.get("binary", "?")
        return ("phase0", "eval", binary)
    if item_class == "phase1a_partition":
        return ("phase1a", "partition", None)
    if item_class == "phase1b_merge":
        return ("phase1b", "merge", None)
    # All build-shaped tasks (toolchain, common_dep, variant) share
    # the single ``phase_build`` phase. Nix's daemon serializes
    # shared dependencies via its build lock, so toolchain builds
    # complete-or-substitute before their dependent variants finish
    # their own build call — no explicit phase ordering needed.
    if item_class == "phase2_toolchain":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return ("phase_build", "toolchain", f"{compiler}-{arch}")
    if item_class == "phase2_toolchain_validate":
        # Validate-only items use a dedicated type_id so the
        # build_worker can branch on it (fetch-from-peer instead of
        # nix-build) without having to inspect the manifest payload.
        # Affinity follows the build variant for cache reuse: the
        # secondary that pulls a toolchain is the natural candidate
        # to pick up the variants that depend on it.
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return ("phase_build", "toolchain_validate", f"{compiler}-{arch}")
    if item_class == "phase2_common_dep":
        return ("phase_build", "common_dep", None)
    if item_class == "phase3_variant":
        compiler = header.payload.get("compiler_id", "?")
        arch = header.payload.get("arch", "?")
        return ("phase_build", "variant", f"{compiler}-{arch}")
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
    for the three build-heavy types (toolchain, common_dep, variant);
    partition and merge are always uncapped (cheap, IO-bound). ``None``
    leaves all types unconstrained.
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

    # Single phase: toolchain + common_dep + variant all dispatched
    # together. Nix's daemon naturally serializes shared dependencies
    # via its build lock — when worker A starts a variant whose
    # toolchain isn't built yet, the nix-build call walks the drv
    # graph, builds (or substitutes) the toolchain, then the variant.
    # Worker B picking up the toolchain task at the same time hits
    # the same daemon lock and either waits or substitutes the
    # finished result. With harmonia federation between secondaries,
    # the first toolchain build is amortized across the fleet.
    #
    # The artificial phase-2 → phase-3 boundary used to gate variants
    # on all toolchains finishing first, which created a stall in
    # the dispatch pipeline (no continuous tarball production until
    # every toolchain was done) and a known cascade trigger when
    # secondaries idled at the boundary. Collapsing to one phase
    # lets the workload flow continuously: toolchains finish, their
    # downstream variants unblock via the nix dep graph, tarballs
    # emerge as soon as their full closure is realized.
    # ``toolchain_validate`` is uncapped: the work is a path-info
    # probe + at most one ``nix copy`` per item, so the build-heavy
    # cap (which targets nix-build oversubscription) doesn't apply.
    # Keeping it uncapped also avoids starving phase-3 variants
    # behind the validate phase when the same cap is configured low
    # for compile-throttling.
    return (
        PhaseSpec(
            phase_id="phase_build",
            types=(
                TaskTypeSpec(
                    type_id="toolchain",
                    worker_module="compiler_suit_runner.workers.build_worker",
                    **build_kwargs,
                ),
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
# Phase 0 quiesce watcher
# ---------------------------------------------------------------------------


class _Phase0QuiesceWatcher:
    """Wait for every ``phase0_eval_<binary>`` task to complete, then plan.

    The watcher is registered as a task-completion listener at
    ``on_run_start``. Each call to :meth:`on_task_completed` either
    ignores a non-phase-0 task_id or marks the matching phase-0 task
    as complete. Once the completed set covers the expected set, the
    watcher fires :func:`phase1_planner.plan_phase1` exactly once and
    hands the resulting :class:`ManifestHeader` list to
    ``spawn_tasks``.

    The ``spawn_tasks`` callback today is a stub: it serialises the
    header list to ``out_dir / "_phase1_graph.json"`` and emits an INFO
    log line with the header count. When the framework's Q5
    ``primary.spawn_tasks`` API lands the stub gets swapped out for the
    real dispatch (see TODO inline).

    The watcher is NOT a framework task — it runs entirely in-process
    on whichever thread the framework's task-completion notification is
    delivered on. Calls to :meth:`on_task_completed` are guarded by an
    internal lock so concurrent completions from a worker pool are safe.
    """

    def __init__(
        self,
        expected_task_ids: Iterable[str],
        out_dir: pathlib.Path,
        toolchain_task_ids: dict[str, str],
        *,
        logger: Optional[logging.Logger] = None,
        sys_name: str = "x86_64-linux",
    ) -> None:
        self._expected: frozenset[str] = frozenset(expected_task_ids)
        self._completed: set[str] = set()
        self._out_dir = pathlib.Path(out_dir)
        self._toolchain_task_ids: dict[str, str] = dict(toolchain_task_ids)
        self._sys_name = sys_name
        self._fired: bool = False
        self._lock = threading.Lock()
        self._logger = logger or logging.getLogger(__name__)

    # ── Public read-only inspection (used by tests) ────────────────────

    @property
    def expected(self) -> frozenset[str]:
        return self._expected

    @property
    def completed(self) -> frozenset[str]:
        return frozenset(self._completed)

    @property
    def fired(self) -> bool:
        return self._fired

    # ── Event entry point ──────────────────────────────────────────────

    def on_task_completed(
        self, task_id: str, result: Any = None
    ) -> None:
        """Process one task-completion notification.

        ``result`` is forwarded by the framework's hook (eventual API);
        we don't currently inspect it because the Phase 0 worker writes
        its manifest to ``out_dir/<binary>/_phase0/manifest.json`` and
        the planner re-reads them. The result slot is kept on the
        signature so the framework's call site doesn't need a wrapper.

        No-op when:

        * ``task_id`` is not a phase-0 task_id (e.g. a toolchain task
          completion arrives — the watcher coexists with K=3 listeners
          on the same hook).
        * The watcher has already fired (``self._fired``).
        * The task_id has already been marked complete (idempotent).
        """
        del result  # see docstring
        if not task_id:
            return
        with self._lock:
            if self._fired:
                return
            if task_id not in self._expected:
                return
            if task_id in self._completed:
                return
            self._completed.add(task_id)
            if self._completed < self._expected:
                # Still waiting on more phase 0 tasks.
                return
            self._fired = True
            should_fire = True
        if should_fire:
            self._fire()

    # ── Plan invocation ────────────────────────────────────────────────

    def _fire(self) -> None:
        """Re-read all phase-0 manifests and call ``plan_phase1``.

        Swallows planner exceptions and logs them: the framework's
        task-completion thread should not raise out into the
        scheduler. A planner failure leaves Phase 1 unscheduled (the
        run will then stall on the build phase — operator-visible).
        """
        try:
            phase0_manifests = phase1_planner.read_phase0_manifests(
                self._out_dir
            )
        except Exception:  # noqa: BLE001 — log + degrade
            self._logger.exception(
                "_Phase0QuiesceWatcher: read_phase0_manifests failed"
                " for %s; Phase 1 will not be scheduled",
                self._out_dir,
            )
            return

        self._logger.info(
            "_Phase0QuiesceWatcher: all %d phase-0 tasks complete;"
            " loaded %d manifests; calling plan_phase1",
            len(self._expected),
            len(phase0_manifests),
        )

        try:
            phase1_planner.plan_phase1(
                phase0_manifests,
                self._toolchain_task_ids,
                self._spawn_tasks_stub,
                sys_name=self._sys_name,
            )
        except Exception:  # noqa: BLE001 — log + degrade
            self._logger.exception(
                "_Phase0QuiesceWatcher: plan_phase1 raised; Phase 1"
                " not scheduled"
            )

    # ── spawn_tasks stub ───────────────────────────────────────────────

    def _spawn_tasks_stub(
        self, headers: list[ManifestHeader]
    ) -> None:
        """Serialise ``headers`` to ``_phase1_graph.json`` + log count.

        TODO(Q5): once the framework exposes
        ``primary.spawn_tasks(...)`` replace this stub with a thin
        adapter that translates :class:`ManifestHeader` records into
        the framework's ``TaskInfo`` shape and calls the API. The
        graph dump stays useful for offline inspection / resume.
        """
        self._out_dir.mkdir(parents=True, exist_ok=True)
        graph_path = self._out_dir / "_phase1_graph.json"
        # Serialise to a JSON-safe shape — ManifestHeader is a frozen
        # dataclass, so we round-trip via dict.
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
            import json
            graph_path.write_text(
                json.dumps(serialised, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            self._logger.exception(
                "_Phase0QuiesceWatcher: failed writing %s",
                graph_path,
            )
        self._logger.info(
            "_Phase0QuiesceWatcher: spawn_tasks stub received %d"
            " header(s); wrote %s (Q5 not yet wired)",
            len(headers),
            graph_path,
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
    raw_partition_dir: pathlib.Path
    partition_dir: pathlib.Path
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
    input_hash: str = ""
    toolchain_drvs: frozenset[str] = frozenset()
    common_threshold: int = 10
    variants: tuple = ()


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
        # Phase 0 drv broadcast consumer (mirrors ReplicationReceiver but
        # for the all-peers flood-fill protocol). Without this the
        # ``/peer/path-broadcast-offer`` endpoint sees deduped offers
        # land on a no-op callback that returns False, so the cluster
        # never actually fetches the broadcast drvs.
        self._broadcast_receiver: Optional[BroadcastReceiver] = None
        # Phase 0 → Phase 1 quiesce watcher. Populated by on_run_start
        # only when --distributed-eval is on (i.e. phase0_eval items
        # are present in the manifest dir). Reference held so the
        # listener doesn't get GC'd; framework task-completion events
        # drive its on_task_completed method (best-effort hook
        # registration; until the framework's task_completed surface
        # lands the consumer drives it from its own dispatch loop).
        self._phase0_watcher: Optional[_Phase0QuiesceWatcher] = None
        self._setup_done: bool = False
        self._setup_lock = threading.Lock()

    # ── Worker-function injection seams (used by tests) ────────────────

    @property
    def _partition_worker(self):
        return partition_worker

    @property
    def _merge_worker(self):
        return merge_worker

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
        if type_id == "partition":
            return common + [
                "--raw-partition-dir",
                str(self.config.raw_partition_dir),
                "--flake-ref",
                self.config.flake_ref,
            ]
        if type_id == "merge":
            return common + [
                "--raw-partition-dir",
                str(self.config.raw_partition_dir),
                "--partition-dir",
                str(self.config.partition_dir),
                "--input-hash",
                self.config.input_hash,
                "--common-threshold",
                str(self.config.common_threshold),
            ]
        if type_id in {"toolchain", "toolchain_validate", "common_dep", "variant"}:
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
            # authenticates the ``path-have`` push fan-out.
            if self.config.shared_fs is not None:
                argv += ["--shared-fs", str(self.config.shared_fs)]
            if self.config.secondary_id:
                argv += ["--secondary-id", self.config.secondary_id]
            if self._signing_key is not None:
                argv += [
                    "--signing-public-key", self._signing_key.public_key,
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
    ) -> None:
        """Bring up peer-cache state. Idempotent."""
        del source_dir, args  # unused (output_dir consumed below)
        with self._setup_lock:
            if self._setup_done:
                return

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
                    )
                    self._replication_sender = ReplicationSender(repl_ctx)
                    self._replication_receiver = ReplicationReceiver(
                        repl_ctx, self._replication_sender,
                    )
                    self._replication_repair = ReplicationRepairWorker(
                        repl_ctx, self._replication_sender,
                    )
                    # Fallback wiring: placement-diff drives repair.
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

                # Phase 0 broadcast consumer (path-broadcast-offer).
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
                        " phase 0 drv flood-fill consumer disabled"
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
                    # ``item_class="phase0_eval_drv"`` so the
                    # placement-map watcher on every other secondary
                    # learns we hold it. Mirrors the K=3 receiver's
                    # post-fetch ``record_self_has`` call but with the
                    # phase0 item-class so phase0 drv holders are
                    # distinguishable from toolchain / variant holders.
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

            # 6b. Phase 0 → Phase 1 quiesce watcher.
            #
            # Scan the manifest dir for phase0_eval items. If any exist,
            # spin up a _Phase0QuiesceWatcher whose on_task_completed
            # method is the framework's task-completion hook target. The
            # watcher fires phase1_planner.plan_phase1 once all phase 0
            # tasks have completed and dumps the resulting graph to
            # ``output_dir / "_phase1_graph.json"`` (Q5 stub).
            #
            # We register the watcher best-effort against whatever
            # task-completion surface the framework currently exposes
            # (none in 2552f7c — the consumer drives it directly until
            # Q5 lands). The reference is held on ``self`` so it is
            # not garbage-collected mid-run.
            try:
                self._phase0_watcher = self._build_phase0_watcher(
                    output_dir=output_dir,
                )
                if self._phase0_watcher is not None:
                    self._register_phase0_watcher(self._phase0_watcher)
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: _Phase0QuiesceWatcher setup failed;"
                    " Phase 1 planner will not auto-fire"
                )
                self._phase0_watcher = None

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

            # Drop the phase 0 watcher reference; if it never fired
            # (e.g. run aborted mid-Phase-0) it just gets GC'd. No
            # cleanup work — the watcher owns no threads, files, or
            # sockets of its own.
            self._phase0_watcher = None
            self._signing_key = None
            self._setup_done = False

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

    # ── Broadcast-receive placement gossip ────────────────────────────

    def _make_broadcast_record_self_has(
        self,
        peer_watcher: PeerListWatcher,
        public_key: str,
    ) -> Any:
        """Return a callable suitable for ``PeerPushServer.record_broadcast_self_has``.

        The returned function calls :func:`peer_paths.record_self_has`
        with ``item_class=ITEM_CLASS_PHASE0_EVAL_DRV`` and the live
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
                    item_class=peer_paths.ITEM_CLASS_PHASE0_EVAL_DRV,
                    peers=list(peer_watcher_ref.peers),
                    our_pubkey=bound_pubkey,
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "broadcast record_self_has failed for %s", path,
                )

        return _record

    # ── Phase 0 watcher wiring ────────────────────────────────────────

    def _build_phase0_watcher(
        self,
        *,
        output_dir: Optional[pathlib.Path],
    ) -> Optional[_Phase0QuiesceWatcher]:
        """Scan the manifest dir and return a watcher if Phase 0 is active.

        Returns ``None`` (no watcher) when no ``phase0_eval`` manifest is
        present — that's the legacy-eval path (``--distributed-eval``
        off) and there's nothing to wait for.

        ``output_dir`` is the framework-supplied per-run output
        directory; the planner reads ``out/<binary>/_phase0/
        manifest.json`` under it and dumps ``_phase1_graph.json`` into
        it. When the framework doesn't pass one we fall back to
        ``config.shared_fs / 'out'`` so the layout stays consistent
        with what the workers write.
        """
        manifest_dir = self.config.manifest_dir
        try:
            entries = sorted(manifest_dir.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return None

        expected_ids: set[str] = set()
        toolchain_task_ids: dict[str, str] = {}

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
            if header.item_class == "phase0_eval":
                binary = header.payload.get("binary", "")
                if isinstance(binary, str) and binary:
                    expected_ids.add(
                        header.task_id or phase0_eval_task_id(binary)
                    )
                continue
            if header.item_class in (
                "phase2_toolchain",
                "phase2_toolchain_validate",
            ):
                if not header.task_id:
                    continue
                # The planner keys toolchain_task_ids by drv path —
                # the same identifier a variant's ``inputDrvs`` set
                # will reference. Preflight stamps that drv on the
                # toolchain header's payload under ``drv``; fall back
                # to the legacy ``outpath`` only when ``drv`` is
                # missing (older manifests).
                drv = header.payload.get("drv") or header.payload.get(
                    "drv_path"
                )
                if isinstance(drv, str) and drv:
                    toolchain_task_ids[drv] = header.task_id

        if not expected_ids:
            return None

        resolved_out_dir = (
            pathlib.Path(output_dir)
            if output_dir is not None
            else self.config.shared_fs / "out"
        )

        return _Phase0QuiesceWatcher(
            expected_task_ids=expected_ids,
            out_dir=resolved_out_dir,
            toolchain_task_ids=toolchain_task_ids,
            logger=self._logger,
            sys_name=self.config.sys_name,
        )

    def _register_phase0_watcher(
        self, watcher: _Phase0QuiesceWatcher
    ) -> None:
        """Best-effort wire the watcher onto the framework's hook surface.

        The framework's task-completion event seam is still in flight
        (see Q4 / pending PRs). Try a few duck-typed registration
        surfaces in priority order; if none exist the watcher remains
        callable from the consumer's own dispatch loop (legacy single-
        process CLI drives it directly). Either way we hold the
        reference on ``self`` so it isn't GC'd.
        """
        # Candidate surfaces, in order of preference. Each is a tuple
        # of (object_attr, method_name, arity-tag) — duck-checked.
        # ``arity-tag`` documents what we'd pass; not used in code.
        try:
            from dynamic_runner import run as dynrunner_run  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 — framework absent
            self._logger.debug(
                "_register_phase0_watcher: dynamic_runner.run not"
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
                        "_register_phase0_watcher: wired via"
                        " dynamic_runner.run.%s",
                        attr,
                    )
                    return
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "_register_phase0_watcher: %s raised", attr,
                    )
        self._logger.info(
            "_register_phase0_watcher: framework task_completed hook"
            " not found; watcher reachable via"
            " SuitTask._phase0_watcher.on_task_completed"
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
        if item_class == "phase1a_partition":
            env = PartitionWorkerEnv(
                raw_partition_dir=self.config.raw_partition_dir,
                flake_ref=self.config.flake_ref,
            )
            self._partition_worker(path, env)
            return

        if item_class == "phase1b_merge":
            env = MergeWorkerEnv(
                raw_partition_dir=self.config.raw_partition_dir,
                partition_dir=self.config.partition_dir,
                input_hash=self.config.input_hash,
                variants=tuple(self.config.variants),
                toolchain_drvs=frozenset(self.config.toolchain_drvs),
                common_threshold=self.config.common_threshold,
            )
            self._merge_worker(path, env)
            return

        if item_class in _PHASE2_BUILD_CLASSES or item_class == "phase3_variant":
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

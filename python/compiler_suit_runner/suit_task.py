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
from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    read_manifest,
)
from compiler_suit_runner.peer_cache import (
    SUBSTITUTERS_FILENAME,
    HarmoniaProcess,
    PeerInfo,
    PeerListWatcher,
    SigningKey,
    announce_self,
    generate_signing_key,
    withdraw_self,
)
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


# Item classes that route through the build worker.
_PHASE2_BUILD_CLASSES: frozenset[str] = frozenset(
    {"phase2_toolchain", "phase2_common_dep"}
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
    if item_class == "phase1a_partition":
        return ("phase1a", "partition", None)
    if item_class == "phase1b_merge":
        return ("phase1b", "merge", None)
    if item_class == "phase2_toolchain":
        compiler = header.payload.get("compiler_label", "?")
        arch = header.payload.get("arch", "?")
        return ("phase2", "toolchain", f"{compiler}-{arch}")
    if item_class == "phase2_common_dep":
        return ("phase2", "common_dep", None)
    if item_class == "phase3_variant":
        compiler = header.payload.get("compiler_id", "?")
        arch = header.payload.get("arch", "?")
        return ("phase3", "variant", f"{compiler}-{arch}")
    raise ValueError(f"unknown item_class {item_class!r}")


def _make_task_info(
    path: pathlib.Path,
    size: int,
    *,
    phase_id: str,
    type_id: str,
    affinity_id: Optional[str],
    payload: dict,
):
    """Return a framework-compatible :class:`TaskInfo`, falling back to a stub.

    When :mod:`dynamic_runner` is importable we use the real
    :class:`TaskInfo`; otherwise we synthesise an attribute-compatible
    stub via :class:`types.SimpleNamespace` so unit tests run without
    the framework on ``sys.path``.
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
        )

    identifier = BinaryIdentifier(
        binary_name=path.name,
        platform="manifest",
        compiler="manifest",
        version="0",
        opt_level="manifest",
    )
    return TaskInfo(
        path=path,
        size=size,
        identifier=identifier,
        phase_id=phase_id,
        type_id=type_id,
        affinity_id=affinity_id,
        payload=dict(payload),
    )


def _phase_specs():
    """Build the framework :class:`PhaseSpec` tuple for this run.

    Implemented as a function (rather than a module-level constant) so
    importing this module never fails when ``dynamic_runner`` is
    absent — the framework's :class:`PhaseSpec` and :class:`TaskTypeSpec`
    are imported lazily here and only matter at run time.
    """
    from dynamic_runner.task_protocol import (  # type: ignore[import-not-found]
        PhaseSpec,
        TaskTypeSpec,
    )

    # Memory budgeting is disabled for this task: every TaskTypeSpec
    # uses the default ``estimator_attr = "estimate_memory"`` which
    # resolves to :meth:`SuitTask.estimate_memory` and returns a
    # 1-byte constant. The framework's resource scheduler then treats
    # all items as zero-cost and packs purely by ``--jobs N``.
    return (
        PhaseSpec(
            phase_id="phase1a",
            types=(
                TaskTypeSpec(
                    type_id="partition",
                    worker_module="compiler_suit_runner.workers.partition_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id="phase1b",
            depends_on=("phase1a",),
            types=(
                TaskTypeSpec(
                    type_id="merge",
                    worker_module="compiler_suit_runner.workers.merge_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id="phase2",
            depends_on=("phase1b",),
            types=(
                TaskTypeSpec(
                    type_id="toolchain",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
                TaskTypeSpec(
                    type_id="common_dep",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
        PhaseSpec(
            phase_id="phase3",
            depends_on=("phase2",),
            types=(
                TaskTypeSpec(
                    type_id="variant",
                    worker_module="compiler_suit_runner.workers.build_worker",
                ),
            ),
        ),
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
        self._cachix_uploader: Optional[CachixUploader] = None
        self._harmonia: Optional[HarmoniaProcess] = None
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
        return self.config.peers_dir / SUBSTITUTERS_FILENAME

    # ── Topology ───────────────────────────────────────────────────────

    def get_phases(self):
        """Return the four-phase :class:`PhaseSpec` graph."""
        return _phase_specs()

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
            yield _make_task_info(
                entry,
                header.size,
                phase_id=phase_id,
                type_id=type_id,
                affinity_id=affinity_id,
                payload=dict(header.payload),
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
        if type_id in {"toolchain", "common_dep", "variant"}:
            argv = common + [
                "--flake-ref",
                self.config.flake_ref,
                "--dataset-output-dir",
                str(self.config.dataset_dir),
            ]
            substituters = self._substituters_file_path()
            if substituters is not None:
                argv += ["--substituters-file", str(substituters)]
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
        del source_dir, output_dir, args  # unused
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

            # 2. Announce self.
            public_key = (
                self._signing_key.public_key
                if self._signing_key is not None
                else ""
            )
            try:
                announce_self(
                    self.config.shared_fs,
                    PeerInfo(
                        secondary_id=self.config.secondary_id,
                        hostname=self.config.hostname,
                        port=self.config.harmonia_port,
                        public_key=public_key,
                    ),
                )
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: announce_self failed"
                )

            # 3. Watcher.
            try:
                self._peer_watcher = PeerListWatcher(
                    shared_fs=self.config.shared_fs,
                    exclude_id=self.config.secondary_id,
                )
                self._peer_watcher.start()
            except Exception:  # noqa: BLE001 — log + continue
                self._logger.exception(
                    "on_run_start: PeerListWatcher failed to start"
                )
                self._peer_watcher = None

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
                    # group is needed.
                    from .peer_cache import start_nix_daemon
                    start_nix_daemon()
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "on_run_start: nix-daemon start failed"
                        " (harmonia will likely 500 on store queries)"
                    )
                try:
                    self._harmonia = HarmoniaProcess(
                        bind_addr=f"0.0.0.0:{self.config.harmonia_port}",
                        signing_key_path=self._signing_key.secret_path,
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

            try:
                withdraw_self(
                    self.config.shared_fs, self.config.secondary_id
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "on_run_end: withdraw_self failed"
                )

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
            )
            self._build_worker(path, env)
            return

        raise ValueError(
            f"unknown item_class {item_class!r} in manifest {path}"
        )

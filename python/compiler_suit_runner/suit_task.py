"""The single ``TaskDefinition`` that orchestrates the compiler-suit run.

The dynamic_runner framework dispatches one shared queue of items to a pool
of workers. We pack three logical phases (1a partition, 1b merge, 2 build,
3 variant build, plus barrier sentinels) into that one queue by encoding a
*phase rank* into the high bits of each item's ``size`` field. The Rust
scheduler sorts pending items by ``size`` DESC, so larger ranks dispatch
first. ``estimate_memory`` strips the rank back off to recover the per-item
memory budget the scheduler should reserve.

The :class:`SuitTask` class wraps that mechanic and owns the runner-side
bookkeeping that the framework does not provide:

* Manifest discovery from the pre-flight directory written by
  :mod:`compiler_suit_runner.manifest_gen`.
* Worker dispatch — switches on ``item_class`` to route to the correct
  phase-specific worker function (``partition_worker`` / ``merge_worker``
  / ``build_worker`` / ``barrier_worker``).
* Phase counters — every successful or failed dispatch of a barrier-bound
  rank advances a counter. When the counter hits its expected total, the
  matching ``flags/<phase>_done`` file is written, which unblocks the
  sentinel items still parked on :func:`barrier_worker.wait_for_flag`.
  Failure-counter advancement is deliberate: a single shard failure must
  not deadlock the whole run.
* Lifecycle hooks: :meth:`setup_peer_cache` (signing key, peer
  announcement, watcher, optional harmonia + Cachix uploader) and
  :meth:`teardown` (the inverse, idempotent).

The framework's structural :class:`Protocol` (see
``dynamic_runner/task_protocol.py``) is also implemented with sensible
defaults — many of those methods (``get_stages``, ``add_task_arguments``,
``build_worker_command_args``, ...) are framework wiring that this single
in-process task never actually exercises, but the methods exist so a
:class:`SuitTask` instance satisfies the Protocol's ``runtime_checkable``
isinstance check without surprises.
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
    ItemClass,
    ManifestHeader,
    read_manifest,
)
from compiler_suit_runner.memory_budget import MEMORY_FLOOR_BYTES
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


# Item classes that map onto the build worker. Both phase-2 toolchains
# and phase-2 common deps use the same nix-build entry point.
_PHASE2_BUILD_CLASSES: frozenset[str] = frozenset(
    {"phase2_toolchain", "phase2_common_dep"}
)


def _make_binary_info(path: pathlib.Path, size: int):
    """Return a framework-compatible BinaryInfo, falling back to a stub.

    The dynamic_runner framework defines
    :class:`dynamic_runner._shared.BinaryInfo` as the canonical
    scheduling-item shape. When the framework is on ``sys.path`` (i.e.
    the secondary container's environment) we use the real class so the
    Rust scheduler treats the result identically to other tasks.
    Otherwise (unit tests, local development without the framework
    installed) we synthesise an attribute-compatible stub via
    :class:`types.SimpleNamespace`; only the ``path`` and ``size``
    attributes are actually consulted by our dispatch surface, so the
    stub is sufficient.
    """
    try:
        from dynamic_runner._shared import (  # type: ignore[import-not-found]
            BinaryIdentifier,
            BinaryInfo,
        )
    except Exception:  # noqa: BLE001 — framework absent
        return SimpleNamespace(path=path, size=size)

    # Real BinaryInfo demands an identifier — manifests don't carry one
    # (they're not ELF binaries) so fill in best-effort placeholders.
    identifier = BinaryIdentifier(
        binary_name=path.name,
        platform="manifest",
        compiler="manifest",
        version="0",
        opt_level="manifest",
    )
    return BinaryInfo(path=path, size=size, identifier=identifier)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SuitTaskConfig:
    """Static, frozen configuration for one runner invocation.

    The CLI (B4.1) builds this from argparse output; tests build it
    directly. All filesystem paths are absolute; relative paths would
    break the secondary's working-directory assumptions.
    """

    flake_ref: str
    sys_name: str
    shared_fs: pathlib.Path
    manifest_dir: pathlib.Path
    raw_partition_dir: pathlib.Path
    partition_dir: pathlib.Path
    flags_dir: pathlib.Path
    dataset_dir: pathlib.Path
    peers_dir: pathlib.Path
    run_id: str
    secondary_id: str
    hostname: str
    harmonia_port: int = 5000
    enable_harmonia: bool = True
    cachix_cache: Optional[str] = None
    cachix_token_file: Optional[pathlib.Path] = None
    poll_interval_seconds: float = 2.0
    barrier_timeout_seconds: float = 24 * 60 * 60
    input_hash: str = ""
    toolchain_drvs: frozenset[str] = frozenset()
    common_threshold: int = 10
    variants: tuple = ()


# ---------------------------------------------------------------------------
# SuitTask
# ---------------------------------------------------------------------------


class SuitTask:
    """The single dynamic_runner :class:`TaskDefinition` for the run.

    Lifecycle (orchestrated by the CLI):

    1. ``__init__``
    2. :meth:`initialize_counters` — build :class:`PhaseCounter` map.
    3. :meth:`setup_peer_cache` — bring up signing key, peer
       announcement, watcher, optional services.
    4. ``coord.run(binaries)`` — framework dispatch loop calls
       :meth:`find_binaries`, :meth:`estimate_memory`, and
       :meth:`dispatch_binary` for each item.
    5. :meth:`teardown` — tear down peer-cache state.

    The framework Protocol surface (``get_stages``, ``add_task_arguments``,
    etc.) is implemented with conservative defaults — see the docstrings
    of each method for the exact behaviour.
    """

    def __init__(
        self,
        config: SuitTaskConfig,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self._logger = logger or logging.getLogger(__name__)

        # Peer-cache state: lazily populated by setup_peer_cache.
        self._signing_key: Optional[SigningKey] = None
        self._peer_watcher: Optional[PeerListWatcher] = None
        self._cachix_uploader: Optional[CachixUploader] = None
        self._harmonia: Optional[HarmoniaProcess] = None
        self._setup_done: bool = False
        self._setup_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Worker function injection (used by tests; defaults are the real ones).
    # Module-level constants would suffice for tests that monkeypatch the
    # imports; instance attributes give callers a clearer seam.
    # ------------------------------------------------------------------

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
        """Return the substituters-file path :class:`PeerListWatcher` writes.

        Returns ``None`` when ``peers_dir`` is unset; the build worker
        treats that as "no peer substitution".
        """
        if self.config.peers_dir is None:
            return None
        return self.config.peers_dir / SUBSTITUTERS_FILENAME

    # ==================================================================
    # Custom dispatch surface (used by the runner orchestrator)
    # ==================================================================

    def find_binaries(self, input_dir: Optional[pathlib.Path] = None) -> list:
        """Discover manifest files and wrap them as scheduling items.

        ``input_dir`` defaults to ``self.config.manifest_dir``. Each
        ``*.json`` direct child is wrapped as a BinaryInfo-shaped object
        whose ``.size`` is :func:`os.stat` ``st_size`` (the encoded
        scheduling integer; see :func:`memory_budget.encode_size`).
        Sub-directories and dotfiles are ignored.

        Missing or non-existent ``input_dir`` returns an empty list rather
        than raising — this matches what other dynamic_runner tasks do
        when their input tree was never produced.
        """
        target = (
            pathlib.Path(input_dir)
            if input_dir is not None
            else self.config.manifest_dir
        )
        try:
            entries = sorted(target.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return []

        binaries: list = []
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.suffix != ".json":
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            binaries.append(_make_binary_info(entry, size))
        return binaries

    def estimate_memory(self, item) -> int:
        """Return per-item memory budget in bytes, floored.

        Per-type estimators (8.6) override this; the fallback shape
        accepts either a :class:`TaskInfo` or a raw integer for
        backward compatibility with the legacy in-process surface.
        """
        size = getattr(item, "size", item)
        try:
            return max(int(size), MEMORY_FLOOR_BYTES)
        except (TypeError, ValueError):
            return MEMORY_FLOOR_BYTES

    def dispatch_binary(
        self,
        binary_info,
        output_dir: Optional[pathlib.Path] = None,
        **kwargs: Any,
    ) -> None:
        """Read ``binary_info.path`` and route to the right worker.

        Phase drain detection is now framework-owned (via
        :class:`PhaseSpec.depends_on`); this method just reads the
        manifest and dispatches one item, swallowing any worker
        exception so the secondary's process stays alive.

        ``output_dir`` and ``kwargs`` are accepted for forward
        compatibility with framework hooks that pass extra context; we
        pull what we need from ``self.config``.
        """
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

    # --- Internal dispatch helpers --------------------------------------

    def _dispatch_item(
        self,
        item_class: str,
        path: pathlib.Path,
        header: ManifestHeader,
    ) -> None:
        """Route to the right worker. May raise; caller swallows."""
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

    # ==================================================================
    # Setup / teardown
    # ==================================================================

    def setup_peer_cache(self) -> None:
        """Bring up peer-cache state. Idempotent.

        Steps:

        1. Generate / load the cluster-wide nix signing keypair.
        2. Announce this secondary on shared FS.
        3. Start the :class:`PeerListWatcher`.
        4. Optionally start a Cachix uploader (if the config names a
           cache) and a harmonia subprocess (if ``enable_harmonia`` is
           True and the binary is available).

        Failures of optional services (harmonia binary missing; Cachix
        token unreadable) are logged and the call still returns —
        peer-cache discovery continues to work without them.
        """
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
                    "setup_peer_cache: signing key generation skipped"
                    " (nix CLI not found): %s",
                    exc,
                )
                self._signing_key = None
            except Exception:  # noqa: BLE001 — never raise out
                self._logger.exception(
                    "setup_peer_cache: signing key generation failed"
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
                    "setup_peer_cache: announce_self failed"
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
                    "setup_peer_cache: PeerListWatcher failed to start"
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
                        "setup_peer_cache: CachixUploader failed to start"
                    )
                    self._cachix_uploader = None

            # 5. Harmonia (optional).
            if self.config.enable_harmonia and self._signing_key is not None:
                try:
                    self._harmonia = HarmoniaProcess(
                        bind_addr=f"0.0.0.0:{self.config.harmonia_port}",
                        signing_key_path=self._signing_key.secret_path,
                    )
                    self._harmonia.start()
                except FileNotFoundError as exc:
                    self._logger.info(
                        "setup_peer_cache: harmonia binary not found,"
                        " continuing without local cache server: %s",
                        exc,
                    )
                    self._harmonia = None
                except Exception:  # noqa: BLE001 — log + continue
                    self._logger.exception(
                        "setup_peer_cache: harmonia failed to start"
                    )
                    self._harmonia = None

            self._setup_done = True

    def teardown(self) -> None:
        """Reverse :meth:`setup_peer_cache`. Idempotent and best-effort.

        Each component is torn down independently so a single failing
        teardown doesn't strand resources owned by a later component.
        """
        with self._setup_lock:
            # Reverse start order.

            if self._harmonia is not None:
                try:
                    self._harmonia.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "teardown: harmonia stop failed"
                    )
                finally:
                    self._harmonia = None

            if self._cachix_uploader is not None:
                try:
                    self._cachix_uploader.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "teardown: CachixUploader stop failed"
                    )
                finally:
                    self._cachix_uploader = None

            if self._peer_watcher is not None:
                try:
                    self._peer_watcher.stop()
                except Exception:  # noqa: BLE001
                    self._logger.exception(
                        "teardown: PeerListWatcher stop failed"
                    )
                finally:
                    self._peer_watcher = None

            try:
                withdraw_self(
                    self.config.shared_fs, self.config.secondary_id
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "teardown: withdraw_self failed"
                )

            self._signing_key = None
            self._setup_done = False

    # ==================================================================
    # Framework Protocol surface (sensible defaults).
    #
    # The dynamic_runner task_protocol expects a handful of methods our
    # custom dispatch loop never actually calls; we still implement them
    # so a SuitTask satisfies the runtime_checkable Protocol's
    # ``isinstance(t, TaskDefinition)`` check unchanged.
    # ==================================================================

    def get_stages(self) -> list:
        """Return a single empty :class:`StageDefinition` list.

        We don't model stages; the encoded ``size`` integer carries all
        the per-item ordering information the scheduler needs. Returning
        ``[]`` makes the framework treat the run as a single anonymous
        stage.
        """
        return []

    def organize_and_sort_items(self, items: list) -> list:
        """Identity sort — the scheduler re-sorts by ``size`` DESC anyway.

        We could pre-group by phase or compiler class for cache locality,
        but the gain is dwarfed by the framework's mandatory size-DESC
        re-sort (see ``rust/.../assignment.rs:48``).
        """
        return list(items)

    def get_worker_module(self) -> str:
        """Module path used by the framework's subprocess factory.

        Our run never spawns a separate worker subprocess (everything
        dispatches in-process from :meth:`dispatch_binary`), but the
        Protocol still requires this; return our package's module path
        for diagnostics.
        """
        return "compiler_suit_runner.suit_task"

    def add_task_arguments(self, parser: ArgumentParser) -> None:
        """No-op: the runner CLI (B4.1) handles argument plumbing."""
        return None

    def build_worker_command_args(
        self,
        args: Namespace,
        source_dir: pathlib.Path,
        output_dir: pathlib.Path,
        skip_existing: bool,
    ) -> list:
        """Return an empty argv — we never spawn an out-of-process worker."""
        return []

    def get_output_filename_pattern(self, input_filename: str) -> str:
        """Identity — the worker writes its own output filename."""
        return input_filename

    def get_reserved_memory_per_worker(self) -> int:
        """No additional reserved memory beyond what ``estimate_memory`` returns."""
        return 0

    # Convenience flat alias used by some pipeline.py call sites that
    # expected a one-arg ``get_subdirectory(binary)``: returning empty
    # string keeps everything in the source root.
    def get_subdirectory(self, binary_info) -> str:  # pragma: no cover - default
        return ""

    def format_binary_size(self, size: int) -> str:  # pragma: no cover - default
        gib = max(0, size) / (1024**3)
        return f"mem={gib:.2f}GiB"

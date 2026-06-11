"""Peer-to-peer nix store sharing.

This module manages peer discovery and substituter wiring between the
SLURM secondaries that participate in a single ``compiler_suit_runner``
run. Discovery is implemented as shared-FS gossip: each secondary
publishes a small JSON file under ``${shared_fs}/peers/<id>.json`` on
startup, polls for peer files on a tick, and assembles a per-build
substituter env-var fragment from the live peer set.

A signing keypair (shared by every secondary in the run) is bootstrapped
under ``${shared_fs}/peers/__signing-key`` / ``__public-key`` so that
paths produced by one secondary can be trusted by the others.

Design constraints (see plan):
- This module is imported by the runner orchestrator and by worker
  processes; it must stay lightweight at import time. ``nix`` and
  ``harmonia`` are *only* invoked as subprocesses inside runtime
  functions, never at import.
- Stale peer files (e.g. a crashed secondary) are pruned by
  :func:`prune_stale`, called opportunistically.
- Atomic writes: temp file + ``os.replace`` ensures concurrent readers
  never observe a partially written JSON file.
- Files starting with ``__`` under ``peers/`` are reserved for keys and
  similar bookkeeping; they are skipped by the peer lister.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import signal
import socket as _socket
import subprocess
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Callable, Optional

__all__ = [
    "PeerInfo",
    "SigningKey",
    "SUBSTITUTERS_FILENAME",
    "generate_signing_key",
    "announce_self",
    "withdraw_self",
    "list_peers",
    "assemble_substituter_env",
    "build_nix_extra_args",
    "PeerListWatcher",
    "PeerNixConfWatcher",
    "PathPlacementWatcher",
    "PATHS_FILE_PREFIX",
    "prune_stale",
    "HarmoniaProcess",
    "start_nix_daemon",
    "load_store_db_registration",
    "NixStoreRegistrationError",
    "NIX_DB_REGISTRATION_ENV",
    "NIX_DAEMON_SOCKET",
    "PEER_CONF_PATH",
    "SubmitterPeer",
]

logger = logging.getLogger(__name__)

# Filename prefix marker for reserved (non-peer) files under peers/.
RESERVED_PREFIX = "__"

# Default poll interval for the watcher thread.
DEFAULT_TICK_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerInfo:
    """A single peer's public coordinates, as gossiped via shared FS."""

    secondary_id: str
    hostname: str
    port: int
    public_key: str  # nix trusted-public-key string "name:base64..."

    def substituter_url(self) -> str:
        """Return the harmonia URL for this peer."""
        return f"http://{self.hostname}:{self.port}"


@dataclass
class SigningKey:
    """A nix signing keypair shared across the SLURM run."""

    name: str  # "asm-suit-cluster-<run_id>"
    secret_path: pathlib.Path
    public_path: pathlib.Path
    public_key: str  # cached contents of public_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _peers_dir(shared_fs: pathlib.Path) -> pathlib.Path:
    """Return (and create) the peers/ subdirectory of *shared_fs*."""
    d = pathlib.Path(shared_fs) / "peers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _writer_id() -> str:
    """Cluster-unique per-writer suffix for tmp filenames.

    Hostname + PID names this writer uniquely across every secondary
    sharing the NFS mount; without it, two writers racing the same
    ``foo.tmp`` produce ``FileNotFoundError`` on the second
    ``os.replace`` (winner consumed the tmp, loser's source is gone).
    """
    return f"{_socket.gethostname()}.{os.getpid()}"


def _atomic_write_json(path: pathlib.Path, data: object) -> None:
    """Atomically write *data* as JSON to *path*.

    Per-writer ``.tmp`` filename so concurrent secondaries on the same
    NFS mount don't race the rename; last-writer-wins on the final
    path (callers that need single-writer semantics should use
    :func:`_atomic_create_bytes_excl`).
    """
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + f".{_writer_id()}.tmp")
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _atomic_write_bytes(path: pathlib.Path, data: bytes, mode: int) -> None:
    """Atomic write of raw bytes with explicit file mode.

    Per-writer ``.tmp`` filename — see :func:`_atomic_write_json`.
    """
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + f".{_writer_id()}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # os.replace preserves the tmp file mode; ensure the final mode is correct
    # in case the umask interfered when O_CREAT applied.
    os.chmod(path, mode)


def _atomic_create_bytes_excl(
    path: pathlib.Path, data: bytes, mode: int
) -> bool:
    """Create *path* with *data* iff it does not yet exist.

    Returns True if this caller wrote the file, False if another
    writer beat us to it (file already existed). Used for shared
    cluster artifacts (signing key) where ALL secondaries must agree
    on identical bytes — last-writer-wins would let each secondary
    keep an in-memory copy that differs from on-disk content.
    """
    path = pathlib.Path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, mode)
    return True


# ---------------------------------------------------------------------------
# Signing key bootstrap
# ---------------------------------------------------------------------------


def _read_existing_signing_key(
    secret_path: pathlib.Path,
    public_path: pathlib.Path,
    name: str,
) -> Optional[SigningKey]:
    """Reload an existing cluster keypair, or None if mismatched/unreadable."""
    try:
        public_key = public_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not public_key:
        return None
    if not (
        public_key.startswith(name + ":")
        or public_key.startswith("asm-suit-cluster-")
    ):
        return None
    return SigningKey(
        name=name,
        secret_path=secret_path,
        public_path=public_path,
        public_key=public_key,
    )


def generate_signing_key(
    shared_fs: pathlib.Path, run_id: str
) -> SigningKey:
    """Generate (or reload) the cluster-wide nix signing keypair.

    Race-safe across N secondaries on a shared NFS mount:

    * Fast path — both files exist with a matching name prefix:
      reload from disk so every secondary returns identical bytes.
    * Slow path — files missing: ``nix key generate-secret`` locally,
      then ``O_CREAT|O_EXCL`` create. The first writer wins; every
      other writer sees ``FileExistsError`` and reloads the winner's
      key. The previous implementation used last-writer-wins via a
      shared ``__signing-key.tmp``, which gave each secondary an
      in-memory key that diverged from on-disk bytes — peers signed
      with key X but advertised pub-Y in their ``<id>.json``, so
      cross-peer signature verification failed and harmonia
      substitution silently fell back to building from source.

    The secret is written with mode 0600, the public with mode 0644.
    Requires the ``nix`` CLI (looked up via ``shutil.which``) only
    when the keys do not yet exist on disk; a missing ``nix`` binary
    on the slow path raises :class:`FileNotFoundError`.
    """
    peers = _peers_dir(shared_fs)
    secret_path = peers / "__signing-key"
    public_path = peers / "__public-key"
    name = f"asm-suit-cluster-{run_id}"

    if secret_path.exists() and public_path.exists():
        existing = _read_existing_signing_key(secret_path, public_path, name)
        if existing is not None:
            return existing

    nix = shutil.which("nix")
    if nix is None:
        raise FileNotFoundError(
            "nix CLI not found in PATH; cannot generate signing key"
        )

    secret_proc = subprocess.run(
        [nix, "key", "generate-secret", "--key-name", name],
        check=True,
        capture_output=True,
    )
    secret_bytes = secret_proc.stdout
    if not secret_bytes:
        raise RuntimeError("nix key generate-secret produced empty output")

    public_proc = subprocess.run(
        [nix, "key", "convert-secret-to-public"],
        input=secret_bytes,
        check=True,
        capture_output=True,
    )
    public_bytes = public_proc.stdout
    if not public_bytes:
        raise RuntimeError(
            "nix key convert-secret-to-public produced empty output"
        )

    # Secret is the synchronization point. Whoever wins ``secret_path``
    # via ``O_CREAT|O_EXCL`` becomes the sole keypair authority and
    # publishes the matching ``public_path``; everybody else reloads
    # the winner's pair. Racing on each file independently would let
    # writer A win secret while writer B wins public, leaving the
    # cluster with a mismatched (secret-A, public-B) pair on disk.
    if _atomic_create_bytes_excl(secret_path, secret_bytes, mode=0o600):
        # Sole secret writer — publish OUR public, overwriting any
        # stale leftover so the on-disk pair stays consistent.
        _atomic_write_bytes(public_path, public_bytes, mode=0o644)
        return SigningKey(
            name=name,
            secret_path=secret_path,
            public_path=public_path,
            public_key=public_bytes.decode("utf-8").strip(),
        )

    # Lost the race. Spin briefly: the secret winner may not have
    # finished writing public yet.
    for _ in range(40):
        existing = _read_existing_signing_key(
            secret_path, public_path, name
        )
        if existing is not None:
            return existing
        time.sleep(0.05)
    raise RuntimeError(
        "signing key race lost but reload from peer failed; "
        "shared FS may be inconsistent"
    )


# ---------------------------------------------------------------------------
# Peer announcement / withdrawal / discovery
# ---------------------------------------------------------------------------


def announce_self(shared_fs: pathlib.Path, info: PeerInfo) -> pathlib.Path:
    """Atomically announce this secondary's coordinates to peers.

    Writes ``peers/<secondary_id>.json``. Returns the final path.
    """
    if info.secondary_id.startswith(RESERVED_PREFIX):
        raise ValueError(
            f"secondary_id may not start with {RESERVED_PREFIX!r} "
            f"(reserved for keys / metadata)"
        )
    peers = _peers_dir(shared_fs)
    target = peers / f"{info.secondary_id}.json"
    payload = {
        "secondary_id": info.secondary_id,
        "hostname": info.hostname,
        "port": info.port,
        "public_key": info.public_key,
    }
    _atomic_write_json(target, payload)
    return target


def withdraw_self(shared_fs: pathlib.Path, secondary_id: str) -> None:
    """Remove ``peers/<secondary_id>.json`` (no error if absent).

    Also unlinks the path-placement gossip file
    ``peers/_paths_<secondary_id>.jsonl`` so other peers' watchers
    drop our placement records on the next tick. Stale placement
    records pointing at a no-longer-running secondary would cause
    workers to issue a ``nix copy --from http://<host>:<port>`` that
    times out, then fall through to the next candidate — correctness
    is preserved but latency is paid; removing the file up front
    avoids the dead-peer probe altogether.
    """
    peers = _peers_dir(shared_fs)
    target = peers / f"{secondary_id}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    paths_file = peers / f"{PATHS_FILE_PREFIX}{secondary_id}.jsonl"
    try:
        paths_file.unlink()
    except FileNotFoundError:
        return


def _read_peer_file(path: pathlib.Path) -> Optional[PeerInfo]:
    """Read a peer file, returning ``None`` if malformed."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("skipping malformed peer file %s: %s", path, exc)
        return None
    try:
        return PeerInfo(
            secondary_id=str(data["secondary_id"]),
            hostname=str(data["hostname"]),
            port=int(data["port"]),
            public_key=str(data["public_key"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("skipping incomplete peer file %s: %s", path, exc)
        return None


def list_peers(
    shared_fs: pathlib.Path, exclude_id: str | None = None
) -> list[PeerInfo]:
    """Read all peers/<id>.json files.

    Reserved files (those whose name starts with ``__``) are skipped.
    Malformed files are skipped (logged at debug level) rather than
    raising, so that one bad writer doesn't take the whole watcher
    down.
    """
    peers_dir = _peers_dir(shared_fs)
    out: list[PeerInfo] = []
    try:
        entries = sorted(peers_dir.iterdir())
    except FileNotFoundError:
        return []
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name.startswith(RESERVED_PREFIX):
            continue
        if entry.suffix != ".json":
            continue
        info = _read_peer_file(entry)
        if info is None:
            continue
        if exclude_id is not None and info.secondary_id == exclude_id:
            continue
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Substituter env / nix CLI argument assembly
# ---------------------------------------------------------------------------


def assemble_substituter_env(peers: list[PeerInfo]) -> dict[str, str]:
    """Build env-var fragment for ``nix build`` consumers.

    Returns a dict with ``NIX_CONFIG_EXTRA_SUBSTITUTERS`` and
    ``NIX_CONFIG_EXTRA_TRUSTED_PUBLIC_KEYS``. Empty list -> empty
    strings (the caller should typically *not* set these in the
    environment when empty, to avoid confusing nix; the empty values
    are returned for inspection).
    """
    if not peers:
        return {
            "NIX_CONFIG_EXTRA_SUBSTITUTERS": "",
            "NIX_CONFIG_EXTRA_TRUSTED_PUBLIC_KEYS": "",
        }
    urls = " ".join(p.substituter_url() for p in peers)
    keys = " ".join(p.public_key for p in peers)
    return {
        "NIX_CONFIG_EXTRA_SUBSTITUTERS": urls,
        "NIX_CONFIG_EXTRA_TRUSTED_PUBLIC_KEYS": keys,
    }


def build_nix_extra_args(
    peers: list[PeerInfo],
    substitute_on_destination: bool = False,  # noqa: ARG001 — kept for API stability
) -> list[str]:
    """Build ``nix build`` arguments for peer substitution.

    Returns a list of args ready to splat into ``nix build`` after the
    attribute. Empty list -> empty list (no flags at all).

    ``--substitute-on-destination`` is a ``nix copy`` flag, not a
    ``nix build`` flag. Earlier versions of this function injected it
    unconditionally and ``nix build`` aborted with
    ``error: unrecognised flag '--substitute-on-destination'``. The
    parameter is preserved to keep callers' signatures stable but is
    now ignored — the flag has no place in a build invocation. Peer
    substitution still works via ``--extra-substituters`` +
    ``--extra-trusted-public-keys``.
    """
    if not peers:
        return []
    urls = " ".join(p.substituter_url() for p in peers)
    keys = " ".join(p.public_key for p in peers)
    return [
        "--extra-substituters",
        urls,
        "--extra-trusted-public-keys",
        keys,
    ]


# ---------------------------------------------------------------------------
# Stale-file pruning
# ---------------------------------------------------------------------------


def prune_stale(
    shared_fs: pathlib.Path,
    max_age_seconds: float,
    *,
    now: float | None = None,
) -> int:
    """Remove peers/*.json files older than ``now - max_age_seconds``.

    Reserved files (starting with ``__``) are skipped. Returns the
    number of files removed. ``now`` may be injected for test
    determinism; defaults to ``time.time()``.
    """
    peers_dir = _peers_dir(shared_fs)
    if now is None:
        now = time.time()
    cutoff = now - max_age_seconds
    removed = 0
    try:
        entries = list(peers_dir.iterdir())
    except FileNotFoundError:
        return 0
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.name.startswith(RESERVED_PREFIX):
            continue
        if entry.suffix != ".json":
            continue
        try:
            mtime = entry.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


# ---------------------------------------------------------------------------
# Watcher thread
# ---------------------------------------------------------------------------


SUBSTITUTERS_FILENAME = "_substituters.txt"


def substituters_filename_for(secondary_id: str | None) -> str:
    """Return the per-secondary substituters file name.

    Each secondary's PeerListWatcher excludes itself from the peer
    list (we don't want a secondary to list its own harmonia as a
    substituter — would deadlock on self-fetch). That makes the
    "live peer set" view per-secondary, so writers in different
    secondaries produce DIFFERENT contents for the same shared
    file. Last-writer-wins clobbers earlier writers' views.

    Per-secondary filename eliminates the collision: each writer
    owns its own file, the local build_worker reads it. Falls back
    to the legacy shared name only when ``secondary_id`` is unset
    (single-process / dev-box mode where there's only one writer).
    """
    if secondary_id:
        return f"_substituters.{secondary_id}.txt"
    return SUBSTITUTERS_FILENAME


def _write_substituters_file(
    target: pathlib.Path, args: list[str]
) -> None:
    """Atomically write ``args`` (one per line) to ``target``.

    Uses a writer-unique sibling ``.tmp`` + ``os.replace`` so
    concurrent readers never observe a partially-written file AND
    concurrent writers from different secondaries don't collide on
    a shared ``.tmp`` filename (the substituters file lives on
    ``/app/log-network/peers/`` — NFS-shared across every
    secondary's PeerListWatcher). Empty ``args`` produces an empty
    (but present) file, which workers treat as "no peers".
    """
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer suffix: hostname + pid uniquely names this writer
    # across the cluster. Without this, two secondaries' watchers
    # racing each other's ``os.replace`` produced
    #   FileNotFoundError: ``_substituters.txt.tmp`` -> ``_substituters.txt``
    # because the tmp file name was identical and one watcher's
    # replace already consumed it.
    writer_id = f"{_socket.gethostname()}.{os.getpid()}"
    tmp = target.with_suffix(target.suffix + f".{writer_id}.tmp")
    payload = "\n".join(args)
    if payload:
        payload += "\n"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)


class PeerListWatcher(threading.Thread):
    """Background thread that polls the peers/ dir and republishes args.

    On every successful refresh the watcher writes the current
    nix-build extra args into ``peers/_substituters.txt`` (atomically),
    one argument per line. Workers re-read the file on every nix-build
    invocation, so they never need to share an in-process reference to
    the watcher.

    Provides:

    * ``self.peers`` — current snapshot ``list[PeerInfo]`` (read under
      the internal lock; the property returns a fresh copy).
    * ``self.substituters_path`` — the published file's absolute path.
    * ``self.stop()`` — request the thread to exit.

    The watcher is a daemon thread so it does not prevent process
    shutdown if the parent forgets to stop it.
    """

    def __init__(
        self,
        shared_fs: pathlib.Path,
        exclude_id: str | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        substitute_on_destination: bool = True,
    ) -> None:
        super().__init__(name="PeerListWatcher", daemon=True)
        self._shared_fs = pathlib.Path(shared_fs)
        self._exclude_id = exclude_id
        self._tick_seconds = float(tick_seconds)
        self._substitute_on_destination = substitute_on_destination
        self._stop_event = threading.Event()
        # Wake event: set on stop() OR on request_refresh(). The run
        # loop blocks on this between ticks so a push notification can
        # collapse the latency from one tick to ~one refresh.
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._peers: list[PeerInfo] = []
        # Per-secondary file so concurrent writers from different
        # secondaries don't clobber each other's self-excluded views.
        self._substituters_path = (
            _peers_dir(self._shared_fs)
            / substituters_filename_for(exclude_id)
        )
        # Prime the snapshot + published file once synchronously so
        # callers that read immediately after .start() see a meaningful
        # initial state.
        self._refresh()

    # --- public read-side API -------------------------------------------------

    @property
    def peers(self) -> list[PeerInfo]:
        with self._lock:
            return list(self._peers)

    @property
    def substituters_path(self) -> pathlib.Path:
        return self._substituters_path

    # --- lifecycle ------------------------------------------------------------

    def stop(self) -> None:
        """Request the thread to exit at its next tick."""
        self._stop_event.set()
        self._wake_event.set()

    def request_refresh(self) -> None:
        """Wake the run loop for an out-of-band refresh.

        Thread-safe; intended for the peer-push server to call when an
        announce/withdraw POST arrives. The run loop's next iteration
        re-reads ``peers/`` and republishes the substituters file. The
        wake event is consumed at the start of each iteration so a
        push that fires *during* a refresh still triggers a follow-up
        refresh instead of being lost.
        """
        self._wake_event.set()

    def run(self) -> None:  # pragma: no cover - exercised via integration
        while not self._stop_event.is_set():
            # Consume any prior wake before refreshing; a wake that
            # arrives DURING ``_refresh`` re-sets the event and the
            # post-refresh ``wait`` returns immediately.
            self._wake_event.clear()
            try:
                self._refresh()
            except Exception:  # noqa: BLE001 - keep the watcher alive
                logger.exception("PeerListWatcher refresh failed")
            # Wait either for the tick, for an out-of-band refresh
            # request, or for stop() (which also sets _wake_event).
            self._wake_event.wait(self._tick_seconds)

    # --- internals ------------------------------------------------------------

    def _refresh(self) -> None:
        new_peers = list_peers(self._shared_fs, exclude_id=self._exclude_id)
        new_args = build_nix_extra_args(
            new_peers,
            substitute_on_destination=self._substitute_on_destination,
        )
        with self._lock:
            self._peers = new_peers
        try:
            _write_substituters_file(self._substituters_path, new_args)
        except OSError:
            # Republish failure must never crash the watcher; workers
            # gracefully fall back to "no peers" until the next tick.
            logger.exception(
                "PeerListWatcher: failed to publish %s",
                self._substituters_path,
            )


# ---------------------------------------------------------------------------
# harmonia subprocess control
# ---------------------------------------------------------------------------


# Default runtime dir for harmonia's per-node files (TOML config + log).
# Container-local; never on NFS. See HarmoniaProcess docstring.
DEFAULT_HARMONIA_RUNTIME_DIR = pathlib.Path("/tmp/harmonia")


@dataclass
class HarmoniaProcess:
    """Context-manager wrapper around a ``harmonia`` (or fallback) server.

    ``harmonia`` is a rust reimplementation of ``nix-serve``; we prefer
    it for cluster fan-in. If the harmonia binary is missing we fall
    back to ``nix-serve`` (also looked up via :func:`shutil.which`).
    Missing both -> :class:`FileNotFoundError` from :meth:`start`.

    ``harmonia.toml`` lives under ``runtime_dir`` — MUST be
    CONTAINER-LOCAL, never on the shared NFS mount. Earlier versions
    wrote it next to the signing key (``${shared_fs}/peers/``); on a
    6-secondary run all writers raced the same TOML via
    ``Path.write_text`` (truncate-then-write, not atomic on NFS), so
    harmonia occasionally parsed a half-written TOML and exited
    silently.

    ``log_path`` can sit on NFS — but the PATH MUST be unique per node
    (e.g. ``/app/log-network/<secondary_id>/harmonia.log``) so
    concurrent writers never share an inode. Default is
    ``runtime_dir / "harmonia.log"`` (container-local) for callers
    that don't need the log shared off-node; the suit-task secondary
    overrides it to land inside the secondary's own subdir alongside
    the framework's per-role logs on the gateway-readable mount.

    Tests typically monkeypatch :func:`subprocess.Popen` so no real
    server is spawned.
    """

    bind_addr: str
    signing_key_path: pathlib.Path
    binary: Optional[str] = None  # if None, autodetected on .start()
    extra_args: list[str] = field(default_factory=list)
    workers: int = 16
    runtime_dir: pathlib.Path = field(
        default_factory=lambda: DEFAULT_HARMONIA_RUNTIME_DIR
    )
    log_path: Optional[pathlib.Path] = None
    _proc: Optional[subprocess.Popen] = field(default=None, init=False, repr=False)

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> "HarmoniaProcess":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # --- lifecycle ------------------------------------------------------------

    def _resolve_binary(self) -> str:
        """Pick the cache-server binary. Prefer harmonia, then nix-serve."""
        if self.binary is not None:
            resolved = shutil.which(self.binary) or self.binary
            if not resolved or shutil.which(resolved) is None:
                raise FileNotFoundError(
                    f"binary cache server {self.binary!r} not found in PATH"
                )
            return resolved
        for candidate in ("harmonia", "nix-serve"):
            found = shutil.which(candidate)
            if found is not None:
                return found
        raise FileNotFoundError(
            "no binary cache server found in PATH "
            "(looked for: harmonia, nix-serve); install harmonia "
            "or set HarmoniaProcess.binary explicitly"
        )

    def start(self) -> None:
        """Spawn the cache server subprocess.

        Raises :class:`FileNotFoundError` if no suitable binary is
        on ``PATH``. Idempotent if already running (no-op).

        Harmonia 3.x compatibility: harmonia-cache no longer takes
        ``--bind`` on argv; the bind address goes in a TOML config
        file referenced by ``CONFIG_FILE`` env. Nix-serve still uses
        ``--bind``. We detect the binary basename and switch.
        """
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        binary = self._resolve_binary()
        binary_name = os.path.basename(binary)

        env = dict(os.environ)
        env["SIGN_KEY_PATH"] = str(self.signing_key_path)
        env["NIX_SECRET_KEY_FILE"] = str(self.signing_key_path)
        # harmonia 3.x renamed SIGN_KEY_PATH → SIGN_KEY_PATHS (plural,
        # multi-key support). Set both so legacy + current both work.
        env["SIGN_KEY_PATHS"] = str(self.signing_key_path)

        runtime_dir = pathlib.Path(self.runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)

        if "harmonia-cache" in binary_name or binary_name == "harmonia":
            # Harmonia 3.x: TOML config via CONFIG_FILE env. The wrapper
            # binary at /bin/harmonia is a multi-tool launcher; the
            # actual cache server is harmonia-cache.
            cache_bin = binary
            if binary_name == "harmonia":
                # Resolve the harmonia-cache sibling.
                cache_dir = os.path.dirname(os.path.realpath(binary))
                candidate = os.path.join(cache_dir, "harmonia-cache")
                if os.path.exists(candidate):
                    cache_bin = candidate

            cfg_path = runtime_dir / "harmonia.toml"
            cfg_path.write_text(
                f'bind = "{self.bind_addr}"\nworkers = {int(self.workers)}\n'
            )
            env["CONFIG_FILE"] = str(cfg_path)
            cmd = [cache_bin, *self.extra_args]
        else:
            # nix-serve (or anything else): legacy --bind flag.
            cmd = [binary, "--bind", self.bind_addr, *self.extra_args]

        logger.info("starting binary cache server: %s", cmd)
        # Detached + log-to-file (NOT PIPE — PIPE without a drainer
        # blocks once kernel buffer fills, killing harmonia silently
        # under heavy load.) ``log_path`` may live on NFS — caller's
        # responsibility to make the filename unique-per-node.
        log_path = (
            pathlib.Path(self.log_path)
            if self.log_path is not None
            else runtime_dir / "harmonia.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "ab", buffering=0)
        self._proc = subprocess.Popen(  # noqa: S603
            cmd,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )

    def stop(self, timeout: float = 10.0) -> Optional[int]:
        """SIGTERM the server and wait up to *timeout* seconds.

        Returns the exit code, or ``None`` if no process was running.
        Falls back to SIGKILL if the server doesn't terminate in time.
        """
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is not None:
            self._proc = None
            return proc.returncode
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            self._proc = None
            return proc.returncode
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "harmonia did not exit within %.1fs, sending SIGKILL", timeout
            )
            proc.kill()
            rc = proc.wait()
        self._proc = None
        return rc

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ---------------------------------------------------------------------------
# nix-daemon helper
# ---------------------------------------------------------------------------


NIX_DAEMON_SOCKET = "/nix/var/nix/daemon-socket/socket"

# Env var baked into the runner image (see nix/docker-image.nix) that
# points at the ``nix-store --load-db``-format registration file
# covering every store path shipped in the image rootfs. When set, the
# registration MUST be loaded before the store serves any traffic;
# unset means we're running outside the image (e.g. the submitter
# host) where the local nix DB is managed by the host install.
NIX_DB_REGISTRATION_ENV = "CSR_NIX_DB_REGISTRATION"


class NixStoreRegistrationError(RuntimeError):
    """The image's baked store-DB registration could not be loaded.

    Raised LOUDLY (never swallowed) because continuing with an
    unloaded DB leaves every rootfs store path present-on-disk but
    INVALID in the nix database. Nix's ``LocalStore::addToStore``
    (used by both ``nix-store --import`` and substitution)
    ``deletePath()``s an existing-but-invalid destination before
    re-unpacking it — so the first concurrent import storm deletes
    live rootfs paths (glibc, python, nix itself) out from under
    running processes, which then die with ENOENT mid-exec/dlopen.
    """


def load_store_db_registration(
    nix_store: str,
    env: dict[str, str] | None = None,
    registration: str | None = None,
) -> bool:
    """Register the image's baked store paths as VALID in the nix DB.

    ``dockerTools.buildLayeredImage`` ships the store *paths* but no
    nix *database*; the image bakes a ``closureInfo`` registration
    file and points :data:`NIX_DB_REGISTRATION_ENV` at it. This runs
    ``nix-store --load-db < registration`` against the LOCAL store
    (caller must pass an env with ``NIX_REMOTE`` stripped). load-db
    is idempotent: re-loading an already-registered DB is a no-op.

    Returns ``True`` if the registration was loaded, ``False`` when
    no registration is configured (host context — nothing to do).

    Raises :class:`NixStoreRegistrationError` if a registration is
    configured but missing/unloadable. A silent skip here reproduces
    the delete-then-restore tear class described on the exception.
    """
    if registration is None:
        registration = os.environ.get(NIX_DB_REGISTRATION_ENV)
    if not registration:
        logger.debug(
            "no %s in environment; skipping store-DB registration "
            "(host-managed nix DB)",
            NIX_DB_REGISTRATION_ENV,
        )
        return False
    if not os.path.exists(registration):
        raise NixStoreRegistrationError(
            f"store-DB registration file {registration!r} (from "
            f"${NIX_DB_REGISTRATION_ENV}) does not exist; refusing to "
            "serve a store whose baked rootfs paths are unregistered"
        )
    if not os.path.exists(nix_store):
        raise NixStoreRegistrationError(
            f"nix-store binary {nix_store!r} not found; cannot load "
            f"store-DB registration {registration!r}"
        )
    try:
        with open(registration, "rb") as reg_fh:
            proc = subprocess.run(  # noqa: S603 - argv built in-module
                [nix_store, "--load-db"],
                check=False,
                stdin=reg_fh,
                capture_output=True,
                shell=False,
                timeout=600,
                env=env,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NixStoreRegistrationError(
            f"nix-store --load-db < {registration} failed to run: {exc}"
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise NixStoreRegistrationError(
            f"nix-store --load-db < {registration} exited "
            f"{proc.returncode}: {stderr}"
        )
    logger.info(
        "registered baked image store paths in the nix DB from %s",
        registration,
    )
    return True


def start_nix_daemon(log_path: pathlib.Path | None = None) -> int | None:
    """Start ``nix-daemon`` detached. No-op if its socket already exists.

    Required by harmonia-cache 3.x — it queries the daemon over
    ``/nix/var/nix/daemon-socket/socket`` for store info; without
    a daemon every narinfo request 500s.

    The image's ``nix.conf`` runs in single-user mode
    (``build-users-group =``) so the daemon doesn't need nixbld* users.

    Pre-initializes ``/nix/var/nix/db/schema`` synchronously before
    spawning the daemon. dockerTools.buildLayeredImage does not bake
    the store DB into the image, so the first nix invocation in a
    fresh container has to create ``schema``, ``db.sqlite`` and
    ``temproots/``. With multiple workers in the same container all
    invoking ``nix build`` concurrently they race the create-empty-
    then-write-content sequence and one of them sees a half-written
    schema file, aborting with ``error: "/nix/var/nix/db/schema" is
    corrupt``. ``nix-store --init`` is idempotent and forces the
    init synchronously in this single process before any other
    nix invocation can race it.

    After the init, loads the image's baked store-DB registration
    (``nix-store --load-db``, see :func:`load_store_db_registration`)
    so every store path shipped in the rootfs is VALID in the DB
    before the daemon serves a single request. Raises
    :class:`NixStoreRegistrationError` if the image declares a
    registration (via :data:`NIX_DB_REGISTRATION_ENV`) that can't be
    loaded — continuing would let the first import/substitution storm
    delete live rootfs paths out from under running processes.

    Returns the PID, or None if the daemon was already running.
    """
    if os.path.exists(NIX_DAEMON_SOCKET):
        return None
    # Synchronously initialize the local store DB so concurrent
    # workers (and harmonia, which is started right after this
    # function returns) see a fully-formed schema. Best-effort —
    # if nix-store is missing or the call fails we let the daemon
    # retry the init itself, preserving the existing behaviour.
    #
    # Strip ``NIX_REMOTE`` here too: the parent env carries
    # ``NIX_REMOTE=daemon`` from the image, which would route
    # ``nix-store --init`` through a daemon socket that doesn't
    # exist yet. The init must talk to the local store directly.
    local_store_env = {
        k: v for k, v in os.environ.items() if k != "NIX_REMOTE"
    }
    nix_store = shutil.which("nix-store") or "/bin/nix-store"
    if os.path.exists(nix_store):
        try:
            subprocess.run(  # noqa: S603 - argv constructed in-module
                [nix_store, "--init"],
                check=False,
                capture_output=True,
                shell=False,
                timeout=30,
                env=local_store_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            # fall through to daemon spawn; daemon will retry init
            pass
    # Load the image's baked store-DB registration BEFORE the daemon
    # starts serving (and before harmonia / any worker import or
    # substitution can run). The image rootfs carries thousands of
    # store paths that buildLayeredImage does NOT register in the DB;
    # until they're registered, any import/substitution touching them
    # deletePath()s live rootfs paths mid-flight (#delete-then-restore
    # tear, see NixStoreRegistrationError). Loud on failure by design.
    load_store_db_registration(nix_store, env=local_store_env)
    binary = shutil.which("nix-daemon") or "/bin/nix-daemon"
    if not os.path.exists(binary):
        raise FileNotFoundError("nix-daemon not found on PATH or /bin")
    # Default log path is /tmp/nix-daemon.log, but containers built via
    # dockerTools.buildLayeredImage don't get a /tmp by default — fall
    # back to /dev/null rather than crashing the lifecycle hook over a
    # missing log file. Daemon errors then surface on the secondary's
    # own stderr (slurm_<jobid>.err) which is just as useful.
    target = log_path or pathlib.Path("/tmp/nix-daemon.log")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(target, "ab", buffering=0)
    except OSError:
        log_fh = open(os.devnull, "wb")
    # Strip ``NIX_REMOTE`` from the daemon's env: the image sets it to
    # "daemon" so client processes route through this daemon, but if the
    # daemon ITSELF inherits it the daemon's forked workers will then
    # try to connect to themselves as a client (infinite-loop reset).
    # Symptom on the client side: ``error: cannot open connection to
    # remote store 'daemon': error: read of 32768 bytes: Connection
    # reset by peer`` while the daemon's log keeps logging
    # ``accepted connection from pid X, user root (trusted)``
    # without ever serving a single request.
    proc = subprocess.Popen(  # noqa: S603
        [binary],
        env=local_store_env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    # Wait until the daemon is actually accepting connections, not
    # just until its socket inode exists. ``nix-daemon`` creates the
    # socket file early in startup (during ``listen(2)`` setup) but
    # there's a window where ``connect(2)`` succeeds and the kernel's
    # accept queue immediately RSTs because the daemon hasn't entered
    # its accept loop. A worker that races into that window sees
    # exactly the symptom we hit on smoke11:
    # ``cannot open connection to remote store 'daemon': error: read
    # of 32768 bytes: Connection reset by peer``. Probe via a real
    # ``connect()`` + tiny round-trip and only return once the daemon
    # has actually responded once.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _nix_daemon_accepts_connections():
            return proc.pid
        time.sleep(0.05)
    return proc.pid


def _nix_daemon_accepts_connections() -> bool:
    """Return True iff a ``connect`` to the daemon socket round-trips.

    Opens the socket, sends the nix-daemon worker-magic handshake
    (the four-byte little-endian ``0x6e697863`` value the daemon
    expects as the very first read on a new connection — see
    ``src/libstore/daemon.cc`` ``WORKER_MAGIC_1``) and waits briefly
    for any reply. If the daemon hasn't reached its accept loop yet
    the kernel sends RST and we fail; once it has, the daemon writes
    its own magic byte and we know it's alive.

    We don't care what the daemon writes back — only that *something*
    came through, which means the accept loop is live.
    """
    if not os.path.exists(NIX_DAEMON_SOCKET):
        return False
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.settimeout(0.25)
        try:
            sock.connect(NIX_DAEMON_SOCKET)
        except OSError:
            return False
        try:
            sock.sendall(b"\x63\x78\x69\x6e")  # WORKER_MAGIC_1, LE
        except OSError:
            return False
        try:
            data = sock.recv(4)
        except OSError:
            return False
        return len(data) > 0
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Path-placement watcher
# ---------------------------------------------------------------------------


# Per-secondary placement gossip file: ``peers/_paths_<id>.jsonl``.
# Underscore-prefixed so :func:`list_peers` (which only scans ``*.json``)
# ignores them, but distinct from the existing ``__signing-key`` /
# ``__public-key`` reserved files so the path-placement reader can
# still find them by prefix match.
PATHS_FILE_PREFIX = "_paths_"


@dataclass(frozen=True)
class PlacementDiff:
    """Delta between two consecutive :class:`PathPlacementWatcher`
    snapshots.

    ``added[outpath]`` is the set of secondary-ids that became holders
    of *outpath* in this refresh; ``removed[outpath]`` is the set that
    stopped holding it. Outpaths with neither addition nor removal are
    omitted from both maps. An empty diff (``added`` and ``removed``
    both empty) means the refresh saw no change.

    Used by :meth:`PathPlacementWatcher.register_diff_callback` as the
    **fallback** signal for K=3 repair when the framework's
    peer-removed hook isn't yet available. Primary signal for
    peer-death is the framework hook (see plan: Framework Ask #4).
    """

    added: dict[str, set[str]]
    removed: dict[str, set[str]]

    def is_empty(self) -> bool:
        return not self.added and not self.removed


class PathPlacementWatcher(threading.Thread):
    """Background thread that aggregates per-secondary placement gossip.

    On every tick the watcher reads every ``peers/_paths_*.jsonl``
    file and rebuilds an in-memory aggregate
    ``dict[outpath, set[secondary_id]]``. Workers query the aggregate
    via :meth:`snapshot` to decide which peer to fetch a given store
    path from.

    Push-driven wake-ups (via :meth:`request_refresh`) collapse the
    update latency from one tick to a single re-read; the polling
    tick is the safety net for missed pushes.

    The watcher is a daemon thread; ``stop()`` requests exit at the
    next tick or wake.

    Diff callbacks (:meth:`register_diff_callback`) fire on every
    refresh that produces a non-empty :class:`PlacementDiff`. They are
    a fallback signal for K=3 replication repair-on-death when the
    framework's peer-removed hook isn't available; the framework hook
    is the primary signal.
    """

    def __init__(
        self,
        shared_fs: pathlib.Path,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        super().__init__(name="PathPlacementWatcher", daemon=True)
        self._shared_fs = pathlib.Path(shared_fs)
        self._tick_seconds = float(tick_seconds)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        # outpath -> { secondary_id, ... }
        self._placements: dict[str, set[str]] = {}
        # Diff-callback registry. Append-only after start(); each entry
        # is invoked synchronously per refresh that produces a
        # non-empty diff, OUTSIDE the lock so callbacks can call
        # snapshot() without deadlocking. Exceptions are isolated per
        # callback so one buggy callback doesn't take the watcher down.
        self._diff_callbacks: list[Callable[[PlacementDiff], None]] = []
        # Prime once synchronously so callers reading right after
        # ``start()`` see a meaningful initial snapshot. The prime
        # refresh intentionally does NOT fire diff callbacks: there is
        # no "previous" state to diff against and callbacks haven't
        # been registered yet anyway.
        self._refresh(fire_callbacks=False)

    # --- public read-side API ----------------------------------------------

    def snapshot(self) -> dict[str, set[str]]:
        """Return a fresh copy of the current placement aggregate.

        Each entry's value is a copy of the secondary-id set so
        callers can mutate without disturbing the watcher's state.
        """
        with self._lock:
            return {outpath: set(sids) for outpath, sids in self._placements.items()}

    def register_diff_callback(
        self, fn: Callable[[PlacementDiff], None],
    ) -> None:
        """Register *fn* to be called on every refresh that observes a
        non-empty :class:`PlacementDiff`.

        Callbacks are invoked synchronously on the watcher thread,
        OUTSIDE the placement lock (so they may call :meth:`snapshot`
        freely). Exceptions raised by a callback are logged and
        suppressed; one buggy callback never takes the watcher down.

        **Fallback path only.** This is the secondary signal for K=3
        replication repair-on-death; the framework's peer-removed hook
        (see plan: Framework Ask #4) is the primary signal with sub-
        tick latency. When the framework hook is wired, this callback
        becomes a redundant backstop for cases where the dying peer
        managed to run ``withdraw_self`` cleanly.
        """
        self._diff_callbacks.append(fn)

    # --- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def request_refresh(self) -> None:
        """Wake the run loop for an out-of-band refresh.

        Intended for :class:`PeerPushServer` to call on every
        ``path-have`` / ``path-gone`` push so a peer's newly-realised
        store paths are visible within ~one refresh instead of waiting
        a full tick.
        """
        self._wake_event.set()

    def run(self) -> None:  # pragma: no cover - integration-tested
        while not self._stop_event.is_set():
            self._wake_event.clear()
            try:
                self._refresh()
            except Exception:  # noqa: BLE001 - keep watcher alive
                logger.exception("PathPlacementWatcher refresh failed")
            self._wake_event.wait(self._tick_seconds)

    # --- internals ---------------------------------------------------------

    def _refresh(self, *, fire_callbacks: bool = True) -> None:
        new_placements = _read_all_placement_files(self._shared_fs)
        with self._lock:
            old_placements = self._placements
            self._placements = new_placements
        if not fire_callbacks or not self._diff_callbacks:
            return
        diff = _compute_placement_diff(old_placements, new_placements)
        if diff.is_empty():
            return
        for fn in self._diff_callbacks:
            try:
                fn(diff)
            except Exception:  # noqa: BLE001 - isolate buggy callback
                logger.exception(
                    "PathPlacementWatcher: diff callback raised"
                )


def _compute_placement_diff(
    old: dict[str, set[str]],
    new: dict[str, set[str]],
) -> PlacementDiff:
    """Symmetric-difference per outpath between two placement maps."""
    added: dict[str, set[str]] = {}
    removed: dict[str, set[str]] = {}
    all_outpaths = set(old) | set(new)
    for outpath in all_outpaths:
        old_sids = old.get(outpath, set())
        new_sids = new.get(outpath, set())
        gained = new_sids - old_sids
        lost = old_sids - new_sids
        if gained:
            added[outpath] = gained
        if lost:
            removed[outpath] = lost
    return PlacementDiff(added=added, removed=removed)


def _read_all_placement_files(
    shared_fs: pathlib.Path,
) -> dict[str, set[str]]:
    """Read every ``peers/_paths_*.jsonl`` and aggregate by outpath.

    Each record is one JSON object per line with at least
    ``{secondary_id, outpath}``. Malformed lines are skipped at debug
    level so one bad writer doesn't take the watcher down. Missing
    ``peers/`` returns an empty aggregate.
    """
    peers_dir = _peers_dir(shared_fs)
    aggregate: dict[str, set[str]] = {}
    try:
        entries = list(peers_dir.iterdir())
    except FileNotFoundError:
        return aggregate
    for entry in entries:
        if not entry.is_file():
            continue
        if not entry.name.startswith(PATHS_FILE_PREFIX):
            continue
        if entry.suffix != ".jsonl":
            continue
        try:
            raw = entry.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug(
                "PathPlacementWatcher: skipping unreadable %s: %s", entry, exc
            )
            continue
        for line_num, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "PathPlacementWatcher: bad line %s:%d: %s",
                    entry, line_num, exc,
                )
                continue
            if not isinstance(rec, dict):
                continue
            outpath = rec.get("outpath")
            sid = rec.get("secondary_id")
            if not isinstance(outpath, str) or not isinstance(sid, str):
                continue
            if not outpath or not sid:
                continue
            aggregate.setdefault(outpath, set()).add(sid)
    return aggregate


# ---------------------------------------------------------------------------
# /etc/nix/peer.conf live-rewriter
# ---------------------------------------------------------------------------


PEER_CONF_PATH = "/etc/nix/peer.conf"


class PeerNixConfWatcher(threading.Thread):
    """Translates a :class:`PeerListWatcher` snapshot into
    ``/etc/nix/peer.conf`` continuously, so every nix invocation
    inside the container picks up the live federation transparently.

    The image's baseline ``/etc/nix/nix.conf`` does
    ``!include /etc/nix/peer.conf`` (soft include — silently
    skipped if missing). Each refresh writes::

        extra-substituters = http://node-a:5000 http://node-b:5000
        extra-trusted-public-keys = name-a:KEY name-b:KEY

    so any subsequent ``nix-store --realise <path>`` /
    ``nix build`` / ``nix shell`` resolves through every peer's
    harmonia without the caller passing ``--from`` / ``--substituters``.

    Daemon thread; ``stop()`` requests exit at the next tick.
    """

    def __init__(
        self,
        peer_watcher: "PeerListWatcher",
        target_conf: str = PEER_CONF_PATH,
        tick_seconds: float = 2.5,
    ) -> None:
        super().__init__(name="PeerNixConfWatcher", daemon=True)
        self._watcher = peer_watcher
        self._target = pathlib.Path(target_conf)
        self._tick = float(tick_seconds)
        self._stop = threading.Event()
        self._last_signature: tuple | None = None
        # Quick writability probe: on the submitter side (not inside a
        # secondary container as root), ``/etc/nix`` is read-only. The
        # watcher's purpose is to keep the container's nix-daemon
        # config fresh — when there's no writable target, the refresh
        # loop is pointless. Detect once at construction and degrade
        # to a no-op rather than burning log noise on every tick.
        self._writable = self._probe_writable()
        if not self._writable:
            logger.info(
                "PeerNixConfWatcher: %s not writable from this process;"
                " skipping live peer.conf updates (submitter uses"
                " --option extra-substituters per-call instead).",
                self._target,
            )
            return
        # Prime peer.conf synchronously from the watcher's already-snapshotted
        # peer set so the first ``nix build`` invocation (which the framework
        # may dispatch within ~280 ms of bootstrap returning) sees the live
        # substituter set instead of an empty / missing file. The poll thread
        # below then keeps it fresh; without this prime, the first wave of
        # tasks falls back to cache.nixos.org and rebuilds toolchains that
        # would otherwise substitute from the dev-box harmonia.
        self._refresh_once(suppress_log=True)

    def _probe_writable(self) -> bool:
        """Return True iff this process can create/modify ``self._target``.

        We treat any of the following as ``not writable``:
        * the parent directory doesn't exist AND can't be created;
        * the parent directory exists but isn't writable.
        Used at construction to short-circuit when the watcher would
        otherwise emit a permission-denied traceback on every refresh.
        """
        parent = self._target.parent
        try:
            if not parent.is_dir():
                parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return os.access(parent, os.W_OK)

    def stop(self) -> None:
        self._stop.set()

    def _refresh_once(self, *, suppress_log: bool = False) -> None:
        try:
            peers = list(self._watcher.peers)
            urls = [p.substituter_url() for p in peers]
            keys = [p.public_key for p in peers if p.public_key]
            sig = (tuple(urls), tuple(keys))
            if sig != self._last_signature:
                self._write_conf(urls, keys)
                self._last_signature = sig
        except Exception:  # noqa: BLE001
            if not suppress_log:
                logger.exception("PeerNixConfWatcher refresh failed")

    def run(self) -> None:  # pragma: no cover — exercised via integration
        if not self._writable:
            return
        while not self._stop.is_set():
            self._refresh_once()
            self._stop.wait(self._tick)

    def _write_conf(self, urls: list[str], keys: list[str]) -> None:
        try:
            self._target.parent.mkdir(parents=True, exist_ok=True)
            body = ""
            if urls:
                body += "extra-substituters = " + " ".join(urls) + "\n"
            if keys:
                body += (
                    "extra-trusted-public-keys = "
                    + " ".join(keys)
                    + "\n"
                )
            tmp = self._target.with_suffix(self._target.suffix + ".tmp")
            tmp.write_text(body)
            tmp.replace(self._target)
            logger.debug(
                "peer.conf updated: %d peer(s)", len(urls),
            )
        except OSError:
            logger.exception("peer.conf write failed")


# ---------------------------------------------------------------------------
# Submitter-side peer integration
# ---------------------------------------------------------------------------


_SUBMITTER_KEY_DIR = pathlib.Path.home() / ".cache" / "asm-dataset-nix-runner"


def _generate_submitter_signing_key(
    key_dir: pathlib.Path,
) -> tuple[pathlib.Path, str]:
    """Get the submitter signing keypair, generating a stable one once.

    The keypair is **persistent across dispatches**, not regenerated
    each time. Reasoning: a previous dispatch's harmonia process can
    outlive its SubmitterPeer (e.g. when the dispatch crashes or
    Ctrl-C exits before ``stop()`` runs cleanly, or when SubmitterPeer
    fails to bind a fresh harmonia because the port's still held by
    the old one). The stale harmonia keeps signing artifacts with
    whatever signing key was loaded at its startup. If the next
    dispatch generated a NEW signing key, the secondaries' published
    ``trusted-public-keys`` would mismatch the stale harmonia's
    signatures and nix would refuse to substitute (signature check
    failure → fall back to building from source). Pinning the key
    name + reusing the on-disk keypair when present makes the key
    invariant across dispatches; any harmonia (running or freshly
    spawned) signs with the same key and every dispatch's secondaries
    trust it.

    Key name is stable (``asm-dataset-submitter-local``) — no
    timestamp suffix. The keypair is private to the dev-box and
    never leaves it, so single-name is fine for security.
    """
    key_dir.mkdir(parents=True, exist_ok=True)
    secret_path = key_dir / "submitter.key"
    pub_path = key_dir / "submitter.key.pub"

    # Reuse on-disk keypair if both halves are present + non-empty.
    # Idempotent across dispatches; a future operator can rotate by
    # deleting both files (next dispatch generates fresh).
    if secret_path.is_file() and pub_path.is_file():
        try:
            public_existing = pub_path.read_text("utf-8").strip()
            if public_existing.startswith("asm-dataset-submitter-"):
                return secret_path, public_existing
        except OSError:
            pass  # fall through to regenerate

    name = "asm-dataset-submitter-local"
    secret = subprocess.run(  # noqa: S603
        ["nix", "--extra-experimental-features", "nix-command",
         "key", "generate-secret", "--key-name", name],
        check=True, capture_output=True,
    ).stdout
    if not secret:
        raise RuntimeError("nix key generate-secret returned empty")
    secret_path.write_bytes(secret)
    secret_path.chmod(0o600)

    public = subprocess.run(  # noqa: S603
        ["nix", "--extra-experimental-features", "nix-command",
         "key", "convert-secret-to-public"],
        input=secret, check=True, capture_output=True,
    ).stdout
    pub_path.write_bytes(public)
    pub_path.chmod(0o644)
    return secret_path, public.decode("utf-8").strip()


class SubmitterPeer:
    """Adds the dispatching machine ("the submitter") to the cluster's
    peer-cache federation.

    Pairs with ``TaskDeploymentSpec.extra_port_forwards`` (added in
    dynamic-runner ``afa024e``): the framework's primary opens an
    SSH-R from the dev-box to the gateway as part of its existing
    ControlMaster, binding ``0.0.0.0:<gateway_port>`` on the gateway
    so compute-nodes reach our harmonia via cluster-internal network.

    Responsibilities of this class — narrowed to what the framework
    does NOT do:

      1. Generate a fresh signing keypair under
         ``~/.cache/asm-dataset-nix-runner/``. Short-lived; one per
         dispatch.
      2. Spawn ``harmonia-cache`` via
         ``nix shell nixpkgs#harmonia --command harmonia-cache`` so
         harmonia doesn't need to be in the dev shell's python env.
         Listens on ``127.0.0.1:<local_port>``.
      3. After the framework's dispatch creates the run dir on the
         gateway, drop ``peers/submitter.json`` referencing
         ``<gateway-host>:<gateway_port>`` and the public key. The
         :class:`PeerListWatcher` in each container picks this up
         within one tick (~3s).

    Caller's responsibility: pass the matching
    ``extra_port_forwards=((local_port, gateway_port),)`` to the
    framework's :class:`TaskDeploymentSpec` BEFORE calling
    ``dynamic_runner.run(...)``. The framework will refuse to bind
    if ``gateway_port`` is taken on the gateway host — pick a
    high randomised port if you may run multiple dispatches in
    parallel.

    Use as ``with SubmitterPeer(...) as p: ...`` so ``stop`` always
    fires (best-effort peer-file removal + harmonia teardown).
    """

    def __init__(
        self,
        gateway_url: str,
        slurm_root: str,
        local_port: int = 5005,
        gateway_port: int = 5005,
        identity_file: str | None = None,
        config_file: str | None = None,
        log: logging.Logger = logger,
    ) -> None:
        self.gateway_url = gateway_url
        self.slurm_root = slurm_root.rstrip("/")
        self.local_port = local_port
        self.gateway_port = gateway_port
        self.identity_file = identity_file
        self.config_file = config_file
        self.log = log

        self._gateway_host: str | None = None
        self._gateway_user: str | None = None
        self._gateway_ssh_port: int = 22
        self._harmonia: subprocess.Popen | None = None
        self._signing_key_path: pathlib.Path | None = None
        self._public_key: str | None = None

        self._stop_evt = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._peer_published = False
        self._placements_published = False
        # ``(outpath, drv_path, item_class)`` triples to advertise once
        # the run_id is known. Caller fills this before ``start()`` or
        # at any point during dispatch; the poll loop flushes on each
        # tick after the peer file is published.
        self._pending_placements: list[tuple[str, str, str]] = []
        # Captured at start(); _discover_run_id only accepts dirs we
        # didn't see at submitter init. Without this, polling races
        # the framework's run-dir creation and publishes the peer
        # file into a stale dispatch's run dir (which the current
        # dispatch's compute-nodes never read).
        self._initial_run_ids: set[str] = set()
        # Distributed-eval Phase -1 toolchain drv seed set. Buffered
        # here by the cli's distributed-eval submit path; the poll
        # loop flushes it via :meth:`seed_toolchain_drvs` exactly
        # once after the peer file is published AND a non-submitter
        # secondary peer file appears on the gateway. Empty set =
        # no-op (legacy local-eval flow).
        self._pending_toolchain_seed_drvs: set[str] = set()
        self._toolchain_seed_done = False

    def set_placements(
        self, outpaths: Iterable[tuple[str, str, str]],
    ) -> None:
        """Buffer placement records for the next ``_poll_loop`` tick.

        Each tuple is ``(outpath, drv_path, item_class)``. The poll
        loop flushes via :meth:`publish_placements` once ``run_id``
        is discovered + the peer file is published (so workers'
        substituters list AND placement map agree on the submitter's
        identity before they're told what paths it serves)."""
        self._pending_placements = list(outpaths)
        self._placements_published = False

    def set_pending_toolchain_seed_drvs(
        self, drv_set: Iterable[str],
    ) -> None:
        """Buffer the toolchain drv set for deferred seeding.

        Called by the cli's submit path before
        (or after) :meth:`start`. The :meth:`_poll_loop` calls
        :meth:`seed_toolchain_drvs` exactly once after both the
        submitter peer file is published AND a non-submitter peer
        file appears on the gateway (so we have a real
        ``first_secondary_url`` to broadcast to). Empty set short-
        circuits to a no-op.
        """
        self._pending_toolchain_seed_drvs = {str(d) for d in drv_set if d}
        self._toolchain_seed_done = False

    def __enter__(self) -> "SubmitterPeer":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    @property
    def deployment_extra_port_forwards(self) -> tuple[tuple[int, int], ...]:
        """Pass this into ``TaskDeploymentSpec.extra_port_forwards``
        on the same dispatch — the framework will route the SSH-R."""
        return ((self.local_port, self.gateway_port),)

    def start(self) -> None:
        # Parse the gateway URL once so stop() can issue cleanup ssh
        # commands without re-parsing.
        try:
            from dynamic_runner.packaging.gateway import (  # type: ignore[import-not-found]
                parse_gateway_url,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error(
                "dynamic_runner.packaging.gateway unavailable: %s; "
                "submitter peer aborted",
                exc,
            )
            return
        cfg = parse_gateway_url(self.gateway_url)
        if cfg.mode != "ssh":
            self.log.warning(
                "submitter-peer skipped: gateway is %s, not ssh",
                cfg.mode,
            )
            return
        self._gateway_host = cfg.ssh_host
        self._gateway_user = cfg.ssh_user
        self._gateway_ssh_port = cfg.ssh_port or 22

        # 1. fresh signing keypair.
        self._signing_key_path, self._public_key = (
            _generate_submitter_signing_key(_SUBMITTER_KEY_DIR)
        )
        self.log.info(
            "submitter signing key: %s...", self._public_key[:64],
        )

        # 2. harmonia-cache via nix shell nixpkgs#harmonia.
        config_path = _SUBMITTER_KEY_DIR / "harmonia.toml"
        config_path.write_text(
            f'bind = "127.0.0.1:{self.local_port}"\nworkers = 16\n'
        )
        env = dict(os.environ)
        env["CONFIG_FILE"] = str(config_path)
        env["SIGN_KEY_PATH"] = str(self._signing_key_path)
        env["SIGN_KEY_PATHS"] = str(self._signing_key_path)
        cmd = [
            "nix", "shell",
            "--extra-experimental-features", "nix-command flakes",
            "nixpkgs#harmonia",
            "--command", "harmonia-cache",
        ]
        log_path = _SUBMITTER_KEY_DIR / "harmonia.log"
        # If a previous SubmitterPeer left an orphaned harmonia
        # squatting on local_port, our new one will fail to bind with
        # EADDRINUSE while the probe still succeeds (the orphan answers
        # for ``/nix-cache-info``). Detect that state, clear the
        # squatter, and retry.
        for attempt in range(2):
            log_fh = open(log_path, "wb")
            self._harmonia = subprocess.Popen(  # noqa: S603
                cmd, env=env,
                stdout=log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            probe = (
                f"http://127.0.0.1:{self.local_port}/nix-cache-info"
            )
            bound_ok = False
            bind_failed = False
            for _ in range(40):
                if self._harmonia.poll() is not None:
                    # Subprocess already exited - either bind-failure
                    # or some other startup error. Read the log to
                    # discriminate.
                    try:
                        log_tail = log_path.read_bytes()[-512:].decode(
                            "utf-8", errors="replace"
                        )
                    except OSError:
                        log_tail = ""
                    if "AddrInUse" in log_tail or (
                        "Address already in use" in log_tail
                    ):
                        bind_failed = True
                    break
                try:
                    with urllib.request.urlopen(probe, timeout=0.5):
                        bound_ok = True
                        break
                except OSError:
                    time.sleep(0.25)

            if bound_ok:
                self.log.info(
                    "submitter harmonia listening on 127.0.0.1:%d "
                    "(framework will tunnel as 0.0.0.0:%d)",
                    self.local_port, self.gateway_port,
                )
                break

            if bind_failed and attempt == 0:
                # Wipe the squatter so the retry can bind. We only
                # touch processes named harmonia-cache to avoid
                # killing unrelated services that happened to
                # collide on the port.
                self.log.warning(
                    "submitter harmonia bind to :%d failed - clearing"
                    " orphan harmonia processes and retrying",
                    self.local_port,
                )
                try:
                    subprocess.run(  # noqa: S603,S607
                        ["pkill", "-KILL", "-f", "harmonia-cache"],
                        check=False, capture_output=True, timeout=5,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
                time.sleep(0.5)
                continue

            self.log.warning(
                "submitter harmonia did not respond on :%d within 10s"
                " (bind_failed=%s)", self.local_port, bind_failed,
            )
            break

        # Snapshot existing run dirs so we don't publish into a
        # stale dispatch's dir that's still on the gateway.
        self._initial_run_ids = self._list_run_ids()
        if self._initial_run_ids:
            self.log.debug(
                "submitter peer: %d existing run-dir(s) ignored",
                len(self._initial_run_ids),
            )

        # 3. polling thread that publishes the peer file after the
        # framework creates the run dir.
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="SubmitterPeerPoll",
            daemon=True,
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        # Best-effort peer-file removal so a future dispatch's
        # PeerListWatcher doesn't see a stale URL pointing at a
        # tunnel that's gone.
        if self._run_id and self._peer_published:
            remote_path = (
                f"{self.slurm_root}/log/{self._run_id}"
                "/peers/submitter.json"
            )
            self._ssh_oneshot([f"rm -f {remote_path}"])
        if self._harmonia is not None:
            # ``nix shell --command harmonia-cache`` runs in its own
            # session (``start_new_session=True``), so the Popen handle
            # points at the ``nix shell`` wrapper. ``terminate()`` would
            # only signal the wrapper - the actual ``harmonia-cache``
            # child survives, squats on the local port across
            # dispatches, and the next dispatch's harmonia hits
            # ``EADDRINUSE`` on bind. Send the signal to the whole
            # process group instead so harmonia-cache exits too.
            try:
                pgid = os.getpgid(self._harmonia.pid)
            except (OSError, ProcessLookupError):
                pgid = None
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    self._harmonia.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    try:
                        self._harmonia.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

    def _poll_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                if self._run_id is None:
                    self._run_id = self._discover_run_id()
                if (
                    self._run_id is not None
                    and not self._peer_published
                ):
                    self._publish_peer_file()
                    self._peer_published = True
                if (
                    self._run_id is not None
                    and self._peer_published
                    and not self._placements_published
                    and self._pending_placements
                ):
                    if self.publish_placements(self._pending_placements):
                        self._placements_published = True
                if (
                    self._run_id is not None
                    and self._peer_published
                    and not self._toolchain_seed_done
                    and self._pending_toolchain_seed_drvs
                ):
                    first_url = self._discover_first_secondary_url()
                    if first_url:
                        try:
                            self.seed_toolchain_drvs(
                                self._pending_toolchain_seed_drvs,
                                first_url,
                            )
                        except Exception:  # noqa: BLE001
                            self.log.exception(
                                "submitter-peer: deferred"
                                " seed_toolchain_drvs failed"
                            )
                        # One-shot regardless of per-drv success — the
                        # broadcast helper logs partial failures and the
                        # cluster keeps working via harmonia substitution.
                        self._toolchain_seed_done = True
                if (
                    self._peer_published
                    and (
                        self._placements_published
                        or not self._pending_placements
                    )
                    and (
                        self._toolchain_seed_done
                        or not self._pending_toolchain_seed_drvs
                    )
                ):
                    return
            except Exception:  # noqa: BLE001
                self.log.exception(
                    "submitter-peer poll iteration failed"
                )
            self._stop_evt.wait(2.0)

    def _discover_first_secondary_url(self) -> str | None:
        """Return one non-submitter secondary's push-listener base URL.

        Reads ``${slurm_root}/log/${run_id}/peers/`` on the gateway,
        picks any ``<id>.json`` that is NOT ``submitter.json`` or a
        reserved ``__*`` bookkeeping file, decodes its ``host`` +
        ``port`` fields, and returns the bare push base URL
        (``http://<host>:<push_port>``). ``push_port`` is derived
        from the peer's harmonia port via :func:`push_port_for`.
        Returns ``None`` if no secondary peer is published yet, or
        on any decode error — the caller retries on the next poll
        tick.
        """
        if not self._run_id:
            return None
        from compiler_suit_runner.peer_push import push_port_for
        peers_dir = f"{self.slurm_root}/log/{self._run_id}/peers"
        # ls -1 then filter; cat the first matching file to read its
        # JSON. Two round-trips per discovery attempt is fine since
        # this only fires once.
        rc, out, _err = self._ssh_oneshot([
            f"ls -1 {peers_dir} 2>/dev/null"
            " | grep -E '^[^_].*\\.json$'"
            " | grep -v '^submitter\\.json$'"
            " | head -1",
        ])
        if rc != 0 or not out.strip():
            return None
        peer_file = out.strip().splitlines()[0].strip()
        if not peer_file:
            return None
        rc2, blob, _err2 = self._ssh_oneshot([
            f"cat {peers_dir}/{peer_file} 2>/dev/null",
        ])
        if rc2 != 0 or not blob.strip():
            return None
        try:
            info = json.loads(blob)
        except (ValueError, TypeError):
            return None
        # Each secondary's ``peers/<id>.json`` (written by
        # :func:`announce_self`) carries ``hostname`` + ``port``;
        # the submitter's own ``peers/submitter.json`` uses ``host``.
        # We're filtering out submitter.json above, so prefer
        # ``hostname`` and fall back to ``host`` for robustness.
        host = info.get("hostname") or info.get("host")
        port = info.get("port")
        if not host or not isinstance(port, int):
            return None
        push_port = push_port_for(int(port))
        return f"http://{host}:{push_port}"

    def _ssh_oneshot(
        self, remote_cmds: list[str], stdin_input: bytes | None = None,
    ) -> tuple[int, str, str]:
        """Run a one-shot ssh command against the gateway. We don't
        share the framework's ControlMaster (it's owned by the
        primary's pipeline thread) — but we don't need to either:
        the file we're writing is small + infrequent, ad-hoc ssh
        amortises just fine.
        """
        if self._gateway_user and self._gateway_host:
            target = f"{self._gateway_user}@{self._gateway_host}"
        elif self._gateway_host:
            target = self._gateway_host
        else:
            return 1, "", "no gateway host"
        argv = ["ssh", "-o", "BatchMode=yes"]
        # Mirror the framework's gateway auth contract: with an explicit
        # identity, lock ssh to it and shut the agent out. Without these
        # the user's ~/.ssh/config (e.g. 1password agent socket via a
        # ``Match host *`` block) leaks all agent keys into auth, blowing
        # past sshd's MaxAuthTries on every poll-loop iteration and —
        # under OpenSSH 9.8+ ``PerSourcePenalties`` — landing the source
        # IP in the penalty box, which then drops the framework's own
        # gateway-master commands as collateral.
        if self.identity_file:
            argv.extend([
                "-i", self.identity_file,
                "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none",
            ])
        if self.config_file:
            argv.extend(["-F", self.config_file])
        argv.extend([
            "-p", str(self._gateway_ssh_port),
            target, "; ".join(remote_cmds),
        ])
        try:
            res = subprocess.run(  # noqa: S603
                argv, input=stdin_input, capture_output=True, timeout=15,
            )
            return (
                res.returncode,
                res.stdout.decode("utf-8", errors="replace"),
                res.stderr.decode("utf-8", errors="replace"),
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return 1, "", str(exc)

    def _list_run_ids(self) -> set[str]:
        rc, out, _err = self._ssh_oneshot([
            f"ls -1 {self.slurm_root}/log 2>/dev/null"
            " | grep -E '^run_[0-9_]+$'",
        ])
        if rc != 0:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _discover_run_id(self) -> str | None:
        """Wait for a run dir we didn't see at submitter init.

        Without this guard, ``ls | sort -r | head -1`` races the
        framework: if a stale run dir already exists on the gateway,
        we'd publish into it (and the *current* dispatch's
        compute-nodes — reading their *own* run dir — would never
        see the submitter peer).
        """
        current = self._list_run_ids()
        new = current - self._initial_run_ids
        if not new:
            return None
        # Pick the lexicographically-newest of the new dirs (handles
        # the unlikely "two dispatches start within the same poll
        # tick" case by deterministic tie-break).
        candidate = max(new)
        self.log.info(
            "submitter peer: discovered new run_id=%s", candidate
        )
        return candidate

    def publish_placements(
        self, outpaths: Iterable[tuple[str, str, str]],
    ) -> bool:
        """Write ``peers/_paths_submitter.jsonl`` to the remote run dir.

        ``outpaths`` is an iterable of ``(outpath, drv_path, item_class)``
        tuples — one record per store path the primary already has in
        its local nix store. Workers' :class:`PathPlacementWatcher`
        reads this file on every tick + on every ``path-have`` push and
        treats ``submitter`` as a fetch candidate for those paths.

        Without this file the placement map has no entry for the
        primary's toolchains, and the validate-only worker path fails
        with "no peer in the placement map could serve it" even though
        the primary's harmonia is reachable through the SSH-R fan-out.

        Returns True on success, False on any failure (network /
        permission). Best-effort: caller should not abort dispatch
        on failure.
        """
        if self._run_id is None or self._gateway_host is None:
            self.log.warning(
                "publish_placements called before run_id discovered;"
                " skipping"
            )
            return False
        records: list[str] = []
        for outpath, drv_path, item_class in outpaths:
            if not isinstance(outpath, str) or not outpath:
                continue
            rec = {
                "secondary_id": "submitter",
                "outpath": outpath,
                "drv_path": drv_path or "",
                "item_class": item_class or "",
                "ts": time.time(),
            }
            records.append(json.dumps(rec, sort_keys=True))
        if not records:
            return True
        body = "\n".join(records) + "\n"
        remote_dir = f"{self.slurm_root}/log/{self._run_id}/peers"
        remote_path = f"{remote_dir}/_paths_submitter.jsonl"

        import base64
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        rc, _out, err = self._ssh_oneshot([
            f"mkdir -p {remote_dir}",
            f"echo {b64} | base64 -d > {remote_path}",
        ])
        if rc != 0:
            self.log.warning(
                "publish_placements failed: rc=%d err=%s", rc, err
            )
            return False
        self.log.info(
            "submitter placements published: %d outpath(s) (run %s)",
            len(records), self._run_id,
        )
        return True

    def _publish_peer_file(self) -> None:
        assert self._run_id is not None
        assert self._public_key is not None
        assert self._gateway_host is not None
        # The framework fan-outs ``extra_port_forwards`` per-secondary
        # over the primary→secondary ProxyJump SSH session (since
        # dynamic-runner c89775c), so each compute node sees the
        # submitter on its OWN loopback. Workers reach the submitter
        # at ``http://localhost:<gateway_port>`` regardless of the
        # gateway's sshd ``GatewayPorts`` setting. See
        # ``TaskDeploymentSpec.extra_port_forwards`` in the framework
        # docstring for the contract.
        payload = {
            "secondary_id": "submitter",
            "hostname": "localhost",
            "port": self.gateway_port,
            "public_key": self._public_key,
        }
        body = json.dumps(payload, indent=2, sort_keys=True)
        remote_dir = (
            f"{self.slurm_root}/log/{self._run_id}/peers"
        )
        remote_path = f"{remote_dir}/submitter.json"

        # base64-encode to keep the remote shell command quoting-safe.
        import base64
        b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        rc, _out, err = self._ssh_oneshot([
            f"mkdir -p {remote_dir}",
            f"echo {b64} | base64 -d > {remote_path}",
        ])
        if rc != 0:
            self.log.warning(
                "publish peer file failed: rc=%d err=%s", rc, err
            )
            return
        self.log.info(
            "submitter peer published: localhost:%d (run %s, "
            "fan-out terminates on each compute node via "
            "ProxyJump-side -R)",
            self.gateway_port, self._run_id,
        )

    # ------------------------------------------------------------------
    # Phase -1 bootstrap — toolchain drv broadcast seeding
    # ------------------------------------------------------------------

    # The submitter participates in the cluster peer mesh with this
    # stable id (matches the secondary_id written by
    # :meth:`_publish_peer_file`). Broadcast receivers use it as the
    # ``origin_peer_id`` dedup tie-breaker; logs surface it as the
    # broadcast originator.
    SUBMITTER_PEER_ID = "submitter"

    @property
    def peer_id(self) -> str:
        """Submitter's stable peer id within the cluster federation."""
        return self.SUBMITTER_PEER_ID

    def _first_secondary_reachable(self, base_url: str) -> bool:
        """Probe ``<base_url>/healthz`` (or root) with a short timeout.

        Off-cluster submitters can't reach the compute-node's HTTP push
        listener directly; the framework's `-R` reverse tunnel is the
        only path back. Probing once before the flood-fill loop avoids
        burning N × push-timeout on guaranteed-failing requests.
        """
        import socket
        import urllib.error
        import urllib.parse
        import urllib.request
        parsed = urllib.parse.urlsplit(base_url)
        if not parsed.scheme or not parsed.netloc:
            return False
        probe_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "/", "", "")
        )
        try:
            req = urllib.request.Request(probe_url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
                return resp.status < 500
        except urllib.error.HTTPError as exc:
            # 4xx means the listener IS up; we treat any HTTP response
            # as proof of reachability.
            return 400 <= exc.code < 500
        except (urllib.error.URLError, TimeoutError, OSError, socket.timeout):
            return False

    def seed_toolchain_drvs(
        self,
        drv_set: set[str],
        first_secondary_url: str,
    ) -> dict:
        """Seed the cluster's toolchain drv flood-fill (Phase -1).

        For every drv path in *drv_set*, POSTs a
        ``/peer/path-broadcast-offer`` to a single secondary's push
        listener (``first_secondary_url`` — the bare base URL, e.g.
        ``http://node1:6000``). The receiver's broadcast handler
        cascades each drv to ALL OTHER peers via ``fan_out_broadcast_drv``,
        so the submitter only needs to talk to one secondary.

        Returns ``{"sent": int, "failed": int, "failed_drvs":
        list[str]}``. An empty *drv_set* short-circuits to all-zeros
        without any network activity. Per drv we resolve the on-disk
        size (``Path(drv).stat().st_size``), mint a fresh UUID-hex
        ``broadcast_id``, set ``hop_count=0`` (this submitter is the
        originator), and call
        :func:`peer_push.push_path_broadcast_offer`. Failures (None
        return from the helper, missing on-disk file, etc.) accumulate
        in ``failed_drvs`` rather than aborting the loop — the broadcast
        is best-effort; the cluster keeps working with a partially
        seeded drv set, at the cost of secondaries having to re-fetch
        the missing drv files via harmonia substitution.
        """
        if not drv_set:
            return {"sent": 0, "failed": 0, "failed_drvs": []}

        # Import lazily so this method does not pull peer_push into
        # peer_cache's import graph (peer_push already depends on
        # peer_cache for PeerInfo — see peer_push.py's top imports).
        from compiler_suit_runner import peer_push

        our_pubkey = self._public_key or ""

        # Reachability probe before the loop: when the submitter is
        # OFF the cluster network (e.g. LMU Krater dispatched from a
        # laptop), the compute-node port 6000 is NOT reachable from
        # the submitter — only the framework's `-R` reverse-tunnelled
        # `localhost:5005` goes the other way. Per the operational
        # memory and cluster_dispatch_pitfalls notes, this is structural
        # noise: the workers pull from the submitter's harmonia, no
        # push needed. Skipping the per-drv loop avoids 317×2s = ~10 min
        # of submitter time burned on guaranteed-failing POSTs.
        if not self._first_secondary_reachable(first_secondary_url):
            self.log.info(
                "seed_toolchain_drvs: %s unreachable from submitter; "
                "skipping flood-fill (workers pull via -R tunnel) "
                "[%d drv(s) not broadcast]",
                first_secondary_url, len(drv_set),
            )
            return {
                "sent": 0,
                "failed": 0,
                "failed_drvs": [],
                "skipped_unreachable": True,
            }

        sent = 0
        failed = 0
        failed_drvs: list[str] = []
        for drv in sorted(drv_set):
            try:
                size = pathlib.Path(drv).stat().st_size
            except OSError as exc:
                self.log.warning(
                    "seed_toolchain_drvs: cannot stat %s: %s", drv, exc,
                )
                failed += 1
                failed_drvs.append(drv)
                continue
            broadcast_id = uuid.uuid4().hex
            self.log.info(
                "seed_toolchain_drvs: broadcasting %s "
                "(size=%d, broadcast_id=%s) -> %s",
                drv, size, broadcast_id, first_secondary_url,
            )
            response = peer_push.push_path_broadcast_offer(
                target_url=first_secondary_url,
                path=drv,
                size=size,
                origin_peer_id=self.peer_id,
                broadcast_id=broadcast_id,
                hop_count=0,
                our_pubkey=our_pubkey,
            )
            if response is None:
                self.log.warning(
                    "seed_toolchain_drvs: broadcast for %s failed "
                    "(target=%s)",
                    drv, first_secondary_url,
                )
                failed += 1
                failed_drvs.append(drv)
            else:
                sent += 1
        return {
            "sent": sent,
            "failed": failed,
            "failed_drvs": failed_drvs,
        }

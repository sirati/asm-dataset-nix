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
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "PeerInfo",
    "SigningKey",
    "generate_signing_key",
    "announce_self",
    "withdraw_self",
    "list_peers",
    "assemble_substituter_env",
    "build_nix_extra_args",
    "PeerListWatcher",
    "prune_stale",
    "HarmoniaProcess",
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


def _atomic_write_json(path: pathlib.Path, data: object) -> None:
    """Atomically write *data* as JSON to *path*.

    Writes to ``path.with_suffix(path.suffix + ".tmp")``, fsyncs, then
    renames into place. Concurrent readers see either the old contents
    or the fully written new contents — never a torn write.
    """
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    # Open with O_CREAT|O_WRONLY|O_TRUNC; mode 0o644 (or 0o600 for keys
    # — caller is responsible for chmoding after).
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _atomic_write_bytes(path: pathlib.Path, data: bytes, mode: int) -> None:
    """Atomic write of raw bytes with explicit file mode."""
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
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


# ---------------------------------------------------------------------------
# Signing key bootstrap
# ---------------------------------------------------------------------------


def generate_signing_key(
    shared_fs: pathlib.Path, run_id: str
) -> SigningKey:
    """Generate (or reload) the cluster-wide nix signing keypair.

    Idempotent: if ``peers/__signing-key`` and ``peers/__public-key``
    already exist for the same run, reload them instead of regenerating
    so that all secondaries see the same key even if more than one
    races to bootstrap.

    The secret is written with mode 0600, the public with mode 0644.

    Requires the ``nix`` CLI (looked up via ``shutil.which``) only when
    the keys do not already exist on disk. A missing ``nix`` binary
    raises :class:`FileNotFoundError`.
    """
    peers = _peers_dir(shared_fs)
    secret_path = peers / "__signing-key"
    public_path = peers / "__public-key"
    name = f"asm-suit-cluster-{run_id}"

    if secret_path.exists() and public_path.exists():
        try:
            public_key = public_path.read_text(encoding="utf-8").strip()
            # If the existing key has a matching name prefix, reuse it.
            if public_key.startswith(name + ":") or public_key.startswith(
                "asm-suit-cluster-"
            ):
                return SigningKey(
                    name=name,
                    secret_path=secret_path,
                    public_path=public_path,
                    public_key=public_key,
                )
        except OSError:
            # Fall through to regenerate if we can't read.
            pass

    nix = shutil.which("nix")
    if nix is None:
        raise FileNotFoundError(
            "nix CLI not found in PATH; cannot generate signing key"
        )

    # nix key generate-secret --key-name <name>  -> secret on stdout
    secret_proc = subprocess.run(
        [nix, "key", "generate-secret", "--key-name", name],
        check=True,
        capture_output=True,
    )
    secret_bytes = secret_proc.stdout
    if not secret_bytes:
        raise RuntimeError("nix key generate-secret produced empty output")

    _atomic_write_bytes(secret_path, secret_bytes, mode=0o600)

    # nix key convert-secret-to-public  (reads from stdin)
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

    _atomic_write_bytes(public_path, public_bytes, mode=0o644)

    return SigningKey(
        name=name,
        secret_path=secret_path,
        public_path=public_path,
        public_key=public_bytes.decode("utf-8").strip(),
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
    """Remove ``peers/<secondary_id>.json`` (no error if absent)."""
    peers = _peers_dir(shared_fs)
    target = peers / f"{secondary_id}.json"
    try:
        target.unlink()
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
    peers: list[PeerInfo], substitute_on_destination: bool = True
) -> list[str]:
    """Build ``nix build`` arguments for peer substitution.

    Returns a list of args ready to splat into ``nix build`` after the
    attribute. Empty list -> empty list (no flags at all).

    ``--substitute-on-destination`` is the key mechanic that turns
    "first to build wins" into a passive announcement: harmonia pushes
    the freshly built path to peers as part of the build graph rather
    than waiting for them to poll.
    """
    if not peers:
        return []
    urls = " ".join(p.substituter_url() for p in peers)
    keys = " ".join(p.public_key for p in peers)
    args = [
        "--extra-substituters",
        urls,
        "--extra-trusted-public-keys",
        keys,
    ]
    if substitute_on_destination:
        args.append("--substitute-on-destination")
    return args


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


class PeerListWatcher(threading.Thread):
    """Background thread that polls the peers/ dir and rebuilds args.

    Provides:

    * ``self.peers`` — current snapshot ``list[PeerInfo]`` (read under
      the internal lock; the property returns a fresh copy).
    * ``self.extra_args`` — current nix-build extra args
      (``list[str]``); same locking discipline.
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
        self._lock = threading.Lock()
        self._peers: list[PeerInfo] = []
        self._extra_args: list[str] = []
        # Prime the snapshot once synchronously so callers that read
        # immediately after .start() see a meaningful initial state.
        self._refresh()

    # --- public read-side API -------------------------------------------------

    @property
    def peers(self) -> list[PeerInfo]:
        with self._lock:
            return list(self._peers)

    @property
    def extra_args(self) -> list[str]:
        with self._lock:
            return list(self._extra_args)

    # --- lifecycle ------------------------------------------------------------

    def stop(self) -> None:
        """Request the thread to exit at its next tick."""
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover - exercised via integration
        while not self._stop_event.is_set():
            try:
                self._refresh()
            except Exception:  # noqa: BLE001 - keep the watcher alive
                logger.exception("PeerListWatcher refresh failed")
            # Wait either for the tick or for stop().
            self._stop_event.wait(self._tick_seconds)

    # --- internals ------------------------------------------------------------

    def _refresh(self) -> None:
        new_peers = list_peers(self._shared_fs, exclude_id=self._exclude_id)
        new_args = build_nix_extra_args(
            new_peers,
            substitute_on_destination=self._substitute_on_destination,
        )
        with self._lock:
            self._peers = new_peers
            self._extra_args = new_args


# ---------------------------------------------------------------------------
# harmonia subprocess control
# ---------------------------------------------------------------------------


@dataclass
class HarmoniaProcess:
    """Context-manager wrapper around a ``harmonia`` (or fallback) server.

    ``harmonia`` is a rust reimplementation of ``nix-serve``; we prefer
    it for cluster fan-in. If the harmonia binary is missing we fall
    back to ``nix-serve`` (also looked up via :func:`shutil.which`).
    Missing both -> :class:`FileNotFoundError` from :meth:`start`.

    Tests typically monkeypatch :func:`subprocess.Popen` so no real
    server is spawned.
    """

    bind_addr: str
    signing_key_path: pathlib.Path
    binary: Optional[str] = None  # if None, autodetected on .start()
    extra_args: list[str] = field(default_factory=list)
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
        """
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        binary = self._resolve_binary()
        cmd = [binary, "--bind", self.bind_addr, *self.extra_args]
        env = dict(os.environ)
        # harmonia / nix-serve consult these env vars for signing.
        env["SIGN_KEY_PATH"] = str(self.signing_key_path)
        env["NIX_SECRET_KEY_FILE"] = str(self.signing_key_path)
        logger.info("starting binary cache server: %s", cmd)
        self._proc = subprocess.Popen(  # noqa: S603 - command is constructed safely
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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

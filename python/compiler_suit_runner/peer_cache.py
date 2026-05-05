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
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

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
    "prune_stale",
    "HarmoniaProcess",
    "start_nix_daemon",
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


def _write_substituters_file(
    target: pathlib.Path, args: list[str]
) -> None:
    """Atomically write ``args`` (one per line) to ``target``.

    Uses a sibling ``.tmp`` + ``os.replace`` so concurrent readers
    never observe a partially-written file. Empty ``args`` produces
    an empty (but present) file, which workers treat as "no peers".
    """
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
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
        self._lock = threading.Lock()
        self._peers: list[PeerInfo] = []
        self._substituters_path = (
            _peers_dir(self._shared_fs) / SUBSTITUTERS_FILENAME
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

            # Write a TOML config in the same dir as the signing key
            # (already user-private). Workers default to 2 — enough for
            # cluster fan-in without saturating the secondary.
            cfg_dir = self.signing_key_path.parent
            cfg_path = cfg_dir / "harmonia.toml"
            cfg_path.write_text(
                f'bind = "{self.bind_addr}"\nworkers = 2\n'
            )
            env["CONFIG_FILE"] = str(cfg_path)
            cmd = [cache_bin, *self.extra_args]
        else:
            # nix-serve (or anything else): legacy --bind flag.
            cmd = [binary, "--bind", self.bind_addr, *self.extra_args]

        logger.info("starting binary cache server: %s", cmd)
        # Detached + log-to-file (NOT PIPE — PIPE without a drainer
        # blocks once kernel buffer fills, killing harmonia silently
        # under heavy load. We tee to a file alongside the signing
        # key so operators can find the log.)
        log_path = self.signing_key_path.parent / "harmonia.log"
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

    Returns the PID, or None if the daemon was already running.
    """
    if os.path.exists(NIX_DAEMON_SOCKET):
        return None
    # Synchronously initialize the local store DB so concurrent
    # workers (and harmonia, which is started right after this
    # function returns) see a fully-formed schema. Best-effort —
    # if nix-store is missing or the call fails we let the daemon
    # retry the init itself, preserving the existing behaviour.
    nix_store = shutil.which("nix-store") or "/bin/nix-store"
    if os.path.exists(nix_store):
        try:
            subprocess.run(  # noqa: S603 - argv constructed in-module
                [nix_store, "--init"],
                check=False,
                capture_output=True,
                shell=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            # fall through to daemon spawn; daemon will retry init
            pass
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
    daemon_env = {k: v for k, v in os.environ.items() if k != "NIX_REMOTE"}
    proc = subprocess.Popen(  # noqa: S603
        [binary],
        env=daemon_env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    # Wait up to ~5s for the socket to appear.
    for _ in range(20):
        if os.path.exists(NIX_DAEMON_SOCKET):
            return proc.pid
        time.sleep(0.25)
    return proc.pid


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
        # Prime peer.conf synchronously from the watcher's already-snapshotted
        # peer set so the first ``nix build`` invocation (which the framework
        # may dispatch within ~280 ms of bootstrap returning) sees the live
        # substituter set instead of an empty / missing file. The poll thread
        # below then keeps it fresh; without this prime, the first wave of
        # tasks falls back to cache.nixos.org and rebuilds toolchains that
        # would otherwise substitute from the dev-box harmonia.
        self._refresh_once(suppress_log=True)

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
    """Make a fresh submitter signing keypair under ``key_dir``.

    Independent of :func:`generate_signing_key` (which targets the
    cluster-shared NFS dir): the submitter's keypair is short-lived,
    regenerated each dispatch, and never leaves the local machine.
    """
    key_dir.mkdir(parents=True, exist_ok=True)
    secret_path = key_dir / "submitter.key"
    pub_path = key_dir / "submitter.key.pub"
    name = f"asm-dataset-submitter-{int(time.time())}"

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
        log: logging.Logger = logger,
    ) -> None:
        self.gateway_url = gateway_url
        self.slurm_root = slurm_root.rstrip("/")
        self.local_port = local_port
        self.gateway_port = gateway_port
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
        # Captured at start(); _discover_run_id only accepts dirs we
        # didn't see at submitter init. Without this, polling races
        # the framework's run-dir creation and publishes the peer
        # file into a stale dispatch's run dir (which the current
        # dispatch's compute-nodes never read).
        self._initial_run_ids: set[str] = set()

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
            f'bind = "127.0.0.1:{self.local_port}"\nworkers = 2\n'
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
        log_fh = open(log_path, "wb")
        self._harmonia = subprocess.Popen(  # noqa: S603
            cmd, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        probe = f"http://127.0.0.1:{self.local_port}/nix-cache-info"
        for _ in range(40):
            try:
                with urllib.request.urlopen(probe, timeout=0.5):
                    self.log.info(
                        "submitter harmonia listening on 127.0.0.1:%d "
                        "(framework will tunnel as 0.0.0.0:%d)",
                        self.local_port, self.gateway_port,
                    )
                    break
            except OSError:
                time.sleep(0.25)
        else:
            self.log.warning(
                "submitter harmonia did not respond on :%d within 10s",
                self.local_port,
            )

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
            try:
                self._harmonia.terminate()
            except OSError:
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
                    return
            except Exception:  # noqa: BLE001
                self.log.exception(
                    "submitter-peer poll iteration failed"
                )
            self._stop_evt.wait(2.0)

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
        argv = [
            "ssh", "-o", "BatchMode=yes",
            "-p", str(self._gateway_ssh_port),
            target, "; ".join(remote_cmds),
        ]
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

    def _publish_peer_file(self) -> None:
        assert self._run_id is not None
        assert self._public_key is not None
        assert self._gateway_host is not None
        # c89775c+: extra_port_forwards fan out per-compute-node as
        # `ssh -J gateway -R <gateway_port>:localhost:<local_port>
        # compute-node`. From every compute-node's perspective, the
        # submitter's harmonia is reachable as
        # `http://localhost:<gateway_port>` regardless of whether the
        # gateway has GatewayPorts=on or =off — the fan-out makes the
        # URL shape stable. Publish localhost so peers in the
        # container hit the per-compute-node tunnel.
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
            "submitter peer published: %s:%d (run %s)",
            self._gateway_host, self.gateway_port, self._run_id,
        )

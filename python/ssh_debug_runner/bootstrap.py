"""Container-startup orchestration for the ssh-debug image.

Brings up — automatically, on every podman start — the daemons and
peer-cache federation needed for transparent nix-store sharing
between all containers in the SLURM run:

  1. ``nix-daemon`` — required by both ``harmonia-cache`` 3.x
     (it talks to ``/nix/var/nix/daemon-socket/socket`` for store
     queries) and by interactive nix invocations from ssh sessions.
  2. ``harmonia-cache`` — peer binary cache server, listens on
     ``0.0.0.0:5000``. Every other container in the run polls
     this and substitutes from it on demand.
  3. **Shared signing key** — generated once on the NFS-shared SLURM
     log dir (mounted at ``/app/log-network``). All secondaries
     pick up the same key, so paths produced by one container are
     trusted by every other.
  4. **Self-announcement** — write
     ``/app/log-network/peers/<secondary_id>.json`` with hostname +
     port + public key.
  5. **Peer-list watcher** — background thread that polls
     ``/app/log-network/peers/``, takes the live set, and rewrites
     ``/etc/nix/peer.conf`` with ``extra-substituters`` and
     ``extra-trusted-public-keys`` for every peer. The image's
     baseline ``/etc/nix/nix.conf`` does ``!include /etc/nix/peer.conf``
     so any nix invocation (interactive or framework-driven) sees
     the federation transparently.

Standalone mode: when the image is run directly (no shared FS at
``/app/log-network``), only steps 1+2 fire and the container has
nix-daemon + harmonia but no peer set. Useful for ``podman run
image:tag serve`` smoke tests.

Reuses :mod:`compiler_suit_runner.peer_cache` (vendored into the same
image) for the gossip + signing-key bootstrap; we only add the
nix.conf rewrite layer on top.
"""

from __future__ import annotations

import logging
import os
import socket as _socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


LOG = logging.getLogger("ssh_debug_runner.bootstrap")

RUNTIME = "/tmp/ssh-debug"
BOOTSTRAP_LOG = f"{RUNTIME}/bootstrap.log"
NIX_DAEMON_LOG = f"{RUNTIME}/nix-daemon.log"
HARMONIA_LOG = f"{RUNTIME}/harmonia.log"

# Default port for our own harmonia. Pinning it per-container is fine
# because every SLURM container gets its own host network namespace
# (--network host means the compute-node host's :5000, but each
# compute-node only runs one secondary container so no conflict).
DEFAULT_HARMONIA_PORT = 5000

# Where the watcher writes the federation snippet that the baseline
# /etc/nix/nix.conf includes via `!include`.
PEER_CONF_PATH = "/etc/nix/peer.conf"

# NFS mount the framework drops into every SLURM container. Maps to
# ~/BIG/slurm/log/<run_id>/ on the gateway.
LOG_NETWORK_DIR = "/app/log-network"


def _diag(msg: str) -> None:
    """Append a timestamped line to bootstrap.log. Best-effort."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    try:
        Path(RUNTIME).mkdir(parents=True, exist_ok=True)
        with open(BOOTSTRAP_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError:
        pass


def start_harmonia(port: int, signing_key_path: Path) -> int | None:
    """Start ``/bin/harmonia-cache`` bound to ``0.0.0.0:port`` detached.

    Probes ``http://127.0.0.1:port/nix-cache-info`` until it returns
    (up to 5s) so callers can rely on the cache being ready when
    we return.
    """
    config_path = f"{RUNTIME}/harmonia.toml"
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text(
        f'bind = "0.0.0.0:{port}"\nworkers = 2\n'
    )

    env = dict(os.environ)
    env["CONFIG_FILE"] = config_path
    # SIGN_KEY_PATH is the legacy name (deprecated in 3.0 — still works);
    # SIGN_KEY_PATHS (plural) is the new name. Set both for forward-compat.
    env["SIGN_KEY_PATH"] = str(signing_key_path)
    env["SIGN_KEY_PATHS"] = str(signing_key_path)

    Path(HARMONIA_LOG).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["/bin/harmonia-cache"],
        env=env,
        stdout=open(HARMONIA_LOG, "ab"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    probe_url = f"http://127.0.0.1:{port}/nix-cache-info"
    for _ in range(20):
        try:
            with urllib.request.urlopen(probe_url, timeout=0.5):
                _diag(f"harmonia: PID={proc.pid} ready on :{port}")
                return proc.pid
        except OSError:
            time.sleep(0.25)
    _diag(f"harmonia: PID={proc.pid} not responding on :{port} within 5s")
    return proc.pid


def bootstrap(
    secondary_id: str,
    *,
    harmonia_port: int = DEFAULT_HARMONIA_PORT,
) -> dict[str, Any]:
    """Run all the container-startup tasks. Idempotent within a process.

    Returns a dict capturing the started resources (handy for tests
    and for parent code that may want to keep references alive).
    """
    Path(RUNTIME).mkdir(parents=True, exist_ok=True)
    _diag(f"bootstrap start secondary_id={secondary_id}")

    state: dict[str, Any] = {
        "nix_daemon_pid": None,
        "harmonia_pid": None,
        "signing_key": None,
        "peer_watcher": None,
        "nix_conf_watcher": None,
    }

    # Delegate to compiler_suit_runner.peer_cache.start_nix_daemon so the
    # debug image inherits the synchronous ``nix-store --init`` race fix
    # for the SQLite schema (see peer_cache.py docstring).
    try:
        from compiler_suit_runner.peer_cache import (
            start_nix_daemon as _start_nix_daemon,
        )
        state["nix_daemon_pid"] = _start_nix_daemon(Path(NIX_DAEMON_LOG))
    except Exception as exc:  # noqa: BLE001
        _diag(f"start_nix_daemon failed: {exc}")

    shared = Path(LOG_NETWORK_DIR)
    standalone = not shared.is_dir()
    if standalone:
        _diag(
            f"shared FS {LOG_NETWORK_DIR} absent — standalone mode "
            "(harmonia ON, peer-watcher OFF)"
        )

    # Lazy import: we want a clean ImportError if compiler_suit_runner
    # isn't on PYTHONPATH (rather than failing at module import time).
    try:
        from compiler_suit_runner.peer_cache import (
            PeerInfo,
            PeerListWatcher,
            PeerNixConfWatcher,
            announce_self,
            generate_signing_key,
        )
    except Exception as exc:  # noqa: BLE001
        _diag(f"compiler_suit_runner.peer_cache import failed: {exc}")
        return state

    # Pick a key path: shared in cluster mode, container-local
    # otherwise. Standalone keys are throwaway — every fresh podman
    # run gets a new keypair.
    key_dir = shared if not standalone else Path(RUNTIME)
    key_run_id = "ssh-debug-cluster" if not standalone else "ssh-debug-standalone"
    try:
        signing_key = generate_signing_key(key_dir, key_run_id)
        state["signing_key"] = signing_key
        _diag(
            f"signing key OK: name={signing_key.name} "
            f"public={signing_key.public_key[:64]}..."
        )
    except Exception as exc:  # noqa: BLE001
        _diag(f"signing key failed: {exc}")
        return state

    state["harmonia_pid"] = start_harmonia(
        harmonia_port, signing_key.secret_path
    )

    if standalone:
        # No peer set in standalone mode; we're done.
        return state

    try:
        host = _socket.gethostname()
        announce_self(
            shared,
            PeerInfo(
                secondary_id=secondary_id,
                hostname=host,
                port=harmonia_port,
                public_key=signing_key.public_key,
            ),
        )
        _diag(f"announced: {secondary_id} {host}:{harmonia_port}")
    except Exception as exc:  # noqa: BLE001
        _diag(f"announce_self failed: {exc}")
        return state

    try:
        peer_watcher = PeerListWatcher(
            shared_fs=shared,
            exclude_id=secondary_id,
            tick_seconds=3.0,
        )
        peer_watcher.start()
        state["peer_watcher"] = peer_watcher

        # Use the canonical peer_cache.PeerNixConfWatcher so the
        # debug image inherits the synchronous initial peer.conf
        # write — without that, the first ``nix build`` invocation
        # in the container races the watcher's first poll tick and
        # falls back to cache.nixos.org.
        nix_conf_watcher = PeerNixConfWatcher(peer_watcher)
        nix_conf_watcher.start()
        state["nix_conf_watcher"] = nix_conf_watcher
        _diag("peer watcher + nix.conf watcher running")
    except Exception as exc:  # noqa: BLE001
        _diag(f"peer-watcher start failed: {exc}")

    _diag("bootstrap complete")
    return state

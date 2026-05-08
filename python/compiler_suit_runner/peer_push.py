"""Push-based peer discovery between SLURM secondaries.

Augments the polling :class:`peer_cache.PeerListWatcher` with HTTP push
notifications: when a secondary calls ``announce_self`` (or
``withdraw_self``) it POSTs to every currently-known peer's push port
(harmonia_port + ``PUSH_PORT_OFFSET``) so each peer can wake its
watcher and re-read ``peers/`` immediately, without waiting for the
next NFS poll. The polling watcher remains as a safety net (default
tick relaxed to 60 s when push is enabled) for join races and lost
packets.

Wire format (single endpoint per event, JSON body, JSON-only response
codes):

    POST /peer/announce
        body: PeerInfo as ``{secondary_id, hostname, port, public_key}``
    POST /peer/withdraw
        body: ``{secondary_id}``

Auth is intra-cluster only: every request carries
``X-Cluster-PubKey: <our public key>``; the server rejects requests
whose header value does not match its own (i.e. is from outside the
SLURM run). Push failures are non-fatal — the polling safety net
covers any peer the push couldn't reach.

The server is a daemon thread spawned during
:meth:`SuitTaskMaster.on_run_start` and stopped in
:meth:`SuitTaskMaster.on_run_end`; it never owns blocking work so a
crash here cannot deadlock the secondary.
"""

from __future__ import annotations

import http.server
import json
import logging
import socket as _socket
import threading
import urllib.error
import urllib.request
from typing import Callable, Optional

from compiler_suit_runner.peer_cache import PeerInfo

__all__ = [
    "CLUSTER_PUBKEY_HEADER",
    "PUSH_PORT_OFFSET",
    "PeerPushServer",
    "fan_out_announce",
    "fan_out_withdraw",
    "push_port_for",
    "push_to_peer",
]

logger = logging.getLogger(__name__)

# Push listener binds at ``harmonia_port + PUSH_PORT_OFFSET``. Choosing
# +1000 keeps push ports well clear of the harmonia range (5000-5xxx
# in our defaults) and any submitter-tunnel port, while staying inside
# the unprivileged port space.
PUSH_PORT_OFFSET = 1000

# Cluster-internal authentication: the cluster's shared signing
# public-key is sent as a request header. A foreign caller without
# the right pubkey is rejected with 403.
CLUSTER_PUBKEY_HEADER = "X-Cluster-PubKey"

# Conservative HTTP timeouts; push is best-effort, the polling safety
# net handles peers we couldn't reach within the deadline.
DEFAULT_PUSH_TIMEOUT = 2.0


def push_port_for(harmonia_port: int) -> int:
    """Return the push-listener port that pairs with *harmonia_port*."""
    return int(harmonia_port) + PUSH_PORT_OFFSET


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _PushHandler(http.server.BaseHTTPRequestHandler):
    """Per-instance handler. Subclassed dynamically by
    :class:`PeerPushServer` so the callbacks + expected pubkey are
    bound as class attributes (``BaseHTTPRequestHandler`` is
    constructed by the server with no extra args)."""

    expected_pubkey: str = ""
    on_announce: Callable[[PeerInfo], None] = staticmethod(lambda info: None)
    on_withdraw: Callable[[str], None] = staticmethod(lambda sid: None)

    # Quiet the default access log; the framework's own logging is
    # the system of record.
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        del fmt, args
        return None

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _check_auth(self) -> bool:
        got = self.headers.get(CLUSTER_PUBKEY_HEADER, "")
        # Constant-time compare is overkill (the secret is the shared
        # cluster public-key, not a password); plain == is fine.
        return bool(got) and got == self.expected_pubkey

    def _respond(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        body = self._read_body()
        if not self._check_auth():
            self._respond(403)
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond(400)
            return
        try:
            if self.path == "/peer/announce":
                info = PeerInfo(
                    secondary_id=str(data["secondary_id"]),
                    hostname=str(data["hostname"]),
                    port=int(data["port"]),
                    public_key=str(data["public_key"]),
                )
                self.on_announce(info)
            elif self.path == "/peer/withdraw":
                self.on_withdraw(str(data["secondary_id"]))
            else:
                self._respond(404)
                return
        except (KeyError, TypeError, ValueError):
            self._respond(400)
            return
        except Exception:  # noqa: BLE001 - server stays up
            logger.exception(
                "PeerPushServer: callback raised on %s", self.path,
            )
            self._respond(500)
            return
        self._respond(204)


class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """``ThreadingHTTPServer`` with ``allow_reuse_address`` so a quick
    secondary restart on the same port doesn't trip ``Address already
    in use`` on a still-draining socket."""

    allow_reuse_address = True
    daemon_threads = True


class PeerPushServer(threading.Thread):
    """Daemon thread that runs the push HTTP listener.

    Construct with the cluster public-key (used both as expected
    request header and as the server's identity) and two callbacks —
    typically ``watcher.request_refresh`` for both announce + withdraw,
    since the on-disk peers/ dir is still authoritative; push only
    needs to wake the watcher.

    Lifecycle:

    * ``start()`` — bind + spawn the thread (raises ``OSError`` on
      bind failure).
    * ``stop()`` — graceful shutdown (idempotent).

    The bind happens synchronously in ``start()`` so callers see
    bind failures immediately rather than at first request time.
    """

    def __init__(
        self,
        bind_host: str,
        port: int,
        expected_pubkey: str,
        on_announce: Callable[[PeerInfo], None],
        on_withdraw: Callable[[str], None],
    ) -> None:
        super().__init__(name="PeerPushServer", daemon=True)
        self._bind_host = str(bind_host)
        self._port = int(port)
        # Build a per-instance handler subclass so callbacks + pubkey
        # are bound to THIS server (handlers are instantiated by the
        # HTTPServer with no extra args).
        bound_announce = on_announce
        bound_withdraw = on_withdraw
        bound_pubkey = str(expected_pubkey)

        class _BoundHandler(_PushHandler):
            expected_pubkey = bound_pubkey
            on_announce = staticmethod(bound_announce)
            on_withdraw = staticmethod(bound_withdraw)

        self._handler_cls = _BoundHandler
        self._server: Optional[_ThreadingHTTPServer] = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return self._port

    @property
    def bind_host(self) -> str:
        return self._bind_host

    def start(self) -> None:  # type: ignore[override]
        with self._lock:
            if self._server is not None:
                return  # idempotent
            self._server = _ThreadingHTTPServer(
                (self._bind_host, self._port), self._handler_cls,
            )
        super().start()

    def run(self) -> None:  # pragma: no cover - integration-tested
        srv = self._server
        if srv is None:
            return
        try:
            srv.serve_forever(poll_interval=0.5)
        except Exception:  # noqa: BLE001
            logger.exception("PeerPushServer: serve_forever crashed")

    def stop(self) -> None:
        with self._lock:
            srv = self._server
            self._server = None
        if srv is None:
            return
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("PeerPushServer: shutdown failed")
        try:
            srv.server_close()
        except Exception:  # noqa: BLE001
            logger.exception("PeerPushServer: server_close failed")


# ---------------------------------------------------------------------------
# Push client helpers
# ---------------------------------------------------------------------------


def push_to_peer(
    peer: PeerInfo,
    event: str,
    payload: dict,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> bool:
    """POST ``payload`` to ``peer``'s push listener for ``event``.

    Returns True on a 2xx response, False on any failure (network
    error, auth rejection, server error, malformed peer). Failures
    are silent at WARN level so a single missing peer does not fill
    the log on every announce.
    """
    if event not in ("announce", "withdraw"):
        raise ValueError(f"unsupported push event: {event!r}")
    if not peer.hostname:
        return False
    try:
        target_port = push_port_for(peer.port)
    except (TypeError, ValueError):
        return False
    url = f"http://{peer.hostname}:{target_port}/peer/{event}"
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        logger.exception(
            "push_to_peer: payload not JSON-serializable for %s", url,
        )
        return False
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            CLUSTER_PUBKEY_HEADER: str(our_pubkey),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except urllib.error.HTTPError as exc:
        logger.debug(
            "push_to_peer %s -> HTTP %d", url, exc.code,
        )
        return False
    except (urllib.error.URLError, TimeoutError, OSError, _socket.timeout) as exc:
        logger.debug("push_to_peer %s -> %s", url, exc)
        return False


def fan_out_announce(
    peers: list[PeerInfo],
    my_info: PeerInfo,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> int:
    """Push an ``announce`` event to every peer in *peers*.

    Returns the count of successful pushes. Failures are non-fatal
    (the polling safety net covers them on the next tick).
    """
    payload = {
        "secondary_id": my_info.secondary_id,
        "hostname": my_info.hostname,
        "port": my_info.port,
        "public_key": my_info.public_key,
    }
    sent = 0
    for peer in peers:
        if peer.secondary_id == my_info.secondary_id:
            continue  # self
        if push_to_peer(peer, "announce", payload, our_pubkey, timeout=timeout):
            sent += 1
    return sent


def fan_out_withdraw(
    peers: list[PeerInfo],
    secondary_id: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> int:
    """Push a ``withdraw`` event to every peer in *peers*.

    Returns the count of successful pushes.
    """
    payload = {"secondary_id": secondary_id}
    sent = 0
    for peer in peers:
        if peer.secondary_id == secondary_id:
            continue  # self
        if push_to_peer(peer, "withdraw", payload, our_pubkey, timeout=timeout):
            sent += 1
    return sent

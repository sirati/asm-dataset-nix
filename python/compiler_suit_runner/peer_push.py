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
    POST /peer/path-have
        body: ``{secondary_id, outpath, drv_path, item_class}``
        Issued when a secondary has just realised a new store path
        (toolchain/common-dep/variant) and wants peers to learn it
        before the next placement-watcher tick.
    POST /peer/path-gone
        body: ``{secondary_id, outpath}``
        Per-path retraction; rarely needed (withdraw_self deletes the
        whole _paths_<sid>.jsonl file in one shot).

    POST /peer/path-offer
        body: ``{from_secondary_id, outpath, drv_path, item_class}``
        K=3 replication handshake — sender offers to push *outpath* to
        the recipient. Recipient replies via /peer/path-accept or
        /peer/path-reject; sender's 1 s timer treats no reply as
        rejection.
    POST /peer/path-accept
        body: ``{from_secondary_id, outpath}``
        Recipient confirms the offer; will fetch *outpath* from
        from_secondary_id next.
    POST /peer/path-reject
        body: ``{from_secondary_id, outpath, reason}``
        Recipient declines; reason in {already-have, already-targeted,
        disk-full, ...}.
    POST /peer/path-cancel
        body: ``{from_secondary_id, outpath}``
        Sender aborts an outstanding offer (e.g. K satisfied by another
        path or the placement converged elsewhere).

    POST /peer/path-broadcast-offer
        body: ``{path, size, origin_peer_id, broadcast_id, hop_count}``
        Broadcast (all-peers) replication offer used for toolchain
        seeding. Recipient that has not seen this ``broadcast_id``
        before fetches *path* and re-fans-out to every other peer
        (minus the sender, minus self); on a duplicate broadcast_id
        the recipient acks with ``{"dedup": true}`` and does nothing
        else, keeping the fan-out gossip from looping.

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
    "fan_out_broadcast_drv",
    "fan_out_path_gone",
    "fan_out_path_have",
    "fan_out_withdraw",
    "push_path_accept",
    "push_path_broadcast_offer",
    "push_path_cancel",
    "push_path_offer",
    "push_path_reject",
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
    on_path_have: Callable[[dict], None] = staticmethod(lambda rec: None)
    on_path_gone: Callable[[dict], None] = staticmethod(lambda rec: None)
    on_path_offer: Callable[[dict], None] = staticmethod(lambda rec: None)
    on_path_accept: Callable[[dict], None] = staticmethod(lambda rec: None)
    on_path_reject: Callable[[dict], None] = staticmethod(lambda rec: None)
    on_path_cancel: Callable[[dict], None] = staticmethod(lambda rec: None)

    # Broadcast offer dedup. ``seen_broadcast_ids`` is a server-scoped
    # set guarded by ``broadcast_lock``; both are rebound by
    # :class:`PeerPushServer` per-instance so two servers in the same
    # process (test code) do not share state. Default to a no-op
    # callback that rejects (so an un-wired server cannot accidentally
    # forward).
    on_broadcast_offer: Callable[..., bool] = staticmethod(
        lambda path, size, origin_peer_id, broadcast_id, hop_count: False,
    )
    # Post-accept hook. Called with the broadcast path AFTER
    # ``on_broadcast_offer`` returns True (the consumer has fetched the
    # path into the local store). Default is a no-op so an un-wired
    # server stays silent on the placement-map.
    record_broadcast_self_has: Callable[[str], None] = staticmethod(
        lambda _path: None,
    )
    seen_broadcast_ids: set[str] = set()
    broadcast_lock: threading.Lock = threading.Lock()

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

    def _respond_json(self, status: int, body: dict) -> None:
        """Reply with a JSON body. Used by path-broadcast-offer where
        the originator needs to learn whether the recipient deduped."""
        try:
            payload = json.dumps(body).encode("utf-8")
        except (TypeError, ValueError):
            payload = b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
            elif self.path == "/peer/path-have":
                record = {
                    "secondary_id": str(data["secondary_id"]),
                    "outpath": str(data["outpath"]),
                    "drv_path": str(data.get("drv_path", "")),
                    "item_class": str(data.get("item_class", "")),
                }
                self.on_path_have(record)
            elif self.path == "/peer/path-gone":
                record = {
                    "secondary_id": str(data["secondary_id"]),
                    "outpath": str(data["outpath"]),
                }
                self.on_path_gone(record)
            elif self.path == "/peer/path-offer":
                record = {
                    "from_secondary_id": str(data["from_secondary_id"]),
                    "outpath": str(data["outpath"]),
                    "drv_path": str(data.get("drv_path", "")),
                    "item_class": str(data.get("item_class", "")),
                }
                self.on_path_offer(record)
            elif self.path == "/peer/path-accept":
                record = {
                    "from_secondary_id": str(data["from_secondary_id"]),
                    "outpath": str(data["outpath"]),
                }
                self.on_path_accept(record)
            elif self.path == "/peer/path-reject":
                record = {
                    "from_secondary_id": str(data["from_secondary_id"]),
                    "outpath": str(data["outpath"]),
                    "reason": str(data.get("reason", "")),
                }
                self.on_path_reject(record)
            elif self.path == "/peer/path-cancel":
                record = {
                    "from_secondary_id": str(data["from_secondary_id"]),
                    "outpath": str(data["outpath"]),
                }
                self.on_path_cancel(record)
            elif self.path == "/peer/path-broadcast-offer":
                path = str(data["path"])
                size = int(data["size"])
                origin_peer_id = str(data["origin_peer_id"])
                broadcast_id = str(data["broadcast_id"])
                hop_count = int(data["hop_count"])
                # Dedup: same broadcast_id seen twice → ack-only, do
                # not invoke the callback. The lock guards both the
                # membership test and the insert so two simultaneous
                # arrivals of the same id collapse to a single fetch.
                with self.broadcast_lock:
                    if broadcast_id in self.seen_broadcast_ids:
                        self._respond_json(200, {"dedup": True})
                        return
                    self.seen_broadcast_ids.add(broadcast_id)
                try:
                    accepted = bool(self.on_broadcast_offer(
                        path, size, origin_peer_id,
                        broadcast_id, hop_count,
                    ))
                except Exception:  # noqa: BLE001 - server stays up
                    logger.exception(
                        "PeerPushServer: on_broadcast_offer raised",
                    )
                    self._respond(500)
                    return
                # Placement-map gossip: only record self as a holder
                # when the consumer accepted (the path is now in our
                # local store). A rejected offer means no fetch
                # happened, so no placement record. Exceptions are
                # swallowed so the gossip layer cannot corrupt the
                # handshake response to the originator.
                if accepted:
                    try:
                        self.record_broadcast_self_has(path)
                    except Exception:  # noqa: BLE001 - best-effort
                        logger.exception(
                            "PeerPushServer: record_broadcast_self_has "
                            "raised for %s", path,
                        )
                self._respond_json(
                    200, {"dedup": False, "accepted": accepted},
                )
                return
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
        on_path_have: Optional[Callable[[dict], None]] = None,
        on_path_gone: Optional[Callable[[dict], None]] = None,
        on_path_offer: Optional[Callable[[dict], None]] = None,
        on_path_accept: Optional[Callable[[dict], None]] = None,
        on_path_reject: Optional[Callable[[dict], None]] = None,
        on_path_cancel: Optional[Callable[[dict], None]] = None,
        on_broadcast_offer: Optional[
            Callable[[str, int, str, str, int], bool]
        ] = None,
        record_broadcast_self_has: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(name="PeerPushServer", daemon=True)
        self._bind_host = str(bind_host)
        self._port = int(port)
        # Build a per-instance handler subclass so callbacks + pubkey
        # are bound to THIS server (handlers are instantiated by the
        # HTTPServer with no extra args).
        bound_announce = on_announce
        bound_withdraw = on_withdraw
        bound_path_have = on_path_have or (lambda _rec: None)
        bound_path_gone = on_path_gone or (lambda _rec: None)
        bound_path_offer = on_path_offer or (lambda _rec: None)
        bound_path_accept = on_path_accept or (lambda _rec: None)
        bound_path_reject = on_path_reject or (lambda _rec: None)
        bound_path_cancel = on_path_cancel or (lambda _rec: None)
        # Default broadcast callback rejects (so an un-wired server
        # cannot accidentally fan-out). Real consumers wire this to
        # BroadcastSender / Receiver.
        bound_broadcast = on_broadcast_offer or (
            lambda _p, _sz, _opid, _bid, _hop: False
        )
        # Default record-self-has hook is a no-op; the suit_task
        # lifecycle wires it to ``peer_paths.record_self_has`` with
        # ``item_class="matrix_eval_drv"`` so placement-map gossip
        # captures broadcast holders.
        bound_record_broadcast = record_broadcast_self_has or (
            lambda _p: None
        )
        bound_pubkey = str(expected_pubkey)
        # Per-instance dedup state — never shared between servers.
        bound_seen: set[str] = set()
        bound_broadcast_lock = threading.Lock()

        class _BoundHandler(_PushHandler):
            expected_pubkey = bound_pubkey
            on_announce = staticmethod(bound_announce)
            on_withdraw = staticmethod(bound_withdraw)
            on_path_have = staticmethod(bound_path_have)
            on_path_gone = staticmethod(bound_path_gone)
            on_path_offer = staticmethod(bound_path_offer)
            on_path_accept = staticmethod(bound_path_accept)
            on_path_reject = staticmethod(bound_path_reject)
            on_path_cancel = staticmethod(bound_path_cancel)
            on_broadcast_offer = staticmethod(bound_broadcast)
            record_broadcast_self_has = staticmethod(bound_record_broadcast)
            seen_broadcast_ids = bound_seen
            broadcast_lock = bound_broadcast_lock

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
    if event not in (
        "announce",
        "withdraw",
        "path-have",
        "path-gone",
        "path-offer",
        "path-accept",
        "path-reject",
        "path-cancel",
    ):
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


def fan_out_path_have(
    peers: list[PeerInfo],
    my_secondary_id: str,
    outpath: str,
    drv_path: str,
    item_class: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> int:
    """Push a ``path-have`` event to every peer in *peers*.

    Best-effort, mirrors :func:`fan_out_announce`. The path-placement
    watcher on each peer will wake on receipt and re-read the
    placement gossip files; the polling tick is the safety net.
    """
    payload = {
        "secondary_id": my_secondary_id,
        "outpath": outpath,
        "drv_path": drv_path,
        "item_class": item_class,
    }
    sent = 0
    for peer in peers:
        if peer.secondary_id == my_secondary_id:
            continue  # self
        if push_to_peer(peer, "path-have", payload, our_pubkey, timeout=timeout):
            sent += 1
    return sent


def fan_out_path_gone(
    peers: list[PeerInfo],
    my_secondary_id: str,
    outpath: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> int:
    """Push a ``path-gone`` event to every peer in *peers*.

    Rare; used when a placement record needs explicit retraction (a
    GC pass removed the path before withdraw, say). Day-to-day,
    withdraw_self deletes the whole _paths_<sid>.jsonl file in one
    shot and per-record retraction is unnecessary.
    """
    payload = {
        "secondary_id": my_secondary_id,
        "outpath": outpath,
    }
    sent = 0
    for peer in peers:
        if peer.secondary_id == my_secondary_id:
            continue  # self
        if push_to_peer(peer, "path-gone", payload, our_pubkey, timeout=timeout):
            sent += 1
    return sent


# ---------------------------------------------------------------------------
# K=3 replication handshake — point-to-point single-peer helpers
# ---------------------------------------------------------------------------
#
# Unlike fan_out_*, these target ONE peer per call. They are the
# building blocks the ReplicationSender / ReplicationReceiver in
# peer_replication.py use to negotiate "I'll push this outpath to you"
# before doing the actual transfer.


def push_path_offer(
    peer: PeerInfo,
    from_secondary_id: str,
    outpath: str,
    drv_path: str,
    item_class: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> bool:
    """Offer *outpath* to *peer*. Returns True if the POST succeeded
    (transport-level), not whether the recipient accepted — that
    arrives asynchronously via a separate path-accept / path-reject
    POST in the opposite direction."""
    payload = {
        "from_secondary_id": from_secondary_id,
        "outpath": outpath,
        "drv_path": drv_path,
        "item_class": item_class,
    }
    return push_to_peer(peer, "path-offer", payload, our_pubkey, timeout=timeout)


def push_path_accept(
    peer: PeerInfo,
    from_secondary_id: str,
    outpath: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> bool:
    """Reply to an offer from *peer*: accept it. ``from_secondary_id``
    is OUR id (the recipient/accepter); the offerer correlates by
    (outpath, from_secondary_id) tuple."""
    payload = {
        "from_secondary_id": from_secondary_id,
        "outpath": outpath,
    }
    return push_to_peer(peer, "path-accept", payload, our_pubkey, timeout=timeout)


def push_path_reject(
    peer: PeerInfo,
    from_secondary_id: str,
    outpath: str,
    reason: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> bool:
    """Reply to an offer from *peer*: reject it with *reason* (one of
    ``already-have``, ``already-targeted``, ``disk-full``, ...).
    ``from_secondary_id`` is OUR id."""
    payload = {
        "from_secondary_id": from_secondary_id,
        "outpath": outpath,
        "reason": reason,
    }
    return push_to_peer(peer, "path-reject", payload, our_pubkey, timeout=timeout)


def push_path_cancel(
    peer: PeerInfo,
    from_secondary_id: str,
    outpath: str,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> bool:
    """Cancel an outstanding offer to *peer*. ``from_secondary_id`` is
    OUR id (the offerer). Used when K converges before the recipient
    has fetched, so a still-pending offer should not result in a
    redundant fetch."""
    payload = {
        "from_secondary_id": from_secondary_id,
        "outpath": outpath,
    }
    return push_to_peer(peer, "path-cancel", payload, our_pubkey, timeout=timeout)


# ---------------------------------------------------------------------------
# Broadcast replication offer — URL-targeted helpers
# ---------------------------------------------------------------------------
#
# Broadcast offers fan out to ALL OTHER peers (minus sender, minus
# self) and are gossip-deduped by ``broadcast_id`` at the receiver.
# Unlike the K=3 handshake helpers above, the broadcast helpers
# accept a full target URL (not a :class:`PeerInfo`) — the caller
# typically constructs the URL list from the SubmitterPeer view of
# the cluster (which may include the submitter itself or other
# non-PeerInfo participants).


def push_path_broadcast_offer(
    target_url: str,
    path: str,
    size: int,
    origin_peer_id: str,
    broadcast_id: str,
    hop_count: int,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> Optional[dict]:
    """POST a ``path-broadcast-offer`` to a single *target_url*.

    *target_url* is the bare peer push base (e.g.
    ``http://node3:6000``); ``/peer/path-broadcast-offer`` is
    appended. Returns the recipient's parsed JSON response dict on
    success (typically ``{"dedup": false, "accepted": true}`` or
    ``{"dedup": true}``), or ``None`` on any transport-level failure
    (network error, non-2xx, malformed response).
    """
    if not target_url:
        return None
    url = str(target_url).rstrip("/") + "/peer/path-broadcast-offer"
    payload = {
        "path": str(path),
        "size": int(size),
        "origin_peer_id": str(origin_peer_id),
        "broadcast_id": str(broadcast_id),
        "hop_count": int(hop_count),
    }
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        logger.exception(
            "push_path_broadcast_offer: payload not JSON-serializable",
        )
        return None
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
            if not 200 <= int(status) < 300:
                return None
            try:
                raw = resp.read()
            except Exception:  # noqa: BLE001
                return None
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None
    except urllib.error.HTTPError as exc:
        logger.debug(
            "push_path_broadcast_offer %s -> HTTP %d", url, exc.code,
        )
        return None
    except (urllib.error.URLError, TimeoutError, OSError, _socket.timeout) as exc:
        logger.debug("push_path_broadcast_offer %s -> %s", url, exc)
        return None


def fan_out_broadcast_drv(
    peer_urls: list[str],
    path: str,
    size: int,
    broadcast_id: str,
    origin_peer_id: str,
    hop_count: int,
    our_pubkey: str,
    timeout: float = DEFAULT_PUSH_TIMEOUT,
) -> tuple[int, int, list[str]]:
    """Parallel POST a broadcast offer to every URL in *peer_urls*.

    Spawns one thread per target (mirrors the per-recipient threading
    style used by :class:`peer_replication.ReplicationReceiver`),
    waits for all to complete, and returns
    ``(success_count, fail_count, failed_peer_urls)``. A success is
    any non-``None`` response from
    :func:`push_path_broadcast_offer` — including a ``{"dedup": True}``
    ack, since the recipient successfully refused the duplicate.
    """
    if not peer_urls:
        return 0, 0, []

    results: list[tuple[str, Optional[dict]]] = []
    results_lock = threading.Lock()

    def _one(url: str) -> None:
        resp = push_path_broadcast_offer(
            target_url=url,
            path=path,
            size=size,
            origin_peer_id=origin_peer_id,
            broadcast_id=broadcast_id,
            hop_count=hop_count,
            our_pubkey=our_pubkey,
            timeout=timeout,
        )
        with results_lock:
            results.append((url, resp))

    threads: list[threading.Thread] = []
    for url in peer_urls:
        t = threading.Thread(
            target=_one,
            args=(url,),
            name=f"broadcast-offer-{url[-12:]}",
            daemon=True,
        )
        t.start()
        threads.append(t)
    # Wait for each with a slack-bounded join (each fan-out target
    # already enforces its own ``timeout``; +0.5 s tolerates thread
    # scheduling jitter so we don't return before workers finish).
    for t in threads:
        t.join(timeout=float(timeout) + 0.5)

    success = 0
    failed_urls: list[str] = []
    for url, resp in results:
        if resp is None:
            failed_urls.append(url)
        else:
            success += 1
    # Threads that didn't manage to record a result in time count as
    # failures (their URL is in peer_urls but not in results).
    recorded = {u for u, _ in results}
    for url in peer_urls:
        if url not in recorded:
            failed_urls.append(url)
    fail = len(failed_urls)
    return success, fail, failed_urls

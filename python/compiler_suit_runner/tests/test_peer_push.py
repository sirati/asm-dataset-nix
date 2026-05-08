"""Unit tests for :mod:`compiler_suit_runner.peer_push`.

The push server is exercised via real loopback HTTP requests (cheap,
self-contained, and the actual integration surface). Push-client
helpers are tested with monkeypatched ``urllib.request.urlopen`` so
the test suite stays hermetic when no listener is reachable.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from io import BytesIO

import pytest

from compiler_suit_runner.peer_cache import PeerInfo
from compiler_suit_runner.peer_push import (
    CLUSTER_PUBKEY_HEADER,
    PUSH_PORT_OFFSET,
    PeerPushServer,
    fan_out_announce,
    fan_out_withdraw,
    push_port_for,
    push_to_peer,
)


# ---------------------------------------------------------------------------
# push_port_for
# ---------------------------------------------------------------------------


def test_push_port_offset_matches_constant() -> None:
    assert push_port_for(5000) == 5000 + PUSH_PORT_OFFSET
    assert push_port_for(7321) == 7321 + PUSH_PORT_OFFSET


# ---------------------------------------------------------------------------
# Server end-to-end (loopback)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind-then-close on port 0 to discover an unused port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(
    port: int,
    path: str,
    body: object,
    pubkey: str,
    *,
    timeout: float = 2.0,
) -> int:
    """POST JSON to the local push listener; return the HTTP status."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            CLUSTER_PUBKEY_HEADER: pubkey,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _post_raw(
    port: int, path: str, raw: bytes, headers: dict[str, str],
    *, timeout: float = 2.0,
) -> int:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url, data=raw, method="POST", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


@pytest.fixture
def server_factory():
    """Yield (factory, list_of_started_servers); auto-stop on teardown."""
    started: list[PeerPushServer] = []

    def make(
        announces: list[PeerInfo] | None = None,
        withdraws: list[str] | None = None,
        pubkey: str = "test-pubkey:AAAAAAAA",
    ) -> tuple[PeerPushServer, int, list[PeerInfo], list[str]]:
        a = announces if announces is not None else []
        w = withdraws if withdraws is not None else []
        port = _free_port()
        srv = PeerPushServer(
            bind_host="127.0.0.1",
            port=port,
            expected_pubkey=pubkey,
            on_announce=a.append,
            on_withdraw=w.append,
        )
        srv.start()
        started.append(srv)
        return srv, port, a, w

    yield make

    for srv in started:
        try:
            srv.stop()
        except Exception:  # noqa: BLE001 - best-effort
            pass


def test_announce_round_trip(server_factory) -> None:
    srv, port, announces, _ = server_factory()
    payload = {
        "secondary_id": "sec-7",
        "hostname": "node-7.example",
        "port": 5007,
        "public_key": "k:Z" * 8,
    }
    rc = _post(port, "/peer/announce", payload, "test-pubkey:AAAAAAAA")
    assert rc == 204
    assert len(announces) == 1
    assert announces[0].secondary_id == "sec-7"
    assert announces[0].port == 5007


def test_withdraw_round_trip(server_factory) -> None:
    srv, port, _, withdraws = server_factory()
    rc = _post(
        port, "/peer/withdraw",
        {"secondary_id": "sec-9"},
        "test-pubkey:AAAAAAAA",
    )
    assert rc == 204
    assert withdraws == ["sec-9"]


def test_auth_rejected_with_wrong_pubkey(server_factory) -> None:
    srv, port, announces, _ = server_factory(pubkey="server-key:XYZ")
    payload = {
        "secondary_id": "x", "hostname": "x", "port": 1, "public_key": "k:1",
    }
    rc = _post(port, "/peer/announce", payload, "client-key:WRONG")
    assert rc == 403
    assert announces == []


def test_auth_rejected_with_missing_header(server_factory) -> None:
    srv, port, announces, _ = server_factory()
    rc = _post_raw(
        port, "/peer/announce",
        json.dumps({
            "secondary_id": "x", "hostname": "x",
            "port": 1, "public_key": "k:1",
        }).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    assert rc == 403
    assert announces == []


def test_unknown_path_returns_404(server_factory) -> None:
    srv, port, _, _ = server_factory()
    rc = _post(port, "/peer/wat", {}, "test-pubkey:AAAAAAAA")
    assert rc == 404


def test_malformed_json_returns_400(server_factory) -> None:
    srv, port, announces, _ = server_factory()
    rc = _post_raw(
        port, "/peer/announce",
        b"this-is-not-json",
        {
            "Content-Type": "application/json",
            CLUSTER_PUBKEY_HEADER: "test-pubkey:AAAAAAAA",
        },
    )
    assert rc == 400
    assert announces == []


def test_missing_field_returns_400(server_factory) -> None:
    srv, port, announces, _ = server_factory()
    rc = _post(
        port, "/peer/announce",
        {"secondary_id": "x"},  # missing hostname, port, public_key
        "test-pubkey:AAAAAAAA",
    )
    assert rc == 400
    assert announces == []


def test_callback_exception_returns_500(server_factory) -> None:
    """A buggy callback must not take the server down."""

    def boom(_info: PeerInfo) -> None:
        raise RuntimeError("kaboom")

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pubkey:AAAAAAAA",
        on_announce=boom,
        on_withdraw=lambda _sid: None,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/announce",
            {
                "secondary_id": "x", "hostname": "x", "port": 1,
                "public_key": "k:1",
            },
            "test-pubkey:AAAAAAAA",
        )
        assert rc == 500
        # second request still served (server alive)
        rc2 = _post(
            port, "/peer/withdraw",
            {"secondary_id": "y"},
            "test-pubkey:AAAAAAAA",
        )
        assert rc2 == 204
    finally:
        srv.stop()


def test_stop_is_idempotent(server_factory) -> None:
    srv, port, _, _ = server_factory()
    srv.stop()
    srv.stop()  # no exception


# ---------------------------------------------------------------------------
# push_to_peer (mocked transport)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _peer(i: int, port: int = 5000) -> PeerInfo:
    return PeerInfo(
        secondary_id=f"sec{i}",
        hostname=f"node{i}",
        port=port + i,
        public_key=f"k{i}:Z",
    )


def test_push_to_peer_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001 - signature compat
        captured.append(req)
        return _FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    peer = _peer(1)
    ok = push_to_peer(
        peer, "announce",
        {"secondary_id": "me", "hostname": "h", "port": 1, "public_key": "k:1"},
        "my-key:Z",
    )
    assert ok is True
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.full_url == f"http://node1:{push_port_for(5001)}/peer/announce"
    assert req.headers.get("X-cluster-pubkey") == "my-key:Z"


def test_push_to_peer_returns_false_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            "http://x", 403, "Forbidden", {}, BytesIO(b""),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok = push_to_peer(
        _peer(2), "announce",
        {"secondary_id": "x", "hostname": "x", "port": 1, "public_key": "k"},
        "pk",
    )
    assert ok is False


def test_push_to_peer_returns_false_on_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    ok = push_to_peer(
        _peer(3), "withdraw",
        {"secondary_id": "x"}, "pk",
    )
    assert ok is False


def test_push_to_peer_rejects_unknown_event() -> None:
    with pytest.raises(ValueError):
        push_to_peer(_peer(1), "wat", {}, "pk")


def test_push_to_peer_handles_empty_hostname() -> None:
    p = PeerInfo(
        secondary_id="x", hostname="", port=5001, public_key="k:1",
    )
    assert push_to_peer(p, "announce", {}, "pk") is False


# ---------------------------------------------------------------------------
# fan_out_*
# ---------------------------------------------------------------------------


def test_fan_out_announce_skips_self_and_counts_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets: list[str] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        targets.append(req.full_url)
        # peer 2 is "down": simulate via URLError on its hostname.
        if "node2" in req.full_url:
            raise urllib.error.URLError("down")
        return _FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    me = PeerInfo(
        secondary_id="me", hostname="me-host", port=5009,
        public_key="me:Z",
    )
    peers = [_peer(1), _peer(2), _peer(3), me]
    sent = fan_out_announce(peers, me, "me:Z")
    assert sent == 2  # peer1 + peer3; peer2 down; me skipped
    assert all("/peer/announce" in t for t in targets)
    # self skipped: no request to me-host
    assert not any("me-host" in t for t in targets)


def test_fan_out_withdraw_skips_self_and_counts_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        return _FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    peers = [_peer(1), _peer(2), _peer(3)]
    # ``secondary_id="sec2"`` happens to match peer2 — expect that one
    # to be skipped as self.
    sent = fan_out_withdraw(peers, "sec2", "pk")
    assert sent == 2


# ---------------------------------------------------------------------------
# Watcher integration: request_refresh wakes the run loop early
# ---------------------------------------------------------------------------


def test_request_refresh_wakes_watcher(
    tmp_path,
) -> None:
    """End-to-end: a request_refresh between ticks must trigger an
    out-of-band ``_refresh`` so the watcher sees a peer-file added
    after its last poll."""
    from compiler_suit_runner.peer_cache import (
        PeerListWatcher,
        announce_self,
    )

    shared = tmp_path / "shared"
    shared.mkdir()

    # Long tick: if the wake event doesn't fire, the test would hang
    # (caught by pytest's default timeout).
    watcher = PeerListWatcher(
        shared_fs=shared,
        exclude_id="me",
        tick_seconds=30.0,
    )
    watcher.start()
    try:
        # Initially no peers.
        assert watcher.peers == []

        # Drop a peer file then nudge the watcher.
        announce_self(
            shared,
            PeerInfo(
                secondary_id="other",
                hostname="other-host",
                port=5000,
                public_key="k:Z" * 8,
            ),
        )
        watcher.request_refresh()

        # Poll briefly for the watcher to pick up the new peer. With
        # request_refresh wired correctly the latency is a few ms.
        deadline = threading.Event()
        timer = threading.Timer(2.0, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while not deadline.is_set():
                if watcher.peers:
                    break
                threading.Event().wait(0.02)
            assert watcher.peers, (
                "request_refresh failed to wake the watcher within 2 s"
            )
            assert watcher.peers[0].secondary_id == "other"
        finally:
            timer.cancel()
    finally:
        watcher.stop()
        watcher.join(timeout=3.0)

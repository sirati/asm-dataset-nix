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
    fan_out_broadcast_drv,
    fan_out_path_gone,
    fan_out_path_have,
    fan_out_withdraw,
    push_path_accept,
    push_path_broadcast_offer,
    push_path_cancel,
    push_path_offer,
    push_path_reject,
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
# path-have / path-gone server + fan-out
# ---------------------------------------------------------------------------


def test_path_have_round_trip() -> None:
    """A ``POST /peer/path-have`` invokes the registered callback with
    the parsed record (used by :class:`PathPlacementWatcher` to wake
    its refresh event)."""
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_have=received.append,
    )
    srv.start()
    try:
        payload = {
            "secondary_id": "sec-3",
            "outpath": "/nix/store/aaa-toolchain",
            "drv_path": "/nix/store/bbb-toolchain.drv",
            "item_class": "toolchain",
        }
        rc = _post(port, "/peer/path-have", payload, "test-pk")
        assert rc == 204
        assert len(received) == 1
        assert received[0]["outpath"] == "/nix/store/aaa-toolchain"
        assert received[0]["secondary_id"] == "sec-3"
    finally:
        srv.stop()


def test_path_gone_round_trip() -> None:
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_gone=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-gone",
            {"secondary_id": "sec-9", "outpath": "/nix/store/x"},
            "test-pk",
        )
        assert rc == 204
        assert received == [
            {"secondary_id": "sec-9", "outpath": "/nix/store/x"}
        ]
    finally:
        srv.stop()


def _ppeer(i: int) -> PeerInfo:
    return PeerInfo(
        secondary_id=f"sec{i}",
        hostname=f"node{i}",
        port=5000 + i,
        public_key=f"k{i}:Z",
    )


def test_fan_out_path_have_skips_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan-out helper must skip the sending secondary's own id —
    otherwise we self-broadcast on every record."""

    seen_payloads: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001 - signature compat
        body = req.data.decode("utf-8") if req.data else "{}"
        seen_payloads.append((req.full_url, json.loads(body)))

        class _R:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def getcode(self):
                return 204

        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    peers = [_ppeer(1), _ppeer(2), _ppeer(3)]
    sent = fan_out_path_have(
        peers,
        my_secondary_id="sec2",
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
        our_pubkey="me:Z",
    )
    assert sent == 2
    targets = [u for u, _ in seen_payloads]
    assert all("/peer/path-have" in t for t in targets)
    # sec2 is self → not contacted.
    assert not any("node2" in t for t in targets)
    # Payloads carry the full record.
    for _u, body in seen_payloads:
        assert body["outpath"] == "/nix/store/aaa"
        assert body["drv_path"] == "/nix/store/aaa.drv"
        assert body["item_class"] == "toolchain"


def test_fan_out_path_gone_skips_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        captured.append(req.full_url)

        class _R:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def getcode(self):
                return 204

        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    peers = [_ppeer(1), _ppeer(2), _ppeer(3)]
    sent = fan_out_path_gone(
        peers, my_secondary_id="sec1",
        outpath="/nix/store/aaa",
        our_pubkey="me:Z",
    )
    assert sent == 2
    assert all("/peer/path-gone" in t for t in captured)
    assert not any("node1" in t for t in captured)


def test_push_to_peer_accepts_path_have_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire dispatcher must recognise ``path-have`` (else
    fan_out_path_have raises before any push happens)."""

    def fake_urlopen(_req, timeout):  # noqa: ARG001
        class _R:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def getcode(self):
                return 204

        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert push_to_peer(_ppeer(1), "path-have", {"x": 1}, "pk") is True
    assert push_to_peer(_ppeer(1), "path-gone", {"x": 1}, "pk") is True


# ---------------------------------------------------------------------------
# K=3 replication handshake — server + originator helpers
# ---------------------------------------------------------------------------


def test_push_to_peer_accepts_handshake_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wire dispatcher must recognise all four handshake events."""

    def fake_urlopen(_req, timeout):  # noqa: ARG001
        class _R:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def getcode(self):
                return 204

        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for event in ("path-offer", "path-accept", "path-reject", "path-cancel"):
        assert push_to_peer(_ppeer(1), event, {"x": 1}, "pk") is True


def test_path_offer_round_trip() -> None:
    """POST /peer/path-offer routes to on_path_offer with the parsed
    record (from_secondary_id + outpath + drv_path + item_class)."""
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_offer=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-offer",
            {
                "from_secondary_id": "sec-7",
                "outpath": "/nix/store/aaa-toolchain",
                "drv_path": "/nix/store/bbb-toolchain.drv",
                "item_class": "toolchain",
            },
            "test-pk",
        )
        assert rc == 204
        assert len(received) == 1
        assert received[0]["from_secondary_id"] == "sec-7"
        assert received[0]["outpath"] == "/nix/store/aaa-toolchain"
        assert received[0]["item_class"] == "toolchain"
    finally:
        srv.stop()


def test_path_accept_round_trip() -> None:
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_accept=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-accept",
            {"from_secondary_id": "sec-2", "outpath": "/nix/store/x"},
            "test-pk",
        )
        assert rc == 204
        assert received == [
            {"from_secondary_id": "sec-2", "outpath": "/nix/store/x"}
        ]
    finally:
        srv.stop()


def test_path_reject_round_trip_carries_reason() -> None:
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_reject=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-reject",
            {
                "from_secondary_id": "sec-3",
                "outpath": "/nix/store/y",
                "reason": "already-targeted",
            },
            "test-pk",
        )
        assert rc == 204
        assert len(received) == 1
        assert received[0]["reason"] == "already-targeted"
    finally:
        srv.stop()


def test_path_cancel_round_trip() -> None:
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_cancel=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-cancel",
            {"from_secondary_id": "sec-4", "outpath": "/nix/store/z"},
            "test-pk",
        )
        assert rc == 204
        assert received == [
            {"from_secondary_id": "sec-4", "outpath": "/nix/store/z"}
        ]
    finally:
        srv.stop()


def test_handshake_auth_rejected_with_wrong_pubkey() -> None:
    """Each new handshake endpoint must enforce the same X-Cluster-PubKey
    check as announce/withdraw."""
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="server-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_offer=received.append,
        on_path_accept=received.append,
        on_path_reject=received.append,
        on_path_cancel=received.append,
    )
    srv.start()
    try:
        for path in (
            "/peer/path-offer",
            "/peer/path-accept",
            "/peer/path-reject",
            "/peer/path-cancel",
        ):
            rc = _post(
                port, path,
                {"from_secondary_id": "x", "outpath": "/nix/store/x"},
                "wrong-pk",
            )
            assert rc == 403
        assert received == []
    finally:
        srv.stop()


def test_path_offer_missing_field_returns_400() -> None:
    received: list[dict] = []
    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_path_offer=received.append,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-offer",
            {"from_secondary_id": "x"},  # missing outpath
            "test-pk",
        )
        assert rc == 400
        assert received == []
    finally:
        srv.stop()


def test_push_path_offer_sends_correct_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        body = req.data.decode("utf-8") if req.data else "{}"
        captured.append((req.full_url, json.loads(body)))
        return _FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = push_path_offer(
        _ppeer(1),
        from_secondary_id="me",
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
        our_pubkey="me:Z",
    )
    assert ok is True
    assert len(captured) == 1
    url, body = captured[0]
    assert "/peer/path-offer" in url
    assert body == {
        "from_secondary_id": "me",
        "outpath": "/nix/store/aaa",
        "drv_path": "/nix/store/aaa.drv",
        "item_class": "toolchain",
    }


def test_push_path_accept_reject_cancel_payload_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict]] = []

    def fake_urlopen(req, timeout):  # noqa: ARG001
        body = req.data.decode("utf-8") if req.data else "{}"
        captured.append((req.full_url, json.loads(body)))
        return _FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert push_path_accept(
        _ppeer(1), "me", "/nix/store/x", "me:Z",
    ) is True
    assert push_path_reject(
        _ppeer(2), "me", "/nix/store/y", "already-have", "me:Z",
    ) is True
    assert push_path_cancel(
        _ppeer(3), "me", "/nix/store/z", "me:Z",
    ) is True

    assert len(captured) == 3
    accept_body = captured[0][1]
    reject_body = captured[1][1]
    cancel_body = captured[2][1]

    assert accept_body == {"from_secondary_id": "me", "outpath": "/nix/store/x"}
    assert reject_body == {
        "from_secondary_id": "me",
        "outpath": "/nix/store/y",
        "reason": "already-have",
    }
    assert cancel_body == {
        "from_secondary_id": "me",
        "outpath": "/nix/store/z",
    }
    assert "/peer/path-accept" in captured[0][0]
    assert "/peer/path-reject" in captured[1][0]
    assert "/peer/path-cancel" in captured[2][0]


def test_push_path_offer_returns_false_on_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = push_path_offer(
        _ppeer(1), "me", "/nix/store/aaa", "/nix/store/aaa.drv",
        "toolchain", "me:Z",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# /peer/path-broadcast-offer — endpoint + originator + fan-out
# ---------------------------------------------------------------------------


def _post_with_body(
    port: int,
    path: str,
    body: object,
    pubkey: str,
    *,
    timeout: float = 2.0,
) -> tuple[int, bytes]:
    """POST JSON; return ``(status, raw_response_body)``. Used for
    broadcast-offer where the dedup flag travels in the response."""
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
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def _broadcast_payload(broadcast_id: str = "bid-1") -> dict:
    return {
        "path": "/nix/store/aaa-toolchain.drv",
        "size": 4096,
        "origin_peer_id": "sec-origin",
        "broadcast_id": broadcast_id,
        "hop_count": 0,
    }


def test_broadcast_offer_route_and_callback_invocation() -> None:
    """POST /peer/path-broadcast-offer reaches on_broadcast_offer with
    the five positional fields and responds with the consumer's bool."""
    calls: list[tuple] = []

    def cb(path, size, origin_peer_id, broadcast_id, hop_count):
        calls.append((path, size, origin_peer_id, broadcast_id, hop_count))
        return True

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        rc, raw = _post_with_body(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload("bid-A"),
            "test-pk",
        )
        assert rc == 200
        assert calls == [
            ("/nix/store/aaa-toolchain.drv", 4096, "sec-origin", "bid-A", 0),
        ]
        body = json.loads(raw.decode("utf-8"))
        assert body == {"dedup": False, "accepted": True}
    finally:
        srv.stop()


def test_broadcast_offer_auth_rejected_with_wrong_pubkey() -> None:
    """X-Cluster-PubKey enforcement parity with the other endpoints."""
    calls: list[tuple] = []

    def cb(*args):
        calls.append(args)
        return True

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="server-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        rc = _post(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload(), "wrong-pk",
        )
        assert rc == 403
        assert calls == []
    finally:
        srv.stop()


def test_broadcast_offer_auth_rejected_with_missing_header() -> None:
    calls: list[tuple] = []

    def cb(*args):
        calls.append(args)
        return True

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="server-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        rc = _post_raw(
            port, "/peer/path-broadcast-offer",
            json.dumps(_broadcast_payload()).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        assert rc == 403
        assert calls == []
    finally:
        srv.stop()


def test_broadcast_offer_dedup_fires_callback_once() -> None:
    """Same ``broadcast_id`` POSTed twice → callback fires exactly
    once; the second POST returns ``{"dedup": True}`` without
    invoking the consumer."""
    calls: list[tuple] = []

    def cb(path, size, origin_peer_id, broadcast_id, hop_count):
        calls.append((path, broadcast_id))
        return True

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        rc1, raw1 = _post_with_body(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload("bid-dedup"),
            "test-pk",
        )
        rc2, raw2 = _post_with_body(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload("bid-dedup"),
            "test-pk",
        )
        assert rc1 == 200
        assert rc2 == 200
        body1 = json.loads(raw1.decode("utf-8"))
        body2 = json.loads(raw2.decode("utf-8"))
        assert body1 == {"dedup": False, "accepted": True}
        assert body2 == {"dedup": True}
        # Distinct broadcast_id slips past the dedup gate.
        rc3, raw3 = _post_with_body(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload("bid-other"),
            "test-pk",
        )
        assert rc3 == 200
        body3 = json.loads(raw3.decode("utf-8"))
        assert body3 == {"dedup": False, "accepted": True}
        assert len(calls) == 2
        assert calls[0][1] == "bid-dedup"
        assert calls[1][1] == "bid-other"
    finally:
        srv.stop()


def test_broadcast_offer_missing_field_returns_400() -> None:
    calls: list[tuple] = []

    def cb(*args):
        calls.append(args)
        return True

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        partial = {"path": "/nix/store/x", "broadcast_id": "b"}
        rc = _post(
            port, "/peer/path-broadcast-offer", partial, "test-pk",
        )
        assert rc == 400
        assert calls == []
    finally:
        srv.stop()


def test_broadcast_offer_callback_rejection_propagates() -> None:
    """If the consumer returns False the response is
    ``{"dedup": False, "accepted": False}`` — the originator decides
    what to do with that signal."""

    def cb(_p, _sz, _opid, _bid, _hop):
        return False

    port = _free_port()
    srv = PeerPushServer(
        bind_host="127.0.0.1",
        port=port,
        expected_pubkey="test-pk",
        on_announce=lambda _i: None,
        on_withdraw=lambda _s: None,
        on_broadcast_offer=cb,
    )
    srv.start()
    try:
        rc, raw = _post_with_body(
            port, "/peer/path-broadcast-offer",
            _broadcast_payload("bid-reject"),
            "test-pk",
        )
        assert rc == 200
        body = json.loads(raw.decode("utf-8"))
        assert body == {"dedup": False, "accepted": False}
    finally:
        srv.stop()


def test_push_path_broadcast_offer_returns_parsed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The originator helper returns the recipient's parsed JSON dict
    (so the caller can read the ``dedup`` flag)."""
    captured: list[tuple[str, dict, dict]] = []

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps({"dedup": False, "accepted": True}).encode("utf-8")

        def getcode(self):
            return 200

    def fake_urlopen(req, timeout):  # noqa: ARG001
        body = json.loads(req.data.decode("utf-8"))
        captured.append((req.full_url, dict(req.headers), body))
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = push_path_broadcast_offer(
        target_url="http://node1:6001",
        path="/nix/store/zzz.drv",
        size=8192,
        origin_peer_id="sec-O",
        broadcast_id="bid-1",
        hop_count=2,
        our_pubkey="me:Z",
    )
    assert resp == {"dedup": False, "accepted": True}
    assert len(captured) == 1
    url, headers, body = captured[0]
    assert url == "http://node1:6001/peer/path-broadcast-offer"
    # Header dict on a urllib Request is title-cased.
    assert headers.get("X-cluster-pubkey") == "me:Z"
    assert body == {
        "path": "/nix/store/zzz.drv",
        "size": 8192,
        "origin_peer_id": "sec-O",
        "broadcast_id": "bid-1",
        "hop_count": 2,
    }


def test_push_path_broadcast_offer_returns_none_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any transport-level failure → ``None``; caller treats it as
    'peer unreachable, retry via gossip safety-net'."""

    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise OSError("socket error: connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = push_path_broadcast_offer(
        target_url="http://node-down:6001",
        path="/nix/store/zzz.drv",
        size=4,
        origin_peer_id="sec-O",
        broadcast_id="bid-fail",
        hop_count=0,
        our_pubkey="me:Z",
    )
    assert resp is None


def test_push_path_broadcast_offer_returns_none_on_url_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise urllib.error.URLError("name not known")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = push_path_broadcast_offer(
        target_url="http://node-bogus:6001",
        path="/nix/store/zzz.drv",
        size=4,
        origin_peer_id="sec-O",
        broadcast_id="bid-fail2",
        hop_count=0,
        our_pubkey="me:Z",
    )
    assert resp is None


def test_push_path_broadcast_offer_returns_none_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(_req, timeout):  # noqa: ARG001
        raise urllib.error.HTTPError(
            "http://x", 500, "boom", {}, BytesIO(b""),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = push_path_broadcast_offer(
        target_url="http://node-err:6001",
        path="/nix/store/zzz.drv",
        size=4,
        origin_peer_id="sec-O",
        broadcast_id="bid-err",
        hop_count=0,
        our_pubkey="me:Z",
    )
    assert resp is None


def test_fan_out_broadcast_drv_parallel_posts_to_all_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every URL receives one POST; success_count == len(peer_urls)
    when all targets reply OK."""

    seen: list[tuple[str, dict]] = []
    seen_lock = threading.Lock()

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps({"dedup": False, "accepted": True}).encode("utf-8")

        def getcode(self):
            return 200

    def fake_urlopen(req, timeout):  # noqa: ARG001
        body = json.loads(req.data.decode("utf-8"))
        with seen_lock:
            seen.append((req.full_url, body))
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    urls = [
        "http://node1:6001",
        "http://node2:6002",
        "http://node3:6003",
        "http://node4:6004",
    ]
    success, fail, failed = fan_out_broadcast_drv(
        peer_urls=urls,
        path="/nix/store/abc.drv",
        size=1024,
        broadcast_id="bid-fan",
        origin_peer_id="sec-origin",
        hop_count=1,
        our_pubkey="me:Z",
    )
    assert success == 4
    assert fail == 0
    assert failed == []
    # One POST per URL, all carrying the same payload.
    posted_urls = sorted(u for u, _ in seen)
    assert posted_urls == sorted(
        f"{u}/peer/path-broadcast-offer" for u in urls
    )
    for _u, body in seen:
        assert body["broadcast_id"] == "bid-fan"
        assert body["hop_count"] == 1
        assert body["origin_peer_id"] == "sec-origin"


def test_fan_out_broadcast_drv_counts_partial_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subset of peers down → counts split, failed_urls lists the
    unreachable ones."""

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps({"dedup": False, "accepted": True}).encode("utf-8")

        def getcode(self):
            return 200

    def fake_urlopen(req, timeout):  # noqa: ARG001
        if "node2" in req.full_url or "node4" in req.full_url:
            raise urllib.error.URLError("down")
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    urls = [
        "http://node1:6001",
        "http://node2:6002",
        "http://node3:6003",
        "http://node4:6004",
    ]
    success, fail, failed = fan_out_broadcast_drv(
        peer_urls=urls,
        path="/nix/store/abc.drv",
        size=4,
        broadcast_id="bid-mixed",
        origin_peer_id="sec-O",
        hop_count=0,
        our_pubkey="me:Z",
    )
    assert success == 2
    assert fail == 2
    assert sorted(failed) == sorted([
        "http://node2:6002",
        "http://node4:6004",
    ])


def test_fan_out_broadcast_drv_empty_peer_list_is_noop() -> None:
    success, fail, failed = fan_out_broadcast_drv(
        peer_urls=[],
        path="/nix/store/abc.drv",
        size=4,
        broadcast_id="bid-empty",
        origin_peer_id="sec-O",
        hop_count=0,
        our_pubkey="me:Z",
    )
    assert success == 0
    assert fail == 0
    assert failed == []


def test_fan_out_broadcast_drv_treats_dedup_ack_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``{"dedup": True}`` response is a successful refusal of the
    duplicate (not a transport failure) — counted in success_count."""

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps({"dedup": True}).encode("utf-8")

        def getcode(self):
            return 200

    def fake_urlopen(_req, timeout):  # noqa: ARG001
        return _R()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    urls = ["http://nodeA:6001", "http://nodeB:6002"]
    success, fail, failed = fan_out_broadcast_drv(
        peer_urls=urls,
        path="/nix/store/abc.drv",
        size=4,
        broadcast_id="bid-dup",
        origin_peer_id="sec-O",
        hop_count=2,
        our_pubkey="me:Z",
    )
    assert success == 2
    assert fail == 0
    assert failed == []


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

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
    fan_out_path_gone,
    fan_out_path_have,
    fan_out_withdraw,
    push_path_accept,
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

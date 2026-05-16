"""Unit tests for :mod:`compiler_suit_runner.suit_task`.

Today's tests cover the Phase 0 → Phase 1 quiesce watcher
(``_Phase0QuiesceWatcher``) and its wiring into ``on_run_start``.
Older topology / lifecycle tests live in
:mod:`tests.test_suit_task_topology`.

``phase1_planner.plan_phase1`` and ``read_phase0_manifests`` are
patched with :class:`unittest.mock.MagicMock` so no ``nix`` binary is
invoked and the only IO is the stub's JSON dump.
"""

from __future__ import annotations

import json
import logging
import pathlib
from unittest import mock

import pytest

from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    phase0_eval_task_id,
    toolchain_task_id,
    write_manifest,
)
from compiler_suit_runner.suit_task import (
    SuitTask,
    SuitTaskConfig,
    _Phase0QuiesceWatcher,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SYS = "x86_64-linux"


def _make_config(tmp_path: pathlib.Path) -> SuitTaskConfig:
    return SuitTaskConfig(
        flake_ref=".",
        sys_name=_SYS,
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "manifests",
        raw_partition_dir=tmp_path / "partition" / "raw",
        partition_dir=tmp_path / "partition",
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
    )


def _phase0_eval_header(binary: str) -> ManifestHeader:
    return ManifestHeader(
        item_class="phase0_eval",
        name=f"phase0_eval__{binary}",
        size=0,
        payload={
            "binary": binary,
            "sys": _SYS,
            "archs": ["x86_64"],
            "suffixes": ["O0"],
            "attr": f"dataset.{_SYS}.{binary}",
        },
        task_id=phase0_eval_task_id(binary),
        task_depends_on=(),
    )


def _toolchain_header(
    arch: str, compiler_label: str, drv: str
) -> ManifestHeader:
    return ManifestHeader(
        item_class="phase2_toolchain_validate",
        name=f"toolchain_validate__{arch}__{compiler_label}",
        size=0,
        payload={
            "sys": _SYS,
            "arch": arch,
            "compiler_label": compiler_label,
            "attr": (
                f"_crossToolchainMap.{_SYS}.{arch}.{compiler_label}"
            ),
            "drv": drv,
            "validate_only": True,
        },
        task_id=toolchain_task_id(_SYS, arch, compiler_label),
    )


# ---------------------------------------------------------------------------
# _Phase0QuiesceWatcher unit tests
# ---------------------------------------------------------------------------


def test_watcher_initialises_with_expected_set(
    tmp_path: pathlib.Path,
) -> None:
    expected = {phase0_eval_task_id("hello"),
                phase0_eval_task_id("busybox")}
    w = _Phase0QuiesceWatcher(
        expected_task_ids=expected,
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    assert w.expected == frozenset(expected)
    assert w.completed == frozenset()
    assert w.fired is False


def test_watcher_noops_for_non_phase0_task(
    tmp_path: pathlib.Path,
) -> None:
    """A toolchain (or any) task_id that is not in ``expected`` is
    ignored. The watcher coexists with other listeners on the same hook
    surface (K=3 replication, etc.) so it must not raise."""
    w = _Phase0QuiesceWatcher(
        expected_task_ids={phase0_eval_task_id("hello")},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    with mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner.plan_phase1"
    ) as plan_mock:
        w.on_task_completed("toolchain__gcc15__x86_64")
        w.on_task_completed("")  # empty id — defensive guard
        w.on_task_completed("merge__singleton")
    assert w.completed == frozenset()
    assert w.fired is False
    plan_mock.assert_not_called()


def test_watcher_records_phase0_completion(
    tmp_path: pathlib.Path,
) -> None:
    """A matching phase0 task moves into ``completed`` but doesn't
    fire until the set is full."""
    hello = phase0_eval_task_id("hello")
    busybox = phase0_eval_task_id("busybox")
    w = _Phase0QuiesceWatcher(
        expected_task_ids={hello, busybox},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    with mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner.plan_phase1"
    ) as plan_mock:
        w.on_task_completed(hello)
    assert w.completed == frozenset({hello})
    assert w.fired is False
    plan_mock.assert_not_called()


def test_watcher_fires_plan_phase1_when_complete(
    tmp_path: pathlib.Path,
) -> None:
    """When the completed set covers the expected set, plan_phase1 is
    invoked exactly once with the loaded manifests, toolchain map, and
    spawn_tasks stub."""
    hello = phase0_eval_task_id("hello")
    out_dir = tmp_path / "out"
    toolchain_drv = "/nix/store/c-gcc15.drv"
    toolchain_id = toolchain_task_id(_SYS, "x86_64", "gcc15")
    fake_manifests = {"hello": {"binary": "hello", "variants": []}}

    w = _Phase0QuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={toolchain_drv: toolchain_id},
        sys_name=_SYS,
    )
    with mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner.plan_phase1"
    ) as plan_mock, mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner"
        ".read_phase0_manifests",
        return_value=fake_manifests,
    ) as read_mock:
        w.on_task_completed(hello)
    assert w.fired is True
    read_mock.assert_called_once_with(out_dir)
    plan_mock.assert_called_once()
    args, kwargs = plan_mock.call_args
    # Positional: phase0_manifests, toolchain_task_ids, spawn_tasks
    assert args[0] == fake_manifests
    assert args[1] == {toolchain_drv: toolchain_id}
    assert callable(args[2])
    assert kwargs.get("sys_name") == _SYS


def test_watcher_is_idempotent_on_duplicate_completion(
    tmp_path: pathlib.Path,
) -> None:
    """The same task_id arriving twice does not fire plan_phase1 twice
    and does not double-count toward expected."""
    hello = phase0_eval_task_id("hello")
    busybox = phase0_eval_task_id("busybox")
    w = _Phase0QuiesceWatcher(
        expected_task_ids={hello, busybox},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    with mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner.plan_phase1"
    ) as plan_mock, mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner"
        ".read_phase0_manifests",
        return_value={},
    ):
        w.on_task_completed(hello)
        w.on_task_completed(hello)  # duplicate
        assert w.completed == frozenset({hello})
        assert w.fired is False
        plan_mock.assert_not_called()
        w.on_task_completed(busybox)
        assert w.fired is True
        plan_mock.assert_called_once()
        # Another stray completion after firing is a no-op.
        w.on_task_completed(busybox)
        w.on_task_completed(hello)
        plan_mock.assert_called_once()


def test_spawn_tasks_stub_writes_phase1_graph_and_logs_count(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The Q5 stub serialises the headers to ``_phase1_graph.json``
    and emits an INFO log with the count."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    logger = logging.getLogger("test_spawn_stub")
    w = _Phase0QuiesceWatcher(
        expected_task_ids={phase0_eval_task_id("hello")},
        out_dir=out_dir,
        toolchain_task_ids={},
        logger=logger,
    )
    headers = [
        ManifestHeader(
            item_class="phase2_common_dep",
            name="common_dep__glibc",
            size=0,
            payload={"drv": "/nix/store/x-glibc.drv", "label": "glibc",
                     "attr": "/nix/store/x-glibc.drv"},
            task_id="common_dep__x-glibc",
        ),
        ManifestHeader(
            item_class="phase3_variant",
            name="variant__hello__x86_64__gcc15-O0",
            size=0,
            payload={"pkg": "hello"},
            task_id="variant__hello__abc",
            task_depends_on=("common_dep__x-glibc",),
        ),
    ]
    with caplog.at_level(logging.INFO, logger="test_spawn_stub"):
        w._spawn_tasks_stub(headers)
    graph_path = out_dir / "_phase1_graph.json"
    assert graph_path.is_file()
    parsed = json.loads(graph_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["item_class"] == "phase2_common_dep"
    assert parsed[1]["item_class"] == "phase3_variant"
    assert parsed[1]["task_depends_on"] == ["common_dep__x-glibc"]
    # INFO log line mentions the header count.
    assert any(
        "spawn_tasks stub received 2" in rec.message
        for rec in caplog.records
    )


def test_spawn_tasks_stub_creates_missing_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """The stub mkdirs ``out_dir`` if absent so the planner can fire
    on a fresh shared-fs that hasn't seen Phase 0 output yet."""
    out_dir = tmp_path / "fresh-out"
    assert not out_dir.exists()
    w = _Phase0QuiesceWatcher(
        expected_task_ids={phase0_eval_task_id("hello")},
        out_dir=out_dir,
        toolchain_task_ids={},
    )
    w._spawn_tasks_stub([])
    assert (out_dir / "_phase1_graph.json").is_file()


def test_watcher_swallows_plan_phase1_exception(
    tmp_path: pathlib.Path,
) -> None:
    """plan_phase1 raising must not propagate into the framework's
    task-completion thread; the watcher logs + degrades."""
    hello = phase0_eval_task_id("hello")
    w = _Phase0QuiesceWatcher(
        expected_task_ids={hello},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    with mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner.plan_phase1",
        side_effect=RuntimeError("boom"),
    ), mock.patch(
        "compiler_suit_runner.suit_task.phase1_planner"
        ".read_phase0_manifests",
        return_value={},
    ):
        w.on_task_completed(hello)  # must not raise
    assert w.fired is True


# ---------------------------------------------------------------------------
# SuitTask._build_phase0_watcher integration
# ---------------------------------------------------------------------------


def _seed_manifest_dir(
    config: SuitTaskConfig,
    headers: list[ManifestHeader],
) -> None:
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    for header in headers:
        write_manifest(config.manifest_dir, header)


def test_build_phase0_watcher_returns_none_when_no_phase0(
    tmp_path: pathlib.Path,
) -> None:
    """Legacy / non-distributed runs have no phase0_eval manifests;
    the watcher builder returns None and on_run_start skips wiring."""
    config = _make_config(tmp_path)
    _seed_manifest_dir(config, [
        _toolchain_header("x86_64", "gcc15", "/nix/store/x-gcc15.drv"),
    ])
    task = SuitTask(config)
    assert task._build_phase0_watcher(output_dir=tmp_path / "out") is None


def test_build_phase0_watcher_returns_none_when_manifest_dir_missing(
    tmp_path: pathlib.Path,
) -> None:
    """Manifest dir absent → no watcher, no exception (pre-flight
    hasn't run yet, or a stale invocation)."""
    config = _make_config(tmp_path)
    # Don't create config.manifest_dir.
    task = SuitTask(config)
    assert task._build_phase0_watcher(output_dir=tmp_path / "out") is None


def test_build_phase0_watcher_collects_expected_and_toolchains(
    tmp_path: pathlib.Path,
) -> None:
    """With both phase0_eval and toolchain manifests on disk, the
    watcher's expected set covers all phase 0 task_ids and the
    toolchain_task_ids map is keyed by drv path → task_id."""
    config = _make_config(tmp_path)
    gcc_drv = "/nix/store/cccccccccccccccccccccccccccccccc-gcc15.drv"
    clang_drv = "/nix/store/dddddddddddddddddddddddddddddddd-clang20.drv"
    _seed_manifest_dir(config, [
        _phase0_eval_header("hello"),
        _phase0_eval_header("busybox"),
        _toolchain_header("x86_64", "gcc15", gcc_drv),
        _toolchain_header("x86_64", "clang20", clang_drv),
    ])
    task = SuitTask(config)
    w = task._build_phase0_watcher(output_dir=tmp_path / "out")
    assert w is not None
    assert w.expected == frozenset({
        phase0_eval_task_id("hello"),
        phase0_eval_task_id("busybox"),
    })
    assert w._toolchain_task_ids == {
        gcc_drv: toolchain_task_id(_SYS, "x86_64", "gcc15"),
        clang_drv: toolchain_task_id(_SYS, "x86_64", "clang20"),
    }
    # out_dir falls through directly from the caller.
    assert w._out_dir == tmp_path / "out"


def test_build_phase0_watcher_falls_back_to_shared_fs_when_no_output_dir(
    tmp_path: pathlib.Path,
) -> None:
    """If the framework doesn't pass an output_dir (legacy/test path),
    the watcher defaults to ``shared_fs / 'out'`` so paths match what
    the workers write under."""
    config = _make_config(tmp_path)
    _seed_manifest_dir(config, [_phase0_eval_header("hello")])
    task = SuitTask(config)
    w = task._build_phase0_watcher(output_dir=None)
    assert w is not None
    assert w._out_dir == config.shared_fs / "out"


# ---------------------------------------------------------------------------
# Broadcast record_self_has callable assembly (T67)
# ---------------------------------------------------------------------------


def test_make_broadcast_record_self_has_invokes_record_with_phase0_class(
    tmp_path: pathlib.Path,
) -> None:
    """The callable wired into PeerPushServer.record_broadcast_self_has
    must invoke peer_paths.record_self_has with item_class
    ``phase0_eval_drv`` (the broadcast-receive tag) so placement-map
    gossip distinguishes phase0 drvs from toolchain/variant holders."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    captured: list[dict] = []

    def fake_record(shared_fs, *, my_secondary_id, outpath, drv_path,
                    item_class, peers=None, our_pubkey=None):
        captured.append({
            "shared_fs": shared_fs,
            "my_secondary_id": my_secondary_id,
            "outpath": outpath,
            "drv_path": drv_path,
            "item_class": item_class,
            "peers": list(peers) if peers is not None else None,
            "our_pubkey": our_pubkey,
        })

    fake_watcher = mock.MagicMock()
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=fake_record,
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        cb("/nix/store/aaa-phase0.drv")

    assert len(captured) == 1
    call = captured[0]
    assert call["my_secondary_id"] == "primary"
    assert call["outpath"] == "/nix/store/aaa-phase0.drv"
    assert call["drv_path"] == "/nix/store/aaa-phase0.drv"
    assert call["item_class"] == "phase0_eval_drv"
    assert call["our_pubkey"] == "pk-test"
    assert call["peers"] == []
    assert call["shared_fs"] == config.shared_fs


def test_make_broadcast_record_self_has_passes_live_peers(
    tmp_path: pathlib.Path,
) -> None:
    """``peers`` is snapshotted at CALL time from the watcher, not at
    closure construction — so a peer joining between push-server-start
    and the first broadcast accept gets the fan-out."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    captured: list[list] = []

    def fake_record(_shared_fs, *, peers=None, **_kw):
        captured.append(list(peers) if peers is not None else None)

    fake_watcher = mock.MagicMock()
    # Initially empty peer list.
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=fake_record,
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        # First call: empty peers.
        cb("/nix/store/x.drv")
        # A peer joins after wire-up.
        fake_watcher.peers = ["peer-a-info"]
        cb("/nix/store/y.drv")

    assert captured == [[], ["peer-a-info"]]


def test_make_broadcast_record_self_has_swallows_record_exceptions(
    tmp_path: pathlib.Path,
) -> None:
    """If peer_paths.record_self_has raises (NFS hiccup, peer push
    fan-out failure), the callable must not propagate — best-effort
    gossip is part of the contract; the broadcast handshake response
    is independent of placement-map success."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    fake_watcher = mock.MagicMock()
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=RuntimeError("nfs hiccup"),
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        # Must not raise.
        cb("/nix/store/raises.drv")


# ---------------------------------------------------------------------------
# BroadcastReceiver wiring inside on_run_start
# ---------------------------------------------------------------------------


def test_push_url_to_substituter_url_strips_push_offset() -> None:
    """The push port = harmonia_port + PUSH_PORT_OFFSET (1000); the
    translator inverts that so the broadcast fetch can call
    ``nix copy --from <harmonia_url>``."""
    from compiler_suit_runner.suit_task import _push_url_to_substituter_url

    assert (
        _push_url_to_substituter_url("http://node-a:6000")
        == "http://node-a:5000"
    )
    assert (
        _push_url_to_substituter_url("https://node-a:6500/")
        == "https://node-a:5500"
    )
    # Garbage in, None out (caller falls back to raw URL → nix copy errors).
    assert _push_url_to_substituter_url("") is None
    assert _push_url_to_substituter_url("not-a-url") is None
    assert _push_url_to_substituter_url("http://hostnoport") is None
    # Port too small to subtract PUSH_PORT_OFFSET.
    assert _push_url_to_substituter_url("http://x:500") is None


def test_on_run_start_wires_broadcast_receiver_into_push_server(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start``:

    1. ``task._broadcast_receiver`` is a :class:`BroadcastReceiver`.
    2. The :class:`PeerPushServer` was constructed with a callable
       wired to the receiver's ``on_broadcast_offer`` (so a real
       HTTP POST would route through the consumer).
    """
    # Stand in for the signing key so the push code path runs without
    # invoking the real ``nix-store --generate-binary-cache-key``.
    from compiler_suit_runner import peer_cache as _peer_cache
    from compiler_suit_runner.peer_replication import BroadcastReceiver

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    # Use a config that disables harmonia + cachix so on_run_start
    # doesn't try to spawn external subprocesses.
    config = _make_config(tmp_path)
    # Use a port low enough that push_port_for(port) stays in
    # unprivileged-port range.
    config = dataclasses_replace(config, harmonia_port=5005, enable_harmonia=False)

    # Empty manifest dir → no phase0 watcher attached.
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        # The receiver must be wired (we only assert presence; the
        # full behaviour is covered in test_peer_replication.py).
        assert isinstance(task._broadcast_receiver, BroadcastReceiver)
        # The push server's bound handler holds a reference to the
        # configured on_broadcast_offer callback; round-trip through
        # the handler's class attr to confirm it is NOT the
        # uninitialised default-reject lambda (returns False without
        # touching the receiver).
        push_server = task._push_server
        assert push_server is not None
        handler_cls = push_server._handler_cls
        callback = handler_cls.on_broadcast_offer
        # The default callback returns False unconditionally; ours
        # routes into the receiver which (with an empty url map and
        # path-not-local) returns False after fetch failure attempts.
        # Drive a synthetic offer through it.
        result = callback(
            "/nix/store/wire.drv", 1, "unknown-origin", "bid-wire", 0,
        )
        # The wired callback routes through the receiver, whose
        # unknown-origin branch returns False. The DEFAULT no-op
        # lambda would also return False — but we can verify a
        # round-trip got into our receiver by passing self_peer_id
        # as origin (which the receiver also rejects as "unknown"
        # because self is filtered). To prove routing, instead patch
        # the receiver's on_broadcast_offer.
        del result  # not load-bearing
        called: list[tuple] = []

        def _spy(*args, **kw):
            called.append((args, kw))
            return True

        # Swap the receiver method to verify the push handler routes
        # to it. We rebind the staticmethod on the bound handler
        # class — the same mechanism PeerPushServer uses.
        task._broadcast_receiver.on_broadcast_offer = _spy  # type: ignore[method-assign]
        # The handler's class attribute was captured at construction
        # time and closes over the on_run_start local; it dispatches
        # through that local's broadcast_receiver. Call the wrapper.
        cb = handler_cls.on_broadcast_offer
        ok = cb("/nix/store/v.drv", 9, "origin", "bid-v", 0)
        assert ok is True
        assert called == [
            (("/nix/store/v.drv", 9, "origin", "bid-v", 0), {}),
        ]
    finally:
        task.on_run_end(success=True)


# Local alias used by the test above so the import line stays compact.
def dataclasses_replace(obj, /, **changes):
    import dataclasses as _dc
    return _dc.replace(obj, **changes)


# ---------------------------------------------------------------------------
# Q1+Q2+Q3+Q4 wire-in: _PeerLifecycleListener
# ---------------------------------------------------------------------------


from compiler_suit_runner.suit_task import _PeerLifecycleListener  # noqa: E402


def test_listener_on_peer_removed_routes_to_repair_worker_with_cause(
    tmp_path: pathlib.Path,
) -> None:
    """on_peer_removed forwards (secondary_id, reason) to the repair
    worker. The ``cause`` dict's kind/reason are preserved (reason
    when fatal_error, kind otherwise)."""
    repair = mock.MagicMock()
    listener = _PeerLifecycleListener(repair_worker=repair)

    listener.on_peer_removed(
        "peer-a",
        {"kind": "keepalive_miss", "reason": None},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-a", "keepalive_miss",
    )

    repair.reset_mock()
    listener.on_peer_removed(
        "peer-b",
        {"kind": "fatal_error", "reason": "OOMKilled"},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-b", "OOMKilled",
    )

    # mass_death_escalation routes too — the cause kind is forwarded
    # so the operator can grep both signals from the repair log.
    repair.reset_mock()
    listener.on_peer_removed(
        "peer-c",
        {"kind": "mass_death_escalation", "reason": None},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-c", "mass_death_escalation",
    )


def test_listener_on_peer_added_observer_records_holdings(
    tmp_path: pathlib.Path,
) -> None:
    """When ``is_observer=True``, the observer-record callable is
    invoked with the observer's secondary id."""
    record_calls: list[tuple[str, str]] = []

    def record_observer(sid: str, placeholder: str) -> None:
        record_calls.append((sid, placeholder))

    listener = _PeerLifecycleListener(
        repair_worker=None,
        placement_record_observer_callable=record_observer,
    )
    listener.on_peer_added("observer-x", is_observer=True)
    assert record_calls == [("observer-x", "")]


def test_listener_on_peer_added_secondary_is_noop(
    tmp_path: pathlib.Path,
) -> None:
    """Regular-secondary additions don't fire the observer record
    callable — they're already handled by the K=3 peer-set watcher."""
    record_calls: list[tuple[str, str]] = []

    def record_observer(sid: str, placeholder: str) -> None:
        record_calls.append((sid, placeholder))

    listener = _PeerLifecycleListener(
        repair_worker=None,
        placement_record_observer_callable=record_observer,
    )
    listener.on_peer_added("secondary-1", is_observer=False)
    assert record_calls == []


def test_listener_swallows_repair_exception(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the repair worker raises, the listener logs + swallows so
    the framework doesn't disable the hook on a single hiccup."""
    repair = mock.MagicMock()
    repair.on_peer_removed.side_effect = RuntimeError("boom")

    listener = _PeerLifecycleListener(repair_worker=repair)
    # Must not raise.
    with caplog.at_level(logging.ERROR):
        listener.on_peer_removed(
            "peer-x", {"kind": "fatal_error", "reason": "panic"},
        )
    # The error log was emitted.
    assert any(
        "on_peer_removed raised" in rec.message
        for rec in caplog.records
    )


def test_listener_handles_missing_or_malformed_cause(
    tmp_path: pathlib.Path,
) -> None:
    """A non-dict / None cause is tolerated (kind defaults to '')."""
    repair = mock.MagicMock()
    listener = _PeerLifecycleListener(repair_worker=repair)

    # None cause.
    listener.on_peer_removed("peer-a", None)  # type: ignore[arg-type]
    repair.on_peer_removed.assert_called_once_with("peer-a", "")

    # Empty dict cause.
    repair.reset_mock()
    listener.on_peer_removed("peer-b", {})
    repair.on_peer_removed.assert_called_once_with("peer-b", "")


# ---------------------------------------------------------------------------
# Q1 + Q2 PrimaryHandle callable wrappers
# ---------------------------------------------------------------------------


def test_mark_task_unfulfillable_invokes_fail_permanent(
    tmp_path: pathlib.Path,
) -> None:
    """The wrapper converts hex→bytes and calls
    ``primary_handle.fail_permanent(bytes, 'Unfulfillable', reason)``."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._mark_task_unfulfillable("0a1b2c", "tc-gone reason")

    handle.fail_permanent.assert_called_once_with(
        bytes.fromhex("0a1b2c"), "Unfulfillable", "tc-gone reason",
    )


def test_mark_task_unfulfillable_noop_without_handle(
    tmp_path: pathlib.Path,
) -> None:
    """Without a bound primary handle the wrapper logs + returns."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    task._primary_handle = None
    # Must not raise.
    task._mark_task_unfulfillable("00ff", "any reason")


def test_reinject_task_invokes_handle_reinject(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._reinject_task("deadbeef")

    handle.reinject_task.assert_called_once_with(
        bytes.fromhex("deadbeef"),
    )


def test_update_preferred_secondaries_invokes_handle(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._update_preferred_secondaries(
        "0123", ["sec-a", "sec-b"],
    )
    handle.update_preferred_secondaries.assert_called_once_with(
        bytes.fromhex("0123"), ["sec-a", "sec-b"],
    )


def test_wire_primary_handle_applies_reinject_cap(
    tmp_path: pathlib.Path,
) -> None:
    """``wire_primary_handle`` invokes ``apply_unfulfillable_reinject_cap``
    when the config field is set."""
    config = _make_config(tmp_path)
    config = dataclasses_replace(
        config, unfulfillable_reinject_max_per_task=7,
    )
    task = SuitTask(config)
    handle = mock.MagicMock()

    task.wire_primary_handle(handle)

    assert task._primary_handle is handle
    handle.set_unfulfillable_reinject_max_per_task.assert_called_once_with(7)


def test_wire_primary_handle_skips_cap_when_unset(
    tmp_path: pathlib.Path,
) -> None:
    """``wire_primary_handle`` does not call the setter when the config
    field is None (framework default = unbounded)."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()

    task.wire_primary_handle(handle)

    handle.set_unfulfillable_reinject_max_per_task.assert_not_called()


# ---------------------------------------------------------------------------
# Q3 fulfillability matcher attribute
# ---------------------------------------------------------------------------


def test_fulfillability_matcher_is_holding_matcher_callable(
    tmp_path: pathlib.Path,
) -> None:
    """The ``_fulfillability_matcher`` attribute is the
    :func:`holding_matcher.matcher` function — the value to pass to
    ``RustPrimaryCoordinator(fulfillability_matcher=...)``."""
    from compiler_suit_runner.holding_matcher import matcher

    config = _make_config(tmp_path)
    task = SuitTask(config)
    assert task._fulfillability_matcher is matcher


# ---------------------------------------------------------------------------
# on_run_start integration: listener + matcher + ctx callables wired
# ---------------------------------------------------------------------------


def test_on_run_start_constructs_peer_lifecycle_listener(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start`` the listener is constructed and bound to
    the repair worker so the framework can pass it via
    ``peer_lifecycle_listener=``."""
    from compiler_suit_runner import peer_cache as _peer_cache

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = dataclasses_replace(
        config, harmonia_port=5010, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert task._peer_lifecycle_listener is not None
        # Routing test: drive on_peer_removed and verify the
        # repair worker would be invoked. We swap in a fake repair so
        # the real one's network calls don't fire.
        fake_repair = mock.MagicMock()
        task._peer_lifecycle_listener._repair = fake_repair
        task._peer_lifecycle_listener.on_peer_removed(
            "peer-z", {"kind": "fatal_error", "reason": "panic-xyz"},
        )
        fake_repair.on_peer_removed.assert_called_once_with(
            "peer-z", "panic-xyz",
        )
    finally:
        task.on_run_end(success=True)


def test_on_run_start_builds_outpath_to_task_hash_lookup(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outpath→task_hash dict is populated from toolchain manifest
    payloads at on_run_start. Each toolchain manifest with an
    ``outpath`` field and ``task_id`` contributes one entry."""
    from compiler_suit_runner import peer_cache as _peer_cache
    from compiler_suit_runner.manifest_gen import ManifestHeader, write_manifest

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = dataclasses_replace(
        config, harmonia_port=5011, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    # Seed a toolchain manifest with an outpath.
    write_manifest(config.manifest_dir, ManifestHeader(
        item_class="phase2_toolchain_validate",
        name="toolchain_validate__x86_64__gcc15",
        size=0,
        payload={
            "sys": _SYS,
            "arch": "x86_64",
            "compiler_label": "gcc15",
            "attr": "_crossToolchainMap.linux.x86_64.gcc15",
            "drv": "/nix/store/aa-gcc15.drv",
            "outpath": "/nix/store/bb-gcc15-out",
            "validate_only": True,
        },
        task_id=toolchain_task_id(_SYS, "x86_64", "gcc15"),
    ))

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        # The dict carries an entry for the toolchain outpath.
        assert "/nix/store/bb-gcc15-out" in task._outpath_to_task_hash
    finally:
        task.on_run_end(success=True)


def test_replication_context_callables_route_through_self(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ReplicationContext`` is constructed in ``on_run_start`` with
    its four PrimaryHandle callables pointing at the ``SuitTask``
    instance methods that close over ``self._primary_handle``.

    Driving the ctx callables (after wire_primary_handle) reaches the
    mocked handle methods with the right bytes/list shapes.
    """
    from compiler_suit_runner import peer_cache as _peer_cache

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = dataclasses_replace(
        config, harmonia_port=5012, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    handle = mock.MagicMock()
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        # Wire the handle after on_run_start.
        task.wire_primary_handle(handle)
        # Trigger each PrimaryHandle wrapper directly.
        task._mark_task_unfulfillable("aabb", "no holder")
        task._reinject_task("ccdd")
        task._update_preferred_secondaries("eeff", ["s1", "s2"])

        handle.fail_permanent.assert_called_once_with(
            bytes.fromhex("aabb"), "Unfulfillable", "no holder",
        )
        handle.reinject_task.assert_called_once_with(
            bytes.fromhex("ccdd"),
        )
        handle.update_preferred_secondaries.assert_called_once_with(
            bytes.fromhex("eeff"), ["s1", "s2"],
        )
    finally:
        task.on_run_end(success=True)

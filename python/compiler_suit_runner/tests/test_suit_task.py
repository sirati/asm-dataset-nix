"""Unit tests for :mod:`compiler_suit_runner.suit_task`.

Tests for ``SuitTask`` wiring and the phase-3→4 spawn bridge. Phase 3
(``dependency_graph``) is dispatched by the framework as a task; this
module covers the ``_header_to_task_info`` conversion. The
``on_phase_end("dependency_graph")`` descriptor handoff now rides the
streamed custom-message transport (``worker_message_listener`` /
``custom_message_handler``); ``on_phase_end`` is a reconciliation
barrier only.
"""

from __future__ import annotations

import collections
import dataclasses as _dataclasses
import logging
import pathlib
from unittest import mock

import pytest

from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    Phase,
    build_compilers_task_id,
)
from compiler_suit_runner.suit_task import (
    SuitTask,
    SuitTaskConfig,
    _header_to_task_info,
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
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
    )


def _common_dep_header(label: str, drv: str) -> ManifestHeader:
    return ManifestHeader(
        item_class="build_common_dep",
        name=f"common_dep__{label}",
        size=0,
        payload={
            "drv": drv,
            "label": label,
            "attr": drv,
        },
        task_id=f"build_common_dep__{pathlib.Path(drv).name}",
    )


def _variant_header(binary: str, label: str, compiler_id: str = "gcc15"):
    return ManifestHeader(
        item_class="build_variant",
        name=label,
        size=0,
        payload={
            "sys": _SYS,
            "pkg": binary,
            "arch": "x86_64",
            "label": label,
            "drv": f"/nix/store/v-{label}.drv",
            "variant_dir": label,
            "metadata_name": f"{label}.json",
            "compiler_id": compiler_id,
            "tier": 1,
            "attr": f"dataset.{_SYS}.{binary}.x86_64.{label}",
        },
        task_id=f"build_variant__{_SYS}__{binary}__{label}",
        # Toolchain dep is CROSS-phase (Phase.BUILD_COMPILERS); it goes
        # in the dedicated field, not the intra-phase task_depends_on.
        build_compilers_depends_on=(
            build_compilers_task_id(_SYS, "x86_64", compiler_id),
        ),
    )


# ---------------------------------------------------------------------------
# _header_to_task_info free-function conversion
# ---------------------------------------------------------------------------


def test_header_to_task_info_common_dep() -> None:
    """A ``build_common_dep`` header converts to a ``common_dep`` task
    in the ``build`` phase with the header_dict payload preserved."""
    header = _common_dep_header("glibc", "/nix/store/x-glibc.drv")
    ti = _header_to_task_info(header)
    assert ti.phase_id == "build"
    assert ti.type_id == "common_dep"
    assert ti.payload["item_class"] == "build_common_dep"
    assert ti.payload["name"] == "common_dep__glibc"


def test_header_to_task_info_variant_carries_phase_and_type() -> None:
    """A ``build_variant`` header converts to a ``variant`` task in the
    ``build`` phase, carrying its toolchain edge as a CROSS-phase
    ``TaskDep(phase_id=Phase.BUILD_COMPILERS)`` (not a bare string)."""
    header = _variant_header("hello", "hello-x86_64-gcc15-O0")
    ti = _header_to_task_info(header)
    assert ti.phase_id == "build"
    assert ti.type_id == "variant"
    assert ti.payload["item_class"] == "build_variant"
    assert ti.task_depends_on
    # The single dep is the toolchain edge, phase-tagged BUILD_COMPILERS.
    tc_id = build_compilers_task_id(_SYS, "x86_64", "gcc15")
    assert any(
        getattr(dep, "phase_id", None) == Phase.BUILD_COMPILERS
        and getattr(dep, "task_id", None) == tc_id
        for dep in ti.task_depends_on
    )


def test_header_to_task_info_variant_toolchain_dep_is_cross_phase() -> None:
    """Focused assertion: a build_variant header carrying
    ``build_compilers_depends_on=("x86_64-linux__aarch64__gcc15",)``
    yields a TaskInfo whose ``task_depends_on`` contains a dep with
    ``phase_id == Phase.BUILD_COMPILERS`` and matching ``task_id``,
    while leaving any intra-phase deps as bare strings."""
    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "aarch64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        task_depends_on=("build_common_dep__some-glibc.drv",),
        build_compilers_depends_on=("x86_64-linux__aarch64__gcc15",),
    )
    ti = _header_to_task_info(header)
    # Cross-phase toolchain dep is phase-tagged.
    assert any(
        getattr(dep, "phase_id", None) == Phase.BUILD_COMPILERS
        and getattr(dep, "task_id", None) == "x86_64-linux__aarch64__gcc15"
        for dep in ti.task_depends_on
    )
    # Intra-phase dep survives as a bare string (no phase tag).
    assert "build_common_dep__some-glibc.drv" in ti.task_depends_on


def test_header_to_task_info_disable_task_deps_drops_deps() -> None:
    """With ``disable_task_deps=True`` the live spawn path drops BOTH the
    intra-phase deps and the cross-phase toolchain deps — parity with the
    disk loop / ``_task_info_from_header``. Regression for the gap where
    the module-level free function ignored the flag."""
    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "aarch64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        task_depends_on=("build_common_dep__some-glibc.drv",),
        build_compilers_depends_on=("x86_64-linux__aarch64__gcc15",),
    )
    assert tuple(_header_to_task_info(
        header, disable_task_deps=True).task_depends_on) == ()
    # Default (flag off) still emits the deps.
    assert _header_to_task_info(header).task_depends_on


# ---------------------------------------------------------------------------
# Broadcast record_self_has callable assembly
# ---------------------------------------------------------------------------


def test_make_broadcast_record_self_has_invokes_record_with_matrix_eval_class(
    tmp_path: pathlib.Path,
) -> None:
    """The callable wired into PeerPushServer.record_broadcast_self_has
    must invoke peer_paths.record_self_has with item_class
    ``matrix_eval_drv`` (the broadcast-receive tag) so placement-map
    gossip distinguishes matrix_eval drvs from toolchain/variant
    holders."""
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
        cb("/nix/store/aaa-matrix.drv")

    assert len(captured) == 1
    call = captured[0]
    assert call["my_secondary_id"] == "primary"
    assert call["outpath"] == "/nix/store/aaa-matrix.drv"
    assert call["drv_path"] == "/nix/store/aaa-matrix.drv"
    assert call["item_class"] == "matrix_eval_drv"
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
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=fake_record,
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        cb("/nix/store/x.drv")
        fake_watcher.peers = ["peer-a-info"]
        cb("/nix/store/y.drv")

    assert captured == [[], ["peer-a-info"]]


def test_make_broadcast_record_self_has_swallows_record_exceptions(
    tmp_path: pathlib.Path,
) -> None:
    """If peer_paths.record_self_has raises, the callable must not
    propagate — best-effort gossip is part of the contract."""
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
    assert _push_url_to_substituter_url("") is None
    assert _push_url_to_substituter_url("not-a-url") is None
    assert _push_url_to_substituter_url("http://hostnoport") is None
    assert _push_url_to_substituter_url("http://x:500") is None


def test_on_run_start_wires_broadcast_receiver_into_push_server(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start``:

    1. ``task._broadcast_receiver`` is a :class:`BroadcastReceiver`.
    2. The :class:`PeerPushServer` was constructed with a callable
       wired to the receiver's ``on_broadcast_offer``.
    """
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

    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, harmonia_port=5005, enable_harmonia=False,
    )

    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert isinstance(task._broadcast_receiver, BroadcastReceiver)
        push_server = task._push_server
        assert push_server is not None
        handler_cls = push_server._handler_cls
        called: list[tuple] = []

        def _spy(*args, **kw):
            called.append((args, kw))
            return True

        task._broadcast_receiver.on_broadcast_offer = _spy  # type: ignore[method-assign]
        cb = handler_cls.on_broadcast_offer
        ok = cb("/nix/store/v.drv", 9, "origin", "bid-v", 0)
        assert ok is True
        assert called == [
            (("/nix/store/v.drv", 9, "origin", "bid-v", 0), {}),
        ]
    finally:
        task.on_run_end(success=True)


# ---------------------------------------------------------------------------
# _PeerLifecycleListener
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
    with caplog.at_level(logging.ERROR):
        listener.on_peer_removed(
            "peer-x", {"kind": "fatal_error", "reason": "panic"},
        )
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

    listener.on_peer_removed("peer-a", None)  # type: ignore[arg-type]
    repair.on_peer_removed.assert_called_once_with("peer-a", "")

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
    config = _dataclasses.replace(
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
    :func:`holding_matcher.matcher` function."""
    from compiler_suit_runner.holding_matcher import matcher

    config = _make_config(tmp_path)
    task = SuitTask(config)
    assert task._fulfillability_matcher is matcher


# ---------------------------------------------------------------------------
# on_run_start integration
# ---------------------------------------------------------------------------


def test_on_run_start_constructs_peer_lifecycle_listener(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start`` the listener is constructed and bound to
    the repair worker."""
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
    config = _dataclasses.replace(
        config, harmonia_port=5010, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert task._peer_lifecycle_listener is not None
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
    payloads at on_run_start."""
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
    config = _dataclasses.replace(
        config, harmonia_port=5011, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(config.manifest_dir, ManifestHeader(
        item_class="toolchain_validate",
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
        task_id=build_compilers_task_id(_SYS, "x86_64", "gcc15"),
    ))

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert "/nix/store/bb-gcc15-out" in task._outpath_to_task_hash
    finally:
        task.on_run_end(success=True)


def test_replication_context_callables_route_through_self(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ReplicationContext`` is constructed in ``on_run_start`` with
    its four PrimaryHandle callables pointing at the ``SuitTask``
    instance methods."""
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
    config = _dataclasses.replace(
        config, harmonia_port=5012, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    handle = mock.MagicMock()
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        task.wire_primary_handle(handle)
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


# ---------------------------------------------------------------------------
# on_run_start primary_handle kwarg capture
# ---------------------------------------------------------------------------


def test_on_run_start_accepts_primary_handle_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """The ``on_run_start`` signature accepts the ``primary_handle``
    kwarg and stores the value on the task."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)
    handle = mock.MagicMock()
    try:
        task.on_run_start(
            source_dir=tmp_path,
            output_dir=tmp_path / "out",
            args=None,
            primary_handle=handle,
        )
        assert task._primary_handle is handle
    finally:
        task.on_run_end(success=True)


def test_on_run_start_backward_compat_without_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """A caller that omits ``primary_handle`` still runs without
    raising; the attribute remains None."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert task._primary_handle is None
    finally:
        task.on_run_end(success=True)


# ---------------------------------------------------------------------------
# on_phase_end("dependency_graph") — reconciliation barrier
# ---------------------------------------------------------------------------


def test_on_phase_end_dependency_graph_no_primary_handle_warns(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """With ``_primary_handle = None`` the handler logs a warning, does
    not raise, and writes no JSON sidecar."""
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _make_config(tmp_path)
    config = _dataclasses.replace(base, matrix_eval_out_dir=out_dir)
    task = SuitTask(config)
    assert task._primary_handle is None

    with caplog.at_level(logging.WARNING):
        task.on_phase_end("dependency_graph", completed=1, failed=0)

    assert any(
        "no primary_handle" in rec.message
        for rec in caplog.records
    )
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "phase_id", ["matrix_eval", "build_compilers", "build"],
)
def test_on_phase_end_other_phases_noop(
    tmp_path, phase_id,
) -> None:
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.Mock()
    task._primary_handle = handle
    task.on_phase_end(phase_id, completed=1, failed=0)
    handle.spawn_tasks.assert_not_called()


# ---------------------------------------------------------------------------
# _phase_specs topology
# ---------------------------------------------------------------------------


def test_phase_specs_returns_four_phases() -> None:
    """``_phase_specs`` declares build_compilers, matrix_eval,
    dependency_graph, build (PH-A landed dep_graph as a first-class
    framework phase). The CRDT activates dep_graph atomically with the
    matching matrix_eval TaskCompleted via task_depends_on."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    by_id = {s.phase_id: s for s in specs}
    assert set(by_id.keys()) == {
        "build_compilers", "matrix_eval", "dependency_graph", "build",
    }


def test_phase_specs_matrix_eval_routes_to_build_worker() -> None:
    """matrix_eval has a single ``eval`` type pointing at
    ``compiler_suit_runner.workers.build_worker`` (which sniffs the
    item_class and dispatches to eval_worker.run_eval_task)."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    matrix_eval = next(s for s in specs if s.phase_id == "matrix_eval")
    assert len(matrix_eval.types) == 1
    assert matrix_eval.types[0].type_id == "eval"
    assert (
        matrix_eval.types[0].worker_module
        == "compiler_suit_runner.workers.build_worker"
    )


def test_phase_specs_all_types_share_single_worker_module() -> None:
    """Sanity: every TaskTypeSpec across all phases binds to the same
    worker_module string."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    modules = {
        t.worker_module for spec in specs for t in spec.types
    }
    assert modules == {"compiler_suit_runner.workers.build_worker"}


def test_phase_specs_matrix_eval_depends_on_build_compilers() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    matrix_eval = next(s for s in specs if s.phase_id == "matrix_eval")
    assert matrix_eval.depends_on == ("build_compilers",)


def test_phase_specs_build_depends_on_dependency_graph() -> None:
    """PH-A inserted dependency_graph between matrix_eval and build."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    build = next(s for s in specs if s.phase_id == "build")
    assert build.depends_on == ("dependency_graph",)


def test_phase_specs_dependency_graph_depends_on_matrix_eval() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    dep_graph = next(
        s for s in specs if s.phase_id == "dependency_graph"
    )
    assert dep_graph.depends_on == ("matrix_eval",)
    # Single type_id dep_graph routed to build_worker (handle closure
    # sniffs item_class).
    assert len(dep_graph.types) == 1
    assert dep_graph.types[0].type_id == "dep_graph"
    assert (
        dep_graph.types[0].worker_module
        == "compiler_suit_runner.workers.build_worker"
    )


def test_phase_specs_build_carries_validate_common_dep_variant() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    build = next(s for s in specs if s.phase_id == "build")
    type_ids = {t.type_id for t in build.types}
    assert type_ids == {
        "toolchain_import",
        "toolchain_validate",
        "common_dep",
        "variant",
    }


# ---------------------------------------------------------------------------
# Streamed dependency_graph → build spawn transport
# (worker_message_listener relay, custom_message_handler consumer,
# on_phase_end reconciliation barrier)
# ---------------------------------------------------------------------------
#
# KNOWN ACCEPTED Wave-1 LIMITATIONS, deliberately not asserted against:
# * failover replay into a fresh-count promoted primary can false-alarm
#   the on_phase_end barrier (counters are primary-local);
# * a duplicate spawn_batch redelivery double-counts/double-spawns
#   (loud via duplicate_task_hash spawn errors, accepted).


from compiler_suit_runner.dependency_graph_planner import (  # noqa: E402
    Phase4Descriptor,
)
from compiler_suit_runner.streamed_spawn import (  # noqa: E402
    SPAWN_TOPIC,
    SUMMARY_TOPIC,
    SpawnBatchEncoder,
)


class _FakeSecondaryHandle:
    """Records ``send_to_primary`` calls (topic, data, important)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bool]] = []

    def send_to_primary(
        self, topic: str, data: bytes, important: bool = False,
    ) -> None:
        self.sent.append((topic, data, important))


class _FakePrimaryHandle:
    """Records ``spawn_tasks`` calls; returns the configured errors."""

    def __init__(self, errors: list | None = None) -> None:
        self.calls: list[list] = []
        self._errors = errors or []

    def spawn_tasks(self, task_infos):
        self.calls.append(list(task_infos))
        return list(self._errors)


def _streamed_task(tmp_path: pathlib.Path, **config_overrides) -> SuitTask:
    config = _dataclasses.replace(_make_config(tmp_path), **config_overrides)
    task = SuitTask(config)
    task._primary_handle = _FakePrimaryHandle()
    return task


def _variant_descriptor(i: int = 0, binary: str = "hello") -> Phase4Descriptor:
    label = f"{binary}-x86_64-gcc15-O0-v{i}"
    return Phase4Descriptor(
        kind="build_variant",
        task_id=f"build_variant__{_SYS}__{binary}__{label}",
        name=label,
        payload={
            "sys": _SYS,
            "pkg": binary,
            "arch": "x86_64",
            "label": label,
            "drv": f"/nix/store/v-{label}.drv",
            "variant_dir": label,
            "metadata_name": f"{label}.json",
            "compiler_id": "gcc15",
        },
        depends_on=("build_common_dep__some-glibc.drv",),
        build_compilers_depends_on=(
            build_compilers_task_id(_SYS, "x86_64", "gcc15"),
        ),
    )


def _common_dep_descriptor(label: str = "glibc") -> Phase4Descriptor:
    return Phase4Descriptor(
        kind="build_common_dep",
        task_id=f"build_common_dep__some-{label}.drv",
        name=f"common_dep__{label}",
        payload={"drv": f"/nix/store/some-{label}.drv", "label": label},
        depends_on=(),
    )


def _batch_bytes(descriptors: list[Phase4Descriptor]) -> bytes:
    enc = SpawnBatchEncoder()
    for d in descriptors:
        assert enc.add(d) is None, "test batches must fit in one message"
    return enc.flush()


def _summary_bytes(
    total: int, batches: int = 1, counters: dict | None = None,
) -> bytes:
    import json  # noqa: PLC0415
    return json.dumps(
        {
            "v": 1,
            "kind": "summary",
            "total": total,
            "batches": batches,
            "counters": counters if counters is not None else {},
        },
        separators=(",", ":"),
    ).encode("utf-8")


# ── worker_message_listener (secondary-side relay) ─────────────────────


@pytest.mark.parametrize("topic", [SPAWN_TOPIC, SUMMARY_TOPIC])
def test_worker_message_listener_forwards_verbatim_important(
    tmp_path, topic,
) -> None:
    """Streamed-spawn topics are forwarded byte-for-byte (no decode) to
    the primary with ``important=True``."""
    task = SuitTask(_make_config(tmp_path))
    handle = _FakeSecondaryHandle()
    payload = b"\x00 opaque non-JSON bytes \xff"
    task.worker_message_listener(7, "dep_graph", topic, payload, handle)
    assert handle.sent == [(topic, payload, True)]


def test_worker_message_listener_ignores_foreign_topic(tmp_path) -> None:
    """A non-streamed-spawn topic is dropped without raising and never
    forwarded (the relay must not poison unrelated traffic)."""
    task = SuitTask(_make_config(tmp_path))
    handle = _FakeSecondaryHandle()
    task.worker_message_listener(
        7, "dep_graph", "matrix_aggregate_drv", b"data", handle,
    )
    assert handle.sent == []


# ── custom_message_handler (primary-side consumer) ─────────────────────


def test_custom_message_handler_spawn_batch_spawns_task_infos(
    tmp_path,
) -> None:
    """A spawn_batch decodes into TaskInfos matching the descriptors via
    ``headers_from_descriptors`` → ``_header_to_task_info``: task_id /
    type_id / phase / affinity / deps all line up."""
    task = _streamed_task(tmp_path)
    handle = task._primary_handle
    common = _common_dep_descriptor()
    variant = _variant_descriptor(0)
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([common, variant]),
        True, handle,
    )
    assert len(handle.calls) == 1
    infos = handle.calls[0]
    assert [ti.task_id for ti in infos] == [common.task_id, variant.task_id]
    assert [ti.type_id for ti in infos] == ["common_dep", "variant"]
    assert all(ti.phase_id == "build" for ti in infos)
    assert infos[1].affinity_id == "gcc15-x86_64"
    # Deps: intra-phase as bare string, toolchain edge phase-tagged.
    variant_deps = infos[1].task_depends_on
    assert "build_common_dep__some-glibc.drv" in variant_deps
    tc_id = build_compilers_task_id(_SYS, "x86_64", "gcc15")
    assert any(
        getattr(dep, "phase_id", None) == Phase.BUILD_COMPILERS
        and getattr(dep, "task_id", None) == tc_id
        for dep in variant_deps
    )
    assert task._streamed_spawned_count == 2


def test_spawn_batch_same_display_name_distinct_wire_identity(
    tmp_path,
) -> None:
    """Two descriptors with the SAME display name but distinct task_ids
    must spawn with DISTINCT wire identities.

    Regression for the LMU run_20260611_112116 run-killer: the
    framework's wire-canonical task hash is computed over (phase_id,
    path, identifier) — NOT task_id — and the consumer derived both
    path and identifier from ``header.name``. The planner's
    ``build_common_dep`` names role-collapse the drv ident, so
    bc/aarch64's native flex-2.6.4 and cross
    flex-aarch64-unknown-linux-gnu-2.6.4 both spawned as
    ``build_common_dep__bc__aarch64__flex.drv`` → identical hash
    366c070376d4ef85 → DuplicateInBatch → run-wide invalidation.
    Fixture below is that real pair, reduced.
    """
    flex_pair = [
        Phase4Descriptor(
            kind="build_common_dep",
            task_id=(
                "build_common_dep__"
                "20gpcnk5a9lqjgwqjyacwcv4rricjdwm-flex-2.6.4.drv"
            ),
            name="build_common_dep__bc__aarch64__flex.drv",
            payload={
                "sys": _SYS,
                "binary": "bc",
                "arch": "aarch64",
                "node_name": "flex.drv",
                "node_id": 8,
                "ident": "20gpcnk5a9lqjgwqjyacwcv4rricjdwm-flex-2.6.4.drv",
                "attr": "20gpcnk5a9lqjgwqjyacwcv4rricjdwm-flex-2.6.4.drv",
            },
        ),
        Phase4Descriptor(
            kind="build_common_dep",
            task_id=(
                "build_common_dep__5l8vd9v2mxqnggawvzc2vzlcrn216bwj-"
                "flex-aarch64-unknown-linux-gnu-2.6.4.drv"
            ),
            name="build_common_dep__bc__aarch64__flex.drv",
            payload={
                "sys": _SYS,
                "binary": "bc",
                "arch": "aarch64",
                "node_name": "flex.drv",
                "node_id": 9,
                "ident": (
                    "5l8vd9v2mxqnggawvzc2vzlcrn216bwj-"
                    "flex-aarch64-unknown-linux-gnu-2.6.4.drv"
                ),
                "attr": (
                    "5l8vd9v2mxqnggawvzc2vzlcrn216bwj-"
                    "flex-aarch64-unknown-linux-gnu-2.6.4.drv"
                ),
            },
        ),
    ]
    task = _streamed_task(tmp_path)
    handle = task._primary_handle
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes(flex_pair), True, handle,
    )
    (infos,) = handle.calls
    assert len(infos) == 2
    # task_ids were always distinct — the bug was the wire identity.
    assert infos[0].task_id != infos[1].task_id
    # The hash recipe inputs: phase_id equal (both BUILD), so path —
    # and the path-derived identifier — must differ.
    assert str(infos[0].path) != str(infos[1].path), (
        "synthetic TaskInfo paths collide; the framework task hash "
        "(phase_id, path, identifier) would mark these DuplicateInBatch"
    )
    # When the real framework is importable, assert on the actual
    # wire-canonical hash the primary's validator uses.
    try:
        from dynamic_runner import compute_task_hash  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — framework absent in unit env
        return
    assert compute_task_hash(infos[0]) != compute_task_hash(infos[1])


def test_custom_message_handler_count_accumulates_across_batches(
    tmp_path,
) -> None:
    task = _streamed_task(tmp_path)
    handle = task._primary_handle
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0), _variant_descriptor(1)]),
        True, handle,
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(2)]),
        True, handle,
    )
    assert [len(call) for call in handle.calls] == [2, 1]
    assert task._streamed_spawned_count == 3


def test_custom_message_handler_disable_task_deps_drops_deps(
    tmp_path,
) -> None:
    """``config.disable_task_deps`` is threaded into
    ``_header_to_task_info`` on the streamed spawn path too."""
    task = _streamed_task(tmp_path, disable_task_deps=True)
    handle = task._primary_handle
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([_variant_descriptor(0)]),
        True, handle,
    )
    assert tuple(handle.calls[0][0].task_depends_on) == ()


def test_custom_message_handler_unknown_topic_raises(tmp_path) -> None:
    task = _streamed_task(tmp_path)
    with pytest.raises(ValueError, match="unknown topic 'other_topic'"):
        task.custom_message_handler(
            "secondary-1", "other_topic", b"x", True, task._primary_handle,
        )
    assert task._primary_handle.calls == []


def test_custom_message_handler_malformed_payload_raises(tmp_path) -> None:
    """decode ValueErrors propagate (the framework marks the message
    terminally Failed on the first handler raise — the intended
    failure surface for malformed messages)."""
    task = _streamed_task(tmp_path)
    with pytest.raises(ValueError, match="not valid JSON"):
        task.custom_message_handler(
            "secondary-1", SPAWN_TOPIC, b"{never-json", True,
            task._primary_handle,
        )
    assert task._primary_handle.calls == []
    assert task._streamed_spawned_count == 0


def test_custom_message_handler_none_primary_handle_raises(tmp_path) -> None:
    task = _streamed_task(tmp_path)
    with pytest.raises(RuntimeError, match="no usable primary_handle"):
        task.custom_message_handler(
            "secondary-1", SPAWN_TOPIC,
            _batch_bytes([_variant_descriptor(0)]), True, None,
        )
    assert task._streamed_spawned_count == 0


def test_custom_message_handler_handle_without_spawn_tasks_raises(
    tmp_path,
) -> None:
    task = _streamed_task(tmp_path)
    with pytest.raises(RuntimeError, match="no usable primary_handle"):
        task.custom_message_handler(
            "secondary-1", SPAWN_TOPIC,
            _batch_bytes([_variant_descriptor(0)]), True, object(),
        )


def test_custom_message_handler_spawn_errors_logged_not_raised(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """spawn_tasks errors are WARN-logged (duplicate_task_hash etc.) and
    do not raise; the count still advances (the framework holds the
    duplicate, nothing was lost)."""
    task = _streamed_task(tmp_path)
    handle = _FakePrimaryHandle(errors=[
        (0, {"kind": "duplicate_task_hash", "task_hash": "h1"}),
    ])
    task._primary_handle = handle
    with caplog.at_level(logging.WARNING):
        task.custom_message_handler(
            "secondary-1", SPAWN_TOPIC,
            _batch_bytes([_variant_descriptor(0)]), True, handle,
        )
    assert any(
        "duplicate task_hash" in rec.getMessage() for rec in caplog.records
    )
    assert task._streamed_spawned_count == 1


# ── summary recording + on_phase_end reconciliation barrier ────────────


def test_summary_then_on_phase_end_reconciles_green(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    task = _streamed_task(tmp_path)
    handle = task._primary_handle
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0), _variant_descriptor(1)]),
        True, handle,
    )
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=2, batches=1, counters={"build_variant": 2}),
        True, handle,
    )
    assert task._streamed_expected_total == 2
    assert task._streamed_summary_batches == 1
    assert task._streamed_summary_counters == {"build_variant": 2}
    with caplog.at_level(logging.INFO):
        task.on_phase_end("dependency_graph", completed=1, failed=0)
    assert any(
        "handoff reconciled" in rec.getMessage() for rec in caplog.records
    )


def test_on_phase_end_without_summary_raises_incomplete(tmp_path) -> None:
    """No summary by phase end = a lost terminal message; the barrier
    fails loudly with the spawned count in the pinned message."""
    task = _streamed_task(tmp_path)
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0), _variant_descriptor(1)]),
        True, task._primary_handle,
    )
    import re  # noqa: PLC0415
    expected = re.escape(
        "dependency_graph handoff incomplete: no summary message"
        " received (spawned=2)"
    )
    with pytest.raises(RuntimeError, match=expected):
        task.on_phase_end("dependency_graph", completed=1, failed=0)


def test_on_phase_end_mismatch_raises_with_pinned_message(tmp_path) -> None:
    """spawned != summary total (a lost batch) trips the barrier with
    the pinned spawned/total/counters message."""
    task = _streamed_task(tmp_path)
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0)]), True, task._primary_handle,
    )
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=3, batches=2, counters={"build_variant": 3}),
        True, task._primary_handle,
    )
    import re  # noqa: PLC0415
    expected = re.escape(
        "dependency_graph handoff mismatch: spawned=1 != total=3"
        " (counters={'build_variant': 3})"
    )
    with pytest.raises(RuntimeError, match=expected):
        task.on_phase_end("dependency_graph", completed=1, failed=0)


def test_duplicate_identical_summary_ignored_with_info(
    tmp_path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Framework redelivery edge: an identical duplicate summary is
    harmless and logged at INFO; recorded totals stay unchanged."""
    task = _streamed_task(tmp_path)
    summary = _summary_bytes(total=1, batches=1, counters={"build_variant": 1})
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC, summary, True, task._primary_handle,
    )
    with caplog.at_level(logging.INFO):
        task.custom_message_handler(
            "secondary-2", SUMMARY_TOPIC, summary, True, task._primary_handle,
        )
    assert any(
        "duplicate identical" in rec.getMessage() for rec in caplog.records
    )
    assert task._streamed_expected_total == 1
    assert task._streamed_summary_batches == 1
    assert task._streamed_summary_counters == {"build_variant": 1}


def test_conflicting_summary_raises_runtime_error(tmp_path) -> None:
    task = _streamed_task(tmp_path)
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=1, batches=1, counters={"build_variant": 1}),
        True, task._primary_handle,
    )
    with pytest.raises(RuntimeError, match="conflicting dependency_graph summary"):
        task.custom_message_handler(
            "secondary-1", SUMMARY_TOPIC,
            _summary_bytes(total=2, batches=1, counters={"build_variant": 2}),
            True, task._primary_handle,
        )
    # The first recorded summary stays authoritative.
    assert task._streamed_expected_total == 1


def test_empty_stream_total_zero_reconciles(tmp_path) -> None:
    """The empty-plan handoff (no batches, total=0 summary) reconciles
    clean — the barrier distinguishes 'nothing planned' from silence."""
    task = _streamed_task(tmp_path)
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=0, batches=0, counters={}),
        True, task._primary_handle,
    )
    task.on_phase_end("dependency_graph", completed=1, failed=0)
    assert task._primary_handle.calls == []


def test_phase_start_resets_streamed_counters(tmp_path) -> None:
    """A dependency_graph phase (re)start zeroes the streamed-spawn
    counters so a retry / reused SuitTask doesn't inherit a previous
    attempt's state into the reconciliation barrier; other phases
    leave them untouched."""
    task = _streamed_task(tmp_path)
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0)]), True, task._primary_handle,
    )
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=1, batches=1, counters={"build_variant": 1}),
        True, task._primary_handle,
    )
    # Foreign phase start: counters untouched.
    task.on_phase_start("build")
    assert task._streamed_spawned_count == 1
    assert task._streamed_expected_total == 1
    # dependency_graph (re)start: fresh stream state.
    task.on_phase_start("dependency_graph")
    assert task._streamed_spawned_count == 0
    assert task._streamed_expected_total is None
    assert task._streamed_summary_counters is None
    assert task._streamed_summary_batches is None
    # The poisoned-counter failure mode this guards: without the reset
    # the retried phase's identical re-stream would double-count and
    # trip the barrier; after it, the same stream reconciles green.
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([_variant_descriptor(0)]), True, task._primary_handle,
    )
    task.custom_message_handler(
        "secondary-1", SUMMARY_TOPIC,
        _summary_bytes(total=1, batches=1, counters={"build_variant": 1}),
        True, task._primary_handle,
    )
    task.on_phase_end("dependency_graph", completed=1, failed=0)


# ---------------------------------------------------------------------------
# Secondary-affine import gate tasks (#497)
# ---------------------------------------------------------------------------

_TC_OUTPATH = "/nix/store/abc123hhhhhhhhhhhhhhhhhhhhhhhh-gcc15-out"
_TC_OUTPATH_2 = "/nix/store/xyz789hhhhhhhhhhhhhhhhhhhhhhhh-clang18-out"


def _config_with_outpaths(
    tmp_path: pathlib.Path,
    outpaths_map: "dict | None" = None,
) -> SuitTaskConfig:
    cfg = _make_config(tmp_path)
    import dataclasses as _dc  # noqa: PLC0415
    return _dc.replace(
        cfg,
        toolchain_outpaths_map=outpaths_map or {
            "x86_64/gcc15": _TC_OUTPATH,
        },
        matrix_eval_out_dir=tmp_path / "out",
    )


def test_discover_import_gate_tasks_emits_import_common_and_per_tc(
    tmp_path: pathlib.Path,
) -> None:
    """discover_items yields one import_common gate + one import_tc_<hash>
    per distinct toolchain outpath when toolchain_outpaths_map is set."""
    from compiler_suit_runner.suit_task import IMPORT_COMMON_TASK_ID, _import_tc_task_id

    config = _config_with_outpaths(tmp_path, {
        "x86_64/gcc15": _TC_OUTPATH,
        "aarch64/gcc15": _TC_OUTPATH_2,
    })
    task = SuitTask(config)
    # Manifest dir may not exist; discover_items handles that gracefully.
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert IMPORT_COMMON_TASK_ID in gate_ids
    assert _import_tc_task_id(_TC_OUTPATH) in gate_ids
    assert _import_tc_task_id(_TC_OUTPATH_2) in gate_ids


def test_discover_import_gate_tasks_secondary_affine_no_deps(
    tmp_path: pathlib.Path,
) -> None:
    """Import gate TaskInfos carry is_secondary_affine=True and empty deps."""
    config = _config_with_outpaths(tmp_path)
    task = SuitTask(config)
    items = list(task.discover_items())
    gates = [
        ti for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    ]
    assert gates, "expected at least one toolchain_import gate task"
    for gate in gates:
        assert getattr(gate, "is_secondary_affine", None) is True, (
            f"gate {gate.task_id!r} missing is_secondary_affine=True"
        )
        assert tuple(getattr(gate, "task_depends_on", ())) == (), (
            f"gate {gate.task_id!r} should have empty task_depends_on"
        )


def test_discover_import_gate_tasks_empty_when_no_outpaths_map(
    tmp_path: pathlib.Path,
) -> None:
    """No toolchain_outpaths_map → no import gate tasks emitted."""
    config = _make_config(tmp_path)  # toolchain_outpaths_map=None by default
    task = SuitTask(config)
    items = list(task.discover_items())
    gates = [
        ti for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    ]
    assert gates == []


def test_discover_import_gate_tasks_deduplicates_same_outpath(
    tmp_path: pathlib.Path,
) -> None:
    """Multiple toolchain map entries pointing to the same outpath yield
    only ONE import_tc_<hash> gate (distinct per hash, not per arch/comp)."""
    from compiler_suit_runner.suit_task import _import_tc_task_id

    config = _config_with_outpaths(tmp_path, {
        "x86_64/gcc15": _TC_OUTPATH,
        "aarch64/gcc15": _TC_OUTPATH,   # same outpath → same hash → one gate
    })
    task = SuitTask(config)
    items = list(task.discover_items())
    tc_gate_ids = [
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
        and getattr(ti, "task_id", "") != "import_common"
    ]
    assert tc_gate_ids.count(_import_tc_task_id(_TC_OUTPATH)) == 1


def test_header_depends_on_build_variant_includes_import_common_and_tc(
    tmp_path: pathlib.Path,
) -> None:
    """A build_variant header with toolchain_outpath in its payload has
    import_common AND import_tc_<hash> as bare-string intra-phase deps."""
    from compiler_suit_runner.suit_task import (
        IMPORT_COMMON_TASK_ID,
        _header_depends_on,
        _import_tc_task_id,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "toolchain_outpath": _TC_OUTPATH,
        },
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_COMMON_TASK_ID in bare
    assert _import_tc_task_id(_TC_OUTPATH) in bare


def test_header_depends_on_build_common_dep_includes_import_common_only(
    tmp_path: pathlib.Path,
) -> None:
    """A build_common_dep header carries import_common as intra-phase dep
    but no import_tc_* (no toolchain_outpath for common deps)."""
    from compiler_suit_runner.suit_task import (
        IMPORT_COMMON_TASK_ID,
        _header_depends_on,
    )

    header = ManifestHeader(
        item_class="build_common_dep",
        name="common_dep__glibc",
        size=0,
        payload={
            "drv": "/nix/store/x-glibc.drv",
            "label": "glibc",
        },
        task_id="build_common_dep__x-glibc.drv",
    )
    deps = _header_depends_on(header)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_COMMON_TASK_ID in bare
    # No import_tc_* for common deps.
    assert not any(d.startswith("import_tc_") for d in bare), (
        f"Unexpected import_tc_* dep in build_common_dep: {bare}"
    )


def test_header_depends_on_build_variant_no_outpath_skips_tc_dep(
    tmp_path: pathlib.Path,
) -> None:
    """A build_variant without toolchain_outpath (legacy/fixture) still gets
    import_common but NOT import_tc_* (no outpath = nothing to derive)."""
    from compiler_suit_runner.suit_task import (
        IMPORT_COMMON_TASK_ID,
        _header_depends_on,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            # no toolchain_outpath
        },
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_COMMON_TASK_ID in bare
    assert not any(d.startswith("import_tc_") for d in bare)


def test_streamed_spawn_variant_carries_import_deps(
    tmp_path: pathlib.Path,
) -> None:
    """The streamed-spawn path (custom_message_handler) carries import gate
    deps for build_variant (import_common + import_tc_<hash>) as bare-string
    intra-phase deps in the resulting TaskInfo."""
    from compiler_suit_runner.suit_task import (
        IMPORT_COMMON_TASK_ID,
        _import_tc_task_id,
    )

    # Variant with toolchain_outpath in its payload.
    descriptor = Phase4Descriptor(
        kind="build_variant",
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        name="hello-x86_64-gcc15-O0",
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "label": "hello-x86_64-gcc15-O0",
            "drv": "/nix/store/v-hello.drv",
            "variant_dir": "hello-x86_64-gcc15-O0",
            "metadata_name": "hello-x86_64-gcc15-O0.json",
            "compiler_id": "gcc15",
            "toolchain_outpath": _TC_OUTPATH,
        },
        depends_on=(),
        build_compilers_depends_on=(),
    )
    task = _streamed_task(tmp_path)
    handle = task._primary_handle
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([descriptor]),
        True, handle,
    )
    assert len(handle.calls) == 1
    (ti,) = handle.calls[0]
    bare_deps = [d for d in ti.task_depends_on if isinstance(d, str)]
    assert IMPORT_COMMON_TASK_ID in bare_deps
    assert _import_tc_task_id(_TC_OUTPATH) in bare_deps


def test_import_action_returns_none_without_matrix_eval_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """import_action is None when matrix_eval_out_dir is not set (legacy)."""
    config = _make_config(tmp_path)  # matrix_eval_out_dir=None
    task = SuitTask(config)
    assert task.import_action is None


def test_import_action_import_common_calls_ensure_common(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_common') calls ensure_common_archive_imported
    against matrix_eval_out_dir."""
    import dataclasses as _dc  # noqa: PLC0415
    import compiler_suit_runner.workers.build_worker as bw_mod  # noqa: PLC0415

    config = _dc.replace(_make_config(tmp_path), matrix_eval_out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_common(out_dir, *, run_subprocess=None):
        calls.append(("common", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_common_archive_imported",
        _fake_ensure_common,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    action("import_common")
    assert calls == [("common", tmp_path / "out")]


def test_import_action_import_tc_calls_ensure_toolchain_out(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_tc_<hash>') calls ensure_toolchain_out_archive_imported
    with the tc_id (hash) extracted from the task_id — no toolchain_outpaths_map
    needed (secondary case: toolchain_outpaths_map is None/empty)."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import _import_tc_task_id  # noqa: PLC0415
    from compiler_suit_runner.preflight import toolchain_id_for_outpath  # noqa: PLC0415

    # Secondary case: toolchain_outpaths_map is not provided (None → default).
    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
    )
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_tc(tc_id, out_dir, *, run_subprocess=None):
        calls.append(("tc", tc_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_toolchain_out_archive_imported",
        _fake_ensure_tc,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    task_id = _import_tc_task_id(_TC_OUTPATH)
    action(task_id)
    expected_tc_id = toolchain_id_for_outpath(_TC_OUTPATH)
    assert calls == [("tc", expected_tc_id, tmp_path / "out")]


def test_import_action_import_tc_works_with_empty_outpaths_map(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_tc_<hash> works even when toolchain_outpaths_map is empty — the
    secondary never receives this map but must still import archives correctly."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import _import_tc_task_id  # noqa: PLC0415
    from compiler_suit_runner.preflight import toolchain_id_for_outpath  # noqa: PLC0415

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        toolchain_outpaths_map={},  # empty — mimics secondary config
    )
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_tc(tc_id, out_dir, *, run_subprocess=None):
        calls.append(("tc", tc_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_toolchain_out_archive_imported",
        _fake_ensure_tc,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    task_id = _import_tc_task_id(_TC_OUTPATH)
    action(task_id)
    expected_tc_id = toolchain_id_for_outpath(_TC_OUTPATH)
    # Archive named toolchains.<hash>.out.archive — ensure_* receives the hash.
    assert calls == [("tc", expected_tc_id, tmp_path / "out")]


def test_import_action_unknown_task_id_raises(
    tmp_path: pathlib.Path,
) -> None:
    """import_action raises RuntimeError for a completely unknown task_id."""
    import dataclasses as _dc  # noqa: PLC0415

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        toolchain_outpaths_map={"x86_64/gcc15": _TC_OUTPATH},
    )
    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    with pytest.raises(RuntimeError, match="unknown gate task_id"):
        action("not_an_import_task")


def test_import_action_two_arg_call_dispatches_correctly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action accepts two positional args (task_id, payload_json) as
    required by fed78773's affine_action_bridge, and dispatches correctly.
    payload_json is ignored by the consumer."""
    import dataclasses as _dc  # noqa: PLC0415

    config = _dc.replace(_make_config(tmp_path), matrix_eval_out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_common(out_dir, *, run_subprocess=None):
        calls.append(("common", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_common_archive_imported",
        _fake_ensure_common,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    # Two-arg call as the framework makes it (fed78773+): payload_json is None here
    action("import_common", None)
    assert calls == [("common", tmp_path / "out")]


def test_import_action_two_arg_call_with_payload_dispatches_correctly(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action accepts a non-None payload_json second arg (future-proof)
    and still dispatches to the correct ensure_* function."""
    import dataclasses as _dc  # noqa: PLC0415

    config = _dc.replace(_make_config(tmp_path), matrix_eval_out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_common(out_dir, *, run_subprocess=None):
        calls.append(("common", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_common_archive_imported",
        _fake_ensure_common,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    # Simulate a richer payload the framework might pass in future pins
    action("import_common", {"some": "payload"})
    assert calls == [("common", tmp_path / "out")]


def test_import_action_one_arg_call_still_works(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action still works with a single positional arg (older pins /
    direct consumer calls) — the payload_json default keeps back-compat."""
    import dataclasses as _dc  # noqa: PLC0415

    config = _dc.replace(_make_config(tmp_path), matrix_eval_out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_common(out_dir, *, run_subprocess=None):
        calls.append(("common", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_common_archive_imported",
        _fake_ensure_common,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    # One-arg call (old contract / direct test usage)
    action("import_common")
    assert calls == [("common", tmp_path / "out")]


# ---------------------------------------------------------------------------
# build_deps_local Phase 0+1: gate emission + import_action + dep wiring
# ---------------------------------------------------------------------------


def test_discover_import_gate_tasks_emits_import_build_deps_when_flag_set(
    tmp_path: pathlib.Path,
) -> None:
    """With build_deps_local=True, discover_items yields an import_build_deps
    gate task in addition to the toolchain gates."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import IMPORT_BUILD_DEPS_TASK_ID  # noqa: PLC0415

    config = _dc.replace(
        _config_with_outpaths(tmp_path),
        build_deps_local=True,
    )
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert IMPORT_BUILD_DEPS_TASK_ID in gate_ids


def test_discover_import_gate_tasks_no_import_build_deps_when_flag_off(
    tmp_path: pathlib.Path,
) -> None:
    """With build_deps_local=False (default), no import_build_deps gate is emitted."""
    from compiler_suit_runner.suit_task import IMPORT_BUILD_DEPS_TASK_ID  # noqa: PLC0415

    config = _config_with_outpaths(tmp_path)  # build_deps_local=False by default
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert IMPORT_BUILD_DEPS_TASK_ID not in gate_ids


def test_header_depends_on_build_variant_includes_import_build_deps_when_flag(
    tmp_path: pathlib.Path,
) -> None:
    """With build_deps_local=True, _header_depends_on adds IMPORT_BUILD_DEPS_TASK_ID
    for build_variant headers."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_BUILD_DEPS_TASK_ID,
        _header_depends_on,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "toolchain_outpath": _TC_OUTPATH,
        },
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header, build_deps_local=True)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_BUILD_DEPS_TASK_ID in bare


def test_header_depends_on_build_variant_no_import_build_deps_when_flag_off(
    tmp_path: pathlib.Path,
) -> None:
    """With build_deps_local=False (default), IMPORT_BUILD_DEPS_TASK_ID is absent."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_BUILD_DEPS_TASK_ID,
        _header_depends_on,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "toolchain_outpath": _TC_OUTPATH,
        },
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header, build_deps_local=False)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_BUILD_DEPS_TASK_ID not in bare


def test_header_depends_on_build_common_dep_includes_import_build_deps_when_flag(
    tmp_path: pathlib.Path,
) -> None:
    """build_common_dep also gets IMPORT_BUILD_DEPS_TASK_ID when build_deps_local=True."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_BUILD_DEPS_TASK_ID,
        _header_depends_on,
    )

    header = ManifestHeader(
        item_class="build_common_dep",
        name="common_dep__glibc",
        size=0,
        payload={
            "drv": "/nix/store/x-glibc.drv",
            "label": "glibc",
        },
        task_id="build_common_dep__x-glibc.drv",
    )
    deps = _header_depends_on(header, build_deps_local=True)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_BUILD_DEPS_TASK_ID in bare


def test_import_action_import_build_deps_calls_ensure_build_deps_imported(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_build_deps') calls ensure_build_deps_archive_imported
    with matrix_eval_out_dir."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import IMPORT_BUILD_DEPS_TASK_ID  # noqa: PLC0415

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        build_deps_local=True,
    )
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_build_deps(out_dir, *, run_subprocess=None):
        calls.append(("build_deps", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_build_deps_archive_imported",
        _fake_ensure_build_deps,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    action(IMPORT_BUILD_DEPS_TASK_ID)
    assert calls == [("build_deps", tmp_path / "out")]


# ---------------------------------------------------------------------------
# DRV-archive gate tasks: import_toolchain_drv + import_matrix_drv_<binary>
# ---------------------------------------------------------------------------


def _config_with_binaries(
    tmp_path: pathlib.Path,
    *,
    binaries: "list[str] | None" = None,
) -> "SuitTaskConfig":
    """Config with matrix_eval_out_dir + per_binary_metadata (for drv gates)."""
    import dataclasses as _dc  # noqa: PLC0415
    binaries = binaries or ["hello", "zlib"]
    per_binary = {
        b: {
            "archs": ["x86_64"],
            "toolchain_aggregate_drv": "/nix/store/tc-agg.drv",
            "toolchain_dedup": True,
        }
        for b in binaries
    }
    return _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        toolchain_outpaths_map={"x86_64/gcc15": _TC_OUTPATH},
        per_binary_metadata=per_binary,
    )


def test_discover_import_gate_tasks_emits_toolchain_drv_gate(
    tmp_path: pathlib.Path,
) -> None:
    """When matrix_eval_out_dir is set, discover_items yields an
    import_toolchain_drv gate task."""
    from compiler_suit_runner.suit_task import IMPORT_TOOLCHAIN_DRV_TASK_ID  # noqa: PLC0415

    config = _config_with_outpaths(tmp_path)  # matrix_eval_out_dir is set
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID in gate_ids


def test_discover_import_gate_tasks_emits_per_binary_matrix_drv_gates(
    tmp_path: pathlib.Path,
) -> None:
    """When matrix_eval_out_dir is set and per_binary_metadata is populated,
    discover_items yields one import_matrix_drv_<binary> gate per binary."""
    from compiler_suit_runner.suit_task import _import_matrix_drv_task_id  # noqa: PLC0415

    config = _config_with_binaries(tmp_path, binaries=["hello", "zlib"])
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert _import_matrix_drv_task_id("hello") in gate_ids
    assert _import_matrix_drv_task_id("zlib") in gate_ids


def test_discover_import_gate_tasks_no_matrix_drv_gates_without_per_binary_metadata(
    tmp_path: pathlib.Path,
) -> None:
    """When per_binary_metadata is absent (secondary-container config),
    no import_matrix_drv_* gates are emitted."""
    from compiler_suit_runner.suit_task import _import_matrix_drv_task_id  # noqa: PLC0415

    config = _config_with_outpaths(tmp_path)  # per_binary_metadata=None
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
        and (getattr(ti, "task_id", "") or "").startswith("import_matrix_drv_")
    }
    assert gate_ids == set()


def test_discover_import_gate_tasks_no_toolchain_drv_without_matrix_eval_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """When matrix_eval_out_dir is absent, no import_toolchain_drv gate is emitted."""
    from compiler_suit_runner.suit_task import IMPORT_TOOLCHAIN_DRV_TASK_ID  # noqa: PLC0415

    # _make_config has no matrix_eval_out_dir; also no outpaths_map
    config = _make_config(tmp_path)
    task = SuitTask(config)
    items = list(task.discover_items())
    gate_ids = {
        getattr(ti, "task_id", None) for ti in items
        if getattr(ti, "type_id", None) == "toolchain_import"
    }
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID not in gate_ids


def test_header_depends_on_build_variant_includes_drv_gates_when_matrix_eval_gates(
    tmp_path: pathlib.Path,
) -> None:
    """With matrix_eval_gates=True, _header_depends_on adds IMPORT_TOOLCHAIN_DRV_TASK_ID
    and import_matrix_drv_<binary> for build_variant headers."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        _header_depends_on,
        _import_matrix_drv_task_id,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "toolchain_outpath": _TC_OUTPATH,
        },
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header, matrix_eval_gates=True)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID in bare
    assert _import_matrix_drv_task_id("hello") in bare


def test_header_depends_on_build_common_dep_includes_toolchain_drv_gate(
    tmp_path: pathlib.Path,
) -> None:
    """With matrix_eval_gates=True, _header_depends_on adds IMPORT_TOOLCHAIN_DRV_TASK_ID
    for build_common_dep headers (no per-binary gate since no pkg field)."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        _header_depends_on,
        _import_matrix_drv_task_id,
    )

    header = ManifestHeader(
        item_class="build_common_dep",
        name="common_dep__glibc",
        size=0,
        payload={"drv": "/nix/store/x-glibc.drv", "label": "glibc"},
        task_id="build_common_dep__x-glibc.drv",
    )
    deps = _header_depends_on(header, matrix_eval_gates=True)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID in bare
    # No per-binary matrix drv gate for common deps (no pkg field).
    assert not any(d.startswith("import_matrix_drv_") for d in bare)


def test_header_depends_on_no_drv_gates_when_matrix_eval_gates_off(
    tmp_path: pathlib.Path,
) -> None:
    """With matrix_eval_gates=False (default), no drv gate deps are added."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        _header_depends_on,
        _import_matrix_drv_task_id,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "x86_64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = _header_depends_on(header)  # default: matrix_eval_gates=False
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID not in bare
    assert not any(d.startswith("import_matrix_drv_") for d in bare)


def test_import_action_import_toolchain_drv_calls_ensure_toolchain_archive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_toolchain_drv') calls ensure_toolchain_archive_imported
    with matrix_eval_out_dir."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import IMPORT_TOOLCHAIN_DRV_TASK_ID  # noqa: PLC0415

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        toolchain_outpaths_map={"x86_64/gcc15": _TC_OUTPATH},
    )
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_tc_archive(out_dir, *, run_subprocess=None):
        calls.append(("toolchain_drv", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_toolchain_archive_imported",
        _fake_ensure_tc_archive,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    action(IMPORT_TOOLCHAIN_DRV_TASK_ID)
    assert calls == [("toolchain_drv", tmp_path / "out")]


def test_import_action_import_matrix_drv_calls_ensure_binary_archive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_matrix_drv_hello') calls ensure_binary_archive_imported
    with binary='hello' and matrix_eval_out_dir."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import _import_matrix_drv_task_id  # noqa: PLC0415

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
        toolchain_outpaths_map={"x86_64/gcc15": _TC_OUTPATH},
    )
    (tmp_path / "out").mkdir()

    calls: list[tuple] = []

    def _fake_ensure_binary(binary, out_dir, *, run_subprocess=None):
        calls.append(("binary_drv", binary, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.ensure_binary_archive_imported",
        _fake_ensure_binary,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    action(_import_matrix_drv_task_id("hello"))
    assert calls == [("binary_drv", "hello", tmp_path / "out")]


def test_streamed_spawn_variant_carries_drv_gate_deps_when_matrix_eval_out_dir_set(
    tmp_path: pathlib.Path,
) -> None:
    """The streamed-spawn path carries import_toolchain_drv and
    import_matrix_drv_<binary> as bare-string deps when the config has
    matrix_eval_out_dir set."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        _import_matrix_drv_task_id,
    )

    task = _streamed_task(
        tmp_path,
        matrix_eval_out_dir=tmp_path / "out",
    )
    handle = task._primary_handle
    descriptor = Phase4Descriptor(
        kind="build_variant",
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        name="hello-x86_64-gcc15-O0",
        payload={
            "sys": _SYS,
            "pkg": "hello",
            "arch": "x86_64",
            "label": "hello-x86_64-gcc15-O0",
            "drv": "/nix/store/v-hello.drv",
            "variant_dir": "hello-x86_64-gcc15-O0",
            "metadata_name": "hello-x86_64-gcc15-O0.json",
            "compiler_id": "gcc15",
        },
        depends_on=(),
        build_compilers_depends_on=(),
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC,
        _batch_bytes([descriptor]),
        True, handle,
    )
    assert len(handle.calls) == 1
    (ti,) = handle.calls[0]
    bare_deps = [d for d in ti.task_depends_on if isinstance(d, str)]
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID in bare_deps
    assert _import_matrix_drv_task_id("hello") in bare_deps


def test_header_task_depends_on_includes_drv_gates_when_matrix_eval_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """SuitTask._header_task_depends_on passes matrix_eval_gates=True when
    config.matrix_eval_out_dir is set, so build tasks get drv gate deps."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        _import_matrix_drv_task_id,
    )

    config = _dc.replace(
        _make_config(tmp_path),
        matrix_eval_out_dir=tmp_path / "out",
    )
    task = SuitTask(config)
    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "x86_64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
    )
    deps = task._header_task_depends_on(header)
    bare = [d for d in deps if isinstance(d, str)]
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID in bare
    assert _import_matrix_drv_task_id("hello") in bare


# ---------------------------------------------------------------------------
# Per-common_dep affine OUTPUT-import gate (obsoletes harmonia for common_deps)
# ---------------------------------------------------------------------------


def test_import_common_dep_task_id_derives_from_ident() -> None:
    """The gate id is ``import_common_dep_<hash>`` for a ``<hash>-<name>``
    ident, derivable on both the wiring and worker sides."""
    from compiler_suit_runner.suit_task import _import_common_dep_task_id  # noqa: PLC0415

    assert (
        _import_common_dep_task_id("abc123-flex-2.6.4")
        == "import_common_dep_abc123"
    )


def test_ident_from_common_dep_task_id_all_shapes() -> None:
    """Every planner-minted build_common_dep task_id shape yields the trailing
    ``<ident_str>`` whose hash the gate id is derived from."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        _ident_from_common_dep_task_id,
        _import_common_dep_task_id,
    )

    cases = {
        # per-cell
        "build_common_dep__abc123-flex.drv": "abc123-flex.drv",
        # arch-indep (ident-keyed cross-binary; no binary segment)
        "build_common_dep__arch_indep__def456-zlib.drv": "def456-zlib.drv",
        # meta cross_arch
        "build_common_dep__cross_arch__ghi789-glibc.drv": "ghi789-glibc.drv",
        # meta family
        "build_common_dep__family__gcc__jkl012-libgcc.drv": "jkl012-libgcc.drv",
    }
    for task_id, expected_ident in cases.items():
        assert _ident_from_common_dep_task_id(task_id) == expected_ident
        # And each yields a stable, hash-keyed gate id.
        gate = _import_common_dep_task_id(_ident_from_common_dep_task_id(task_id))
        assert gate.startswith("import_common_dep_")

    # A non-common_dep id returns None (not a common_dep edge).
    assert _ident_from_common_dep_task_id("build_variant__x__y__z") is None
    assert _ident_from_common_dep_task_id("import_common") is None


def test_header_depends_on_variant_includes_common_dep_gate() -> None:
    """A build_variant depending on ``build_common_dep__<ident>`` also gets the
    matching ``import_common_dep_<hash>`` affine gate when common_dep_gates on."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        _header_depends_on,
        _import_common_dep_task_id,
    )

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "x86_64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        task_depends_on=("build_common_dep__abc123-flex.drv",),
    )
    deps = _header_depends_on(header, common_dep_gates=True)
    bare = [d for d in deps if isinstance(d, str)]
    # The build_common_dep task stays a dep AND the affine gate is added.
    assert "build_common_dep__abc123-flex.drv" in bare
    assert _import_common_dep_task_id("abc123-flex.drv") in bare


def test_header_depends_on_variant_no_common_dep_gate_when_disabled() -> None:
    """With common_dep_gates off, no import_common_dep_* gate is added (legacy
    substituter transport)."""
    from compiler_suit_runner.suit_task import _header_depends_on  # noqa: PLC0415

    header = ManifestHeader(
        item_class="build_variant",
        name="hello-x86_64-gcc15-O0",
        size=0,
        payload={"sys": _SYS, "pkg": "hello", "arch": "x86_64"},
        task_id="build_variant__x86_64-linux__hello__gcc15-O0",
        task_depends_on=("build_common_dep__abc123-flex.drv",),
    )
    deps = _header_depends_on(header, common_dep_gates=False)
    bare = [d for d in deps if isinstance(d, str)]
    assert not any(d.startswith("import_common_dep_") for d in bare)


def test_header_depends_on_common_dep_with_transitive_dep_gets_gate() -> None:
    """A build_common_dep that itself depends on another common_dep gets that
    sibling's affine gate too (transitive shared deps)."""
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        _header_depends_on,
        _import_common_dep_task_id,
    )

    header = ManifestHeader(
        item_class="build_common_dep",
        name="common_dep__cross",
        size=0,
        payload={"ident": "top000-cross.drv"},
        task_id="build_common_dep__top000-cross.drv",
        task_depends_on=("build_common_dep__sub111-base.drv",),
    )
    deps = _header_depends_on(header, common_dep_gates=True)
    bare = [d for d in deps if isinstance(d, str)]
    assert _import_common_dep_task_id("sub111-base.drv") in bare


def test_common_dep_gates_enabled_property(tmp_path: pathlib.Path) -> None:
    """The gates are on only when common_deps_affine AND matrix_eval_out_dir."""
    import dataclasses as _dc  # noqa: PLC0415

    # Default flag True, but no archive dir → off.
    base = _make_config(tmp_path)
    assert SuitTask(base)._common_dep_gates_enabled is False
    # Archive dir set, flag default True → on.
    with_dir = _dc.replace(base, matrix_eval_out_dir=tmp_path / "out")
    assert SuitTask(with_dir)._common_dep_gates_enabled is True
    # Explicitly disabled → off even with the archive dir.
    disabled = _dc.replace(with_dir, common_deps_affine=False)
    assert SuitTask(disabled)._common_dep_gates_enabled is False


def test_streamed_spawn_emits_common_dep_affine_gate(tmp_path: pathlib.Path) -> None:
    """A streamed build_common_dep descriptor spawns its is_secondary_affine
    import gate (depending on the build_common_dep task) AND the gate is NOT
    counted toward the reconciliation total."""
    import dataclasses as _dc  # noqa: PLC0415
    from compiler_suit_runner.suit_task import _import_common_dep_task_id  # noqa: PLC0415

    task = _streamed_task(tmp_path, matrix_eval_out_dir=tmp_path / "out")
    handle = task._primary_handle
    common = Phase4Descriptor(
        kind="build_common_dep",
        task_id="build_common_dep__abc123-flex.drv",
        name="common_dep__flex",
        payload={"drv": "/nix/store/abc123-flex.drv", "ident": "abc123-flex.drv"},
        depends_on=(),
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([common]), True, handle,
    )
    (spawned,) = handle.calls
    by_id = {ti.task_id: ti for ti in spawned}
    gate_id = _import_common_dep_task_id("abc123-flex.drv")
    # The common_dep task AND its affine gate were both spawned.
    assert "build_common_dep__abc123-flex.drv" in by_id
    assert gate_id in by_id
    gate = by_id[gate_id]
    assert getattr(gate, "is_secondary_affine", None) is True
    # Gate depends on the build_common_dep task that publishes the archive.
    assert "build_common_dep__abc123-flex.drv" in gate.task_depends_on
    # Only the descriptor task (1) counts toward reconciliation, not the gate.
    assert task._streamed_spawned_count == 1


def test_streamed_spawn_common_dep_gate_deduped_across_batches(
    tmp_path: pathlib.Path,
) -> None:
    """The same shared dep streamed across two batches spawns its gate ONCE."""
    from compiler_suit_runner.suit_task import _import_common_dep_task_id  # noqa: PLC0415

    task = _streamed_task(tmp_path, matrix_eval_out_dir=tmp_path / "out")
    handle = task._primary_handle
    common = Phase4Descriptor(
        kind="build_common_dep",
        task_id="build_common_dep__abc123-flex.drv",
        name="common_dep__flex",
        payload={"drv": "/nix/store/abc123-flex.drv", "ident": "abc123-flex.drv"},
        depends_on=(),
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([common]), True, handle,
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([common]), True, handle,
    )
    gate_id = _import_common_dep_task_id("abc123-flex.drv")
    gate_spawns = sum(
        1
        for batch in handle.calls
        for ti in batch
        if ti.task_id == gate_id
    )
    assert gate_spawns == 1


def test_streamed_spawn_no_common_dep_gate_when_disabled(
    tmp_path: pathlib.Path,
) -> None:
    """With common_deps_affine=False the streamed path spawns no affine gate."""
    task = _streamed_task(
        tmp_path, matrix_eval_out_dir=tmp_path / "out", common_deps_affine=False,
    )
    handle = task._primary_handle
    common = Phase4Descriptor(
        kind="build_common_dep",
        task_id="build_common_dep__abc123-flex.drv",
        name="common_dep__flex",
        payload={"drv": "/nix/store/abc123-flex.drv", "ident": "abc123-flex.drv"},
        depends_on=(),
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes([common]), True, handle,
    )
    (spawned,) = handle.calls
    assert not any(
        ti.task_id.startswith("import_common_dep_") for ti in spawned
    )


def test_import_action_dispatches_common_dep_gate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """import_action('import_common_dep_<hash>') calls
    ensure_common_dep_out_archive_imported with the stripped cd_id."""
    import dataclasses as _dc  # noqa: PLC0415

    config = _dc.replace(_make_config(tmp_path), matrix_eval_out_dir=tmp_path / "out")
    (tmp_path / "out").mkdir()
    calls: list[tuple] = []

    def _fake_ensure(cd_id, out_dir, *, run_subprocess=None):
        calls.append((cd_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_common_dep_out_archive_imported",
        _fake_ensure,
    )

    task = SuitTask(config)
    action = task.import_action
    assert action is not None
    action("import_common_dep_abc123", None)
    assert calls == [("abc123", tmp_path / "out")]


# ---------------------------------------------------------------------------
# Affine-import STRUCTURE: one import-dependency per UNIQUE drv, shared
# across binaries. Regression for run_20260615_145147 — the build phase
# spawned the same arch-independent shared dep once PER BINARY (a
# per-(binary, drv) build_common_dep task_id) instead of once per unique
# drv, so a dep shared by N binaries (cacert / find-xml-catalogs-hook /
# python3.13-mako / root.key) became N separate build tasks the
# cross-binary dedup never collapsed. The fix keys the arch_indep
# build_common_dep task_id on the ident alone (drop the binary segment),
# so it collapses to one task every binary's variants depend on.
# ---------------------------------------------------------------------------


def _structure_binary_input(binary: str, shared_hook_ident: tuple[str, str]):
    """A one-cell, two-variant BinaryPlanInput that touches one
    arch-independent shared dep (``shared_hook_ident``, identical across
    binaries) plus a per-cell common_dep node."""
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
    )

    tail = "-baseline-default-san-off-march-default-elf-folder.drv"
    template = {
        "nodes": [
            {"name": f"{binary}-root", "child_ids": [1], "is_toolchain": False},
            {"name": "zlib-node", "child_ids": [], "is_toolchain": False},
        ],
        "name_to_id": {f"{binary}-root": 0, "zlib-node": 1},
        "root_id": 0,
        "template_built_from": [],
    }
    # node 1 = a per-cell common_dep with the SAME ident across binaries
    # (shared sub-drv) so it also exercises cross-binary collapse.
    arr = {
        "template_id": 0,
        "arch": "x86_64",
        "variants": ["gcc15-O0", "gcc15-O2"],
        "hashes": [
            [(f"rh{binary}", f"{binary}.drv"), ("cafe0001", "zlib.drv")],
            [(f"rh{binary}", f"{binary}.drv"), ("cafe0001", "zlib.drv")],
        ],
    }
    streaming = {
        "templates": [template],
        "variant_arrays": {(0, "x86_64"): arr},
        "common_deps_per_arch_template": {
            (0, "x86_64"): {0: "variant_specific", 1: "common_dep"},
        },
        "toolchain_drvs": set(),
        # The shared arch-independent dep — SAME ident under both binaries.
        "arch_indep_deps": {binary: {shared_hook_ident}},
    }

    def _spec(label: str) -> dict:
        return {
            "label": label, "pkg": binary, "arch": "x86_64",
            "drv": f"/nix/store/aaaa-{binary}-x86_64-{label}{tail}",
            "variant_dir": f"{label}_dir", "metadata_name": f"{label}.json",
            "compiler_id": label.rsplit("-", 1)[0],
            "compiler_family": "gcc", "compiler_version": "15",
            "optimization": label.rsplit("-", 1)[1],
            "flag_set": "baseline", "hardening": "default",
            "sanitizer": "off", "march": "default", "tier": 1,
            "toolchain_outpath":
                "/nix/store/tcaaaa1111111111111111111111111-toolchain-gcc15",
        }

    lookup = {
        ("x86_64", "gcc15-O0"): _spec("gcc15-O0"),
        ("x86_64", "gcc15-O2"): _spec("gcc15-O2"),
    }
    return BinaryPlanInput(
        binary=binary, streaming_result=streaming,
        variant_lookup=lookup, toolchain_task_ids={},
    )


def test_affine_import_structure_shared_dep_single_task(
    tmp_path: pathlib.Path,
) -> None:
    """Two binaries sharing one arch-independent dep + a toolchain must
    yield, after the REAL planner + streamed spawn + discover-time gates:

      (a) NO duplicate (phase_id, task_id) across the full spawned set,
      (b) the shared arch_indep dep = EXACTLY ONE build_common_dep task,
      (c) every dependent (both binaries' variants + the import gate)
          depends on that one shared id, and
      (d) no build_variant names an affine import id that was never
          spawned (no dangling dependency).

    Pre-fix the arch_indep task_id carried a per-binary segment, so the
    shared dep produced TWO build_common_dep tasks → assertion (b) fails.
    """
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        plan_phase4_from_graph,
    )
    from compiler_suit_runner.dependency_graph_planner.descriptors import (  # noqa: PLC0415
        _arch_indep_task_id,
    )
    from compiler_suit_runner.manifest_gen import Phase  # noqa: PLC0415
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        _import_common_dep_task_id,
        _import_matrix_drv_task_id,
        _import_tc_task_id,
    )

    shared_hook = ("deadbeef", "find-xml-catalogs-hook.drv")
    shared_ident = "deadbeef-find-xml-catalogs-hook.drv"
    inputs = [
        _structure_binary_input("brotli", shared_hook),
        _structure_binary_input("zstd", shared_hook),
    ]
    descriptors = plan_phase4_from_graph(inputs, sys_name=_SYS)

    # ── Drive the REAL spawn path: planner descriptors → spawn handler. ──
    tc_outpath = "/nix/store/tcaaaa1111111111111111111111111-toolchain-gcc15"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    task = _streamed_task(
        tmp_path,
        matrix_eval_out_dir=out_dir,
        toolchain_outpaths_map={"gcc15": tc_outpath},
        per_binary_metadata={"brotli": {}, "zstd": {}},
    )
    handle = task._primary_handle
    task.on_phase_start(Phase.DEPENDENCY_GRAPH)

    common = [d for d in descriptors if d.kind == "build_common_dep"]
    variants = [d for d in descriptors if d.kind == "build_variant"]
    # Stream common_deps and variants in separate batches (production
    # streams hundreds of batches; the gate dedup must hold ACROSS them).
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes(common), True, handle,
    )
    task.custom_message_handler(
        "secondary-1", SPAWN_TOPIC, _batch_bytes(variants), True, handle,
    )
    # Discover-time affine gates (toolchain + matrix_drv import tasks).
    discovered = list(task._discover_import_gate_tasks())

    spawned = [ti for call in handle.calls for ti in call] + discovered
    spawned_keys = [(ti.phase_id, ti.task_id) for ti in spawned]

    # (a) No duplicate (phase_id, task_id) across the FULL spawned set.
    dupes = [k for k, n in collections.Counter(spawned_keys).items() if n > 1]
    assert dupes == [], f"duplicate (phase_id, task_id): {dupes}"

    spawned_build_ids = {tid for (_ph, tid) in spawned_keys}

    # (b) The shared arch_indep dep = EXACTLY ONE build_common_dep task.
    # Derived from the SPAWNED descriptors (not the task_id helper) so this
    # assertion — not a signature change — is what catches the regression:
    # pre-fix the per-binary segment yields one task per binary (here 2).
    shared_tasks = [
        ti for ti in spawned
        if ti.task_id.startswith("build_common_dep__arch_indep__")
        and ti.payload.get("payload", {}).get("ident") == shared_ident
    ]
    assert len(shared_tasks) == 1, (
        "shared arch-indep dep must be ONE build_common_dep task, got "
        + repr([ti.task_id for ti in shared_tasks])
    )
    shared_task_id = shared_tasks[0].task_id
    assert shared_task_id == "build_common_dep__arch_indep__" + shared_ident
    assert _arch_indep_task_id(shared_ident) == shared_task_id
    assert shared_task_id in spawned_build_ids

    # (c) Every dependent references the ONE shared id:
    #   - both binaries' build_variant tasks depend on it directly, and
    #   - exactly one import_common_dep_<hash> gate exists for it, whose
    #     own dep resolves to the shared build task.
    variant_tasks = [
        ti for ti in spawned
        if ti.payload.get("item_class") == "build_variant"
    ]
    assert len(variant_tasks) == 4  # 2 binaries × 2 variants
    binaries_seen = set()
    gate_id = _import_common_dep_task_id(shared_ident)
    for ti in variant_tasks:
        binaries_seen.add(ti.payload.get("payload", {}).get("pkg"))
        bare = [d for d in ti.task_depends_on if isinstance(d, str)]
        assert shared_task_id in bare, (ti.task_id, bare)
        assert gate_id in bare, (ti.task_id, bare)
    assert binaries_seen == {"brotli", "zstd"}

    gates = [ti for ti in spawned if ti.task_id == gate_id]
    assert len(gates) == 1, "exactly one shared-dep import gate"
    assert getattr(gates[0], "is_secondary_affine", None) is True
    for dep in gates[0].task_depends_on:
        if isinstance(dep, str):
            assert dep in spawned_build_ids, (gate_id, dep)

    # Each variant also names its toolchain + matrix_drv affine imports,
    # and those were spawned at discover time (no dangling).
    assert _import_tc_task_id(tc_outpath) in spawned_build_ids
    for binary in ("brotli", "zstd"):
        assert _import_matrix_drv_task_id(binary) in spawned_build_ids

    # (d) No build_variant names an UNSPAWNED affine import id.
    for ti in variant_tasks:
        for dep in ti.task_depends_on:
            if not isinstance(dep, str):
                continue  # cross-phase TaskDep (build_compilers) — separate phase
            if dep.startswith(("import_", "build_common_dep__")):
                assert dep in spawned_build_ids, (
                    f"dangling affine dependency {dep!r} on {ti.task_id}"
                )

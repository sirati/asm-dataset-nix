"""Unit tests for :mod:`compiler_suit_runner.suit_task`.

Tests for ``SuitTask`` wiring and the phase-3→4 spawn bridge. Phase 3
(``dependency_graph``) is dispatched by the framework as a task; this
module covers the ``_header_to_task_info`` conversion. The
``on_phase_end("dependency_graph")`` descriptor handoff transport was
removed pending its replacement.
"""

from __future__ import annotations

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
# on_phase_end("dependency_graph") — transport removed
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
        "toolchain_validate",
        "common_dep",
        "variant",
    }

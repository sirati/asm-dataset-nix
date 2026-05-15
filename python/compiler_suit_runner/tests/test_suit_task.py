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

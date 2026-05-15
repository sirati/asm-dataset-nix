"""Unit tests for :mod:`compiler_suit_runner.peer_paths`.

The placement-gossip layer is the **disk side** of the cluster-wide
``dict[outpath, set[secondary_id]]`` map; it sits below the watcher
(:mod:`peer_cache.PathPlacementWatcher`, exercised in
:mod:`test_peer_cache`) and the push-side fan-out (exercised in
:mod:`test_peer_push`). This file covers the per-secondary JSONL
file shape and the local-only ``record_self_has`` write path —
push effects are mocked with ``monkeypatch``.

Hermetic: no real harmonia, no real nix, no NFS. Stdlib + pytest.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner import peer_paths
from compiler_suit_runner.peer_cache import PATHS_FILE_PREFIX, PeerInfo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_fs(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "shared"
    root.mkdir()
    return root


def _peer(i: int) -> PeerInfo:
    return PeerInfo(
        secondary_id=f"sec{i}",
        hostname=f"node{i}",
        port=5000 + i,
        public_key=f"key{i}:AAAA",
    )


# ---------------------------------------------------------------------------
# paths_file_for
# ---------------------------------------------------------------------------


def test_paths_file_for_returns_expected_path(shared_fs):
    p = peer_paths.paths_file_for(shared_fs, "sec1")
    assert p == shared_fs / "peers" / f"{PATHS_FILE_PREFIX}sec1.jsonl"
    assert p.name == "_paths_sec1.jsonl"


def test_paths_file_for_creates_peers_dir(shared_fs):
    assert not (shared_fs / "peers").exists()
    _ = peer_paths.paths_file_for(shared_fs, "sec1")
    assert (shared_fs / "peers").is_dir()


# ---------------------------------------------------------------------------
# record_self_has (local-write path; push is mocked or disabled)
# ---------------------------------------------------------------------------


def test_record_self_has_appends_one_jsonl_line(shared_fs):
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/aaa-toolchain",
        drv_path="/nix/store/bbb-toolchain.drv",
        item_class=peer_paths.ITEM_CLASS_TOOLCHAIN,
    )
    target = peer_paths.paths_file_for(shared_fs, "sec1")
    raw = target.read_text(encoding="utf-8")
    # One record, newline-terminated, parses cleanly.
    lines = raw.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["secondary_id"] == "sec1"
    assert record["outpath"] == "/nix/store/aaa-toolchain"
    assert record["drv_path"] == "/nix/store/bbb-toolchain.drv"
    assert record["item_class"] == peer_paths.ITEM_CLASS_TOOLCHAIN
    assert isinstance(record.get("ts"), float)


def test_record_self_has_appends_multiple_records(shared_fs):
    for i, outpath in enumerate(("/nix/store/a", "/nix/store/b", "/nix/store/c")):
        peer_paths.record_self_has(
            shared_fs,
            my_secondary_id="sec1",
            outpath=outpath,
            drv_path=f"/nix/store/d{i}.drv",
            item_class=peer_paths.ITEM_CLASS_COMMON_DEP,
        )
    target = peer_paths.paths_file_for(shared_fs, "sec1")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    outpaths = [json.loads(line)["outpath"] for line in lines]
    assert outpaths == ["/nix/store/a", "/nix/store/b", "/nix/store/c"]


def test_record_self_has_uses_O_APPEND_atomic_write(shared_fs):
    """Verify each record-write is a single ``os.write`` (the line is
    written atomically — kernel-serialises concurrent appenders)."""
    target = peer_paths.paths_file_for(shared_fs, "sec1")
    # Seed with a partial line (no newline). A non-O_APPEND writer
    # would clobber this; O_APPEND seeks to end every write.
    with open(target, "wb") as f:
        f.write(b"partial-no-newline")
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
    )
    raw = target.read_bytes()
    # Partial pre-existing data preserved (we appended, didn't truncate).
    assert raw.startswith(b"partial-no-newline")
    # The new record's JSON line is appended after.
    assert b'"outpath": "/nix/store/aaa"' in raw
    assert raw.endswith(b"\n")


def test_record_self_has_rejects_empty_outpath(shared_fs):
    with pytest.raises(ValueError, match="outpath"):
        peer_paths.record_self_has(
            shared_fs,
            my_secondary_id="sec1",
            outpath="",
            drv_path="/nix/store/x.drv",
            item_class="toolchain",
        )


def test_record_self_has_rejects_empty_secondary_id(shared_fs):
    with pytest.raises(ValueError, match="my_secondary_id"):
        peer_paths.record_self_has(
            shared_fs,
            my_secondary_id="",
            outpath="/nix/store/aaa",
            drv_path="/nix/store/aaa.drv",
            item_class="toolchain",
        )


def test_record_self_has_skips_push_when_peers_missing(shared_fs, monkeypatch):
    """Without ``peers`` or ``our_pubkey`` the push fan-out must not run."""
    sentinel = {"called": False}

    def _spy(*_args, **_kwargs):
        sentinel["called"] = True
        return 0

    monkeypatch.setattr(peer_paths.peer_push, "fan_out_path_have", _spy)

    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
    )
    assert sentinel["called"] is False


def test_record_self_has_invokes_push_when_peers_present(
    shared_fs, monkeypatch
):
    calls: list[dict] = []

    def _spy(peers, **kwargs):
        calls.append({"peers": list(peers), **kwargs})
        return len(peers)

    monkeypatch.setattr(peer_paths.peer_push, "fan_out_path_have", _spy)

    peers = [_peer(1), _peer(2), _peer(3)]
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",  # matches peer 1 → skipped inside fan_out
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
        peers=peers,
        our_pubkey="cluster:Z",
    )
    assert len(calls) == 1
    invoked = calls[0]
    # Self filtered out of the peer list before fan-out.
    invoked_ids = {p.secondary_id for p in invoked["peers"]}
    assert invoked_ids == {"sec2", "sec3"}
    assert invoked["my_secondary_id"] == "sec1"
    assert invoked["outpath"] == "/nix/store/aaa"
    assert invoked["our_pubkey"] == "cluster:Z"


def test_record_self_has_skips_push_when_only_self_in_peers(
    shared_fs, monkeypatch
):
    calls: list[int] = []

    def _spy(*_args, **_kwargs):
        calls.append(1)
        return 0

    monkeypatch.setattr(peer_paths.peer_push, "fan_out_path_have", _spy)

    me = _peer(1)
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id=me.secondary_id,
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
        peers=[me],
        our_pubkey="pk",
    )
    # No reachable peers (self filtered) → no broadcast at all.
    assert calls == []


def test_record_self_has_swallows_push_failure(shared_fs, monkeypatch):
    """File write is the source of truth; a flaky push must not propagate."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("upstream is on fire")

    monkeypatch.setattr(peer_paths.peer_push, "fan_out_path_have", _boom)

    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/aaa",
        drv_path="/nix/store/aaa.drv",
        item_class="toolchain",
        peers=[_peer(2)],
        our_pubkey="pk",
    )
    target = peer_paths.paths_file_for(shared_fs, "sec1")
    # The local record still landed despite the push blow-up.
    assert target.exists()
    assert len(target.read_text().splitlines()) == 1


# ---------------------------------------------------------------------------
# list_self_placements
# ---------------------------------------------------------------------------


def test_list_self_placements_round_trip(shared_fs):
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/a",
        drv_path="/nix/store/a.drv",
        item_class="toolchain",
    )
    peer_paths.record_self_has(
        shared_fs,
        my_secondary_id="sec1",
        outpath="/nix/store/b",
        drv_path="/nix/store/b.drv",
        item_class="common_dep",
    )
    placements = peer_paths.list_self_placements(shared_fs, "sec1")
    by_outpath = {p.outpath: p for p in placements}
    assert set(by_outpath) == {"/nix/store/a", "/nix/store/b"}
    assert by_outpath["/nix/store/a"].item_class == "toolchain"
    assert by_outpath["/nix/store/b"].item_class == "common_dep"
    assert by_outpath["/nix/store/a"].drv_path == "/nix/store/a.drv"
    assert all(p.secondary_id == "sec1" for p in placements)


def test_list_self_placements_missing_file_returns_empty(shared_fs):
    placements = peer_paths.list_self_placements(shared_fs, "no-such-sec")
    assert placements == []


def test_list_self_placements_tolerates_bad_lines(shared_fs):
    target = peer_paths.paths_file_for(shared_fs, "sec1")
    target.write_text(
        "\n".join(
            [
                "",  # blank
                "not json at all",
                json.dumps({"secondary_id": "sec1", "outpath": "/nix/store/a"}),
                json.dumps([1, 2]),  # not an object
                json.dumps({"outpath": "/nix/store/b"}),  # missing sid
                json.dumps({"secondary_id": "sec1"}),  # missing outpath
                json.dumps(
                    {"secondary_id": "sec1", "outpath": "/nix/store/c"}
                ),
            ]
        )
        + "\n"
    )
    out = peer_paths.list_self_placements(shared_fs, "sec1")
    outpaths = sorted(p.outpath for p in out)
    # Only the two well-formed lines survive parsing.
    assert outpaths == ["/nix/store/a", "/nix/store/c"]

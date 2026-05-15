"""Unit tests for :mod:`compiler_suit_runner.peer_paths_fetch`.

Targeted ``nix copy --from <peer>`` is the runtime side of the
placement-map flow. These tests verify:

* candidate ordering (``prefer`` → ``primary`` synthetic id →
  deterministic shuffle),
* argv shape (``--no-check-sigs``, only one ``--from``,
  the path-info short-circuit). ``nix copy --from`` already pins
  the source, so ``--no-substituters`` is NOT in the argv — it
  is invalid for the ``copy`` subcommand and would yield
  ``error: unrecognised flag``.
* retry-on-failure semantics across candidates,
* fall-through behaviour when the placement map has no entry for the
  requested outpath (let nix's native substituters handle it).

Hermetic: ``run_subprocess`` is replaced with a recording stub; no real
``nix`` is invoked.
"""

from __future__ import annotations

from typing import Optional

import pytest

from compiler_suit_runner import peer_paths_fetch
from compiler_suit_runner.peer_cache import PeerInfo
from compiler_suit_runner.peer_paths_fetch import (
    PRIMARY_CANDIDATE_ID,
    fetch_from_peer,
    is_path_locally_valid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _peer(i: int, *, sid: Optional[str] = None) -> PeerInfo:
    return PeerInfo(
        secondary_id=sid or f"sec{i}",
        hostname=f"node{i}",
        port=5000 + i,
        public_key=f"key{i}:AAAA",
    )


class _Runner:
    """Programmable run_subprocess stub.

    ``script`` is a list of ``(stdout, stderr, rc)`` tuples consumed in
    order. ``calls`` records the argv of every invocation so assertions
    can verify the exact wire shape (no shell, no extra flags).
    """

    def __init__(self, script: list[tuple[bytes, bytes, int]]) -> None:
        self._script = list(script)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[bytes, bytes, int]:
        self.calls.append(list(argv))
        if not self._script:
            raise AssertionError(
                f"runner script exhausted at call #{len(self.calls)}: "
                f"argv={argv}"
            )
        return self._script.pop(0)


def _path_info_argv(out: str) -> list[str]:
    return [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "path-info",
        out,
    ]


def _copy_argv(url: str, out: str) -> list[str]:
    return [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "copy",
        "--from", url,
        "--no-check-sigs",
        out,
    ]


# ---------------------------------------------------------------------------
# is_path_locally_valid
# ---------------------------------------------------------------------------


def test_is_path_locally_valid_returns_true_on_zero_rc():
    runner = _Runner([(b"/nix/store/a\n", b"", 0)])
    assert is_path_locally_valid("/nix/store/a", run_subprocess=runner) is True
    assert runner.calls == [_path_info_argv("/nix/store/a")]


def test_is_path_locally_valid_returns_false_on_nonzero_rc():
    runner = _Runner([(b"", b"missing\n", 1)])
    assert is_path_locally_valid("/nix/store/a", run_subprocess=runner) is False


# ---------------------------------------------------------------------------
# fetch_from_peer
# ---------------------------------------------------------------------------


def test_fetch_short_circuits_when_path_locally_valid():
    """If we already have it, no nix copy is issued; returns None (not failure)."""
    runner = _Runner([(b"", b"", 0)])  # path-info success
    placements = {"/nix/store/aaa": {"sec1"}}
    peers = [_peer(1)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result is None
    # Only the path-info probe ran.
    assert len(runner.calls) == 1
    assert runner.calls[0][3] == "path-info"


def test_fetch_returns_none_when_no_candidate_in_map():
    """Empty candidate set -> None (caller falls back to nix substituters)."""
    runner = _Runner([(b"", b"", 1)])  # path-info: not local
    placements: dict[str, set[str]] = {}
    peers = [_peer(1), _peer(2)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result is None
    # Only the path-info probe; no nix copy.
    assert len(runner.calls) == 1


def test_fetch_emits_correct_argv_for_targeted_copy():
    """The wire shape MUST include ``--no-check-sigs`` and exactly
    one ``--from`` URL — that's the whole point of the targeted-fetch
    design. ``--no-substituters`` is NOT in the argv (it's invalid
    for ``nix copy``; the ``--from`` pin already restricts the
    source to one URL)."""
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info: not local
            (b"", b"", 0),  # nix copy success
        ]
    )
    placements = {"/nix/store/aaa": {"sec1"}}
    peers = [_peer(1)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result == "sec1"
    assert len(runner.calls) == 2
    copy_argv = runner.calls[1]
    assert copy_argv == _copy_argv(
        "http://node1:5001", "/nix/store/aaa"
    )
    # Defensive: only one ``--from``.
    assert copy_argv.count("--from") == 1


def test_fetch_retries_next_candidate_on_first_failure():
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info: not local
            (b"", b"truncated NAR\n", 1),  # sec1 fails
            (b"", b"", 0),  # sec2 succeeds
        ]
    )
    placements = {"/nix/store/aaa": {"sec1", "sec2"}}
    peers = [_peer(1), _peer(2)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers,
        prefer="sec1",  # force order: sec1 first
        run_subprocess=runner,
    )
    assert result == "sec2"
    copy_calls = [c for c in runner.calls if "copy" in c]
    assert len(copy_calls) == 2
    assert "http://node1:5001" in copy_calls[0]
    assert "http://node2:5002" in copy_calls[1]


def test_fetch_returns_none_when_all_candidates_fail():
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info
            (b"", b"e1\n", 1),  # sec1 fail
            (b"", b"e2\n", 1),  # sec2 fail
        ]
    )
    placements = {"/nix/store/aaa": {"sec1", "sec2"}}
    peers = [_peer(1), _peer(2)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers,
        prefer="sec1",
        run_subprocess=runner,
    )
    assert result is None


def test_fetch_skips_local_check_when_check_local_false():
    runner = _Runner([(b"", b"", 0)])  # nix copy success
    placements = {"/nix/store/aaa": {"sec1"}}
    peers = [_peer(1)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers,
        run_subprocess=runner,
        check_local=False,
    )
    assert result == "sec1"
    # No path-info call at all.
    assert all("path-info" not in c for c in runner.calls)


# ---------------------------------------------------------------------------
# Candidate ordering
# ---------------------------------------------------------------------------


def test_prefer_wins_over_primary_and_others(monkeypatch):
    """``prefer`` is consulted first; the synthetic ``submitter`` id is
    second; remaining candidates come last in shuffle order."""
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info
            (b"", b"", 0),  # first peer succeeds; we check which one
        ]
    )
    placements = {
        "/nix/store/aaa": {PRIMARY_CANDIDATE_ID, "sec1", "sec2", "sec3"},
    }
    peers = [
        PeerInfo(
            secondary_id=PRIMARY_CANDIDATE_ID,
            hostname="primary",
            port=5099,
            public_key="p:Z",
        ),
        _peer(1),
        _peer(2),
        _peer(3),
    ]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers,
        prefer="sec2",
        run_subprocess=runner,
    )
    assert result == "sec2"
    copy_argv = runner.calls[1]
    assert "--from" in copy_argv
    assert "http://node2:5002" in copy_argv


def test_primary_wins_when_no_prefer_set():
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info
            (b"", b"", 0),  # nix copy success
        ]
    )
    placements = {
        "/nix/store/aaa": {PRIMARY_CANDIDATE_ID, "sec1", "sec2", "sec3"},
    }
    peers = [
        PeerInfo(
            secondary_id=PRIMARY_CANDIDATE_ID,
            hostname="primary",
            port=5099,
            public_key="p:Z",
        ),
        _peer(1),
        _peer(2),
        _peer(3),
    ]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result == PRIMARY_CANDIDATE_ID
    copy_argv = runner.calls[1]
    assert "http://primary:5099" in copy_argv


def test_shuffle_is_deterministic_per_outpath():
    """Two callers fetching the same outpath must agree on candidate
    order, so the cluster doesn't accidentally concentrate fetches
    on one peer when N secondaries race the same cache miss."""
    placements = {"/nix/store/x": {"sec1", "sec2", "sec3"}}
    peers = [_peer(1), _peer(2), _peer(3)]
    order_a = peer_paths_fetch._order_candidates(
        placements["/nix/store/x"], peers, prefer=None, outpath="/nix/store/x",
    )
    order_b = peer_paths_fetch._order_candidates(
        placements["/nix/store/x"], peers, prefer=None, outpath="/nix/store/x",
    )
    assert [p.secondary_id for p in order_a] == [
        p.secondary_id for p in order_b
    ]
    # Different outpath -> different (deterministic) order is allowed:
    # we just verify both runs of the same outpath agree.


def test_shuffle_differs_across_outpaths():
    """Per-outpath SHA-1 keying spreads load across the cluster: two
    different outpaths should usually hash to different orders.
    Guard against a degenerate hash collision with three candidates."""
    peers = [_peer(1), _peer(2), _peer(3)]
    cand = {"sec1", "sec2", "sec3"}
    orders = set()
    for op in ("/nix/store/a", "/nix/store/b", "/nix/store/c", "/nix/store/d"):
        order = peer_paths_fetch._order_candidates(
            cand, peers, prefer=None, outpath=op,
        )
        orders.add(tuple(p.secondary_id for p in order))
    # At least two distinct orderings across four outpaths.
    assert len(orders) >= 2


def test_unresolvable_candidate_id_is_silently_skipped():
    """A placement record references a sid that's not currently in the
    peer list (peer dropped between read and fetch). Skip it; don't
    fail the fetch."""
    runner = _Runner(
        [
            (b"", b"", 1),  # path-info
            (b"", b"", 0),  # nix copy from sec2 succeeds
        ]
    )
    placements = {"/nix/store/aaa": {"phantom-sid", "sec2"}}
    peers = [_peer(2)]  # phantom-sid not present
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result == "sec2"


def test_all_unresolvable_returns_none():
    runner = _Runner([(b"", b"", 1)])  # only path-info; no copy issued
    placements = {"/nix/store/aaa": {"phantom-1", "phantom-2"}}
    peers = [_peer(1)]
    result = fetch_from_peer(
        "/nix/store/aaa", placements, peers, run_subprocess=runner,
    )
    assert result is None

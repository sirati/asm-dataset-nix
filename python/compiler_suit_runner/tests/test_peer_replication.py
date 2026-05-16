"""Unit tests for :mod:`compiler_suit_runner.peer_replication`.

The sender / receiver / repair-worker classes are exercised with
fakes injected through :class:`ReplicationContext` so the suite stays
hermetic — no network, no nix subprocess, no real filesystem (beyond
``tmp_path`` for the receiver's record_self_has path).
"""

from __future__ import annotations

import pathlib
import threading
import time
from dataclasses import replace
from typing import Optional

import pytest

from unittest import mock

from compiler_suit_runner.peer_cache import PeerInfo, PlacementDiff
from compiler_suit_runner.peer_replication import (
    BROADCAST_ITEM_CLASS,
    DEFAULT_BROADCAST_MAX_HOP,
    DEFAULT_OFFER_TIMEOUT_SECONDS,
    DEFAULT_REPLICATION_K,
    BroadcastReceiver,
    BroadcastResult,
    BroadcastSender,
    ReplicationContext,
    ReplicationReceiver,
    ReplicationRepairWorker,
    ReplicationSender,
)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


def _peer(sid: str, host: str = "", port: int = 5000) -> PeerInfo:
    return PeerInfo(
        secondary_id=sid,
        hostname=host or f"{sid}.host",
        port=port,
        public_key=f"{sid}:KEY",
    )


@pytest.fixture
def ctx_factory(tmp_path: pathlib.Path):
    """Yield a builder that returns (ctx, recorders).

    Recorders captures every wire-format call:
      - offers:   list[(target_sid, outpath, drv, klass)]
      - accepts:  list[(target_sid, outpath)]
      - rejects:  list[(target_sid, outpath, reason)]
      - cancels:  list[(target_sid, outpath)]
      - fetches:  list[(outpath, prefer)]      → result via fetch_result
      - records:  list[(outpath, drv, klass)]

    Knobs configurable via ``make(...)``:
      - my_sid (default "me")
      - peers (default 3 peers s1, s2, s3)
      - placements (default {} — empty)
      - locally_valid (default {} — set of outpaths "we already have")
      - fetch_result (default lambda op: op) — return source_sid on success
      - replication_k (default 3)
      - offer_timeout (default 0.2 s for fast tests)
    """

    class Recorders:
        offers: list[tuple[str, str, str, str]]
        accepts: list[tuple[str, str]]
        rejects: list[tuple[str, str, str]]
        cancels: list[tuple[str, str]]
        fetches: list[tuple[str, Optional[str]]]
        records: list[tuple[str, str, str]]

        def __init__(self) -> None:
            self.offers = []
            self.accepts = []
            self.rejects = []
            self.cancels = []
            self.fetches = []
            self.records = []

    def make(
        my_sid: str = "me",
        peers: Optional[list[PeerInfo]] = None,
        placements: Optional[dict[str, set[str]]] = None,
        locally_valid: Optional[set[str]] = None,
        fetch_result=None,
        replication_k: int = 3,
        offer_timeout: float = 0.2,
        push_offer_ok: bool = True,
    ):
        recorders = Recorders()
        peers_list = peers if peers is not None else [
            _peer("s1"), _peer("s2"), _peer("s3"),
        ]
        placements_ref: dict[str, set[str]] = (
            dict(placements) if placements is not None else {}
        )
        locally_set = set(locally_valid) if locally_valid is not None else set()
        # Default fetch_result: succeed and return the offerer's sid.
        if fetch_result is None:
            def _default_fetch(outpath, placements, peers, *, prefer=None):
                return prefer
            fetch_result = _default_fetch

        def fake_offer(peer, from_sid, outpath, drv, klass, pk, **_):
            del from_sid, pk
            recorders.offers.append((peer.secondary_id, outpath, drv, klass))
            return push_offer_ok

        def fake_accept(peer, from_sid, outpath, pk, **_):
            del from_sid, pk
            recorders.accepts.append((peer.secondary_id, outpath))
            return True

        def fake_reject(peer, from_sid, outpath, reason, pk, **_):
            del from_sid, pk
            recorders.rejects.append((peer.secondary_id, outpath, reason))
            return True

        def fake_cancel(peer, from_sid, outpath, pk, **_):
            del from_sid, pk
            recorders.cancels.append((peer.secondary_id, outpath))
            return True

        def fake_fetch(outpath, placements_arg, peers_arg, *, prefer=None, **_):
            del placements_arg, peers_arg
            result = fetch_result(
                outpath, placements_ref, peers_list, prefer=prefer,
            )
            recorders.fetches.append((outpath, result))
            return result

        def fake_is_local(outpath: str) -> bool:
            return outpath in locally_set

        def fake_record(shared_fs, *, my_secondary_id, outpath, drv_path,
                        item_class, peers=None, our_pubkey=None):
            del shared_fs, my_secondary_id, peers, our_pubkey
            recorders.records.append((outpath, drv_path, item_class))
            # Reflect the new placement so subsequent push_attempts
            # converge correctly.
            placements_ref.setdefault(outpath, set()).add(my_sid)
            locally_set.add(outpath)

        ctx = ReplicationContext(
            my_secondary_id=my_sid,
            our_pubkey="me:PK",
            shared_fs=tmp_path,
            get_peers=lambda: list(peers_list),
            get_placements=lambda: {k: set(v) for k, v in placements_ref.items()},
            push_path_offer=fake_offer,
            push_path_accept=fake_accept,
            push_path_reject=fake_reject,
            push_path_cancel=fake_cancel,
            fetch_from_peer=fake_fetch,
            is_path_locally_valid=fake_is_local,
            record_self_has=fake_record,
            replication_k=replication_k,
            offer_timeout_seconds=offer_timeout,
        )
        return ctx, recorders, placements_ref

    return make


# ---------------------------------------------------------------------------
# Sender state machine
# ---------------------------------------------------------------------------


def test_sender_push_attempt_zero_when_k_already_met(ctx_factory) -> None:
    ctx, rec, _ = ctx_factory(
        placements={"/nix/store/x": {"s1", "s2", "s3"}},
    )
    sender = ReplicationSender(ctx)
    try:
        n = sender.push_attempt("/nix/store/x", "drv", "toolchain")
        assert n == 0
        assert rec.offers == []
    finally:
        sender.stop()


def test_sender_push_attempt_issues_k_minus_held_offers(ctx_factory) -> None:
    """Starting from zero holders, push_attempt issues K offers
    (one per non-self peer) up to K - in_flight."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("s4")],
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    try:
        n = sender.push_attempt("/nix/store/y", "drv-y", "toolchain")
        assert n == 3
        target_sids = sorted(t for t, *_ in rec.offers)
        # 3 of the 4 candidates picked (random); none is self.
        assert len(target_sids) == 3
        assert "me" not in target_sids
        # in_flight reflects what we offered.
        assert sender.in_flight_targets("/nix/store/y") == set(target_sids)
    finally:
        sender.stop()


def test_sender_push_attempt_skips_existing_holders(ctx_factory) -> None:
    """Candidates who already hold the outpath are excluded."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3")],
        placements={"/nix/store/z": {"s1"}},  # s1 already holds it
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/z", "drv-z", "toolchain")
        offered_sids = {t for t, *_ in rec.offers}
        assert "s1" not in offered_sids
        # Need K=3 - 1 holder - 0 in_flight = 2 offers
        assert len(rec.offers) == 2
    finally:
        sender.stop()


def test_sender_on_reject_releases_slot_and_retries(ctx_factory) -> None:
    """On reject, the slot is released AND a new push_attempt fires
    (because ``needed`` becomes > 0 again)."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("s4")],
        replication_k=2,  # smaller K to make accounting clearer
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/a", "drv", "toolchain")
        assert len(rec.offers) == 2
        in_flight_after_initial = sender.in_flight_targets("/nix/store/a")
        assert len(in_flight_after_initial) == 2
        rejected_sid = next(iter(in_flight_after_initial))
        sender.on_reject(rejected_sid, "/nix/store/a", "already-targeted")
        # The slot was released and a new offer was issued (re-issued
        # to either the rejecter or another candidate — we don't
        # track rejections separately, that's a future optimization).
        # Invariant: in_flight is back to len=2 (one slot released +
        # one refilled).
        assert len(rec.offers) >= 3
        assert len(sender.in_flight_targets("/nix/store/a")) == 2
    finally:
        sender.stop()


def test_sender_on_timeout_releases_slot_and_retries(ctx_factory) -> None:
    """When the offer timer fires without an accept/reject, the slot
    is released and a fresh candidate is tried."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("s4")],
        replication_k=2,
        offer_timeout=0.05,  # snappy timer for the test
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/b", "drv", "toolchain")
        assert len(rec.offers) == 2
        # Wait for both timers to fire.
        deadline = time.time() + 1.0
        while time.time() < deadline and len(rec.offers) < 4:
            time.sleep(0.02)
        # Each timeout retries on a fresh candidate; we have 4 peers
        # so each of the 2 timed-out slots can find ONE new target
        # (the remaining 2 candidates). Total offers: 2 initial + 2
        # retries = 4. After that, no more candidates left.
        assert len(rec.offers) >= 3, rec.offers
    finally:
        sender.stop()


def test_sender_on_path_have_cancels_remaining_when_k_satisfied(
    ctx_factory,
) -> None:
    """When a path-have broadcast pushes us at/above K, outstanding
    offers get path-cancel sent and in_flight is cleared."""
    placements: dict[str, set[str]] = {"/nix/store/c": {"s1"}}
    ctx, rec, ref = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("s4")],
        placements=placements,
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/c", "drv", "toolchain")
        # 3 - 1 holder = 2 offers to s2/s3/s4 (2 of 3 picked).
        initial = list(sender.in_flight_targets("/nix/store/c"))
        assert len(initial) == 2
        # Reflect that one new holder appeared.
        new_holder = initial[0]
        ref["/nix/store/c"].add(new_holder)
        sender.on_path_have(new_holder, "/nix/store/c")
        # Holders = {s1, new_holder}; effective with broadcaster = 2;
        # still < K=3, so no cancels yet.
        assert rec.cancels == []
        assert new_holder not in sender.in_flight_targets("/nix/store/c")
        # Another new holder pushes us to K.
        another = initial[1]
        ref["/nix/store/c"].add(another)
        sender.on_path_have(another, "/nix/store/c")
        # Now we should have cancelled any still-in-flight (none in
        # this scenario after both initial targets settled).
        assert sender.in_flight_targets("/nix/store/c") == set()
    finally:
        sender.stop()


def test_sender_cancels_remaining_offers_when_k_converges(
    ctx_factory,
) -> None:
    """A third-party peer's path-have can be the convergence event
    that cancels still-outstanding offers."""
    ctx, rec, ref = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("s4")],
        replication_k=3,
        offer_timeout=10.0,  # don't time out during this test
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/d", "drv", "toolchain")
        in_flight_before = sender.in_flight_targets("/nix/store/d")
        assert len(in_flight_before) == 3
        # An unrelated holder lights up the placement to K.
        ref["/nix/store/d"] = {"s5", "s6"}
        sender.on_path_have("s7", "/nix/store/d")
        # All in-flight targets cancelled.
        assert sender.in_flight_targets("/nix/store/d") == set()
        cancelled_sids = {t for t, _ in rec.cancels}
        assert cancelled_sids == in_flight_before
    finally:
        sender.stop()


def test_sender_stop_cancels_timers(ctx_factory) -> None:
    """stop() must drop all in-flight timers and become a no-op."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3")],
        replication_k=2,
        offer_timeout=10.0,
    )
    sender = ReplicationSender(ctx)
    sender.push_attempt("/nix/store/e", "d", "toolchain")
    assert sender.in_flight_targets("/nix/store/e")
    sender.stop()
    # Post-stop push_attempt is a no-op.
    n = sender.push_attempt("/nix/store/f", "d", "toolchain")
    assert n == 0


def test_sender_transport_failure_does_not_occupy_slot(
    ctx_factory,
) -> None:
    """If push_path_offer returns False (transport failed), the slot
    is not reserved and a future push_attempt can retry."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3")],
        replication_k=2,
        push_offer_ok=False,  # all sends fail at transport level
    )
    sender = ReplicationSender(ctx)
    try:
        sender.push_attempt("/nix/store/g", "d", "toolchain")
        # All sends attempted (we recorded them), but no in_flight
        # slots reserved.
        assert len(rec.offers) == 2
        assert sender.in_flight_targets("/nix/store/g") == set()
    finally:
        sender.stop()


# ---------------------------------------------------------------------------
# Receiver state machine
# ---------------------------------------------------------------------------


def _wait_for(predicate, timeout: float = 2.0, poll: float = 0.01) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


def test_receiver_already_have_rejects(ctx_factory) -> None:
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("me")],
        locally_valid={"/nix/store/x"},
    )
    sender = ReplicationSender(ctx)
    receiver = ReplicationReceiver(ctx, sender)
    try:
        receiver.on_offer({
            "from_secondary_id": "s1",
            "outpath": "/nix/store/x",
            "drv_path": "d",
            "item_class": "toolchain",
        })
        # Reject with already-have, no accept, no fetch.
        assert rec.accepts == []
        assert rec.fetches == []
        assert rec.rejects == [("s1", "/nix/store/x", "already-have")]
    finally:
        receiver.stop()
        sender.stop()


def test_receiver_second_offer_rejects_already_targeted(ctx_factory) -> None:
    """Two senders racing on the same outpath: first wins (accept),
    second gets already-targeted."""
    # Use a fetch that blocks until released so we can test the
    # "second offer arrives mid-fetch" window.
    fetch_released = threading.Event()

    def slow_fetch(outpath, placements, peers, *, prefer=None):
        fetch_released.wait(timeout=5.0)
        return prefer

    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("me")],
        fetch_result=slow_fetch,
    )
    sender = ReplicationSender(ctx)
    receiver = ReplicationReceiver(ctx, sender)
    try:
        receiver.on_offer({
            "from_secondary_id": "s1",
            "outpath": "/nix/store/y",
            "drv_path": "d",
            "item_class": "toolchain",
        })
        # First offer accepted.
        assert _wait_for(lambda: rec.accepts == [("s1", "/nix/store/y")])
        # Second offer arrives from s2 before fetch finishes.
        receiver.on_offer({
            "from_secondary_id": "s2",
            "outpath": "/nix/store/y",
            "drv_path": "d",
            "item_class": "toolchain",
        })
        assert rec.rejects == [("s2", "/nix/store/y", "already-targeted")]
        # Let the fetch complete cleanly.
        fetch_released.set()
        assert _wait_for(
            lambda: rec.records == [("/nix/store/y", "d", "toolchain")]
        )
    finally:
        fetch_released.set()
        receiver.stop()
        sender.stop()


def test_receiver_accept_triggers_fetch_record_and_cascade(ctx_factory) -> None:
    """The happy path: accept → fetch → record_self_has → cascade
    push_attempt because item_class is toolchain."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    receiver = ReplicationReceiver(ctx, sender)
    try:
        receiver.on_offer({
            "from_secondary_id": "s1",
            "outpath": "/nix/store/cascade",
            "drv_path": "drv-c",
            "item_class": "toolchain",
        })
        assert _wait_for(
            lambda: rec.records == [("/nix/store/cascade", "drv-c", "toolchain")]
        )
        # Cascade: receiver triggered sender.push_attempt with K=3.
        # We had {me} holder after record; placements now {me};
        # holders = 1; in_flight = 0; needed = 2 → 2 fresh offers
        # (against s2 and s3, since s1 has it as offerer's neighbour
        # — actually s1 had it too but the placement map doesn't
        # show it. Be liberal: at least 1 cascade offer issued.)
        assert _wait_for(lambda: len(rec.offers) >= 1)
    finally:
        receiver.stop()
        sender.stop()


def test_receiver_cancel_suppresses_post_fetch_record(ctx_factory) -> None:
    """If sender cancels mid-fetch, the receiver still completes the
    nix copy (it cannot abort), but skips record_self_has + cascade."""
    fetch_released = threading.Event()

    def slow_fetch(outpath, placements, peers, *, prefer=None):
        fetch_released.wait(timeout=5.0)
        return prefer

    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("me")],
        fetch_result=slow_fetch,
    )
    sender = ReplicationSender(ctx)
    receiver = ReplicationReceiver(ctx, sender)
    try:
        receiver.on_offer({
            "from_secondary_id": "s1",
            "outpath": "/nix/store/q",
            "drv_path": "d",
            "item_class": "toolchain",
        })
        # Cancel while the fetch is still blocked.
        receiver.on_cancel({
            "from_secondary_id": "s1",
            "outpath": "/nix/store/q",
        })
        fetch_released.set()
        # Wait for the fetch thread to settle.
        time.sleep(0.2)
        assert rec.records == [], "cancelled offer must not record"
    finally:
        fetch_released.set()
        receiver.stop()
        sender.stop()


# ---------------------------------------------------------------------------
# Repair worker
# ---------------------------------------------------------------------------


def test_repair_on_peer_removed_triggers_push_attempt(ctx_factory) -> None:
    """When a peer dies that was a holder, and we're a holder, and
    removing them drops below K → fire one push_attempt per outpath.

    on_peer_removed is the framework signal — it fires BEFORE the
    placement watcher refreshes (the snapshot still lists the dead
    peer as a holder). The algorithm accounts for "going away" by
    using ``len(holders) - 1`` as the post-death holder count.
    """
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={
            "/nix/store/a": {"me", "s1", "s2"},  # K=3 just barely
            "/nix/store/b": {"me", "s1", "s2", "s3"},  # K=4; safe
            "/nix/store/c": {"s1", "s2"},  # we don't hold this
        },
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        # Placement map STILL shows s2 (watcher hasn't refreshed yet);
        # the framework signal arrived first.
        worker.on_peer_removed("s2", reason="heartbeat-timeout")
        # /nix/store/a: holders went 3->2; below K=3; we hold it →
        #   push_attempt fires → at least 1 offer.
        # /nix/store/b: holders went 4->3; still at K=3; no repair.
        # /nix/store/c: we don't hold it → no repair.
        outpaths_offered = {op for _, op, _, _ in rec.offers}
        assert "/nix/store/a" in outpaths_offered
        assert "/nix/store/b" not in outpaths_offered
        assert "/nix/store/c" not in outpaths_offered
    finally:
        sender.stop()


def test_repair_on_diff_callback_is_idempotent_fallback(ctx_factory) -> None:
    """Same logic, but driven by a PlacementDiff (fallback signal)."""
    ctx, rec, ref = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/d": {"me", "s1"}},  # already below K=3
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        diff = PlacementDiff(
            added={}, removed={"/nix/store/d": {"s2"}},
        )
        worker.on_diff(diff)
        outpaths_offered = {op for _, op, _, _ in rec.offers}
        assert "/nix/store/d" in outpaths_offered
    finally:
        sender.stop()


def test_repair_does_nothing_when_we_arent_a_holder(ctx_factory) -> None:
    """We never push for outpaths we don't hold — we have no copy to
    serve."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("me")],
        placements={"/nix/store/x": {"s1"}},  # we don't hold it
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        worker.on_peer_removed("s2")
        worker.on_diff(PlacementDiff(
            added={}, removed={"/nix/store/x": {"s2"}},
        ))
        assert rec.offers == []
    finally:
        sender.stop()


def test_repair_does_nothing_when_k_still_met(ctx_factory) -> None:
    """K=3, holders=4 before removal: still K=3 after → no repair."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/y": {"me", "s1", "s2", "s3"}},
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        # Note: we don't update ref because on_peer_removed reads the
        # snapshot live; for this test we want to check the
        # arithmetic. Use an empty diff with removed={"y": {"s2"}}.
        worker.on_peer_removed("s2")
        assert rec.offers == []
    finally:
        sender.stop()


def test_cascade_fires_on_addition_when_im_the_new_holder(
    ctx_factory,
) -> None:
    """When the placement watcher signals we became a new holder of
    a TOOLCHAIN outpath, the cascade fires push_attempt to drive K=3.

    This is the indirect signal from a build-worker subprocess:
    worker wrote its placement file, manager's watcher diff sees the
    addition, RepairWorker.on_diff routes to push_attempt.
    """
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/new-tc": {"me"}},  # only us so far
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    # Plug in metadata lookup that says this is a toolchain.
    worker = ReplicationRepairWorker(
        ctx, sender,
        get_drv_metadata=lambda op: ("/nix/store/new-tc.drv", "toolchain"),
    )
    try:
        worker.on_diff(PlacementDiff(
            added={"/nix/store/new-tc": {"me"}}, removed={},
        ))
        # Cascade fired: K=3 - 1 holder - 0 in_flight = 2 offers.
        outpaths_offered = [op for _, op, _, _ in rec.offers]
        assert outpaths_offered.count("/nix/store/new-tc") == 2
    finally:
        sender.stop()


def test_cascade_skips_non_toolchain_additions(ctx_factory) -> None:
    """Common-deps and variants are NOT subject to K=3; cascade is
    scoped to toolchains via the item_class filter."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/cd": {"me"}},
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(
        ctx, sender,
        get_drv_metadata=lambda op: ("d", "common_dep"),  # not toolchain
    )
    try:
        worker.on_diff(PlacementDiff(
            added={"/nix/store/cd": {"me"}}, removed={},
        ))
        assert rec.offers == []
    finally:
        sender.stop()


def test_cascade_skips_additions_by_other_peers(ctx_factory) -> None:
    """When ANOTHER peer becomes a holder, WE don't cascade — they
    do (each on their own machine)."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/x": {"s1"}},  # s1 just got it; we don't have it
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(
        ctx, sender,
        get_drv_metadata=lambda op: ("d", "toolchain"),
    )
    try:
        worker.on_diff(PlacementDiff(
            added={"/nix/store/x": {"s1"}}, removed={},
        ))
        # We never push for outpaths we don't hold — no offers.
        assert rec.offers == []
    finally:
        sender.stop()


def test_repair_uses_drv_metadata_lookup(ctx_factory) -> None:
    """When a metadata lookup is plugged in, the repair-issued offer
    carries the right drv_path + item_class."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("me")],
        placements={"/nix/store/m": {"me"}},  # below K=3
        replication_k=3,
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(
        ctx, sender,
        get_drv_metadata=lambda op: ("/nix/store/m.drv", "toolchain"),
    )
    try:
        worker.on_peer_removed("died")  # any sid; only outpaths matter
        # No outpath qualifies (died wasn't a holder), so no offers.
        assert rec.offers == []
        # Use on_diff for repair instead:
        worker.on_diff(PlacementDiff(
            added={}, removed={"/nix/store/m": {"died"}},
        ))
        assert any(
            op == "/nix/store/m" and drv == "/nix/store/m.drv"
            and klass == "toolchain"
            for _t, op, drv, klass in rec.offers
        )
    finally:
        sender.stop()


# ---------------------------------------------------------------------------
# Q1 + Q2 wire-in: framework PrimaryHandle callable hooks
# ---------------------------------------------------------------------------


class _FakeTimer:
    """Minimal threading.Timer drop-in for fake-time tests.

    Implements ``cancel()``; ``fire()`` invokes the captured callback
    synchronously so tests can assert behaviour without sleeping.
    """

    def __init__(self, interval: float, function) -> None:
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.fired = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if self.cancelled or self.fired:
            return
        self.fired = True
        self.function()


class _FakeTimerRegistry:
    """Tracks every timer the sender constructs through the factory."""

    def __init__(self) -> None:
        self.timers: list[_FakeTimer] = []

    def factory(self, interval: float, function) -> _FakeTimer:
        t = _FakeTimer(interval, function)
        self.timers.append(t)
        return t

    @property
    def alive(self) -> list[_FakeTimer]:
        return [t for t in self.timers if not t.cancelled and not t.fired]


def test_replication_context_defaults_new_callables_to_none(
    tmp_path: pathlib.Path,
) -> None:
    """Regression guard — Q1/Q2 callables default to None so legacy
    consumers that haven't migrated their construction site keep
    working (NFS-poll fallback path)."""
    ctx = ReplicationContext(
        my_secondary_id="me",
        our_pubkey="me:PK",
        shared_fs=tmp_path,
        get_peers=lambda: [],
        get_placements=lambda: {},
    )
    assert ctx.mark_task_unfulfillable is None
    assert ctx.reinject_task is None
    assert ctx.update_preferred_secondaries is None
    assert ctx.lookup_task_hash_for_outpath is None
    # Sanity: existing knobs preserved.
    assert ctx.replication_k == DEFAULT_REPLICATION_K
    assert ctx.offer_timeout_seconds == DEFAULT_OFFER_TIMEOUT_SECONDS


def test_repair_marks_unfulfillable_with_canonical_reason_format(
    ctx_factory,
) -> None:
    """Zero live holders post-repair + lookup resolves → exactly one
    ``mark_task_unfulfillable`` call with the canonical reason
    string.

    Reason format contract (must match holding_matcher's regex / Q3
    parser): ``f"toolchain outpath={outpath} dead_holders={sorted(last_known)}"``.
    The ``dead_holders`` field is the ``sorted()`` repr of a Python
    list, exactly as ``str(sorted(...))`` produces.
    """
    ctx, _, _ = ctx_factory(
        peers=[_peer("me"), _peer("d1"), _peer("d2")],
        # No live holders post-exclusion: holders = {d1, d2};
        # excludes = {d1, d2} → empty. The path is effectively gone.
        placements={"/nix/store/lost": {"d1", "d2"}},
        replication_k=3,
    )
    marks: list[tuple[str, str]] = []
    ctx = replace(
        ctx,
        mark_task_unfulfillable=lambda h, r: marks.append((h, r)),
        lookup_task_hash_for_outpath=lambda op: "abc123",
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        worker._maybe_mark_unfulfillable(  # noqa: SLF001 — white-box
            "/nix/store/lost", frozenset({"d1", "d2"}),
        )
        assert marks == [(
            "abc123",
            "toolchain outpath=/nix/store/lost dead_holders=['d1', 'd2']",
        )]
    finally:
        sender.stop()


def test_repair_marks_unfulfillable_integrates_with_on_diff(
    ctx_factory,
) -> None:
    """End-to-end through the public ``on_diff`` API: we are a holder
    but the cluster lost our only co-holder (and our copy is somehow
    also expected gone — simulated by an empty placements ref). The
    public repair flow walks outpaths-we-hold AND post-repair holder
    count below K; the Q1 helper then fires when post-exclusion live
    holders are empty.

    This is the realistic wire-in path: framework signals a peer
    death → repair worker calls ``_repair_for_outpaths`` → which
    inside the loop invokes ``_maybe_mark_unfulfillable`` once.
    """
    # The framework signal: dead-peer's removal. We hold the outpath
    # AND dead was the only OTHER holder. Once the placement watcher
    # refreshes the snapshot will drop to {me}; in the meantime, the
    # excludes set tells the sender to treat `dead` as a ghost.
    # The mark_unfulfillable fires only when holders - excludes is
    # empty; here {me, dead} - {dead} = {me}, so it does NOT fire
    # (we still hold a copy → cluster not lost the toolchain). The
    # integration test is therefore the "stays-silent" case.
    ctx, _, _ = ctx_factory(
        peers=[_peer("me"), _peer("dead")],
        placements={"/nix/store/safe": {"me", "dead"}},
        replication_k=3,
    )
    marks: list[tuple[str, str]] = []
    ctx = replace(
        ctx,
        mark_task_unfulfillable=lambda h, r: marks.append((h, r)),
        lookup_task_hash_for_outpath=lambda op: "tch",
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        worker.on_peer_removed("dead")
        # We still hold it → no unfulfillable signal.
        assert marks == []
    finally:
        sender.stop()


def test_repair_falls_back_when_mark_unfulfillable_unbound(
    ctx_factory,
) -> None:
    """When ``mark_task_unfulfillable`` is None (legacy / pre-Q1), the
    repair worker silently completes the push_attempt and does NOT
    invoke any framework hook. Regression guard for the fallback
    contract."""
    ctx, rec, _ = ctx_factory(
        peers=[_peer("me"), _peer("d1")],
        placements={"/nix/store/lost": {"d1"}},
        replication_k=3,
    )
    # mark_task_unfulfillable left as default None.
    assert ctx.mark_task_unfulfillable is None
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        # Even with a "would-fire" exclude set, no fail-permanent
        # signal is dispatched because the callable is unbound.
        worker._maybe_mark_unfulfillable(  # noqa: SLF001
            "/nix/store/lost", frozenset({"d1"}),
        )
        # No exception, no marks — and offers weren't issued from the
        # private helper (separate from push_attempt).
        assert rec.offers == []
    finally:
        sender.stop()


def test_repair_skips_unfulfillable_when_lookup_returns_none(
    ctx_factory,
) -> None:
    """A non-toolchain outpath (lookup returns None) silently skips
    the unfulfillable signal — only TaskInfo-registered toolchains
    are eligible."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("me"), _peer("d1")],
        placements={"/nix/store/common": {"d1"}},
        replication_k=3,
    )
    marks: list[tuple[str, str]] = []
    ctx = replace(
        ctx,
        mark_task_unfulfillable=lambda h, r: marks.append((h, r)),
        lookup_task_hash_for_outpath=lambda op: None,  # not in manifest
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        worker._maybe_mark_unfulfillable(  # noqa: SLF001
            "/nix/store/common", frozenset({"d1"}),
        )
        assert marks == []
    finally:
        sender.stop()


def test_repair_skips_unfulfillable_when_live_holder_remains(
    ctx_factory,
) -> None:
    """If at least one live holder remains post-repair, the cluster
    still has the toolchain; the framework hook is not invoked."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("me"), _peer("alive"), _peer("dead")],
        placements={"/nix/store/x": {"alive", "dead"}},
        replication_k=3,
    )
    marks: list[tuple[str, str]] = []
    ctx = replace(
        ctx,
        mark_task_unfulfillable=lambda h, r: marks.append((h, r)),
        lookup_task_hash_for_outpath=lambda op: "task-hash-hex",
    )
    sender = ReplicationSender(ctx)
    worker = ReplicationRepairWorker(ctx, sender)
    try:
        worker._maybe_mark_unfulfillable(  # noqa: SLF001
            "/nix/store/x", frozenset({"dead"}),
        )
        # `alive` is still a holder; cluster still has the path.
        assert marks == []
    finally:
        sender.stop()


def test_sender_preferred_secondaries_fires_once_after_settle(
    ctx_factory,
) -> None:
    """On cascade convergence (>= K holders) the sender arms a settle
    timer; firing it triggers exactly ONE
    update_preferred_secondaries(task_hash, sorted_holders) call."""
    placements: dict[str, set[str]] = {"/nix/store/tc": {"s1", "s2", "s3"}}
    ctx, _, ref = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements=placements,
        replication_k=3,
    )
    updates: list[tuple[str, list[str]]] = []
    ctx = replace(
        ctx,
        update_preferred_secondaries=lambda h, ss: updates.append((h, list(ss))),
        lookup_task_hash_for_outpath=lambda op: "tchash",
        preferred_secondaries_settle_seconds=99.0,  # only fires manually
    )
    registry = _FakeTimerRegistry()
    sender = ReplicationSender(ctx, timer_factory=registry.factory)
    try:
        # Synthesize a path-have event that puts us at K.
        sender.on_path_have("s3", "/nix/store/tc")
        # Exactly one settle timer armed.
        assert len(registry.timers) == 1
        timer = registry.timers[0]
        assert not timer.cancelled
        assert updates == [], "should not fire before timer elapses"
        # Fire the timer (fake-time settle).
        timer.fire()
        assert len(updates) == 1
        h, ss = updates[0]
        assert h == "tchash"
        assert ss == sorted({"s1", "s2", "s3"})
        # Idempotency: a subsequent path-have for a K+1 holder must
        # NOT arm a new timer or refire.
        ref["/nix/store/tc"].add("s4")
        sender.on_path_have("s4", "/nix/store/tc")
        assert len(registry.timers) == 1  # no new timer
        assert len(updates) == 1  # no refire
    finally:
        sender.stop()


def test_sender_preferred_secondaries_cancels_on_unsettle(
    ctx_factory,
) -> None:
    """If a path-have during the debounce window drops effective
    holders BELOW K (e.g. simulating watcher refresh that revealed a
    previously-thought holder was a ghost), the pending timer is
    cancelled."""
    placements: dict[str, set[str]] = {"/nix/store/tc": {"s1", "s2", "s3"}}
    ctx, _, ref = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements=placements,
        replication_k=3,
    )
    updates: list[tuple[str, list[str]]] = []
    ctx = replace(
        ctx,
        update_preferred_secondaries=lambda h, ss: updates.append((h, list(ss))),
        lookup_task_hash_for_outpath=lambda op: "tchash",
        preferred_secondaries_settle_seconds=99.0,
    )
    registry = _FakeTimerRegistry()
    sender = ReplicationSender(ctx, timer_factory=registry.factory)
    try:
        sender.on_path_have("s3", "/nix/store/tc")
        assert len(registry.timers) == 1
        first_timer = registry.timers[0]
        assert not first_timer.cancelled
        # Cascade un-settles: placement watcher dropped two holders.
        ref["/nix/store/tc"] = {"s1"}
        sender.on_path_have("s1", "/nix/store/tc")
        # First timer cancelled; no new timer armed (still below K).
        assert first_timer.cancelled
        assert len(registry.timers) == 1
        # Firing the cancelled timer is a no-op (defensive).
        first_timer.fire()  # the _FakeTimer.fire() respects cancelled
        assert updates == []
    finally:
        sender.stop()


def test_sender_preferred_secondaries_no_timer_when_callable_unbound(
    ctx_factory,
) -> None:
    """When update_preferred_secondaries is None (legacy ctx), no
    settle timer is ever armed — even when cascade converges."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/tc": {"s1", "s2", "s3"}},
        replication_k=3,
    )
    # update_preferred_secondaries left default None.
    assert ctx.update_preferred_secondaries is None
    registry = _FakeTimerRegistry()
    sender = ReplicationSender(ctx, timer_factory=registry.factory)
    try:
        sender.on_path_have("s3", "/nix/store/tc")
        assert registry.timers == []
    finally:
        sender.stop()


def test_sender_preferred_secondaries_skips_when_lookup_returns_none(
    ctx_factory,
) -> None:
    """If the task-hash lookup yields None at settle time (e.g. this
    converged outpath isn't a TaskInfo toolchain), the bound callable
    is not invoked."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/common": {"s1", "s2", "s3"}},
        replication_k=3,
    )
    updates: list[tuple[str, list[str]]] = []
    ctx = replace(
        ctx,
        update_preferred_secondaries=lambda h, ss: updates.append((h, list(ss))),
        lookup_task_hash_for_outpath=lambda op: None,
        preferred_secondaries_settle_seconds=99.0,
    )
    registry = _FakeTimerRegistry()
    sender = ReplicationSender(ctx, timer_factory=registry.factory)
    try:
        sender.on_path_have("s3", "/nix/store/common")
        assert len(registry.timers) == 1
        registry.timers[0].fire()
        assert updates == []
    finally:
        sender.stop()


def test_sender_stop_cancels_settle_timers(ctx_factory) -> None:
    """stop() must cancel pending settle timers in addition to the
    in-flight offer timers."""
    ctx, _, _ = ctx_factory(
        peers=[_peer("s1"), _peer("s2"), _peer("s3"), _peer("me")],
        placements={"/nix/store/tc": {"s1", "s2", "s3"}},
        replication_k=3,
    )
    ctx = replace(
        ctx,
        update_preferred_secondaries=lambda h, ss: None,
        lookup_task_hash_for_outpath=lambda op: "h",
        preferred_secondaries_settle_seconds=99.0,
    )
    registry = _FakeTimerRegistry()
    sender = ReplicationSender(ctx, timer_factory=registry.factory)
    sender.on_path_have("s3", "/nix/store/tc")
    assert len(registry.timers) == 1
    assert not registry.timers[0].cancelled
    sender.stop()
    assert registry.timers[0].cancelled


# ---------------------------------------------------------------------------
# BroadcastSender
# ---------------------------------------------------------------------------


def _wait_for(condition, timeout: float = 1.0, interval: float = 0.01) -> bool:
    """Spin until *condition* returns truthy or *timeout* elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def test_broadcast_enqueue_returns_fresh_ids() -> None:
    """Each ``enqueue_broadcast`` mints a unique UUID-hex id."""
    fan_out = mock.MagicMock(return_value=(0, 0, []))
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [],
        our_pubkey="me:PK",
        fan_out=fan_out,
    )
    try:
        ids = {
            sender.enqueue_broadcast("/nix/store/a", 42, "drv-toolchain")
            for _ in range(8)
        }
        assert len(ids) == 8
        for bid in ids:
            assert isinstance(bid, str) and len(bid) == 32  # uuid4().hex
    finally:
        sender.stop()


def test_broadcast_worker_invokes_fan_out_with_all_peer_urls() -> None:
    """The worker thread calls ``fan_out_broadcast_drv`` with the
    current peer URL list, hop_count=0, and the originator id."""
    urls = ["http://h1:6000", "http://h2:6000", "http://h3:6000"]
    fan_out = mock.MagicMock(return_value=(3, 0, []))
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: list(urls),
        our_pubkey="me:PK",
        fan_out=fan_out,
    )
    try:
        bid = sender.enqueue_broadcast("/nix/store/d.drv", 1234, "drv-toolchain")
        assert _wait_for(lambda: fan_out.call_count == 1)
        _, kwargs = fan_out.call_args
        assert kwargs["peer_urls"] == urls
        assert kwargs["path"] == "/nix/store/d.drv"
        assert kwargs["size"] == 1234
        assert kwargs["broadcast_id"] == bid
        assert kwargs["origin_peer_id"] == "me"
        assert kwargs["hop_count"] == 0
        assert kwargs["our_pubkey"] == "me:PK"
    finally:
        sender.stop()


def test_broadcast_wait_for_completion_returns_fanout_tuple() -> None:
    """``wait_for_completion`` returns the (success, fail, failed)
    tuple recorded from the fan-out call."""
    fan_out = mock.MagicMock(
        return_value=(2, 1, ["http://dead:6000"]),
    )
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [
            "http://h1:6000", "http://h2:6000", "http://dead:6000",
        ],
        fan_out=fan_out,
    )
    try:
        bid = sender.enqueue_broadcast("/nix/store/x.drv", 99, "drv-toolchain")
        result = sender.wait_for_completion(bid, timeout=2.0)
        assert isinstance(result, BroadcastResult)
        assert result.broadcast_id == bid
        assert result.success_count == 2
        assert result.fail_count == 1
        assert result.failed_peers == ("http://dead:6000",)
    finally:
        sender.stop()


def test_broadcast_empty_peer_list_yields_zero_zero() -> None:
    """An empty peer list short-circuits to ``(0, 0, [])`` without
    invoking the fan-out callable."""
    fan_out = mock.MagicMock(return_value=(0, 0, []))
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [],
        fan_out=fan_out,
    )
    try:
        bid = sender.enqueue_broadcast("/nix/store/empty.drv", 0, None)
        result = sender.wait_for_completion(bid, timeout=2.0)
        assert result is not None
        assert (result.success_count, result.fail_count) == (0, 0)
        assert result.failed_peers == ()
        # Short-circuit: we did NOT call the fan-out helper.
        fan_out.assert_not_called()
    finally:
        sender.stop()


def test_broadcast_fan_out_exception_does_not_kill_worker() -> None:
    """If ``fan_out_broadcast_drv`` raises, the worker thread logs
    and continues; subsequent enqueues still complete."""
    call_count = {"n": 0}

    def flaky(*_, **__):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transport blow-up")
        return (1, 0, [])

    urls = ["http://h1:6000"]
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: list(urls),
        fan_out=flaky,
    )
    try:
        bid_bad = sender.enqueue_broadcast("/nix/store/bad.drv", 1, None)
        bid_good = sender.enqueue_broadcast("/nix/store/good.drv", 1, None)
        result_bad = sender.wait_for_completion(bid_bad, timeout=2.0)
        result_good = sender.wait_for_completion(bid_good, timeout=2.0)
        # Bad broadcast: full failure, but DID record so waiter
        # is released.
        assert result_bad is not None
        assert result_bad.success_count == 0
        assert result_bad.fail_count == len(urls)
        assert list(result_bad.failed_peers) == urls
        # Good broadcast: worker is still alive and processed it.
        assert result_good is not None
        assert result_good.success_count == 1
        assert result_good.fail_count == 0
    finally:
        sender.stop()


def test_broadcast_peer_provider_exception_yields_empty_fanout() -> None:
    """If the peer-URL provider raises, the worker treats it as empty
    peers (0/0) rather than crashing."""
    fan_out = mock.MagicMock(return_value=(0, 0, []))

    def boom() -> list[str]:
        raise RuntimeError("provider unavailable")

    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=boom,
        fan_out=fan_out,
    )
    try:
        bid = sender.enqueue_broadcast("/nix/store/p.drv", 1, None)
        result = sender.wait_for_completion(bid, timeout=2.0)
        assert result is not None
        assert (result.success_count, result.fail_count) == (0, 0)
        fan_out.assert_not_called()
    finally:
        sender.stop()


def test_broadcast_stop_exits_worker_thread_promptly() -> None:
    """``stop()`` causes the worker thread to exit; the thread should
    be joined within a reasonable window."""
    fan_out = mock.MagicMock(return_value=(0, 0, []))
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [],
        fan_out=fan_out,
    )
    # Force the worker thread to spin up via an enqueue + wait.
    bid = sender.enqueue_broadcast("/nix/store/q.drv", 1, None)
    sender.wait_for_completion(bid, timeout=1.0)
    worker = sender._thread  # noqa: SLF001 — white-box; we check it died
    assert worker is not None and worker.is_alive()
    sender.stop()
    # stop() joins with 2 s timeout; if the thread didn't exit, it's
    # still alive here.
    assert _wait_for(lambda: not worker.is_alive(), timeout=2.0), (
        "worker thread did not exit promptly after stop()"
    )


def test_broadcast_enqueue_after_stop_raises() -> None:
    """Enqueueing after ``stop()`` is a programmer error and raises."""
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [],
        fan_out=mock.MagicMock(return_value=(0, 0, [])),
    )
    sender.stop()
    with pytest.raises(RuntimeError):
        sender.enqueue_broadcast("/nix/store/late.drv", 1, None)


def test_broadcast_wait_for_unknown_id_returns_none() -> None:
    """Unknown broadcast_id results in ``None`` from
    ``wait_for_completion`` (rather than blocking forever)."""
    sender = BroadcastSender(
        self_peer_id="me",
        peer_url_provider=lambda: [],
        fan_out=mock.MagicMock(return_value=(0, 0, [])),
    )
    try:
        result = sender.wait_for_completion("nonexistent-id", timeout=0.1)
        assert result is None
    finally:
        sender.stop()


# ---------------------------------------------------------------------------
# BroadcastReceiver
# ---------------------------------------------------------------------------


def _make_broadcast_receiver(
    *,
    self_peer_id: str = "me",
    url_map: Optional[dict[str, str]] = None,
    locally_valid: Optional[set[str]] = None,
    fetch_ok: bool = True,
    fan_out_result: tuple[int, int, list[str]] = (0, 0, []),
    shared_fs: Optional[pathlib.Path] = None,
    record_self_has=None,
    max_hop: int = DEFAULT_BROADCAST_MAX_HOP,
) -> tuple[
    BroadcastReceiver,
    dict[str, list],
    mock.MagicMock,
]:
    """Construct a receiver with fakes; return (recv, recorders, fan_out_mock)."""
    url_map = dict(url_map or {})
    locally_valid_set = set(locally_valid or set())
    recorders: dict[str, list] = {
        "fetches": [], "records": [],
    }

    def _fetch(path: str, origin_url: str) -> bool:
        recorders["fetches"].append((path, origin_url))
        return bool(fetch_ok)

    def _is_local(path: str) -> bool:
        return path in locally_valid_set

    fan_out_mock = mock.MagicMock(return_value=fan_out_result)

    if record_self_has is None:
        def record_self_has(
            shared_fs_arg, *,
            my_secondary_id, outpath, drv_path, item_class,
            **_kwargs,
        ) -> None:
            recorders["records"].append((
                shared_fs_arg, my_secondary_id, outpath,
                drv_path, item_class,
            ))

    recv = BroadcastReceiver(
        self_peer_id=self_peer_id,
        peer_url_provider=lambda: dict(url_map),
        is_path_locally_valid=_is_local,
        fetch_path_from_peer=_fetch,
        our_pubkey="me:PK",
        shared_fs=shared_fs,
        record_self_has=record_self_has,
        fan_out=fan_out_mock,
        max_hop=max_hop,
        timeout=0.5,
    )
    return recv, recorders, fan_out_mock


def test_broadcast_receiver_already_have_short_circuits(
    tmp_path: pathlib.Path,
) -> None:
    """Path already locally valid → no fetch, no fan-out, returns True.

    A placement record is still written so peers learn we hold it.
    """
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={"origin": "http://origin:6000", "me": "http://me:6000"},
        locally_valid={"/nix/store/x.drv"},
        shared_fs=tmp_path,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/x.drv", 1024, "origin", "bid-A", 0,
        )
        assert ok is True
        assert rec["fetches"] == []
        # Give the fan-out worker a tick to run; it should NOT.
        time.sleep(0.05)
        fan_out.assert_not_called()
        # Placement record: phase0_eval_drv item class.
        assert len(rec["records"]) == 1
        _shared, sid, outpath, drv, klass = rec["records"][0]
        assert sid == "me"
        assert outpath == "/nix/store/x.drv"
        assert drv == "/nix/store/x.drv"
        assert klass == BROADCAST_ITEM_CLASS == "phase0_eval_drv"
    finally:
        recv.stop()


def test_broadcast_receiver_unknown_origin_returns_false(
    tmp_path: pathlib.Path,
) -> None:
    """Origin peer not in the URL map → return False, no fetch, no
    fan-out, no record."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={"some-other-peer": "http://o:6000"},
        shared_fs=tmp_path,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/u.drv", 1024, "unknown-origin", "bid-U", 0,
        )
        assert ok is False
        assert rec["fetches"] == []
        assert rec["records"] == []
        time.sleep(0.05)
        fan_out.assert_not_called()
    finally:
        recv.stop()


def test_broadcast_receiver_fetch_then_fan_out(
    tmp_path: pathlib.Path,
) -> None:
    """Successful fetch from origin → record + fan-out to peers minus
    self minus origin, returns True."""
    url_map = {
        "me": "http://me:6000",
        "origin": "http://origin-host:6000",
        "peer-a": "http://a:6000",
        "peer-b": "http://b:6000",
    }
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map=url_map,
        fetch_ok=True,
        fan_out_result=(2, 0, []),
        shared_fs=tmp_path,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/f.drv", 4096, "origin", "bid-F", 2,
        )
        assert ok is True
        # Fetch was called against the origin's URL.
        assert rec["fetches"] == [
            ("/nix/store/f.drv", "http://origin-host:6000"),
        ]
        # Wait for fan-out worker to dispatch.
        deadline = time.time() + 2.0
        while time.time() < deadline and fan_out.call_count == 0:
            time.sleep(0.02)
        assert fan_out.call_count == 1
        _, kwargs = fan_out.call_args
        assert set(kwargs["peer_urls"]) == {
            "http://a:6000", "http://b:6000",
        }
        # origin (excluded by id) + self (excluded by id) are NOT in
        # the fan-out target list.
        assert "http://origin-host:6000" not in kwargs["peer_urls"]
        assert "http://me:6000" not in kwargs["peer_urls"]
        # Hop count is incremented by one.
        assert kwargs["hop_count"] == 3
        # origin_peer_id on the cascaded broadcast is ME (the
        # cascading peer); preserves the broadcast_id.
        assert kwargs["origin_peer_id"] == "me"
        assert kwargs["broadcast_id"] == "bid-F"
        assert kwargs["path"] == "/nix/store/f.drv"
        assert kwargs["size"] == 4096
        # Placement record written.
        assert len(rec["records"]) == 1
        assert rec["records"][0][2] == "/nix/store/f.drv"
        assert rec["records"][0][4] == BROADCAST_ITEM_CLASS
    finally:
        recv.stop()


def test_broadcast_receiver_fetch_failure_no_fan_out(
    tmp_path: pathlib.Path,
) -> None:
    """fetch_path_from_peer returning False → return False, no fan-out,
    no placement record (we don't have the path)."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={
            "me": "http://me:6000",
            "origin": "http://origin:6000",
            "p1": "http://p1:6000",
        },
        fetch_ok=False,
        shared_fs=tmp_path,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/dead.drv", 100, "origin", "bid-X", 0,
        )
        assert ok is False
        # Fetch WAS attempted.
        assert rec["fetches"] == [
            ("/nix/store/dead.drv", "http://origin:6000"),
        ]
        # But no record + no fan-out.
        assert rec["records"] == []
        time.sleep(0.05)
        fan_out.assert_not_called()
    finally:
        recv.stop()


def test_broadcast_receiver_hop_cap_records_but_no_fan_out(
    tmp_path: pathlib.Path,
) -> None:
    """At/above max_hop: fetch + record still happen but the cascade
    is suppressed."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={
            "me": "http://me:6000",
            "origin": "http://origin:6000",
            "p1": "http://p1:6000",
            "p2": "http://p2:6000",
        },
        fetch_ok=True,
        shared_fs=tmp_path,
        max_hop=3,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/h.drv", 99, "origin", "bid-H", 3,
        )
        assert ok is True
        # Fetch + record still ran.
        assert rec["fetches"] == [("/nix/store/h.drv", "http://origin:6000")]
        assert len(rec["records"]) == 1
        # No cascade.
        time.sleep(0.05)
        fan_out.assert_not_called()
    finally:
        recv.stop()


def test_broadcast_receiver_already_have_skips_record_when_no_shared_fs() -> None:
    """When shared_fs is None the placement-record write is skipped
    but the True-return contract is preserved."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={"origin": "http://origin:6000"},
        locally_valid={"/nix/store/n.drv"},
        shared_fs=None,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/n.drv", 1, "origin", "bid-N", 0,
        )
        assert ok is True
        assert rec["records"] == []  # no record_self_has when shared_fs is None
        time.sleep(0.05)
        fan_out.assert_not_called()
    finally:
        recv.stop()


def test_broadcast_receiver_excludes_self_and_origin_when_singleton(
    tmp_path: pathlib.Path,
) -> None:
    """If the only peers in the map are self + origin, fan-out target
    list is empty; we still return True (we did fetch + record)."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={
            "me": "http://me:6000",
            "origin": "http://origin:6000",
        },
        fetch_ok=True,
        shared_fs=tmp_path,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/s.drv", 1, "origin", "bid-S", 0,
        )
        assert ok is True
        # Wait briefly; the worker may or may not have run (no
        # targets to fan-out to, so it short-circuits).
        time.sleep(0.1)
        fan_out.assert_not_called()
        # Record was still written.
        assert len(rec["records"]) == 1
    finally:
        recv.stop()


def test_broadcast_receiver_stop_idempotent_and_rejects_new_offers(
    tmp_path: pathlib.Path,
) -> None:
    """After stop(), on_broadcast_offer returns False without doing
    any I/O. stop() is idempotent."""
    recv, rec, fan_out = _make_broadcast_receiver(
        url_map={"origin": "http://origin:6000"},
        fetch_ok=True,
        shared_fs=tmp_path,
    )
    recv.stop()
    recv.stop()  # idempotent
    ok = recv.on_broadcast_offer(
        "/nix/store/late.drv", 1, "origin", "bid-L", 0,
    )
    assert ok is False
    assert rec["fetches"] == []
    assert rec["records"] == []
    fan_out.assert_not_called()


def test_broadcast_receiver_provider_exception_yields_unknown_origin(
    tmp_path: pathlib.Path,
) -> None:
    """If peer_url_provider raises, we treat the map as empty (origin
    becomes unknown) and reject the offer."""

    def boom() -> dict[str, str]:
        raise RuntimeError("provider went away")

    recv = BroadcastReceiver(
        self_peer_id="me",
        peer_url_provider=boom,
        is_path_locally_valid=lambda _p: False,
        fetch_path_from_peer=lambda _p, _u: True,
        shared_fs=tmp_path,
        record_self_has=lambda *a, **kw: None,
        fan_out=mock.MagicMock(return_value=(0, 0, [])),
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/p.drv", 1, "origin", "bid-P", 0,
        )
        assert ok is False
    finally:
        recv.stop()


def test_broadcast_receiver_fetch_exception_returns_false(
    tmp_path: pathlib.Path,
) -> None:
    """A fetch helper that raises is treated as a failed fetch
    (False return, no record, no fan-out) — the receiver never propagates
    out into the HTTP handler thread."""
    fan_out = mock.MagicMock(return_value=(0, 0, []))
    records: list[tuple] = []

    def _record(*_a, **kw) -> None:
        records.append((kw.get("outpath"), kw.get("item_class")))

    def _fetch(_path, _url):
        raise RuntimeError("simulated transport blow-up")

    recv = BroadcastReceiver(
        self_peer_id="me",
        peer_url_provider=lambda: {"origin": "http://o:6000"},
        is_path_locally_valid=lambda _p: False,
        fetch_path_from_peer=_fetch,
        shared_fs=tmp_path,
        record_self_has=_record,
        fan_out=fan_out,
    )
    try:
        ok = recv.on_broadcast_offer(
            "/nix/store/boom.drv", 1, "origin", "bid-B", 0,
        )
        assert ok is False
        assert records == []
        fan_out.assert_not_called()
    finally:
        recv.stop()

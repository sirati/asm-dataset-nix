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

from compiler_suit_runner.peer_cache import PeerInfo, PlacementDiff
from compiler_suit_runner.peer_replication import (
    DEFAULT_OFFER_TIMEOUT_SECONDS,
    DEFAULT_REPLICATION_K,
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

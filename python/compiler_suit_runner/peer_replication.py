"""K=3 toolchain-outpath replication coordination (consumer-only).

Layered on top of :mod:`peer_paths` / :mod:`peer_paths_fetch` /
:mod:`peer_push`, this module maintains the cluster-wide invariant
"every toolchain outpath is held by at least K distinct secondaries"
through a handshake-based push protocol:

* Receive-side cascade — when this secondary fetches a toolchain for
  the first time (via :class:`ReplicationReceiver` or after a regular
  ``phase2_toolchain_validate``), the local
  :class:`ReplicationSender.push_attempt` tries to push the outpath to
  ``K - current_holder_count`` more peers (typically 2).
* Death-side repair — when a peer is detected as dead/removed,
  :meth:`ReplicationRepairWorker.on_peer_removed` walks the placement
  map and, for each outpath where the deceased was a holder and total
  holders dropped below K, fires a single replacement
  :meth:`ReplicationSender.push_attempt`.

The handshake exchanges four point-to-point events over the existing
``peer_push`` HTTP channel:

* sender → recipient: ``/peer/path-offer``
* recipient → sender: ``/peer/path-accept`` or ``/peer/path-reject``
* sender → recipient: ``/peer/path-cancel`` (used when convergence
  obviates a still-pending offer)

The sender's 1 s timeout per offer ensures the cluster converges
quickly even when a candidate is unreachable.

**Architectural boundary.** Everything in this module is consumer-side
async coordination, NOT framework tasks. The replication work runs in
parallel to the framework's task and worker management; it
communicates exclusively over the peer_push HTTP channel, holds no
locks shared with the dynrunner Rust↔Python boundary, and its failure
modes never fail a framework task. A push that doesn't converge to
K=3 just leaves placement at K=2; the cluster keeps running with
reduced redundancy.

The peer-removed signal is intended to come from a future framework
hook (see plan: Framework Ask #4); in this revision the
:class:`PathPlacementWatcher`'s :func:`PlacementDiff`-callback is the
sole signal (degraded-mode polling) but the
:meth:`ReplicationRepairWorker.on_peer_removed` API is shaped so the
framework hook can be wired in once it lands.
"""

from __future__ import annotations

import logging
import pathlib
import random
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Optional

from compiler_suit_runner import peer_paths, peer_paths_fetch, peer_push
from compiler_suit_runner.peer_cache import PeerInfo, PlacementDiff

__all__ = [
    "DEFAULT_OFFER_TIMEOUT_SECONDS",
    "DEFAULT_REPLICATION_K",
    "ReplicationContext",
    "ReplicationReceiver",
    "ReplicationRepairWorker",
    "ReplicationSender",
]

logger = logging.getLogger(__name__)

DEFAULT_REPLICATION_K = 3
DEFAULT_OFFER_TIMEOUT_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Shared dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationContext:
    """Read-only handles the replication classes need.

    Live state (peers list, placement snapshot) is fetched via the
    ``get_*`` callables on every operation so the classes always see
    the freshest data without needing to subscribe to watcher updates
    themselves.

    The push/fetch/record callables are dependency-injected to keep
    the module testable: tests substitute fakes that record arguments
    instead of hitting the network.
    """

    my_secondary_id: str
    our_pubkey: str
    shared_fs: pathlib.Path
    get_peers: Callable[[], list[PeerInfo]]
    get_placements: Callable[[], dict[str, set[str]]]
    # Wire-format callables (default to peer_push helpers in production)
    push_path_offer: Callable[..., bool] = field(repr=False, default=peer_push.push_path_offer)
    push_path_accept: Callable[..., bool] = field(repr=False, default=peer_push.push_path_accept)
    push_path_reject: Callable[..., bool] = field(repr=False, default=peer_push.push_path_reject)
    push_path_cancel: Callable[..., bool] = field(repr=False, default=peer_push.push_path_cancel)
    # Heavyweight functions; injected for tests.
    fetch_from_peer: Callable[..., Optional[str]] = field(
        repr=False, default=peer_paths_fetch.fetch_from_peer,
    )
    is_path_locally_valid: Callable[[str], bool] = field(
        repr=False, default=peer_paths_fetch.is_path_locally_valid,
    )
    record_self_has: Callable[..., None] = field(
        repr=False, default=peer_paths.record_self_has,
    )
    # Knobs
    replication_k: int = DEFAULT_REPLICATION_K
    offer_timeout_seconds: float = DEFAULT_OFFER_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Sender — push-attempt state machine
# ---------------------------------------------------------------------------


@dataclass
class _InFlightOffer:
    """One pending outgoing offer."""

    target_sid: str
    timer: Optional[threading.Timer]
    drv_path: str
    item_class: str


class ReplicationSender:
    """Drives outgoing ``path-offer`` handshakes for K=3 convergence.

    All public methods are safe to call from any thread; internal
    state is guarded by a single re-entrant lock. Timer firings,
    HTTP handler dispatches, and worker-thread push_attempt calls
    all funnel through the same critical sections.
    """

    def __init__(self, ctx: ReplicationContext) -> None:
        self._ctx = ctx
        # outpath -> {target_sid: _InFlightOffer}
        self._in_flight: dict[str, dict[str, _InFlightOffer]] = {}
        # Per-outpath metadata so we can re-issue a push_attempt
        # after a reject/timeout without the caller having to remember
        # the drv_path + item_class.
        self._metadata: dict[str, tuple[str, str]] = {}  # outpath -> (drv_path, item_class)
        self._lock = threading.RLock()
        self._stopped = False

    # --- public API ---------------------------------------------------------

    def push_attempt(
        self,
        outpath: str,
        drv_path: str = "",
        item_class: str = peer_paths.ITEM_CLASS_TOOLCHAIN,
        exclude_holders: Optional[Iterable[str]] = None,
    ) -> int:
        """Issue offers until ``current_holders + in_flight >= K``.

        Returns the number of fresh offers issued in this call.
        Idempotent: if K is already satisfied (counting in-flight),
        returns 0 without I/O.

        ``exclude_holders`` lets the repair worker tell the sender
        "treat these secondaries as ghosts" — they appear in the
        placement snapshot (the watcher hasn't refreshed since their
        death) but should not be counted toward K. Such ids are also
        excluded from candidate selection.
        """
        with self._lock:
            if self._stopped:
                return 0
            # Record metadata so reject/timeout re-issues can find it.
            self._metadata[outpath] = (drv_path, item_class)
            excludes = frozenset(exclude_holders or ())
            return self._push_attempt_locked(outpath, excludes)

    def on_accept(self, from_sid: str, outpath: str) -> None:
        """Recipient accepted an offer; keep timer until path-have or
        timeout settles the slot."""
        # Nothing structural to do — the in-flight slot stays until
        # the recipient broadcasts path-have (settled positively) or
        # the timer fires (assumed dropped). The accept is mostly
        # informational; we don't extend the timer because the fetch
        # itself is what we're waiting on, and the broadcast comes
        # AFTER fetch completion.
        del from_sid, outpath  # acknowledged

    def on_reject(self, from_sid: str, outpath: str, reason: str) -> None:
        """Recipient declined; release the slot and try another peer."""
        with self._lock:
            self._release_slot(outpath, from_sid)
            logger.debug(
                "ReplicationSender: %s rejected %s (%s); retrying",
                from_sid, outpath, reason,
            )
            # On reject we don't carry excludes — the rejecter is
            # already in self._metadata's "tried" set via in_flight
            # bookkeeping (popped above) but a future retry should
            # still be able to pick them again later. Use empty.
            self._push_attempt_locked(outpath)

    def on_timeout(self, outpath: str, target_sid: str) -> None:
        """Offer timer elapsed without an accept/reject; assume
        dropped and try another peer."""
        with self._lock:
            slot = self._in_flight.get(outpath, {}).get(target_sid)
            if slot is None:
                return  # already settled by accept/path-have/cancel
            logger.debug(
                "ReplicationSender: offer to %s for %s timed out",
                target_sid, outpath,
            )
            self._release_slot(outpath, target_sid)
            self._push_attempt_locked(outpath)

    def on_path_have(self, holder_sid: str, outpath: str) -> None:
        """A peer (or we) broadcasts that it now holds *outpath*.

        If the new holder was one of OUR in-flight targets, settle
        that slot. After settling, if K is met (or exceeded) cancel
        the remaining outstanding offers for this outpath.
        """
        with self._lock:
            # Settle our slot if applicable.
            self._release_slot(outpath, holder_sid)
            # Convergence check.
            holders = self._ctx.get_placements().get(outpath, set())
            # Note: include the freshly-broadcasting sid since the
            # placement watcher might not have refreshed yet.
            effective = set(holders) | {holder_sid}
            if len(effective) < self._ctx.replication_k:
                return
            # K satisfied: cancel any remaining outstanding offers.
            outstanding = list(self._in_flight.get(outpath, {}).items())
            if not outstanding:
                return
            peers = {p.secondary_id: p for p in self._ctx.get_peers()}
            for target_sid, slot in outstanding:
                if slot.timer is not None:
                    slot.timer.cancel()
                target_peer = peers.get(target_sid)
                if target_peer is not None:
                    try:
                        self._ctx.push_path_cancel(
                            target_peer,
                            self._ctx.my_secondary_id,
                            outpath,
                            self._ctx.our_pubkey,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "ReplicationSender: cancel push to %s failed",
                            target_sid,
                        )
            self._in_flight.pop(outpath, None)

    def stop(self) -> None:
        """Cancel all outstanding timers; subsequent push_attempt
        calls become no-ops."""
        with self._lock:
            self._stopped = True
            for slots in self._in_flight.values():
                for slot in slots.values():
                    if slot.timer is not None:
                        slot.timer.cancel()
            self._in_flight.clear()

    # --- introspection (mostly for tests) -----------------------------------

    def in_flight_targets(self, outpath: str) -> set[str]:
        with self._lock:
            return set(self._in_flight.get(outpath, {}).keys())

    # --- internals ----------------------------------------------------------

    def _push_attempt_locked(
        self, outpath: str, excludes: frozenset[str] = frozenset(),
    ) -> int:
        """Caller must hold ``self._lock``. Returns offers issued."""
        if self._stopped:
            return 0
        drv_path, item_class = self._metadata.get(outpath, ("", ""))
        placements = self._ctx.get_placements()
        # Exclude ghost holders (e.g. peers the framework just
        # reported dead but the placement watcher hasn't dropped yet).
        holders = set(placements.get(outpath, set())) - excludes
        in_flight = self._in_flight.get(outpath, {})
        needed = self._ctx.replication_k - len(holders) - len(in_flight)
        if needed <= 0:
            return 0
        peers = self._ctx.get_peers()
        candidates = [
            p for p in peers
            if p.secondary_id != self._ctx.my_secondary_id
            and p.secondary_id not in holders
            and p.secondary_id not in in_flight
            and p.secondary_id not in excludes
        ]
        if not candidates:
            return 0
        chosen = random.sample(candidates, min(needed, len(candidates)))
        issued = 0
        for target in chosen:
            ok = False
            try:
                ok = self._ctx.push_path_offer(
                    target,
                    self._ctx.my_secondary_id,
                    outpath,
                    drv_path,
                    item_class,
                    self._ctx.our_pubkey,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ReplicationSender: offer push to %s raised",
                    target.secondary_id,
                )
            if not ok:
                # Transport-level failure (peer unreachable, etc.).
                # Don't occupy a slot; the peer is effectively dead
                # for our purposes. Picking another is a future
                # push_attempt call's job — we don't recurse here to
                # avoid storming a fully-dead cluster.
                continue
            timer = threading.Timer(
                self._ctx.offer_timeout_seconds,
                self.on_timeout,
                args=(outpath, target.secondary_id),
            )
            timer.daemon = True
            timer.start()
            slot = _InFlightOffer(
                target_sid=target.secondary_id,
                timer=timer,
                drv_path=drv_path,
                item_class=item_class,
            )
            self._in_flight.setdefault(outpath, {})[target.secondary_id] = slot
            issued += 1
        return issued

    def _release_slot(self, outpath: str, target_sid: str) -> None:
        slots = self._in_flight.get(outpath)
        if not slots:
            return
        slot = slots.pop(target_sid, None)
        if slot is None:
            return
        if slot.timer is not None:
            slot.timer.cancel()
        if not slots:
            self._in_flight.pop(outpath, None)


# ---------------------------------------------------------------------------
# Receiver — offer-handling state machine
# ---------------------------------------------------------------------------


class ReplicationReceiver:
    """Handles incoming ``path-offer`` events.

    Tracks which sender (if any) we've already accepted for each
    outpath; subsequent offers from different senders get an
    ``already-targeted`` reject. The actual fetch runs in a background
    daemon thread so HTTP handlers return promptly.
    """

    def __init__(
        self,
        ctx: ReplicationContext,
        sender: ReplicationSender,
    ) -> None:
        self._ctx = ctx
        self._sender = sender
        # outpath -> targeting_sender_id (None when no in-flight fetch)
        self._targeting: dict[str, str] = {}
        # outpath -> set of sender_ids whose offer we accepted but
        # later cancelled; used to short-circuit the post-fetch
        # record_self_has on cancellation races.
        self._cancelled: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        self._stopped = False

    # --- handlers (wired to PeerPushServer callbacks) -----------------------

    def on_offer(self, record: dict) -> None:
        from_sid = record.get("from_secondary_id", "")
        outpath = record.get("outpath", "")
        drv_path = record.get("drv_path", "")
        item_class = record.get("item_class", "")
        if not from_sid or not outpath:
            return
        if self._stopped:
            self._reject(from_sid, outpath, "shutting-down")
            return
        # Already have it locally?
        try:
            locally_valid = self._ctx.is_path_locally_valid(outpath)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationReceiver: is_path_locally_valid raised for %s",
                outpath,
            )
            locally_valid = False
        if locally_valid:
            self._reject(from_sid, outpath, "already-have")
            return
        # Already targeted by another sender?
        with self._lock:
            current = self._targeting.get(outpath)
            if current is not None and current != from_sid:
                self._reject(from_sid, outpath, "already-targeted")
                return
            self._targeting[outpath] = from_sid
        # Accept — recipient will fetch in the background.
        self._accept(from_sid, outpath)
        threading.Thread(
            target=self._do_fetch,
            args=(from_sid, outpath, drv_path, item_class),
            name=f"replication-fetch-{outpath[-12:]}",
            daemon=True,
        ).start()

    def on_cancel(self, record: dict) -> None:
        from_sid = record.get("from_secondary_id", "")
        outpath = record.get("outpath", "")
        if not from_sid or not outpath:
            return
        with self._lock:
            current = self._targeting.get(outpath)
            if current != from_sid:
                return  # not from our offerer; ignore
            # We can't abort an in-flight `nix copy` cleanly; flag the
            # post-fetch path so record_self_has + cascade are
            # skipped.
            self._cancelled.setdefault(outpath, set()).add(from_sid)
            self._targeting.pop(outpath, None)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._targeting.clear()
            self._cancelled.clear()

    # --- internals ----------------------------------------------------------

    def _reject(self, from_sid: str, outpath: str, reason: str) -> None:
        peer = self._peer_by_sid(from_sid)
        if peer is None:
            return
        try:
            self._ctx.push_path_reject(
                peer,
                self._ctx.my_secondary_id,
                outpath,
                reason,
                self._ctx.our_pubkey,
            )
        except Exception:  # noqa: BLE001
            logger.exception("ReplicationReceiver: reject push raised")

    def _accept(self, from_sid: str, outpath: str) -> None:
        peer = self._peer_by_sid(from_sid)
        if peer is None:
            return
        try:
            self._ctx.push_path_accept(
                peer,
                self._ctx.my_secondary_id,
                outpath,
                self._ctx.our_pubkey,
            )
        except Exception:  # noqa: BLE001
            logger.exception("ReplicationReceiver: accept push raised")

    def _peer_by_sid(self, sid: str) -> Optional[PeerInfo]:
        for p in self._ctx.get_peers():
            if p.secondary_id == sid:
                return p
        return None

    def _do_fetch(
        self,
        from_sid: str,
        outpath: str,
        drv_path: str,
        item_class: str,
    ) -> None:
        # Pin the fetch to the offerer's peer.
        offerer = self._peer_by_sid(from_sid)
        if offerer is None:
            with self._lock:
                if self._targeting.get(outpath) == from_sid:
                    self._targeting.pop(outpath, None)
            return
        # Build a one-element placement override so fetch_from_peer
        # picks the offerer regardless of the live map.
        placements_override = {outpath: {from_sid}}
        peers_singleton = [offerer]
        try:
            source = self._ctx.fetch_from_peer(
                outpath,
                placements_override,
                peers_singleton,
                prefer=from_sid,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationReceiver: fetch_from_peer raised for %s",
                outpath,
            )
            source = None
        # Decide whether to record + cascade.
        with self._lock:
            cancelled = (
                from_sid in self._cancelled.get(outpath, set())
            )
            # Always clear targeting state.
            if self._targeting.get(outpath) == from_sid:
                self._targeting.pop(outpath, None)
            self._cancelled.get(outpath, set()).discard(from_sid)
            stopped = self._stopped
        if stopped or cancelled or source is None:
            return
        # Successful fetch: record + cascade.
        try:
            self._ctx.record_self_has(
                self._ctx.shared_fs,
                my_secondary_id=self._ctx.my_secondary_id,
                outpath=outpath,
                drv_path=drv_path,
                item_class=item_class,
                peers=self._ctx.get_peers(),
                our_pubkey=self._ctx.our_pubkey,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationReceiver: record_self_has raised for %s",
                outpath,
            )
            return
        if item_class == peer_paths.ITEM_CLASS_TOOLCHAIN:
            try:
                self._sender.push_attempt(outpath, drv_path, item_class)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ReplicationReceiver: cascade push_attempt raised"
                )


# ---------------------------------------------------------------------------
# Repair-on-death
# ---------------------------------------------------------------------------


class ReplicationRepairWorker:
    """Drives repair-on-death AND receive-side cascade push_attempts.

    Three triggers:

    * :meth:`on_peer_removed` — primary repair signal; ought to be
      wired to a framework hook (Framework Ask #4) on every node.
      Best-case latency: sub-second.
    * :meth:`on_diff` — fallback for both repair (on ``diff.removed``)
      AND cascade-on-receive (on ``diff.added`` where my secondary id
      is a new holder of a toolchain outpath). Wired to
      :meth:`PathPlacementWatcher.register_diff_callback`. Latency
      bounded by the watcher's tick (5–60 s).

    The cascade trigger is the way the build worker subprocess
    indirectly signals "I just realised a toolchain path" — it writes
    its placement gossip file via :func:`peer_paths.record_self_has`,
    the manager-process placement watcher sees the addition on its
    next refresh, and this callback fires push_attempt.

    The ``get_drv_metadata`` callable maps ``outpath -> (drv_path,
    item_class)``. It is also the filter that scopes cascade work to
    toolchain placements (returning anything other than
    ``ITEM_CLASS_TOOLCHAIN`` skips the cascade for that path); the
    default looks up the local placement file.
    """

    def __init__(
        self,
        ctx: ReplicationContext,
        sender: ReplicationSender,
        get_drv_metadata: Optional[
            Callable[[str], tuple[str, str]]
        ] = None,
    ) -> None:
        self._ctx = ctx
        self._sender = sender
        # Caller can plug in a metadata lookup (outpath -> (drv_path,
        # item_class)) for repair so the cascade carries class info
        # forward. Without it, fall back to reading the local
        # _paths_<my_sid>.jsonl file via list_self_placements; that
        # gives us the right item_class for cascade scoping in the
        # subprocess-worker → manager flow without any IPC.
        self._get_drv_metadata = get_drv_metadata or self._default_metadata_lookup

    # --- entry points -------------------------------------------------------

    def on_peer_removed(self, secondary_id: str, reason: str = "") -> None:
        """Framework signalled that *secondary_id* is dead/removed.

        Walk the current placement map and trigger one push_attempt
        per outpath where:

        * we are still a holder, AND
        * the dead peer was a holder, AND
        * after removing the dead peer, holder count drops below K.
        """
        if not secondary_id:
            return
        snapshot = self._ctx.get_placements()
        outpaths_to_repair: list[str] = []
        for outpath, holders in snapshot.items():
            if self._ctx.my_secondary_id not in holders:
                continue
            if secondary_id not in holders:
                continue
            if (len(holders) - 1) >= self._ctx.replication_k:
                continue
            outpaths_to_repair.append(outpath)
        if outpaths_to_repair:
            logger.info(
                "ReplicationRepairWorker: peer %s removed (%s); "
                "repairing %d outpaths",
                secondary_id, reason, len(outpaths_to_repair),
            )
        # Exclude the dead peer from the sender's K-accounting:
        # they're still in the placement snapshot (watcher hasn't
        # refreshed) but they don't count as a real holder.
        self._repair_for_outpaths(
            outpaths_to_repair, excludes=frozenset({secondary_id}),
        )

    def on_diff(self, diff: PlacementDiff) -> None:
        """Fallback signal from :class:`PathPlacementWatcher`.

        Two paths drive push_attempt:

        * **Repair** (``diff.removed``): for any outpath where we are
          still a holder AND total holders dropped below K, trigger
          a repair push_attempt.
        * **Cascade** (``diff.added``): for any outpath where WE just
          became a new holder AND the item_class (looked up via the
          metadata callable) is ``ITEM_CLASS_TOOLCHAIN``, trigger a
          cascade push_attempt to drive K=3 from the receive side.
          This is the indirect signal from the build-worker
          subprocess: it called ``record_self_has``, the placement
          watcher saw the addition, we fire the cascade.
        """
        snapshot = self._ctx.get_placements()
        my_sid = self._ctx.my_secondary_id

        # ----- Repair path (removals).
        if diff.removed:
            outpaths_to_repair: list[str] = []
            for outpath in diff.removed:
                holders = snapshot.get(outpath, set())
                if my_sid not in holders:
                    continue
                if len(holders) >= self._ctx.replication_k:
                    continue
                outpaths_to_repair.append(outpath)
            if outpaths_to_repair:
                logger.info(
                    "ReplicationRepairWorker: placement diff "
                    "(degraded-mode signal); repairing %d outpaths",
                    len(outpaths_to_repair),
                )
            self._repair_for_outpaths(outpaths_to_repair)

        # ----- Cascade path (additions where I'm the new holder).
        if diff.added:
            for outpath, new_sids in diff.added.items():
                if my_sid not in new_sids:
                    continue
                drv_path, item_class = self._get_drv_metadata(outpath)
                if item_class != peer_paths.ITEM_CLASS_TOOLCHAIN:
                    continue
                try:
                    self._sender.push_attempt(
                        outpath, drv_path, item_class,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "ReplicationRepairWorker: cascade push_attempt "
                        "raised for %s", outpath,
                    )

    # --- metadata lookup ----------------------------------------------------

    def _default_metadata_lookup(
        self, outpath: str,
    ) -> tuple[str, str]:
        """Read local _paths_<my_sid>.jsonl and find the most recent
        record matching *outpath*. Returns (drv_path, item_class).

        Used by the cascade path: when the placement watcher signals
        that we just gained a holder for *outpath*, we need to know
        whether it's a toolchain (push_attempt-eligible) or a common
        dep / variant (cascade out of scope). The local placement
        file is the authoritative record we wrote ourselves.

        Returns empty strings on lookup failure — the cascade then
        treats the addition as non-toolchain (no push), which is the
        safe default.
        """
        try:
            records = peer_paths.list_self_placements(
                self._ctx.shared_fs, self._ctx.my_secondary_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationRepairWorker: list_self_placements raised"
            )
            return ("", "")
        # Walk in reverse so the most recent record for *outpath* wins
        # if there are duplicates (which there shouldn't be, but the
        # JSONL is append-only).
        for rec in reversed(records):
            if rec.outpath == outpath:
                return (rec.drv_path, rec.item_class)
        return ("", "")

    # --- internals ----------------------------------------------------------

    def _repair_for_outpaths(
        self,
        outpaths: Iterable[str],
        excludes: Optional[frozenset[str]] = None,
    ) -> None:
        for outpath in outpaths:
            drv_path, item_class = self._get_drv_metadata(outpath)
            try:
                self._sender.push_attempt(
                    outpath, drv_path, item_class,
                    exclude_holders=excludes,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ReplicationRepairWorker: push_attempt raised for %s",
                    outpath,
                )

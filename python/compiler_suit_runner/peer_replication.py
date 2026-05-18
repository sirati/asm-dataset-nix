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
import queue
import random
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Optional

from compiler_suit_runner import peer_paths, peer_paths_fetch, peer_push
from compiler_suit_runner.peer_cache import PeerInfo, PlacementDiff

__all__ = [
    "DEFAULT_BROADCAST_MAX_HOP",
    "DEFAULT_OFFER_TIMEOUT_SECONDS",
    "DEFAULT_REPLICATION_K",
    "BROADCAST_ITEM_CLASS",
    "BroadcastReceiver",
    "BroadcastResult",
    "BroadcastSender",
    "ReplicationContext",
    "ReplicationReceiver",
    "ReplicationRepairWorker",
    "ReplicationSender",
]

logger = logging.getLogger(__name__)

DEFAULT_REPLICATION_K = 3
DEFAULT_OFFER_TIMEOUT_SECONDS = 1.0

# Loop-prevention cap for the broadcast gossip protocol. Even with
# broadcast_id-based dedup at the receiver, a hop cap is the cheap
# belt-and-suspenders defense against pathological topologies; an
# eight-hop fan-out is comfortably larger than any reasonable cluster
# diameter (≤ ten secondaries → ceil(log2(10)) = 4 hops). A receiver
# that sees hop_count ≥ DEFAULT_BROADCAST_MAX_HOP still records the
# path as locally-held when applicable but skips the fan-out.
DEFAULT_BROADCAST_MAX_HOP = 8

# Item class stamped on placement records written after a successful
# broadcast receive. Matches the value the originator
# (:mod:`workers.eval_worker`) writes via
# :func:`peer_paths.record_self_has` so the cluster-wide placement map
# is uniform across origin + flood-fill recipients.
BROADCAST_ITEM_CLASS = "matrix_eval_drv"


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

    The framework-handle callables (``mark_task_unfulfillable``,
    ``reinject_task``, ``update_preferred_secondaries``,
    ``lookup_task_hash_for_outpath``) default to ``None`` so the
    legacy NFS-poll fallback path stays unbroken when the primary has
    not bound them yet (e.g. on secondaries, or before
    ``suit_task.on_run_start`` has wired the ``PrimaryHandle``).
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
    # Framework PrimaryHandle bindings (consumer-side hex strings; the
    # binding layer in suit_task.on_run_start converts hex→bytes before
    # invoking the Rust API). Default None ⇒ no-op + legacy fallback.
    mark_task_unfulfillable: Optional[Callable[[str, str], None]] = field(
        repr=False, default=None,
    )
    reinject_task: Optional[Callable[[str], None]] = field(
        repr=False, default=None,
    )
    update_preferred_secondaries: Optional[
        Callable[[str, list[str]], None]
    ] = field(repr=False, default=None)
    lookup_task_hash_for_outpath: Optional[
        Callable[[str], Optional[str]]
    ] = field(repr=False, default=None)
    # Knobs
    replication_k: int = DEFAULT_REPLICATION_K
    offer_timeout_seconds: float = DEFAULT_OFFER_TIMEOUT_SECONDS
    # Q2 settle-window: debounce between cascade convergence
    # (`len(holders) >= K`) and the single `update_preferred_secondaries`
    # call. Picked at the module's default of 5 s per the wire-in plan;
    # tests can override (typically with a tiny value via the fake
    # timer factory) and production callers can knob it down for
    # latency-sensitive deployments.
    preferred_secondaries_settle_seconds: float = 5.0


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

    def __init__(
        self,
        ctx: ReplicationContext,
        *,
        timer_factory: Optional[
            Callable[[float, Callable[[], None]], "threading.Timer"]
        ] = None,
    ) -> None:
        self._ctx = ctx
        # outpath -> {target_sid: _InFlightOffer}
        self._in_flight: dict[str, dict[str, _InFlightOffer]] = {}
        # Per-outpath metadata so we can re-issue a push_attempt
        # after a reject/timeout without the caller having to remember
        # the drv_path + item_class.
        self._metadata: dict[str, tuple[str, str]] = {}  # outpath -> (drv_path, item_class)
        self._lock = threading.RLock()
        self._stopped = False
        # Q2 batched preferred_secondaries updater state.
        # outpath -> debounce Timer arming the single
        # update_preferred_secondaries call. We track per-outpath
        # rather than per-task_hash because the consumer-side cascade
        # signal we observe (on_path_have / placement diff) is
        # outpath-keyed; the bound callable resolves the toolchain
        # task_hash itself at settle time.
        self._settle_timer: dict[str, threading.Timer] = {}
        # outpath -> True once we've fired the single
        # update_preferred_secondaries call. Idempotency guard so a
        # later K+1 race or duplicate path-have doesn't refire.
        self._preferred_secondaries_fired: dict[str, bool] = {}
        # Factory hook for tests — defaults to threading.Timer so
        # production paths don't pay any extra cost. Signature mirrors
        # the threading.Timer(interval, function) ctor; the factory
        # owns daemon-setting + start().
        self._timer_factory = (
            timer_factory if timer_factory is not None
            else self._default_timer_factory
        )

    @staticmethod
    def _default_timer_factory(
        interval: float, function: Callable[[], None],
    ) -> threading.Timer:
        timer = threading.Timer(interval, function)
        timer.daemon = True
        timer.start()
        return timer

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

        When the cascade hits K for the first time this also arms the
        Q2 debounce timer so a single
        ``ReplicationContext.update_preferred_secondaries`` call fires
        once the converged set has had a chance to settle. K+1 races
        observed during the debounce just reschedule the timer with
        the freshest converged set; a drop below K cancels the timer
        so we don't publish a stale set.
        """
        with self._lock:
            # Settle our slot if applicable.
            self._release_slot(outpath, holder_sid)
            # Convergence check.
            holders = self._ctx.get_placements().get(outpath, set())
            # Note: include the freshly-broadcasting sid since the
            # placement watcher might not have refreshed yet.
            effective = set(holders) | {holder_sid}
            converged = len(effective) >= self._ctx.replication_k
            # Q2 batched preferred_secondaries — arm the debounce.
            self._arm_settle_locked(outpath, converged)
            if not converged:
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
            for timer in self._settle_timer.values():
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001 — defensive
                    pass
            self._settle_timer.clear()

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

    # --- Q2 batched preferred_secondaries -----------------------------------

    def _arm_settle_locked(self, outpath: str, converged: bool) -> None:
        """Arm or cancel the per-outpath settle timer.

        Caller must hold ``self._lock``. Behaviour:

        * converged (``len(effective_holders) >= K``): if we have not
          yet fired ``update_preferred_secondaries`` for this outpath,
          (re-)arm a fresh debounce timer. A second arm during the
          window simply replaces the previous timer with one that
          fires later — equivalent to "the freshest converged set is
          the one we publish".
        * not converged (cascade un-settled): cancel any pending
          timer; we will arm again when the cascade re-converges.
        """
        existing = self._settle_timer.get(outpath)
        if not converged:
            if existing is not None:
                try:
                    existing.cancel()
                except Exception:  # noqa: BLE001 — defensive
                    pass
                self._settle_timer.pop(outpath, None)
            return
        # Already fired this outpath → idempotent no-op. Subsequent
        # K+1 / K+2 holders don't refire (the converged set at first
        # settle is what we committed to).
        if self._preferred_secondaries_fired.get(outpath):
            return
        # Skip entirely if the framework hook is unbound (legacy path).
        if self._ctx.update_preferred_secondaries is None:
            return
        # Replace any pending timer with a fresh one anchored at "now".
        if existing is not None:
            try:
                existing.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
        timer = self._timer_factory(
            self._ctx.preferred_secondaries_settle_seconds,
            lambda: self._fire_settle(outpath),
        )
        self._settle_timer[outpath] = timer

    def _fire_settle(self, outpath: str) -> None:
        """Timer-callback: publish the converged holder set once.

        Resolves the live holder set at fire time (not at arm time);
        the cascade may have grown to K+1 since the arm, and we want
        the freshest snapshot. Bound callable receives the
        toolchain task_hash (hex) + sorted holder list.
        """
        with self._lock:
            if self._stopped:
                return
            # Idempotency guard: a manual fire racing the timer.
            if self._preferred_secondaries_fired.get(outpath):
                self._settle_timer.pop(outpath, None)
                return
            updater = self._ctx.update_preferred_secondaries
            lookup = self._ctx.lookup_task_hash_for_outpath
            holders = sorted(
                self._ctx.get_placements().get(outpath, set())
            )
            # Drop the timer slot now so a re-arm after fire is fresh.
            self._settle_timer.pop(outpath, None)
            # Mark fired BEFORE invoking the bound callable so a
            # callable that itself triggers a path-have can't recurse.
            self._preferred_secondaries_fired[outpath] = True
        if updater is None:
            return
        # Resolve task_hash. The lookup may legitimately return None
        # (outpath not in the manifest's toolchain set — e.g. a
        # common_dep cascade that happens to converge); in that case
        # we silently skip.
        task_hash_hex: Optional[str] = None
        if lookup is not None:
            try:
                task_hash_hex = lookup(outpath)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ReplicationSender: lookup_task_hash_for_outpath "
                    "raised for %s", outpath,
                )
                return
        if not task_hash_hex:
            return
        if len(holders) < self._ctx.replication_k:
            # Settle conditions un-met by fire time (cascade dropped
            # mid-debounce after we set fired=True). Roll back the
            # fired flag so a re-converge can fire again.
            with self._lock:
                self._preferred_secondaries_fired.pop(outpath, None)
            return
        try:
            updater(task_hash_hex, list(holders))
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationSender: update_preferred_secondaries raised "
                "for outpath=%s task_hash=%s", outpath, task_hash_hex,
            )


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
          a repair push_attempt. With the Q4 ``peer_lifecycle_listener``
          wired, the framework's keepalive-miss / fatal-error hook
          races this diff signal — the INFO log line below lets the
          operator see whether the framework hook or the diff polling
          caught the death first. The diff path stays registered as a
          backstop for clusters where the framework hook hasn't been
          delivered yet (and for cascade-on-addition; see below).
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
                # INFO so the operator can correlate diff vs. Q4
                # framework hook for the same removal.
                logger.info(
                    "ReplicationRepairWorker: diff-callback caught "
                    "removal (backstop signal, races Q4 framework "
                    "hook); repairing %d outpaths",
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
        excludes = excludes or frozenset()
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
            # Q1 wire-in: after the repair attempt, if NO live holder
            # remains (the dead peer(s) excluded), the toolchain is
            # effectively gone from the cluster. Signal the framework
            # so it can transition the toolchain task to Unfulfillable
            # and cascade-block dependents. Skip silently when the
            # framework hook is unbound (legacy NFS-poll fallback).
            self._maybe_mark_unfulfillable(outpath, excludes)

    def _maybe_mark_unfulfillable(
        self, outpath: str, excludes: frozenset[str],
    ) -> None:
        """Emit ``mark_task_unfulfillable`` when this outpath has zero
        live holders post-repair.

        Conditions (all must hold):

        * ``ReplicationContext.mark_task_unfulfillable`` bound;
        * ``ReplicationContext.lookup_task_hash_for_outpath`` resolves
          to a task hash (i.e. this outpath belongs to a toolchain
          TaskInfo — common_dep / variant fall-through skips the
          signal);
        * ``len(live_holders) == 0`` where live = placement-snapshot
          minus ``excludes`` (the just-removed-dead-peer set).
        """
        ctx = self._ctx
        if ctx.mark_task_unfulfillable is None:
            return
        if ctx.lookup_task_hash_for_outpath is None:
            return
        placements = ctx.get_placements()
        holders = set(placements.get(outpath, set())) - set(excludes)
        if holders:
            return  # at least one live holder; nothing permanent here
        try:
            task_hash_hex = ctx.lookup_task_hash_for_outpath(outpath)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationRepairWorker: lookup_task_hash_for_outpath "
                "raised for %s", outpath,
            )
            return
        if not task_hash_hex:
            return
        # Reason format contract (Q3 holding_matcher consumer):
        #   f"toolchain outpath={outpath} dead_holders={sorted(last_known)}"
        # TODO: swap to `holding_matcher.UNFULFILLABLE_REASON_TEMPLATE`
        # once #70 (holding_matcher.py) lands so the format definition
        # lives in exactly one place.
        last_known = sorted(excludes)
        reason = (
            f"toolchain outpath={outpath} dead_holders={last_known}"
        )
        try:
            ctx.mark_task_unfulfillable(task_hash_hex, reason)
        except Exception:  # noqa: BLE001
            logger.exception(
                "ReplicationRepairWorker: mark_task_unfulfillable raised "
                "for outpath=%s task_hash=%s", outpath, task_hash_hex,
            )


# ---------------------------------------------------------------------------
# Broadcast sender — Phase 0 drv flood-fill
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BroadcastResult:
    """Outcome of a single broadcast fan-out.

    Mirrors the tuple returned by
    :func:`peer_push.fan_out_broadcast_drv`: a count of successful
    peers (any non-``None`` response, including ``{"dedup": true}``),
    a count of failed peers (transport-level failures), and the list
    of failed peer URLs so callers can re-try or log.
    """

    broadcast_id: str
    success_count: int
    fail_count: int
    failed_peers: tuple[str, ...]


@dataclass
class _BroadcastJob:
    """One enqueued broadcast awaiting dispatch."""

    broadcast_id: str
    path: str
    size: int
    item_class: Optional[str]


class BroadcastSender:
    """Originator-side fan-out for ``path-broadcast-offer`` (drv flood).

    Unlike :class:`ReplicationSender`, the broadcast protocol carries
    no K accounting, no preferred-secondary selection, and no
    "already-targeted" reject coordination: every broadcast goes to
    every peer at ``hop_count=0`` and each receiver dedupes via
    ``broadcast_id`` and forwards once. The sender's job is therefore
    simply:

    * Mint a fresh UUID4 ``broadcast_id`` per :meth:`enqueue_broadcast`
      call and return it to the caller without blocking.
    * Dispatch the fan-out on a single worker thread (consumer of an
      internal queue) so the caller never waits on HTTP.
    * Record per-broadcast results so callers can
      :meth:`wait_for_completion` and inspect ``(success, fail,
      failed_peers)``.

    Lifecycle mirrors the rest of the module: construct → no explicit
    ``start()`` needed (the worker thread is spawned on first use) →
    :meth:`stop` for clean shutdown. The worker thread is a daemon so
    a forgotten ``stop()`` cannot block process exit.

    Failure handling: an exception raised by ``fan_out_broadcast_drv``
    is caught in the worker (the job is marked as full-fail with an
    empty failed-peers list), so a single bad broadcast cannot crash
    the worker thread.
    """

    def __init__(
        self,
        self_peer_id: str,
        peer_url_provider: Callable[[], list[str]],
        our_pubkey: str = "",
        fan_out: Callable[..., tuple[int, int, list[str]]] = (
            peer_push.fan_out_broadcast_drv
        ),
        timeout: float = peer_push.DEFAULT_PUSH_TIMEOUT,
    ) -> None:
        self._self_peer_id = str(self_peer_id)
        self._peer_url_provider = peer_url_provider
        self._our_pubkey = str(our_pubkey)
        self._fan_out = fan_out
        self._timeout = float(timeout)
        # Unbounded — broadcasts are small (10–50 KB drv files), the
        # cluster is small (≤ tens of peers), and back-pressure here
        # would block the eval-worker thread anyway.
        self._queue: queue.Queue[Optional[_BroadcastJob]] = queue.Queue()
        self._results: dict[str, BroadcastResult] = {}
        # Per-broadcast events so multiple waiters can ``wait_for_completion``
        # on different ids concurrently without polling.
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    # --- public API ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker thread if not already running.

        Idempotent. Most callers do not need to call this explicitly;
        :meth:`enqueue_broadcast` calls it lazily.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError(
                    "BroadcastSender: cannot start after stop()"
                )
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="BroadcastSender",
                daemon=True,
            )
            self._thread.start()

    def enqueue_broadcast(
        self,
        path: str,
        size: int,
        item_class: Optional[str] = None,
    ) -> str:
        """Enqueue a fan-out and return the broadcast_id immediately.

        Non-blocking. The worker thread picks the job up and calls
        :func:`peer_push.fan_out_broadcast_drv` with the current peer
        URL list and ``hop_count=0`` (originator).
        """
        broadcast_id = uuid.uuid4().hex
        job = _BroadcastJob(
            broadcast_id=broadcast_id,
            path=str(path),
            size=int(size),
            item_class=item_class,
        )
        with self._lock:
            if self._stopped:
                raise RuntimeError(
                    "BroadcastSender: enqueue after stop()"
                )
            self._events[broadcast_id] = threading.Event()
        self.start()
        self._queue.put(job)
        return broadcast_id

    def wait_for_completion(
        self,
        broadcast_id: str,
        timeout: Optional[float] = None,
    ) -> Optional[BroadcastResult]:
        """Block until the named broadcast finishes (or *timeout*).

        Returns the :class:`BroadcastResult` recorded by the worker,
        or ``None`` if the wait timed out before the fan-out
        completed. Unknown ``broadcast_id`` also returns ``None``.
        """
        with self._lock:
            event = self._events.get(broadcast_id)
        if event is None:
            return None
        if not event.wait(timeout=timeout):
            return None
        with self._lock:
            return self._results.get(broadcast_id)

    def stop(self) -> None:
        """Signal the worker thread to exit and join it briefly.

        Idempotent. Any waiters blocked on :meth:`wait_for_completion`
        for unresolved broadcasts are NOT released — they will time
        out per their own timeout argument. (In practice ``stop()`` is
        called during teardown after all eval work has completed.)
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        # Sentinel wakes the worker even when the queue is empty.
        self._queue.put(None)
        if thread is not None:
            thread.join(timeout=2.0)

    # --- internals ----------------------------------------------------------

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                # Sentinel; either stop() was called or a spurious None
                # slipped through. Re-check the stopped flag.
                with self._lock:
                    if self._stopped:
                        return
                continue
            self._dispatch(job)

    def _dispatch(self, job: _BroadcastJob) -> None:
        try:
            peer_urls = list(self._peer_url_provider())
        except Exception:  # noqa: BLE001
            logger.exception(
                "BroadcastSender: peer_url_provider raised for %s",
                job.broadcast_id,
            )
            peer_urls = []
        # Empty peer list is a legitimate state (single-secondary
        # cluster, all peers down) — ack as a 0/0 success.
        if not peer_urls:
            self._record_result(job.broadcast_id, 0, 0, [])
            return
        try:
            success, fail, failed = self._fan_out(
                peer_urls=peer_urls,
                path=job.path,
                size=job.size,
                broadcast_id=job.broadcast_id,
                origin_peer_id=self._self_peer_id,
                hop_count=0,
                our_pubkey=self._our_pubkey,
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "BroadcastSender: fan_out raised for %s (path=%s)",
                job.broadcast_id, job.path,
            )
            # Treat as full failure but DO record + signal so waiters
            # don't hang forever. failed_peers is empty because we
            # don't know which target(s) actually raised.
            self._record_result(
                job.broadcast_id, 0, len(peer_urls), peer_urls,
            )
            return
        self._record_result(
            job.broadcast_id, int(success), int(fail), list(failed),
        )

    def _record_result(
        self,
        broadcast_id: str,
        success: int,
        fail: int,
        failed_peers: list[str],
    ) -> None:
        result = BroadcastResult(
            broadcast_id=broadcast_id,
            success_count=int(success),
            fail_count=int(fail),
            failed_peers=tuple(failed_peers),
        )
        with self._lock:
            self._results[broadcast_id] = result
            event = self._events.get(broadcast_id)
        if event is not None:
            event.set()


# ---------------------------------------------------------------------------
# Broadcast receiver — Phase 0 drv flood-fill consumer
# ---------------------------------------------------------------------------


@dataclass
class _BroadcastFanoutJob:
    """One enqueued fan-out hop awaiting dispatch on the worker thread."""

    path: str
    size: int
    broadcast_id: str
    origin_peer_id: str
    hop_count: int


class BroadcastReceiver:
    """Consumer for ``/peer/path-broadcast-offer`` notifications.

    The :class:`peer_push.PeerPushServer` calls
    :meth:`on_broadcast_offer` from its HTTP handler thread when a
    distinct ``broadcast_id`` arrives (the server itself dedups by id,
    so this class only ever sees first-time broadcasts). The return
    value tells the push server whether the recipient now holds the
    path: the JSON response body carries that bool, and the originator
    side uses it for placement-map accounting.

    Logic per receive:

    1. Short-circuit when ``is_path_locally_valid(path)`` is True —
       record a placement entry (so peers learn we have it) and
       return True. No fetch, no fan-out.
    2. Otherwise look up the origin peer's URL via the
       ``peer_url_provider`` mapping. Unknown origin → return False
       and skip fetch (we have no source to pull from).
    3. Call ``fetch_path_from_peer(path, origin_peer_url)``. On
       success, record the placement and enqueue a fan-out hop to
       every OTHER peer (minus self, minus origin). Return True.
    4. On fetch failure: log warn, return False. Do NOT fan-out and
       do NOT record — we don't actually have the path.

    The fan-out itself runs on a single daemon worker thread fed by an
    internal queue (mirrors :class:`BroadcastSender`). The HTTP
    handler thread is therefore never blocked on N peer POSTs; it
    only enqueues a job and returns.

    Hop-count protection: if the inbound ``hop_count >=
    DEFAULT_BROADCAST_MAX_HOP`` we still fetch/record but never
    fan out, even on a fresh ``broadcast_id``. The broadcast_id
    dedup at the server is the primary loop guard; the hop cap is the
    cheap belt-and-suspenders defense for pathological topologies.
    """

    def __init__(
        self,
        self_peer_id: str,
        peer_url_provider: Callable[[], dict[str, str]],
        is_path_locally_valid: Callable[[str], bool],
        fetch_path_from_peer: Callable[[str, str], bool],
        our_pubkey: str = "",
        *,
        shared_fs: Optional[pathlib.Path] = None,
        record_self_has: Optional[Callable[..., None]] = None,
        fan_out: Callable[..., tuple[int, int, list[str]]] = (
            peer_push.fan_out_broadcast_drv
        ),
        max_hop: int = DEFAULT_BROADCAST_MAX_HOP,
        timeout: float = peer_push.DEFAULT_PUSH_TIMEOUT,
    ) -> None:
        """Construct a receiver.

        Parameters
        ----------
        self_peer_id :
            This secondary's id. Used both to subtract self from the
            fan-out target list and as the ``my_secondary_id`` written
            into placement records.
        peer_url_provider :
            Zero-arg callable returning ``{peer_id: push_url}`` for
            every currently-known peer (INCLUDING self — subtraction
            happens at fan-out time, per the broadcast contract). Push
            URLs are the bare base (e.g. ``http://node3:6000``); the
            ``/peer/path-broadcast-offer`` suffix is appended by the
            fan-out helper.
        is_path_locally_valid :
            ``Callable[[path], bool]`` — typically
            :func:`peer_paths_fetch.is_path_locally_valid`.
        fetch_path_from_peer :
            ``Callable[[path, origin_peer_url], bool]`` returning True
            on a successful pull. The receiver is intentionally agnostic
            to which URL scheme the fetcher needs (push port vs
            harmonia port); the caller wires that translation.
        our_pubkey :
            Cluster shared pubkey for the ``X-Cluster-PubKey`` header
            on fan-out posts.
        shared_fs, record_self_has :
            Together feed the post-receive placement record. When
            ``shared_fs`` is None or ``record_self_has`` is None,
            placement-record writes are skipped (handy for tests and
            for the degraded-mode path where the placement gossip
            file is not yet bootstrapped). Default ``record_self_has``
            is :func:`peer_paths.record_self_has`.
        fan_out :
            Injection seam for tests; defaults to
            :func:`peer_push.fan_out_broadcast_drv`.
        max_hop :
            Loop guard; see module-level
            ``DEFAULT_BROADCAST_MAX_HOP``.
        timeout :
            Per-target HTTP timeout for fan-out POSTs.
        """
        self._self_peer_id = str(self_peer_id)
        self._peer_url_provider = peer_url_provider
        self._is_path_locally_valid = is_path_locally_valid
        self._fetch_path_from_peer = fetch_path_from_peer
        self._our_pubkey = str(our_pubkey)
        self._fan_out = fan_out
        self._max_hop = int(max_hop)
        self._timeout = float(timeout)
        self._shared_fs = shared_fs
        self._record_self_has = (
            record_self_has if record_self_has is not None
            else peer_paths.record_self_has
        )
        # Fan-out worker queue; unbounded for the same reason as
        # BroadcastSender (broadcasts are small, the cluster is small,
        # back-pressure on the HTTP handler thread would just block
        # the push server).
        self._queue: queue.Queue[Optional[_BroadcastFanoutJob]] = queue.Queue()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    # --- public API ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the fan-out worker thread if not already running.

        Idempotent. Most callers do not need to call this explicitly;
        :meth:`on_broadcast_offer` calls it lazily.
        """
        with self._lock:
            if self._stopped:
                raise RuntimeError(
                    "BroadcastReceiver: cannot start after stop()"
                )
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="BroadcastReceiver",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Signal the worker thread to exit and join it briefly.

        Idempotent. Subsequent :meth:`on_broadcast_offer` calls reject
        (return False) so a torn-down receiver cannot accidentally
        accept new flood-fill traffic.
        """
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        # Sentinel wakes the worker even when the queue is empty.
        self._queue.put(None)
        if thread is not None:
            thread.join(timeout=2.0)

    def on_broadcast_offer(
        self,
        path: str,
        size: int,
        origin_peer_id: str,
        broadcast_id: str,
        hop_count: int,
    ) -> bool:
        """Handle one path-broadcast-offer event.

        Wire-shape matches the callable contract
        :class:`peer_push.PeerPushServer` invokes. Returns True iff
        the recipient now holds the path (either already had it or
        successfully fetched it); False otherwise. The originator side
        consults that bool for placement-map accounting.

        See class docstring for the four-branch decision tree.
        """
        with self._lock:
            if self._stopped:
                return False
        if not path:
            return False

        # ── Branch 1: already-have short-circuit ─────────────────────
        try:
            already_have = bool(self._is_path_locally_valid(path))
        except Exception:  # noqa: BLE001 — defensive; treat as miss
            logger.exception(
                "BroadcastReceiver: is_path_locally_valid raised for %s",
                path,
            )
            already_have = False
        if already_have:
            # Record so peers learn we hold it; no fetch, no fan-out.
            self._maybe_record(path)
            logger.debug(
                "BroadcastReceiver: %s already local, accepting %s",
                path, broadcast_id,
            )
            return True

        # ── Branch 2: resolve origin URL ─────────────────────────────
        url_map = self._safe_url_map()
        origin_url = url_map.get(origin_peer_id)
        if not origin_url:
            logger.warning(
                "BroadcastReceiver: unknown origin peer %s for "
                "broadcast %s (path=%s); rejecting",
                origin_peer_id, broadcast_id, path,
            )
            return False

        # ── Branch 3: fetch from origin ──────────────────────────────
        try:
            fetched = bool(self._fetch_path_from_peer(path, origin_url))
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "BroadcastReceiver: fetch_path_from_peer raised for %s",
                path,
            )
            fetched = False
        if not fetched:
            logger.warning(
                "BroadcastReceiver: fetch failed for %s from %s "
                "(broadcast %s); not fanning out",
                path, origin_url, broadcast_id,
            )
            return False

        # Record placement so peers learn we now hold it.
        self._maybe_record(path)

        # ── Branch 4: cascade fan-out to other peers ────────────────
        if hop_count >= self._max_hop:
            logger.info(
                "BroadcastReceiver: hop_count %d ≥ max_hop %d for "
                "broadcast %s; skipping cascade",
                hop_count, self._max_hop, broadcast_id,
            )
            return True

        self._enqueue_fanout(
            _BroadcastFanoutJob(
                path=path,
                size=int(size),
                broadcast_id=broadcast_id,
                origin_peer_id=origin_peer_id,
                hop_count=int(hop_count) + 1,
            )
        )
        return True

    # --- internals ----------------------------------------------------------

    def _maybe_record(self, path: str) -> None:
        """Record placement when shared_fs + record callable are present.

        The placement record carries
        ``item_class == BROADCAST_ITEM_CLASS`` so it lines up with what
        the originator (eval_worker) writes; the cluster-wide
        placement aggregate then shows uniform records for both
        origin + flood-fill recipients of the same drv.
        """
        if self._shared_fs is None or self._record_self_has is None:
            return
        if not self._self_peer_id:
            return
        try:
            self._record_self_has(
                self._shared_fs,
                my_secondary_id=self._self_peer_id,
                outpath=path,
                drv_path=path,
                item_class=BROADCAST_ITEM_CLASS,
            )
        except Exception:  # noqa: BLE001 — placement is best-effort
            logger.exception(
                "BroadcastReceiver: record_self_has raised for %s",
                path,
            )

    def _safe_url_map(self) -> dict[str, str]:
        try:
            url_map = self._peer_url_provider() or {}
        except Exception:  # noqa: BLE001 — defensive
            logger.exception(
                "BroadcastReceiver: peer_url_provider raised"
            )
            return {}
        # Normalize to a dict[str, str]; tolerate the (rare) case
        # where a provider hands back ``items()``-style iterables.
        try:
            return dict(url_map)
        except (TypeError, ValueError):
            logger.exception(
                "BroadcastReceiver: peer_url_provider yielded a "
                "non-mappable %r", type(url_map).__name__,
            )
            return {}

    def _enqueue_fanout(self, job: _BroadcastFanoutJob) -> None:
        """Push a fan-out hop onto the worker queue (lazy start)."""
        try:
            self.start()
        except RuntimeError:
            # start-after-stop; the receiver is winding down, drop the
            # cascade hop on the floor (best-effort gossip).
            return
        self._queue.put(job)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                with self._lock:
                    if self._stopped:
                        return
                continue
            self._dispatch(job)

    def _dispatch(self, job: _BroadcastFanoutJob) -> None:
        url_map = self._safe_url_map()
        # Subtract self + origin at fan-out time (the contract is
        # "provider returns full set"); also dedup URLs in case the
        # provider repeats them.
        targets: list[str] = []
        seen: set[str] = set()
        for pid, url in url_map.items():
            if not url or pid == self._self_peer_id:
                continue
            if pid == job.origin_peer_id:
                continue
            if url in seen:
                continue
            seen.add(url)
            targets.append(url)
        if not targets:
            logger.debug(
                "BroadcastReceiver: no fan-out targets for broadcast "
                "%s after subtracting self+origin",
                job.broadcast_id,
            )
            return
        try:
            success, fail, failed = self._fan_out(
                peer_urls=targets,
                path=job.path,
                size=job.size,
                broadcast_id=job.broadcast_id,
                origin_peer_id=self._self_peer_id,
                hop_count=job.hop_count,
                our_pubkey=self._our_pubkey,
                timeout=self._timeout,
            )
        except Exception:  # noqa: BLE001 — worker stays alive
            logger.exception(
                "BroadcastReceiver: fan_out raised for broadcast %s",
                job.broadcast_id,
            )
            return
        logger.debug(
            "BroadcastReceiver: cascade for %s → %d ok, %d fail "
            "(targets=%d, hop=%d, failed=%s)",
            job.broadcast_id, success, fail, len(targets),
            job.hop_count, failed,
        )

"""Cluster-wide path-placement gossip.

Each secondary records every store path it has realised into a
per-secondary append-only JSONL file at
``${shared_fs}/peers/_paths_<secondary_id>.jsonl``. Peers aggregate the
files via :class:`peer_cache.PathPlacementWatcher` to build the
``dict[outpath, set[secondary_id]]`` placement map. Workers query
the map at runtime to pick a single peer for targeted
``nix copy --from http://...`` transfers (see
:mod:`compiler_suit_runner.peer_paths_fetch`).

Wire shape — one JSON record per line::

    {
        "secondary_id": "<sid>",
        "outpath":      "/nix/store/<hash>-name",
        "drv_path":     "/nix/store/<hash>-name.drv",
        "item_class":   "toolchain" | "common_dep" | "variant",
        "ts":           1715000000.0
    }

JSONL with ``O_APPEND`` + single ``write()`` is the natural fit:
- Kernel serialises concurrent appenders on a regular file so
  multiple workers in the same secondary's process group can
  ``record_self_has`` without coordination.
- Readers (:class:`PathPlacementWatcher`) read the whole file each
  tick; partial lines (worker still mid-write) are tolerated via
  per-line ``json.loads`` skip-on-error.
- No NFS-atomic-replace dance needed: append-only files don't have
  the partial-write problem that ``_atomic_write_json`` solves for
  full-file overwrites.

The push side (:func:`peer_push.fan_out_path_have`) is best-effort —
the polling watcher is the safety net for missed pushes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import time
from typing import Iterable, Optional

from compiler_suit_runner import peer_push
from compiler_suit_runner.peer_cache import (
    PATHS_FILE_PREFIX,
    PeerInfo,
    _peers_dir,
)

__all__ = [
    "PathPlacement",
    "ITEM_CLASS_TOOLCHAIN",
    "ITEM_CLASS_COMMON_DEP",
    "ITEM_CLASS_VARIANT",
    "ITEM_CLASS_PHASE0_EVAL_DRV",
    "paths_file_for",
    "record_self_has",
    "list_self_placements",
]

logger = logging.getLogger(__name__)


# Stable item-class strings; matches the values build_worker writes
# into BuildWorkerResult.item_class for these phase-2 / phase-3 items.
# Kept here (not in :mod:`build_worker`) so test code can reference
# them without importing the build worker.
ITEM_CLASS_TOOLCHAIN = "toolchain"
ITEM_CLASS_COMMON_DEP = "common_dep"
ITEM_CLASS_VARIANT = "variant"
# Phase 0 distributed-eval drv flood-fill. Set by the broadcast
# receive path in :mod:`peer_push` when a ``/peer/path-broadcast-offer``
# is accepted; matches the broadcast tag used by
# :mod:`workers.eval_worker` so T21's holder-count assertion can
# scope to phase-0-only records.
ITEM_CLASS_PHASE0_EVAL_DRV = "phase0_eval_drv"


@dataclasses.dataclass(frozen=True)
class PathPlacement:
    """One placement record: a peer claims to have a store path.

    ``drv_path`` and ``item_class`` are diagnostic — workers fetch by
    ``outpath`` alone. The drv is logged on candidate selection to
    make debugging "why did peer X have this path" tractable; the
    item_class lets the watcher rank classes if we ever need to.
    """

    outpath: str
    secondary_id: str
    drv_path: str = ""
    item_class: str = ""


def paths_file_for(
    shared_fs: pathlib.Path, secondary_id: str
) -> pathlib.Path:
    """Return the path of the per-secondary placement gossip file."""
    return _peers_dir(shared_fs) / f"{PATHS_FILE_PREFIX}{secondary_id}.jsonl"


def _append_record_line(target: pathlib.Path, record: dict) -> None:
    """Append ``record`` as one JSON line to ``target``.

    Uses ``O_APPEND`` + a single ``write()`` so the kernel serialises
    concurrent appenders. The newline is part of the same write so
    readers never see a half-line (and the line-based watcher reader
    drops the partial line anyway via ``splitlines()``).
    """
    payload = json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def record_self_has(
    shared_fs: pathlib.Path,
    *,
    my_secondary_id: str,
    outpath: str,
    drv_path: str,
    item_class: str,
    peers: Optional[Iterable[PeerInfo]] = None,
    our_pubkey: Optional[str] = None,
) -> None:
    """Record that this secondary has ``outpath`` and notify peers.

    Appends a :class:`PathPlacement` JSON line to
    ``peers/_paths_<my_secondary_id>.jsonl`` (atomic on Linux), then
    fan-outs a ``path-have`` push to every reachable peer so their
    :class:`peer_cache.PathPlacementWatcher` sees the new path within
    a single refresh instead of one full poll tick.

    Push delivery is best-effort: a peer we can't reach learns of
    the path on the next NFS poll. The placement file is the source
    of truth — if the file write fails the function raises (the
    caller's build artifact would otherwise be invisible to the
    rest of the cluster).

    ``peers`` and ``our_pubkey`` may be omitted (e.g. in tests) — in
    that case only the local file is updated, no push is issued.
    """
    if not outpath:
        raise ValueError("record_self_has: outpath must be non-empty")
    if not my_secondary_id:
        raise ValueError("record_self_has: my_secondary_id must be non-empty")

    target = paths_file_for(shared_fs, my_secondary_id)
    record = {
        "secondary_id": my_secondary_id,
        "outpath": outpath,
        "drv_path": drv_path,
        "item_class": item_class,
        "ts": time.time(),
    }
    _append_record_line(target, record)

    if peers is None or our_pubkey is None:
        return
    peer_list = [p for p in peers if p.secondary_id != my_secondary_id]
    if not peer_list:
        return
    try:
        peer_push.fan_out_path_have(
            peer_list,
            my_secondary_id=my_secondary_id,
            outpath=outpath,
            drv_path=drv_path,
            item_class=item_class,
            our_pubkey=our_pubkey,
        )
    except Exception:  # noqa: BLE001 - push is best-effort
        logger.exception(
            "record_self_has: push fan-out failed (file is still authoritative)"
        )


def list_self_placements(
    shared_fs: pathlib.Path, secondary_id: str
) -> list[PathPlacement]:
    """Return the placement records this secondary has written.

    Primarily for tests + diagnostics; the live placement aggregate
    used by workers comes from :class:`PathPlacementWatcher`, not
    this function.
    """
    target = paths_file_for(shared_fs, secondary_id)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out: list[PathPlacement] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        outpath = rec.get("outpath")
        sid = rec.get("secondary_id")
        if not isinstance(outpath, str) or not isinstance(sid, str):
            continue
        out.append(
            PathPlacement(
                outpath=outpath,
                secondary_id=sid,
                drv_path=str(rec.get("drv_path", "")),
                item_class=str(rec.get("item_class", "")),
            )
        )
    return out

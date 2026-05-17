"""Targeted peer-to-peer store transfers.

The federated-substituters approach (every secondary lists every
peer's harmonia URL in ``extra-substituters``) has two well-known
failure modes: every ``.narinfo`` lookup fans out to every peer (slow
peers tail-latency every build), and an occasional truncated NAR
surfaces late and inconsistently. Both are mitigated by issuing
*targeted* fetches from a single peer at a time, picked from the
cluster-wide placement map (see
:mod:`compiler_suit_runner.peer_paths`).

This module provides :func:`fetch_from_peer`: one path, one peer per
attempt, ``nix copy --from http://<host>:<port> --no-check-sigs
<outpath>``. On HTTP error / timeout / truncated NAR the function
tries the next candidate; on exhaustion it returns ``None`` and the
caller decides whether to fall back to nix's native substituter
resolution or fail.

``nix copy --from <url>`` already restricts the source to the
explicit URL — unlike ``nix build``, the ``copy`` subcommand does
NOT consult ``extra-substituters`` from nix.conf, so the fanout the
new design was avoiding doesn't apply here. ``--no-check-sigs``
keeps the fetch robust to startup races where the receiving
secondary's ``trusted-public-keys`` haven't been synced yet — the
auth boundary is the cluster pubkey header on the harmonia push
side, not the in-store signature.

Subprocess invocation is dependency-injected via ``run_subprocess``
(same seam as :mod:`compiler_suit_runner.preflight`) so tests stay
hermetic.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from collections.abc import Callable
from typing import Iterable, Optional

from compiler_suit_runner.peer_cache import PeerInfo

__all__ = [
    "RunSubprocess",
    "PRIMARY_CANDIDATE_ID",
    "fetch_from_peer",
    "is_path_locally_valid",
    "nix_copy_from_url",
]

logger = logging.getLogger(__name__)


# Synthetic candidate id used by callers that want to prefer the
# submitter/primary (which always carries every toolchain outpath in
# its store post-preflight) over a random peer.
PRIMARY_CANDIDATE_ID = "submitter"


# argv -> (stdout_bytes, stderr_bytes, returncode). Matches the seam
# used in :mod:`compiler_suit_runner.preflight`.
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


_NIX_BASE_CMD: tuple[str, ...] = (
    "nix",
    "--extra-experimental-features",
    "nix-command flakes",
)


def is_path_locally_valid(
    outpath: str, *, run_subprocess: Optional[RunSubprocess] = None
) -> bool:
    """Return True iff ``outpath`` is already a valid local store path.

    Wraps ``nix path-info <outpath>`` (non-recursive, single path).
    A valid path on disk -> returncode 0; any other state (missing,
    invalid) -> non-zero. Errors in the nix CLI itself (binary
    missing, daemon down) also surface as False; the caller treats
    them the same as "not local, must fetch".
    """
    runner = run_subprocess or _default_run_subprocess
    cmd = [*_NIX_BASE_CMD, "path-info", outpath]
    _stdout, _stderr, rc = runner(cmd)
    return rc == 0


def _shuffle_key(secondary_id: str, outpath: str) -> int:
    """Deterministic shuffle ordering key for a (sid, outpath) pair.

    SHA-1 prefix of ``sid|outpath`` as an int. Stable across runs so
    two secondaries fetching the same outpath converge on the same
    candidate order (avoids hammering one peer with N concurrent
    fetches when N secondaries hit the same cache miss in lockstep
    -- bad outcome only avoided by deterministic, *per-outpath*
    spread of load).
    """
    h = hashlib.sha1(f"{secondary_id}|{outpath}".encode("utf-8"))
    return int.from_bytes(h.digest()[:8], "big")


def _order_candidates(
    candidate_ids: set[str],
    peers: list[PeerInfo],
    *,
    prefer: Optional[str],
    outpath: str,
) -> list[PeerInfo]:
    """Order candidates: prefer → primary → deterministic shuffle.

    Only candidates that are both in the placement set AND resolvable
    to a PeerInfo are returned. Unresolvable ids (peer dropped between
    map read and fetch) are silently filtered.
    """
    peers_by_id = {p.secondary_id: p for p in peers}
    seen: set[str] = set()
    ordered: list[PeerInfo] = []

    def _push(sid: str) -> None:
        if sid in seen:
            return
        seen.add(sid)
        peer = peers_by_id.get(sid)
        if peer is None:
            return
        ordered.append(peer)

    if prefer is not None and prefer in candidate_ids:
        _push(prefer)
    if PRIMARY_CANDIDATE_ID in candidate_ids:
        _push(PRIMARY_CANDIDATE_ID)
    remaining = sorted(
        candidate_ids - seen,
        key=lambda sid: _shuffle_key(sid, outpath),
    )
    for sid in remaining:
        _push(sid)
    return ordered


def fetch_from_peer(
    outpath: str,
    placements: dict[str, set[str]],
    peers: Iterable[PeerInfo],
    *,
    prefer: Optional[str] = None,
    run_subprocess: Optional[RunSubprocess] = None,
    check_local: bool = True,
) -> Optional[str]:
    """Fetch ``outpath`` from one peer via targeted ``nix copy --from``.

    Returns the ``secondary_id`` of the peer the path was copied from
    on success, or ``None`` if no candidate succeeded.

    Strategy:

    1. (Optional) Short-circuit when the path is already locally
       valid via :func:`is_path_locally_valid`; the caller can opt
       out with ``check_local=False`` (e.g. when it has already
       checked).
    2. Look up the candidate set in ``placements[outpath]``. Empty
       set -> ``None`` (the path is not known to any peer; let nix's
       native substituter resolution handle it).
    3. Order candidates via :func:`_order_candidates`. For each, run
       ``nix copy --from http://<host>:<port> --no-check-sigs
       <outpath>`` and return on first success. ``nix copy --from``
       only reads from the explicit URL (no consultation of
       ``extra-substituters``), so the targeted-fetch contract is
       upheld by the URL pinning alone.
    4. Failures (HTTP error, timeout, truncated NAR) are logged at
       debug; the explicit URL makes each failure isolated and
       attributable.
    """
    if check_local and is_path_locally_valid(outpath, run_subprocess=run_subprocess):
        return None  # nothing to do; not "failure"

    candidate_ids = placements.get(outpath, set())
    if not candidate_ids:
        return None

    peers_list = list(peers)
    ordered = _order_candidates(
        candidate_ids, peers_list, prefer=prefer, outpath=outpath,
    )
    if not ordered:
        return None

    runner = run_subprocess or _default_run_subprocess
    for candidate in ordered:
        url = candidate.substituter_url()
        # ``nix copy --from <url>`` already restricts the source to
        # the explicit URL — there's no fanout to ``extra-substituters``
        # from nix.conf the way ``nix build`` would do. The
        # ``--no-substituters`` flag does NOT exist on the ``copy``
        # subcommand (only on ``build``/``store``); passing it here
        # would yield ``error: unrecognised flag '--no-substituters'``.
        # ``--no-check-sigs`` IS valid and is what keeps the fetch
        # robust to startup races where ``trusted-public-keys`` haven't
        # synced yet.
        cmd = [
            *_NIX_BASE_CMD,
            "copy",
            "--from", url,
            "--no-check-sigs",
            outpath,
        ]
        logger.debug(
            "fetch_from_peer: trying %s for %s", candidate.secondary_id, outpath,
        )
        stdout, stderr, rc = runner(cmd)
        if rc == 0:
            logger.info(
                "fetch_from_peer: %s <- %s (via %s)",
                outpath, candidate.secondary_id, url,
            )
            return candidate.secondary_id
        logger.debug(
            "fetch_from_peer: %s from %s failed rc=%d stderr=%r",
            outpath, candidate.secondary_id, rc,
            stderr.decode("utf-8", errors="replace")[-400:],
        )
        # Swallow stdout to avoid keeping multi-MB NAR snippets in RAM
        # when an upstream nix dumps progress to stdout on the failure
        # path; the rc + stderr are the diagnostic value we want.
        del stdout
    return None


def nix_copy_from_url(
    outpath: str,
    source_url: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> bool:
    """Run ``nix copy --from <source_url> --no-check-sigs <outpath>``.

    Returns True iff the subprocess exited with code 0. Unlike
    :func:`fetch_from_peer`, this helper does not consult the
    placement map nor a candidate list — it pulls from a single
    explicit URL. Used by :class:`peer_replication.BroadcastReceiver`
    where the origin peer is dictated by the broadcast offer's
    ``origin_peer_id`` field, not chosen heuristically.

    ``source_url`` should be the peer's harmonia substituter URL
    (``http://<host>:<harmonia_port>``); ``nix copy --from`` reads
    only from that URL and ignores ``extra-substituters`` in
    ``nix.conf``. ``--no-check-sigs`` matches
    :func:`fetch_from_peer`'s rationale: the cluster's auth boundary
    is the X-Cluster-PubKey header on the push side, not the
    in-store signature.
    """
    if not outpath or not source_url:
        return False
    runner = run_subprocess or _default_run_subprocess
    cmd = [
        *_NIX_BASE_CMD,
        "copy",
        "--from", source_url,
        "--no-check-sigs",
        outpath,
    ]
    _stdout, stderr, rc = runner(cmd)
    if rc == 0:
        logger.info(
            "nix_copy_from_url: %s <- %s", outpath, source_url,
        )
        return True
    logger.debug(
        "nix_copy_from_url: %s from %s failed rc=%d stderr=%r",
        outpath, source_url, rc,
        stderr.decode("utf-8", errors="replace")[-400:],
    )
    return False


def has_nix_cli() -> bool:
    """Return True iff ``nix`` is on the caller's PATH.

    Provided as a small probe for callers that want to short-circuit
    fetch attempts in environments where nix is unavailable (e.g.
    some unit-test paths). The main fetch flow doesn't need this —
    a missing ``nix`` binary surfaces as a fail-fast non-zero rc
    from the subprocess invocation.
    """
    return shutil.which("nix") is not None

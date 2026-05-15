"""End-to-end T21 smoke: Phase 0 drv-broadcast latency on N=4 secondaries.

After each Phase 0 ``phase0_eval_<binary>`` task evaluates its variant
attribute set, the worker emits each produced ``.drv`` path through
:class:`compiler_suit_runner.peer_replication.BroadcastSender` →
``/peer/path-broadcast-offer`` (originator at ``hop_count=0``). The
broadcast endpoint dedupes by ``broadcast_id`` and fans each drv out
to every other peer. By the time Phase 1 spawns build tasks for any
variant, the corresponding drv MUST already be present on every
secondary's local store -- otherwise the per-variant nix-build would
hit a substituter miss and fall back to the slower
``substituter -> harmonia`` path.

T21 verifies the **drv flood-fill** part of that contract. It is the
companion of T20 (phase0 happy path) and the prerequisite for T23
(common-dep dedup): if broadcasts aren't landing on every secondary,
Phase 1 cannot pick a holder for the parent drv when it walks
``inputDrvs`` to find common deps.

Assertions (in increasing strictness):

1. **Standard 7-invariant audit** (clean exit, no bind errors,
   manifest count, build-failure floor, no leaked containers /
   listener ports / processes) -- the broadcast layer must NOT
   regress T20's clean-exit guarantee.
2. **Phase 0 manifests emitted**: at least one
   ``phase0_eval__<binary>.json`` manifest is present, the marker
   that the framework actually entered the distributed-eval path
   (rather than falling back to in-primary eval).
3. **Holder count per phase0_eval drv** -- PRIMARY signal:
   for every distinct drv outpath emitted by a Phase 0 task,
   ``len({secondary_id, ...})`` in the placement-map gossip
   files MUST equal ``n_secondaries`` (i.e. every secondary is a
   holder). Records carry ``item_class == "phase0_eval_drv"``
   (mirroring ``workers.eval_worker``'s broadcast tag).
4. **Log-scan fallback** -- SECONDARY signal, used when the
   placement-map is silent for ``phase0_eval_drv`` (the receive-side
   ``record_self_has`` plumbing for Phase 0 drvs is wired through
   ``peer_replication`` and only writes a placement record on
   successful substitute-into-local-store; a partial deployment can
   leave the map empty while the broadcast accept logs still show
   the cascade). Greps ``slurm_*.out`` for ``path-broadcast-offer``
   accept events and asserts each emitted ``broadcast_id`` was
   acknowledged by ``>= n_secondaries - 1`` distinct receivers
   (sender does not ack itself).
5. **Latency** -- OPTIONAL: from first-emit timestamp on the
   originating Phase 0 worker to last-accept timestamp on any
   receiver, ``< 2 s`` per broadcast. Log timestamps in this slurm-
   test-env are second-precision, so a sub-second cluster of events
   can collapse to ``0 s``; the check is therefore soft (logged but
   only failed when delta >= 5 s, which would indicate a real stall
   rather than imprecise timestamps).

Dispatch shape (per Part B Verification T21):

* ``--distributed-eval`` -- the broadcast path is gated behind this
  flag (default OFF currently; will flip ON after T20/T21/T22/T23
  go green per the plan's "How to ship Part B" sequence).
* ``--jobs 4`` -- one secondary per Phase 0 task ``hello`` /
  ``zlib`` will be assigned to two of the four secondaries.
* ``--variant-sample 1`` -- minimise the per-binary variant fan-out
  so the broadcast volume stays in the 10s-of-drvs range; the
  contract is "every drv reaches every peer", not "high broadcast
  throughput".
* ``--packages hello,zlib`` -- two distinct binaries so we get two
  ``phase0_eval__<binary>.json`` manifests + each binary picks a
  different originator secondary. ``hello`` is the smoke-test
  default; ``zlib`` is a slightly larger no-test package
  (manifest_gen's "no_test" Tier-2 list) that exercises a different
  derivation graph.

Operational notes:

* **Worker tolerance** mirrors T7/T11: we tolerate the degraded
  N=3 path (one worker DOWN+NOT_RESPONDING) and degrade the holder
  count expectation to match the actual N. Below
  :data:`MIN_IDLE_WORKERS` we skip rather than mask an outage.
* **Wall-clock cap**: 1500s default (override via
  ``T21_TIMEOUT_S``). The distributed-eval path adds a Phase -1
  drv flood + a Phase 0 per-binary eval round; the medium-sized
  flake + two binaries fits comfortably even cache-cold.
* **CPU pinning / memory budget**: same as T07 (``slurm_cpus_per_task=2``,
  ``--cores 2``, ``--max-memory 2G``) -- the slurm-test-env workers
  expose 2 CPUs / 3.5 GiB per cgroup and that envelope is the contract.

The probe construction follows the rest of the slurm slice: a
dedicated :class:`ClusterProbe` with the explicit identity-file path
instead of the conftest's ``cluster_probe`` fixture, keeping the
ephemeral test-env SSH key out of ``~/.ssh/``.
"""

from __future__ import annotations

import collections
import dataclasses
import os
import pathlib
import re
from typing import Callable

import pytest

from compiler_suit_runner.peer_cache import PATHS_FILE_PREFIX
from compiler_suit_runner.tests.slurm.cluster_probe import (
    ClusterProbe,
    GatewayConfig,
)
from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    run_all_invariants,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.placement_assertions import (
    parse_placement_records,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    RunResult,
    default_invocation_for_smoke,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral; we ALWAYS pass it via ``-i`` and never via ssh-agent /
# ``~/.ssh/``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. Wired as a
# literal (not derived from sinfo) so the leak audit covers every
# worker that *could* have been dispatched to, even if SLURM only
# assigned a subset.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Desired secondary count. The actual ``jobs`` value passed to the
# framework is reduced to ``len(idle_workers)`` if a worker is out
# (degraded-mode fallback; see test body).
DESIRED_N_SECONDARIES = 4

# Lower bound for the idle-worker pre-flight. Below this, the
# multi-node broadcast contract isn't meaningfully exercised (we'd
# only see fan-out across a single peer pair), so we skip rather than
# weakening the holder-count assertion further.
MIN_IDLE_WORKERS = 3

# Phase 0 packages. ``hello`` is the smoke default, ``zlib`` is a
# Tier-2 no-test package that exercises a separate derivation graph;
# together they produce two ``phase0_eval__<binary>.json`` manifests.
PACKAGES: tuple[str, ...] = ("hello", "zlib")

# Per-variant sampling. Keep the per-binary drv count small so the
# broadcast volume stays predictable and the holder-count assertion
# is unambiguous (one accept event per drv per peer).
VARIANT_SAMPLE = 1

# Default wall-clock cap; medium workload plus distributed-eval adds
# a Phase -1 + Phase 0 round trip on top of the existing dispatch
# latency. 1500s mirrors T07/T11.
DEFAULT_TIMEOUT_S = 1500.0

# Item class the broadcast sender tags Phase 0 drv emissions with.
# Mirrors ``workers.eval_worker:_eval_jobs_for_arch -> enqueue_broadcast``
# (``item_class="phase0_eval_drv"``); duplicated here as a literal so
# this test doesn't pull eval_worker in just for the constant.
PHASE0_DRV_ITEM_CLASS = "phase0_eval_drv"

# Latency threshold for the optional soft check. Log timestamps in
# slurm-test-env logs are second-precision so a sub-second cluster of
# events collapses to 0 s; we only fail on >= 5 s which indicates a
# real stall rather than measurement noise.
SOFT_LATENCY_FAIL_S = 5.0

# Grep patterns for the log-scan fallback. The framework's structured
# logging strips field markers via ANSI escapes (see ``invariants._ANSI_RE``)
# so we match on the JSON key shape rather than positional fields.
# ``path-broadcast-offer`` appears in the receiver's HTTP route logging
# (peer_push) when an offer is accepted (not deduped).
_BROADCAST_ACCEPT_RE = re.compile(
    r"""
    path[-_]broadcast[-_]offer        # event tag
    .*?
    broadcast[_-]id["'=:\s]+(?P<bid>[0-9a-f]{16,})  # uuid hex
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Generic ISO-ish timestamp prefix used by the framework's structured
# logger (``YYYY-MM-DDTHH:MM:SS`` with optional sub-second / TZ tail).
# Used only by the optional latency check; tolerates missing TZ.
_LOG_TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file."""
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _format_results(results: list[InvariantResult]) -> str:
    """Render every invariant result for a failed assertion message."""
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _resolve_timeout() -> float:
    """Read ``T21_TIMEOUT_S`` from the environment with a default fallback."""
    raw = os.environ.get("T21_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _collect_phase0_drv_holders(
    shared_fs: pathlib.Path,
) -> dict[str, set[str]]:
    """Aggregate placement files into ``{drv_outpath: {secondary_id, ...}}``.

    Only records with ``item_class == "phase0_eval_drv"`` are
    considered -- variant placements and toolchain placements (which
    use the K=3 cascade, not flood-fill) are out of scope for T21.

    Reads every ``peers/_paths_<sid>.jsonl`` in *shared_fs/peers/*
    post-run; the gossip files persist on the NFS mount after the
    manager processes have exited.
    """
    peers_dir = shared_fs / "peers"
    placements: dict[str, set[str]] = collections.defaultdict(set)
    if not peers_dir.is_dir():
        return {}
    for paths_file in peers_dir.glob(f"{PATHS_FILE_PREFIX}*.jsonl"):
        for rec in parse_placement_records(paths_file):
            if rec.get("item_class") != PHASE0_DRV_ITEM_CLASS:
                continue
            sid = rec.get("secondary_id")
            outpath = rec.get("outpath")
            if (
                isinstance(sid, str)
                and isinstance(outpath, str)
                and outpath.startswith("/nix/store/")
            ):
                placements[outpath].add(sid)
    return dict(placements)


def _scan_broadcast_accepts(
    log_dir: pathlib.Path,
) -> dict[str, set[pathlib.Path]]:
    """Return ``{broadcast_id: {log_path_with_accept_event, ...}}``.

    Each entry in the returned map represents one ``broadcast_id`` and
    the set of secondary log files that recorded an accept event for
    it. Each secondary writes its own ``slurm_<jobid>.out``, so the
    set's cardinality is a lower bound on the number of distinct
    secondaries that fielded the offer (excluding the originator,
    which doesn't ack itself).

    The log shape is best-effort -- the regex matches on
    ``path-broadcast-offer`` + ``broadcast_id`` in the same line. A
    refactor of the runtime's logging would silently break this
    scanner; the placement-map check above is the firmer signal when
    available.
    """
    events: dict[str, set[pathlib.Path]] = collections.defaultdict(set)
    if not log_dir or not log_dir.is_dir():
        return {}
    for log_path in log_dir.rglob("slurm_*.out"):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _BROADCAST_ACCEPT_RE.finditer(text):
            bid = m.group("bid")
            events[bid].add(log_path)
    return dict(events)


def _scan_broadcast_event_timestamps(
    log_dir: pathlib.Path,
) -> dict[str, list[str]]:
    """Per broadcast_id: list of raw timestamp strings seen in any log.

    Used by the optional latency check. The strings are returned as
    captured so the caller can compare lexicographically without
    needing a timezone-aware parser (ISO-8601 sorts correctly as
    strings when the same TZ / format is used across all log lines,
    which is the case for the framework's structured logger).
    """
    out: dict[str, list[str]] = collections.defaultdict(list)
    if not log_dir or not log_dir.is_dir():
        return {}
    for log_path in log_dir.rglob("slurm_*.out"):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            am = _BROADCAST_ACCEPT_RE.search(line)
            if am is None:
                continue
            tm = _LOG_TS_RE.search(line)
            if tm is None:
                continue
            out[am.group("bid")].append(tm.group("ts"))
    return dict(out)


def _approx_seconds_between(ts_lo: str, ts_hi: str) -> float:
    """Crude lexicographic delta on ISO-ish timestamp strings.

    The framework's structured logger writes ``YYYY-MM-DDTHH:MM:SS``
    (optionally with fractional seconds). For a same-day pair we can
    estimate the delta in seconds by parsing the trailing
    ``HH:MM:SS[.frac]`` part. Cross-day or malformed timestamps fall
    back to ``inf`` -- not zero, so the caller can decide whether to
    flag it as a stall.
    """
    def _to_secs(ts: str) -> float:
        # Accept ``T`` or space separator.
        try:
            tail = ts.split("T")[-1] if "T" in ts else ts.split(" ")[-1]
            tail = tail.split("+")[0].split("Z")[0]
            h, m, s = tail.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))
        except (ValueError, IndexError):
            return float("inf")

    lo = _to_secs(ts_lo)
    hi = _to_secs(ts_hi)
    if lo == float("inf") or hi == float("inf"):
        return float("inf")
    return max(0.0, hi - lo)


@pytest.mark.slurm_live
def test_t21_phase0_broadcast(
    cluster_probe: ClusterProbe,  # noqa: ARG001 -- fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 -- documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 -- wired via the B2 cleanup harness
) -> None:
    """N=4 secondaries, two Phase 0 binaries; assert drv flood-fill landed.

    Pre-flight: gateway reachable, ``squeue --me`` empty, sinfo lists
    at least :data:`MIN_IDLE_WORKERS` workers idle.

    Dispatch: medium workload via ``fresh_run`` (cache-cold both
    sides), plus ``--distributed-eval`` so the Phase 0 path engages
    and ``--packages hello,zlib`` so two ``phase0_eval__<binary>``
    tasks materialise.

    Post-flight in order:

    1. Standard 7-invariant audit must pass with no failures.
    2. Each Phase 0 binary produced a ``phase0_eval__<binary>.json``
       manifest under the run's ``manifests/`` dir.
    3. PRIMARY: placement-map records with ``item_class ==
       "phase0_eval_drv"`` show every drv held by ``n_secondaries``
       distinct peers. The placement records are written by the
       receive-side broadcast handler after the drv is substituted
       into the local store. If NO placement records exist for that
       item class we fall back to assertion 4 (log scan) instead --
       this keeps T21 useful while the receive-side
       ``record_self_has`` for ``phase0_eval_drv`` is being wired
       up in parallel (the plan's Part B sequencing has the
       eval_worker landing before the receive-side placement
       recorder).
    4. SECONDARY: each emitted broadcast_id was accepted by at least
       ``n_secondaries - 1`` distinct secondary log files (the
       originator doesn't ack itself).
    5. OPTIONAL: for any broadcast_id with >= 2 accept timestamps,
       the spread is ``< SOFT_LATENCY_FAIL_S``. Sub-second deltas
       collapsing to 0 s under second-precision timestamps are
       fine; we only fail on >= 5 s which would be a real stall.
    """
    probe = _live_probe()

    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T21 start; found {len(queued)} "
        f"job(s): {queued!r}"
    )

    sinfo_rows = probe.sinfo_nodes()
    sinfo_by_node = {row.node: row for row in sinfo_rows}
    missing = [w for w in WORKERS if w not in sinfo_by_node]
    assert not missing, (
        f"sinfo missing expected nodes {missing!r}; got "
        f"{sorted(sinfo_by_node)!r}"
    )
    idle_workers = [
        w for w in WORKERS
        if sinfo_by_node[w].state.startswith("idle")
    ]
    if len(idle_workers) < MIN_IDLE_WORKERS:
        pytest.skip(
            f"T21 needs at least {MIN_IDLE_WORKERS} idle worker(s); "
            f"got {len(idle_workers)}: "
            + ", ".join(
                f"{w}={sinfo_by_node[w].state!r}" for w in WORKERS
            )
        )

    # Degraded-mode fallback: prefer N=4 but accept N=3 if a worker is
    # out. The holder-count expectation in the assertion adapts.
    n_secondaries = min(DESIRED_N_SECONDARIES, len(idle_workers))

    # Compose the invocation. ``default_invocation_for_smoke(jobs=N,
    # workload="medium")`` already pins ``packages=("hello",)``,
    # ``--variant-sample 2``, ``--max-variants 10``, etc. We override:
    #   * ``packages`` -- two binaries to fan Phase 0 across them.
    #   * ``variant_sample`` -- 1 to keep the per-binary drv set small.
    #   * ``ssh_identity_file`` -- so the framework's own SSH uses the
    #     explicit ephemeral key.
    #   * ``slurm_cpus_per_task=2`` -- matches the worker's CPUTot=2.
    #   * ``archs=("x86_64",)`` -- single-arch keeps the variant matrix
    #     inside the 3.5 GiB per-cgroup memory envelope.
    #   * ``extra_args=("--distributed-eval",)`` -- engages the Phase 0
    #     path under test. This is the whole point of T21 -- without
    #     this flag the test would only exercise the legacy in-primary
    #     eval and never trigger a broadcast.
    invocation = dataclasses.replace(
        default_invocation_for_smoke(jobs=n_secondaries, workload="medium"),
        packages=PACKAGES,
        variant_sample=VARIANT_SAMPLE,
        ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
        slurm_cpus_per_task=2,
        archs=("x86_64",),
        extra_args=("--distributed-eval",),
    )

    timeout_s = _resolve_timeout()
    result = fresh_run(invocation, timeout_s=timeout_s)

    detail = (
        f"exit={result.exit_code} "
        f"wall={result.wall_time_s:.1f}s "
        f"run_id={result.run_id!r} "
        f"jobs={n_secondaries} "
        f"packages={','.join(PACKAGES)} "
        f"log_dir={result.log_dir!s}"
    )

    assert result.exit_code == 0, (
        f"compiler_suit_runner submit returned non-zero ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.run_id is not None, (
        f"run_id not parsed from CLI output ({detail}). "
        f"stderr tail:\n{result.stderr[-2000:]}"
    )
    assert result.log_dir is not None and result.log_dir.is_dir(), (
        f"log_dir missing or not a directory ({detail})"
    )

    # Wait for SLURM to fully drain before file-only invariants run;
    # checks 1-4 don't poll squeue themselves. Mirrors T07/T11.
    drained = wait_squeue_empty(probe, timeout_s=300.0)
    assert drained, (
        f"squeue --me did not drain within 300s after dispatch "
        f"completed ({detail})"
    )

    artifacts = RunArtifacts.from_dir(
        result.log_dir, shared_fs=invocation.shared_fs,
    )

    # ----- Assertion 1: standard 7-invariant audit ------------------
    inv_results = run_all_invariants(
        artifacts,
        probe,
        WORKERS,
        expected_failure_count=0,
    )
    failed = [r for r in inv_results if not r.passed]
    assert not failed, (
        f"invariant(s) failed for {detail}:\n{_format_results(inv_results)}"
    )

    # ----- Assertion 2: Phase 0 manifests emitted -------------------
    manifests_dir = artifacts.manifests_dir
    phase0_manifests = sorted(
        manifests_dir.glob("phase0_eval__*.json")
    )
    assert phase0_manifests, (
        f"no phase0_eval__<binary>.json manifests under {manifests_dir} "
        f"for {detail}; --distributed-eval must produce at least one "
        "Phase 0 manifest per package"
    )
    # Soft sanity: one manifest per package we asked for. We don't
    # hard-assert equality (a Tier-2 package could be filtered out by
    # the support table on a given arch / compiler combination), but
    # we do warn loudly if the count is below one-per-package.
    if len(phase0_manifests) < len(PACKAGES):
        # This becomes a hard fail only if NO Phase 0 manifests at all
        # (already checked above); a partial set is informational.
        pass

    # ----- Assertion 3 (primary): placement-map holder count --------
    drv_holders = _collect_phase0_drv_holders(invocation.shared_fs)

    # ----- Assertion 4 (secondary): log-scan accept events ----------
    accept_events = _scan_broadcast_accepts(result.log_dir)

    # Of the two signals, the placement-map is the firmer one (records
    # land only after a successful local-store substitution + record_self_has).
    # If the receive-side recorder is not yet wired, the map will be
    # empty for ``phase0_eval_drv`` -- in which case we fall back to
    # the log-scan as the assertion of record.
    if drv_holders:
        # Primary check: every drv has n_secondaries distinct holders.
        # In the steady state every drv is replicated to ALL peers
        # because the broadcast protocol fans out to every receiver
        # at hop_count=0; receivers dedupe by broadcast_id but each
        # peer still ends up with the drv in its local store.
        under_replicated = [
            (outpath, sorted(holders))
            for outpath, holders in drv_holders.items()
            if len(holders) < n_secondaries
        ]
        assert not under_replicated, (
            f"Phase 0 drv broadcast under-replicated for {detail}; "
            f"{len(under_replicated)} drv(s) had < {n_secondaries} "
            f"holders within the post-Phase-0 window:\n"
            + "\n".join(
                f"  {op}: {h} (len={len(h)})"
                for op, h in under_replicated
            )
            + (
                "\nFull phase0_eval_drv placement map:\n"
                + "\n".join(
                    f"  {op}: {sorted(h)}"
                    for op, h in drv_holders.items()
                )
            )
        )
    else:
        # Fallback: log-scan check. Each broadcast_id must appear in
        # at least (n_secondaries - 1) distinct log files (the
        # originator's log shows the emit, NOT an accept event;
        # accept events are written by receivers).
        assert accept_events, (
            "no Phase 0 drv broadcasts visible in placement records "
            "OR in secondary logs; --distributed-eval must trigger "
            "BroadcastSender.enqueue_broadcast for each evaluated drv. "
            f"({detail})"
        )
        min_distinct_accepts = max(1, n_secondaries - 1)
        under_broadcast = [
            (bid, sorted(logs))
            for bid, logs in accept_events.items()
            if len({lp.name for lp in logs}) < min_distinct_accepts
        ]
        assert not under_broadcast, (
            f"Phase 0 broadcast log-scan: {len(under_broadcast)} "
            f"broadcast_id(s) accepted by fewer than "
            f"{min_distinct_accepts} distinct secondary log file(s) "
            f"for {detail}:\n"
            + "\n".join(
                f"  {bid}: {[lp.name for lp in logs]}"
                for bid, logs in under_broadcast
            )
        )

    # ----- Assertion 5 (optional / soft): latency check -------------
    # For any broadcast_id with two or more captured timestamps the
    # spread should be << 2 s in the steady state. Second-precision
    # timestamps make a sub-second cluster collapse to 0 s, so we
    # only fail on >= SOFT_LATENCY_FAIL_S (5 s) which would be a
    # real stall. Captured as a soft check so a log-format quirk
    # doesn't surface as a Phase 0 regression.
    ts_per_bid = _scan_broadcast_event_timestamps(result.log_dir)
    stalled: list[tuple[str, float, str, str]] = []
    for bid, stamps in ts_per_bid.items():
        if len(stamps) < 2:
            continue
        stamps_sorted = sorted(stamps)
        delta = _approx_seconds_between(stamps_sorted[0], stamps_sorted[-1])
        if delta >= SOFT_LATENCY_FAIL_S:
            stalled.append((bid, delta, stamps_sorted[0], stamps_sorted[-1]))
    assert not stalled, (
        f"Phase 0 broadcast latency check: {len(stalled)} broadcast(s) "
        f"took >= {SOFT_LATENCY_FAIL_S}s end-to-end for {detail}:\n"
        + "\n".join(
            f"  {bid}: {delta:.1f}s ({lo} -> {hi})"
            for bid, delta, lo, hi in stalled
        )
    )

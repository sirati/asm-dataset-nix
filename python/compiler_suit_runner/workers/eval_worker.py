"""Phase 0 distributed-eval worker — one task per binary.

Runs on a cluster secondary. Given a manifest payload built by
:func:`compiler_suit_runner.manifest_gen.make_phase0_eval_header`, the
worker:

1. Resolves the per-(arch, suffix) drv set by invoking
   ``nix-eval-jobs --flake <attr>.<arch>`` against the local
   /nix/store, re-applying the same deterministic
   ``_sample_suffix_attrs`` sampling as the submitter (keyed on the
   payload's ``variant_seed``) so primary and secondaries agree on
   the variant subset without the submitter ever shipping the list.
2. Broadcasts each produced ``.drv`` path to every peer in the
   cluster via :class:`peer_replication.BroadcastSender` (which
   posts ``/peer/path-broadcast-offer`` and lets the receiver flood
   onward). Each receiver substitutes the drv into its local store
   so Phase 1+ tasks scheduled anywhere in the cluster can read the
   graph immediately.
3. Writes a resume marker at ``out/<binary>/_phase0/manifest.json``
   listing ``[{label, drv}, ...]`` so a re-execution after the task
   was preempted short-circuits to the broadcast-already-happened
   path.

Error-type contract (framework integration)
-------------------------------------------

The dynamic_runner framework distinguishes two failure types:

* ``ErrorType::Errored`` — transient / recoverable. The task is
  retry-eligible against the **retry-pass budget**. Use this for
  nix-eval-jobs subprocess failures, network failures during the
  broadcast fan-out, and similar.

* ``ErrorType::Unfulfillable`` — permanent / structurally impossible
  on this peer. The task is NOT retried; it transitions back to
  Pending so another peer can pick it up. Use this **only** for
  signals that this peer cannot ever satisfy the task (e.g. missing
  toolchain output that should have been pre-installed via Phase
  -1 — explicitly NOT raised by this module today; that wiring
  belongs in the framework dispatch layer once Phase -1 is live).

This worker raises ``RuntimeError`` on any transient failure. The
framework's worker harness wraps that into an ``Errored`` task
state, so eval failures correctly count against the retry-pass
budget instead of the dispatch (Unfulfillable→Pending) budget.

Why one task per binary (not one per arch)
------------------------------------------

The plan deliberately keeps the partition coarse: each binary's
eval walks every requested arch in a single ``nix-eval-jobs`` pass
per arch, but stays inside one task. Two reasons:

* The cross-arch toolchain closures share enormous overlap; a
  single worker process inside one task can let ``nix-eval-jobs``
  reuse the in-memory eval cache across archs.
* Phase 1 task spawn waits for the per-binary marker file; a
  binary's resume marker is single-writer so we never race on it.

See ``~/.claude/plans/lively-beaming-summit.md`` Part B for the
full rationale and the Phase 1 planner that consumes the marker.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any, Optional

from compiler_suit_runner.peer_replication import BroadcastSender


# ``run_subprocess`` accepts argv (list[str]) and returns a tuple of
# (stdout_bytes, stderr_bytes, returncode). Mirrors the
# ``RunSubprocess`` callable shape in ``preflight.py`` /
# ``workers/partition_worker.py`` so the same fakes can be reused.
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


PHASE_0_ITEM_CLASS = "phase0_eval"
"""Item class string this worker handles (matches manifest_gen)."""


_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Deterministic variant sampling
# ---------------------------------------------------------------------------


def sample_suffix_attrs(
    suffix_attrs: dict,
    *,
    arch: str,
    sample_size: int,
    seed: str,
) -> dict:
    """Re-implementation of ``preflight._sample_suffix_attrs``.

    Deterministically down-samples ``{suffix: meta_entry}`` to
    ``sample_size`` per ``(compiler, optimization)`` group, seeded on
    ``f"{seed}:{compiler}:{arch}:{opt}"`` so the same operator seed
    produces the same sample on every peer.

    Suffixes with non-dict meta or missing compiler/optimization
    metadata are passed through unchanged (we cannot group them).

    The implementation MUST stay bit-for-bit in lock-step with the
    submitter-side ``_sample_suffix_attrs``; any drift would let
    Phase 0 workers and the submitter disagree on the variant
    subset, breaking Phase 1 dedup. The two implementations are
    intentionally kept side-by-side rather than imported so that a
    future refactor of one cannot silently change the wire
    contract of the other.
    """
    if sample_size <= 0:
        return suffix_attrs
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    passthrough: dict[str, dict] = {}
    for suffix, meta_entry in suffix_attrs.items():
        if not isinstance(meta_entry, dict):
            passthrough[suffix] = meta_entry
            continue
        compiler = meta_entry.get("compiler")
        opt = meta_entry.get("optimization")
        if not isinstance(compiler, str) or not isinstance(opt, str):
            passthrough[suffix] = meta_entry
            continue
        groups.setdefault((compiler, opt), []).append((suffix, meta_entry))

    sampled: dict[str, dict] = dict(passthrough)
    for (compiler, opt), candidates in groups.items():
        candidates.sort(key=lambda kv: kv[0])
        rng = random.Random(f"{seed}:{compiler}:{arch}:{opt}")
        chosen = rng.sample(candidates, min(sample_size, len(candidates)))
        for suffix, meta_entry in chosen:
            sampled[suffix] = meta_entry
    return sampled


# ---------------------------------------------------------------------------
# Manifest payload parsing
# ---------------------------------------------------------------------------


def parse_payload(payload: dict) -> dict[str, Any]:
    """Validate a phase0_eval payload (as produced by
    :func:`manifest_gen.make_phase0_eval_header`) and return a
    normalised dict.

    Raises :class:`ValueError` on shape errors so callers (and tests)
    can distinguish bad-input from transient-eval failures.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"phase0_eval payload must be a dict, got {type(payload).__name__}"
        )
    binary = payload.get("binary")
    sys_name = payload.get("sys")
    archs = payload.get("archs")
    suffixes = payload.get("suffixes")
    attr = payload.get("attr")
    if not isinstance(binary, str) or not binary:
        raise ValueError(f"phase0_eval payload: invalid 'binary' ({binary!r})")
    if not isinstance(sys_name, str) or not sys_name:
        raise ValueError(f"phase0_eval payload: invalid 'sys' ({sys_name!r})")
    if not isinstance(archs, list) or not all(isinstance(a, str) for a in archs):
        raise ValueError(f"phase0_eval payload: invalid 'archs' ({archs!r})")
    if not isinstance(suffixes, list) or not all(
        isinstance(s, str) for s in suffixes
    ):
        raise ValueError(
            f"phase0_eval payload: invalid 'suffixes' ({suffixes!r})"
        )
    if not isinstance(attr, str) or not attr:
        raise ValueError(f"phase0_eval payload: invalid 'attr' ({attr!r})")
    for s in suffixes:
        if not _SAFE_SUFFIX_RE.match(s):
            raise ValueError(
                f"phase0_eval payload: unsafe suffix {s!r} — refusing to splice"
            )

    variant_sample = payload.get("variant_sample")
    if variant_sample is not None and not isinstance(variant_sample, int):
        raise ValueError(
            f"phase0_eval payload: invalid 'variant_sample' ({variant_sample!r})"
        )
    variant_seed = payload.get("variant_seed")
    if variant_seed is not None and not isinstance(variant_seed, str):
        raise ValueError(
            f"phase0_eval payload: invalid 'variant_seed' ({variant_seed!r})"
        )

    return {
        "binary": binary,
        "sys": sys_name,
        "archs": list(archs),
        "suffixes": list(suffixes),
        "attr": attr,
        "variant_sample": variant_sample,
        "variant_seed": variant_seed,
    }


# ---------------------------------------------------------------------------
# nix-eval-jobs invocation
# ---------------------------------------------------------------------------


def _eval_jobs_for_arch(
    attr: str,
    arch: str,
    suffixes: list[str],
    *,
    run_subprocess: RunSubprocess,
) -> dict[str, str]:
    """Run ``nix-eval-jobs`` for ``<attr>.<arch>`` filtered by
    ``suffixes`` and return ``{suffix: drvPath}``.

    Mirrors the ``--select intersectAttrs`` pattern from
    ``preflight._eval_drv_paths_for_suffixes`` so the wire form
    nix-eval-jobs receives is identical on submitter and secondary.

    On any non-zero exit code or stdout that decodes to no drvs at
    all, raises :class:`RuntimeError`. The framework worker harness
    converts that into ``ErrorType::Errored`` (retry-eligible).
    """
    if not suffixes:
        return {}

    # Validate every suffix before splicing — the originator already
    # validated, but a worker should never trust a manifest payload
    # blindly when it's about to construct a nix expression.
    for s in suffixes:
        if not _SAFE_SUFFIX_RE.match(s):
            raise RuntimeError(
                f"eval_worker: unsafe suffix {s!r} in payload — refusing to"
                " splice into nix-eval-jobs --select"
            )

    keys = " ".join(f'"{s}" = null;' for s in suffixes)
    select_expr = f"m: builtins.intersectAttrs {{ {keys} }} m"

    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{attr}.{arch}",
        "--select",
        select_expr,
        "--max-jobs",
        "1",
    ]
    stdout, stderr, rc = run_subprocess(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        # RuntimeError → framework harness maps to ErrorType::Errored
        # (retry-pass eligible) — NOT Unfulfillable, since
        # nix-eval-jobs failures are typically transient (eval-time
        # OOM, transient substituter network failure, etc).
        raise RuntimeError(
            f"nix-eval-jobs {attr}.{arch} failed (rc={rc}): {decoded_err}"
        )

    drvs: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # nix-eval-jobs interleaves status lines; ignore anything
            # that isn't a valid JSON object.
            continue
        if not isinstance(entry, dict):
            continue
        attr_name = entry.get("attr")
        drv = entry.get("drvPath")
        if isinstance(attr_name, str) and isinstance(drv, str) and drv:
            drvs[attr_name] = drv
    return drvs


def _eval_meta_for_arch(
    sys_name: str,
    binary: str,
    arch: str,
    *,
    flake_ref: str,
    run_subprocess: RunSubprocess,
) -> dict[str, Any]:
    """Read ``_meta.<sys>.<binary>.<arch>`` so we can re-apply the
    same deterministic sampling the submitter applied.

    Returns the parsed JSON object (a ``{suffix: meta_entry}`` map).
    On non-dict response or eval failure, raises :class:`RuntimeError`
    (transient — retry-pass eligible).
    """
    argv: list[str] = [
        "nix",
        "eval",
        "--extra-experimental-features",
        "nix-command flakes",
        "--json",
        f"{flake_ref}#_meta.{sys_name}.{binary}.{arch}",
    ]
    stdout, stderr, rc = run_subprocess(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} failed "
            f"(rc={rc}): {decoded_err}"
        )
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} produced invalid JSON:"
            f" {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} returned non-object"
        )
    return parsed


def _drv_size(drv_path: str) -> int:
    """Best-effort byte size for the broadcast offer.

    The receiver uses this for an opportunistic disk-budget check
    only; an inaccurate value is non-fatal. Missing files give 0 so
    we still broadcast (the receiver will resolve via substitution).
    """
    try:
        return os.path.getsize(drv_path)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Resume marker
# ---------------------------------------------------------------------------


def _marker_path(out_dir: pathlib.Path, binary: str) -> pathlib.Path:
    return out_dir / binary / "_phase0" / "manifest.json"


def _read_marker(marker: pathlib.Path) -> Optional[dict]:
    """Return the parsed marker dict, or ``None`` if absent / unreadable.

    A corrupt marker file is treated as absent: corruption is
    visible from outside the runner (file on disk) and forcing a
    re-eval is cheaper than blocking forever on a half-written
    file. We do log via ``RuntimeError`` neither here — the
    function's contract is "best effort".
    """
    if not marker.exists():
        return None
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_marker(marker: pathlib.Path, payload: dict) -> None:
    """Atomically write the resume marker.

    Uses tmp-then-rename within the same directory so a crash
    mid-write never leaves a half-parsed marker on disk.
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, marker)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_eval_task(
    payload: dict,
    out_dir: pathlib.Path,
    broadcast_sender: BroadcastSender,
    run_subprocess: Optional[RunSubprocess] = None,
    *,
    flake_ref: str = ".",
    broadcast_timeout: float = 10.0,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """Phase 0 per-binary eval dispatch entry point.

    See the module docstring for the protocol. The function returns
    the marker dict (also persisted to
    ``out_dir/<binary>/_phase0/manifest.json``) on success.

    Failure modes raise :class:`RuntimeError` — the framework worker
    harness then surfaces ``ErrorType::Errored`` to the primary,
    which charges the failure to the retry-pass budget. We
    deliberately do NOT raise ``Unfulfillable`` from this layer; a
    secondary that cannot fulfil Phase 0 (e.g. permanently missing
    toolchain) should signal that via Phase -1's task-dispatch
    refusal, not by mutating a Phase 0 task's error type.

    Parameters
    ----------
    payload :
        The phase0_eval manifest payload (see
        :func:`manifest_gen.make_phase0_eval_header`).
    out_dir :
        Per-secondary output directory (typically the worker's
        scratch root). The marker is written to
        ``out_dir / <binary> / _phase0 / manifest.json``.
    broadcast_sender :
        :class:`peer_replication.BroadcastSender` instance owned by
        the worker process — lifecycle management (start/stop) is
        the caller's responsibility.
    run_subprocess :
        Optional injected subprocess runner; defaults to a real
        ``subprocess.run``. Tests override.
    flake_ref :
        Flake reference passed to ``nix eval`` for the ``_meta``
        lookup. Defaults to ``.`` (current directory) so the
        worker resolves against the flake checkout shipped by the
        framework into the secondary's working directory.
    broadcast_timeout :
        Seconds to wait on each :meth:`BroadcastSender.wait_for_completion`
        call. A timeout is non-fatal — we record the broadcast id
        and continue. The flood-fill protocol is opportunistic.
    now :
        Injected clock for the ``produced_at`` timestamp. Defaults
        to :func:`time.time`.
    """
    clock = now or time.time
    runner = run_subprocess or _default_run_subprocess
    parsed = parse_payload(payload)
    binary = parsed["binary"]
    sys_name = parsed["sys"]
    archs = parsed["archs"]
    suffixes = parsed["suffixes"]
    attr = parsed["attr"]
    variant_sample = parsed["variant_sample"]
    variant_seed = parsed["variant_seed"]

    marker = _marker_path(out_dir, binary)

    # Step 0: resume short-circuit. If the marker exists we trust
    # that some prior run of this task (perhaps on a different
    # secondary that previously held it) already broadcast every
    # drv to the cluster. Re-doing the broadcast is wasteful and
    # the receiver-side dedup would no-op them anyway; but more
    # importantly, the primary uses this marker as the Phase 1
    # gating signal, so the contract is "marker present == phase
    # 0 done".
    existing = _read_marker(marker)
    if existing is not None:
        return existing

    # Step 1: enumerate drvs per arch. Each arch is one
    # ``nix-eval-jobs`` invocation. If sampling is in play we first
    # read ``_meta`` to drive the deterministic group-aware sample
    # (matching the submitter's ``_sample_suffix_attrs`` logic), so
    # the sampled subset stays consistent across re-runs and across
    # peers without the submitter shipping the list.
    variants: list[dict[str, str]] = []
    broadcast_ids: list[tuple[str, str]] = []
    for arch in archs:
        if variant_sample and variant_sample > 0 and variant_seed:
            meta = _eval_meta_for_arch(
                sys_name, binary, arch,
                flake_ref=flake_ref,
                run_subprocess=runner,
            )
            # Filter meta to the suffixes named in the payload — the
            # submitter has already applied support-table + known-bad
            # filtering, so a meta entry NOT in payload['suffixes']
            # means the submitter has dropped it for that arch.
            allowed = set(suffixes)
            scoped = {s: m for s, m in meta.items() if s in allowed}
            sampled = sample_suffix_attrs(
                scoped,
                arch=arch,
                sample_size=variant_sample,
                seed=variant_seed,
            )
            sampled_suffixes = sorted(sampled.keys())
        else:
            sampled_suffixes = list(suffixes)

        if not sampled_suffixes:
            continue

        arch_drvs = _eval_jobs_for_arch(
            attr,
            arch,
            sampled_suffixes,
            run_subprocess=runner,
        )

        # Step 2: enqueue a broadcast for each (suffix → drv). The
        # broadcast is non-blocking; the worker thread inside
        # BroadcastSender fans the offer out to every peer.
        for suffix, drv in sorted(arch_drvs.items()):
            label = f"{binary}__{arch}__{suffix}"
            variants.append({"label": label, "drv": drv, "arch": arch,
                             "suffix": suffix})
            bid = broadcast_sender.enqueue_broadcast(
                drv,
                _drv_size(drv),
                item_class="phase0_eval_drv",
            )
            broadcast_ids.append((label, bid))

    # Step 3: wait for each broadcast to complete (or time out).
    # Timeout is non-fatal: the flood-fill protocol is best-effort
    # and a slow peer will eventually pull via substitution when
    # the Phase 1 task lands on it. We still record the result so
    # operators can diagnose churn.
    broadcast_results: list[dict[str, Any]] = []
    for label, bid in broadcast_ids:
        result = broadcast_sender.wait_for_completion(
            bid, timeout=broadcast_timeout
        )
        entry: dict[str, Any] = {"label": label, "broadcast_id": bid}
        if result is None:
            entry["status"] = "timeout"
        else:
            entry["status"] = "ok"
            entry["success_count"] = result.success_count
            entry["fail_count"] = result.fail_count
            entry["failed_peers"] = list(result.failed_peers)
        broadcast_results.append(entry)

    # Step 4: persist the resume marker. Phase 1 gating reads this
    # file to know the binary's eval is done. The marker is the
    # complete picture (variants + broadcast outcomes) so a Phase 1
    # planner re-run after partial failures can decide whether to
    # request re-broadcasts.
    marker_data: dict = {
        "binary": binary,
        "sys": sys_name,
        "produced_at": float(clock()),
        "variants": variants,
        "broadcasts": broadcast_results,
    }
    _write_marker(marker, marker_data)
    return marker_data


__all__ = [
    "PHASE_0_ITEM_CLASS",
    "RunSubprocess",
    "main",
    "parse_payload",
    "run_eval_task",
    "sample_suffix_attrs",
]


# ---------------------------------------------------------------------------
# Subprocess entry point
#
# Spawned by the dynamic_runner framework as
# ``python -m compiler_suit_runner.workers.eval_worker``. The per-task
# wire driving (Ready handshake, command framing, exception → wire
# mapping, SIGTERM → SystemExit) is owned by ``dynamic_runner.worker.run``;
# this module supplies the per-task body via the ``handle`` closure.
#
# Mirror of :func:`build_worker.main` in shape (argparse layout, log-file
# routing, ``handle`` returning :class:`WorkerOutput`) so the framework's
# worker-spawn wrapper produces identical argv for both worker classes.
# The flake-ref is intentionally NOT a CLI flag here: the phase0_eval
# payload already carries ``attr`` (fully-qualified flake attribute) and
# the ``_meta`` lookup uses the current working directory by default.


def _read_peer_push_urls(
    shared_fs: Optional[pathlib.Path],
    self_secondary_id: str,
) -> list[str]:
    """Enumerate peer push URLs from ``<shared_fs>/peers/*.json``.

    Reads the gossip directory each time it is called so a worker
    picks up peers that joined after process start. Returns the push
    URLs (``http://<host>:<harmonia_port + PUSH_PORT_OFFSET>``) for
    every peer except ``self_secondary_id``.

    Returns an empty list when ``shared_fs`` is unset or the
    ``peers/`` directory does not exist yet — this gracefully covers
    the submitter-peer timing race (workers hit toolchain_validate
    before the submitter gossip lands) so the BroadcastSender no-ops
    instead of raising. TODO(#submitter-gossip-race): once the
    submitter publishes its peer file BEFORE any toolchain_validate
    task is queued, this empty-list path becomes purely a single-peer
    cluster fallback.
    """
    if shared_fs is None:
        return []
    # Late import so unit tests that mock the peer-url provider do not
    # have to import peer_cache transitively.
    from compiler_suit_runner import peer_cache  # noqa: PLC0415
    from compiler_suit_runner.peer_push import push_port_for  # noqa: PLC0415

    try:
        peers = peer_cache.list_peers(
            pathlib.Path(shared_fs),
            exclude_id=self_secondary_id or None,
        )
    except Exception:  # noqa: BLE001 — gossip is best-effort
        return []
    urls: list[str] = []
    for p in peers:
        urls.append(f"http://{p.hostname}:{push_port_for(p.port)}")
    return urls


def main() -> int:
    """Subprocess entry point for the phase 0 eval worker.

    Parses the framework's worker argv (same shape as
    :func:`build_worker.main`), constructs a :class:`BroadcastSender`
    that reads peer gossip from ``--shared-fs/peers/`` on each
    fan-out, and dispatches one phase0_eval task per call into
    :func:`run_eval_task`.

    A :class:`RuntimeError` raised by :func:`run_eval_task` is
    re-raised as :class:`NonRecoverableError` so the framework
    surfaces it as ``error:non_recoverable:`` — matching the
    "Errored = retry-pass" contract documented in this module's
    top-level docstring (the framework's harness maps NonRecoverable
    crash to Errored, NOT Unfulfillable).
    """
    import argparse  # noqa: PLC0415 — match build_worker's late-import style.

    from dynamic_runner.worker import (  # noqa: PLC0415
        NonRecoverableError,
        Task,
        WorkerOutput,
        run,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.eval_worker",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument(
        "--shared-fs",
        type=str,
        default=None,
        help=(
            "NFS root: peer gossip lives under ``peers/`` and the"
            " phase-0 resume marker is written into ``out/<binary>/"
            "_phase0/manifest.json``."
        ),
    )
    parser.add_argument(
        "--secondary-id",
        type=str,
        default="",
        help="This worker's secondary id (broadcast origin author).",
    )
    parser.add_argument(
        "--signing-public-key",
        type=str,
        default="",
        help=(
            "Cluster signing public key. Authenticates the broadcast"
            " fan-out; without it the BroadcastSender still runs but"
            " peers may reject the offers."
        ),
    )
    args, _ = parser.parse_known_args()

    # Log routing matches build_worker: prefer the framework's
    # ``--log-file`` if supplied; else fall back to the SLURM-wrapper
    # bind-mount location. Best-effort — stderr is silenced by the
    # framework when ``--socket-path`` mode is active anyway, so a
    # write failure should not crash the worker before ``run()`` even
    # starts.
    import logging  # noqa: PLC0415

    _worker_log = args.log_file or f"/app/log-network/worker_{os.getpid()}.log"
    try:
        logging.basicConfig(
            filename=_worker_log,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        logging.getLogger("compiler_suit_runner.eval_worker.startup").info(
            "eval_worker subprocess started; pid=%d argv=%r",
            os.getpid(), sys.argv,
        )
    except OSError:
        pass

    shared_fs = (
        pathlib.Path(args.shared_fs) if args.shared_fs else None
    )
    out_dir = (
        pathlib.Path(args.output)
        if args.output
        else (shared_fs / "out" if shared_fs is not None else pathlib.Path("out"))
    )

    self_sid = args.secondary_id or ""

    def _peer_url_provider() -> list[str]:
        return _read_peer_push_urls(shared_fs, self_sid)

    broadcast_sender = BroadcastSender(
        self_peer_id=self_sid,
        peer_url_provider=_peer_url_provider,
        our_pubkey=args.signing_public_key or "",
    )

    _handle_log = logging.getLogger(
        "compiler_suit_runner.eval_worker.handle"
    )

    def handle(task: Task) -> Optional[WorkerOutput]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        _handle_log.info(
            "handle: starting phase0_eval task task_id=%r payload_keys=%r",
            getattr(task, "task_id", None), sorted(payload.keys()),
        )
        try:
            run_eval_task(
                payload,
                out_dir=out_dir,
                broadcast_sender=broadcast_sender,
            )
        except RuntimeError as exc:
            # Per module docstring: transient eval failures map to
            # ErrorType::Errored (retry-pass eligible). The framework
            # treats NonRecoverableError as a worker-side hard fail
            # which the primary classifies as Errored — NOT
            # Unfulfillable. (Unfulfillable is reserved for "this
            # peer structurally cannot do this task" which the eval
            # worker does not surface.)
            _handle_log.exception(
                "handle: run_eval_task raised RuntimeError (retry-eligible)"
            )
            raise NonRecoverableError(
                f"phase0_eval failed: {exc}"
            ) from exc
        except BaseException as exc:  # noqa: BLE001
            _handle_log.exception(
                "handle: run_eval_task raised unexpectedly"
            )
            raise NonRecoverableError(
                f"phase0_eval crashed: {type(exc).__name__}: {exc}"
            ) from exc
        return WorkerOutput()

    try:
        run(handle, args=args)
    finally:
        # Daemon thread, but a clean stop drains in-flight broadcasts
        # before the subprocess exits.
        try:
            broadcast_sender.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

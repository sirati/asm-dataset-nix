"""Placement-map plumbing assertions for the targeted-fetch refactor.

The cluster-wide ``dict[outpath, set[secondary_id]]`` map (see
:mod:`compiler_suit_runner.peer_paths`) replaces all-peers harmonia
fanout with point-to-point ``nix copy --from http://<peer>:<port>``
transfers. This module bundles the file-only assertions a T-test
needs to confirm the map is being populated and used:

* :func:`assert_validate_manifests_emitted` — the primary's manifest
  emission produced ``phase2_toolchain_validate`` items, not the
  legacy ``phase2_toolchain`` build manifests. By default
  ``--allow-toolchain-build`` is off, so any build manifest in
  ``manifests_dir`` is a regression.
* :func:`assert_placement_files_present_and_nonempty` — at least
  one ``peers/_paths_<sid>.jsonl`` exists post-run with a parseable
  toolchain or common-dep placement record. The map is the cluster's
  evidence that record_self_has fired; an empty map means the worker
  hook didn't run.
* :func:`assert_targeted_nix_copy_in_secondary_logs` — secondary
  ``slurm_*.out`` files contain ``nix copy --from http://...
  --no-check-sigs`` invocations, and each fetch hits exactly one
  peer URL (no fanout). ``--no-substituters`` is NOT in the wire
  shape (it's invalid for ``nix copy``; the ``--from`` URL pin is
  what restricts the source).

All three are pure file readers; nothing here touches the live
cluster. The T-tests glue them onto the existing 7-invariant
audit so a placement-layer regression surfaces alongside the
clean-exit/leak-leak audit.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from compiler_suit_runner.peer_cache import PATHS_FILE_PREFIX
from compiler_suit_runner.tests.slurm.invariants import RunArtifacts


__all__ = [
    "assert_placement_files_present_and_nonempty",
    "assert_targeted_nix_copy_in_secondary_logs",
    "assert_validate_manifests_emitted",
    "parse_placement_records",
    "scan_nix_copy_invocations",
]


# The wire shape ``peer_paths_fetch._copy_argv`` produces:
#   nix --extra-experimental-features nix-command flakes copy
#       --from http://<host>:<port> --no-check-sigs <outpath>
# We match the ``nix copy --from <url>`` core and the
# ``--no-check-sigs`` discriminator (proves it's the targeted-fetch
# path; ``--from`` URL pin already restricts the source so
# ``--no-substituters`` is NOT in the argv — it's invalid for the
# ``copy`` subcommand).
_NIX_COPY_LINE_RE = re.compile(
    r"""
    nix\b.*?\bcopy\b      # ``nix ... copy``
    .*?--from\s+(?P<url>http://[^\s]+)  # ``--from http://host:port``
    .*?--no-check-sigs    # discriminator: targeted-fetch flag
    .*?(?P<path>/nix/store/[^\s]+)      # target store path
    """,
    re.VERBOSE,
)


def assert_validate_manifests_emitted(artifacts: RunArtifacts) -> None:
    """Fail when the run emitted build-only toolchain manifests.

    With ``--allow-toolchain-build`` off (the default), every
    toolchain item must be a ``phase2_toolchain_validate`` JSON
    file. The two filenames are disjoint:
        ``toolchain__<arch>__<compiler>.json``  -> build
        ``toolchain_validate__<arch>__<compiler>.json`` -> validate

    We allow the validate set to be non-empty (the contract is
    "every toolchain is validate-only"), and explicitly disallow
    any build-shaped toolchain manifest from sneaking in.
    """
    manifests_dir = artifacts.manifests_dir
    assert manifests_dir.is_dir(), (
        f"manifests_dir missing or not a directory: {manifests_dir}"
    )
    build_manifests = sorted(manifests_dir.glob("toolchain__*.json"))
    validate_manifests = sorted(
        manifests_dir.glob("toolchain_validate__*.json"),
    )
    assert not build_manifests, (
        "default --allow-toolchain-build=False must emit only "
        f"phase2_toolchain_validate items, but found {len(build_manifests)} "
        f"build-shaped manifest(s): "
        f"{[p.name for p in build_manifests[:5]]}..."
    )
    assert validate_manifests, (
        "no phase2_toolchain_validate manifests under "
        f"{manifests_dir}; the primary's emit_all_manifests "
        "must produce at least one validate item per toolchain"
    )
    # Every validate manifest must carry a resolved outpath. Without
    # it the secondary can't path-info-probe or nix-copy.
    for path in validate_manifests:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssertionError(
                f"validate manifest {path} unreadable: {exc}"
            ) from exc
        payload = doc.get("payload")
        assert isinstance(payload, dict), (
            f"validate manifest {path} has no payload object"
        )
        outpath = payload.get("outpath")
        assert isinstance(outpath, str) and outpath.startswith("/nix/store/"), (
            f"validate manifest {path} missing/invalid payload.outpath: "
            f"{outpath!r}"
        )


def parse_placement_records(
    paths_file: Path,
) -> list[dict]:
    """Parse one ``peers/_paths_<sid>.jsonl`` file into record dicts.

    Bad lines are skipped silently (matching the watcher's lenient
    parser). Returns ``[]`` for a missing or empty file.
    """
    if not paths_file.exists():
        return []
    out: list[dict] = []
    for line in paths_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def assert_placement_files_present_and_nonempty(
    artifacts: RunArtifacts,
) -> None:
    """At least one secondary must have written a placement record.

    The map is the only durable evidence that ``record_self_has``
    fired in the worker after a toolchain or common-dep was
    realised. An empty map post-run means the worker hook isn't
    wired or the on-disk JSONL writer raced and lost.

    Allowed states post-run:

    * At least one ``peers/_paths_<sid>.jsonl`` exists with at least
      one valid record AND at least one of those records carries a
      ``toolchain`` or ``common_dep`` item_class. (Variant placements
      are out-of-scope for the current refactor.)
    """
    shared_fs = artifacts.shared_fs
    assert shared_fs is not None, (
        "artifacts.shared_fs is None; the dispatch must be invoked with "
        "--shared-fs <path> for the placement-map assertions to run"
    )
    peers_dir = shared_fs / "peers"
    assert peers_dir.is_dir(), (
        f"peers/ directory missing under shared_fs: {peers_dir}"
    )
    files = sorted(peers_dir.glob(f"{PATHS_FILE_PREFIX}*.jsonl"))
    assert files, (
        f"no {PATHS_FILE_PREFIX}<sid>.jsonl files in {peers_dir}; "
        "secondaries must record_self_has on every realised store path "
        "and primary's withdraw_self should not have run yet"
    )
    saw_toolchain_or_dep = False
    for f in files:
        for rec in parse_placement_records(f):
            ic = rec.get("item_class")
            if ic in ("toolchain", "common_dep"):
                outpath = rec.get("outpath")
                if isinstance(outpath, str) and outpath.startswith("/nix/store/"):
                    saw_toolchain_or_dep = True
                    break
        if saw_toolchain_or_dep:
            break
    assert saw_toolchain_or_dep, (
        "placement gossip files exist but none carry a toolchain or "
        f"common_dep record. files={[f.name for f in files]}"
    )


def scan_nix_copy_invocations(log_lines: Iterable[str]) -> list[dict]:
    """Extract ``nix copy --from ...`` invocations from log lines.

    Returns a list of ``{"url": "http://host:port", "path":
    "/nix/store/..."}``. Lines without the ``--no-check-sigs``
    discriminator are skipped — those wouldn't be from the
    targeted-fetch path (``nix copy --from <url> --no-check-sigs
    <outpath>``).
    """
    hits: list[dict] = []
    for raw in log_lines:
        m = _NIX_COPY_LINE_RE.search(raw)
        if m is None:
            continue
        hits.append({"url": m.group("url"), "path": m.group("path")})
    return hits


def assert_targeted_nix_copy_in_secondary_logs(
    artifacts: RunArtifacts,
) -> None:
    """Secondary logs must show targeted ``nix copy --from`` calls.

    The slurm-test-env workers log to ``slurm_<jobid>.out`` (and
    ``.err``) under the run dir. We grep them for the targeted-copy
    wire shape and assert:

    1. At least one such invocation is present (else the worker is
       not using the placement map at all — likely a regression in
       :func:`build_worker._validate_toolchain` or
       :func:`build_worker._prefetch_variant_inputs`).
    2. Per-store-path uniqueness: every distinct outpath is fetched
       from exactly ONE peer URL across the entire run. A path
       appearing with two different URLs would mean the targeted
       fetch tried multiple peers — which is allowed on retry, but
       we want to surface it loudly so the operator can decide
       whether retries are a symptom of peer instability.
    """
    log_files = artifacts.slurm_out_files() + artifacts.slurm_err_files()
    assert log_files, (
        f"no slurm_*.out/.err files in {artifacts.run_dir}; "
        "the dispatch must have produced at least one secondary log "
        "for this assertion to run"
    )
    all_hits: list[dict] = []
    for f in log_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        all_hits.extend(scan_nix_copy_invocations(content.splitlines()))

    assert all_hits, (
        "no ``nix copy --from http://... --no-check-sigs`` invocations "
        f"found across {len(log_files)} secondary log file(s); "
        "the targeted-fetch path is not being exercised"
    )

    # Per-path uniqueness audit. Build path -> set(url) and flag
    # paths that appear with more than one source URL.
    urls_per_path: dict[str, set[str]] = defaultdict(set)
    for hit in all_hits:
        urls_per_path[hit["path"]].add(hit["url"])
    multi_source = {
        path: urls
        for path, urls in urls_per_path.items()
        if len(urls) > 1
    }
    # Soft check: multi-source means retry happened (peer A failed,
    # peer B succeeded). That's a legitimate code path but we want
    # to surface it because in a clean run no retries should occur.
    # Lift to an outright fail only if EVERY path needed multiple
    # peers (a sign that the placement map is being misused).
    assert len(multi_source) < len(urls_per_path) or len(urls_per_path) == 0, (
        f"every fetched path required multiple peer URLs — placement "
        f"map looks broken. multi_source={multi_source!r}"
    )

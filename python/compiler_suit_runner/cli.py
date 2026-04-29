"""Command-line entry point for ``compiler_suit_runner``.

This module wires :mod:`compiler_suit_runner.preflight`,
:mod:`compiler_suit_runner.manifest_gen`, :mod:`compiler_suit_runner.suit_task`
and :mod:`compiler_suit_runner.incremental_cache` into a small argparse
front-end. Subcommands:

* ``submit`` — primary host's flow: pre-flight (or incremental cache
  hit), emit manifests, dispatch the run via either the in-process
  single-process loop or the dynamic_runner SLURM bridge.
* ``secondary`` — secondary container entry: bring up peer-cache state
  and live until told to stop. SLURM dispatch handles per-item work.
* ``preflight`` — pre-flight only; print the manifest count by class.
* ``clear-cache`` — invalidate the local incremental cache (or one
  specific hash).

Single-process execution is implemented inline via
:func:`run_single_process` because it is small enough to keep with the
CLI; SLURM execution defers to dynamic_runner's pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import socket
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from typing import Optional

from compiler_suit_runner.incremental_cache import (
    CacheEntry,
    DEFAULT_CACHE_ROOT,
    IncrementalCache,
    InputHashInputs,
    collect_input_hash_inputs,
    compute_input_hash,
)
from compiler_suit_runner.manifest_gen import emit_all_manifests
from compiler_suit_runner.preflight import (
    PreflightResult,
    preflight as run_preflight,
)
from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig


__all__ = [
    "build_parser",
    "main",
    "cmd_submit",
    "cmd_secondary",
    "cmd_preflight",
    "cmd_clear_cache",
    "run_single_process",
]


_VALID_MULTI_COMPUTER = ("single-process", "slurm")
_VALID_PACKAGING = ("podman", "none")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add the args every subcommand shares.

    These are kept on each subparser (rather than on the top-level
    parser) so ``--help`` for each subcommand is self-contained.
    """
    parser.add_argument(
        "--flake",
        default=".",
        help="Flake reference to evaluate (default: '.')",
    )
    parser.add_argument(
        "--shared-fs",
        type=pathlib.Path,
        help="Shared filesystem root used by the run for peers/, "
        "manifests/, partition/, dataset/.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run identifier (default: timestamp).",
    )
    parser.add_argument(
        "--sys",
        dest="sys_name",
        default="x86_64-linux",
        help="Flake system attribute (default: x86_64-linux).",
    )
    parser.add_argument(
        "--packages",
        nargs="+",
        default=None,
        metavar="PKG",
        help="Limit to these matrix packages (default: all).",
    )
    parser.add_argument(
        "--archs",
        nargs="+",
        default=None,
        metavar="ARCH",
        help="Limit to these target architectures (default: all).",
    )
    parser.add_argument(
        "--multi-computer",
        choices=_VALID_MULTI_COMPUTER,
        default="single-process",
        help="Distribution mode (default: single-process).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Workers per secondary (default: cpu_count).",
    )
    parser.add_argument(
        "--packaging",
        choices=_VALID_PACKAGING,
        default="none",
        help="SLURM packaging method (default: none).",
    )
    parser.add_argument(
        "--gateway",
        default=None,
        help="SLURM gateway URL (only used with --multi-computer slurm).",
    )
    parser.add_argument(
        "--slurm-root-folder",
        type=pathlib.Path,
        default=None,
        help="SLURM root folder on the gateway.",
    )
    parser.add_argument(
        "--cachix-cache",
        default=None,
        help="Cachix cache name to push toolchain outputs to (optional).",
    )
    parser.add_argument(
        "--cachix-auth-token-file",
        type=pathlib.Path,
        default=None,
        help="Path to the Cachix auth token (mode 0400).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the incremental cache (always pre-flight).",
    )
    parser.add_argument(
        "--cache-root",
        type=pathlib.Path,
        default=DEFAULT_CACHE_ROOT,
        help="Local incremental-cache root.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner",
        description=(
            "Driver for the asm-dataset-nix variant matrix. Submits a"
            " full multi-phase build to a SLURM cluster (or runs in a"
            " single process for testing)."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose debug logging.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="Kick off a run from this host.")
    _add_common_args(p_submit)

    p_secondary = sub.add_parser(
        "secondary", help="Run as a per-node secondary worker."
    )
    _add_common_args(p_secondary)
    p_secondary.add_argument(
        "--secondary-id",
        required=False,
        help="Unique identifier for this secondary.",
    )

    p_preflight = sub.add_parser(
        "preflight",
        help="Run preflight only and print the manifest count.",
    )
    _add_common_args(p_preflight)

    p_clear = sub.add_parser(
        "clear-cache",
        help="Invalidate the incremental cache.",
    )
    p_clear.add_argument(
        "--cache-root",
        type=pathlib.Path,
        default=DEFAULT_CACHE_ROOT,
        help="Local incremental-cache root.",
    )
    p_clear.add_argument(
        "--hash",
        default=None,
        help=(
            "Specific input_hash to invalidate (default: clear the entire"
            " cache root)."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(debug: bool) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("compiler_suit_runner")


def _resolve_run_id(args: argparse.Namespace) -> str:
    if getattr(args, "run_id", None):
        return args.run_id
    return time.strftime("%Y%m%dT%H%M%S")


def _resolve_jobs(args: argparse.Namespace) -> int:
    if getattr(args, "jobs", None):
        return max(1, int(args.jobs))
    return max(1, os.cpu_count() or 1)


def _config_from_args(
    args: argparse.Namespace,
    *,
    run_id: str,
    secondary_id: str,
    input_hash: str,
    toolchain_drvs: frozenset[str],
    variants: tuple,
) -> SuitTaskConfig:
    """Translate the parsed argparse namespace into a SuitTaskConfig.

    The shared FS subdirectories (manifests/, partition/, dataset/,
    peers/) are derived from ``--shared-fs``.
    """
    shared = pathlib.Path(args.shared_fs)
    return SuitTaskConfig(
        flake_ref=args.flake,
        sys_name=args.sys_name,
        shared_fs=shared,
        manifest_dir=shared / "manifests",
        raw_partition_dir=shared / "partition" / "raw",
        partition_dir=shared / "partition",
        dataset_dir=shared / "dataset",
        peers_dir=shared / "peers",
        run_id=run_id,
        secondary_id=secondary_id,
        hostname=socket.gethostname(),
        cachix_cache=args.cachix_cache,
        cachix_token_file=args.cachix_auth_token_file,
        input_hash=input_hash,
        toolchain_drvs=toolchain_drvs,
        variants=variants,
        # Defaults the user is unlikely to override from the CLI; tests
        # build SuitTaskConfig directly when they need to tweak these.
        enable_harmonia=False,
    )


def _emit_manifests_from_preflight(
    *,
    target_dir: pathlib.Path,
    sys_name: str,
    pre: PreflightResult,
    num_workers: int,
):
    """Bridge :mod:`preflight` output into ``emit_all_manifests``."""
    return emit_all_manifests(
        target_dir=target_dir,
        sys_name=sys_name,
        variants=pre.variants,
        toolchain_specs=pre.toolchain_specs,
        common_deps=pre.common_dep_drvs,
        num_workers=num_workers,
    )


def _restore_manifests_from_archive(
    archive: pathlib.Path, target_dir: pathlib.Path
) -> None:
    """Extract a cache-stored ``manifests.tar`` into ``target_dir``.

    The archive's root entry is ``manifests/`` (see
    :class:`IncrementalCache`); we strip that prefix so files land
    directly in ``target_dir``.

    A special ``manifests/_preflight.json`` entry, when present, holds
    the serialized :class:`PreflightResult` we wrote at cache-store
    time. We use it to re-emit manifests via
    :func:`compiler_suit_runner.manifest_gen.emit_all_manifests` rather
    than extracting potentially-huge sparse manifest files from the
    archive (the cache only carries the small JSON descriptor; the
    sparse files are recreated locally).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    preflight_blob: Optional[bytes] = None
    with tarfile.open(archive, mode="r") as tf:
        for member in tf.getmembers():
            relative = (
                member.name.split("/", 1)[1]
                if "/" in member.name
                else member.name
            )
            if not relative or relative.startswith(".."):
                continue
            if relative == "_preflight.json":
                f = tf.extractfile(member)
                if f is not None:
                    preflight_blob = f.read()
                continue
            # We deliberately do NOT extract other members: the live
            # manifests are sparse multi-PiB files that we only want to
            # materialize once via emit_all_manifests below.

    if preflight_blob is None:
        raise RuntimeError(
            "cache archive missing _preflight.json; cannot restore manifests"
        )

    pre_dict = json.loads(preflight_blob.decode("utf-8"))
    sys_name = pre_dict.get("sys_name", "x86_64-linux")
    variants = tuple(
        {
            "label": v["label"],
            "drv": v["drv"],
            "tarball_name": v["tarball_name"],
            "compiler_id": v["compiler_id"],
            "tier": int(v["tier"]),
            "pkg": v["pkg"],
            "arch": v["arch"],
        }
        for v in pre_dict.get("variants", [])
    )
    toolchain_specs = tuple(
        (entry[0], entry[1]) for entry in pre_dict.get("toolchain_specs", [])
    )
    common_dep_drvs = tuple(
        (entry[0], entry[1]) for entry in pre_dict.get("common_dep_drvs", [])
    )
    num_workers = int(pre_dict.get("num_workers", 1))
    emit_all_manifests(
        target_dir=target_dir,
        sys_name=sys_name,
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_dep_drvs,
        num_workers=num_workers,
    )


def _serialize_preflight_for_cache(
    pre: PreflightResult, num_workers: int, target_path: pathlib.Path
) -> None:
    """Write the cacheable subset of a :class:`PreflightResult` as JSON.

    We only persist what :func:`emit_all_manifests` needs to recreate the
    manifest tree on a future cache hit: variants, toolchain_specs,
    common_dep_drvs, sys_name, num_workers. Storing the sparse manifest
    files themselves would explode the cache (each is up to ~1.5 PiB
    apparent-size).
    """
    payload = {
        "sys_name": pre.sys_name,
        "variants": [dict(v) for v in pre.variants],
        "toolchain_specs": [list(p) for p in pre.toolchain_specs],
        "common_dep_drvs": [list(p) for p in pre.common_dep_drvs],
        "num_workers": num_workers,
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(payload, sort_keys=True, indent=2))


def _compute_input_hash(repo_root: pathlib.Path) -> str:
    """Compute the cache key from the flake state."""
    inputs = collect_input_hash_inputs(repo_root)
    return compute_input_hash(inputs)


# ---------------------------------------------------------------------------
# Single-process execution
# ---------------------------------------------------------------------------


def run_single_process(
    config: SuitTaskConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Drive the entire pipeline in this process — no SLURM, no podman.

    1. Construct :class:`SuitTask`.
    2. ``setup_peer_cache``.
    3. ``find_binaries``; sort by encoded size DESC (matching the Rust
       scheduler's behaviour).
    4. ``initialize_counters`` so the bookkeeping fires when each phase
       hits its expected count, writing the next phase's barrier flag.
    5. Dispatch each item via ``dispatch_binary``.
    6. ``teardown``.

    Returns 0 on success, 1 on any exception during item iteration.
    """
    log = logger or logging.getLogger(__name__)
    task = SuitTask(config)
    try:
        task.setup_peer_cache()

        binaries = task.find_binaries()
        # Sort by size DESC so larger items dispatch first. The
        # framework now owns inter-phase ordering via PhaseSpec; this
        # in-process loop is kept only for tests.
        binaries.sort(
            key=lambda b: getattr(b, "size", 0),
            reverse=True,
        )

        log.info(
            "single-process run: dispatching %d items", len(binaries)
        )
        for binary in binaries:
            task.dispatch_binary(binary)

        # Make sure dataset_dir exists even on an empty matrix.
        config.dataset_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — never raise out
        log.exception("single-process run failed")
        return 1
    finally:
        try:
            task.teardown()
        except Exception:  # noqa: BLE001
            log.exception("teardown failed")
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_submit(args: argparse.Namespace) -> int:
    """Primary host: pre-flight (or cache hit) -> manifests -> dispatch.

    The cache-hit shortcut keys on the input_hash and copies
    ``manifests.tar`` out of the cache into the shared FS manifest dir,
    skipping the local pre-flight entirely.
    """
    log = _setup_logging(args.debug)

    if args.shared_fs is None:
        log.error("--shared-fs is required for submit")
        return 2

    shared_fs = pathlib.Path(args.shared_fs)
    shared_fs.mkdir(parents=True, exist_ok=True)
    manifest_dir = shared_fs / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    run_id = _resolve_run_id(args)
    num_workers = _resolve_jobs(args)

    cache = IncrementalCache(args.cache_root)

    # Compute input hash; failure here is non-fatal — we still proceed
    # without caching, mirroring the plan's "cache invalidation" notes.
    input_hash = ""
    repo_root = pathlib.Path(args.flake).resolve() if args.flake != "." else pathlib.Path.cwd()
    try:
        input_hash = _compute_input_hash(repo_root)
    except Exception:  # noqa: BLE001
        log.warning(
            "failed to compute input hash; continuing without cache",
            exc_info=True,
        )

    cache_hit: Optional[CacheEntry] = None
    if input_hash and not args.no_cache:
        cache_hit = cache.lookup(input_hash)

    pre: Optional[PreflightResult] = None

    if cache_hit is not None:
        log.info("cache hit: %s", input_hash)
        try:
            _restore_manifests_from_archive(
                cache_hit.manifests_archive, manifest_dir
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to restore manifests from cache; will pre-flight")
            cache_hit = None

    if cache_hit is None:
        log.info("running pre-flight")
        try:
            pre = run_preflight(
                args.flake,
                args.sys_name,
                packages=args.packages,
                archs=args.archs,
            )
        except Exception:  # noqa: BLE001
            log.exception("pre-flight failed")
            return 1
        try:
            _emit_manifests_from_preflight(
                target_dir=manifest_dir,
                sys_name=args.sys_name,
                pre=pre,
                num_workers=num_workers,
            )
        except Exception:  # noqa: BLE001
            log.exception("manifest emission failed")
            return 1

    # Build SuitTaskConfig.
    toolchain_drvs = pre.toolchain_drvs if pre is not None else frozenset()
    variants = tuple(pre.variants) if pre is not None else ()

    config = _config_from_args(
        args,
        run_id=run_id,
        secondary_id="primary",
        input_hash=input_hash,
        toolchain_drvs=toolchain_drvs,
        variants=variants,
    )

    rc = 0
    if args.multi_computer == "single-process":
        rc = run_single_process(config, logger=log)
    elif args.multi_computer == "slurm":
        # Defer to dynamic_runner's SLURM pipeline. Imported lazily so
        # the test environment does not require the native extension.
        try:
            from dynamic_runner import run as dynamic_runner_run  # type: ignore
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SLURM dispatch requires the dynamic-runner package: %s",
                exc,
            )
            return 1
        try:
            task = SuitTask(config)
            dynamic_runner_run(task)
        except Exception:  # noqa: BLE001
            log.exception("SLURM dispatch failed")
            return 1
    else:
        log.error("unknown --multi-computer mode %r", args.multi_computer)
        return 2

    if rc == 0 and input_hash and pre is not None and not args.no_cache:
        # On success, write the cache so future runs short-circuit.
        try:
            partition_path = config.partition_dir / "partition.json"
            meta_path = manifest_dir / "_meta.json"
            # If a partition.json was never produced (single-process tests
            # don't run phase 1b), synthesize a placeholder so the cache
            # entry is still complete.
            if not partition_path.exists():
                config.partition_dir.mkdir(parents=True, exist_ok=True)
                partition_path.write_text("{}")
            if not meta_path.exists():
                meta_path.write_text("{}")

            # Stage a tiny dir with just the preflight descriptor to
            # avoid tarring up multi-PiB sparse manifest files.
            staging = tempfile.mkdtemp(prefix="suit-runner-cache-")
            try:
                staging_path = pathlib.Path(staging)
                _serialize_preflight_for_cache(
                    pre, num_workers, staging_path / "_preflight.json"
                )
                cache.store(
                    input_hash=input_hash,
                    partition_path=partition_path,
                    manifests_dir=staging_path,
                    meta_path=meta_path,
                )
                log.info("cache stored: %s", input_hash)
            finally:
                import shutil as _shutil
                _shutil.rmtree(staging, ignore_errors=True)
        except Exception:  # noqa: BLE001
            log.warning("cache store failed", exc_info=True)

    return rc


def cmd_secondary(args: argparse.Namespace) -> int:
    """Secondary container entry.

    For SLURM: framework's worker pool drives item dispatch via
    ``SuitTask.dispatch_binary``. We just need to bring up peer-cache
    state and live until the primary signals stop. For single-process
    submission this command is unused.
    """
    log = _setup_logging(args.debug)

    if args.shared_fs is None:
        log.error("--shared-fs is required for secondary")
        return 2

    secondary_id = args.secondary_id or socket.gethostname()
    run_id = _resolve_run_id(args)
    config = _config_from_args(
        args,
        run_id=run_id,
        secondary_id=secondary_id,
        input_hash="",
        toolchain_drvs=frozenset(),
        variants=(),
    )

    task = SuitTask(config)
    try:
        task.setup_peer_cache()
    except Exception:  # noqa: BLE001
        log.exception("secondary setup failed")
        return 1

    try:
        # Iterate manifests once. The framework normally drives this
        # via its own dispatch loop; we only support single-process
        # testing here. SLURM's framework integration is handled by
        # the parent via dynamic_runner.run; this entrypoint exists so
        # containers can `python -m compiler_suit_runner secondary
        # --secondary-id X`.
        for binary in task.find_binaries():
            task.dispatch_binary(binary)
    except Exception:  # noqa: BLE001
        log.exception("secondary dispatch loop failed")
        return 1
    finally:
        try:
            task.teardown()
        except Exception:  # noqa: BLE001
            log.exception("teardown failed")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Run preflight only and print the manifest count by class."""
    log = _setup_logging(args.debug)

    try:
        pre = run_preflight(
            args.flake,
            args.sys_name,
            packages=args.packages,
            archs=args.archs,
        )
    except Exception:  # noqa: BLE001
        log.exception("preflight failed")
        return 1

    print(f"sys: {pre.sys_name}")
    print(f"variants: {len(pre.variants)}")
    print(f"toolchains: {len(pre.toolchain_specs)}")
    print(f"common_deps: {len(pre.common_dep_drvs)}")
    print(f"distinct_drvs: {len(pre.toolchain_drvs)}")
    return 0


def cmd_clear_cache(args: argparse.Namespace) -> int:
    """Invalidate the local incremental cache."""
    log = _setup_logging(getattr(args, "debug", False))

    cache = IncrementalCache(args.cache_root)
    if args.hash:
        cache.invalidate(args.hash)
        log.info("invalidated cache entry %s", args.hash)
        return 0
    removed = cache.clear()
    log.info("cleared %d cache entries from %s", removed, args.cache_root)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


_DISPATCH = {
    "submit": cmd_submit,
    "secondary": cmd_secondary,
    "preflight": cmd_preflight,
    "clear-cache": cmd_clear_cache,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Top-level entry. Returns exit code (0 on success).

    Catches SystemExit raised by argparse for invalid invocations and
    funnels it through the return-code surface; this lets test code use
    ``main([...])`` without a try/except SystemExit dance.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse uses SystemExit for both ``--help`` (code 0) and bad
        # input (code 2). Surface the code unchanged.
        return int(exc.code) if exc.code is not None else 2

    handler = _DISPATCH.get(args.command)
    if handler is None:
        return 2
    return handler(args)

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
import dataclasses
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
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from compiler_suit_runner.partition import VariantSpec

from compiler_suit_runner.incremental_cache import (
    CacheEntry,
    DEFAULT_CACHE_ROOT,
    IncrementalCache,
    collect_input_hash_inputs,
    compute_input_hash,
)
from compiler_suit_runner.manifest_gen import emit_all_manifests
from compiler_suit_runner.preflight import (
    PreflightResult,
    filter_existing_variants,
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
        "--build-max-concurrent",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Global concurrency cap on build-heavy task types "
            "(toolchain, common_dep, variant). Unset = unconstrained. "
            "Each variant build itself spawns parallel compiler invocations,"
            " so a value around cpu_count/4 prevents oversubscription on "
            "small clusters."
        ),
    )
    parser.add_argument(
        "--variant-sample",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Down-sample the variant matrix: keep only N random "
            "(flag, hardening) combinations per (compiler, arch, opt) "
            "group. 0 (default) = no sampling, full matrix. Sample is "
            "deterministic given --variant-seed; change the seed to "
            "draw a different subset on a follow-up run (skip-existing "
            "then unions the two)."
        ),
    )
    parser.add_argument(
        "--variant-seed",
        default="42",
        metavar="SEED",
        help=(
            "Seed string for --variant-sample (default: '42'). Per-group "
            "RNG is keyed on f'{seed}:{compiler}:{arch}:{opt}', so "
            "changing this value reshuffles every group independently."
        ),
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
    # Submitter-peer integration: makes the dispatching machine's
    # local nix store available as a federated harmonia peer to
    # every compute-node container. ON by default for slurm
    # dispatch (it's the whole point of "the computer who started
    # the slurm task can also be a peer"). --no-submitter-peer
    # disables; --submitter-harmonia-port picks the listening port.
    parser.add_argument(
        "--no-submitter-peer",
        dest="submitter_peer",
        action="store_false",
        default=True,
        help=(
            "Disable the submitter-as-peer integration "
            "(skip running harmonia + reverse-tunnel from this "
            "machine to the gateway)."
        ),
    )
    parser.add_argument(
        "--submitter-harmonia-port",
        type=int,
        default=5005,
        help=(
            "Port to bind the submitter's harmonia on (locally and "
            "via reverse-tunnel on the gateway). Default 5005; "
            "compute-nodes hit <gateway-host>:<this-port>."
        ),
    )
    # ssh-debug back-door: spawn sshd inside each container so an
    # operator can ssh in mid-run for live debugging. Off by default
    # — image always ships the bits, but the actual sshd process
    # only starts when this flag is set.
    parser.add_argument(
        "--enable-ssh-debug",
        action="store_true",
        default=False,
        help=(
            "Spawn sshd in each SLURM container for live debugging "
            "during compilation. Connect via "
            "`ssh -i .ssh-debug/id_ed25519 -o IdentitiesOnly=yes "
            "-J <gateway> root@<compute-node> -p <port>` once the "
            "ready marker appears at "
            "~/BIG/slurm/log/<run_id>/ssh-debug.<host>.<port>.ready."
        ),
    )
    parser.add_argument(
        "--ssh-debug-port",
        type=int,
        default=22222,
        help=(
            "Port the in-container sshd binds on (default 22222). "
            "Container uses --network host so this is also the port "
            "the operator hits on <compute-node>."
        ),
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


# CSR-only flags that the framework's argparse doesn't know about.
# The framework re-parses sys.argv when `dynamic_runner.run(task)` is
# invoked, and chokes on any flag it can't resolve — so we strip these
# before handing off. Flags taking a value (single or nargs="+") are
# listed in `_CSR_FLAGS_WITH_VALUE`; boolean / store_true flags are in
# `_CSR_BOOL_FLAGS`. Everything not in either set passes through
# unchanged (e.g. --gateway, --multi-computer, --packaging,
# --slurm-root-folder, --jobs — those the framework owns).
_CSR_FLAGS_WITH_VALUE: frozenset[str] = frozenset({
    "--flake",
    "--shared-fs",
    "--run-id",
    "--sys",
    "--cachix-cache",
    "--cachix-auth-token-file",
    "--cache-root",
    "--submitter-harmonia-port",
    "--ssh-debug-port",
    "--build-max-concurrent",
    "--variant-sample",
    "--variant-seed",
    "--hash",
    # nargs="+" — may be followed by multiple values
    "--packages",
    "--archs",
})
_CSR_NARGS_PLUS: frozenset[str] = frozenset({
    "--packages",
    "--archs",
})
_CSR_BOOL_FLAGS: frozenset[str] = frozenset({
    "--no-cache",
    "--no-submitter-peer",
    "--enable-ssh-debug",
})
_CSR_SUBCOMMANDS: frozenset[str] = frozenset({
    "submit", "secondary", "preflight", "clear-cache",
})


def _strip_csr_argv_for_framework(argv: list[str]) -> list[str]:
    """Remove CSR-only verbs and flags from argv before the framework
    re-parses it. Preserves order of the remaining tokens.

    Handles three forms:
      ``--flag value``        (consumes one trailing token)
      ``--flag=value``        (single token)
      ``--flag v1 v2 ...``    (nargs="+" — consumes all following non-flag tokens)
    """
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok in _CSR_SUBCOMMANDS:
            i += 1
            continue
        if tok in _CSR_BOOL_FLAGS:
            i += 1
            continue
        if "=" in tok:
            head = tok.split("=", 1)[0]
            if head in _CSR_FLAGS_WITH_VALUE or head in _CSR_BOOL_FLAGS:
                i += 1
                continue
        if tok in _CSR_FLAGS_WITH_VALUE:
            i += 1
            if tok in _CSR_NARGS_PLUS:
                while i < n and not argv[i].startswith("-"):
                    i += 1
            elif i < n:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _config_from_args(
    args: argparse.Namespace,
    *,
    run_id: str,
    secondary_id: str,
    input_hash: str,
    toolchain_drvs: frozenset[str],
    variants: tuple["VariantSpec", ...],
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
        # Harmonia ON by default — it's the whole point of cluster
        # peer-cache federation. The on_run_start lifecycle hook
        # starts nix-daemon + harmonia + PeerListWatcher +
        # PeerNixConfWatcher as a single unit.
        enable_harmonia=True,
        # Opt-in ssh-debug back-door. Off by default; the user
        # toggles via --enable-ssh-debug.
        enable_ssh_debug=getattr(args, "enable_ssh_debug", False),
        ssh_debug_port=getattr(args, "ssh_debug_port", 22222),
        build_max_concurrent=getattr(args, "build_max_concurrent", None),
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

    from compiler_suit_runner.partition import VariantSpec

    pre_dict = json.loads(preflight_blob.decode("utf-8"))
    sys_name = pre_dict.get("sys_name", "x86_64-linux")
    variants: tuple[VariantSpec, ...] = tuple(
        VariantSpec(
            label=v["label"],
            drv=v["drv"],
            tarball_name=v["tarball_name"],
            compiler_id=v["compiler_id"],
            tier=int(v["tier"]),
            pkg=v["pkg"],
            arch=v["arch"],
        )
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
    """Drive the pipeline in this process via the framework's runner.

    Constructs a :class:`SuitTask`, hands it to ``dynamic_runner.run``,
    and lets the framework own setup / teardown via the
    ``on_run_start`` / ``on_run_end`` lifecycle hooks. Falls back to
    the legacy in-process dispatch loop when ``dynamic_runner`` is
    not importable (the consumer's hermetic test environment).

    Returns 0 on success, 1 on any unhandled exception.
    """
    log = logger or logging.getLogger(__name__)
    task = SuitTask(config)

    try:
        from dynamic_runner.run import run as dynamic_runner_run  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 — framework absent
        log.warning(
            "dynamic_runner.run unavailable (%s);"
            " falling back to legacy in-process dispatch loop",
            exc,
        )
        return _legacy_run_single_process(task, config, log)

    try:
        # The framework's ``run`` consumes argparse via sys.argv; we
        # let it do so and rely on lifecycle hooks for setup.
        dynamic_runner_run(task)
        config.dataset_dir.mkdir(parents=True, exist_ok=True)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
    except Exception:  # noqa: BLE001 — never raise out
        log.exception("single-process run failed")
        return 1
    return 0


def _legacy_run_single_process(
    task: SuitTask,
    config: SuitTaskConfig,
    log: logging.Logger,
) -> int:
    """Fallback in-process dispatch loop (kept for hermetic tests).

    Used only when ``dynamic_runner.run`` cannot be imported. Sets up
    peer-cache state via the lifecycle hooks, dispatches each
    discovered item via :meth:`SuitTask.dispatch_binary`, then tears
    state back down.
    """
    success = False
    try:
        task.on_run_start()
        binaries = task.find_binaries()
        binaries.sort(key=lambda b: getattr(b, "size", 0), reverse=True)
        log.info(
            "legacy single-process run: dispatching %d items",
            len(binaries),
        )
        for binary in binaries:
            task.dispatch_binary(binary)
        config.dataset_dir.mkdir(parents=True, exist_ok=True)
        success = True
    except Exception:  # noqa: BLE001 — never raise out
        log.exception("legacy single-process run failed")
    finally:
        try:
            task.on_run_end(success)
        except Exception:  # noqa: BLE001
            log.exception("on_run_end failed")
    return 0 if success else 1


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
                sample_size=getattr(args, "variant_sample", 0) or 0,
                sample_seed=getattr(args, "variant_seed", "42") or "42",
            )
        except Exception:  # noqa: BLE001
            log.exception("pre-flight failed")
            return 1

        sample_size = getattr(args, "variant_sample", 0) or 0
        if sample_size > 0:
            log.info(
                "variant sampling: %d kept (sample=%d, seed=%r)",
                len(pre.variants), sample_size, getattr(args, "variant_seed", "42"),
            )

        # Skip-existing: drop variants whose tarball is already on disk.
        # Phase-2 toolchains / common-deps go through nix's own
        # substitution path so don't need an explicit skip; phase-3
        # variants land as flat .tar.zst files in dataset_dir, so we
        # check existence there.
        dataset_dir = pathlib.Path(args.shared_fs) / "dataset"
        kept_variants, skipped = filter_existing_variants(
            pre.variants, dataset_dir=dataset_dir
        )
        if skipped:
            log.info(
                "skip-existing: %d variants already built; %d remain",
                skipped, len(kept_variants),
            )
            pre = dataclasses.replace(pre, variants=kept_variants)

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
            from dynamic_runner import (  # type: ignore[import-not-found]
                TaskDeploymentSpec,
                run as dynamic_runner_run,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "SLURM dispatch requires the dynamic-runner package: %s",
                exc,
            )
            return 1

        # Submitter-peer: makes the dispatching machine's local nix
        # store reachable from compute-node containers as a federated
        # peer cache. The framework's TaskDeploymentSpec.extra_port_forwards
        # (added in dynamic-runner afa024e) does the SSH-R on the
        # primary's existing ControlMaster — we just run harmonia
        # locally + drop the peer-info file on the gateway.
        # Skipped if --no-submitter-peer.
        submitter = None
        extra_pf: tuple[tuple[int, int], ...] = ()
        if (
            getattr(args, "submitter_peer", True)
            and args.gateway
            and args.slurm_root_folder
        ):
            try:
                from .peer_cache import SubmitterPeer
                port = getattr(args, "submitter_harmonia_port", 5005)
                submitter = SubmitterPeer(
                    gateway_url=args.gateway,
                    slurm_root=str(args.slurm_root_folder),
                    local_port=port,
                    gateway_port=port,
                    log=log,
                )
                submitter.start()
                extra_pf = submitter.deployment_extra_port_forwards
            except Exception:  # noqa: BLE001 — never block dispatch
                log.exception(
                    "submitter-peer startup failed; dispatch continues"
                    " without dev-box harmonia"
                )
                submitter = None
                extra_pf = ()

        # Strip CSR-only flags from sys.argv before handing off:
        # `dynamic_runner.run` re-parses sys.argv with its OWN
        # argparse, which doesn't know about --shared-fs / --packages /
        # --archs / --enable-ssh-debug / etc. Without this, the
        # framework dies with "unrecognized arguments" right after
        # preflight. The remaining tokens (--gateway, --multi-computer,
        # --packaging, --slurm-root-folder, --jobs, --debug) flow
        # through untouched.
        original_argv = sys.argv
        forwarded = _strip_csr_argv_for_framework(original_argv[1:])
        # SuitTask doesn't have a real "source dir" — items come from
        # the preflight-emitted manifests, not from walking a binaries
        # tree. The framework still validates that `--source` exists,
        # so point it at the run's shared FS root (`--shared-fs`) which
        # we already created. Not a placeholder: it's the actual root
        # of every artifact this run produces (manifests/, partition/,
        # peers/, dataset/).
        if "--source" not in forwarded and not any(
            t.startswith("--source=") for t in forwarded
        ):
            forwarded += ["--source", str(shared_fs)]
        # `--output` is the real destination for finished binary tars:
        # SuitTask threads `config.dataset_dir` into every build worker
        # via `--dataset-output-dir` (suit_task.py:_command_for_type),
        # so the workers write `.tar.zst` artifacts directly into this
        # directory. Mirror that here so the framework's `args.output`
        # surface (used by lifecycle / stats reporting) points at the
        # same location.
        config.dataset_dir.mkdir(parents=True, exist_ok=True)
        if "--output" not in forwarded and not any(
            t.startswith("--output=") for t in forwarded
        ):
            forwarded += ["--output", str(config.dataset_dir)]
        sys.argv = [original_argv[0]] + forwarded
        log.debug("forwarded argv to dynamic_runner: %s", sys.argv)
        try:
            task = SuitTask(config)
            deployment = TaskDeploymentSpec(
                secondary_module="compiler_suit_runner",
                image_name="asm-dataset-nix-runner",
                extra_port_forwards=extra_pf,
            )
            dynamic_runner_run(task, deployment=deployment)
        except Exception:  # noqa: BLE001
            log.exception("SLURM dispatch failed")
            return 1
        finally:
            sys.argv = original_argv
            if submitter is not None:
                try:
                    submitter.stop()
                except Exception:  # noqa: BLE001
                    log.exception("submitter-peer shutdown failed")
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

    Hands the SuitTask off to ``dynamic_runner.run`` so the
    framework's secondary coordinator drives item dispatch; lifecycle
    hooks own peer-cache setup / teardown.
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
        from dynamic_runner import (  # type: ignore[import-not-found]
            TaskDeploymentSpec,
            run as dynamic_runner_run,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "secondary mode requires dynamic_runner: %s", exc
        )
        return 1

    # Bring up secondary-local peer-cache state BEFORE handing off to
    # the framework's secondary loop: nix-daemon, harmonia (signing
    # key + bind), PeerListWatcher, PeerNixConfWatcher (rewrites
    # /etc/nix/peer.conf as the peer set changes), and optionally an
    # opt-in sshd back-door. ``SuitTask.on_run_start`` is the
    # canonical setup path used on the primary; calling it here keeps
    # the secondary's container in the same federation/peer-cache
    # state, mirroring ssh_debug_runner.cli.cmd_secondary's explicit
    # ``bootstrap()`` step. The hook is idempotent (guarded by an
    # internal ``_setup_lock`` + ``_setup_done`` flag), so if the
    # framework also fires it we'll no-op.
    try:
        task.on_run_start()
    except Exception:  # noqa: BLE001 — never block dispatch
        log.exception(
            "secondary on_run_start failed; continuing without "
            "peer-cache federation (workers will still run, but "
            "won't fetch from sibling secondaries)"
        )

    # Same argv-strip as cmd_submit: framework re-parses sys.argv.
    sys.argv = [sys.argv[0]] + _strip_csr_argv_for_framework(sys.argv[1:])
    rc = 0
    try:
        deployment = TaskDeploymentSpec(
            secondary_module="compiler_suit_runner",
            image_name="asm-dataset-nix-runner",
        )
        dynamic_runner_run(task, deployment=deployment)
    except SystemExit as exc:
        rc = int(exc.code) if exc.code is not None else 0
    except Exception:  # noqa: BLE001
        log.exception("secondary dispatch failed")
        rc = 1
    finally:
        # Tear down peer-cache state (harmonia, watchers, sshd back-door
        # if opted in) symmetrically. on_run_end is idempotent; we
        # call it even if on_run_start partially failed so half-started
        # state still gets unwound.
        try:
            task.on_run_end(rc == 0)
        except Exception:  # noqa: BLE001
            log.exception("secondary on_run_end failed")
    return rc


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
    raw = list(sys.argv[1:] if argv is None else argv)

    # Framework-spawned secondary path: the dynamic_runner pipeline
    # invokes our image with ``python -m compiler_suit_runner
    # --secondary tcp://… --secondary-id … --secondary-quic-port …``
    # — no subcommand verb, no --shared-fs. Our argparse would
    # otherwise reject the `tcp://…` URL as an invalid `command`
    # choice. Mirror ssh_debug_runner's pattern: detect the framework
    # flag and route directly to cmd_secondary with a synthesized
    # namespace; the function fills in container-side defaults
    # (shared-fs = /app/log-network — the run-id-scoped gateway dir
    # bind-mounted into every secondary container).
    if "--secondary" in raw:
        # Pull the framework-assigned secondary id out of the raw argv
        # so SuitTaskConfig.secondary_id matches what the primary
        # coordinator dispatched. Framework spawns us with
        # ``--secondary-id <id>`` (next-token form, never =VALUE).
        sec_id: Optional[str] = None
        for j, tok in enumerate(raw):
            if tok == "--secondary-id" and j + 1 < len(raw):
                sec_id = raw[j + 1]
                break
            if tok.startswith("--secondary-id="):
                sec_id = tok.split("=", 1)[1]
                break
        ns = argparse.Namespace(
            debug="--debug" in raw,
            command="secondary",
            flake=".",
            shared_fs=pathlib.Path("/app/log-network"),
            run_id=None,
            sys_name="x86_64-linux",
            packages=None,
            archs=None,
            multi_computer="slurm",
            jobs=None,
            packaging="podman",
            gateway=None,
            slurm_root_folder=None,
            cachix_cache=None,
            cachix_auth_token_file=None,
            no_cache=False,
            cache_root=DEFAULT_CACHE_ROOT,
            submitter_peer=False,
            submitter_harmonia_port=5005,
            enable_ssh_debug=False,
            ssh_debug_port=22222,
            secondary_id=sec_id,
        )
        return cmd_secondary(ns)

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

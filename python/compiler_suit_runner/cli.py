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
import subprocess
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
from compiler_suit_runner.nix_drv_show import eval_drv_outpaths
from compiler_suit_runner.preflight import (
    PreflightError,
    PreflightResult,
    build_toolchains_locally,
    check_toolchains_locally,
    enumerate_toolchains_only,
    enumerate_variants,
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


def _non_negative_int(value: str) -> int:
    """argparse ``type`` for a non-negative integer.

    Mirrors the framework's own validation for
    ``--unfulfillable-reinject-max-per-task``: zero is a valid value
    (means "don't auto-reinject at all"), negatives are rejected with
    an ``ArgumentTypeError`` so argparse surfaces a clean error.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"expected an integer, got {value!r}",
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"value must be >= 0, got {parsed}",
        )
    return parsed


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
        "--system",
        "--sys",
        dest="sys_name",
        default="x86_64-linux",
        help=(
            "Flake system attribute that the run targets (default: "
            "x86_64-linux). Threaded into the matrix-eval manifest "
            "header and every worker dispatched from this submitter."
        ),
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
    # Forwarded to dynamic_runner's own argparse (deliberately NOT in
    # _CSR_FLAGS_WITH_VALUE, so the value stays in sys.argv after our
    # strip-pass and the framework re-reads it). Without forwarding,
    # the secondary's ``dynrunner_manager_local::pool`` auto-detects
    # cores via ``available_parallelism``, which inside a podman
    # container returns the HOST's full CPU count (cgroup CPU quota
    # isn't reflected in /proc/cpuinfo). On a 32-core host with a
    # 2-CPU cgroup that's 32 workers spawned per secondary, immediately
    # fork-storming the per-job cgroup before any work starts.
    parser.add_argument(
        "--cores",
        type=str,
        default=None,
        metavar="N",
        help=(
            "Forwarded to dynamic_runner --cores: int / +int / -int "
            "controlling workers-per-secondary. Defaults to the "
            "framework's own default (all detected cores). Set "
            "explicitly to match the cgroup CPU envelope."
        ),
    )
    parser.add_argument(
        "--max-memory",
        type=str,
        default=None,
        metavar="SPEC",
        help=(
            "Forwarded to dynamic_runner --max-memory: e.g. '3G', "
            "'8192M', '+1G', '-2G'. Defaults to the framework's own "
            "default (autodetected from /proc/meminfo). Set "
            "explicitly to match the cgroup memory envelope; "
            "autodetect doesn't see cgroup limits inside containers."
        ),
    )
    parser.add_argument(
        "--no-task-depends",
        action="store_true",
        default=False,
        help=(
            "Drop ``task_depends_on`` from TaskInfo at discovery time. "
            "Workaround for a dynamic_runner post-promotion bug where "
            "PendingPool.extend() rejects variants whose toolchain dep "
            "is in-flight (validator only seeds completed task_ids). "
            "Safe because nix's drv graph + harmonia federation order "
            "toolchain-before-variant at the build-lock level."
        ),
    )
    parser.add_argument(
        "--build-compilers",
        action="store_true",
        default=False,
        help=(
            "Enable Phase 1 ``build_compilers`` dispatch: secondaries "
            "build the cross-toolchains from source in-cluster and "
            "publish the closures into ``/out-network/_build_compilers/``. "
            "Default OFF: operators are expected to have every toolchain "
            "output pre-realised in the submitter's local store; missing "
            "toolchains are surfaced as a PreflightError at submit time. "
            "When ON, the manifest emitter switches from the validate-"
            "only ``toolchain_validate`` class to the building "
            "``build_compilers`` class and the Phase 1 stage is added "
            "to the submit-time stages list."
        ),
    )
    parser.add_argument(
        "--build-compiler-workers",
        type=_non_negative_int,
        default=1,
        metavar="N",
        help=(
            "Per-secondary concurrency cap for the Phase 1 "
            "``build_compilers`` worker (default: 1). Each toolchain "
            "build forks ``nix build`` with its own parallel-compile "
            "fanout, so the default of 1 keeps a single compile "
            "in-flight per secondary while still spreading across the "
            "cluster. Has effect only when ``--build-compilers`` is set."
        ),
    )
    parser.add_argument(
        "--debug-testbuild",
        default=None,
        metavar="BINARY",
        help=(
            "Enable the Phase 1.5 ``toolchain_validate`` step using "
            "BINARY (e.g. ``--debug-testbuild hello``). After Phase 1 "
            "completes the cluster builds BINARY against each toolchain "
            "as a fail-fast sanity check. Default OFF (no validation). "
            "Only meaningful in combination with ``--build-compilers``."
        ),
    )
    parser.add_argument(
        "--replication-k",
        type=int,
        default=3,
        metavar="K",
        help=(
            "K=3 toolchain-outpath replication target: number of "
            "distinct secondaries that should hold each toolchain "
            "outpath. On first receive, a secondary push-attempts to "
            "K-1 more peers; if a holder dies and the count drops "
            "below K, remaining holders repair. Default: 3. Set to "
            "1 to disable cascade/repair (every toolchain held by "
            "only its initial fetcher); 0 disables replication "
            "entirely. See plan: K=3 toolchain replication."
        ),
    )
    parser.add_argument(
        "--no-observer-as-holder",
        dest="allow_observer_as_holder",
        action="store_false",
        default=True,
        help=(
            "Don't count a late-attaching observer toward the K=3 "
            "holder set. Default: observers count (when Framework "
            "Ask #3 is shipped). Has no effect without observer "
            "support in the running dynrunner version."
        ),
    )
    parser.add_argument(
        "--unfulfillable-reinject-max-per-task",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help=(
            "Cap how many times a permanently-Unfulfillable task may "
            "auto-reinject when an observer broadcasts the missing "
            "outpath. Default: unbounded. Useful for flap-tolerance "
            "when a flaky peer keeps re-joining. Mirrors the framework "
            "kwarg of the same name; 0 disables auto-reinject entirely."
        ),
    )
    parser.add_argument(
        "--variant-sample",
        type=int,
        default=2,
        metavar="N",
        help=(
            "Down-sample the variant matrix: keep only N random "
            "(flag, hardening) combinations per (compiler, arch, opt) "
            "group. 0 = no sampling, full matrix. Default: 2. Sample is "
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
        "--max-variants",
        type=int,
        default=None,
        metavar="N",
        help=(
            "DEPRECATED — currently a no-op. The dependency-graph "
            "streaming planner picks its own size cap; this flag is "
            "retained only so legacy invocations don't blow up the "
            "argparse and will be removed (or wired into the streaming "
            "planner's size cap) in a future phase."
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
        "--slurm-partition",
        default=None,
        help=(
            "SLURM partition to submit jobs against (sbatch --partition). "
            "Pass-through to the framework; defaults to the framework's "
            "SlurmConfig.partition value when unset."
        ),
    )
    parser.add_argument(
        "--slurm-time-limit",
        default=None,
        help=(
            "Per-secondary SLURM job wallclock limit (sbatch --time format, "
            "e.g. '1:00:00'). Pass-through to the framework."
        ),
    )
    parser.add_argument(
        "--slurm-cpus-per-task",
        type=int,
        default=None,
        help=(
            "Per-secondary SLURM cpus-per-task (sbatch --cpus-per-task). "
            "Pass-through to the framework; defaults to the framework's "
            "SlurmConfig.cpus_per_task value (14) when unset. Must not "
            "exceed the cluster's per-node CPU count."
        ),
    )
    parser.add_argument(
        "--slurm-setup-deadline-secs",
        type=int,
        default=None,
        help=(
            "Per-secondary setup deadline (seconds). Pass-through to the "
            "framework; defaults to the framework's auto-scaled value "
            "max(60, num_secondaries * 15). Override only on clusters "
            "slower than LMU (e.g. slurm-test-env requires 600 to absorb "
            "the rootless-podman image load latency on shared /home)."
        ),
    )
    parser.add_argument(
        "--ssh-identity-file",
        default=None,
        help=(
            "Explicit private-key path for the gateway SSH connection. "
            "Pass-through to the framework (dynamic_runner cli flag of "
            "the same name); the value is forwarded verbatim. Use this "
            "to bypass ssh-agent / IdentityFile defaults when the "
            "gateway accepts only a specific key."
        ),
    )
    parser.add_argument(
        "--ssh-config",
        default=None,
        help=(
            "Explicit ssh-config path for the gateway SSH connection. "
            "Pass-through to the framework. Composes with "
            "--ssh-identity-file."
        ),
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
    "--system",
    "--cachix-cache",
    "--cachix-auth-token-file",
    "--cache-root",
    "--submitter-harmonia-port",
    "--ssh-debug-port",
    "--build-max-concurrent",
    "--variant-sample",
    "--variant-seed",
    "--max-variants",
    "--hash",
    "--replication-k",
    "--build-compiler-workers",
    "--debug-testbuild",
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
    "--no-task-depends",
    "--build-compilers",
    "--no-observer-as-holder",
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
) -> SuitTaskConfig:
    """Translate the parsed argparse namespace into a SuitTaskConfig.

    The shared FS subdirectories (manifests/, dataset/, peers/) are
    derived from ``--shared-fs``. Partition-era kwargs
    (``raw_partition_dir``, ``partition_dir``, ``input_hash``,
    ``toolchain_drvs``, ``variants``, ``common_threshold``) were dropped
    in the phase-taxonomy refactor; matrix_eval workers no longer
    materialise a partition tree and the per-binary outputs land in
    ``matrix_eval_out_dir`` instead.
    """
    shared = pathlib.Path(args.shared_fs)
    # ``dataset_dir`` defaults to a subdir of shared_fs (the dispatcher
    # case where shared_fs IS the run-wide scratch + output root).
    # In the secondary-container case the synthesized namespace
    # overrides this with the framework's actual ``--output`` mount
    # (``/app/out-network``) so finished tarballs don't end up wedged
    # under the per-run log dir.
    dataset_dir = pathlib.Path(getattr(args, "dataset_dir", None) or shared / "dataset")
    # matrix_eval_out_dir: explicit override wins; fall back to
    # ``<dataset_dir>/_matrix_eval``. On the submitter side this
    # resolves to ``<shared_fs>/dataset/_matrix_eval`` (host view of
    # the shared bind mount); on the secondary side the synthesised
    # namespace passes the container view
    # (``/app/out-network/_matrix_eval``) via getattr.
    _raw_me_out = getattr(args, "matrix_eval_out_dir", None)
    matrix_eval_out_dir = (
        pathlib.Path(_raw_me_out) if _raw_me_out is not None
        else dataset_dir / "_matrix_eval"
    )
    return SuitTaskConfig(
        flake_ref=args.flake,
        sys_name=args.sys_name,
        shared_fs=shared,
        manifest_dir=shared / "manifests",
        dataset_dir=dataset_dir,
        peers_dir=shared / "peers",
        run_id=run_id,
        secondary_id=secondary_id,
        hostname=socket.gethostname(),
        cachix_cache=args.cachix_cache,
        cachix_token_file=args.cachix_auth_token_file,
        matrix_eval_out_dir=matrix_eval_out_dir,
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
        disable_task_deps=getattr(args, "no_task_depends", False),
        allow_toolchain_build=getattr(args, "build_compilers", False),
        replication_k=getattr(args, "replication_k", 3),
        allow_observer_as_holder=getattr(
            args, "allow_observer_as_holder", True,
        ),
        unfulfillable_reinject_max_per_task=getattr(
            args, "unfulfillable_reinject_max_per_task", None,
        ),
    )


def _restore_manifests_from_archive(
    archive: pathlib.Path, target_dir: pathlib.Path
) -> dict[tuple[str, str], str]:
    """Extract a cache-stored ``manifests.tar`` into ``target_dir``.

    Returns the restored ``{(arch, compiler): drv_path}`` mapping so the
    caller can use it for the submitter placement broadcast on a
    cache hit (without it, the placement map has no submitter entry
    for toolchain outpaths, and the validate-only worker path fails
    with "no peer in the placement map could serve it" even though
    the toolchain manifests were restored correctly).

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
            variant_dir=v["variant_dir"],
            metadata_name=v["metadata_name"],
            compiler_id=v["compiler_id"],
            compiler_family=v["compiler_family"],
            compiler_version=v["compiler_version"],
            optimization=v["optimization"],
            flag_set=v["flag_set"],
            hardening=v["hardening"],
            sanitizer=v["sanitizer"],
            march=v["march"],
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
    tc_drvs: dict[tuple[str, str], str] = {
        (entry[0], entry[1]): entry[2]
        for entry in pre_dict.get("toolchain_drvs_by_pair", [])
        if isinstance(entry, list) and len(entry) == 3
    }
    num_workers = int(pre_dict.get("num_workers", 1))
    allow_toolchain_build = bool(pre_dict.get("allow_toolchain_build", False))
    emit_all_manifests(
        target_dir=target_dir,
        sys_name=sys_name,
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_dep_drvs,
        num_workers=num_workers,
        toolchain_drvs=tc_drvs,
        allow_toolchain_build=allow_toolchain_build,
    )
    return tc_drvs


def _serialize_preflight_for_cache(
    pre: PreflightResult,
    num_workers: int,
    target_path: pathlib.Path,
    *,
    toolchain_drvs_by_pair: Optional[dict[tuple[str, str], str]] = None,
    allow_toolchain_build: bool = False,
) -> None:
    """Write the cacheable subset of a :class:`PreflightResult` as JSON.

    We only persist what :func:`emit_all_manifests` needs to recreate the
    manifest tree on a future cache hit: variants, toolchain_specs,
    common_dep_drvs, sys_name, num_workers, and the realised toolchain
    drv paths keyed by (arch, compiler). Storing the sparse manifest
    files themselves would explode the cache (each is up to ~1.5 PiB
    apparent-size).

    Without ``toolchain_drvs_by_pair`` the restored toolchain manifests
    fall back to ``flake_ref#_crossToolchainMap.<...>``, which fails on
    SLURM secondaries (no flake.nix in /app).
    """
    tc_pairs = toolchain_drvs_by_pair or {}
    payload = {
        "sys_name": pre.sys_name,
        "variants": [dict(v) for v in pre.variants],
        "toolchain_specs": [list(p) for p in pre.toolchain_specs],
        "common_dep_drvs": [list(p) for p in pre.common_dep_drvs],
        "toolchain_drvs_by_pair": [
            [arch, compiler, drv]
            for (arch, compiler), drv in sorted(tc_pairs.items())
        ],
        "toolchain_aggregate_drv": pre.toolchain_aggregate_drv,
        "num_workers": num_workers,
        "allow_toolchain_build": bool(allow_toolchain_build),
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
    # Default-init these so the cache-hit path (which skips preflight)
    # doesn't hit UnboundLocalError when downstream code references them:
    # submitter placement broadcast, cache.store payload, etc. Cache
    # restore re-emits manifests with the persisted
    # ``allow_toolchain_build`` flag, so the placement-map plumbing isn't
    # needed there (workers find peers via gossip).
    tc_drvs: dict[tuple[str, str], str] = {}
    partition_drv_outpaths: Optional[dict[str, str]] = None

    if cache_hit is not None:
        log.info("cache hit: %s", input_hash)
        try:
            tc_drvs = _restore_manifests_from_archive(
                cache_hit.manifests_archive, manifest_dir
            )
            # Re-resolve toolchain outpaths from the local store so the
            # submitter placement block populates the gossip file even
            # on cache hit. ``eval_drv_outpaths`` is cheap (single
            # batched ``nix derivation show``) and idempotent.
            if tc_drvs:
                try:
                    tc_outpaths = eval_drv_outpaths(
                        [d for d in tc_drvs.values() if d]
                    )
                    if tc_outpaths:
                        partition_drv_outpaths = dict(tc_outpaths)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "cache-hit toolchain outpath eval failed;"
                        " submitter placement may be incomplete"
                    )
        except Exception:  # noqa: BLE001
            log.exception("failed to restore manifests from cache; will pre-flight")
            cache_hit = None
            tc_drvs = {}

    if cache_hit is None:
        # Submit path: only enumerate toolchains locally and emit
        # build_compilers (when --build-compilers) + matrix_eval
        # manifests. The slow per-binary drv-instantiation is deferred
        # to matrix_eval workers on secondaries (see
        # ``workers/eval_worker.py``). Phase 3+ tasks (dependency_graph,
        # build) are spawned at runtime by the primary's quiesce
        # watcher (``_MatrixEvalQuiesceWatcher`` in suit_task.py).
        log.info("running pre-flight (toolchains + per-binary metadata only)")
        try:
            tc_pairs, tc_drvs, tc_aggregate_drv = enumerate_toolchains_only(
                args.flake, args.sys_name, archs=args.archs,
            )
        except Exception:  # noqa: BLE001
            log.exception("toolchain enumeration failed")
            return 1
        try:
            per_binary_meta_raw = enumerate_variants(
                args.flake,
                args.sys_name,
                packages=args.packages,
                archs=args.archs,
                sample_size=getattr(args, "variant_sample", 0) or 0,
                sample_seed=getattr(args, "variant_seed", "42") or "42",
            )
        except Exception:  # noqa: BLE001
            log.exception("variant enumeration failed")
            return 1
        if not isinstance(per_binary_meta_raw, dict):
            log.error(
                "enumerate_variants returned %r instead of a dict; aborting",
                type(per_binary_meta_raw).__name__,
            )
            return 1

        # Flatten the {pkg: {"archs": [...], "suffixes_by_arch":
        # {arch: [...]}, "sample_size": ..., "sample_seed": ...}} shape
        # returned by enumerate_variants into the {binary: {"archs":
        # [...], "suffixes": [...], "variant_sample": ...,
        # "variant_seed": ...}} shape that emit_matrix_eval_manifests /
        # eval_worker.parse_payload expect. ``suffixes`` is the union
        # across archs because the matrix_eval worker re-applies
        # per-(arch, compiler, opt) sampling with the same seed.
        per_binary_metadata: dict[str, dict] = {}
        for pkg, meta in per_binary_meta_raw.items():
            if not isinstance(meta, dict):
                continue
            archs_list = list(meta.get("archs", ()))
            suffix_union: set[str] = set()
            suffixes_by_arch = meta.get("suffixes_by_arch") or {}
            if isinstance(suffixes_by_arch, dict):
                for sfx_list in suffixes_by_arch.values():
                    if isinstance(sfx_list, list):
                        suffix_union.update(s for s in sfx_list if isinstance(s, str))
            per_binary_metadata[pkg] = {
                "archs": archs_list,
                "suffixes": sorted(suffix_union),
                "variant_sample": meta.get("sample_size"),
                "variant_seed": meta.get("sample_seed"),
                "toolchain_aggregate_drv": tc_aggregate_drv,
            }
        log.info(
            "submit pre-flight: %d toolchains, %d binaries queued for matrix_eval",
            len(tc_drvs), len(per_binary_metadata),
        )

        # Local toolchain availability check. Without ``--build-compilers``
        # secondaries only validate; the primary must have every
        # toolchain output realised before dispatch. With it on, the
        # primary builds any missing toolchain locally as a fallback
        # and the in-cluster ``build_compilers`` worker re-realises
        # them on each secondary that wins a dispatched task.
        build_compilers_on = bool(getattr(args, "build_compilers", False))
        debug_testbuild = getattr(args, "debug_testbuild", None)
        tc_drv_set = frozenset(d for d in tc_drvs.values() if d)
        if tc_drv_set:
            try:
                missing_tcs = check_toolchains_locally(tc_drv_set)
            except Exception:  # noqa: BLE001
                log.exception(
                    "toolchain local-validity check failed"
                )
                missing_tcs = tc_drv_set
            if missing_tcs:
                if not build_compilers_on:
                    log.error(
                        "submit pre-flight: %d/%d toolchains missing locally "
                        "and --build-compilers is off",
                        len(missing_tcs), len(tc_drv_set),
                    )
                    for drv in sorted(missing_tcs):
                        log.error("  %s", drv)
                    return 1
                log.warning(
                    "submit pre-flight: building %d missing toolchains locally",
                    len(missing_tcs),
                )
                try:
                    build_toolchains_locally(missing_tcs)
                except PreflightError as exc:
                    log.error("local toolchain build aborted: %s", exc)
                    return 1
                except Exception:  # noqa: BLE001
                    log.exception("local toolchain build failed")
                    return 1

        # Resolve toolchain outpaths so toolchain_validate manifests
        # carry ``payload.outpath``. Without this the build_worker's
        # validate path fails immediately with "manifest missing
        # 'payload.outpath'".
        dist_eval_drv_outpaths: Optional[dict[str, str]] = None
        if tc_drvs:
            try:
                tc_outpaths = eval_drv_outpaths(
                    [d for d in tc_drvs.values() if d]
                )
                dist_eval_drv_outpaths = dict(tc_outpaths) if tc_outpaths else {}
                log.info(
                    "submit pre-flight: toolchain outpath eval: %d/%d resolved",
                    len(dist_eval_drv_outpaths), len(tc_drvs),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "submit pre-flight: toolchain outpath eval failed;"
                    " validate manifests will be missing payload.outpath"
                )
                dist_eval_drv_outpaths = {}

        # Stages literal: matrix_eval is always emitted; build_compilers
        # is added when --build-compilers is on. ``--debug-testbuild
        # <binary>`` would additionally inject Phase 1.5
        # ``toolchain_validate`` headers between build_compilers and
        # matrix_eval, but emit_all_manifests currently picks
        # build_compilers OR toolchain_validate per the
        # ``allow_toolchain_build`` arg — so the debug-testbuild flow
        # needs a manifest_gen extension before both classes can be
        # emitted simultaneously. The flag is captured here and threaded
        # forward; the validate emission itself is a follow-up.
        stages: list[str] = ["matrix_eval"]
        if build_compilers_on:
            stages = ["build_compilers", "matrix_eval"]
        if debug_testbuild and "build_compilers" not in stages:
            stages.insert(0, "build_compilers")

        try:
            emit_all_manifests(
                target_dir=manifest_dir,
                sys_name=args.sys_name,
                variants=(),
                toolchain_specs=tc_pairs,
                common_deps=(),
                num_workers=num_workers,
                toolchain_drvs=tc_drvs,
                allow_toolchain_build=build_compilers_on,
                per_binary_metadata=per_binary_metadata,
                drv_outpaths=dist_eval_drv_outpaths,
                stages=stages,
            )
        except Exception:  # noqa: BLE001
            log.exception("submit pre-flight: manifest emission failed")
            return 1

        # Expose the toolchain outpaths to the submitter-peer placement
        # block below. Without this, ``partition_drv_outpaths`` stays
        # None and the submitter never populates the gossip file, so
        # secondaries see no peer for ``toolchain_validate`` fetches.
        partition_drv_outpaths = dist_eval_drv_outpaths

        # Build a synthetic PreflightResult so the rest of cmd_submit
        # (SuitTaskConfig construction, submitter placement block,
        # cache.store) operates on a non-None ``pre``. Variants are
        # empty by design — phase 1+ variant headers are spawned at
        # runtime by the primary, not at submit time.
        pre = PreflightResult(
            sys_name=args.sys_name,
            variants=(),
            toolchain_specs=tc_pairs,
            common_dep_drvs=(),
            toolchain_drvs=frozenset(tc_drv_set),
            toolchain_aggregate_drv=tc_aggregate_drv,
        )

    # Build SuitTaskConfig. ``pre`` still carries the toolchain set
    # (for the submitter placement block below + the cache.store
    # roundtrip), but the SuitTaskConfig itself no longer holds
    # ``input_hash`` / ``toolchain_drvs`` / ``variants`` — those moved
    # to runtime-derived state on the SuitTask / dependency_graph
    # planner after the phase-taxonomy refactor.
    config = _config_from_args(
        args,
        run_id=run_id,
        secondary_id="primary",
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

        # Opt-in override for clusters where the framework's gateway-port
        # probe is necessary-but-not-sufficient: brasilianit (LMU CIP)
        # binds the SSH reverse-forward on 0.0.0.0 — so the framework's
        # auto-detect sets ``gateway_ports_enabled=True`` and selects
        # gateway-direct outbound mode — but the kraterNN compute nodes
        # are on a different segment and can't actually reach the gateway
        # port. Setting ``DYNRUNNER_FORCE_REVERSE_CONNECTION=1`` coerces
        # ``gateway_ports_enabled=True → False`` so the framework picks
        # the ProxyJump-into-secondaries path. Remove once the framework
        # grows a reachability probe or a config-level override.
        if os.environ.get("DYNRUNNER_FORCE_REVERSE_CONNECTION") == "1":
            try:
                from dynamic_runner.packaging.gateway import (  # type: ignore[import-not-found]
                    ssh_gateway as _dr_ssh_gateway,
                )
                _gw_cls = _dr_ssh_gateway.SSHGateway
                _orig_setattr = _gw_cls.__setattr__

                def _coerce_gpe(self, name, value, _o=_orig_setattr):  # noqa: ANN001
                    if name == "gateway_ports_enabled" and value is True:
                        value = False
                    return _o(self, name, value)

                _gw_cls.__setattr__ = _coerce_gpe
                log.info(
                    "DYNRUNNER_FORCE_REVERSE_CONNECTION=1: coercing "
                    "gateway_ports_enabled=True→False to force ProxyJump"
                )
            except Exception:  # noqa: BLE001 — opt-in workaround; never fatal
                log.exception(
                    "DYNRUNNER_FORCE_REVERSE_CONNECTION=1 set but "
                    "SSHGateway patch failed; dispatch continues with "
                    "the framework's native decision"
                )

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
                    identity_file=args.ssh_identity_file,
                    config_file=args.ssh_config,
                    log=log,
                )
                # Advertise the primary's toolchain outpaths in the
                # cluster placement gossip so secondaries on the
                # validate-only path find ``submitter`` as a fetch
                # candidate. The primary's harmonia (already SSH-R'd
                # into each compute node's localhost via the framework)
                # serves the NARs; this placement file is what tells
                # the worker WHICH peer to dial.
                placements: list[tuple[str, str, str]] = []
                if tc_drvs and partition_drv_outpaths:
                    for drv in tc_drvs.values():
                        if not drv:
                            continue
                        outpath = partition_drv_outpaths.get(drv)
                        if not outpath:
                            continue
                        placements.append((outpath, drv, "toolchain"))
                if placements:
                    submitter.set_placements(placements)
                # Toolchain drv seed for the submitter-side bootstrap.
                # The submitter's poll loop flushes this via
                # SubmitterPeer.seed_toolchain_drvs exactly once after
                # a non-submitter peer file appears on the gateway —
                # so we don't need a synchronously-known
                # ``first_secondary_url`` here. Bootstrap only applies
                # when --build-compilers is OFF; otherwise the in-
                # cluster build_compilers worker re-realises the
                # closures locally on each secondary.
                tc_drv_set_for_seed = frozenset(
                    d for d in tc_drvs.values() if d
                )
                if tc_drv_set_for_seed:
                    submitter.set_pending_toolchain_seed_drvs(
                        tc_drv_set_for_seed
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
        if config.matrix_eval_out_dir is not None:
            config.matrix_eval_out_dir.mkdir(parents=True, exist_ok=True)
        if "--output" not in forwarded and not any(
            t.startswith("--output=") for t in forwarded
        ):
            forwarded += ["--output", str(config.dataset_dir)]
        sys.argv = [original_argv[0]] + forwarded
        log.debug("forwarded argv to dynamic_runner: %s", sys.argv)
        try:
            task = SuitTask(config)
            # Propagate --enable-ssh-debug to the secondary container
            # via an env var. The framework rebuilds the secondary's
            # argv from a synthesized ``--secondary tcp://... --
            # secondary-id ...`` and does not forward primary CLI
            # flags, so the env-var shim is the only reliable path.
            run_args: list[str] = ["--pids-limit=16384"]
            if getattr(args, "enable_ssh_debug", False):
                run_args += [
                    "-e", "CSR_ENABLE_SSH_DEBUG=1",
                    "-e",
                    f"CSR_SSH_DEBUG_PORT="
                    f"{getattr(args, 'ssh_debug_port', 22222)}",
                ]
            # Opt-in kill-chasing instrumentation. Enabled by setting
            # ``ASM_TRACE_KILLS=1`` in the submitter env; no-op
            # otherwise. Adds SYS_PTRACE capability + propagates the
            # env var into the secondary container so cmd_secondary's
            # gated strace spawn can attach. Used during the
            # 2026-05-12 bilateral-SIGTERM diagnostic; kept as future-
            # use diagnostic since signal-attribution problems in
            # nested-podman are easy to hit again.
            if os.environ.get("ASM_TRACE_KILLS") == "1":
                run_args += ["--cap-add=SYS_PTRACE", "-e", "ASM_TRACE_KILLS=1"]
            # Cap concurrent nix builds inside each secondary's container.
            # Without this the inner nix-daemon will honor every worker's
            # parallel-build request — 14 dynrunner workers × heavy cross-
            # LLVM toolchain builds can sum to >120 GiB peak and OOM the
            # container before any toolchain finishes (observed on LMU
            # Krater 2026-05-13: 4 secondaries killed at 28-29 min with
            # ExitCode 9:0). max-jobs serializes the heavy ones; warm-
            # cached variants flow at no risk after toolchains. dynrunner-
            # owner confirmed this is operator territory; ResourceStealing-
            # Scheduler intentionally doesn't gate at the daemon layer.
            # Tunable via ASM_NIX_MAX_JOBS (default 2).
            # Started at 4, but on the 2026-05-13 LMU Krater run that
            # still hit the same OOM wall ~15 minutes later (jobs
            # SIGKILL'd at 44m instead of 29m). Dropping to 2 per
            # dynrunner-owner's curve estimate.
            # Also injects ``connect-timeout = 1`` and
            # ``download-attempts = 1``: when a secondary dies, its
            # harmonia URL stays in the substituter list (peer-mesh
            # liveness is a framework primitive but consumer-side
            # policy), so every variant fetch tries the dead peer
            # first. Default nix waits 30s × 5 attempts = 150s per
            # narfile per dead peer before falling through to the
            # next substituter. Capping at 1s × 1 attempt = 1s.
            # dynrunner-owner endorsed this approach 2026-05-13
            # 08:31; the long-term fix is content-addressable peer
            # caching with redundancy but that's a major design
            # effort.
            _nix_max_jobs = os.environ.get("ASM_NIX_MAX_JOBS", "2")
            _nix_config = (
                f"max-jobs = {_nix_max_jobs}\n"
                "connect-timeout = 1\n"
                "download-attempts = 1"
            )
            run_args += ["-e", f"NIX_CONFIG={_nix_config}"]
            deployment = TaskDeploymentSpec(
                secondary_module="compiler_suit_runner",
                image_name="asm-dataset-nix-runner",
                extra_port_forwards=extra_pf,
                # Bump podman's default pids-limit (2048 is too tight
                # for compile-heavy workloads — autotools configure
                # scripts and gcc/clang fan out hundreds of transient
                # processes per parallel build, and 14-core SLURM
                # nodes with max-jobs=auto easily run 4000+ PIDs in
                # flight during peak fan-out).
                extra_run_args=tuple(run_args),
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
            # The IncrementalCache schema still wants a ``partition_path``
            # slot; the partition/merge phases are gone, so we just plant
            # a placeholder under the shared-fs root to keep the cache
            # entry shape stable. Same for ``_meta.json`` — manifest_gen
            # no longer writes a meta sidecar.
            partition_path = shared_fs / "_partition_placeholder.json"
            meta_path = manifest_dir / "_meta.json"
            if not partition_path.exists():
                partition_path.write_text("{}")
            if not meta_path.exists():
                meta_path.write_text("{}")

            # Stage a tiny dir with just the preflight descriptor to
            # avoid tarring up multi-PiB sparse manifest files.
            staging = tempfile.mkdtemp(prefix="suit-runner-cache-")
            try:
                staging_path = pathlib.Path(staging)
                _serialize_preflight_for_cache(
                    pre,
                    num_workers,
                    staging_path / "_preflight.json",
                    toolchain_drvs_by_pair=tc_drvs,
                    allow_toolchain_build=getattr(
                        config, "allow_toolchain_build", False
                    ),
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

    # Opt-in kill-chasing instrumentation, paired with the
    # SYS_PTRACE / env-propagation block in cmd_submit. Enabled by
    # setting ``ASM_TRACE_KILLS=1`` (submitter forwards it via
    # ``-e`` on ``podman run``); no-op otherwise. Spawns strace
    # attached to this secondary's PID 1 with ``-f`` so all
    # subprocess workers + nix-daemon + harmonia are followed. The
    # trace filter includes clone/fork/vfork/clone3/execve so the
    # PTRACE_O_TRACECLONE events fire correctly (without them
    # ``-f`` doesn't attach to fresh children). Output lands in
    # ``/app/log-network/secondary-strace.log`` (volume-mounted, so
    # it survives container teardown). The ``start_new_session=True``
    # keeps strace in its own session so it isn't reaped by any
    # pgrp-scoped signal to the secondary; container-wide signals
    # (e.g. user@.service slice teardown) still kill it.
    _strace_proc = None
    if os.environ.get("ASM_TRACE_KILLS") == "1":
        try:
            _strace_log = pathlib.Path("/app/log-network/secondary-strace.log")
            _strace_log.parent.mkdir(parents=True, exist_ok=True)
            _strace_proc = subprocess.Popen(
                [
                    "strace", "-f", "-tt", "-p", str(os.getpid()),
                    "-e", "trace=kill,tgkill,tkill,clone,fork,vfork,clone3,execve",
                    "-o", str(_strace_log),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(0.5)
            log.warning("ASM_TRACE_KILLS=1: strace attached to pid=%d, log=%s", os.getpid(), _strace_log)
        except Exception:  # noqa: BLE001
            log.exception("ASM_TRACE_KILLS=1: strace spawn failed; continuing without trace")

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
            sample_size=getattr(args, "variant_sample", 0) or 0,
            sample_seed=getattr(args, "variant_seed", "42") or "42",
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
            # log-network = per-run scratch (manifests, partition,
            # peers) bind-mounted into every secondary container.
            shared_fs=pathlib.Path("/app/log-network"),
            # out-tmp = the framework's per-task staging root.
            # Workers write tarballs/sidecars here, then call
            # ``task.publish_all`` to atomically deliver them to the
            # gateway-shared output mount (``/app/out-network``). The
            # ``dataset`` subdir keeps our ``.tar.zst`` outputs separate
            # from other consumers (e.g. asm-tokenizer) of the same
            # shared output dir on the gateway.
            dataset_dir=pathlib.Path("/app/out-tmp/dataset"),
            # matrix_eval archives land on the shared bind mount so the
            # primary's watcher can read them. /app/out-network is the
            # container view of <shared_fs>/dataset (host view).
            matrix_eval_out_dir=pathlib.Path("/app/out-network/_matrix_eval"),
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
            # The primary side propagates --enable-ssh-debug via the
            # ``CSR_ENABLE_SSH_DEBUG`` env var (the framework rebuilds
            # this argv synthetically and does not forward our CLI
            # flags). Read it here so the secondary's SuitTask config
            # picks up the operator's intent.
            enable_ssh_debug=os.environ.get(
                "CSR_ENABLE_SSH_DEBUG", "0"
            ) == "1",
            ssh_debug_port=int(
                os.environ.get("CSR_SSH_DEBUG_PORT", "22222")
            ),
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

"""Command-line entry point for ``compiler_suit_runner``.

This module wires :mod:`compiler_suit_runner.preflight`,
:mod:`compiler_suit_runner.manifest_gen`, :mod:`compiler_suit_runner.suit_task`
and :mod:`compiler_suit_runner.incremental_cache` into a small argparse
front-end. Subcommands:

* ``submit`` — primary host's flow: pre-flight (its two enumeration
  steps memoized via the incremental cache), emit manifests, dispatch
  the run via either the in-process single-process loop or the
  dynamic_runner SLURM bridge.
* ``secondary`` — secondary container entry: bring up peer-cache state
  and live until told to stop. SLURM dispatch handles per-item work.
* ``preflight`` — pre-flight only; print the manifest count by class.
* ``clear-cache`` — invalidate the local incremental cache (or one
  specific cache key).

Single-process execution is implemented inline via
:func:`run_single_process` because it is small enough to keep with the
CLI; SLURM execution defers to dynamic_runner's pipeline.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from typing import Optional

from compiler_suit_runner.incremental_cache import (
    DEFAULT_CACHE_ROOT,
    IncrementalCache,
    InputHashInputs,
    ToolchainAxes,
    VariantAxes,
    collect_input_hash_inputs,
    compute_subentry_key,
)
from compiler_suit_runner.manifest_gen import emit_all_manifests
from compiler_suit_runner.nix_drv_show import eval_drv_outpaths
from compiler_suit_runner.preflight import (
    PreflightError,
    build_toolchains_locally,
    check_toolchains_locally,
    enumerate_toolchains_only,
    enumerate_variants,
    export_toolchain_archive,
    preflight as run_preflight,
)
from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig


# Framework CLI composition surface. ``add_framework_arguments`` registers
# every dynamic_runner flag onto our submit/secondary subparsers, and
# ``validate_parsed_args`` runs the framework's cross-flag checks (the
# ``args=`` path of ``run()`` does not call it — the consumer owns
# validation). Imported at module load with a graceful fallback so the
# hermetic test environment (no dynamic_runner) still constructs a parser
# — there the framework flags are simply absent and only the SLURM /
# secondary paths, which require the framework anyway, are affected.
try:  # pragma: no cover - exercised in the dev shell, not the bare env
    from dynamic_runner.cli import (  # type: ignore[import-not-found]
        add_framework_arguments as _add_framework_arguments,
        validate_parsed_args as _validate_parsed_args,
    )
except Exception:  # noqa: BLE001 — framework absent (hermetic tests)
    _add_framework_arguments = None
    _validate_parsed_args = None


__all__ = [
    "build_parser",
    "main",
    "cmd_submit",
    "cmd_secondary",
    "cmd_preflight",
    "cmd_clear_cache",
    "run_single_process",
]


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
    # NOTE: --multi-computer, --jobs, --cores, --max-memory are now
    # registered by add_framework_arguments() on the submit/secondary
    # subparsers (see build_parser). Our custom defaults are restored
    # post-registration via set_defaults() so the framework's choice set
    # (which is a superset of ours) and parsing own these flags.
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
    # --unfulfillable-reinject-max-per-task is framework-owned now
    # (registered by add_framework_arguments). _config_from_args still
    # reads args.unfulfillable_reinject_max_per_task → SuitTaskConfig.
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
    # --packaging, --gateway, --slurm-root-folder, --slurm-partition,
    # --slurm-time-limit, --slurm-cpus-per-task, --ssh-identity-file,
    # --ssh-config are all framework-owned now (registered by
    # add_framework_arguments). cmd_submit / _config_from_args still read
    # args.gateway / args.slurm_root_folder / args.ssh_identity_file /
    # args.ssh_config off the namespace; the framework's str/None shapes
    # are compatible with the str()-wrapping consumers.
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
    # --important-stdio-only is framework-owned now (registered by
    # add_framework_arguments via IMPORTANT_STDIO_ONLY_FLAG). The
    # framework detects it (logging_setup) and drops it from the
    # forwarded secondary argv so grid workers keep full logs.


def _attach_framework_args(subparser: argparse.ArgumentParser) -> None:
    """Register every dynamic_runner flag onto ``subparser``.

    Thin wrapper over the framework's ``add_framework_arguments`` so the
    one ``None`` guard (framework absent in the hermetic test env) lives
    in a single place. When the framework can't be imported the wrapper
    is a no-op — the submit / secondary paths require the framework to
    run anyway, so a parser without its flags only ever surfaces in tests
    that exercise consumer-only flags.
    """
    if _add_framework_arguments is not None:
        _add_framework_arguments(subparser)


def _restore_framework_flag_defaults(
    subparser: argparse.ArgumentParser,
) -> None:
    """Re-apply this consumer's custom defaults for framework-owned flags.

    ``add_framework_arguments`` ships the framework's own defaults; a few
    of them differ from what this consumer historically used. We override
    them post-registration via ``set_defaults`` (per the migration recipe;
    ``set_defaults`` can't alter ``type``/``choices``, only defaults):

    * ``--multi-computer``: framework default is ``None`` (choices are a
      superset of ours); restore ``single-process`` so a bare ``submit``
      runs in-process like before.
    * ``--jobs``: framework default is ``1``; restore ``None`` so
      ``_resolve_jobs`` falls back to ``cpu_count`` when unset.

    ``--cores`` / ``--max-memory`` keep the framework defaults
    (``"0"`` / ``"-2G"``) — the consumer never reads them directly, they
    flow to the framework via the namespace, and the framework's parsers
    require a non-None value. ``--packaging`` likewise keeps the framework
    default (``None``); the consumer never reads it.
    """
    subparser.set_defaults(multi_computer="single-process", jobs=None)


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

    # submit + secondary are the two subcommands that hand off to
    # dynamic_runner.run(args=ns). They register the FULL framework flag
    # set via add_framework_arguments (which also supplies --source /
    # --output via add_selection_arguments, --important-stdio-only,
    # --secondary-id, --cores/--max-memory/--jobs/--gateway/--packaging/
    # --slurm-*/--ssh-config/--ssh-identity-file/--debug/etc.) alongside
    # our consumer-only flags. _restore_framework_flag_defaults re-applies
    # our custom defaults on the few framework flags whose defaults differ.
    p_submit = sub.add_parser("submit", help="Kick off a run from this host.")
    _attach_framework_args(p_submit)
    _add_common_args(p_submit)
    # CSR consumer flag (NOT a framework flag): disables toolchain dedup,
    # i.e. each per-binary matrix archive exports its FULL closure
    # (including the toolchain) instead of the binary-specific diff.
    # Rollback switch — also honoured via env ``CSR_TOOLCHAIN_DEDUP=0``.
    p_submit.add_argument(
        "--no-toolchain-dedup",
        dest="no_toolchain_dedup",
        action="store_true",
        default=False,
        help=(
            "Disable toolchain dedup: per-binary matrix archives export "
            "the full closure (including the compiler toolchain) instead "
            "of the binary-specific diff. Rollback switch; also via env "
            "CSR_TOOLCHAIN_DEDUP=0."
        ),
    )
    _restore_framework_flag_defaults(p_submit)

    p_secondary = sub.add_parser(
        "secondary", help="Run as a per-node secondary worker."
    )
    _attach_framework_args(p_secondary)
    _add_common_args(p_secondary)
    _restore_framework_flag_defaults(p_secondary)

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
            "Specific cache key to invalidate across every sub-entry"
            " namespace (default: clear the entire cache root)."
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


def _resolve_toolchain_dedup(args: argparse.Namespace) -> bool:
    """Resolve the toolchain-dedup feature flag (default ON).

    Precedence: the ``--no-toolchain-dedup`` submit flag wins (when the
    operator explicitly passed it), then the ``CSR_TOOLCHAIN_DEDUP`` env
    override (``0`` / ``false`` / ``no`` / ``off`` disable), then the
    default (ON). When dedup is OFF the per-binary matrix archives export
    the FULL closure (today's behaviour) and the submitter skips the
    toolchain-archive export — rollback is this one flag.
    """
    if getattr(args, "no_toolchain_dedup", False):
        return False
    env = os.environ.get("CSR_TOOLCHAIN_DEDUP")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return True


def _config_from_args(
    args: argparse.Namespace,
    *,
    run_id: str,
    secondary_id: str,
    per_binary_metadata: Optional[dict[str, dict]] = None,
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
        # SLURM-only fields; cli's local-mode / test paths omit them.
        gateway_url=getattr(args, "gateway", None),
        slurm_root_folder=(
            str(args.slurm_root_folder)
            if getattr(args, "slurm_root_folder", None) else None
        ),
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
        # JSON-free phase-2/3 input: discover_items builds the
        # matrix_eval (one per binary) + single dependency_graph
        # TaskInfos directly from this in-memory metadata.
        per_binary_metadata=per_binary_metadata or None,
    )


def _toolchain_axes_from_args(args: argparse.Namespace) -> ToolchainAxes:
    """Build the toolchains sub-entry axes from the parsed namespace.

    Exactly the inputs ``enumerate_toolchains_only`` reads off the
    namespace at its :func:`cmd_submit` call site (``sys_name`` +
    ``archs``); see :class:`ToolchainAxes` for the rationale per field.
    """
    return ToolchainAxes.from_values(
        sys_name=getattr(args, "sys_name", "x86_64-linux"),
        archs=getattr(args, "archs", None),
    )


def _variant_axes_from_args(args: argparse.Namespace) -> VariantAxes:
    """Build the variants sub-entry axes from the parsed namespace.

    Exactly the inputs ``enumerate_variants`` reads off the namespace
    at its :func:`cmd_submit` call site. Values are normalized the same
    way the call site normalizes them (the ``variant_sample`` ``or 0``
    / ``variant_seed`` ``or "42"`` fallbacks), so the key reflects the
    effective invocation, not the raw flag spelling.
    """
    return VariantAxes.from_values(
        sys_name=getattr(args, "sys_name", "x86_64-linux"),
        packages=getattr(args, "packages", None),
        archs=getattr(args, "archs", None),
        variant_sample=getattr(args, "variant_sample", 0) or 0,
        variant_seed=getattr(args, "variant_seed", "42") or "42",
    )


def _collect_repo_inputs_for_cache(
    args: argparse.Namespace, log: logging.Logger
) -> Optional[InputHashInputs]:
    """Collect the repo-state half of the memoization keys, or ``None``.

    ``args.flake`` is normally a local path (``"."`` or a checkout
    path). For a NON-path flake ref (``github:...``, a registry alias)
    ``pathlib.Path(args.flake).resolve()`` still produces *a*
    filesystem path — it just points at a directory that has no
    ``flake.lock`` / ``.git``, so :func:`collect_input_hash_inputs`
    raises and we deliberately degrade to "no cache" (every enumeration
    runs) rather than mis-keying the cache on an unrelated directory.
    This used to be an accidental property of a broad exception guard;
    it is now the documented contract of this helper.
    """
    repo_root = (
        pathlib.Path(args.flake).resolve()
        if args.flake != "."
        else pathlib.Path.cwd()
    )
    try:
        return collect_input_hash_inputs(repo_root)
    except Exception:  # noqa: BLE001
        log.warning(
            "failed to collect repo state for the incremental cache;"
            " continuing without cache",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Single-process execution
# ---------------------------------------------------------------------------


def _validate_framework_args(
    args: argparse.Namespace, log: logging.Logger
) -> None:
    """Run the framework's cross-flag validation on a pre-parsed namespace.

    ``run(task, args=ns)`` deliberately does NOT call
    ``validate_parsed_args`` — that's the consumer's job on the args=
    path (the framework only validates on the argv= path it parses
    itself). We mirror it here so an invalid flag combination surfaces
    with the framework's own usage/error shape (``parser.error`` → exit
    2) rather than failing deep inside dispatch. A fresh framework parser
    supplies the ``.error`` routing target; it parses nothing. No-op when
    the framework is absent (hermetic tests).
    """
    if _validate_parsed_args is None:
        return
    try:
        from dynamic_runner.cli import (  # type: ignore[import-not-found]
            build_arg_parser,
        )
        fw_parser = build_arg_parser("compiler_suit_runner")
        _validate_parsed_args(args, fw_parser)
    except SystemExit:
        # parser.error() exits 2 on a genuinely invalid combination —
        # let it propagate so the operator sees the framework's message.
        raise
    except Exception:  # noqa: BLE001 — never block dispatch on a probe
        log.debug(
            "framework validate_parsed_args probe skipped", exc_info=True
        )


def run_single_process(
    config: SuitTaskConfig,
    *,
    args: Optional[argparse.Namespace] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Drive the pipeline in this process via the framework's runner.

    Constructs a :class:`SuitTask`, hands it to ``dynamic_runner.run``
    via the ``args=`` namespace path (the framework no longer reads
    ``sys.argv``), and lets the framework own setup / teardown via the
    ``on_run_start`` / ``on_run_end`` lifecycle hooks. Falls back to
    the legacy in-process dispatch loop when ``dynamic_runner`` is
    not importable (the consumer's hermetic test environment).

    ``args`` is the namespace cmd_submit already parsed (framework flags
    registered via add_framework_arguments) with ``source`` / ``output``
    injected; there is no ``deployment`` for the in-process path.

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
        # args= path: the framework parses nothing, using our pre-parsed
        # namespace directly. No deployment for the in-process mode.
        dynamic_runner_run(task, args=args)
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


def _produce_and_upload_toolchain_archive(
    args: argparse.Namespace,
    tc_aggregate_drv: str,
    log: logging.Logger,
) -> int:
    """Produce + upload the shared ``toolchains.drv.archive`` to the gateway.

    Toolchain dedup ships the compiler-toolchain closure ONCE per
    dispatch: the SUBMITTER produces it locally then transfers it to
    the gateway ``<slurm_root_folder>/out/_matrix_eval/`` — that ``out/``
    is bind-mounted into every secondary as ``/app/out-network``, so the
    archive lands where the eval workers import it
    (``/app/out-network/_matrix_eval/toolchains.drv.archive``) and the
    build workers consume it. The submitter is NOT a worker (no
    ``/app/out-network`` mount), so it cannot use ``task.publish`` — it
    uses the framework gateway primitive (``create_directory`` +
    ``upload_file``) instead, mirroring ``packaging/job_manager.py``.

    AUTH: ``parse_gateway_url`` parses only the URL (it returns a
    ``GatewayConfig`` with ``ssh_identity_file``/``ssh_config_file``
    left ``None``); the consumer's ``--ssh-identity-file`` /
    ``--ssh-config`` are threaded onto the config AFTER parsing, the
    same way the framework's own dispatch threads them — so this upload
    authenticates identically (e.g. LMU 1Password / IdentityAgent
    bypass).

    Returns 0 on success, 1 on any produce/upload failure (a missing
    toolchain archive strands every per-binary diff archive downstream,
    so the caller must abort the dispatch).
    """
    from dynamic_runner.packaging.gateway import (  # noqa: PLC0415
        create_gateway,
        parse_gateway_url,
    )

    from compiler_suit_runner import preflight  # noqa: PLC0415

    # 1. Produce the archive locally (submitter-side tmp dir). The
    #    toolchain aggregate closure is already realised on this host
    #    (the preflight built it), so the export is a cheap requisites +
    #    nix-store --export.
    local_dir = pathlib.Path(tempfile.mkdtemp(prefix="csr-toolchain-archive-"))
    try:
        local_archive = preflight.export_toolchain_archive(
            tc_aggregate_drv, local_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "toolchain-dedup: failed to export toolchains.drv.archive "
            "from %r: %s",
            tc_aggregate_drv, exc,
        )
        return 1

    # 2. Transfer to the gateway. parse_gateway_url leaves auth fields
    #    None; thread the consumer's identity/config onto the config so
    #    the upload authenticates the same way the rest of the dispatch
    #    does.
    cfg = parse_gateway_url(args.gateway)
    cfg.ssh_identity_file = getattr(args, "ssh_identity_file", None)
    cfg.ssh_config_file = getattr(args, "ssh_config", None)
    remote_dir = f"{args.slurm_root_folder}/out/_matrix_eval"
    remote_archive = f"{remote_dir}/{preflight.TOOLCHAIN_ARCHIVE_NAME}"
    gw = create_gateway(cfg)
    try:
        gw.connect()
        gw.create_directory(remote_dir)
        gw.upload_file(str(local_archive), remote_archive)
        log.info(
            "toolchain-dedup: uploaded %s -> %s",
            local_archive.name, remote_archive,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "toolchain-dedup: failed to upload toolchains.drv.archive to "
            "%s: %s",
            remote_archive, exc,
        )
        return 1
    finally:
        try:
            gw.disconnect()
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    """Primary host: enumeration (memoized) -> manifests -> dispatch.

    The incremental cache memoizes ONLY the two pure enumeration
    steps, each under its own key (repo state + the axis subset that
    function actually reads):

    * ``toolchains/<key>`` — :func:`enumerate_toolchains_only` (the
      expensive ~5-min nix-eval-jobs pass + the aggregate instantiate),
      keyed on ``(repo state, sys_name, archs)``.
    * ``variants/<key>`` — :func:`enumerate_variants`, keyed on
      ``(repo state, sys_name, packages, archs, variant_sample,
      variant_seed)``.

    EVERYTHING downstream of the enumerations — the per-binary
    metadata flatten, the empty-aggregate fail-fast, the local
    toolchain availability check, outpath resolution, stage selection,
    manifest emission, SuitTaskConfig construction and the
    toolchain-archive upload gate — runs unconditionally on every
    invocation. A cache hit therefore reaches dispatch with exactly
    the same state a miss builds: there is only one state-building
    path.
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
    # Toolchain-dedup feature flag (rollback): when OFF the per-binary
    # matrix archives export the FULL closure (today's behaviour) and the
    # submitter skips the toolchain-archive export. Resolved from the
    # ``--no-toolchain-dedup`` submit flag plus the ``CSR_TOOLCHAIN_DEDUP``
    # env override (``0`` disables); the resolved bool rides each
    # matrix_eval task payload via ``per_binary_metadata``.
    toolchain_dedup = _resolve_toolchain_dedup(args)

    cache = IncrementalCache(args.cache_root)
    # Repo-state half of the memoization keys; ``None`` (collection
    # failed, or --no-cache) disables both lookups AND stores, so every
    # enumeration runs.
    repo_inputs = (
        None if args.no_cache else _collect_repo_inputs_for_cache(args, log)
    )

    # ------------------------------------------------------------------
    # Toolchain enumeration (memoized — the ~5-min nix-eval-jobs pass).
    # The slow per-binary drv-instantiation is deferred to matrix_eval
    # workers on secondaries (see ``workers/eval_worker.py``); the
    # dependency_graph phase runs as a framework PhaseSpec and the build
    # phase is spawned at runtime by the primary from
    # ``SuitTask.on_phase_end("dependency_graph")``.
    # ------------------------------------------------------------------
    tc_key = ""
    if repo_inputs is not None:
        tc_key = compute_subentry_key(
            repo_inputs, _toolchain_axes_from_args(args)
        )
    tc_cached = cache.lookup_toolchains(tc_key) if tc_key else None
    if tc_cached is not None:
        tc_pairs, tc_drvs, tc_aggregate_drv = tc_cached
        log.info(
            "toolchain enumeration: cache hit (%d pairs, %d drvs)",
            len(tc_pairs), len(tc_drvs),
        )
    else:
        log.info("enumerating toolchains (cache miss)")
        try:
            tc_pairs, tc_drvs, tc_aggregate_drv = enumerate_toolchains_only(
                args.flake, args.sys_name, archs=args.archs,
            )
        except Exception:  # noqa: BLE001
            log.exception("toolchain enumeration failed")
            return 1
        if tc_key:
            try:
                cache.store_toolchains(
                    tc_key, tc_pairs, tc_drvs, tc_aggregate_drv,
                )
            except Exception:  # noqa: BLE001
                log.warning("toolchains cache store failed", exc_info=True)

    # ------------------------------------------------------------------
    # Variant enumeration (memoized — cheap, 1-2 nix evals).
    # ------------------------------------------------------------------
    var_key = ""
    if repo_inputs is not None:
        var_key = compute_subentry_key(
            repo_inputs, _variant_axes_from_args(args)
        )
    per_binary_meta_raw = cache.lookup_variants(var_key) if var_key else None
    if per_binary_meta_raw is not None:
        log.info(
            "variant enumeration: cache hit (%d binaries)",
            len(per_binary_meta_raw),
        )
    else:
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
        if var_key:
            try:
                cache.store_variants(var_key, per_binary_meta_raw)
            except Exception:  # noqa: BLE001
                log.warning("variants cache store failed", exc_info=True)

    # ------------------------------------------------------------------
    # Unified state-building: everything below runs on EVERY invocation
    # (cache hit or miss) — hit-state == miss-state by construction.
    # ------------------------------------------------------------------
    # Fail fast when toolchain enumeration produced no aggregate but
    # binaries are queued for matrix_eval: emit_matrix_eval_manifests
    # would otherwise raise ``ValueError("toolchain_aggregate_drv
    # must be a non-empty string")`` deep inside the manifest-emit
    # step, masking the real preflight failure.
    if per_binary_meta_raw and not tc_aggregate_drv:
        log.error(
            "submit pre-flight: toolchain aggregate is empty (no toolchain "
            "leaves resolved) but %d binaries queued for matrix_eval — "
            "refusing to emit manifests that will fail downstream",
            len(per_binary_meta_raw),
        )
        return 1

    # Flatten the {pkg: {"archs": [...], "sample_size": ...,
    # "sample_seed": ..., "tier": ...}} shape returned by
    # enumerate_variants into the {binary: {"archs": [...],
    # "variant_sample": ..., "variant_seed": ..., "tier": ...,
    # "toolchain_aggregate_drv": ...}} shape that
    # emit_matrix_eval_manifests / eval_worker.parse_payload expect.
    # Per-arch suffix selection now happens inside the matrix_eval
    # worker (eval_worker._sample_per_arch) with the same seed.
    per_binary_metadata = {}
    for pkg, meta in per_binary_meta_raw.items():
        if not isinstance(meta, dict):
            continue
        archs_list = list(meta.get("archs", ()))
        if not archs_list:
            continue
        per_binary_metadata[pkg] = {
            "archs": archs_list,
            "variant_sample": meta.get("sample_size"),
            "variant_seed": meta.get("sample_seed"),
            "tier": meta.get("tier"),
            "toolchain_aggregate_drv": tc_aggregate_drv,
            # Toolchain-dedup feature flag carried per-binary so each
            # matrix_eval worker knows whether to subtract the
            # toolchain closure from its exported diff archive. OFF =
            # full closure (today's behaviour); the consumers still
            # import the toolchain archive first (harmless no-op).
            "toolchain_dedup": toolchain_dedup,
            # ``matrix_eval_out_dir`` is deliberately NOT carried
            # in per_binary_metadata or in the dep_graph payload:
            # the submitter-host path is invalid inside the
            # secondary container. The dep_graph worker reads its
            # archive root from the BuildWorkerEnv (synthesised
            # from ``--matrix-eval-out-dir`` at the container's
            # call site, cli.py ``container_namespace``), so the
            # container view is the single source of truth.
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

    # Stages literal: matrix_eval (phase 2) + dependency_graph
    # (phase 3) are JSON-free and built in-memory by
    # ``SuitTask.discover_items`` from ``per_binary_metadata`` — they
    # are NOT emitted here. The only JSON-backed stage at submit
    # time is build_compilers (gated by --build-compilers or
    # --debug-testbuild); the build stage's tasks are spawned at
    # runtime by the primary via ``primary.spawn_tasks``.
    stages: list[str] = []
    if build_compilers_on:
        stages = ["build_compilers"]
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

    # Build SuitTaskConfig. The SuitTaskConfig no longer holds
    # ``input_hash`` / ``toolchain_drvs`` / ``variants`` — those moved
    # to runtime-derived state on the SuitTask / dependency_graph
    # planner after the phase-taxonomy refactor.
    config = _config_from_args(
        args,
        run_id=run_id,
        secondary_id="primary",
        per_binary_metadata=per_binary_metadata,
    )

    # Inject --source / --output onto the parsed namespace before handing
    # it to the framework's run(args=ns). SuitTask has no real source tree
    # (items come from the preflight-emitted manifests), so --source points
    # at the run's shared FS root (which we created and the framework's
    # process_selection_arguments validates exists). --output is the real
    # destination for finished binary tars: SuitTask threads
    # config.dataset_dir into every build worker via --dataset-output-dir,
    # so mirror that onto args.output for the framework's stats / lifecycle
    # surface. Same injection the old cmd_submit did via forwarded argv,
    # now on the namespace. We do NOT set resolved_output_root (the
    # framework derives it from --output) and do NOT inject --src-network.
    config.dataset_dir.mkdir(parents=True, exist_ok=True)
    if config.matrix_eval_out_dir is not None:
        config.matrix_eval_out_dir.mkdir(parents=True, exist_ok=True)
        # Toolchain-dedup ``toolchains.drv.archive`` is NOT produced
        # here directly: this local matrix_eval_out_dir is a different
        # physical location than the container/gateway mount the SLURM
        # workers read (they resolve matrix_eval_out_dir to
        # /app/out-network/_matrix_eval). The SUBMITTER produces it
        # locally and TRANSFERS it to the gateway out/_matrix_eval (which
        # bind-mounts into each secondary's matrix_eval_out_dir) via the
        # gateway primitive — see ``_produce_and_upload_toolchain_archive``
        # called on the SLURM-dispatch path below. The eval workers then
        # CONSUME (import) it before computing their per-binary diff
        # exports (``eval_worker._import_toolchain_archive``).
    args.source = str(shared_fs)
    args.output = str(config.dataset_dir)
    # The args= path of run() skips validate_parsed_args (the consumer
    # owns validation); mirror the framework's cross-flag checks here.
    _validate_framework_args(args, log)

    rc = 0
    if args.multi_computer == "single-process":
        rc = run_single_process(config, args=args, logger=log)
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

        # Toolchain-dedup: produce + upload the shared
        # ``toolchains.drv.archive`` to the gateway BEFORE dispatch so the
        # eval workers can import it (their per-binary diff exports
        # subtract its closure). Only on the gateway path — a local /
        # single-process dispatch has no gateway mount and the eval
        # worker's dedup import is a no-op there. A missing archive
        # strands every diff archive, so abort the run on failure.
        if (
            toolchain_dedup
            and tc_aggregate_drv
            and args.gateway
            and args.slurm_root_folder
        ):
            rc = _produce_and_upload_toolchain_archive(
                args, tc_aggregate_drv, log,
            )
            if rc != 0:
                return rc

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

        # --source / --output were injected onto ``args`` before the
        # multi-computer branch (the args= path of run() reads them off
        # the namespace; the framework no longer re-parses sys.argv).
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
            # connect-timeout / download-attempts are bumped from the
            # original 1/1 (tuned for many-secondary dead-peer-harmonia
            # fast-fail) to 5/3: cache.nixos.org legitimately needs >1s
            # for source-narinfo round-trips on cold flake inputs, and
            # the per-drv broadcast loop has been removed so dead-peer
            # cost is now minimal (workers consume the matrix .drv.archive
            # rather than peer-substituting individual drvs).
            _nix_config = (
                f"max-jobs = {_nix_max_jobs}\n"
                "connect-timeout = 5\n"
                "download-attempts = 3"
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
            dynamic_runner_run(task, deployment=deployment, args=args)
        except Exception:  # noqa: BLE001
            log.exception("SLURM dispatch failed")
            return 1
        finally:
            if submitter is not None:
                try:
                    submitter.stop()
                except Exception:  # noqa: BLE001
                    log.exception("submitter-peer shutdown failed")
    else:
        log.error("unknown --multi-computer mode %r", args.multi_computer)
        return 2

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

    # The framework's run(args=ns) reads framework flags (--secondary
    # URL, --secondary-id, --cores, --full-log-dir, …) straight off the
    # namespace. For the framework-spawned path those were parsed from
    # the REAL spawned argv in main() (so the framework-regenerated flags
    # land in ns — no flag loss); for the operator-invoked `secondary`
    # subcommand they came from build_parser. Mirror the framework's
    # cross-flag validation that the args= path skips.
    _validate_framework_args(args, log)

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
        dynamic_runner_run(task, deployment=deployment, args=args)
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


def _parse_framework_spawned_secondary(
    raw: list[str],
) -> argparse.Namespace:
    """Parse the framework-spawned secondary argv into a namespace.

    The dynamic_runner pipeline invokes our image with
    ``python -m compiler_suit_runner --secondary tcp://… --secondary-id …
    --secondary-quic-port … --cores … --max-memory … --full-log-dir …`` —
    framework flags only, NO subcommand verb. We parse that REAL argv
    through a framework-enabled parser so every framework-regenerated
    flag lands on the namespace (no flag loss when run(args=ns) reads
    them back), then overlay the consumer-side container defaults the
    spawn argv does not carry (shared-fs, dataset/matrix-eval mounts,
    ssh-debug intent forwarded via env vars).
    """
    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner [secondary]",
        add_help=False,
    )
    _attach_framework_args(parser)
    _add_common_args(parser)
    _restore_framework_flag_defaults(parser)
    # parse_known_args: the spawn argv may carry framework flags newer
    # than the pinned cli surface; ignore unknowns rather than crash the
    # secondary at parse time (the framework's own run() will use the
    # values it knows about off the namespace).
    ns, _unknown = parser.parse_known_args(raw)

    ns.command = "secondary"
    ns.flake = "."
    # log-network = per-run scratch (manifests, partition, peers)
    # bind-mounted into every secondary container.
    ns.shared_fs = pathlib.Path("/app/log-network")
    # out-tmp = the framework's per-task staging root. Workers write
    # tarballs/sidecars here, then task.publish_all atomically delivers
    # them to the gateway-shared output mount (/app/out-network). The
    # ``dataset`` subdir keeps our .tar.zst outputs separate from other
    # consumers (e.g. asm-tokenizer) of the same shared output dir.
    ns.dataset_dir = pathlib.Path("/app/out-tmp/dataset")
    # matrix_eval archives land on the shared bind mount so the primary's
    # watcher can read them. /app/out-network is the container view of
    # <shared_fs>/dataset (host view).
    ns.matrix_eval_out_dir = pathlib.Path("/app/out-network/_matrix_eval")
    ns.sys_name = getattr(ns, "sys_name", None) or "x86_64-linux"
    # The primary side propagates --enable-ssh-debug via the
    # CSR_ENABLE_SSH_DEBUG env var (the framework rebuilds this argv
    # synthetically and does not forward our CLI flags). Read it here so
    # the secondary's SuitTask config picks up the operator's intent.
    ns.enable_ssh_debug = os.environ.get("CSR_ENABLE_SSH_DEBUG", "0") == "1"
    ns.ssh_debug_port = int(os.environ.get("CSR_SSH_DEBUG_PORT", "22222"))
    ns.submitter_peer = False
    return ns


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
        ns = _parse_framework_spawned_secondary(raw)
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

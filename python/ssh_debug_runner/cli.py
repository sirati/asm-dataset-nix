"""Submit / secondary CLI for the ssh-debug task.

Two modes:

* ``submit`` — primary host. Asks dynamic_runner to build the
  ssh-debug image, transfer it to the SLURM gateway, submit N podman
  jobs (one per requested container), and run the primary
  coordinator. Sets a 1h SLURM wallclock by default; user can
  override via ``--slurm-time-limit``.

* ``secondary`` — entry the framework invokes inside each container
  (via ``python -m ssh_debug_runner --secondary ...``). Calls
  ``dynamic_runner.run(task, deployment=...)`` so the secondary
  coordinator connects back to the primary, accepts an item, and
  hands off to :mod:`ssh_debug_runner.worker` (which exec's sshd).

Connection: once a job is running, look for the marker line
``SSH-DEBUG-READY host=<X> port=<P> pid=<…>`` in
``<slurm_root_folder>/log/slurm_<jobid>.out`` to find the host /
port; then::

    ssh -i .ssh-debug/id_ed25519 \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -p <P> -J <gateway-user>@<gateway-host> root@<X>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from .task import SshDebugTask


_DEFAULT_TIME_LIMIT = "01:00:00"
_DEFAULT_N_SECONDARIES = 2
_DEPLOYMENT_IMAGE_NAME = "asm-dataset-nix-ssh-debug"
_DEPLOYMENT_NIX_TARGET = ".#sshDebugImage"

# Submitter harmonia: bound on dev-box localhost; reverse-tunneled
# to each compute-node so containers see it as `localhost:<port>`.
# Each tunnel is a separate `ssh -R` keyed off the connection-info
# files the framework writes on the gateway.
_DEFAULT_SUBMITTER_HARMONIA_PORT = 5005


def _setup_logging(debug: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return logging.getLogger("ssh_debug_runner")


def _build_deployment_spec(
    extra_port_forwards: tuple[tuple[int, int], ...] = (),
) -> Any:
    """Construct the TaskDeploymentSpec lazily so import-time failures
    (e.g. running this module in a hermetic test env without
    dynamic_runner installed) surface only when actually dispatching.
    """
    from dynamic_runner import TaskDeploymentSpec  # type: ignore[import-not-found]

    return TaskDeploymentSpec(
        secondary_module="ssh_debug_runner",
        image_name=_DEPLOYMENT_IMAGE_NAME,
        nix_build_target=_DEPLOYMENT_NIX_TARGET,
        extra_port_forwards=extra_port_forwards,
    )


def _inject_default_time_limit(argv: list[str], default: str) -> list[str]:
    """Insert ``--slurm-time-limit <default>`` if the caller hasn't
    already set one. Modifies sys.argv before dynamic_runner.run
    parses it, so the framework's argparse picks up our default.
    """
    if any(
        a == "--slurm-time-limit" or a.startswith("--slurm-time-limit=")
        for a in argv
    ):
        return argv
    return argv + ["--slurm-time-limit", default]


def _inject_jobs(argv: list[str], n: int) -> list[str]:
    """Make sure ``--jobs <n>`` is in argv so the framework allocates
    exactly the number of SLURM secondaries we asked for. If the caller
    set --jobs explicitly we honour it (warns are out of scope here).
    """
    if any(a == "--jobs" or a.startswith("--jobs=") for a in argv):
        return argv
    return argv + ["--jobs", str(n)]


def _strip_my_args(argv: list[str]) -> list[str]:
    """Remove ssh_debug_runner-specific flags so the framework's
    parser doesn't see them. Currently: `submit` / `secondary`
    subcommand verbs and `--containers / -n N`.
    """
    out = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in ("submit", "secondary"):
            continue
        if token in ("--containers", "-n"):
            skip_next = True
            continue
        if token.startswith("--containers=") or token.startswith("-n="):
            continue
        out.append(token)
    return out


def _extract_argv_value(argv: list[str], flag: str) -> str | None:
    """Return the value of ``--flag VALUE`` or ``--flag=VALUE`` in argv."""
    eq_prefix = flag + "="
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(eq_prefix):
            return tok.split("=", 1)[1]
    return None


def cmd_submit(args: argparse.Namespace, log: logging.Logger) -> int:
    """Primary-host entry: build image, transfer, submit jobs.

    If both ``--gateway`` and ``--slurm-root-folder`` are present
    (and ``--no-submitter-peer`` is NOT), spins up
    :class:`SubmitterPeer` alongside the dispatch — a local
    harmonia-cache + per-compute-node SSH-R tunnel + auto-published
    ``peers/submitter.json``. End result: every container's
    ``/etc/nix/peer.conf`` includes the dev-box harmonia at
    ``http://localhost:5005`` and any ``nix-store --realise``
    invocation transparently fetches paths the submitter has built
    locally.
    """
    n = args.containers
    log.info(
        "ssh-debug submit: %d container(s), default time limit %s",
        n, _DEFAULT_TIME_LIMIT,
    )

    try:
        from dynamic_runner import run as dynamic_runner_run  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        log.error("ssh-debug submit needs dynamic_runner: %s", exc)
        return 1

    task = SshDebugTask(n_secondaries=n)

    # Inspect argv BEFORE we mutate it: pick up the framework's
    # --gateway and --slurm-root-folder so SubmitterPeer can plug
    # itself into the same dispatch.
    gateway_url = _extract_argv_value(sys.argv, "--gateway")
    slurm_root = _extract_argv_value(sys.argv, "--slurm-root-folder")
    skip_submitter = "--no-submitter-peer" in sys.argv
    submitter_port = int(
        _extract_argv_value(sys.argv, "--submitter-harmonia-port")
        or _DEFAULT_SUBMITTER_HARMONIA_PORT
    )

    # Strip our own subcommand / flags from sys.argv before the
    # framework re-parses it, then inject our defaults: 1h SLURM
    # wallclock and `--jobs N` matching --containers.
    cleaned = _strip_my_args(sys.argv)
    cleaned = _inject_default_time_limit(cleaned, _DEFAULT_TIME_LIMIT)
    cleaned = _inject_jobs(cleaned, n)
    sys.argv = cleaned

    log.debug("forwarded argv: %s", sys.argv)

    submitter = None
    extra_pf: tuple[tuple[int, int], ...] = ()
    if gateway_url and slurm_root and not skip_submitter:
        try:
            # Canonical SubmitterPeer lives in compiler_suit_runner.peer_cache;
            # both packages ship in the runner image. The framework's
            # TaskDeploymentSpec.extra_port_forwards (afa024e+) handles
            # the SSH-R on its primary's ControlMaster — submit doesn't
            # open a parallel connection.
            from compiler_suit_runner.peer_cache import SubmitterPeer
            submitter = SubmitterPeer(
                gateway_url=gateway_url,
                slurm_root=slurm_root,
                local_port=submitter_port,
                gateway_port=submitter_port,
                log=log,
            )
            submitter.start()
            extra_pf = submitter.deployment_extra_port_forwards
        except Exception:  # noqa: BLE001 — never block the dispatch
            log.exception(
                "submitter-peer startup failed; dispatch continues "
                "without dev-box harmonia"
            )
            submitter = None
            extra_pf = ()

    deployment = _build_deployment_spec(extra_port_forwards=extra_pf)

    try:
        try:
            dynamic_runner_run(task, deployment=deployment)
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 0
        except Exception:  # noqa: BLE001
            log.exception("ssh-debug submit failed")
            return 1
        return 0
    finally:
        if submitter is not None:
            try:
                submitter.stop()
            except Exception:  # noqa: BLE001
                log.exception("submitter-peer shutdown failed")


def _extract_secondary_id(argv: list[str]) -> str:
    """Pull ``--secondary-id <X>`` (or ``--secondary-id=<X>``) out of
    argv. Falls back to the container's hostname if the framework
    didn't pass one (shouldn't happen in practice).
    """
    import socket as _s
    for i, tok in enumerate(argv):
        if tok == "--secondary-id" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--secondary-id="):
            return tok.split("=", 1)[1]
    try:
        return _s.gethostname()
    except OSError:
        return "unknown"


def cmd_secondary(args: argparse.Namespace, log: logging.Logger) -> int:
    """Secondary container entry. The framework spawns this with
    ``--secondary <primary-url> --secondary-id <id> --secondary-quic-port <p>``
    appended to the argv (via the image's Entrypoint+Cmd contract).

    BEFORE handing off to dynamic_runner, we run :mod:`bootstrap` to
    auto-start nix-daemon, harmonia, and the peer-list watcher. By
    the time the framework spawns the per-task worker (which exec's
    sshd), the container is already a fully participating peer in
    the binary cache federation.
    """
    n = args.containers
    secondary_id = _extract_secondary_id(sys.argv)
    log.info(
        "ssh-debug secondary boot, n_secondaries=%d, secondary_id=%s",
        n, secondary_id,
    )

    try:
        from .bootstrap import bootstrap
        bootstrap(secondary_id)
    except Exception:  # noqa: BLE001 — never block the secondary on
        # bootstrap failure; sshd / framework still come up so the
        # operator can debug.
        log.exception("bootstrap failed (continuing anyway)")

    try:
        from dynamic_runner import run as dynamic_runner_run  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        log.error("ssh-debug secondary needs dynamic_runner: %s", exc)
        return 1

    task = SshDebugTask(n_secondaries=n)
    deployment = _build_deployment_spec()
    try:
        dynamic_runner_run(task, deployment=deployment)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
    except Exception:  # noqa: BLE001
        log.exception("ssh-debug secondary failed")
        return 1
    return 0


def cmd_serve(args: argparse.Namespace, log: logging.Logger) -> int:
    """Standalone container entry: bootstrap + sshd, no framework.

    Useful for ``podman run image:tag serve`` smoke tests of the image
    outside the SLURM dispatch path. Without the shared FS at
    ``/app/log-network`` the bootstrap only starts nix-daemon +
    harmonia (no peer set), then exec sshd in foreground so the
    container stays alive and is ssh-able.
    """
    log.info("ssh-debug serve: bootstrap + sshd (standalone)")
    try:
        from .bootstrap import bootstrap
        bootstrap("standalone")
    except Exception:  # noqa: BLE001
        log.exception("bootstrap failed (continuing anyway)")

    # Use the canonical sshd helpers from compiler_suit_runner.
    try:
        import socket as _socket
        from compiler_suit_runner.ssh_debug import (
            publish_ready_marker, start_sshd_detached,
        )
        sshd_port = int(os.environ.get("SSH_DEBUG_SSHD_PORT", "22222"))
        start_sshd_detached(port=sshd_port, log=log)
        publish_ready_marker(
            _socket.gethostname(), sshd_port,
            output_dir=None, worker_id=0, log=log,
        )
        # Block forever — the user kills the container with podman stop.
        import signal as _sig
        _sig.pause()
    except Exception:  # noqa: BLE001
        log.exception("serve failed")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ssh_debug_runner",
        description=(
            "Spawn N podman containers (typically via SLURM) running "
            "OpenSSH on a high port, for interactive debugging through "
            "the SLURM gateway."
        ),
    )
    p.add_argument(
        "--containers", "-n",
        type=int, default=_DEFAULT_N_SECONDARIES,
        help=(
            "number of debug containers / SLURM secondaries "
            f"(default {_DEFAULT_N_SECONDARIES})"
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="verbose logging",
    )

    sub = p.add_subparsers(dest="cmd")

    p_submit = sub.add_parser(
        "submit",
        help="primary-host entry: build image, transfer, submit",
    )
    p_submit.set_defaults(func=cmd_submit)

    p_secondary = sub.add_parser(
        "secondary",
        help="(internal) container entry — the framework calls this",
    )
    p_secondary.set_defaults(func=cmd_secondary)

    p_serve = sub.add_parser(
        "serve",
        help="standalone podman entry: start nix-daemon + harmonia + sshd",
    )
    p_serve.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry. If invoked WITHOUT a subcommand but with framework
    flags (``--secondary``), defaults to the secondary path so the
    image's `Cmd` slot can stay simple.
    """
    raw = sys.argv[1:] if argv is None else argv

    # If the framework spawned us as a secondary, it appends
    # `<secondary_module> --secondary <url> --secondary-id <id> ...`
    # which our argparse won't know — short-circuit straight into the
    # secondary handler, letting dynamic_runner consume the framework
    # args itself.
    if "--secondary" in raw:
        log = _setup_logging("--debug" in raw)
        # Synthesize a Namespace with the public knobs we honour;
        # dynamic_runner will reparse the framework args downstream.
        ns = argparse.Namespace(
            containers=_DEFAULT_N_SECONDARIES,
            debug="--debug" in raw,
            func=cmd_secondary,
        )
        return cmd_secondary(ns, log)

    parser = build_parser()
    # Use parse_known_args so the framework's flags (e.g. --gateway,
    # --slurm-root-folder, --multi-computer, --slurm-time-limit) flow
    # through unrecognised — dynamic_runner.run will re-parse sys.argv
    # with its own argparse and handle them.
    args, _unknown = parser.parse_known_args(raw)
    log = _setup_logging(args.debug)

    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args, log)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

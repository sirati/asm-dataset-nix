"""Per-item worker: spawns ``sshd -D`` and waits for it.

Architectural shape (per dynamic_runner peer's directive
2026-05-01): the worker is a plain conformant runner-protocol
worker, not a ``container-spawns-sshd-and-detaches`` shim. The
framework's ``dynamic_runner.worker`` runtime owns the read/run/
respond cycle (Ready handshake, command parsing, exception →
wire mapping, SIGTERM → SystemExit translation); this module
only supplies the per-task body.

Per-task contract:

  1. Resolve the sshd port from ``--log-file`` (worker_id-staggered).
  2. Spawn ``sshd -D`` as a CHILD process (NOT a detached session
     leader), via ``compiler_suit_runner.ssh_debug.spawn_sshd_attached``.
  3. Publish a ready marker on every gateway-readable mount so
     operators can find the listener.
  4. Block on ``sshd_proc.wait()`` until sshd exits *or* the worker
     process catches SIGTERM (cgroup teardown). The runtime's
     SIGTERM handler raises SystemExit, which propagates through
     the wait() call; the handler's ``finally`` block reaps sshd
     before the runtime serialises the recoverable-error response.
  5. Classify the sshd exit code: graceful rcs (0, 130, 143)
     return :class:`WorkerOutput`; everything else raises
     :class:`NonRecoverableError`.

There is no consumer-side keepalive cadence: ``SshDebugTask``
sets ``timeout_seconds=None`` for the sshd type, so the manager
never times the worker out. There is no mid-task ``stop``
handling either: the framework runtime queues the StopCommand
until the current task ends, and an interactive sshd session
ends when the operator disconnects (or SLURM signals SIGTERM).

The framework silences the worker subprocess's stdin/stdout/
stderr (``dynrunner-pyo3/src/subprocess_factory.rs:116``: all
``Stdio::null()``), so debug output goes to two log paths:

  /tmp/ssh-debug/worker.log                                  (container-local)
  /app/log-network/worker-<host>-<pid>.log                    (gateway-NFS)
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import signal
import socket
import sys
import traceback
from pathlib import Path

from compiler_suit_runner.ssh_debug import (
    publish_ready_marker as _publish_ready_marker_canon,
    spawn_sshd_attached as _spawn_sshd_attached_canon,
)
from dynamic_runner.worker import (
    NonRecoverableError,
    Task,
    WorkerOutput,
    run,
)


LOG = logging.getLogger("ssh_debug_runner.worker")

RUNTIME_DIR = "/tmp/ssh-debug"
WORKER_LOG = f"{RUNTIME_DIR}/worker.log"

# Exit codes that count as a graceful end of an interactive sshd
# session. Anything outside this set is reported via
# :class:`NonRecoverableError` so the framework surfaces the failure
# rather than silently logging it as success.
#
#   0   — clean process exit (operator graceful disconnect; sshd's
#         own shutdown path).
#   130 — SIGINT (manual kill from operator); rare in the SLURM
#         cgroup-teardown flow which uses SIGTERM, but treated as
#         graceful for symmetry with 143.
#   143 — SIGTERM (SLURM cgroup teardown at wallclock-limit, or our
#         own stop-handler kill); the expected end of an interactive
#         debug session.
_GRACEFUL_SSHD_RCS: frozenset[int] = frozenset({0, 130, 143})


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")


def _diag(msg: str) -> None:
    """Write a timestamped line to ``worker.log`` and the gateway-
    readable NFS mount. Best-effort; failures are silent.
    """
    line = f"{_ts()} pid={os.getpid()} {msg}\n"
    try:
        Path(RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
        with open(WORKER_LOG, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError:
        pass
    try:
        host = socket.gethostname()
        net_path = f"/app/log-network/worker-{host}-{os.getpid()}.log"
        with open(net_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except OSError:
        pass
    try:
        LOG.info(msg)
    except Exception:  # noqa: BLE001
        pass


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _worker_id_from_log_file(log_file: str | None) -> int:
    if not log_file:
        return 0
    m = re.search(r"worker_(\d+)\.log$", log_file)
    return int(m.group(1)) if m else 0


def _resolve_sshd_port(args_log_file: str | None) -> int:
    """Stagger sshd port by worker_id so co-located workers don't
    collide on a fixed port: ``port = base + worker_id``.
    """
    base = int(os.environ.get("SSH_DEBUG_SSHD_PORT", "22222"))
    return base + _worker_id_from_log_file(args_log_file)


def _terminate_child(sshd_proc) -> None:
    """Send SIGTERM, wait briefly, escalate to SIGKILL on hang. Idempotent."""
    if sshd_proc is None or sshd_proc.poll() is not None:
        return
    try:
        sshd_proc.send_signal(signal.SIGTERM)
    except OSError:
        pass
    try:
        sshd_proc.wait(timeout=5.0)
    except Exception:  # noqa: BLE001 — covers TimeoutExpired across versions
        try:
            sshd_proc.kill()
            sshd_proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass


def is_graceful_sshd_rc(rc: int) -> bool:
    """Return True iff ``rc`` is one of the rcs we treat as a graceful
    sshd exit. Lifted out for unit-testability.
    """
    return rc in _GRACEFUL_SSHD_RCS


def make_handle(
    args: argparse.Namespace,
    *,
    spawn_sshd=_spawn_sshd_attached_canon,
    publish_ready=_publish_ready_marker_canon,
):
    """Build the per-task handler bound to ``args``.

    Factored out so tests can inject fakes for ``spawn_sshd`` and
    ``publish_ready`` while keeping the production wiring trivial.
    """

    def handle(task: Task) -> WorkerOutput | None:  # noqa: ARG001 — sentinel
        worker_id = _worker_id_from_log_file(args.log_file)
        sshd_port = _resolve_sshd_port(args.log_file)
        host = _hostname()
        _diag(
            f"task received; sshd_port={sshd_port} host={host} "
            f"worker_id={worker_id}"
        )

        sshd_proc = spawn_sshd(
            port=sshd_port,
            runtime_dir=RUNTIME_DIR,
            log=LOG,
        )
        if sshd_proc is None:
            _diag(
                "spawn_sshd_attached returned None (port collision or"
                " early exit)"
            )
            raise NonRecoverableError("sshd failed to start")

        try:
            publish_ready(
                host, sshd_port,
                output_dir=args.output,
                worker_id=worker_id,
                log=LOG,
            )
            _diag(f"sshd_pid={sshd_proc.pid}; awaiting exit")
            try:
                rc = sshd_proc.wait()
            except (KeyboardInterrupt, SystemExit):
                # SIGTERM-driven teardown (cgroup/SLURM) lands here:
                # the runtime's signal handler raises SystemExit during
                # the blocking wait(), which propagates up after our
                # ``finally`` reaps sshd. Re-raise so the runtime emits
                # the recoverable-error response on the comm channel.
                _diag("wait() interrupted; tearing down sshd")
                raise
        finally:
            _terminate_child(sshd_proc)

        _diag(f"sshd exited rc={rc}")
        if is_graceful_sshd_rc(rc):
            return WorkerOutput()
        raise NonRecoverableError(f"sshd exited unexpectedly rc={rc}")

    return handle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ssh_debug_runner.worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--skip-existing", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="ssh-debug-worker[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    _diag("=== worker boot ===")
    _diag(f"argv: {sys.argv if argv is None else argv}")
    _diag(f"pid: {os.getpid()}  ppid: {os.getppid()}")
    _diag(f"cwd: {os.getcwd()}  euid: {os.geteuid()}")

    try:
        parser = _build_parser()
        args, _ = parser.parse_known_args(
            sys.argv[1:] if argv is None else argv
        )
    except Exception as exc:  # noqa: BLE001
        _diag(f"argv parse failed: {exc}")
        _diag(traceback.format_exc())
        return 1

    _diag(f"parsed args: {vars(args)}")

    if args.log_file:
        try:
            Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(args.log_file)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logging.getLogger().addHandler(handler)
            _diag(f"framework log-file attached: {args.log_file}")
        except OSError as exc:  # noqa: BLE001
            _diag(f"log-file {args.log_file} unusable: {exc}")

    try:
        run(make_handle(args), args=args)
    except Exception as exc:  # noqa: BLE001
        _diag(f"runtime crashed: {exc}")
        _diag(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

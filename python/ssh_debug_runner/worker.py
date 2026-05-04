"""Per-item worker: completes the runner-protocol Ready handshake,
waits for ONE task assignment, then carries the assigned ``sshd``
instance as its task body.

Architectural shape (per dynamic_runner peer's directive
2026-05-01): the worker is a plain conformant runner-protocol
worker, not a ``container-spawns-sshd-and-detaches`` shim. Concretely:

  1. Connect comm fd / socket path supplied by the framework
     (``--dynamic_queue <fd>`` from socketpair mode, or
     ``--socket-path <path>``).
  2. Send ``ready\\n`` on that channel.
  3. Block-recv one line — that's the framework's
     ``ProcessBinaryCommand`` carrying the assigned item's
     relative path. (For ssh_debug_runner the path is a sentinel:
     :class:`SshDebugTask` populates payload[index] but we don't
     read the file — sshd is the work, not a binary.)
  4. Spawn ``sshd -D`` as a CHILD process (NOT a detached session
     leader). Publish a ready-marker on every gateway-readable
     mount so operators can find the listener.
  5. Loop: select() on (comm fd, sshd child).
       * If a line arrives on comm:
           - ``stop`` → kill sshd, write ``done:rc:0\\n``, exit.
           - any other line → ignore; keepalive cadence still
             refreshes the manager's worker-alive belief, so this
             is just a defensive log entry.
       * If sshd exits → write ``done:rc:N\\n`` (or
         ``error:non_recoverable:...\\n`` if rc != 0 and we want
         the dispatch to fail loudly), exit.
       * If neither fires inside the keepalive interval → write
         ``keepalive\\n`` so the manager doesn't declare us dead.

This swap (detached sshd → child sshd) means ssh sessions die
when the SLURM container is torn down — but the previous
"pretend the task is forever in-progress" trick was a kludge
that confused the manager about worker liveness, and the peer
explicitly asked us to stop using it.

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
import select
import signal
import socket
import sys
import time
import traceback
from pathlib import Path

from compiler_suit_runner.ssh_debug import (
    publish_ready_marker as _publish_ready_marker_canon,
    spawn_sshd_attached as _spawn_sshd_attached_canon,
)


LOG = logging.getLogger("ssh_debug_runner.worker")

RUNTIME_DIR = "/tmp/ssh-debug"
WORKER_LOG = f"{RUNTIME_DIR}/worker.log"

# Keepalive cadence on the comm channel. The peer's
# ``KeepaliveResponse().serialize() == b"keepalive\\n"`` is sent on
# the same fd the worker reads commands from. Cadence chosen
# conservatively: 30 s send keeps us well inside the manager's
# default worker-keepalive timeout (typically 60–90 s).
_KEEPALIVE_INTERVAL_SEC = 30.0

# Exit codes that count as a graceful-end of an interactive sshd
# session. Anything outside this set is reported via
# ``error:non_recoverable:...`` so the framework surfaces the failure
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


def classify_sshd_exit(rc: int) -> bytes:
    """Map an sshd exit code to the runner-protocol response line.

    Lifted out of :func:`_drive_task_loop` for unit-testability —
    the function is pure (rc → bytes) and has no side effects.
    """
    if rc in _GRACEFUL_SSHD_RCS:
        return b"done\n"
    return f"error:non_recoverable:sshd exited unexpectedly rc={rc}\n".encode(
        "utf-8"
    )


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


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--source", type=str)
    parser.add_argument("--output", type=str)
    parser.add_argument("--log-file", type=str)
    parser.add_argument("--skip-existing", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _connect_comm(args: argparse.Namespace) -> socket.socket | None:
    if args.dynamic_queue is not None:
        return socket.socket(fileno=args.dynamic_queue)
    if args.socket_path is not None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 10
        while not os.path.exists(args.socket_path):
            if time.time() > deadline:
                raise TimeoutError(
                    f"Socket {args.socket_path} did not appear within 10s"
                )
            time.sleep(0.05)
        s.connect(args.socket_path)
        return s
    return None


def _send(sock: socket.socket, payload: bytes) -> None:
    try:
        sock.sendall(payload)
    except OSError as exc:  # noqa: BLE001
        _diag(f"comm: send {payload!r} failed: {exc}")


def _send_ready(sock: socket.socket) -> None:
    _send(sock, b"ready\n")
    _diag("comm: sent ready")


def _await_first_item(sock: socket.socket) -> bytes | None:
    """Block on the first newline-terminated command from the manager
    — that's our task assignment. Returns the payload bytes (without
    trailing ``\\n``) or ``None`` on peer-close.
    """
    buf = b""
    sock.settimeout(60.0)
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError as exc:  # noqa: BLE001
            _diag(f"comm: recv failed waiting for first item: {exc}")
            return None
        if not chunk:
            _diag("comm: peer closed before first item")
            return None
        buf += chunk
    line, _rest = buf.split(b"\n", 1)
    _diag(f"comm: received first item ({len(line)} bytes): {line[:120]!r}")
    return line


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


def _drive_task_loop(
    sock: socket.socket,
    sshd_proc,
    *,
    keepalive_interval: float = _KEEPALIVE_INTERVAL_SEC,
) -> tuple[str, int]:
    """Hold the comm channel open while sshd runs.

    Returns ``(reason, rc)`` where reason is one of:
      ``"sshd_exit"``  — sshd exited; rc is its returncode.
      ``"stop"``       — manager sent ``stop``; rc is 0 after we
                         reaped sshd cleanly (or its returncode if
                         it had already exited).

    Implementation: ``select()`` on the comm fd with a timeout equal
    to the keepalive cadence. On wakeup, in priority order:
      1. sshd has exited?  → return ``sshd_exit``.
      2. comm fd has data? → read one line; ``stop`` returns
         ``stop`` after killing/reaping sshd, anything else is
         ignored (defensively logged).
      3. neither?          → emit ``keepalive\\n``, loop again.
    """
    sock.settimeout(None)
    sock.setblocking(False)
    buf = b""
    last_keepalive = time.monotonic()

    while True:
        if sshd_proc.poll() is not None:
            rc = sshd_proc.returncode
            _diag(f"sshd exited rc={rc}; acking task")
            _send(sock, classify_sshd_exit(rc))
            return ("sshd_exit", rc)

        timeout = max(
            0.0,
            keepalive_interval - (time.monotonic() - last_keepalive),
        )
        try:
            ready, _, _ = select.select([sock], [], [], timeout)
        except OSError as exc:  # noqa: BLE001
            _diag(f"select() failed: {exc}; treating as peer-gone")
            ready = []

        if ready:
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                chunk = b""
            except OSError as exc:  # noqa: BLE001
                _diag(f"comm: recv failed: {exc}")
                # Peer gone; reap sshd and return.
                _terminate_child(sshd_proc)
                return ("stop", sshd_proc.returncode or 0)
            if not chunk:
                _diag("comm: peer closed; tearing down sshd and exiting")
                _terminate_child(sshd_proc)
                return ("stop", sshd_proc.returncode or 0)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode("utf-8", errors="replace").strip()
                if not cmd:
                    continue
                if cmd == "stop":
                    _diag("comm: stop received; terminating sshd")
                    _terminate_child(sshd_proc)
                    rc = sshd_proc.returncode or 0
                    _send(sock, b"done\n")
                    return ("stop", rc)
                _diag(f"comm: ignoring non-stop line {cmd[:80]!r}")
        else:
            # Timed out — emit keepalive on the same channel.
            _send(sock, b"keepalive\n")
            last_keepalive = time.monotonic()


def _terminate_child(sshd_proc) -> None:
    """Send SIGTERM, wait briefly, escalate to SIGKILL on hang. Idempotent."""
    if sshd_proc.poll() is not None:
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="ssh-debug-worker[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    _diag("=== worker boot ===")
    _diag(f"argv: {sys.argv}")
    _diag(f"pid: {os.getpid()}  ppid: {os.getppid()}")
    _diag(f"cwd: {os.getcwd()}  euid: {os.geteuid()}")

    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
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

    worker_id = _worker_id_from_log_file(args.log_file)
    sshd_port = _resolve_sshd_port(args.log_file)
    host = _hostname()
    _diag(
        f"target sshd port: {sshd_port}  hostname: {host}  "
        f"worker_id: {worker_id}"
    )

    try:
        sock = _connect_comm(args)
        if sock is None:
            _diag("no comm channel supplied; cannot speak protocol")
            return 1

        _send_ready(sock)
        first_item = _await_first_item(sock)
        if first_item is None:
            _diag("never received task assignment; aborting")
            return 1

        sshd_proc = _spawn_sshd_attached_canon(
            port=sshd_port,
            runtime_dir=RUNTIME_DIR,
            log=LOG,
        )
        if sshd_proc is None:
            _diag(
                "spawn_sshd_attached returned None (port collision or "
                "early exit) — acking task as non-recoverable error"
            )
            # No sshd → no debug session possible. Surface as
            # non_recoverable so the operator sees the failure
            # explicitly rather than via an absent ready marker.
            _send(
                sock,
                b"error:non_recoverable:sshd failed to start\n",
            )
            return 1

        _publish_ready_marker_canon(
            host, sshd_port,
            output_dir=args.output,
            worker_id=worker_id,
            log=LOG,
        )
        _diag(
            f"sshd_pid={sshd_proc.pid}; entering task loop "
            f"(keepalive every {_KEEPALIVE_INTERVAL_SEC:.0f}s)"
        )
        reason, rc = _drive_task_loop(sock, sshd_proc)
        _diag(f"task loop returned reason={reason} rc={rc}")
        return 0 if reason in ("sshd_exit", "stop") and rc == 0 else 1
    except Exception as exc:  # noqa: BLE001
        _diag(f"main: unhandled exception: {exc}")
        _diag(traceback.format_exc())
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

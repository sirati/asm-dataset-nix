"""Shared runner-protocol driver for compiler_suit_runner workers.

The dynamic_runner framework spawns each worker subprocess with
``--dynamic_queue <fd>`` (socketpair from the secondary's worker
factory) or ``--socket-path <path>`` (out-of-band UNIX socket), then
streams one ``ProcessBinaryCommand`` per line on that fd. Two forms:

    ready\\n                              (worker → manager)
    <relative_path>\\n                    legacy form (manager → worker)
    task:<json>\\n                        FR-3 form: <json> = {"path": ..., "payload": ...}
    done\\n  (or done:N:M\\n)              (worker → manager, success)
    error:non_recoverable:<msg>\\n         (worker → manager, fatal)
    stop\\n                                (manager → worker, drain)

In legacy form the relative path is interpreted against the worker's
*source* directory (staged by the primary into ``/app/src-tmp``).
In FR-3 form the manager has shipped the entire task payload over the
wire — ``payload`` is a JSON-encoded string the worker decodes
directly (``json.loads``), no filesystem hop. ``compiler_suit_runner``
uses FR-3 with ``uses_file_based_items=False`` so workers never read
manifest files.

Reading from ``sys.stdin`` would not work: the framework's
``subprocess_factory`` silences worker stdin/stdout/stderr
(``dynrunner-pyo3/src/subprocess_factory.rs:116``) precisely so the
framework's own logs aren't polluted by worker chatter; only the
explicit comm fd is meant to carry protocol traffic.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import socket
import time
from collections.abc import Callable
from typing import Optional


__all__ = [
    "DispatchFn",
    "DispatchResult",
    "connect_comm",
    "resolve_item_path",
    "run_protocol_loop",
]


# A worker dispatch callback consumes ``(path, payload)`` for one item
# and returns a :class:`DispatchResult`. ``path`` is the manager-
# supplied identifier (already passed through :func:`resolve_item_path`
# for legacy form; verbatim for FR-3 form). ``payload`` is the parsed
# task payload — ``None`` in legacy form, a ``dict`` (or list / string,
# whatever the task declared) in FR-3 form. Workers that don't care
# about payload can ignore it.
class DispatchResult:
    """Outcome of one item's dispatch.

    ``success`` controls the reply line: ``True`` → ``done\\n``,
    ``False`` → ``error:non_recoverable:<message>\\n``. The message is
    ascii-coerced before sending; embedded newlines are stripped so
    the framing stays one-line-per-message.
    """

    __slots__ = ("success", "message")

    def __init__(self, success: bool, message: str = "") -> None:
        self.success = success
        self.message = message

    @classmethod
    def ok(cls) -> "DispatchResult":
        return cls(True)

    @classmethod
    def error(cls, message: str) -> "DispatchResult":
        return cls(False, message)


DispatchFn = Callable[[pathlib.Path, Optional[object]], DispatchResult]


def connect_comm(
    *,
    dynamic_queue: Optional[int],
    socket_path: Optional[str],
    log: Optional[logging.Logger] = None,
) -> Optional[socket.socket]:
    """Open the framework-supplied comm channel.

    Returns ``None`` if neither flag is set — workers can fall back to
    a no-op exit in that case (used by tests that exercise the
    per-item dispatch function directly).
    """
    if dynamic_queue is not None:
        return socket.socket(fileno=dynamic_queue)
    if socket_path is not None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        deadline = time.time() + 10
        while not os.path.exists(socket_path):
            if time.time() > deadline:
                raise TimeoutError(
                    f"Socket {socket_path} did not appear within 10s"
                )
            time.sleep(0.05)
        s.connect(socket_path)
        if log is not None:
            log.info("connected to comm socket %s", socket_path)
        return s
    return None


def resolve_item_path(
    relative_path: str,
    *,
    source: Optional[str],
) -> pathlib.Path:
    """Map a manager-supplied relative path to an on-disk path.

    The framework's primary stages files into ``--src-network`` and
    each secondary copies them into ``--src-tmp`` before the worker
    fires. Inside the container that's ``/app/src-tmp`` by default
    (see ``dynamic_runner/cli.py:--src-tmp`` help). Outside containers
    the framework injects ``--source`` pointing at the local source
    dir, so we resolve against that when present and fall back to the
    container default.
    """
    candidate = pathlib.Path(relative_path)
    if candidate.is_absolute():
        # Tolerate primary-side absolute paths (older code paths) by
        # trusting them as-is. The framework normally sends relative
        # paths but we don't want to break in either direction.
        return candidate
    base = pathlib.Path(source) if source else pathlib.Path("/app/src-tmp")
    return base / candidate


def _send(sock: socket.socket, payload: bytes, log: logging.Logger) -> None:
    try:
        sock.sendall(payload)
    except OSError as exc:  # noqa: BLE001
        log.warning("comm send %r failed: %s", payload, exc)


def _read_until_newline(
    sock: socket.socket, buf: bytearray, log: logging.Logger
) -> Optional[bytes]:
    """Block on the comm fd until one full line arrives.

    Returns the line (without ``\\n``) or ``None`` on peer-close. The
    accumulator is stored in ``buf`` so partial trailing data carries
    over to the next call without re-reading the fd.
    """
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError as exc:  # noqa: BLE001
            log.warning("comm recv failed: %s", exc)
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    nl = buf.index(b"\n")
    line = bytes(buf[:nl])
    del buf[: nl + 1]
    return line


def run_protocol_loop(
    *,
    sock: socket.socket,
    source: Optional[str],
    dispatch: DispatchFn,
    log: logging.Logger,
) -> int:
    """Drive the framework's worker protocol against ``sock``.

    Returns a process exit code: ``0`` if the loop exited via a
    framework-initiated ``stop`` or peer-close *and* no item dispatch
    failed; ``1`` if at least one dispatch returned an error result
    (the per-item ``error:non_recoverable`` line is sent to the
    manager regardless, so the framework still requeues / surfaces
    each failure on its own).
    """
    _send(sock, b"ready\n", log)
    log.info("worker protocol: ready")

    buf = bytearray()
    rc = 0
    while True:
        line = _read_until_newline(sock, buf, log)
        if line is None:
            log.info("comm peer closed; exiting protocol loop rc=%d", rc)
            return rc
        cmd = line.decode("utf-8", errors="replace").strip()
        if not cmd:
            continue
        if cmd == "stop":
            log.info("comm: stop; draining and exiting rc=%d", rc)
            try:
                sock.close()
            except OSError:
                pass
            return rc
        # Two wire forms:
        #   "task:<json>"   FR-3 — ship payload over the wire
        #   "<rel-path>"    legacy — worker reads the file
        if cmd.startswith("task:"):
            try:
                envelope = json.loads(cmd[len("task:"):])
            except json.JSONDecodeError as exc:
                log.exception("malformed task envelope; treating as fatal")
                _send(
                    sock,
                    f"error:non_recoverable:bad task envelope: {exc!r}\n".encode("ascii"),
                    log,
                )
                rc = 1
                continue
            raw_path = envelope.get("path", "")
            raw_payload = envelope.get("payload")
            payload_obj: Optional[object] = None
            if isinstance(raw_payload, str) and raw_payload:
                try:
                    payload_obj = json.loads(raw_payload)
                except json.JSONDecodeError:
                    payload_obj = raw_payload  # let the worker decide
            elif raw_payload is not None:
                payload_obj = raw_payload
            manifest_path = pathlib.Path(raw_path) if raw_path else pathlib.Path("<inline>")
        else:
            payload_obj = None
            manifest_path = resolve_item_path(cmd, source=source)
        log.info("dispatching item %s", manifest_path)
        try:
            result = dispatch(manifest_path, payload_obj)
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            log.exception("dispatch raised; treating as non-recoverable")
            result = DispatchResult.error(f"unhandled exception: {exc!r}")
        if result.success:
            _send(sock, b"done\n", log)
        else:
            msg = (result.message or "dispatch failed").replace(
                "\n", " "
            ).replace("\r", " ")
            # Strip non-ascii to keep the wire format clean — the
            # framework's parser only requires ascii bytes.
            ascii_msg = msg.encode("ascii", errors="replace").decode("ascii")
            _send(
                sock,
                f"error:non_recoverable:{ascii_msg}\n".encode("ascii"),
                log,
            )
            rc = 1

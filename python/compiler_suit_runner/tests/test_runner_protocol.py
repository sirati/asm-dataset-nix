"""Unit tests for ``compiler_suit_runner.workers._runner_protocol``.

The protocol driver is the only piece that talks to the framework's
comm fd (the per-item dispatch functions don't), so it carries the
correctness burden for: ready handshake, line framing, success vs.
non-recoverable error wire shape, ``stop`` drain semantics, and
peer-close handling. These are pure socket-level tests using
``socket.socketpair`` so we exercise the real :class:`socket.socket`
read/write path without spinning up a subprocess.
"""

from __future__ import annotations

import logging
import pathlib
import socket
import threading
from typing import Optional

import pytest

from compiler_suit_runner.workers._runner_protocol import (
    DispatchResult,
    resolve_item_path,
    run_protocol_loop,
)


@pytest.fixture
def log() -> logging.Logger:
    # Tests run quietly; the protocol loop's diagnostic logs are
    # exercised but never captured to stdout.
    return logging.getLogger("test_runner_protocol")


# ---------------------------------------------------------------------------
# resolve_item_path
# ---------------------------------------------------------------------------


def test_resolve_relative_with_source() -> None:
    p = resolve_item_path("manifests/foo.json", source="/run/shared")
    assert p == pathlib.Path("/run/shared/manifests/foo.json")


def test_resolve_relative_without_source_uses_container_default() -> None:
    p = resolve_item_path("manifests/foo.json", source=None)
    assert p == pathlib.Path("/app/src-tmp/manifests/foo.json")


def test_resolve_absolute_path_passes_through() -> None:
    # Older code paths (and the framework when --source is set to the
    # primary's local dir) may dispatch with absolute paths; we trust
    # them rather than trying to relativize against ``source``.
    p = resolve_item_path("/tmp/asm/manifests/x.json", source="/run/shared")
    assert p == pathlib.Path("/tmp/asm/manifests/x.json")


# ---------------------------------------------------------------------------
# Helpers for socket-pair driven tests
# ---------------------------------------------------------------------------


def _drive_loop(
    *,
    incoming_lines: list[bytes],
    dispatch_results: list[DispatchResult],
    log: logging.Logger,
    source: Optional[str] = "/run/source",
) -> tuple[bytes, list[pathlib.Path], int]:
    """Run ``run_protocol_loop`` against an in-memory socket pair.

    Returns ``(bytes_seen_by_manager, dispatched_paths, exit_rc)``.

    The "manager" side writes ``incoming_lines`` (each must include
    trailing ``\\n``) and reads everything the worker emits on the
    return path. The dispatch callback consumes one
    :class:`DispatchResult` per call from ``dispatch_results``.
    """
    worker_sock, manager_sock = socket.socketpair()
    dispatched: list[pathlib.Path] = []
    pending = list(dispatch_results)

    def dispatch(path: pathlib.Path) -> DispatchResult:
        dispatched.append(path)
        if pending:
            return pending.pop(0)
        return DispatchResult.ok()

    rc_holder: dict[str, int] = {}

    def runner():
        rc_holder["rc"] = run_protocol_loop(
            sock=worker_sock,
            source=source,
            dispatch=dispatch,
            log=log,
        )

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    # Read the worker's ready frame before sending any commands so
    # tests can assert ordering deterministically.
    ready_frame = b""
    while not ready_frame.endswith(b"\n"):
        ready_frame += manager_sock.recv(64)

    # Drive the items.
    for line in incoming_lines:
        manager_sock.sendall(line)

    # Read responses until the worker closes (stop / peer-close).
    response_buf = b""
    manager_sock.settimeout(2.0)
    try:
        while True:
            chunk = manager_sock.recv(4096)
            if not chunk:
                break
            response_buf += chunk
    except (TimeoutError, OSError):
        pass

    manager_sock.close()
    t.join(timeout=2.0)
    return ready_frame + response_buf, dispatched, rc_holder.get("rc", -1)


# ---------------------------------------------------------------------------
# run_protocol_loop
# ---------------------------------------------------------------------------


def test_loop_sends_ready_first(log) -> None:
    transcript, _, rc = _drive_loop(
        incoming_lines=[b"stop\n"],
        dispatch_results=[],
        log=log,
    )
    assert transcript.startswith(b"ready\n"), transcript
    assert rc == 0


def test_loop_dispatches_one_item_and_acks_done(log) -> None:
    transcript, dispatched, rc = _drive_loop(
        incoming_lines=[b"manifests/x.json\n", b"stop\n"],
        dispatch_results=[DispatchResult.ok()],
        log=log,
    )
    assert dispatched == [pathlib.Path("/run/source/manifests/x.json")]
    # ``ready\n`` then ``done\n`` (no counters); ``stop`` doesn't
    # generate a reply line.
    assert transcript == b"ready\ndone\n"
    assert rc == 0


def test_loop_emits_non_recoverable_on_dispatch_error(log) -> None:
    transcript, _, rc = _drive_loop(
        incoming_lines=[b"manifests/bad.json\n", b"stop\n"],
        dispatch_results=[DispatchResult.error("nix build crashed")],
        log=log,
    )
    assert b"error:non_recoverable:nix build crashed\n" in transcript
    assert rc == 1


def test_loop_strips_embedded_newlines_in_error_message(log) -> None:
    # Multiline error messages would break the per-line framing the
    # framework's parser relies on.
    transcript, _, rc = _drive_loop(
        incoming_lines=[b"x.json\n", b"stop\n"],
        dispatch_results=[
            DispatchResult.error("line one\nline two\rmore")
        ],
        log=log,
    )
    error_lines = [
        line for line in transcript.split(b"\n")
        if line.startswith(b"error:")
    ]
    assert len(error_lines) == 1
    assert b"\n" not in error_lines[0]
    assert b"\r" not in error_lines[0]


def test_loop_handles_peer_close_without_stop(log) -> None:
    # No ``stop`` sent — manager just closes the socket. Loop should
    # exit cleanly with whatever rc the dispatches produced.
    transcript, _, rc = _drive_loop(
        incoming_lines=[b"a.json\n"],
        dispatch_results=[DispatchResult.ok()],
        log=log,
    )
    assert b"done\n" in transcript
    assert rc == 0


def test_loop_processes_multiple_items(log) -> None:
    transcript, dispatched, rc = _drive_loop(
        incoming_lines=[
            b"a.json\n",
            b"b.json\n",
            b"c.json\n",
            b"stop\n",
        ],
        dispatch_results=[
            DispatchResult.ok(),
            DispatchResult.error("b broke"),
            DispatchResult.ok(),
        ],
        log=log,
    )
    assert [p.name for p in dispatched] == ["a.json", "b.json", "c.json"]
    # Three responses interleaved between ready and stop.
    lines = transcript.split(b"\n")
    response_lines = [l for l in lines if l]  # noqa: E741
    assert response_lines[0] == b"ready"
    assert response_lines[1] == b"done"
    assert response_lines[2].startswith(b"error:non_recoverable:")
    assert response_lines[3] == b"done"
    assert rc == 1  # one failed dispatch


def test_loop_treats_unhandled_dispatch_exception_as_non_recoverable(
    log,
) -> None:
    worker_sock, manager_sock = socket.socketpair()

    def dispatch(_path: pathlib.Path) -> DispatchResult:
        raise RuntimeError("oops")

    rc_holder: dict[str, int] = {}

    def runner():
        rc_holder["rc"] = run_protocol_loop(
            sock=worker_sock,
            source=None,
            dispatch=dispatch,
            log=log,
        )

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    # Read ready, then send one item + stop.
    manager_sock.recv(64)  # discard ready
    manager_sock.sendall(b"x.json\n")
    response_buf = b""
    manager_sock.settimeout(2.0)
    while b"\n" not in response_buf:
        response_buf += manager_sock.recv(4096)

    manager_sock.sendall(b"stop\n")
    t.join(timeout=2.0)
    manager_sock.close()

    assert response_buf.startswith(b"error:non_recoverable:"), response_buf
    assert b"oops" in response_buf
    assert rc_holder["rc"] == 1

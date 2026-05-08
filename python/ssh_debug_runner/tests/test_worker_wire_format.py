"""Pure-function tests for ``ssh_debug_runner.worker``'s
sshd-exit classification and the per-task handler.

The full read/run/respond loop is owned by
``dynamic_runner.worker.run`` (tested upstream); the parts that
*could* drift are this module's rc → graceful/non-recoverable
classification and the handler's spawn / wait / cleanup
sequencing. Both are exercised directly here, with stub fakes
in place of ``spawn_sshd_attached`` and ``publish_ready_marker``.
"""

from __future__ import annotations

import argparse

import pytest

from ssh_debug_runner.worker import (
    is_graceful_sshd_rc,
    make_handle,
)
from dynamic_runner.worker import NonRecoverableError, Task, WorkerOutput


# ---------------------------------------------------------------------------
# is_graceful_sshd_rc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [0, 130, 143])
def test_graceful_rcs_classify_as_graceful(rc: int) -> None:
    assert is_graceful_sshd_rc(rc) is True


@pytest.mark.parametrize("rc", [1, 2, 9, 11, 137, 255])
def test_nonzero_rcs_classify_as_non_recoverable(rc: int) -> None:
    assert is_graceful_sshd_rc(rc) is False


# ---------------------------------------------------------------------------
# make_handle / per-task handler
# ---------------------------------------------------------------------------


class _FakeSshdProc:
    """Stand-in for the real ``subprocess.Popen`` returned by
    :func:`spawn_sshd_attached`. Only implements the surface area
    the handler exercises: ``pid`` (debug-log only), ``wait``,
    ``poll``, and the kill / send_signal hooks invoked by
    :func:`_terminate_child`.
    """

    def __init__(self, *, rc: int, raise_on_wait: BaseException | None = None) -> None:
        self.pid = 4242
        self._rc = rc
        self.returncode: int | None = None
        self._raise_on_wait = raise_on_wait
        self.kill_called = False
        self.term_called = False

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        if self._raise_on_wait is not None:
            exc = self._raise_on_wait
            # The signal interrupts Python's wait but does NOT (yet)
            # kill the underlying sshd; leave ``returncode`` unset so
            # ``poll()`` reports the process as still running and
            # ``_terminate_child`` actually sends SIGTERM.
            self._raise_on_wait = None
            raise exc
        self.returncode = self._rc
        return self._rc

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, _signum: int) -> None:
        self.term_called = True
        self.returncode = self._rc

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = self._rc


def _args(log_file: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        log_file=log_file,
        output=None,
        source=None,
        dynamic_queue=None,
        socket_path=None,
        skip_existing=False,
    )


def test_handler_returns_worker_output_on_clean_sshd_exit() -> None:
    fake_proc = _FakeSshdProc(rc=0)
    publish_calls: list[tuple] = []

    handle = make_handle(
        _args(),
        spawn_sshd=lambda **kw: fake_proc,
        publish_ready=lambda *a, **kw: publish_calls.append((a, kw)),
    )

    out = handle(Task(relative_path="/nonexistent/ssh-debug-00", payload={"index": 0}))
    assert isinstance(out, WorkerOutput)
    assert out.warnings == 0 and out.filtered == 0
    # publish_ready_marker should have fired exactly once.
    assert len(publish_calls) == 1


def test_handler_raises_non_recoverable_on_unexpected_rc() -> None:
    fake_proc = _FakeSshdProc(rc=137)
    handle = make_handle(
        _args(),
        spawn_sshd=lambda **kw: fake_proc,
        publish_ready=lambda *a, **kw: None,
    )
    with pytest.raises(NonRecoverableError) as ei:
        handle(Task(relative_path="x", payload=None))
    assert "rc=137" in str(ei.value)


def test_handler_raises_non_recoverable_when_spawn_returns_none() -> None:
    handle = make_handle(
        _args(),
        spawn_sshd=lambda **kw: None,
        publish_ready=lambda *a, **kw: None,
    )
    with pytest.raises(NonRecoverableError) as ei:
        handle(Task(relative_path="x", payload=None))
    assert "sshd failed to start" in str(ei.value)


def test_handler_terminates_sshd_on_systemexit_during_wait() -> None:
    """SIGTERM-driven teardown: the runtime translates SIGTERM into
    ``SystemExit`` raised inside ``wait()``. The handler's ``finally``
    block must reap sshd before the SystemExit propagates.
    """
    fake_proc = _FakeSshdProc(rc=143, raise_on_wait=SystemExit("signal 15"))
    handle = make_handle(
        _args(),
        spawn_sshd=lambda **kw: fake_proc,
        publish_ready=lambda *a, **kw: None,
    )
    with pytest.raises(SystemExit):
        handle(Task(relative_path="x", payload=None))
    # _terminate_child must have been called even though wait() raised.
    assert fake_proc.term_called is True

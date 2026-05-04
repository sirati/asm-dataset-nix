"""Pure-function tests for ``ssh_debug_runner.worker``'s
sshd-exit-to-runner-protocol-line classifier.

The full :func:`_drive_task_loop` needs a real sshd Popen and a
socketpair, which is awkward to unit-test; the rc → bytes mapping
itself is pure and that's the part most likely to drift if the
framework's wire format changes again, so it's worth a focused test.
"""

from __future__ import annotations

import pytest

from ssh_debug_runner.worker import classify_sshd_exit


@pytest.mark.parametrize("rc", [0, 130, 143])
def test_graceful_rcs_map_to_done(rc: int) -> None:
    # ``done\n`` (no counters) is the only success shape we emit —
    # warnings/filtered are always zero for an sshd shepherd.
    assert classify_sshd_exit(rc) == b"done\n"


@pytest.mark.parametrize("rc", [1, 2, 9, 11, 137, 255])
def test_nonzero_rcs_map_to_non_recoverable_error(rc: int) -> None:
    line = classify_sshd_exit(rc)
    assert line.startswith(b"error:non_recoverable:")
    assert line.endswith(b"\n")
    # The exit code is embedded so the operator can grep dispatch
    # logs without hunting through worker.log.
    assert f"rc={rc}".encode("ascii") in line


def test_classifier_output_is_newline_terminated_single_line() -> None:
    # The runner-protocol consumer reads one line at a time; never
    # emit multi-line responses or unterminated bytes.
    for rc in (0, 1, 130, 137, 143):
        line = classify_sshd_exit(rc)
        assert line.count(b"\n") == 1
        assert line.endswith(b"\n")


def test_classifier_no_unicode_in_error_path() -> None:
    # Workers run in containers with C locale; emitting non-ascii
    # would risk encoding mismatches on the framework side. Keep the
    # error message strictly ascii.
    line = classify_sshd_exit(42)
    line.decode("ascii")  # raises UnicodeDecodeError if non-ascii crept in

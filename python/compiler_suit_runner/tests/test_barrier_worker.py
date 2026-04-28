"""Tests for the barrier_worker module.

Real time is never allowed to elapse: ``sleep`` and ``clock`` are
injected so the suite stays fast and deterministic.
"""

from __future__ import annotations

import pathlib

import pytest

from compiler_suit_runner.workers import barrier_worker
from compiler_suit_runner.workers.barrier_worker import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    PHASE_1A_DONE_FLAG,
    PHASE_1B_DONE_FLAG,
    PHASE_2_DONE_FLAG,
    BarrierResult,
    BarrierTimeout,
    barrier_worker as barrier_worker_fn,
    wait_for_flag,
    write_flag,
)


class FakeClock:
    """Deterministic clock that advances when fake-sleep is called."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_flag_path(flags_dir: pathlib.Path, flag_name: str) -> pathlib.Path:
    flags_dir.mkdir(parents=True, exist_ok=True)
    p = flags_dir / flag_name
    p.write_bytes(b"")
    return p


# ---------------------------------------------------------------------------
# wait_for_flag — fast path: flag already exists
# ---------------------------------------------------------------------------


def test_wait_for_flag_returns_immediately_when_flag_exists(tmp_path):
    flags_dir = tmp_path / "flags"
    _make_flag_path(flags_dir, PHASE_1A_DONE_FLAG)

    clock = FakeClock(start=42.0)

    def boom_sleep(_: float) -> None:
        raise AssertionError("sleep should not be called when flag exists")

    result = wait_for_flag(
        flags_dir,
        PHASE_1A_DONE_FLAG,
        sleep=boom_sleep,
        clock=clock,
    )

    assert isinstance(result, BarrierResult)
    assert result.flag_name == PHASE_1A_DONE_FLAG
    assert result.polls == 1
    assert result.waited_seconds == 0.0
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# wait_for_flag — polls until flag appears
# ---------------------------------------------------------------------------


def test_wait_for_flag_polls_until_flag_appears(tmp_path):
    flags_dir = tmp_path / "flags"
    flags_dir.mkdir()
    flag_path = flags_dir / PHASE_1B_DONE_FLAG

    clock = FakeClock()
    poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
    n_ticks_before_flag = 4
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        clock.sleep(seconds)
        # After exactly n_ticks_before_flag sleeps the flag appears.
        if len(sleep_calls) == n_ticks_before_flag:
            flag_path.write_bytes(b"")

    result = wait_for_flag(
        flags_dir,
        PHASE_1B_DONE_FLAG,
        poll_interval_seconds=poll_interval,
        sleep=fake_sleep,
        clock=clock,
    )

    # Initial check (polls=1) found nothing -> n sleeps happened ->
    # subsequent checks each increment polls. After n_ticks_before_flag
    # sleeps the flag exists, and the next existence check returns true.
    assert result.flag_name == PHASE_1B_DONE_FLAG
    assert result.polls == 1 + n_ticks_before_flag
    expected_waited = n_ticks_before_flag * poll_interval
    assert result.waited_seconds == pytest.approx(expected_waited)
    assert sleep_calls == [poll_interval] * n_ticks_before_flag


def test_wait_for_flag_uses_custom_poll_interval(tmp_path):
    flags_dir = tmp_path / "flags"
    flags_dir.mkdir()
    flag_path = flags_dir / PHASE_2_DONE_FLAG

    clock = FakeClock()
    custom_interval = 0.5

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        # Place the flag after 3 polls' worth of sleep (1.5s).
        if clock.now >= 1.5:
            flag_path.write_bytes(b"")

    result = wait_for_flag(
        flags_dir,
        PHASE_2_DONE_FLAG,
        poll_interval_seconds=custom_interval,
        sleep=fake_sleep,
        clock=clock,
    )

    assert result.polls == 1 + 3
    assert result.waited_seconds == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# wait_for_flag — timeout
# ---------------------------------------------------------------------------


def test_wait_for_flag_raises_timeout_when_flag_never_appears(tmp_path):
    flags_dir = tmp_path / "flags"
    flags_dir.mkdir()
    # Flag is never created.

    clock = FakeClock()
    poll_interval = 1.0
    timeout = 5.0

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)

    with pytest.raises(BarrierTimeout) as excinfo:
        wait_for_flag(
            flags_dir,
            PHASE_1A_DONE_FLAG,
            poll_interval_seconds=poll_interval,
            timeout_seconds=timeout,
            sleep=fake_sleep,
            clock=clock,
        )

    err = excinfo.value
    assert err.flag_name == PHASE_1A_DONE_FLAG
    assert err.waited_seconds >= timeout
    assert PHASE_1A_DONE_FLAG in str(err)
    # The exposed waited_seconds should appear in str() too.
    assert f"{err.waited_seconds:.3f}" in str(err)


def test_wait_for_flag_zero_timeout_with_missing_flag_raises_immediately(
    tmp_path,
):
    flags_dir = tmp_path / "flags"
    flags_dir.mkdir()

    clock = FakeClock()

    def boom_sleep(_: float) -> None:
        raise AssertionError(
            "sleep should not be called when timeout exhausted on first iter"
        )

    with pytest.raises(BarrierTimeout):
        wait_for_flag(
            flags_dir,
            PHASE_1A_DONE_FLAG,
            poll_interval_seconds=1.0,
            timeout_seconds=0.0,
            sleep=boom_sleep,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# wait_for_flag — input validation
# ---------------------------------------------------------------------------


def test_wait_for_flag_rejects_unknown_flag_name_before_any_sleep(tmp_path):
    def boom_sleep(_: float) -> None:
        raise AssertionError("sleep must not be called on bad input")

    with pytest.raises(ValueError, match="unknown flag_name"):
        wait_for_flag(
            tmp_path,
            "phase1a-done",  # close, but wrong (hyphen)
            sleep=boom_sleep,
            clock=FakeClock(),
        )


def test_wait_for_flag_rejects_empty_flag_name(tmp_path):
    with pytest.raises(ValueError, match="unknown flag_name"):
        wait_for_flag(tmp_path, "")


def test_wait_for_flag_rejects_nonpositive_poll_interval(tmp_path):
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        wait_for_flag(
            tmp_path,
            PHASE_1A_DONE_FLAG,
            poll_interval_seconds=0.0,
        )


def test_wait_for_flag_rejects_negative_timeout(tmp_path):
    with pytest.raises(ValueError, match="timeout_seconds"):
        wait_for_flag(
            tmp_path,
            PHASE_1A_DONE_FLAG,
            timeout_seconds=-1.0,
        )


# ---------------------------------------------------------------------------
# write_flag
# ---------------------------------------------------------------------------


def test_write_flag_creates_file(tmp_path):
    flags_dir = tmp_path / "flags"
    path = write_flag(flags_dir, PHASE_1A_DONE_FLAG)
    assert path == flags_dir / PHASE_1A_DONE_FLAG
    assert path.exists()
    assert path.is_file()


def test_write_flag_is_idempotent(tmp_path):
    flags_dir = tmp_path / "flags"
    first = write_flag(flags_dir, PHASE_1A_DONE_FLAG)
    # Capture the inode before and after to confirm idempotency.
    inode_before = first.stat().st_ino
    second = write_flag(flags_dir, PHASE_1A_DONE_FLAG)
    inode_after = second.stat().st_ino
    assert first == second
    assert inode_before == inode_after


def test_write_flag_creates_parent_directories(tmp_path):
    nested = tmp_path / "deep" / "nested" / "flags"
    assert not nested.exists()
    path = write_flag(nested, PHASE_2_DONE_FLAG)
    assert path.exists()
    assert nested.is_dir()


def test_write_flag_rejects_unknown_flag_name(tmp_path):
    with pytest.raises(ValueError, match="unknown flag_name"):
        write_flag(tmp_path, "not_a_real_flag")


def test_write_flag_does_not_leave_tmp_sibling(tmp_path):
    flags_dir = tmp_path / "flags"
    write_flag(flags_dir, PHASE_1A_DONE_FLAG)
    leftovers = [
        p for p in flags_dir.iterdir() if p.name.startswith(".")
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# barrier_worker (the dispatch surface)
# ---------------------------------------------------------------------------


def test_barrier_worker_returns_immediately_when_flag_exists(tmp_path):
    flags_dir = tmp_path / "flags"
    _make_flag_path(flags_dir, PHASE_1A_DONE_FLAG)

    result = barrier_worker_fn(PHASE_1A_DONE_FLAG, flags_dir)

    assert isinstance(result, BarrierResult)
    assert result.flag_name == PHASE_1A_DONE_FLAG
    assert result.polls == 1
    assert result.waited_seconds == 0.0


def test_barrier_worker_validates_flag_name(tmp_path):
    with pytest.raises(ValueError, match="unknown flag_name"):
        barrier_worker_fn("garbage", tmp_path)


def test_barrier_worker_module_exports():
    """The module-level public surface matches the contract."""
    assert hasattr(barrier_worker, "wait_for_flag")
    assert hasattr(barrier_worker, "write_flag")
    assert hasattr(barrier_worker, "barrier_worker")
    assert hasattr(barrier_worker, "BarrierResult")
    assert hasattr(barrier_worker, "BarrierTimeout")
    assert hasattr(barrier_worker, "VALID_FLAG_NAMES")
    assert PHASE_1A_DONE_FLAG in barrier_worker.VALID_FLAG_NAMES
    assert PHASE_1B_DONE_FLAG in barrier_worker.VALID_FLAG_NAMES
    assert PHASE_2_DONE_FLAG in barrier_worker.VALID_FLAG_NAMES

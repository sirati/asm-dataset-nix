"""Barrier worker — sentinel item dispatch surface.

The dynamic_batch framework has no item-level dependency support, so
phase ordering (1a -> 1b -> 2 -> 3) is enforced by sentinel items whose
worker simply polls a flag file on the shared filesystem until it
exists. This module is the load-bearing mechanic for that ordering;
see the plan doc (section "Phase 1a barrier") for context.

Stdlib only; no threading.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import time
from collections.abc import Callable
from typing import Final

DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 2.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 24 * 60 * 60  # 24h hard cap

# Names of the flag files (the only valid ones — typo-proofing).
PHASE_1A_DONE_FLAG: Final[str] = "phase1a_done"
PHASE_1B_DONE_FLAG: Final[str] = "phase1b_done"
PHASE_2_DONE_FLAG: Final[str] = "phase2_done"

VALID_FLAG_NAMES: frozenset[str] = frozenset(
    {
        PHASE_1A_DONE_FLAG,
        PHASE_1B_DONE_FLAG,
        PHASE_2_DONE_FLAG,
    }
)


@dataclasses.dataclass
class BarrierResult:
    """Outcome of a successful barrier wait."""

    flag_name: str
    waited_seconds: float
    polls: int


class BarrierTimeout(RuntimeError):
    """Raised if the barrier times out before the flag appears.

    Includes flag_name and waited_seconds in str().
    """

    def __init__(self, flag_name: str, waited_seconds: float) -> None:
        self.flag_name = flag_name
        self.waited_seconds = waited_seconds
        super().__init__(
            f"barrier timed out waiting for flag {flag_name!r} "
            f"after {waited_seconds:.3f}s"
        )


def _validate_flag_name(flag_name: str) -> None:
    if flag_name not in VALID_FLAG_NAMES:
        raise ValueError(
            f"unknown flag_name {flag_name!r}; "
            f"expected one of {sorted(VALID_FLAG_NAMES)}"
        )


def wait_for_flag(
    flags_dir: pathlib.Path,
    flag_name: str,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> BarrierResult:
    """Poll ``flags_dir/<flag_name>`` until the file exists or timeout.

    ``sleep`` defaults to :func:`time.sleep`; ``clock`` defaults to
    :func:`time.monotonic`. Both are injectable for tests so real time
    does not elapse.

    Validates ``flag_name`` against :data:`VALID_FLAG_NAMES` (raises
    :class:`ValueError` otherwise) so typos cannot silently hang.

    Returns :class:`BarrierResult` on success; raises
    :class:`BarrierTimeout` if the flag never appears within
    ``timeout_seconds``.

    If the flag is already present at the first check, returns
    immediately with ``polls=1`` and ``waited_seconds=0`` — no sleep is
    performed.
    """
    _validate_flag_name(flag_name)

    if poll_interval_seconds <= 0:
        raise ValueError(
            f"poll_interval_seconds must be > 0, got {poll_interval_seconds!r}"
        )
    if timeout_seconds < 0:
        raise ValueError(
            f"timeout_seconds must be >= 0, got {timeout_seconds!r}"
        )

    _sleep = sleep if sleep is not None else time.sleep
    _clock = clock if clock is not None else time.monotonic

    flag_path = flags_dir / flag_name
    start = _clock()
    polls = 1
    if flag_path.exists():
        return BarrierResult(
            flag_name=flag_name,
            waited_seconds=0.0,
            polls=polls,
        )

    while True:
        elapsed = _clock() - start
        if elapsed >= timeout_seconds:
            raise BarrierTimeout(flag_name, elapsed)

        _sleep(poll_interval_seconds)
        polls += 1

        if flag_path.exists():
            waited = _clock() - start
            return BarrierResult(
                flag_name=flag_name,
                waited_seconds=waited,
                polls=polls,
            )


def write_flag(flags_dir: pathlib.Path, flag_name: str) -> pathlib.Path:
    """Atomically create ``flags_dir/<flag_name>``.

    Idempotent: re-creating an existing flag is a no-op. ``flags_dir``
    is created with ``parents=True`` if missing. Validates
    ``flag_name``. Returns the absolute path to the flag.

    The atomic write writes empty contents to a ``.tmp`` sibling and
    renames into place; this guarantees other secondaries never observe
    a partially-written flag.
    """
    _validate_flag_name(flag_name)

    flags_dir.mkdir(parents=True, exist_ok=True)
    flag_path = flags_dir / flag_name

    if flag_path.exists():
        return flag_path

    # Write to a unique tmp sibling, then rename into place. Using
    # os.getpid() in the tmp name keeps concurrent writers from
    # clobbering each other's tmp files; the final rename is atomic on
    # POSIX.
    tmp_path = flags_dir / f".{flag_name}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, flag_path)
    finally:
        # In case the rename never happened (exception path), clean up.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return flag_path


def barrier_worker(
    flag_name: str,
    flags_dir: pathlib.Path,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> BarrierResult:
    """Dispatch surface used by ``suit_task`` for sentinel items.

    Thin wrapper around :func:`wait_for_flag` — kept separate so tests
    can also call :func:`wait_for_flag` directly with injected
    ``sleep`` / ``clock``.
    """
    return wait_for_flag(
        flags_dir,
        flag_name,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )

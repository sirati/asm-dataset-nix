"""Size-encoding and memory-budget helpers for the compiler-suit runner.

The dynamic_runner framework re-sorts pending items by their integer ``size``
field in descending order before dispatch, and exposes only that single
integer to ``TaskDefinition.estimate_memory``. We therefore pack two pieces
of scheduling metadata into one ``size`` value:

    size = (phase_rank << 48) | (memory_bytes & ((1 << 48) - 1))

The high bits hold a small ``phase_rank`` that imposes a coarse phase
ordering (larger rank = earlier phase, because the framework sorts DESC).
The low 48 bits hold a per-item memory budget in bytes, which the scheduler
extracts via ``estimate_memory``.

Within a phase, items still sort by memory_bytes DESC, so memory-hungry
items get scheduled first. Sentinel/barrier items use ``memory_bytes = 0``
(clamped to the floor) so they fall to the end of their rank's slice.

Per-phase memory budgets are coarse class buckets sourced from the plan's
"Memory: there is no good per-item estimate" section -- we deliberately do
not regress per-item memory because empirical variance dominates any model.
"""

from __future__ import annotations

from typing import Final

# Phase ranks (per the plan's "Updated rank table (final)").
# Larger rank == earlier phase, because the Rust scheduler sorts DESC.
PHASE_3_VARIANT: Final[int] = 0
PHASE_2_BARRIER: Final[int] = 1
PHASE_2_BUILD: Final[int] = 2
PHASE_1B_BARRIER: Final[int] = 3
PHASE_1B_MERGE: Final[int] = 4
PHASE_1A_BARRIER: Final[int] = 5
PHASE_1A_PARTITION: Final[int] = 6

# Memory floor: the scheduler over-packs if we promise too little, so any
# request below this is bumped up.
MEMORY_FLOOR_BYTES: Final[int] = 512 * 1024 * 1024  # 512 MiB

# Low 48 bits hold the memory budget; ceiling is 256 TiB - 1, more than
# enough for any realistic single-task footprint.
MEMORY_MASK: Final[int] = (1 << 48) - 1

# Tier classification per the plan's variant memory table.
_TIER_1_PACKAGES: Final[frozenset[str]] = frozenset({"hello", "busybox"})
_TIER_2_PACKAGES: Final[frozenset[str]] = frozenset({"sqlite", "lua", "xz"})
_TIER_3_PACKAGES: Final[frozenset[str]] = frozenset({"coreutils", "gawk"})

_GIB: Final[int] = 1024 * 1024 * 1024


def encode_size(phase_rank: int, memory_bytes: int) -> int:
    """Pack ``phase_rank`` (high bits) and ``memory_bytes`` (low 48) into one int.

    ``memory_bytes`` is clamped to ``MEMORY_FLOOR_BYTES`` on the low end and
    ``MEMORY_MASK`` on the high end before packing. ``phase_rank`` must be a
    non-negative integer; it is not range-checked beyond that (callers
    should use the ``PHASE_*`` constants).
    """
    if phase_rank < 0:
        raise ValueError(f"phase_rank must be non-negative, got {phase_rank}")
    clamped = max(MEMORY_FLOOR_BYTES, min(memory_bytes, MEMORY_MASK))
    return (phase_rank << 48) | (clamped & MEMORY_MASK)


def decode_size(size: int) -> tuple[int, int]:
    """Return ``(phase_rank, memory_bytes)`` from a packed size integer."""
    if size < 0:
        raise ValueError(f"size must be non-negative, got {size}")
    return (size >> 48, size & MEMORY_MASK)


def tier_of(pkg: str) -> int:
    """Return the memory tier (1, 2, or 3) for ``pkg``.

    Tier 1: hello, busybox (small).
    Tier 2: sqlite, lua, xz (medium) -- also the default for unknown pkgs.
    Tier 3: coreutils, gawk (large).
    """
    if pkg in _TIER_1_PACKAGES:
        return 1
    if pkg in _TIER_3_PACKAGES:
        return 3
    # Tier 2 is the default for unknown packages: a safe medium budget.
    return 2


def variant_memory_bytes(pkg: str) -> int:
    """Return the phase-3 variant memory budget for ``pkg`` in bytes.

    Tier 1 -> 1 GiB, Tier 2 -> 2 GiB, Tier 3 -> 4 GiB.
    """
    tier = tier_of(pkg)
    if tier == 1:
        return 1 * _GIB
    if tier == 3:
        return 4 * _GIB
    return 2 * _GIB


def toolchain_memory_bytes() -> int:
    """Phase-2 toolchain budget: 6 GiB (gcc bootstrap is the worst case)."""
    return 6 * _GIB


def common_dep_memory_bytes() -> int:
    """Phase-2 common host-dep budget: 4 GiB (most are smaller; we pad)."""
    return 4 * _GIB


def partition_shard_memory_bytes() -> int:
    """Phase-1a partition-shard budget: 1 GiB."""
    return 1 * _GIB


def merge_memory_bytes() -> int:
    """Phase-1b merge budget: 2 GiB (reads ~170 JSON files into a dict)."""
    return 2 * _GIB

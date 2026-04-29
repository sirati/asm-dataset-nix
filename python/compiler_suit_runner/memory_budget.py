"""Per-type memory-budget helpers for the compiler-suit runner.

The dynamic_runner framework's :class:`TaskInfo` now carries a raw
``size`` integer (bytes), and per-phase / per-type ordering is owned
by the framework's :class:`PhaseSpec` / :class:`TaskTypeSpec` dependency
graph. We therefore no longer pack a phase rank into the high bits of
``size`` — these helpers return real per-item memory budgets in bytes.

Per-type memory budgets are coarse class buckets sourced from the plan's
"Memory: there is no good per-item estimate" section -- we deliberately
do not regress per-item memory because empirical variance dominates any
model.
"""

from __future__ import annotations

from typing import Final

# Memory floor: the scheduler over-packs if we promise too little, so any
# request below this is bumped up by the per-type estimator dispatch.
MEMORY_FLOOR_BYTES: Final[int] = 512 * 1024 * 1024  # 512 MiB

# Tier classification per the plan's variant memory table.
_TIER_1_PACKAGES: Final[frozenset[str]] = frozenset({"hello", "busybox"})
_TIER_2_PACKAGES: Final[frozenset[str]] = frozenset({"sqlite", "lua", "xz"})
_TIER_3_PACKAGES: Final[frozenset[str]] = frozenset({"coreutils", "gawk"})

_GIB: Final[int] = 1024 * 1024 * 1024


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

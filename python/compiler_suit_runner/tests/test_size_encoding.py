"""Round-trip and ordering tests for the size-encoding scheme."""

from __future__ import annotations

import itertools

import pytest

from compiler_suit_runner.memory_budget import (
    MEMORY_FLOOR_BYTES,
    MEMORY_MASK,
    PHASE_1A_BARRIER,
    PHASE_1A_PARTITION,
    PHASE_1B_BARRIER,
    PHASE_1B_MERGE,
    PHASE_2_BARRIER,
    PHASE_2_BUILD,
    PHASE_3_VARIANT,
    decode_size,
    encode_size,
)

ALL_RANKS: list[int] = [
    PHASE_3_VARIANT,
    PHASE_2_BARRIER,
    PHASE_2_BUILD,
    PHASE_1B_BARRIER,
    PHASE_1B_MERGE,
    PHASE_1A_BARRIER,
    PHASE_1A_PARTITION,
]

# Memory values we exercise across tests. All are >= the floor so they
# survive round-trip unchanged (clamping is tested separately).
SAMPLE_MEMORY_BYTES: list[int] = [
    MEMORY_FLOOR_BYTES,
    MEMORY_FLOOR_BYTES + 1,
    1 * 1024 * 1024 * 1024,
    2 * 1024 * 1024 * 1024,
    4 * 1024 * 1024 * 1024,
    6 * 1024 * 1024 * 1024,
    16 * 1024 * 1024 * 1024,
    MEMORY_MASK,
]


# --- Rank table sanity ------------------------------------------------------


def test_phase_ranks_are_strictly_descending_in_dispatch_order() -> None:
    """The plan's dispatch order is rank-DESC (larger rank fires first).

    Encode-DESC must therefore mean phase 1a runs before phase 3, etc.
    """
    dispatch_order = [
        PHASE_1A_PARTITION,
        PHASE_1A_BARRIER,
        PHASE_1B_MERGE,
        PHASE_1B_BARRIER,
        PHASE_2_BUILD,
        PHASE_2_BARRIER,
        PHASE_3_VARIANT,
    ]
    for earlier, later in itertools.pairwise(dispatch_order):
        assert earlier > later, (
            f"Phase ordering broken: rank {earlier} should be > {later}"
        )


def test_all_ranks_are_distinct() -> None:
    assert len(set(ALL_RANKS)) == len(ALL_RANKS)


# --- Round trip -------------------------------------------------------------


@pytest.mark.parametrize("rank", ALL_RANKS)
@pytest.mark.parametrize("memory", SAMPLE_MEMORY_BYTES)
def test_encode_decode_round_trip(rank: int, memory: int) -> None:
    size = encode_size(rank, memory)
    decoded_rank, decoded_memory = decode_size(size)
    assert decoded_rank == rank
    assert decoded_memory == memory


@pytest.mark.parametrize("rank", ALL_RANKS)
def test_round_trip_at_mask_ceiling(rank: int) -> None:
    size = encode_size(rank, MEMORY_MASK)
    decoded_rank, decoded_memory = decode_size(size)
    assert decoded_rank == rank
    assert decoded_memory == MEMORY_MASK


# --- Floor / clamp ----------------------------------------------------------


def test_zero_memory_is_clamped_up_to_floor() -> None:
    _, memory = decode_size(encode_size(PHASE_1A_BARRIER, 0))
    assert memory == MEMORY_FLOOR_BYTES


def test_below_floor_is_clamped_up_to_floor() -> None:
    for value in (1, 1024, MEMORY_FLOOR_BYTES - 1):
        _, memory = decode_size(encode_size(PHASE_3_VARIANT, value))
        assert memory == MEMORY_FLOOR_BYTES, value


def test_at_floor_is_unchanged() -> None:
    _, memory = decode_size(encode_size(PHASE_3_VARIANT, MEMORY_FLOOR_BYTES))
    assert memory == MEMORY_FLOOR_BYTES


def test_above_mask_is_clamped_down_to_mask() -> None:
    _, memory = decode_size(encode_size(PHASE_3_VARIANT, MEMORY_MASK + 1))
    assert memory == MEMORY_MASK
    _, memory = decode_size(encode_size(PHASE_3_VARIANT, MEMORY_MASK + 9999))
    assert memory == MEMORY_MASK


# --- Sort-key behaviour -----------------------------------------------------


@pytest.mark.parametrize("memory", SAMPLE_MEMORY_BYTES)
def test_higher_rank_always_sorts_first_desc(memory: int) -> None:
    """For any in-range memory, a higher rank produces a larger size.

    The Rust scheduler sorts size DESC; this guarantees phase ordering.
    """
    sizes = [(rank, encode_size(rank, memory)) for rank in ALL_RANKS]
    sizes_sorted = sorted(sizes, key=lambda pair: pair[1], reverse=True)
    expected = sorted(ALL_RANKS, reverse=True)
    assert [rank for rank, _ in sizes_sorted] == expected


def test_within_rank_higher_memory_sorts_first_desc() -> None:
    """Within a single rank, larger memory sorts first under DESC."""
    a = encode_size(PHASE_3_VARIANT, 1 * 1024 * 1024 * 1024)
    b = encode_size(PHASE_3_VARIANT, 4 * 1024 * 1024 * 1024)
    assert b > a


def test_cross_rank_dominates_memory_difference() -> None:
    """A higher rank with floor memory still beats a lower rank with mask memory.

    This is the load-bearing property: phase ordering must NEVER be defeated
    by per-item memory budgets, no matter how lopsided.
    """
    high_rank_low_mem = encode_size(PHASE_1A_PARTITION, 0)  # clamps to floor
    low_rank_high_mem = encode_size(PHASE_3_VARIANT, MEMORY_MASK)
    assert high_rank_low_mem > low_rank_high_mem


def test_neighboring_ranks_are_strictly_ordered() -> None:
    for higher, lower in itertools.pairwise(sorted(ALL_RANKS, reverse=True)):
        assert encode_size(higher, MEMORY_FLOOR_BYTES) > encode_size(
            lower, MEMORY_MASK
        ), f"rank {higher} did not dominate rank {lower}"


# --- Input validation -------------------------------------------------------


def test_negative_rank_rejected() -> None:
    with pytest.raises(ValueError):
        encode_size(-1, MEMORY_FLOOR_BYTES)


def test_decode_negative_rejected() -> None:
    with pytest.raises(ValueError):
        decode_size(-1)


def test_decode_zero_yields_rank_zero_and_zero_memory() -> None:
    """decode is not symmetric with encode for the zero input -- encode would
    clamp memory to the floor, but decode must faithfully invert any
    well-formed ``size``, including zero (e.g. for diagnostics)."""
    rank, memory = decode_size(0)
    assert rank == 0
    assert memory == 0

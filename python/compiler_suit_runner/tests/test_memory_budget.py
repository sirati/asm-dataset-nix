"""Tier classification and per-phase memory-budget tests."""

from __future__ import annotations

import pytest

from compiler_suit_runner.memory_budget import (
    MEMORY_FLOOR_BYTES,
    MEMORY_MASK,
    PHASE_1A_PARTITION,
    PHASE_2_BUILD,
    PHASE_3_VARIANT,
    common_dep_memory_bytes,
    encode_size,
    merge_memory_bytes,
    partition_shard_memory_bytes,
    tier_of,
    toolchain_memory_bytes,
    variant_memory_bytes,
)

_GIB = 1024 * 1024 * 1024


# --- tier_of -----------------------------------------------------------------


@pytest.mark.parametrize("pkg", ["hello", "busybox"])
def test_tier_1_packages(pkg: str) -> None:
    assert tier_of(pkg) == 1


@pytest.mark.parametrize("pkg", ["sqlite", "lua", "xz"])
def test_tier_2_packages(pkg: str) -> None:
    assert tier_of(pkg) == 2


@pytest.mark.parametrize("pkg", ["coreutils", "gawk"])
def test_tier_3_packages(pkg: str) -> None:
    assert tier_of(pkg) == 3


@pytest.mark.parametrize(
    "pkg",
    [
        "",
        "unknown-package",
        "Hello",  # case sensitive on purpose
        "BUSYBOX",
        "openssl",
        "gcc",
    ],
)
def test_unknown_packages_default_to_tier_2(pkg: str) -> None:
    assert tier_of(pkg) == 2


# --- variant_memory_bytes ----------------------------------------------------


def test_variant_memory_tier_1() -> None:
    assert variant_memory_bytes("hello") == 1 * _GIB
    assert variant_memory_bytes("busybox") == 1 * _GIB


def test_variant_memory_tier_2() -> None:
    assert variant_memory_bytes("sqlite") == 2 * _GIB
    assert variant_memory_bytes("lua") == 2 * _GIB
    assert variant_memory_bytes("xz") == 2 * _GIB


def test_variant_memory_tier_3() -> None:
    assert variant_memory_bytes("coreutils") == 4 * _GIB
    assert variant_memory_bytes("gawk") == 4 * _GIB


def test_variant_memory_unknown_defaults_to_tier_2() -> None:
    assert variant_memory_bytes("definitely-not-a-real-pkg") == 2 * _GIB


# --- per-phase fixed budgets -------------------------------------------------


def test_toolchain_memory_is_six_gib() -> None:
    assert toolchain_memory_bytes() == 6 * _GIB


def test_common_dep_memory_is_four_gib() -> None:
    assert common_dep_memory_bytes() == 4 * _GIB


def test_partition_shard_memory_is_one_gib() -> None:
    assert partition_shard_memory_bytes() == 1 * _GIB


def test_merge_memory_is_two_gib() -> None:
    assert merge_memory_bytes() == 2 * _GIB


def test_all_budgets_are_at_or_above_floor() -> None:
    """No declared budget should ever fall below the scheduler floor; the
    floor is for sentinels only, not real work."""
    for budget_fn in (
        toolchain_memory_bytes,
        common_dep_memory_bytes,
        partition_shard_memory_bytes,
        merge_memory_bytes,
    ):
        assert budget_fn() >= MEMORY_FLOOR_BYTES, budget_fn.__name__
    for pkg in ("hello", "sqlite", "coreutils", "unknown-pkg"):
        assert variant_memory_bytes(pkg) >= MEMORY_FLOOR_BYTES, pkg


def test_all_budgets_fit_in_48_bits() -> None:
    """The size-encoding scheme reserves 48 bits for memory; budgets must fit."""
    for budget_fn in (
        toolchain_memory_bytes,
        common_dep_memory_bytes,
        partition_shard_memory_bytes,
        merge_memory_bytes,
    ):
        assert budget_fn() <= MEMORY_MASK, budget_fn.__name__
    for pkg in ("hello", "sqlite", "coreutils"):
        assert variant_memory_bytes(pkg) <= MEMORY_MASK, pkg


# --- ordering invariant tying budgets back to ranks --------------------------


@pytest.mark.parametrize(
    "in_range_memory",
    [
        MEMORY_FLOOR_BYTES,
        1 * _GIB,
        2 * _GIB,
        4 * _GIB,
        6 * _GIB,
        MEMORY_MASK,
    ],
)
def test_phase_1a_partition_dominates_phase_2_build(in_range_memory: int) -> None:
    """The integration property the plan calls out explicitly:
    encode_size(PHASE_1A_PARTITION, X) > encode_size(PHASE_2_BUILD, X) for any
    in-range X. This ensures the partition phase always dispatches before the
    toolchain build phase regardless of memory budgets."""
    assert encode_size(PHASE_1A_PARTITION, in_range_memory) > encode_size(
        PHASE_2_BUILD, in_range_memory
    )


def test_phase_2_build_dominates_phase_3_variant() -> None:
    for memory in (MEMORY_FLOOR_BYTES, 2 * _GIB, MEMORY_MASK):
        assert encode_size(PHASE_2_BUILD, memory) > encode_size(
            PHASE_3_VARIANT, memory
        )


def test_real_budgets_still_respect_phase_order() -> None:
    """Use the actual budget values (not synthetic) and confirm that phase
    ranks dominate. This catches accidental tier inflation that breaks the
    48-bit reservation."""
    assert encode_size(
        PHASE_1A_PARTITION, partition_shard_memory_bytes()
    ) > encode_size(PHASE_2_BUILD, toolchain_memory_bytes())
    assert encode_size(PHASE_2_BUILD, toolchain_memory_bytes()) > encode_size(
        PHASE_3_VARIANT, variant_memory_bytes("coreutils")
    )

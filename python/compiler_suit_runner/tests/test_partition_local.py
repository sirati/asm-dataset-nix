"""Unit tests for ``compiler_suit_runner.partition_local``.

All ``nix derivation show`` invocations are stubbed so the test suite
never shells out to nix. The injection seam is the ``run_subprocess``
parameter on :func:`compute_partition_locally`.

The fake-subprocess pattern mirrors
``tests.test_preflight._make_run_subprocess``: argv comes in, the
final positional arg is the drv path being shown, and the fake looks
up a pre-baked ``nix derivation show`` payload keyed by that drv path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from compiler_suit_runner.partition import VariantSpec
from compiler_suit_runner.partition_local import (
    PartitionResult,
    _label_from_drv,
    compute_partition_locally,
)


# ---------------------------------------------------------------------------
# Fake-subprocess helpers


def _make_run_subprocess(graphs: dict[str, dict[str, Any]]):
    """Return a fake ``run_subprocess`` that maps a root drv -> sub-graph.

    Each entry of ``graphs`` is the JSON object that
    ``nix derivation show --recursive <drv>`` would print: a flat
    ``{drv_path: node, ...}`` map covering the variant's transitive
    sub-graph. The argv format from
    :mod:`compiler_suit_runner.partition_local` is

        ["nix", "--extra-experimental-features", "nix-command flakes",
         "derivation", "show", "--recursive", "<drv>"]

    so we read the final positional arg as the root drv and look it up
    in ``graphs``. Unknown drvs raise (return rc=1) to surface mistakes
    loudly.
    """
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        target = argv[-1]
        if target not in graphs:
            return b"", f"no fake for {target}".encode(), 1
        return json.dumps(graphs[target]).encode("utf-8"), b"", 0

    return runner, calls


def _node(input_drvs: list[str]) -> dict[str, Any]:
    """Build one ``nix derivation show`` node with newer nested schema."""
    return {
        "inputDrvs": {drv: {"outputs": ["out"]} for drv in input_drvs},
        # Other fields nix emits are irrelevant for partition logic.
    }


def _variant(label: str, drv: str, *, pkg: str = "hello", arch: str = "x86_64") -> VariantSpec:
    """Construct a minimal :class:`VariantSpec` for tests."""
    return {
        "label": label,
        "drv": drv,
        "tarball_name": f"{label}.tar.zst",
        "compiler_id": "gcc15",
        "tier": 1,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Test data
#
# The graph the variants share looks like:
#
#   root_v1 -> shared_dep, lib_a
#   root_v2 -> shared_dep, lib_b
#   root_v3 -> shared_dep, lib_c
#
# i.e. ``shared_dep`` appears in 3 variants; ``lib_a/lib_b/lib_c``
# only one each.


_ROOT_V1 = "/nix/store/aaa-hello-v1.drv"
_ROOT_V2 = "/nix/store/bbb-hello-v2.drv"
_ROOT_V3 = "/nix/store/ccc-hello-v3.drv"

_SHARED_DEP = "/nix/store/ddd-glibc-2.39.drv"
_LIB_A = "/nix/store/eee-libA.drv"
_LIB_B = "/nix/store/fff-libB.drv"
_LIB_C = "/nix/store/ggg-libC.drv"


def _three_variant_graphs() -> dict[str, dict[str, Any]]:
    """Build per-root sub-graphs for the three-variant fixture."""
    common = {
        _SHARED_DEP: _node([]),
    }
    return {
        _ROOT_V1: {
            _ROOT_V1: _node([_SHARED_DEP, _LIB_A]),
            _LIB_A: _node([]),
            **common,
        },
        _ROOT_V2: {
            _ROOT_V2: _node([_SHARED_DEP, _LIB_B]),
            _LIB_B: _node([]),
            **common,
        },
        _ROOT_V3: {
            _ROOT_V3: _node([_SHARED_DEP, _LIB_C]),
            _LIB_C: _node([]),
            **common,
        },
    }


def _three_variants() -> tuple[VariantSpec, ...]:
    return (
        _variant("hello-v1", _ROOT_V1),
        _variant("hello-v2", _ROOT_V2),
        _variant("hello-v3", _ROOT_V3),
    )


# ---------------------------------------------------------------------------
# Refcount basics


def test_refcount_three_variants_share_one_drv():
    """3 variants share ``_SHARED_DEP`` -> refcount 3, included at threshold=3."""
    runner, _calls = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=3,
        run_subprocess=runner,
    )
    assert isinstance(result, PartitionResult)
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert _SHARED_DEP in drvs
    # The non-shared libs are below threshold.
    assert _LIB_A not in drvs
    assert _LIB_B not in drvs
    assert _LIB_C not in drvs


def test_per_variant_inputs_populated():
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=3,
        run_subprocess=runner,
    )
    assert set(result.per_variant_inputs.keys()) == {
        "hello-v1",
        "hello-v2",
        "hello-v3",
    }
    assert result.per_variant_inputs["hello-v1"] == frozenset(
        {_SHARED_DEP, _LIB_A}
    )
    assert result.per_variant_inputs["hello-v2"] == frozenset(
        {_SHARED_DEP, _LIB_B}
    )
    assert result.per_variant_inputs["hello-v3"] == frozenset(
        {_SHARED_DEP, _LIB_C}
    )


# ---------------------------------------------------------------------------
# Threshold semantics


def test_threshold_exact_match_included():
    """Drv at exactly ``threshold`` qualifies (>= comparison)."""
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=3,  # _SHARED_DEP refcount is exactly 3
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert _SHARED_DEP in drvs


def test_threshold_above_excludes():
    """Drv with refcount strictly below threshold is excluded."""
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=4,  # _SHARED_DEP refcount is 3 < 4
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert _SHARED_DEP not in drvs


def test_threshold_one_includes_singletons():
    """``threshold=1`` => every observed input drv is common."""
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert drvs == {_SHARED_DEP, _LIB_A, _LIB_B, _LIB_C}


def test_threshold_zero_includes_everything_observed():
    """``threshold=0`` is equivalent to threshold=1 in practice.

    No drv is recorded with refcount < 1 (we only ever count drvs we
    saw at least once), so threshold=0 collapses to "every observed
    input drv is common".
    """
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=0,
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert drvs == {_SHARED_DEP, _LIB_A, _LIB_B, _LIB_C}


# ---------------------------------------------------------------------------
# Toolchain exclusion


def test_toolchain_drv_excluded_even_above_threshold():
    """A drv in ``toolchain_drvs`` is filtered out of common_deps."""
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset({_SHARED_DEP}),
        threshold=3,
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert _SHARED_DEP not in drvs


def test_variant_root_in_toolchain_drvs_does_not_appear():
    """Variant root drvs never appear in common_deps (excluded by walk).

    The walk excludes ``root_drv`` from its own input set, so even if
    a variant's root showed up in another variant's transitive deps it
    would only appear via that other variant's graph. We verify the
    walk's exclusion behaviour: per_variant_inputs[v1] doesn't contain
    _ROOT_V1 itself.
    """
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset({_ROOT_V1, _ROOT_V2, _ROOT_V3}),
        threshold=1,
        run_subprocess=runner,
    )
    for label, inputs in result.per_variant_inputs.items():
        if label == "hello-v1":
            assert _ROOT_V1 not in inputs


# ---------------------------------------------------------------------------
# Stable labels


def test_label_derivation_strips_hash_and_dots():
    """``_label_from_drv`` produces filesystem-safe names from drv paths."""
    assert _label_from_drv("/nix/store/abc123-glibc-2.39.drv") == "glibc-2-39"
    assert _label_from_drv("/nix/store/xyz789-gcc-13-cc.drv") == "gcc-13-cc"
    # Underscores survive.
    assert _label_from_drv("/nix/store/aaa-foo_bar.drv") == "foo_bar"


def test_label_stable_under_iteration_order():
    """Same set of drvs -> same labels regardless of input variant order.

    We run the partition twice with reversed variant order and check
    the resulting common_dep_drvs (sorted) match exactly.
    """
    runner1, _ = _make_run_subprocess(_three_variant_graphs())
    result1 = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner1,
    )
    runner2, _ = _make_run_subprocess(_three_variant_graphs())
    result2 = compute_partition_locally(
        tuple(reversed(_three_variants())),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner2,
    )
    assert result1.common_dep_drvs == result2.common_dep_drvs


def test_common_dep_drvs_sorted_deterministically():
    runner, _ = _make_run_subprocess(_three_variant_graphs())
    result = compute_partition_locally(
        _three_variants(),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner,
    )
    pairs = list(result.common_dep_drvs)
    assert pairs == sorted(pairs)


# ---------------------------------------------------------------------------
# Edge cases


def test_empty_variants_returns_empty_result():
    runner, calls = _make_run_subprocess({})
    result = compute_partition_locally(
        (),
        toolchain_drvs=frozenset(),
        threshold=10,
        run_subprocess=runner,
    )
    assert result.common_dep_drvs == ()
    assert result.per_variant_inputs == {}
    # No subprocess calls at all when there are no variants.
    assert calls == []


def test_single_variant_threshold_one_lists_all_inputs():
    runner, _ = _make_run_subprocess(
        {
            _ROOT_V1: {
                _ROOT_V1: _node([_SHARED_DEP, _LIB_A]),
                _SHARED_DEP: _node([]),
                _LIB_A: _node([]),
            }
        }
    )
    result = compute_partition_locally(
        (_variant("hello-v1", _ROOT_V1),),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner,
    )
    drvs = {drv for _label, drv in result.common_dep_drvs}
    assert drvs == {_SHARED_DEP, _LIB_A}


def test_subprocess_failure_propagates_as_runtime_error():
    def runner(argv):
        return b"", b"derivation not in store", 1

    with pytest.raises(RuntimeError, match="derivation not in store"):
        compute_partition_locally(
            (_variant("hello-v1", _ROOT_V1),),
            toolchain_drvs=frozenset(),
            threshold=1,
            run_subprocess=runner,
        )


def test_invalid_json_propagates_as_runtime_error():
    def runner(argv):
        return b"not json", b"", 0

    with pytest.raises(RuntimeError, match="invalid JSON"):
        compute_partition_locally(
            (_variant("hello-v1", _ROOT_V1),),
            toolchain_drvs=frozenset(),
            threshold=1,
            run_subprocess=runner,
        )


def test_graph_cache_avoids_redundant_show_calls():
    """Two variants whose roots already live in the cumulative cache
    should not re-trigger ``nix derivation show``.

    This is a behavioural property of the implementation — the
    cumulative cache merges every sub-graph as it goes, so if a later
    variant's root is already known we should skip the call. In the
    fixture below, v2's root happens to live in v1's sub-graph, so v2
    must not produce a second call.
    """
    # v1 transitively contains v2's root drv, simulating a case where
    # the first --recursive show already returned everything we need.
    other_root = "/nix/store/zzz-other.drv"
    graphs = {
        _ROOT_V1: {
            _ROOT_V1: _node([other_root, _SHARED_DEP]),
            other_root: _node([_SHARED_DEP]),
            _SHARED_DEP: _node([]),
        },
        # other_root would have its own graph if asked, but we expect
        # the cumulative cache to make this unnecessary.
    }
    runner, calls = _make_run_subprocess(graphs)
    result = compute_partition_locally(
        (
            _variant("v1", _ROOT_V1),
            _variant("v2", other_root),
        ),
        toolchain_drvs=frozenset(),
        threshold=1,
        run_subprocess=runner,
    )
    # Only one call: the second variant's root was already cached.
    assert len(calls) == 1
    # Both variants resolved their inputs correctly:
    assert result.per_variant_inputs["v1"] == frozenset(
        {other_root, _SHARED_DEP}
    )
    assert result.per_variant_inputs["v2"] == frozenset({_SHARED_DEP})

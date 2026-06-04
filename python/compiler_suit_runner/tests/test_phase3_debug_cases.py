"""Parameterised regression harness for the phase-3 debug-case fixtures.

Each fixture under ``tests/fixtures/phase3_debug_cases/`` carries
``tree.txt``, ``variant_lookup.json`` (list-of-records), and
``toolchain_task_ids.json`` + ``expected.json``. The harness runs
``plan_from_tree_streaming(lax=True)`` followed by
``plan_phase4_for_binary(...)`` (matching the production
``dependency_graph_worker`` default) and asserts each present
``expected`` field. Assertion messages always include the fixture name.

Two cross-fixture invariants are also enforced:

  * one ``build_variant`` per lookup entry, and
  * every common-dep task has >= 2 dependents -- a one-consumer dep
    should have been inlined; emitting it as its own task wastes
    dispatch. Toolchain deps are cross-phase (in each variant's
    ``build_compilers_depends_on``, never an intra-phase ``depends_on``
    edge) so they don't participate in this count; per-fixture opt-out
    via ``relax_dependent_count: true``.

Expected.json fields (all optional, unknown ignored):

  build_variant_count, common_deps_cross_arch_min,
  source_terminal_skipped_min, violations_min/max,
  violations_at_least_kinds, per_variant_toolchain_task,
  no_common_dep_for_terminals, relax_dependent_count.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from compiler_suit_runner.dependency_graph_planner import (
    plan_phase4_for_binary,
)
from template_graph.streaming import plan_from_tree_streaming


# ---------------------------------------------------------------------------
# Discovery + loaders
# ---------------------------------------------------------------------------


_CASES_DIR = (
    pathlib.Path(__file__).parent / "fixtures" / "phase3_debug_cases"
)

# All fixtures share the same synthetic binary name.
_FIXTURE_BINARY = "hello"


def _discover_cases() -> list[pathlib.Path]:
    """Fixture sub-directories under ``_CASES_DIR``, sorted for
    deterministic pytest parameter ids."""
    return sorted(p for p in _CASES_DIR.iterdir() if p.is_dir())


def _load_tree(case_dir: pathlib.Path) -> str:
    return (case_dir / "tree.txt").read_text()


def _load_variant_lookup(
    case_dir: pathlib.Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Deserialise list-of-records into the tuple-keyed mapping the
    planner consumes. JSON cannot represent tuple keys directly, hence
    the list-of-records detour at fixture-emit time.
    """
    raw = json.loads((case_dir / "variant_lookup.json").read_text())
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for record in raw:
        key = record["key"]
        # ``key`` round-trips as a 2-element list; restore the tuple.
        out[(key[0], key[1])] = record["value"]
    return out


def _load_toolchain_task_ids(case_dir: pathlib.Path) -> dict[str, str]:
    return json.loads((case_dir / "toolchain_task_ids.json").read_text())


def _load_expected(case_dir: pathlib.Path) -> dict[str, Any]:
    expected_path = case_dir / "expected.json"
    if not expected_path.exists():
        raise AssertionError(
            f"fixture {case_dir.name!r}: expected.json missing"
        )
    return json.loads(expected_path.read_text())


# ---------------------------------------------------------------------------
# Helpers for the test body
# ---------------------------------------------------------------------------


def _maybe_inject_toolchain_task_ids(
    toolchain_task_ids: dict[str, str],
    expected: dict[str, Any],
) -> dict[str, str]:
    """When ``toolchain_task_ids.json`` is empty but the fixture
    expects non-null ``per_variant_toolchain_task`` values, synthesise
    a mapping so the planner's ``known_task_ids`` (a frozenset over
    the VALUES only) contains the expected task_ids. Keys are
    irrelevant for the wiring path exercised here.
    """
    if toolchain_task_ids:
        return toolchain_task_ids
    per_variant = expected.get("per_variant_toolchain_task")
    if not isinstance(per_variant, dict):
        return toolchain_task_ids
    non_null_ids = sorted(
        {v for v in per_variant.values() if isinstance(v, str)}
    )
    if not non_null_ids:
        return toolchain_task_ids
    return {
        f"synthetic-{idx}-toolchain.drv": task_id
        for idx, task_id in enumerate(non_null_ids)
    }


def _find_variant_descriptor(descriptors, label: str):
    for d in descriptors:
        if d.kind == "build_variant" and d.payload.get("label") == label:
            return d
    return None


def _dependent_counts(descriptors) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in descriptors:
        for dep_task_id in (d.depends_on or ()):
            counts[dep_task_id] = counts.get(dep_task_id, 0) + 1
    return counts


def _assert_planner_invariants(
    case_name: str,
    descriptors,
    variant_lookup,
    expected: dict[str, Any],
) -> None:
    """Cross-fixture invariants: exactly one ``build_variant`` per
    lookup entry, and every common-dep task carries >= 2 dependents.
    Toolchain deps are cross-phase (each variant's
    ``build_compilers_depends_on``, not an intra-phase ``depends_on``
    edge) so they never appear in the dependent count. Fixtures may opt
    out of the dedup check via ``relax_dependent_count: true`` in
    ``expected.json``.
    """
    variants = [d for d in descriptors if d.kind == "build_variant"]
    assert len(variants) == len(variant_lookup), (
        f"fixture {case_name!r}: expected len(variant_lookup)="
        f"{len(variant_lookup)} build_variant descriptors, got "
        f"{len(variants)}"
    )
    if expected.get("relax_dependent_count"):
        return
    counts = _dependent_counts(descriptors)
    variant_task_ids = {d.task_id for d in variants}
    # Phase-4 descriptors are only build_common_dep / build_variant;
    # toolchain tasks live in phase-1 and reach variants via the
    # cross-phase build_compilers_depends_on field, so they never show
    # up as descriptors here.
    violators = sorted(
        d.task_id for d in descriptors
        if d.task_id not in variant_task_ids
        and counts.get(d.task_id, 0) < 2
    )
    assert not violators, (
        f"fixture {case_name!r}: common-dep tasks with < 2 "
        f"dependents (one-consumer dep should have been inlined): "
        f"{violators}"
    )


# ---------------------------------------------------------------------------
# Parameterised test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_dir",
    _discover_cases(),
    ids=lambda p: p.name,
)
def test_phase3_debug_case(case_dir: pathlib.Path) -> None:
    """One assertion per present ``expected.json`` field plus the two
    cross-fixture invariants. Missing fields are left unchecked.
    """
    case_name = case_dir.name
    tree_text = _load_tree(case_dir)
    variant_lookup = _load_variant_lookup(case_dir)
    expected = _load_expected(case_dir)
    toolchain_task_ids = _maybe_inject_toolchain_task_ids(
        _load_toolchain_task_ids(case_dir), expected,
    )
    # Match the production default (``dependency_graph_worker`` runs
    # with ``lax=True``); strict mode raises TreeWalkError on the
    # lax_mode_violation_recorded fixture.
    result = plan_from_tree_streaming(tree_text, lax=True)
    descriptors = plan_phase4_for_binary(
        _FIXTURE_BINARY, result, variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    _assert_planner_invariants(
        case_name, descriptors, variant_lookup, expected,
    )

    # ── build_variant_count ────────────────────────────────────────
    if "build_variant_count" in expected:
        n_var = sum(1 for d in descriptors if d.kind == "build_variant")
        assert n_var == expected["build_variant_count"], (
            f"fixture {case_name!r}: build_variant_count expected "
            f"{expected['build_variant_count']}, got {n_var}"
        )

    # ── common_deps_cross_arch_min ─────────────────────────────────
    if expected.get("common_deps_cross_arch_min") is not None:
        threshold = expected["common_deps_cross_arch_min"]
        n_x = sum(
            1 for d in descriptors
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__cross_arch__")
        )
        assert n_x >= threshold, (
            f"fixture {case_name!r}: cross-arch common_dep expected "
            f">= {threshold}, got {n_x}"
        )

    # ── source_terminal_skipped_min ────────────────────────────────
    if expected.get("source_terminal_skipped_min") is not None:
        threshold = expected["source_terminal_skipped_min"]
        skipped = result.get("source_terminal_skipped", 0)
        assert skipped >= threshold, (
            f"fixture {case_name!r}: source_terminal_skipped expected "
            f">= {threshold}, got {skipped}"
        )

    # ── violations_min / violations_max / violations_at_least_kinds ─
    violations = result.get("violations", []) or []
    if expected.get("violations_min") is not None:
        assert len(violations) >= expected["violations_min"], (
            f"fixture {case_name!r}: violations >= "
            f"{expected['violations_min']}, got {len(violations)}"
        )
    if expected.get("violations_max") is not None:
        assert len(violations) <= expected["violations_max"], (
            f"fixture {case_name!r}: violations <= "
            f"{expected['violations_max']}, got {len(violations)}"
        )
    if expected.get("violations_at_least_kinds"):
        seen = {v.get("kind") for v in violations}
        for kind in expected["violations_at_least_kinds"]:
            assert kind in seen, (
                f"fixture {case_name!r}: missing violation kind {kind!r} "
                f"(seen: {sorted(k for k in seen if k)})"
            )

    # ── per_variant_toolchain_task ─────────────────────────────────
    if "per_variant_toolchain_task" in expected:
        for label, expected_task_id in expected["per_variant_toolchain_task"].items():
            descriptor = _find_variant_descriptor(descriptors, label)
            assert descriptor is not None, (
                f"fixture {case_name!r}: no build_variant descriptor "
                f"with label {label!r}"
            )
            # Toolchain deps are CROSS-phase: they live in the dedicated
            # build_compilers_depends_on field (bare ids), no longer in
            # the intra-phase depends_on.
            deps = descriptor.build_compilers_depends_on
            # ``bad`` is the set of toolchain deps OTHER than the
            # expected one. Every entry in this field is a toolchain dep,
            # so a regression mixing gcc14 + clang20 task ids into a
            # gcc14-O2 variant surfaces here.
            bad = [d for d in deps if d != expected_task_id]
            if expected_task_id is not None:
                assert expected_task_id in deps, (
                    f"fixture {case_name!r}: variant {label!r} missing "
                    f"toolchain dep {expected_task_id!r} (deps: {list(deps)})"
                )
            assert not bad, (
                f"fixture {case_name!r}: variant {label!r} has unexpected "
                f"toolchain deps {bad!r} (expected: {expected_task_id!r})"
            )

    # ── no_common_dep_for_terminals ────────────────────────────────
    if expected.get("no_common_dep_for_terminals"):
        idents = [
            d.payload.get("ident", "")
            for d in descriptors if d.kind == "build_common_dep"
        ]
        for forbidden in expected["no_common_dep_for_terminals"]:
            leaked = [i for i in idents if forbidden in i]
            assert not leaked, (
                f"fixture {case_name!r}: source-terminal {forbidden!r} "
                f"leaked into build_common_dep descriptors: {leaked!r}"
            )

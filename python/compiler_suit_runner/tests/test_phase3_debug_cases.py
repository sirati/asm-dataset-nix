"""Parameterised regression harness for the phase-3 debug-case fixtures.

The fixtures under ``tests/fixtures/phase3_debug_cases/`` each pin one
specific behaviour of the dependency-graph planner. Every fixture
carries ``tree.txt``, ``variant_lookup.json`` (list-of-records, JSON-
serialisable form of the tuple-keyed mapping), ``toolchain_task_ids.json``,
and ``expected.json``. The harness runs
``plan_from_tree_streaming(tree_text, lax=True)`` followed by
``plan_phase4_for_binary(...)`` (matching the production
``dependency_graph_worker`` default) and asserts each present expected
field. Assertion failure messages always include the fixture name.

Expected.json schema (every field optional; unknown fields ignored for
forward-compat):

  build_variant_count: int
  common_deps_cross_arch_min: int|null
  source_terminal_skipped_min: int|null
  violations_max: int|null
  violations_min: int|null
  violations_at_least_kinds: list[str]
  per_variant_toolchain_task: dict[label_str, task_id_or_null]
  no_common_dep_for_terminals: list[drv_basename_str]
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
    """Enumerate fixture sub-directories under ``_CASES_DIR``.

    Sorted to keep pytest's parameter-id ordering stable across
    platforms / filesystems.
    """
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
    """When ``toolchain_task_ids.json`` is empty but the fixture's
    ``per_variant_toolchain_task`` expects non-null task ids, synthesise
    a mapping that puts those task_ids into the planner's
    ``known_task_ids`` set.

    ``_variant_toolchain_dep`` composes
    ``build_compilers__<sys>__<arch>__<comp>`` from the variant drv's
    basename and only emits the wiring when the composed id is present
    in ``known_task_ids = frozenset(toolchain_task_ids.values())``. We
    therefore only need the VALUES of the mapping to contain the
    expected task_ids -- the keys are irrelevant for the wiring path
    exercised by these fixtures (they would matter only if the planner
    were also resolving role-name → ident → task_id via the toolchain
    drv set, which these debug-case fixtures do not pin).

    Fixtures whose ``per_variant_toolchain_task`` values are all
    ``null`` (e.g. ``cross_arch_common_dep_two_archs``) document the
    "no wiring expected" case and need no injection.
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


# ---------------------------------------------------------------------------
# Parameterised test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_dir",
    _discover_cases(),
    ids=lambda p: p.name,
)
def test_phase3_debug_case(case_dir: pathlib.Path) -> None:
    """Run one phase-3 debug-case fixture through the planner and
    assert against ``expected.json``.

    Each present field in ``expected.json`` adds one assertion;
    missing fields leave that aspect unchecked (fixtures pin one
    behaviour each on purpose). Assertion messages always include the
    fixture name so a regression points at the right case.
    """
    case_name = case_dir.name

    tree_text = _load_tree(case_dir)
    variant_lookup = _load_variant_lookup(case_dir)
    raw_toolchain_task_ids = _load_toolchain_task_ids(case_dir)
    expected = _load_expected(case_dir)

    toolchain_task_ids = _maybe_inject_toolchain_task_ids(
        raw_toolchain_task_ids, expected,
    )

    # Match the production default (``dependency_graph_worker`` runs
    # with ``lax=True``); strict mode would raise TreeWalkError on the
    # lax_mode_violation_recorded fixture.
    result = plan_from_tree_streaming(tree_text, lax=True)
    descriptors = plan_phase4_for_binary(
        _FIXTURE_BINARY,
        result,
        variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )

    # ── build_variant_count ────────────────────────────────────────
    if "build_variant_count" in expected:
        expected_count = expected["build_variant_count"]
        variant_count = sum(
            1 for d in descriptors if d.kind == "build_variant"
        )
        assert variant_count == expected_count, (
            f"fixture {case_name!r}: build_variant_count "
            f"expected {expected_count}, got {variant_count}"
        )

    # ── common_deps_cross_arch_min ─────────────────────────────────
    if expected.get("common_deps_cross_arch_min") is not None:
        threshold = expected["common_deps_cross_arch_min"]
        cross_arch_count = sum(
            1 for d in descriptors
            if d.kind == "build_common_dep"
            and d.task_id.startswith("build_common_dep__cross_arch__")
        )
        assert cross_arch_count >= threshold, (
            f"fixture {case_name!r}: cross-arch common_dep count "
            f"expected >= {threshold}, got {cross_arch_count}"
        )

    # ── source_terminal_skipped_min ────────────────────────────────
    if expected.get("source_terminal_skipped_min") is not None:
        threshold = expected["source_terminal_skipped_min"]
        skipped = result.get("source_terminal_skipped", 0)
        assert skipped >= threshold, (
            f"fixture {case_name!r}: source_terminal_skipped "
            f"expected >= {threshold}, got {skipped}"
        )

    # ── violations_min / violations_max ────────────────────────────
    violations = result.get("violations", []) or []
    if expected.get("violations_min") is not None:
        v_min = expected["violations_min"]
        assert len(violations) >= v_min, (
            f"fixture {case_name!r}: violations count expected >= "
            f"{v_min}, got {len(violations)} ({violations!r})"
        )
    if expected.get("violations_max") is not None:
        v_max = expected["violations_max"]
        assert len(violations) <= v_max, (
            f"fixture {case_name!r}: violations count expected <= "
            f"{v_max}, got {len(violations)} ({violations!r})"
        )

    # ── violations_at_least_kinds ─────────────────────────────────
    if expected.get("violations_at_least_kinds"):
        required_kinds = expected["violations_at_least_kinds"]
        seen_kinds = {v.get("kind") for v in violations}
        for kind in required_kinds:
            assert kind in seen_kinds, (
                f"fixture {case_name!r}: required violation kind "
                f"{kind!r} not in {sorted(k for k in seen_kinds if k)}"
            )

    # ── per_variant_toolchain_task ─────────────────────────────────
    if "per_variant_toolchain_task" in expected:
        per_variant = expected["per_variant_toolchain_task"]
        for label, expected_task_id in per_variant.items():
            descriptor = _find_variant_descriptor(descriptors, label)
            assert descriptor is not None, (
                f"fixture {case_name!r}: no build_variant descriptor "
                f"with label {label!r} (have: "
                f"{sorted(d.payload.get('label') for d in descriptors if d.kind == 'build_variant')})"
            )
            deps = descriptor.depends_on
            if expected_task_id is None:
                # No toolchain wiring expected: no build_compilers__*
                # dep present on this variant.
                bad = [d for d in deps if d.startswith("build_compilers__")]
                assert not bad, (
                    f"fixture {case_name!r}: variant {label!r} expected "
                    f"no toolchain dep, got {bad!r}"
                )
            else:
                assert expected_task_id in deps, (
                    f"fixture {case_name!r}: variant {label!r} missing "
                    f"toolchain dep {expected_task_id!r} "
                    f"(deps: {list(deps)})"
                )
                # Beyond presence: variant must carry EXACTLY this
                # one toolchain dep, not also others. A regression
                # over-wiring both gcc14 and clang20 task_ids into a
                # gcc14-O2 variant would slip past a plain `in deps`
                # check; this catches it.
                bad = [
                    d for d in deps
                    if d.startswith("build_compilers__")
                    and d != expected_task_id
                ]
                assert not bad, (
                    f"fixture {case_name!r}: variant {label!r} has "
                    f"unexpected toolchain deps {bad!r}; expected only "
                    f"{expected_task_id!r}"
                )

    # ── no_common_dep_for_terminals ────────────────────────────────
    if expected.get("no_common_dep_for_terminals"):
        forbidden_names = expected["no_common_dep_for_terminals"]
        common_dep_idents = [
            d.payload.get("ident", "")
            for d in descriptors
            if d.kind == "build_common_dep"
        ]
        for forbidden in forbidden_names:
            leaked = [
                ident for ident in common_dep_idents
                if forbidden in ident
            ]
            assert not leaked, (
                f"fixture {case_name!r}: source-terminal "
                f"{forbidden!r} leaked into build_common_dep "
                f"descriptors: {leaked!r}"
            )

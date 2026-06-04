"""Live-matrix end-to-end smoke test for the Phase-3 streaming planner.

Complements the hand-built ``phase3_debug_cases`` corpus (sibling D.3)
by exercising the FULL discoverable compiler / arch matrix through real
``nix-instantiate`` AND the aggregate-wrapper flow the production code
path now uses (phase 1 ``toolchains`` aggregate, phase 2
``matrix-<binary>`` aggregate, phase 3 ``make_sum_drv_from_paths``).
Scope:

  * 1 binary (``hello``), x86_64-linux primary, all 7 opt levels;
  * all compilers from ``_crossToolchainMap.<sys>`` + all archs from
    ``_debug.targets`` (overrideable via ``ASM_PHASE3_SMOKE_ARCHS``;
    default: ``x86_64`` + ``aarch64``);
  * two inner-axis combos per ``(comp, opt)`` cell (``baseline-default``
    + ``noinline-default``, both ``san-off`` ``march-default``) so the
    streaming planner's calibration pair fires;
  * matrix variants are deterministically sampled by the same
    per-(compiler, optimization) sampler the eval_worker uses.

Validation contract (mirrors ``test_phase3_debug_cases.py``):

  * ``len(descriptors) > 0``, ``len(build_variants) > 0``;
  * every ``build_variant`` carries EXACTLY the canonical bare
    ``<sys>__<arch>__<comp>`` task_id in its cross-phase
    ``build_compilers_depends_on`` — pins
    ``plan_cell._variant_toolchain_dep`` to the RIGHT toolchain, not
    just SOME toolchain;
  * descriptor task_ids unique across the plan;
  * ``counters["source_terminal_skipped"] >= 1``;
  * ``len(violations) == 0`` in lax mode.

Marked ``@pytest.mark.nix``; excluded from the default ``pytest``
invocation. Run explicitly:

    pytest -m nix python/compiler_suit_runner/tests/test_phase3_smoke.py -v

Skips cleanly when ``nix-instantiate`` / ``nix-eval-jobs`` aren't on PATH.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from compiler_suit_runner.tests._phase3_smoke_helpers import (
    BINARY,
    STORE_HASH_RE,
    SYS_NAME,
    build_matrix_aggregate,
    build_sum_drv_from_aggregates,
    build_toolchain_aggregate,
    build_variant_lookup,
    discover_archs,
    discover_compilers_per_arch_from_leaves,
    eval_bash_drv_path,
    eval_sampled_matrix_leaves,
    eval_toolchain_leaves,
    plan_from_tree,
    query_tree,
    resolved_smoke_archs,
    toolchain_task_ids_for_combos,
)


pytestmark = pytest.mark.nix


# Sample size matches the production default the eval_worker uses for
# the smoke; deterministic seed keeps the wrapper-drv hash stable.
_SAMPLE_SIZE = 2
_SAMPLE_SEED = "phase3-smoke-42"


def _flake_root() -> Path:
    """Repo root — same ``parents[3]`` walk used by ``conftest.py``."""
    return Path(__file__).resolve().parents[3]


def _skip_unless_nix_available() -> None:
    """Skip cleanly when the required nix binaries aren't on PATH."""
    for tool in ("nix-instantiate", "nix-store", "nix", "nix-eval-jobs"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not in PATH")
    if not (_flake_root() / "flake.nix").is_file():
        pytest.skip(f"no flake.nix at expected root {_flake_root()}")


def _canonical_toolchain_task_id_for(descriptor) -> str:
    """Derive the bare ``<sys>__<arch>__<comp>`` build_compilers task_id
    from the descriptor's variant drv basename via ``parse_variant_path``.

    Phase-A per-variant resolver uses the same composition; mirroring
    it here means a regression that wires "a" toolchain instead of
    "the right" toolchain fails this assertion.
    """
    from template_graph.tree_walker import parse_variant_path  # noqa: PLC0415

    drv = descriptor.payload.get("drv", "")
    assert drv, (
        f"build_variant descriptor {descriptor.task_id!r} has empty 'drv' "
        f"payload field; cannot derive canonical toolchain task id"
    )
    basename = STORE_HASH_RE.sub("", drv)
    binary, arch_v, comp, _opt = parse_variant_path(basename)
    assert binary == BINARY, (
        f"descriptor drv {basename!r} parsed binary={binary!r} != {BINARY!r}"
    )
    return f"{SYS_NAME}__{arch_v}__{comp}"


def _assert_descriptors_contract(
    descriptors, streaming_result, variant_lookup,
) -> None:
    """Invariants the live matrix is expected to satisfy."""
    from compiler_suit_runner.workers.dependency_graph_worker.counters import (  # noqa: PLC0415
        compute_dependency_graph_counters,
    )
    assert len(descriptors) > 0, "no descriptors emitted"

    build_variant_descs = [
        d for d in descriptors if d.kind == "build_variant"
    ]
    assert len(build_variant_descs) > 0, (
        "no build_variant descriptors — variant_lookup never matched"
    )
    assert len(build_variant_descs) == len(variant_lookup), (
        f"build_variant_count {len(build_variant_descs)} != "
        f"len(variant_lookup) {len(variant_lookup)}"
    )

    task_ids = [d.task_id for d in descriptors]
    assert len(task_ids) == len(set(task_ids)), (
        "duplicate task_ids in plan: "
        + repr([t for t in task_ids if task_ids.count(t) > 1][:5])
    )

    counts: dict[str, int] = {}
    for d in descriptors:
        for dep in (d.depends_on or ()):
            counts[dep] = counts.get(dep, 0) + 1
    variant_task_ids = {d.task_id for d in build_variant_descs}
    # Toolchain deps are cross-phase (build_compilers_depends_on), so
    # they never appear as phase-4 descriptors or depends_on edges.
    violators = sorted(
        d.task_id for d in descriptors
        if d.task_id not in variant_task_ids
        and counts.get(d.task_id, 0) < 2
    )
    assert not violators, (
        f"common-dep tasks with < 2 dependents (planner output "
        f"bug -- a one-consumer dep should have been inlined): "
        f"{violators}"
    )

    wrong_tc: list[tuple[str, str, tuple[str, ...]]] = []
    for d in build_variant_descs:
        expected_id = _canonical_toolchain_task_id_for(d)
        if expected_id not in d.build_compilers_depends_on:
            wrong_tc.append(
                (d.task_id, expected_id, d.build_compilers_depends_on)
            )
    assert not wrong_tc, (
        f"{len(wrong_tc)} build_variant descriptor(s) missing the "
        f"canonical toolchain task id; first 3: "
        + repr(wrong_tc[:3])
    )

    counters = compute_dependency_graph_counters(
        streaming_result=streaming_result,
        descriptors=descriptors,
        binaries=[BINARY],
    )
    assert counters["source_terminal_skipped"] >= 1, (
        "expected at least one source_terminal_skipped — real trees "
        "always carry tarball / patch deps"
    )

    violations = list(streaming_result.get("violations", []) or [])
    assert len(violations) == 0, (
        f"streaming planner recorded {len(violations)} violation(s) "
        f"in lax mode; first 3: {violations[:3]}"
    )


def test_phase3_smoke_live_matrix():
    """End-to-end probe: bulk-eval toolchain + matrix leaves, build the
    aggregate wrapper drvs the production code path does, glue them with
    ``make_sum_drv_from_paths``, then run streaming planner +
    ``plan_phase4_for_binary``. Stage helpers narrow failures."""
    _skip_unless_nix_available()
    root = _flake_root()

    discovered_archs = discover_archs(root=root)
    smoke_archs = resolved_smoke_archs(discovered_archs)
    assert smoke_archs, (
        f"no smoke archs resolved from "
        f"ASM_PHASE3_SMOKE_ARCHS={os.environ.get('ASM_PHASE3_SMOKE_ARCHS')!r}; "
        f"discovered={discovered_archs}"
    )

    # Phase 1 mirror: bulk-eval toolchain leaves, build the single
    # ``toolchains`` aggregate wrapper drv.
    toolchain_leaves = eval_toolchain_leaves(root=root, archs=smoke_archs)
    assert toolchain_leaves, "no toolchain leaves discovered"
    compilers_per_arch = discover_compilers_per_arch_from_leaves(
        root=root, archs=smoke_archs,
    )
    assert any(compilers_per_arch.values()), (
        "no (arch, compiler) combos discovered from _crossToolchainMap"
    )
    toolchain_agg = build_toolchain_aggregate(
        toolchain_leaves, sys_name=SYS_NAME,
    )
    assert toolchain_agg.endswith(".drv"), toolchain_agg

    # Phase 2 mirror: bulk-eval the sampled matrix leaves, build the
    # ``matrix-<binary>`` aggregate wrapper drv (toolchain + leaves).
    matrix_leaves = eval_sampled_matrix_leaves(
        root=root, binary=BINARY,
        smoke_archs=smoke_archs,
        compilers_per_arch=compilers_per_arch,
        sample_size=_SAMPLE_SIZE,
        sample_seed=_SAMPLE_SEED,
    )
    assert matrix_leaves, (
        "no matrix leaves discovered — sampler emptied everything?"
    )
    matrix_agg = build_matrix_aggregate(
        toolchain_agg, matrix_leaves, binary=BINARY, sys_name=SYS_NAME,
    )
    assert matrix_agg.endswith(".drv"), matrix_agg

    # Phase 3 mirror: assemble the sum-root via the post-aggregate
    # entry point and walk the resulting tree.
    bash_path = eval_bash_drv_path(root)
    sum_drv = build_sum_drv_from_aggregates(
        toolchain_agg, {BINARY: matrix_agg},
        bash_path=bash_path, sys_name=SYS_NAME,
    )
    assert sum_drv.endswith(".drv"), sum_drv
    tree_text = query_tree(sum_drv)
    assert tree_text, "empty tree text from nix-store --query --tree"

    variant_lookup = build_variant_lookup(tree_text)
    assert variant_lookup, (
        "variant_lookup empty — no matrix-depth2 entries parsed from tree"
    )
    toolchain_task_ids = toolchain_task_ids_for_combos(compilers_per_arch)
    assert toolchain_task_ids, "toolchain_task_ids empty"

    descriptors, streaming_result = plan_from_tree(
        tree_text,
        variant_lookup=variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    _assert_descriptors_contract(descriptors, streaming_result, variant_lookup)

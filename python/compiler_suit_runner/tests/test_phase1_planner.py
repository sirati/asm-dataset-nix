"""Unit tests for :mod:`compiler_suit_runner.phase1_planner`.

The drv-graph reader is dependency-injected, so no ``nix`` binary is
ever invoked. Each test wires a stub reader that returns a synthetic
``inputDrvs`` set per drv path. ``spawn_tasks`` is a
:class:`unittest.mock.MagicMock` so call-count / payload assertions
are easy.
"""

from __future__ import annotations

import json
import pathlib
from typing import Callable
from unittest import mock

import pytest

from compiler_suit_runner.manifest_gen import (
    common_dep_task_id,
    toolchain_task_id,
)
from compiler_suit_runner.phase1_planner import (
    Phase1PlannerError,
    plan_phase1,
    read_phase0_manifests,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SYS = "x86_64-linux"


def _variant_spec(
    *,
    label: str,
    drv: str,
    pkg: str = "hello",
    arch: str = "x86_64",
    compiler_id: str = "gcc15",
    tier: int = 1,
) -> dict:
    """Minimal VariantSpec-shaped dict that satisfies make_variant_header."""
    return {
        "label": label,
        "drv": drv,
        "variant_dir": f"{compiler_id}_{arch}_O0_abc",
        "metadata_name": f"{compiler_id}_{arch}_O0_abc.json",
        "compiler_id": compiler_id,
        "compiler_family": "gcc",
        "compiler_version": "15",
        "optimization": "O0",
        "flag_set": "",
        "hardening": "",
        "sanitizer": "",
        "march": "",
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


def _make_reader(
    graph: dict[str, set[str]]
) -> tuple[Callable[[str], set[str]], list[str]]:
    """Build a stub reader returning ``graph[drv]`` and recording each call."""
    calls: list[str] = []

    def reader(drv_path: str) -> set[str]:
        calls.append(drv_path)
        return set(graph.get(drv_path, set()))

    return reader, calls


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_two_variants_share_one_common_dep() -> None:
    """3 variants, 2 share one common dep, 1 has a unique dep.

    Expect: exactly 1 phase2_common_dep task + 3 phase3_variant tasks.
    Each variant's task_depends_on references the right dep + toolchain.
    """
    v1_drv = "/nix/store/00000000000000000000000000000001-hello-v1.drv"
    v2_drv = "/nix/store/00000000000000000000000000000002-hello-v2.drv"
    v3_drv = "/nix/store/00000000000000000000000000000003-hello-v3.drv"
    shared_dep = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-glibc.drv"
    unique_dep = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-libpng.drv"
    toolchain_drv = "/nix/store/cccccccccccccccccccccccccccccccc-gcc15.drv"

    # v1 + v2 share ``shared_dep`` + ``toolchain_drv`` (refcount=2 each
    # for the toolchain, refcount=2 for shared_dep). v3 has only the
    # toolchain + a unique_dep (refcount=1 for unique_dep => NOT a
    # common dep; refcount=3 for toolchain but it's pre-registered as
    # a toolchain task, not a common dep).
    graph: dict[str, set[str]] = {
        v1_drv: {shared_dep, toolchain_drv},
        v2_drv: {shared_dep, toolchain_drv},
        v3_drv: {unique_dep, toolchain_drv},
        shared_dep: set(),
        unique_dep: set(),
        toolchain_drv: set(),
    }
    reader, _calls = _make_reader(graph)

    manifests = {
        "hello": {
            "binary": "hello",
            "variants": [
                _variant_spec(label="hello-v1", drv=v1_drv),
                _variant_spec(label="hello-v2", drv=v2_drv),
                _variant_spec(label="hello-v3", drv=v3_drv),
            ],
        }
    }
    tc_id = toolchain_task_id(_SYS, "x86_64", "gcc15")
    toolchain_task_ids = {toolchain_drv: tc_id}
    spawn = mock.MagicMock()

    summary = plan_phase1(
        manifests,
        toolchain_task_ids,
        spawn,
        sys_name=_SYS,
        reader=reader,
    )

    # Toolchain refcount=3 but it's in toolchain_task_ids so it must
    # NOT be emitted as a common_dep — verify by inspecting the split.
    spawn.assert_called_once()
    (headers,), _kwargs = spawn.call_args
    common_deps = [h for h in headers if h.item_class == "phase2_common_dep"]
    variants = [h for h in headers if h.item_class == "phase3_variant"]
    assert len(common_deps) == 1, [h.payload for h in common_deps]
    assert common_deps[0].payload["drv"] == shared_dep
    assert len(variants) == 3
    # Per-variant deps:
    by_drv = {h.payload["drv"]: h for h in variants}
    shared_id = common_dep_task_id(shared_dep)
    assert set(by_drv[v1_drv].task_depends_on) == {shared_id, tc_id}
    assert set(by_drv[v2_drv].task_depends_on) == {shared_id, tc_id}
    # v3 has only its toolchain — unique_dep is refcount=1, so it
    # is NOT promoted to a phase2_common_dep and there's nothing
    # else for v3 to depend on. (The build itself substitutes it.)
    assert set(by_drv[v3_drv].task_depends_on) == {tc_id}

    assert summary == {
        "common_deps": 1,
        "variants": 3,
        "drv_graph_size": len(graph),
    }


# ---------------------------------------------------------------------------
# Refcount threshold
# ---------------------------------------------------------------------------


def test_refcount_threshold_is_two_not_ten() -> None:
    """A drv shared by exactly 2 variants is a common dep.

    Regression-pins the threshold against the legacy >=10 heuristic
    in ``partition_local.py``. Plan: refcount >= 2 is the canonical
    threshold.
    """
    v1_drv = "/nix/store/d1111111111111111111111111111111-a.drv"
    v2_drv = "/nix/store/d2222222222222222222222222222222-b.drv"
    shared = "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-libz.drv"
    graph = {
        v1_drv: {shared},
        v2_drv: {shared},
        shared: set(),
    }
    reader, _calls = _make_reader(graph)
    manifests = {
        "alpha": {
            "binary": "alpha",
            "variants": [_variant_spec(label="a", drv=v1_drv)],
        },
        "beta": {
            "binary": "beta",
            "variants": [_variant_spec(label="b", drv=v2_drv)],
        },
    }
    spawn = mock.MagicMock()
    plan_phase1(manifests, {}, spawn, sys_name=_SYS, reader=reader)
    (headers,), _ = spawn.call_args
    common = [h for h in headers if h.item_class == "phase2_common_dep"]
    assert len(common) == 1
    assert common[0].payload["drv"] == shared


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_cycle_in_drv_graph_raises() -> None:
    """Defensive cycle detection: A->B->A raises Phase1PlannerError."""
    a = "/nix/store/aa111111111111111111111111111111-a.drv"
    b = "/nix/store/bb222222222222222222222222222222-b.drv"
    graph = {a: {b}, b: {a}}
    reader, _calls = _make_reader(graph)
    manifests = {
        "loop": {
            "binary": "loop",
            "variants": [_variant_spec(label="loop", drv=a)],
        }
    }
    with pytest.raises(Phase1PlannerError, match="cycle"):
        plan_phase1(manifests, {}, mock.MagicMock(), sys_name=_SYS, reader=reader)


# ---------------------------------------------------------------------------
# Memo
# ---------------------------------------------------------------------------


def test_memo_reads_each_drv_once_across_variants() -> None:
    """Same drv referenced from multiple variants is read once.

    Two binaries each emit a variant with the same root drv path
    (e.g. resume scenario where the same Phase 0 manifest is loaded
    twice into the union). The reader stub records every call; we
    assert call-count == number of DISTINCT drvs in the graph.
    """
    v_drv = "/nix/store/ee111111111111111111111111111111-x.drv"
    dep = "/nix/store/ff222222222222222222222222222222-y.drv"
    graph = {v_drv: {dep}, dep: set()}
    reader, calls = _make_reader(graph)
    manifests = {
        "x": {
            "binary": "x",
            "variants": [
                _variant_spec(label="x1", drv=v_drv),
                _variant_spec(label="x2", drv=v_drv),
            ],
        },
        "y": {
            "binary": "y",
            "variants": [_variant_spec(label="y1", drv=v_drv)],
        },
    }
    plan_phase1(manifests, {}, mock.MagicMock(), sys_name=_SYS, reader=reader)
    # 2 distinct drvs (v_drv + dep) -> 2 reads, NOT 3*1 + 2 = 5.
    assert sorted(calls) == sorted([v_drv, dep])
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# spawn_tasks call shape
# ---------------------------------------------------------------------------


def test_spawn_tasks_called_exactly_once_with_full_list() -> None:
    """One call, one positional list-of-headers arg."""
    v_drv = "/nix/store/cc111111111111111111111111111111-c.drv"
    reader, _calls = _make_reader({v_drv: set()})
    manifests = {
        "c": {"binary": "c", "variants": [_variant_spec(label="c", drv=v_drv)]}
    }
    spawn = mock.MagicMock()
    plan_phase1(manifests, {}, spawn, sys_name=_SYS, reader=reader)
    assert spawn.call_count == 1
    (positional,), kwargs = spawn.call_args
    assert isinstance(positional, list)
    assert kwargs == {}
    # In this single-variant + zero-shared-deps case the headers list
    # is exactly 1 variant header (no common deps to emit).
    assert len(positional) == 1
    assert positional[0].item_class == "phase3_variant"


# ---------------------------------------------------------------------------
# read_phase0_manifests
# ---------------------------------------------------------------------------


def test_read_phase0_manifests_glob_walk(tmp_path: pathlib.Path) -> None:
    """Glob-walks out/<binary>/_phase0/manifest.json under ``out_dir``."""
    out_dir = tmp_path / "out"
    for binary in ("hello", "yes"):
        (out_dir / binary / "_phase0").mkdir(parents=True)
        (out_dir / binary / "_phase0" / "manifest.json").write_text(
            json.dumps(
                {
                    "binary": binary,
                    "produced_at": "2026-05-15T00:00:00Z",
                    "variants": [{"label": f"{binary}-1", "drv": f"/nix/store/x-{binary}.drv"}],
                }
            ),
            encoding="utf-8",
        )
    # Garbage files outside the expected layout must be ignored.
    (out_dir / "stray.json").write_text("not a manifest", encoding="utf-8")
    (out_dir / "no" / "_phase0").mkdir(parents=True)
    (out_dir / "no" / "_phase0" / "manifest.json").write_text(
        "not json", encoding="utf-8"
    )

    manifests = read_phase0_manifests(out_dir)
    assert set(manifests.keys()) == {"hello", "yes"}
    assert manifests["hello"]["variants"][0]["drv"] == "/nix/store/x-hello.drv"


def test_read_phase0_manifests_missing_dir(tmp_path: pathlib.Path) -> None:
    """Non-existent ``out_dir`` returns ``{}`` (resume convenience)."""
    assert read_phase0_manifests(tmp_path / "does-not-exist") == {}


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_phase0_manifests_yields_empty_graph() -> None:
    """Zero binaries -> spawn_tasks called once with an empty list."""
    spawn = mock.MagicMock()
    reader, calls = _make_reader({})
    summary = plan_phase1({}, {}, spawn, sys_name=_SYS, reader=reader)
    spawn.assert_called_once_with([])
    assert calls == []
    assert summary == {"common_deps": 0, "variants": 0, "drv_graph_size": 0}


# ---------------------------------------------------------------------------
# Default reader delegation
# ---------------------------------------------------------------------------


def test_default_reader_delegates_to_drv_graph() -> None:
    """When ``reader`` is None, the default reader calls
    :func:`drv_graph.read_input_drvs`."""
    v_drv = "/nix/store/dd111111111111111111111111111111-d.drv"
    with mock.patch(
        "compiler_suit_runner.phase1_planner.drv_graph.read_input_drvs",
        return_value=set(),
    ) as m:
        plan_phase1(
            {"d": {"binary": "d", "variants": [_variant_spec(label="d", drv=v_drv)]}},
            {},
            mock.MagicMock(),
            sys_name=_SYS,
        )
        m.assert_called_once_with(v_drv)

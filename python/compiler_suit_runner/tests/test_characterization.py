"""Characterization tests: freeze what SuitTask hands the framework.

Later refactoring steps move task-info building, gate emission, worker
argv and the streamed-spawn handler out of ``suit_task.py``. These tests
pin the current output so a move that changes behaviour is noticed. They
compare against ``characterization/suit_task.json``.

When a change is intended, regenerate the snapshot and review its diff::

    CSR_UPDATE_SNAPSHOTS=1 pytest compiler_suit_runner/tests/test_characterization.py
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import pytest

from compiler_suit_runner.dependency_graph_planner import Phase4Descriptor
from compiler_suit_runner.manifest_gen import (
    make_build_common_dep_header,
    make_build_compilers_header,
    make_build_variant_header,
    write_manifest,
)
from compiler_suit_runner.streamed_spawn import SPAWN_TOPIC, SpawnBatchEncoder
from compiler_suit_runner.suit_task import SuitTask
from compiler_suit_runner.tests.fakes import FakePrimaryHandle, make_suit_config

pytest.importorskip("dynamic_runner.task_protocol")

_SNAPSHOT = pathlib.Path(__file__).parent / "characterization" / "suit_task.json"
_SYS = "x86_64-linux"
_TC_AGG = "/nix/store/0000000000000000000000000000000a-toolchains.drv"
_TC_OUT = {
    "aarch64/gcc15": "/nix/store/1111111111111111111111111111111b-stdenv-linux",
    "aarch64/clang19": "/nix/store/2222222222222222222222222222222c-stdenv-linux",
}
_PER_BINARY = {
    "hello": {
        "archs": ["x86_64", "aarch64"], "variant_sample": 4,
        "variant_seed": "s-hello", "tier": 1, "toolchain_aggregate_drv": _TC_AGG,
    },
    "c-ares": {
        "archs": ["aarch64"], "variant_sample": 2,
        "variant_seed": "s-c-ares", "tier": 1, "toolchain_aggregate_drv": _TC_AGG,
    },
}


def _normalize(value: Any, tmp: str) -> Any:
    if isinstance(value, str):
        return value.replace(tmp, "<tmp>")
    if isinstance(value, dict):
        return {k: _normalize(v, tmp) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, tmp) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return _normalize(value.to_dict(), tmp)
    return _normalize(
        {k: getattr(value, k) for k in ("task_id", "phase_id", "inherit_outputs")
         if hasattr(value, k)},
        tmp,
    )


def _items(task: SuitTask, tmp: pathlib.Path) -> list[dict]:
    return [_normalize(i, str(tmp)) for i in task.discover_items()]


def _scenario_streamed(tmp: pathlib.Path) -> SuitTask:
    config = make_suit_config(
        tmp,
        matrix_eval_out_dir=tmp / "matrix_eval",
        toolchain_outpaths_map=_TC_OUT,
        build_deps_local=True,
        per_binary_metadata=_PER_BINARY,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    return SuitTask(config)


def _scenario_manifests(tmp: pathlib.Path) -> SuitTask:
    config = make_suit_config(tmp)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(
        config.manifest_dir,
        make_build_compilers_header(_SYS, "aarch64", "gcc15"),
    )
    write_manifest(
        config.manifest_dir,
        make_build_common_dep_header(
            "/nix/store/3333333333333333333333333333333d-glibc.drv", "glibc",
        ),
    )
    label = "hello-aarch64-gcc15-O2"
    write_manifest(
        config.manifest_dir,
        make_build_variant_header(
            {
                "label": label, "drv": f"/nix/store/{label}.drv",
                "variant_dir": label, "metadata_name": f"{label}.json",
                "compiler_id": "gcc15", "compiler_family": "gcc",
                "compiler_version": "15.2.0", "optimization": "O2",
                "flag_set": "baseline", "hardening": "default",
                "sanitizer": "san-off", "march": "march-default", "tier": 1,
                "pkg": "hello", "arch": "aarch64",
            },
            _SYS,
            toolchain_task_id=f"{_SYS}__aarch64__gcc15",
        ),
    )
    return SuitTask(config)


def _worker_argv(tmp: pathlib.Path) -> dict:
    out: dict = {}
    for no_subst in (False, True):
        task = _scenario_streamed(tmp)
        task.config = make_suit_config(
            tmp,
            matrix_eval_out_dir=tmp / "matrix_eval",
            toolchain_outpaths_map=_TC_OUT,
            build_deps_local=True,
            per_binary_metadata=_PER_BINARY,
            no_substituters=no_subst,
        )
        type_ids = sorted(
            {t.type_id for phase in task.get_phases() for t in phase.types}
        )
        out[f"no_substituters={no_subst}"] = {
            type_id: _normalize(
                task.build_worker_command_args(
                    type_id, None, tmp, tmp / "out", False,
                ),
                str(tmp),
            )
            for type_id in type_ids
        }
    return out


def _descriptor(kind: str, ident: str, deps=()) -> Phase4Descriptor:
    if kind == "build_common_dep":
        return Phase4Descriptor(
            kind=kind, task_id=f"build_common_dep__{ident}.drv",
            name=f"common_dep__{ident}",
            payload={"drv": f"/nix/store/{ident}.drv", "label": ident},
            depends_on=tuple(deps),
        )
    label = f"hello-aarch64-gcc15-{ident}"
    return Phase4Descriptor(
        kind="build_variant",
        task_id=f"build_variant__{_SYS}__hello__{label}",
        name=label,
        payload={
            "sys": _SYS, "pkg": "hello", "arch": "aarch64", "label": label,
            "drv": f"/nix/store/v-{label}.drv", "variant_dir": label,
            "metadata_name": f"{label}.json", "compiler_id": "gcc15",
        },
        depends_on=tuple(deps),
        build_compilers_depends_on=(f"{_SYS}__aarch64__gcc15",),
    )


def _streamed_spawn_order(tmp: pathlib.Path) -> list[dict]:
    task = _scenario_streamed(tmp)
    handle = FakePrimaryHandle()
    task._primary_handle = handle
    enc = SpawnBatchEncoder()
    glibc = "4444444444444444444444444444444e-glibc"
    zlib = "5555555555555555555555555555555f-zlib"
    for d in (
        _descriptor("build_variant", "O0", deps=(f"build_common_dep__{glibc}.drv",)),
        _descriptor("build_common_dep", glibc),
        _descriptor("build_variant", "O2", deps=(f"build_common_dep__{zlib}.drv",)),
        _descriptor("build_common_dep", zlib, deps=(f"build_common_dep__{glibc}.drv",)),
    ):
        assert enc.add(d) is None
    task.custom_message_handler("secondary-1", SPAWN_TOPIC, enc.flush(), True, handle)
    assert len(handle.calls) == 1
    return [_normalize(i, str(tmp)) for i in handle.calls[0]]


def _current(tmp_path: pathlib.Path) -> dict:
    streamed = _items(_scenario_streamed(tmp_path / "a"), tmp_path / "a")
    return {
        "discover_items": {
            "streamed": streamed,
            "manifests": _items(_scenario_manifests(tmp_path / "b"), tmp_path / "b"),
        },
        "gate_ids": sorted(i["task_id"] for i in streamed if i["is_secondary_affine"]),
        "worker_argv": _worker_argv(tmp_path / "c"),
        "streamed_spawn_order": _streamed_spawn_order(tmp_path / "d"),
    }


def test_suit_task_output_matches_snapshot(tmp_path: pathlib.Path) -> None:
    current = _current(tmp_path)
    if os.environ.get("CSR_UPDATE_SNAPSHOTS") == "1":
        _SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        pytest.skip(f"snapshot rewritten: {_SNAPSHOT}")
    expected = json.loads(_SNAPSHOT.read_text())
    for section in expected:
        assert current[section] == expected[section], section
    assert set(current) == set(expected)

"""Unit tests for :mod:`compiler_suit_runner.suit_task`.

Today's tests cover the matrix_eval → build quiesce watcher
(``_MatrixEvalQuiesceWatcher``) and its wiring into ``on_run_start``,
plus the peer-lifecycle listener and the PrimaryHandle wrappers.

The watcher's ``_fire`` runs the dependency_graph_worker as a
subprocess and feeds its output into ``primary_handle.spawn_tasks``;
the tests below substitute the subprocess + (optionally) the spawn
handle so the bookkeeping side is testable hermetically.
"""

from __future__ import annotations

import dataclasses as _dataclasses
import json
import logging
import pathlib
import subprocess
from unittest import mock

import pytest

from compiler_suit_runner.dependency_graph_planner import (
    Phase4Descriptor,
)
from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    matrix_eval_task_id,
    build_compilers_task_id,
    write_manifest,
)
from compiler_suit_runner.suit_task import (
    SuitTask,
    SuitTaskConfig,
    _MatrixEvalQuiesceWatcher,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_SYS = "x86_64-linux"


def _make_config(tmp_path: pathlib.Path) -> SuitTaskConfig:
    return SuitTaskConfig(
        flake_ref=".",
        sys_name=_SYS,
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "manifests",
        dataset_dir=tmp_path / "dataset",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="host",
    )


def _matrix_eval_header(binary: str) -> ManifestHeader:
    return ManifestHeader(
        item_class="matrix_eval",
        name=f"matrix_eval__{binary}",
        size=0,
        payload={
            "binary": binary,
            "sys": _SYS,
            "archs": ["x86_64"],
            "suffixes": ["O0"],
            "attr": f"dataset.{_SYS}.{binary}",
        },
        task_id=matrix_eval_task_id(binary),
        task_depends_on=(),
    )


def _toolchain_header(
    arch: str, compiler_label: str, drv: str,
) -> ManifestHeader:
    return ManifestHeader(
        item_class="toolchain_validate",
        name=f"toolchain_validate__{arch}__{compiler_label}",
        size=0,
        payload={
            "sys": _SYS,
            "arch": arch,
            "compiler_label": compiler_label,
            "attr": (
                f"_crossToolchainMap.{_SYS}.{arch}.{compiler_label}"
            ),
            "drv": drv,
            "validate_only": True,
        },
        task_id=build_compilers_task_id(_SYS, arch, compiler_label),
    )


def _common_dep_header(label: str, drv: str) -> ManifestHeader:
    return ManifestHeader(
        item_class="build_common_dep",
        name=f"common_dep__{label}",
        size=0,
        payload={
            "drv": drv,
            "label": label,
            "attr": drv,
        },
        task_id=f"build_common_dep__{pathlib.Path(drv).name}",
    )


def _variant_header(binary: str, label: str, compiler_id: str = "gcc15"):
    return ManifestHeader(
        item_class="build_variant",
        name=label,
        size=0,
        payload={
            "sys": _SYS,
            "pkg": binary,
            "arch": "x86_64",
            "label": label,
            "drv": f"/nix/store/v-{label}.drv",
            "variant_dir": label,
            "metadata_name": f"{label}.json",
            "compiler_id": compiler_id,
            "tier": 1,
            "attr": f"dataset.{_SYS}.{binary}.x86_64.{label}",
        },
        task_id=f"build_variant__{_SYS}__{binary}__{label}",
        task_depends_on=(build_compilers_task_id(_SYS, "x86_64", compiler_id),),
    )


# A minimal subprocess.CompletedProcess stand-in for the watcher's
# ``run_subprocess`` injection point.
def _fake_completed(rc: int, stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=b"", stderr=stderr,
    )


# ---------------------------------------------------------------------------
# _MatrixEvalQuiesceWatcher bookkeeping
# ---------------------------------------------------------------------------


def test_watcher_initialises_with_expected_set(
    tmp_path: pathlib.Path,
) -> None:
    expected = {matrix_eval_task_id("hello"),
                matrix_eval_task_id("busybox")}
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids=expected,
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    assert w.expected == frozenset(expected)
    assert w.completed == frozenset()
    assert w.fired is False


def test_watcher_noops_for_non_matrix_eval_task(
    tmp_path: pathlib.Path,
) -> None:
    """A toolchain (or any) task_id that is not in ``expected`` is
    ignored. The watcher coexists with other listeners on the same hook
    surface (K=3 replication, etc.) so it must not raise."""
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    w.on_task_completed("toolchain__gcc15__x86_64")
    w.on_task_completed("")  # empty id — defensive guard
    w.on_task_completed("merge__singleton")
    assert w.completed == frozenset()
    assert w.fired is False


def test_watcher_records_matrix_eval_completion(
    tmp_path: pathlib.Path,
) -> None:
    """A matching matrix_eval task moves into ``completed`` but doesn't
    fire until the set is full."""
    hello = matrix_eval_task_id("hello")
    busybox = matrix_eval_task_id("busybox")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello, busybox},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
    )
    w.on_task_completed(hello)
    assert w.completed == frozenset({hello})
    assert w.fired is False


def test_watcher_fires_when_complete(
    tmp_path: pathlib.Path,
) -> None:
    """When the completed set covers the expected set, ``fired`` flips
    to True. The ``_fire`` flow runs the dependency_graph subprocess
    (overridden here to a no-op) and writes the artefact dump alongside.
    """
    hello = matrix_eval_task_id("hello")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    toolchain_drv = "/nix/store/c-gcc15.drv"
    toolchain_id = build_compilers_task_id(_SYS, "x86_64", "gcc15")

    # Pre-seed an empty dependency_graph.json so the read step is a no-op.
    (out_dir / "_dependency_graph.json").write_text(
        json.dumps({"phase4_descriptors": []}),
        encoding="utf-8",
    )

    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={toolchain_drv: toolchain_id},
        sys_name=_SYS,
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    w.on_task_completed(hello)
    assert w.fired is True


def test_watcher_is_idempotent_on_duplicate_completion(
    tmp_path: pathlib.Path,
) -> None:
    """The same task_id arriving twice does not flip ``fired`` twice
    and does not double-count toward expected."""
    hello = matrix_eval_task_id("hello")
    busybox = matrix_eval_task_id("busybox")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "_dependency_graph.json").write_text(
        json.dumps({"phase4_descriptors": []}), encoding="utf-8",
    )
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello, busybox},
        out_dir=out_dir,
        toolchain_task_ids={},
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    w.on_task_completed(hello)
    w.on_task_completed(hello)  # duplicate
    assert w.completed == frozenset({hello})
    assert w.fired is False
    w.on_task_completed(busybox)
    assert w.fired is True
    # Another stray completion after firing is a no-op.
    w.on_task_completed(busybox)
    w.on_task_completed(hello)
    assert w.fired is True


# ---------------------------------------------------------------------------
# _import_matrix_eval_archives: walks BOTH matrix_eval + build_compilers
# ---------------------------------------------------------------------------


def test_import_matrix_eval_archives_walks_both_dirs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watcher's archive importer walks ``_matrix_eval/`` AND its
    sibling ``_build_compilers/`` directory.

    Matrix-eval archives carry per-binary kept-variant closures; the
    build_compilers archives carry each toolchain's outputs. The primary
    needs BOTH imported to walk the full sum-drv graph at phase3.
    """
    out_root = tmp_path / "out"
    matrix_eval_dir = out_root / "_matrix_eval"
    build_compilers_dir = out_root / "_build_compilers"
    matrix_eval_dir.mkdir(parents=True)
    build_compilers_dir.mkdir(parents=True)

    me_archive = matrix_eval_dir / "hello.nix-archive"
    me_archive.write_bytes(b"NIX_EXPORT:hello-closure")
    bc_archive_gcc = build_compilers_dir / "x86_64__gcc15.nix-archive"
    bc_archive_gcc.write_bytes(b"NIX_EXPORT:gcc15-closure")
    bc_archive_clang = build_compilers_dir / "x86_64__clang19.nix-archive"
    bc_archive_clang.write_bytes(b"NIX_EXPORT:clang19-closure")

    # Mix in non-archive files in both dirs — they MUST be ignored.
    (matrix_eval_dir / "hello.nix-archive.json").write_text(
        json.dumps({"variant_drvs": []}), encoding="utf-8",
    )
    (build_compilers_dir / "README.md").write_text("noise", encoding="utf-8")

    imported: list[pathlib.Path] = []

    def fake_subprocess_run(argv, *, stdin=None, stdout=None,
                             stderr=None, check=False, **_kw):
        # The watcher passes the open file as stdin; resolve its path
        # via the fd's name attribute so we can assert on the source
        # file regardless of dir walk order.
        assert argv == ["nix-store", "--import"]
        assert stdin is not None
        path = pathlib.Path(stdin.name)
        imported.append(path)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b"",
        )

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.subprocess.run",
        fake_subprocess_run,
    )

    hello = matrix_eval_task_id("hello")
    (matrix_eval_dir / "_dependency_graph.json").write_text(
        json.dumps({"phase4_descriptors": []}), encoding="utf-8",
    )
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=matrix_eval_dir,
        toolchain_task_ids={},
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    w._import_matrix_eval_archives()

    # All three archives imported; non-archive files skipped.
    assert sorted(p.name for p in imported) == [
        "hello.nix-archive",
        "x86_64__clang19.nix-archive",
        "x86_64__gcc15.nix-archive",
    ]


def test_import_matrix_eval_archives_handles_missing_build_compilers_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_build_compilers/`` may be absent (e.g. --build-compilers not
    passed). The watcher imports the matrix_eval archives anyway and
    does NOT log a warning about the missing sibling at WARNING level
    (it logs at DEBUG via _discover_archives_in)."""
    out_root = tmp_path / "out"
    matrix_eval_dir = out_root / "_matrix_eval"
    matrix_eval_dir.mkdir(parents=True)
    # No _build_compilers dir created — that's the scenario.

    (matrix_eval_dir / "hello.nix-archive").write_bytes(b"NIX_EXPORT:x")

    imported: list[str] = []

    def fake_subprocess_run(argv, *, stdin=None, stdout=None,
                             stderr=None, check=False, **_kw):
        imported.append(pathlib.Path(stdin.name).name)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b"",
        )

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.subprocess.run",
        fake_subprocess_run,
    )

    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=matrix_eval_dir,
        toolchain_task_ids={},
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    w._import_matrix_eval_archives()
    assert imported == ["hello.nix-archive"]


def test_import_matrix_eval_archives_logs_when_both_missing(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """When neither directory yields archives, the watcher emits a
    WARNING — operators can correlate against the empty out_dir."""
    out_root = tmp_path / "out"
    matrix_eval_dir = out_root / "_matrix_eval"
    # Neither directory exists.

    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=matrix_eval_dir,
        toolchain_task_ids={},
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    with caplog.at_level(logging.WARNING):
        w._import_matrix_eval_archives()
    assert any(
        "no archives to import" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# _fire: subprocess + spawn integration
# ---------------------------------------------------------------------------


def test_fire_invokes_dependency_graph_subprocess_and_spawns(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Happy path: _fire runs the subprocess override, reads the
    descriptor list from _dependency_graph.json, and feeds the
    translated headers to primary_handle.spawn_tasks."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Seed a non-empty descriptor list on disk; the subprocess is a
    # no-op so we control the input directly.
    descriptors_payload = {
        "phase4_descriptors": [
            {
                "kind": "build_common_dep",
                "task_id": "build_common_dep__hello__x86_64__abc-glibc",
                "name": "build_common_dep__hello__x86_64__glibc",
                "payload": {
                    "sys": _SYS,
                    "binary": "hello",
                    "arch": "x86_64",
                    "ident": "abc-glibc",
                    "node_name": "glibc",
                    "node_id": 0,
                    "attr": "abc-glibc",
                },
                "depends_on": [],
            },
            {
                "kind": "build_variant",
                "task_id": "build_variant__x86_64-linux__hello__hello-x86_64-gcc15-O2",
                "name": "build_variant__hello__hello-x86_64-gcc15-O2",
                "payload": {
                    "sys": _SYS,
                    "pkg": "hello",
                    "arch": "x86_64",
                    "label": "hello-x86_64-gcc15-O2",
                    "compiler_id": "gcc15",
                    "drv": "/nix/store/v.drv",
                    "tier": 1,
                },
                "depends_on": [
                    "build_common_dep__hello__x86_64__abc-glibc",
                ],
            },
        ]
    }
    (out_dir / "_dependency_graph.json").write_text(
        json.dumps(descriptors_payload), encoding="utf-8",
    )

    runs: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess:
        runs.append(list(argv))
        return _fake_completed(0)

    handle = mock.MagicMock()
    handle.spawn_tasks.return_value = []
    logger = logging.getLogger("test_fire_happy")
    hello = matrix_eval_task_id("hello")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=handle,
        sys_name=_SYS,
        manifest_dir=tmp_path / "manifests",
        run_subprocess=fake_run,
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
        logger=logger,
    )
    with caplog.at_level(logging.INFO, logger="test_fire_happy"):
        w.on_task_completed(hello)

    # Subprocess was invoked exactly once.
    assert runs == [["true"]]
    # spawn_tasks was called with two TaskInfos.
    handle.spawn_tasks.assert_called_once()
    args, _ = handle.spawn_tasks.call_args
    task_infos = list(args[0])
    assert len(task_infos) == 2
    assert any(
        ti.task_id == "build_common_dep__hello__x86_64__abc-glibc"
        for ti in task_infos
    )
    assert any(
        ti.task_id
        == "build_variant__x86_64-linux__hello__hello-x86_64-gcc15-O2"
        for ti in task_infos
    )


def test_fire_logs_and_degrades_on_subprocess_failure(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-zero subprocess return code is logged; spawn_tasks is
    never called."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    handle = mock.MagicMock()
    logger = logging.getLogger("test_fire_subprocess_fail")
    hello = matrix_eval_task_id("hello")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=handle,
        run_subprocess=lambda _argv: _fake_completed(
            2, stderr=b"boom"
        ),
        dependency_graph_command_override=["false"],
        bash_path="/nix/store/fake-bash",
        logger=logger,
    )
    with caplog.at_level(logging.ERROR, logger="test_fire_subprocess_fail"):
        w.on_task_completed(hello)
    assert w.fired is True
    handle.spawn_tasks.assert_not_called()
    assert any(
        "dependency_graph worker failed" in rec.message
        and "boom" in rec.message
        for rec in caplog.records
    )


def test_fire_logs_and_degrades_when_descriptors_missing(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """If the descriptor file is missing after a (supposedly) ok
    subprocess run, we log + skip spawn."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # No _dependency_graph.json file.
    handle = mock.MagicMock()
    logger = logging.getLogger("test_fire_no_descriptors")
    hello = matrix_eval_task_id("hello")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=handle,
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
        logger=logger,
    )
    with caplog.at_level(logging.ERROR, logger="test_fire_no_descriptors"):
        w.on_task_completed(hello)
    handle.spawn_tasks.assert_not_called()
    assert any(
        "cannot read" in rec.message and "_dependency_graph.json" in rec.message
        for rec in caplog.records
    )


def test_fire_argv_includes_toolchain_drv_and_task_id_mappings(
    tmp_path: pathlib.Path,
) -> None:
    """The watcher assembles ``--toolchain-drv`` /
    ``--toolchain-task-id`` flags per (drv, task_id) entry, in
    sorted-drv order for determinism."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "_dependency_graph.json").write_text(
        json.dumps({"phase4_descriptors": []}), encoding="utf-8",
    )
    runs: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess:
        runs.append(list(argv))
        return _fake_completed(0)

    hello = matrix_eval_task_id("hello")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={
            "/nix/store/aa-gcc15.drv": "toolchain__lx__x86_64__gcc15",
            "/nix/store/bb-clang20.drv": (
                "toolchain__lx__x86_64__clang20"
            ),
        },
        sys_name=_SYS,
        manifest_dir=tmp_path / "manifests",
        bash_path="/nix/store/fake-bash",
        run_subprocess=fake_run,
        # Do NOT pass dependency_graph_command_override — exercise the
        # real argv assembly.
    )
    w.on_task_completed(hello)

    assert len(runs) == 1
    argv = runs[0]
    # Hard-coded markers that must always be present.
    assert "--matrix-eval-out-dir" in argv
    assert "--bash-path" in argv
    assert "/nix/store/fake-bash" in argv
    assert "--sys-name" in argv and _SYS in argv
    # Per-toolchain markers.
    assert "/nix/store/aa-gcc15.drv" in argv
    assert "/nix/store/bb-clang20.drv" in argv
    assert "aa-gcc15=toolchain__lx__x86_64__gcc15" in argv
    assert "bb-clang20=toolchain__lx__x86_64__clang20" in argv


# ---------------------------------------------------------------------------
# _spawn_tasks dispatch path (used by tests + _fire's translation step)
# ---------------------------------------------------------------------------


def test_spawn_tasks_no_handle_writes_dependency_graph_and_logs_count(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """With ``primary_handle=None`` the spawn path falls back to
    writing ``_dependency_graph.json`` (the headers view) and emits
    an INFO log explaining the degradation."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    logger = logging.getLogger("test_spawn_no_handle")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=None,
        logger=logger,
    )
    headers = [
        _common_dep_header("glibc", "/nix/store/x-glibc.drv"),
        _variant_header("hello", "hello-x86_64-gcc15-O0"),
    ]
    with caplog.at_level(logging.INFO, logger="test_spawn_no_handle"):
        w._spawn_tasks(headers)
    graph_path = out_dir / "_dependency_graph.json"
    assert graph_path.is_file()
    parsed = json.loads(graph_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["item_class"] == "build_common_dep"
    assert parsed[1]["item_class"] == "build_variant"
    assert parsed[1]["task_depends_on"]
    # INFO log line mentions the JSON-only fallback.
    assert any(
        "primary_handle unbound" in rec.message
        for rec in caplog.records
    )


def test_spawn_tasks_no_handle_creates_missing_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """The fallback path mkdirs ``out_dir`` if absent so the planner
    can fire on a fresh shared-fs."""
    out_dir = tmp_path / "fresh-out"
    assert not out_dir.exists()
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=None,
    )
    w._spawn_tasks([])
    assert (out_dir / "_dependency_graph.json").is_file()


def test_spawn_tasks_with_handle_calls_primary_handle(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """When a primary_handle is bound, ``_spawn_tasks`` converts each
    header into a TaskInfo and calls ``primary_handle.spawn_tasks(...)``."""
    out_dir = tmp_path / "out"
    handle = mock.MagicMock()
    handle.spawn_tasks.return_value = []
    logger = logging.getLogger("test_spawn_with_handle")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=out_dir,
        toolchain_task_ids={},
        primary_handle=handle,
        logger=logger,
    )
    headers = [
        _common_dep_header("glibc", "/nix/store/x-glibc.drv"),
        _variant_header("hello", "hello-x86_64-gcc15-O0"),
    ]
    with caplog.at_level(logging.INFO, logger="test_spawn_with_handle"):
        w._spawn_tasks(headers)
    handle.spawn_tasks.assert_called_once()
    args, _ = handle.spawn_tasks.call_args
    task_infos = list(args[0])
    assert len(task_infos) == 2
    ti0 = task_infos[0]
    assert ti0.payload["item_class"] == "build_common_dep"
    ti1 = task_infos[1]
    assert ti1.payload["item_class"] == "build_variant"
    assert (out_dir / "_dependency_graph.json").is_file()
    assert any(
        "spawn_tasks dispatched 2 task(s) with 0 error(s)" in rec.message
        for rec in caplog.records
    )


def test_spawn_tasks_duplicate_task_hash_logs_warning(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``duplicate_task_hash`` error from spawn_tasks gets logged at
    WARNING — we expected a fresh hash and the framework deduped."""
    handle = mock.MagicMock()
    handle.spawn_tasks.return_value = [
        (0, {"kind": "duplicate_task_hash", "task_hash": "abc123"}),
    ]
    logger = logging.getLogger("test_spawn_dup")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
        primary_handle=handle,
        logger=logger,
    )
    headers = [_common_dep_header("glibc", "/nix/store/x-glibc.drv")]
    with caplog.at_level(logging.WARNING, logger="test_spawn_dup"):
        w._spawn_tasks(headers)
    handle.spawn_tasks.assert_called_once()
    assert any(
        "duplicate task_hash" in rec.message
        and "common_dep__glibc" in rec.message
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_spawn_tasks_unknown_dependency_logs_warning(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``unknown_dependency`` error logs WARN with task_hash +
    dep_task_id; that's a real planner bug."""
    handle = mock.MagicMock()
    handle.spawn_tasks.return_value = [
        (
            0,
            {
                "kind": "unknown_dependency",
                "task_hash": "deadbeef",
                "dep_task_id": "missing-dep-id",
            },
        ),
    ]
    logger = logging.getLogger("test_spawn_unknown_dep")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
        primary_handle=handle,
        logger=logger,
    )
    headers = [_variant_header("hello", "hello-x86_64-gcc15-O0")]
    with caplog.at_level(logging.WARNING, logger="test_spawn_unknown_dep"):
        w._spawn_tasks(headers)
    assert any(
        "unknown dependency" in rec.message
        and "deadbeef" in rec.message
        and "missing-dep-id" in rec.message
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    )


def test_spawn_tasks_swallows_handle_exception(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """primary_handle.spawn_tasks raising must not propagate; we log
    + degrade (build phase stalls, which is operator-visible)."""
    handle = mock.MagicMock()
    handle.spawn_tasks.side_effect = RuntimeError("boom")
    logger = logging.getLogger("test_spawn_handle_raises")
    w = _MatrixEvalQuiesceWatcher(
        expected_task_ids={matrix_eval_task_id("hello")},
        out_dir=tmp_path / "out",
        toolchain_task_ids={},
        primary_handle=handle,
        logger=logger,
    )
    headers = [_common_dep_header("glibc", "/nix/store/x-glibc.drv")]
    with caplog.at_level(logging.ERROR, logger="test_spawn_handle_raises"):
        w._spawn_tasks(headers)  # must not raise
    assert any(
        "primary_handle.spawn_tasks" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# SuitTask._build_matrix_eval_watcher integration
# ---------------------------------------------------------------------------


def _seed_manifest_dir(
    config: SuitTaskConfig,
    headers: list[ManifestHeader],
) -> None:
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    for header in headers:
        write_manifest(config.manifest_dir, header)


def test_build_matrix_eval_watcher_returns_none_when_no_matrix_eval(
    tmp_path: pathlib.Path,
) -> None:
    """Without any matrix_eval manifests the builder returns None."""
    config = _make_config(tmp_path)
    _seed_manifest_dir(config, [
        _toolchain_header("x86_64", "gcc15", "/nix/store/x-gcc15.drv"),
    ])
    task = SuitTask(config)
    assert task._build_matrix_eval_watcher(
        output_dir=tmp_path / "out",
    ) is None


def test_build_matrix_eval_watcher_returns_none_when_manifest_dir_missing(
    tmp_path: pathlib.Path,
) -> None:
    """Manifest dir absent → no watcher, no exception."""
    config = _make_config(tmp_path)
    # Don't create config.manifest_dir.
    task = SuitTask(config)
    assert task._build_matrix_eval_watcher(
        output_dir=tmp_path / "out",
    ) is None


def test_build_matrix_eval_watcher_collects_expected_and_toolchains(
    tmp_path: pathlib.Path,
) -> None:
    """With both matrix_eval and toolchain manifests on disk, the
    watcher's expected set covers all matrix_eval task_ids and the
    toolchain_task_ids map is keyed by drv path → task_id."""
    config = _make_config(tmp_path)
    gcc_drv = "/nix/store/cccccccccccccccccccccccccccccccc-gcc15.drv"
    clang_drv = "/nix/store/dddddddddddddddddddddddddddddddd-clang20.drv"
    _seed_manifest_dir(config, [
        _matrix_eval_header("hello"),
        _matrix_eval_header("busybox"),
        _toolchain_header("x86_64", "gcc15", gcc_drv),
        _toolchain_header("x86_64", "clang20", clang_drv),
    ])
    task = SuitTask(config)
    w = task._build_matrix_eval_watcher(output_dir=tmp_path / "out")
    assert w is not None
    assert w.expected == frozenset({
        matrix_eval_task_id("hello"),
        matrix_eval_task_id("busybox"),
    })
    assert w._toolchain_task_ids == {
        gcc_drv: build_compilers_task_id(_SYS, "x86_64", "gcc15"),
        clang_drv: build_compilers_task_id(_SYS, "x86_64", "clang20"),
    }
    # out_dir falls through directly from the caller.
    assert w._out_dir == tmp_path / "out"
    # manifest_dir was wired through.
    assert w._manifest_dir == config.manifest_dir


def test_build_matrix_eval_watcher_falls_back_to_shared_fs(
    tmp_path: pathlib.Path,
) -> None:
    """If the framework doesn't pass an output_dir (legacy/test path)
    and config.matrix_eval_out_dir is None, the watcher defaults to
    ``shared_fs / 'out'``."""
    config = _make_config(tmp_path)
    _seed_manifest_dir(config, [_matrix_eval_header("hello")])
    task = SuitTask(config)
    w = task._build_matrix_eval_watcher(output_dir=None)
    assert w is not None
    assert w._out_dir == config.shared_fs / "out"


def test_build_matrix_eval_watcher_uses_config_matrix_eval_out_dir(
    tmp_path: pathlib.Path,
) -> None:
    """``config.matrix_eval_out_dir`` (when set) takes precedence."""
    explicit = tmp_path / "explicit-archive-dir"
    base = _make_config(tmp_path)
    config = _dataclasses.replace(base, matrix_eval_out_dir=explicit)
    _seed_manifest_dir(config, [_matrix_eval_header("hello")])
    task = SuitTask(config)
    w = task._build_matrix_eval_watcher(output_dir=tmp_path / "other")
    assert w is not None
    assert w._out_dir == explicit


# ---------------------------------------------------------------------------
# Broadcast record_self_has callable assembly
# ---------------------------------------------------------------------------


def test_make_broadcast_record_self_has_invokes_record_with_matrix_eval_class(
    tmp_path: pathlib.Path,
) -> None:
    """The callable wired into PeerPushServer.record_broadcast_self_has
    must invoke peer_paths.record_self_has with item_class
    ``matrix_eval_drv`` (the broadcast-receive tag) so placement-map
    gossip distinguishes matrix_eval drvs from toolchain/variant
    holders."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    captured: list[dict] = []

    def fake_record(shared_fs, *, my_secondary_id, outpath, drv_path,
                    item_class, peers=None, our_pubkey=None):
        captured.append({
            "shared_fs": shared_fs,
            "my_secondary_id": my_secondary_id,
            "outpath": outpath,
            "drv_path": drv_path,
            "item_class": item_class,
            "peers": list(peers) if peers is not None else None,
            "our_pubkey": our_pubkey,
        })

    fake_watcher = mock.MagicMock()
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=fake_record,
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        cb("/nix/store/aaa-matrix.drv")

    assert len(captured) == 1
    call = captured[0]
    assert call["my_secondary_id"] == "primary"
    assert call["outpath"] == "/nix/store/aaa-matrix.drv"
    assert call["drv_path"] == "/nix/store/aaa-matrix.drv"
    assert call["item_class"] == "matrix_eval_drv"
    assert call["our_pubkey"] == "pk-test"
    assert call["peers"] == []
    assert call["shared_fs"] == config.shared_fs


def test_make_broadcast_record_self_has_passes_live_peers(
    tmp_path: pathlib.Path,
) -> None:
    """``peers`` is snapshotted at CALL time from the watcher, not at
    closure construction — so a peer joining between push-server-start
    and the first broadcast accept gets the fan-out."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    captured: list[list] = []

    def fake_record(_shared_fs, *, peers=None, **_kw):
        captured.append(list(peers) if peers is not None else None)

    fake_watcher = mock.MagicMock()
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=fake_record,
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        cb("/nix/store/x.drv")
        fake_watcher.peers = ["peer-a-info"]
        cb("/nix/store/y.drv")

    assert captured == [[], ["peer-a-info"]]


def test_make_broadcast_record_self_has_swallows_record_exceptions(
    tmp_path: pathlib.Path,
) -> None:
    """If peer_paths.record_self_has raises, the callable must not
    propagate — best-effort gossip is part of the contract."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    fake_watcher = mock.MagicMock()
    fake_watcher.peers = []

    with mock.patch(
        "compiler_suit_runner.suit_task.peer_paths.record_self_has",
        side_effect=RuntimeError("nfs hiccup"),
    ):
        cb = task._make_broadcast_record_self_has(
            fake_watcher, public_key="pk-test",
        )
        # Must not raise.
        cb("/nix/store/raises.drv")


# ---------------------------------------------------------------------------
# BroadcastReceiver wiring inside on_run_start
# ---------------------------------------------------------------------------


def test_push_url_to_substituter_url_strips_push_offset() -> None:
    """The push port = harmonia_port + PUSH_PORT_OFFSET (1000); the
    translator inverts that so the broadcast fetch can call
    ``nix copy --from <harmonia_url>``."""
    from compiler_suit_runner.suit_task import _push_url_to_substituter_url

    assert (
        _push_url_to_substituter_url("http://node-a:6000")
        == "http://node-a:5000"
    )
    assert (
        _push_url_to_substituter_url("https://node-a:6500/")
        == "https://node-a:5500"
    )
    assert _push_url_to_substituter_url("") is None
    assert _push_url_to_substituter_url("not-a-url") is None
    assert _push_url_to_substituter_url("http://hostnoport") is None
    assert _push_url_to_substituter_url("http://x:500") is None


def test_on_run_start_wires_broadcast_receiver_into_push_server(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start``:

    1. ``task._broadcast_receiver`` is a :class:`BroadcastReceiver`.
    2. The :class:`PeerPushServer` was constructed with a callable
       wired to the receiver's ``on_broadcast_offer``.
    """
    from compiler_suit_runner import peer_cache as _peer_cache
    from compiler_suit_runner.peer_replication import BroadcastReceiver

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)

    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, harmonia_port=5005, enable_harmonia=False,
    )

    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert isinstance(task._broadcast_receiver, BroadcastReceiver)
        push_server = task._push_server
        assert push_server is not None
        handler_cls = push_server._handler_cls
        called: list[tuple] = []

        def _spy(*args, **kw):
            called.append((args, kw))
            return True

        task._broadcast_receiver.on_broadcast_offer = _spy  # type: ignore[method-assign]
        cb = handler_cls.on_broadcast_offer
        ok = cb("/nix/store/v.drv", 9, "origin", "bid-v", 0)
        assert ok is True
        assert called == [
            (("/nix/store/v.drv", 9, "origin", "bid-v", 0), {}),
        ]
    finally:
        task.on_run_end(success=True)


# ---------------------------------------------------------------------------
# _PeerLifecycleListener
# ---------------------------------------------------------------------------


from compiler_suit_runner.suit_task import _PeerLifecycleListener  # noqa: E402


def test_listener_on_peer_removed_routes_to_repair_worker_with_cause(
    tmp_path: pathlib.Path,
) -> None:
    """on_peer_removed forwards (secondary_id, reason) to the repair
    worker. The ``cause`` dict's kind/reason are preserved (reason
    when fatal_error, kind otherwise)."""
    repair = mock.MagicMock()
    listener = _PeerLifecycleListener(repair_worker=repair)

    listener.on_peer_removed(
        "peer-a",
        {"kind": "keepalive_miss", "reason": None},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-a", "keepalive_miss",
    )

    repair.reset_mock()
    listener.on_peer_removed(
        "peer-b",
        {"kind": "fatal_error", "reason": "OOMKilled"},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-b", "OOMKilled",
    )

    repair.reset_mock()
    listener.on_peer_removed(
        "peer-c",
        {"kind": "mass_death_escalation", "reason": None},
    )
    repair.on_peer_removed.assert_called_once_with(
        "peer-c", "mass_death_escalation",
    )


def test_listener_on_peer_added_observer_records_holdings(
    tmp_path: pathlib.Path,
) -> None:
    """When ``is_observer=True``, the observer-record callable is
    invoked with the observer's secondary id."""
    record_calls: list[tuple[str, str]] = []

    def record_observer(sid: str, placeholder: str) -> None:
        record_calls.append((sid, placeholder))

    listener = _PeerLifecycleListener(
        repair_worker=None,
        placement_record_observer_callable=record_observer,
    )
    listener.on_peer_added("observer-x", is_observer=True)
    assert record_calls == [("observer-x", "")]


def test_listener_on_peer_added_secondary_is_noop(
    tmp_path: pathlib.Path,
) -> None:
    """Regular-secondary additions don't fire the observer record
    callable — they're already handled by the K=3 peer-set watcher."""
    record_calls: list[tuple[str, str]] = []

    def record_observer(sid: str, placeholder: str) -> None:
        record_calls.append((sid, placeholder))

    listener = _PeerLifecycleListener(
        repair_worker=None,
        placement_record_observer_callable=record_observer,
    )
    listener.on_peer_added("secondary-1", is_observer=False)
    assert record_calls == []


def test_listener_swallows_repair_exception(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the repair worker raises, the listener logs + swallows so
    the framework doesn't disable the hook on a single hiccup."""
    repair = mock.MagicMock()
    repair.on_peer_removed.side_effect = RuntimeError("boom")

    listener = _PeerLifecycleListener(repair_worker=repair)
    with caplog.at_level(logging.ERROR):
        listener.on_peer_removed(
            "peer-x", {"kind": "fatal_error", "reason": "panic"},
        )
    assert any(
        "on_peer_removed raised" in rec.message
        for rec in caplog.records
    )


def test_listener_handles_missing_or_malformed_cause(
    tmp_path: pathlib.Path,
) -> None:
    """A non-dict / None cause is tolerated (kind defaults to '')."""
    repair = mock.MagicMock()
    listener = _PeerLifecycleListener(repair_worker=repair)

    listener.on_peer_removed("peer-a", None)  # type: ignore[arg-type]
    repair.on_peer_removed.assert_called_once_with("peer-a", "")

    repair.reset_mock()
    listener.on_peer_removed("peer-b", {})
    repair.on_peer_removed.assert_called_once_with("peer-b", "")


# ---------------------------------------------------------------------------
# Q1 + Q2 PrimaryHandle callable wrappers
# ---------------------------------------------------------------------------


def test_mark_task_unfulfillable_invokes_fail_permanent(
    tmp_path: pathlib.Path,
) -> None:
    """The wrapper converts hex→bytes and calls
    ``primary_handle.fail_permanent(bytes, 'Unfulfillable', reason)``."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._mark_task_unfulfillable("0a1b2c", "tc-gone reason")

    handle.fail_permanent.assert_called_once_with(
        bytes.fromhex("0a1b2c"), "Unfulfillable", "tc-gone reason",
    )


def test_mark_task_unfulfillable_noop_without_handle(
    tmp_path: pathlib.Path,
) -> None:
    """Without a bound primary handle the wrapper logs + returns."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    task._primary_handle = None
    task._mark_task_unfulfillable("00ff", "any reason")


def test_reinject_task_invokes_handle_reinject(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._reinject_task("deadbeef")

    handle.reinject_task.assert_called_once_with(
        bytes.fromhex("deadbeef"),
    )


def test_update_preferred_secondaries_invokes_handle(
    tmp_path: pathlib.Path,
) -> None:
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    task._update_preferred_secondaries(
        "0123", ["sec-a", "sec-b"],
    )
    handle.update_preferred_secondaries.assert_called_once_with(
        bytes.fromhex("0123"), ["sec-a", "sec-b"],
    )


def test_wire_primary_handle_applies_reinject_cap(
    tmp_path: pathlib.Path,
) -> None:
    """``wire_primary_handle`` invokes ``apply_unfulfillable_reinject_cap``
    when the config field is set."""
    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, unfulfillable_reinject_max_per_task=7,
    )
    task = SuitTask(config)
    handle = mock.MagicMock()

    task.wire_primary_handle(handle)

    assert task._primary_handle is handle
    handle.set_unfulfillable_reinject_max_per_task.assert_called_once_with(7)


def test_wire_primary_handle_skips_cap_when_unset(
    tmp_path: pathlib.Path,
) -> None:
    """``wire_primary_handle`` does not call the setter when the config
    field is None (framework default = unbounded)."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    handle = mock.MagicMock()

    task.wire_primary_handle(handle)

    handle.set_unfulfillable_reinject_max_per_task.assert_not_called()


# ---------------------------------------------------------------------------
# Q3 fulfillability matcher attribute
# ---------------------------------------------------------------------------


def test_fulfillability_matcher_is_holding_matcher_callable(
    tmp_path: pathlib.Path,
) -> None:
    """The ``_fulfillability_matcher`` attribute is the
    :func:`holding_matcher.matcher` function."""
    from compiler_suit_runner.holding_matcher import matcher

    config = _make_config(tmp_path)
    task = SuitTask(config)
    assert task._fulfillability_matcher is matcher


# ---------------------------------------------------------------------------
# on_run_start integration
# ---------------------------------------------------------------------------


def test_on_run_start_constructs_peer_lifecycle_listener(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``on_run_start`` the listener is constructed and bound to
    the repair worker."""
    from compiler_suit_runner import peer_cache as _peer_cache

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, harmonia_port=5010, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert task._peer_lifecycle_listener is not None
        fake_repair = mock.MagicMock()
        task._peer_lifecycle_listener._repair = fake_repair
        task._peer_lifecycle_listener.on_peer_removed(
            "peer-z", {"kind": "fatal_error", "reason": "panic-xyz"},
        )
        fake_repair.on_peer_removed.assert_called_once_with(
            "peer-z", "panic-xyz",
        )
    finally:
        task.on_run_end(success=True)


def test_on_run_start_builds_outpath_to_task_hash_lookup(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outpath→task_hash dict is populated from toolchain manifest
    payloads at on_run_start."""
    from compiler_suit_runner import peer_cache as _peer_cache
    from compiler_suit_runner.manifest_gen import ManifestHeader, write_manifest

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, harmonia_port=5011, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(config.manifest_dir, ManifestHeader(
        item_class="toolchain_validate",
        name="toolchain_validate__x86_64__gcc15",
        size=0,
        payload={
            "sys": _SYS,
            "arch": "x86_64",
            "compiler_label": "gcc15",
            "attr": "_crossToolchainMap.linux.x86_64.gcc15",
            "drv": "/nix/store/aa-gcc15.drv",
            "outpath": "/nix/store/bb-gcc15-out",
            "validate_only": True,
        },
        task_id=build_compilers_task_id(_SYS, "x86_64", "gcc15"),
    ))

    task = SuitTask(config)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert "/nix/store/bb-gcc15-out" in task._outpath_to_task_hash
    finally:
        task.on_run_end(success=True)


def test_replication_context_callables_route_through_self(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ReplicationContext`` is constructed in ``on_run_start`` with
    its four PrimaryHandle callables pointing at the ``SuitTask``
    instance methods."""
    from compiler_suit_runner import peer_cache as _peer_cache

    fake_key = _peer_cache.SigningKey(
        name="test-key",
        secret_path=tmp_path / "key.secret",
        public_path=tmp_path / "key.public",
        public_key="test-pubkey:AAAA",
    )
    fake_key.secret_path.write_text("secret")
    fake_key.public_path.write_text(fake_key.public_key)
    monkeypatch.setattr(
        "compiler_suit_runner.suit_task.generate_signing_key",
        lambda *a, **kw: fake_key,
    )

    config = _make_config(tmp_path)
    config = _dataclasses.replace(
        config, harmonia_port=5012, enable_harmonia=False,
    )
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)

    task = SuitTask(config)
    handle = mock.MagicMock()
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        task.wire_primary_handle(handle)
        task._mark_task_unfulfillable("aabb", "no holder")
        task._reinject_task("ccdd")
        task._update_preferred_secondaries("eeff", ["s1", "s2"])

        handle.fail_permanent.assert_called_once_with(
            bytes.fromhex("aabb"), "Unfulfillable", "no holder",
        )
        handle.reinject_task.assert_called_once_with(
            bytes.fromhex("ccdd"),
        )
        handle.update_preferred_secondaries.assert_called_once_with(
            bytes.fromhex("eeff"), ["s1", "s2"],
        )
    finally:
        task.on_run_end(success=True)


# ---------------------------------------------------------------------------
# on_run_start primary_handle kwarg + task_completed_listener
# ---------------------------------------------------------------------------


def test_on_run_start_accepts_primary_handle_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """The ``on_run_start`` signature accepts the ``primary_handle``
    kwarg and stores the value on the task."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)
    handle = mock.MagicMock()
    try:
        task.on_run_start(
            source_dir=tmp_path,
            output_dir=tmp_path / "out",
            args=None,
            primary_handle=handle,
        )
        assert task._primary_handle is handle
    finally:
        task.on_run_end(success=True)


def test_on_run_start_backward_compat_without_kwarg(
    tmp_path: pathlib.Path,
) -> None:
    """A caller that omits ``primary_handle`` still runs without
    raising; the attribute remains None."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.shared_fs.mkdir(parents=True, exist_ok=True)
    try:
        task.on_run_start(output_dir=tmp_path / "out")
        assert task._primary_handle is None
    finally:
        task.on_run_end(success=True)


def test_task_completed_listener_forwards_to_watcher(
    tmp_path: pathlib.Path,
) -> None:
    """The ``task_completed_listener`` property returns a callable that
    routes matching task_ids into the matrix_eval watcher's
    ``on_task_completed``; non-matching ids are NoOp."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    hello = matrix_eval_task_id("hello")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "_dependency_graph.json").write_text(
        json.dumps({"phase4_descriptors": []}), encoding="utf-8",
    )
    watcher = _MatrixEvalQuiesceWatcher(
        expected_task_ids={hello},
        out_dir=out_dir,
        toolchain_task_ids={},
        run_subprocess=lambda _argv: _fake_completed(0),
        dependency_graph_command_override=["true"],
        bash_path="/nix/store/fake-bash",
    )
    task._matrix_eval_watcher = watcher
    listener = task.task_completed_listener
    assert callable(listener)

    listener("toolchain__gcc15__x86_64", True, None)
    assert watcher.completed == frozenset()
    listener("", True, None)
    listener(None, True, None)
    assert watcher.completed == frozenset()

    listener(hello, True, None)
    assert watcher.fired is True


def test_task_completed_listener_noop_without_watcher(
    tmp_path: pathlib.Path,
) -> None:
    """No watcher constructed → the listener callable still answers
    and silently no-ops on every call."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    assert task._matrix_eval_watcher is None
    listener = task.task_completed_listener
    listener("anything", True, None)
    listener("else", False, "recoverable")


def test_task_completed_listener_swallows_watcher_exception(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A buggy watcher must not propagate into the framework's apply
    path."""
    config = _make_config(tmp_path)
    task = SuitTask(config)

    fake_watcher = mock.MagicMock()
    fake_watcher.expected = frozenset({"task-x"})
    fake_watcher.on_task_completed.side_effect = RuntimeError("boom")
    task._matrix_eval_watcher = fake_watcher

    listener = task.task_completed_listener
    with caplog.at_level(logging.ERROR):
        listener("task-x", True, None)

    assert any(
        "task_completed_listener: dispatch raised" in rec.message
        for rec in caplog.records
    )


def test_build_matrix_eval_watcher_threads_primary_handle(
    tmp_path: pathlib.Path,
) -> None:
    """``_build_matrix_eval_watcher`` reads ``self._primary_handle``
    and threads it onto the constructed watcher."""
    config = _make_config(tmp_path)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(config.manifest_dir, _matrix_eval_header("hello"))
    task = SuitTask(config)
    handle = mock.MagicMock()
    task._primary_handle = handle

    w = task._build_matrix_eval_watcher(output_dir=tmp_path / "out")
    assert w is not None
    assert w._primary_handle is handle


def test_task_completed_listener_attribute_signature(
    tmp_path: pathlib.Path,
) -> None:
    """The ``task_completed_listener`` callable accepts the framework's
    ``(task_id, success, error_kind)`` shape."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    listener = task.task_completed_listener
    listener("some-task", False, "recoverable")
    listener("other-task", True, None)
    listener(None, True, None)


# ---------------------------------------------------------------------------
# _phase_specs topology
# ---------------------------------------------------------------------------


def test_phase_specs_returns_three_phases() -> None:
    """``_phase_specs`` declares build_compilers, matrix_eval, build —
    the dependency_graph step is primary-only and not a framework
    phase."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    by_id = {s.phase_id: s for s in specs}
    assert set(by_id.keys()) == {
        "build_compilers", "matrix_eval", "build",
    }


def test_phase_specs_matrix_eval_routes_to_build_worker() -> None:
    """matrix_eval has a single ``eval`` type pointing at
    ``compiler_suit_runner.workers.build_worker`` (which sniffs the
    item_class and dispatches to eval_worker.run_eval_task)."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    matrix_eval = next(s for s in specs if s.phase_id == "matrix_eval")
    assert len(matrix_eval.types) == 1
    assert matrix_eval.types[0].type_id == "eval"
    assert (
        matrix_eval.types[0].worker_module
        == "compiler_suit_runner.workers.build_worker"
    )


def test_phase_specs_all_types_share_single_worker_module() -> None:
    """Sanity: every TaskTypeSpec across all phases binds to the same
    worker_module string."""
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    modules = {
        t.worker_module for spec in specs for t in spec.types
    }
    assert modules == {"compiler_suit_runner.workers.build_worker"}


def test_phase_specs_matrix_eval_depends_on_build_compilers() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    matrix_eval = next(s for s in specs if s.phase_id == "matrix_eval")
    assert matrix_eval.depends_on == ("build_compilers",)


def test_phase_specs_build_depends_on_matrix_eval() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    build = next(s for s in specs if s.phase_id == "build")
    assert build.depends_on == ("matrix_eval",)


def test_phase_specs_build_carries_validate_common_dep_variant() -> None:
    pytest.importorskip("dynamic_runner.task_protocol")
    from compiler_suit_runner.suit_task import _phase_specs
    specs = _phase_specs(build_max_concurrent=None)
    build = next(s for s in specs if s.phase_id == "build")
    type_ids = {t.type_id for t in build.types}
    assert type_ids == {
        "toolchain_validate",
        "common_dep",
        "variant",
    }

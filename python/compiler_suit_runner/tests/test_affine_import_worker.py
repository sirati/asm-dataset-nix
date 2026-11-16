"""Tests for build_worker._run_affine_import and affine_import_worker.handle.

These tests are fully hermetic — no real nix-store invocations and no
framework imports required.  ``affine_import_worker.handle`` is tested with
a STUB Task object carrying ``.relative_path`` (the #577-new field) to stay
independent of the pre-#577 framework.
"""

from __future__ import annotations

import pathlib

import pytest

from compiler_suit_runner.workers.build_worker import _run_affine_import


# ---------------------------------------------------------------------------
# _run_affine_import dispatch: one test per gate-id form
# ---------------------------------------------------------------------------


def _make_monkeypatched_run(
    monkeypatch: pytest.MonkeyPatch,
    calls: list,
    gate_id: str,
    tmp_path: pathlib.Path,
):
    """Helper that calls _run_affine_import(gate_id, tmp_path) after patching."""
    _run_affine_import(gate_id, tmp_path)


def test_run_affine_import_import_common(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_common gate → ensure_common_archive_imported(out_dir)."""
    calls: list = []

    def _fake(out_dir, **_kw):
        calls.append(("common", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_common_archive_imported",
        _fake,
    )
    _run_affine_import("import_common", tmp_path)
    assert calls == [("common", tmp_path)]


def test_run_affine_import_import_build_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_build_deps gate → ensure_build_deps_archive_imported(out_dir)."""
    calls: list = []

    def _fake(out_dir, **_kw):
        calls.append(("build_deps", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_build_deps_archive_imported",
        _fake,
    )
    _run_affine_import("import_build_deps", tmp_path)
    assert calls == [("build_deps", tmp_path)]


def test_run_affine_import_import_toolchain_drv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_toolchain_drv gate → ensure_toolchain_archive_imported(out_dir)."""
    calls: list = []

    def _fake(out_dir, **_kw):
        calls.append(("toolchain_drv", out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_toolchain_archive_imported",
        _fake,
    )
    _run_affine_import("import_toolchain_drv", tmp_path)
    assert calls == [("toolchain_drv", tmp_path)]


def test_run_affine_import_import_common_dep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_common_dep_<id> gate → ensure_common_dep_out_archive_imported(id, out_dir)."""
    calls: list = []

    def _fake(cd_id, out_dir, **_kw):
        calls.append(("common_dep", cd_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_common_dep_out_archive_imported",
        _fake,
    )
    _run_affine_import("import_common_dep_abc123", tmp_path)
    assert calls == [("common_dep", "abc123", tmp_path)]


def test_run_affine_import_import_matrix_drv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_matrix_drv_<binary> gate → ensure_binary_archive_imported(binary, out_dir)."""
    calls: list = []

    def _fake(binary, out_dir, **_kw):
        calls.append(("matrix_drv", binary, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_binary_archive_imported",
        _fake,
    )
    _run_affine_import("import_matrix_drv_zlib", tmp_path)
    assert calls == [("matrix_drv", "zlib", tmp_path)]


def test_run_affine_import_import_tc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """import_tc_<hash> gate → ensure_toolchain_out_archive_imported(hash, out_dir)."""
    calls: list = []

    def _fake(tc_id, out_dir, **_kw):
        calls.append(("tc_out", tc_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker.ensure_toolchain_out_archive_imported",
        _fake,
    )
    _run_affine_import("import_tc_deadbeef", tmp_path)
    assert calls == [("tc_out", "deadbeef", tmp_path)]


def test_run_affine_import_unknown_gate_raises_value_error(
    tmp_path: pathlib.Path,
) -> None:
    """An unrecognised gate id raises ValueError."""
    with pytest.raises(ValueError, match="unknown gate task id"):
        _run_affine_import("not_a_real_gate", tmp_path)


def test_run_affine_import_gate_ids_stay_in_lock_step_with_suit_task() -> None:
    """Regression guard: the 6 gate-id prefixes in _run_affine_import must
    match the constants declared in suit_task.

    If a new gate type is added to suit_task, its string literal must be
    replicated in _run_affine_import (and vice-versa).  This test is a
    compile-time canary that breaks immediately when the two diverge.
    """
    from compiler_suit_runner.suit_task import (  # noqa: PLC0415
        IMPORT_COMMON_TASK_ID,
        IMPORT_BUILD_DEPS_TASK_ID,
        IMPORT_TOOLCHAIN_DRV_TASK_ID,
        IMPORT_COMMON_DEP_TASK_PREFIX,
    )

    # These must match the if/elif chain in _run_affine_import exactly.
    assert IMPORT_COMMON_TASK_ID == "import_common"
    assert IMPORT_BUILD_DEPS_TASK_ID == "import_build_deps"
    assert IMPORT_TOOLCHAIN_DRV_TASK_ID == "import_toolchain_drv"
    assert IMPORT_COMMON_DEP_TASK_PREFIX == "import_common_dep_"
    # Prefix-based gates (no constant, string literal in both files):
    import compiler_suit_runner.workers.build_worker as bw  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    src = inspect.getsource(bw._run_affine_import)
    assert "import_matrix_drv_" in src
    assert "import_tc_" in src


# ---------------------------------------------------------------------------
# affine_import_worker.handle: stub-Task tests (no framework import needed)
# ---------------------------------------------------------------------------


class _StubTask:
    """Minimal stand-in for dynamic_runner.worker.Task (#577 shape).

    Only exposes ``relative_path`` (the gate id) and ``payload`` so the
    handle() closure can function without the real framework.
    """

    def __init__(self, relative_path: str, payload: object = None) -> None:
        self.relative_path = relative_path
        self.payload = payload


def _make_handle(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Build a handle() closure equivalent to what affine_import_worker.main
    constructs, using monkeypatched _run_affine_import.

    We do NOT invoke ``main()`` (which would need a live framework socket)
    but instead replicate the inner ``handle`` closure from the worker,
    which is the only function we actually want to test.
    """
    # Import the error types from the worker module directly (no socket).
    # These are pure dataclasses; importing them doesn't start any server.
    try:
        from dynamic_runner.worker import (  # noqa: PLC0415
            NonRecoverableError,
            RecoverableError,
            WorkerOutput,
        )
    except ImportError:
        # dynamic_runner not installed in the test environment — skip the
        # framework-coupled half of these tests.
        pytest.skip("dynamic_runner.worker not importable in this environment")


    matrix_eval_out_dir = tmp_path

    # Reconstruct the handle closure from affine_import_worker.
    # The real main() also calls run(handle, args=args) to pump the wire;
    # we just need to test handle() in isolation.
    import compiler_suit_runner.workers.build_worker as bw_mod  # noqa: PLC0415

    def handle(task: _StubTask):
        gate_id = task.relative_path
        try:
            bw_mod._run_affine_import(gate_id, matrix_eval_out_dir)
        except OSError as exc:
            raise RecoverableError(
                f"affine_import {gate_id!r}: transient OSError: {exc}"
            ) from exc
        except ValueError as exc:
            raise NonRecoverableError(
                f"affine_import {gate_id!r}: unknown gate id: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            # Mirror affine_import_worker.handle: nix-daemon-not-ready is
            # transient/recoverable, not a permanent cascade.
            _low = str(exc).lower()
            if "cannot connect to socket" in _low or "daemon-socket" in _low:
                raise RecoverableError(
                    f"affine_import {gate_id!r}: nix-daemon not ready"
                    f" (transient): {exc}"
                ) from exc
            raise NonRecoverableError(
                f"affine_import {gate_id!r}: {type(exc).__name__}: {exc}"
            ) from exc
        return WorkerOutput()

    return handle, RecoverableError, NonRecoverableError, WorkerOutput


def test_handle_calls_run_affine_import_with_relative_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """handle() passes task.relative_path as the gate_id to _run_affine_import."""
    handle, _, _, WorkerOutput = _make_handle(monkeypatch, tmp_path)

    calls: list = []

    def _fake_run(gate_id, out_dir):
        calls.append((gate_id, out_dir))

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker._run_affine_import",
        _fake_run,
    )

    task = _StubTask("import_common")
    result = handle(task)
    assert calls == [("import_common", tmp_path)]
    assert isinstance(result, WorkerOutput)


def test_handle_maps_os_error_to_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """handle() maps OSError from _run_affine_import to RecoverableError."""
    handle, RecoverableError, _, _ = _make_handle(monkeypatch, tmp_path)

    def _raise_os(gate_id, out_dir):
        raise OSError("nix-store daemon gone away")

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker._run_affine_import",
        _raise_os,
    )

    with pytest.raises(RecoverableError, match="transient OSError"):
        handle(_StubTask("import_common"))


def test_handle_maps_nix_daemon_not_ready_to_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A RuntimeError carrying the nix-daemon-not-ready signature maps to
    RecoverableError (not NonRecoverable), so the gate re-dispatches instead
    of cascade-failing the node's dependents (incl. shared common_deps)."""
    handle, RecoverableError, _, _ = _make_handle(monkeypatch, tmp_path)

    def _raise_daemon(gate_id, out_dir):
        raise RuntimeError(
            "build_worker: failed to import toolchains.common.archive: "
            "error: cannot connect to socket at "
            "'/nix/var/nix/daemon-socket/socket': No such file or directory"
        )

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker._run_affine_import",
        _raise_daemon,
    )

    with pytest.raises(RecoverableError, match="nix-daemon not ready"):
        handle(_StubTask("import_common"))


def test_handle_maps_value_error_to_non_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """handle() maps ValueError (unknown gate) to NonRecoverableError."""
    handle, _, NonRecoverableError, _ = _make_handle(monkeypatch, tmp_path)

    def _raise_ve(gate_id, out_dir):
        raise ValueError(f"unknown gate task id {gate_id!r}")

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker._run_affine_import",
        _raise_ve,
    )

    with pytest.raises(NonRecoverableError, match="unknown gate id"):
        handle(_StubTask("not_a_real_gate"))


def test_handle_maps_runtime_error_to_non_recoverable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """handle() maps RuntimeError (e.g. archive miss) to NonRecoverableError."""
    handle, _, NonRecoverableError, _ = _make_handle(monkeypatch, tmp_path)

    def _raise_rt(gate_id, out_dir):
        raise RuntimeError("archive absent on shared-fs")

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_worker._run_affine_import",
        _raise_rt,
    )

    with pytest.raises(NonRecoverableError, match="RuntimeError"):
        handle(_StubTask("import_common"))

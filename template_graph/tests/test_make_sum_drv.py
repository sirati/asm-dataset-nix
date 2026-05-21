"""Pure-mock unit tests for the public helpers in
``template_graph.make_sum_drv``.

These do NOT invoke ``nix-instantiate``; they patch
``_run_nix_instantiate`` and assert the constructed Nix expression
and subprocess argv shape. Runs in CI without nix on PATH.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from template_graph import make_sum_drv as mod


def test_make_wrapper_drv_from_paths_calls_nix_instantiate_with_expected_expr():
    """The helper imports ``wrapper_drv.nix`` and forwards drvs/name/system.

    We patch ``_run_nix_instantiate`` so no nix subprocess fires.
    """
    drvs = [
        "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-foo.drv",
        "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-bar.drv",
    ]
    fake_out = "/nix/store/cccccccccccccccccccccccccccccccc-toolchains.drv"

    with patch.object(mod, "_run_nix_instantiate", return_value=fake_out) as m:
        ret = mod.make_wrapper_drv_from_paths(
            drvs=drvs,
            name="toolchains",
            system="x86_64-linux",
        )

    assert ret == fake_out
    assert m.call_count == 1
    (expr,), kwargs = m.call_args
    # mirrors make_sum_drv_from_paths: no flakes, optional extra_nix_args
    assert kwargs["with_flakes"] is False
    assert kwargs["extra_nix_args"] is None

    # Nix import target is wrapper_drv.nix sitting next to make_sum_drv.py.
    assert str(mod.WRAPPER_DRV_NIX) in expr
    assert expr.lstrip().startswith(f"import {mod.WRAPPER_DRV_NIX}")

    # Each drv path is rendered via builtins.appendContext, so its
    # store path appears literally in the expression.
    for drv in drvs:
        assert drv in expr
    assert "builtins.appendContext" in expr

    # name and system arrive as nix string literals.
    assert 'name = "toolchains"' in expr
    assert 'system = "x86_64-linux"' in expr


def test_make_wrapper_drv_from_paths_passes_extra_nix_args():
    """``extra_nix_args`` is forwarded verbatim to ``_run_nix_instantiate``."""
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ) as m:
        mod.make_wrapper_drv_from_paths(
            drvs=["/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x.drv"],
            name="matrix-hello",
            extra_nix_args=["--store", "/tmp/store"],
        )
    _args, kwargs = m.call_args
    assert kwargs["extra_nix_args"] == ["--store", "/tmp/store"]


def test_make_wrapper_drv_from_paths_default_system():
    """``system`` defaults to ``x86_64-linux`` when not supplied."""
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ) as m:
        mod.make_wrapper_drv_from_paths(
            drvs=["/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x.drv"],
            name="toolchains",
        )
    (expr,), _kwargs = m.call_args
    assert 'system = "x86_64-linux"' in expr


def test_make_wrapper_drv_from_paths_rejects_empty_drvs():
    """No basename validation, but at least one drv must be supplied."""
    import pytest

    with pytest.raises(ValueError, match="at least one drv path"):
        mod.make_wrapper_drv_from_paths(drvs=[], name="toolchains")


def test_make_wrapper_drv_from_paths_rejects_empty_name():
    """The wrapper drv ``name`` is mandatory (drives the .drv basename)."""
    import pytest

    with pytest.raises(ValueError, match="name is required"):
        mod.make_wrapper_drv_from_paths(
            drvs=["/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x.drv"],
            name="",
        )


def test_make_wrapper_drv_from_paths_does_not_validate_basenames():
    """The CONSTRUCTION site accepts any basename — validation belongs
    in :func:`make_sum_drv_from_paths` (the assembly site).
    """
    # Names that the assembly site WOULD reject (no "toolchain" /
    # "matrix" markers) must go through here without complaint.
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ):
        mod.make_wrapper_drv_from_paths(
            drvs=[
                "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello.drv",
                "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-bar.drv",
            ],
            name="some-arbitrary-name",
        )


# ---------------------------------------------------------------------------
# make_sum_drv_from_paths — aggregate-assembly site
# ---------------------------------------------------------------------------

_TOOLCHAINS_AGG = (
    "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-toolchains.drv"
)
_MATRIX_HELLO_AGG = (
    "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-matrix-hello.drv"
)
_MATRIX_BUSYBOX_AGG = (
    "/nix/store/cccccccccccccccccccccccccccccccc-matrix-busybox.drv"
)
_BASH_PATH = "/nix/store/dddddddddddddddddddddddddddddddd-bash"


def test_make_sum_drv_from_paths_imports_aggregate_assembler():
    """The helper imports ``sum_drv_from_aggregates.nix`` and passes
    the toolchains drv + matrix drvs in the new arg shape.
    """
    fake_out = "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-sum-root.drv"

    with patch.object(mod, "_run_nix_instantiate", return_value=fake_out) as m:
        ret = mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={
                "matrix-hello":   [_MATRIX_HELLO_AGG],
                "matrix-busybox": [_MATRIX_BUSYBOX_AGG],
            },
            system="x86_64-linux",
        )

    assert ret == fake_out
    assert m.call_count == 1
    (expr,), kwargs = m.call_args
    assert kwargs["with_flakes"] is False
    assert kwargs["extra_nix_args"] is None

    # New target is sum_drv_from_aggregates.nix, NOT sum_drv.nix.
    assert str(mod.SUM_DRV_FROM_AGGREGATES_NIX) in expr
    assert expr.lstrip().startswith(
        f"import {mod.SUM_DRV_FROM_AGGREGATES_NIX}"
    )
    assert str(mod.SUM_DRV_NIX) not in expr

    # New arg names — singular toolchainsDrv, plural matrixDrvs.
    assert "toolchainsDrv =" in expr
    assert "matrixDrvs =" in expr
    # Legacy arg names from sum_drv.nix must NOT leak through.
    assert "toolchains =" not in expr
    assert "matrices =" not in expr
    assert "toolchainsName" not in expr

    # bash arrives as a builtins.storePath; aggregates ride through
    # builtins.appendContext.
    assert f'builtins.storePath "{_BASH_PATH}"' in expr
    assert "builtins.appendContext" in expr
    assert _TOOLCHAINS_AGG in expr
    assert _MATRIX_HELLO_AGG in expr
    assert _MATRIX_BUSYBOX_AGG in expr

    assert 'rootName = "sum-root"' in expr
    assert 'system = "x86_64-linux"' in expr


def test_make_sum_drv_from_paths_default_system_and_root_name():
    """Defaults: ``system=x86_64-linux``, ``rootName=sum-root``."""
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ) as m:
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
        )
    (expr,), _kwargs = m.call_args
    assert 'system = "x86_64-linux"' in expr
    assert 'rootName = "sum-root"' in expr


def test_make_sum_drv_from_paths_passes_extra_nix_args():
    """``extra_nix_args`` is forwarded verbatim to ``_run_nix_instantiate``."""
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ) as m:
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
            extra_nix_args=["--store", "/tmp/store"],
        )
    _args, kwargs = m.call_args
    assert kwargs["extra_nix_args"] == ["--store", "/tmp/store"]


def test_make_sum_drv_from_paths_rejects_empty_toolchains():
    """At least one toolchain drv path is required."""
    import pytest

    with pytest.raises(ValueError, match="at least one toolchain drv path"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
        )


def test_make_sum_drv_from_paths_rejects_empty_matrices():
    """At least one matrix is required."""
    import pytest

    with pytest.raises(ValueError, match="at least one matrix"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={},
        )


def test_make_sum_drv_from_paths_rejects_matrix_with_no_drvs():
    """An empty per-binary list is a contract violation."""
    import pytest

    with pytest.raises(ValueError, match="no drv paths"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": []},
        )


def test_make_sum_drv_from_paths_rejects_multi_toolchain_aggregate():
    """Length-1 invariant: more than one toolchains drv is rejected.

    The new sum_drv_from_aggregates.nix takes ONE toolchains aggregate;
    passing two would silently drop one and is therefore a hard error.
    """
    import pytest

    other_tc = (
        "/nix/store/ffffffffffffffffffffffffffffffff-toolchains.drv"
    )
    with pytest.raises(ValueError, match="exactly ONE toolchains"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG, other_tc],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
        )


def test_make_sum_drv_from_paths_rejects_multi_matrix_aggregate():
    """Length-1 invariant: more than one matrix drv per binary rejected."""
    import pytest

    other_hello = (
        "/nix/store/00000000000000000000000000000001-matrix-hello.drv"
    )
    with pytest.raises(ValueError, match="exactly ONE matrix aggregate"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG, other_hello]},
        )


def test_make_sum_drv_from_paths_rejects_unmarked_toolchain_basename():
    """Validation marker still fires on the toolchains aggregate."""
    import pytest

    bad = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-foo.drv"
    with pytest.raises(ValueError, match="does not contain the expected"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[bad],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
        )


def test_make_sum_drv_from_paths_rejects_unmarked_matrix_basename():
    """Validation marker still fires on each matrix aggregate."""
    import pytest

    bad = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-hello.drv"
    with pytest.raises(ValueError, match="does not contain the expected"):
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": [bad]},
        )


def test_make_sum_drv_from_paths_accepts_canonical_markers():
    """Basenames with ``toolchains`` / ``matrix-<binary>`` pass validation."""
    with patch.object(
        mod, "_run_nix_instantiate", return_value="/nix/store/x.drv"
    ):
        # No raise.
        mod.make_sum_drv_from_paths(
            bash_path=_BASH_PATH,
            toolchain_drvs=[_TOOLCHAINS_AGG],
            matrix_drvs={"matrix-hello": [_MATRIX_HELLO_AGG]},
        )


# ---------------------------------------------------------------------------
# _run_nix_instantiate — stderr mirror + CSR_NIX_DEBUG_DIR dump
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess``.

    ``_run_nix_instantiate`` only reads ``returncode``, ``stdout``
    (bytes), and ``stderr`` (bytes) — keeping the stub lean lets us
    avoid mocking the constructor signature.
    """

    def __init__(self, *, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_nix_instantiate_failure_mirrors_stderr_and_dumps_debug_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: failure path must mirror nix stderr to both
    ``sys.stderr`` and ``$CSR_NIX_DEBUG_DIR/nix-instantiate-*.log``.

    The dynamic_runner wire format truncates the raised RuntimeError
    message; the mirror ensures the full nix diagnostic survives in
    the worker / container log AND on the shared-fs debug dir.
    """
    monkeypatch.setenv("CSR_NIX_DEBUG_DIR", str(tmp_path))

    def _fake_run(argv, capture_output=True, check=False, **_):
        return _FakeCompletedProcess(
            returncode=1,
            stdout=b"",
            stderr=b"sample nix error",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="sample nix error"):
        mod._run_nix_instantiate(
            "let x = 1; in x",
            extra_nix_args=None,
            with_flakes=False,
        )

    # Mirror 1: a debug-log file landed under CSR_NIX_DEBUG_DIR with
    # the verbatim stderr captured.
    logs = list(tmp_path.glob("nix-instantiate-*.log"))
    assert len(logs) == 1, (
        f"expected exactly one nix-instantiate-*.log under {tmp_path}, "
        f"got {[p.name for p in logs]}"
    )
    log_text = logs[0].read_text(encoding="utf-8")
    assert "sample nix error" in log_text
    # The dump also captures the expression so operators can reproduce.
    assert "--- expression ---" in log_text
    assert "let x = 1; in x" in log_text

    # Mirror 2: stderr is echoed via print(..., file=sys.stderr).
    captured = capsys.readouterr()
    assert "sample nix error" in captured.err


def test_template_graph_importable_with_project_rooted_pythonpath(
    tmp_path: Path,
) -> None:
    """Smoke check mirroring ``PYTHONPATH=/app/python:/app/flake`` in the
    docker image: spawning a fresh Python with PYTHONPATH set to the
    project's ``python/`` dir AND the flake-root dir must let
    ``template_graph`` resolve.

    The container env baked in ``nix/docker-image.nix`` is what makes
    the dependency_graph / eval workers able to ``import template_graph``
    at runtime; if the entry ever drops the flake-root component, the
    workers fail at the first sum-drv assembly. This subprocess test
    pins the layout independently of the harness's own sys.path
    additions.
    """
    repo_root = Path(__file__).resolve().parents[2]
    python_root = repo_root / "python"
    if not (repo_root / "template_graph" / "__init__.py").is_file():
        pytest.skip("template_graph not on disk in expected layout")
    if not python_root.is_dir():
        pytest.skip("python/ package root not on disk in expected layout")

    # Mirror the image PYTHONPATH: compiler_suit_runner root + flake root.
    env_pythonpath = f"{python_root}:{repo_root}"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import template_graph; print(template_graph.__name__)",
        ],
        env={
            "PYTHONPATH": env_pythonpath,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
        cwd=str(tmp_path),
        capture_output=True,
        check=False,
        text=True,
    )
    assert proc.returncode == 0, (
        f"importing template_graph with PYTHONPATH={env_pythonpath!r} "
        f"failed:\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == "template_graph"


def test_run_nix_instantiate_failure_without_debug_dir_still_mirrors_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``CSR_NIX_DEBUG_DIR`` unset: the on-disk dump is skipped but the
    stderr mirror still fires (so the framework-log capture survives
    the wire-format truncation).
    """
    monkeypatch.delenv("CSR_NIX_DEBUG_DIR", raising=False)

    def _fake_run(argv, capture_output=True, check=False, **_):
        return _FakeCompletedProcess(
            returncode=2,
            stdout=b"",
            stderr=b"another nix diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(RuntimeError, match="another nix diagnostic"):
        mod._run_nix_instantiate(
            "let y = 2; in y",
            extra_nix_args=None,
            with_flakes=False,
        )

    captured = capsys.readouterr()
    assert "another nix diagnostic" in captured.err

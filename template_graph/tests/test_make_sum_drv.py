"""Pure-mock unit tests for the public helpers in
``template_graph.make_sum_drv``.

These do NOT invoke ``nix-instantiate``; they patch
``_run_nix_instantiate`` and assert the constructed Nix expression
and subprocess argv shape. Runs in CI without nix on PATH.
"""

from __future__ import annotations

from unittest.mock import patch

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

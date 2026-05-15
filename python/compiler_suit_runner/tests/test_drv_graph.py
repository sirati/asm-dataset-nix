"""Unit tests for ``compiler_suit_runner.drv_graph``.

The subprocess seam (``run_subprocess``) is dependency-injected, so
no ``nix`` binary is ever invoked. Each test provides a stub runner
that returns a synthetic ``(stdout, stderr, returncode)`` triple.
"""

from __future__ import annotations

import json
from typing import Callable
from unittest import mock

import pytest

from compiler_suit_runner.drv_graph import DrvGraphError, read_input_drvs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DRV = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello.drv"
_INPUT_A = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-glibc.drv"
_INPUT_B = "/nix/store/cccccccccccccccccccccccccccccccc-gcc-wrapper.drv"
_INPUT_C = "/nix/store/dddddddddddddddddddddddddddddddd-stdenv.drv"


def _make_runner(
    stdout: bytes = b"", stderr: bytes = b"", rc: int = 0
) -> tuple[Callable, list[list[str]]]:
    """Build a stub runner that records every argv and returns the triple."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[bytes, bytes, int]:
        calls.append(list(argv))
        return stdout, stderr, rc

    return runner, calls


def _show_derivation_json(
    drv: str, input_drvs: dict | None
) -> bytes:
    """Serialize a synthetic ``nix show-derivation`` envelope for ``drv``."""
    entry: dict = {
        "outputs": {"out": {"path": "/nix/store/zzzz-out"}},
        "inputSrcs": [],
    }
    if input_drvs is not None:
        entry["inputDrvs"] = input_drvs
    return json.dumps({drv: entry}).encode("utf-8")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_read_input_drvs_happy_path_returns_keys():
    payload = _show_derivation_json(
        _DRV,
        {
            _INPUT_A: {"dynamicOutputs": {}, "outputs": ["out"]},
            _INPUT_B: {"dynamicOutputs": {}, "outputs": ["out"]},
            _INPUT_C: {"dynamicOutputs": {}, "outputs": ["out", "dev"]},
        },
    )
    runner, calls = _make_runner(stdout=payload)

    result = read_input_drvs(_DRV, run_subprocess=runner)

    assert result == {_INPUT_A, _INPUT_B, _INPUT_C}
    assert calls == [["nix", "show-derivation", _DRV]]


def test_read_input_drvs_empty_input_drvs_returns_empty_set():
    payload = _show_derivation_json(_DRV, {})
    runner, _ = _make_runner(stdout=payload)

    assert read_input_drvs(_DRV, run_subprocess=runner) == set()


def test_read_input_drvs_missing_input_drvs_key_returns_empty_set():
    # Some sources / fixed-output derivations omit inputDrvs entirely.
    # Treat this as "no inputs", not as an error.
    payload = _show_derivation_json(_DRV, input_drvs=None)
    runner, _ = _make_runner(stdout=payload)

    assert read_input_drvs(_DRV, run_subprocess=runner) == set()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_read_input_drvs_nonzero_exit_raises_with_stderr():
    runner, _ = _make_runner(
        stdout=b"",
        stderr=b"error: path '/nix/store/zzzz.drv' does not exist\n",
        rc=1,
    )

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    err = excinfo.value
    assert err.drv_path == _DRV
    assert "does not exist" in err.detail
    assert "does not exist" in str(err)


def test_read_input_drvs_nonzero_exit_with_empty_stderr_surfaces_rc():
    runner, _ = _make_runner(stdout=b"", stderr=b"", rc=2)

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert "rc=2" in excinfo.value.detail


def test_read_input_drvs_malformed_json_raises():
    runner, _ = _make_runner(stdout=b"this is not json {{{")

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert excinfo.value.detail == "malformed JSON"
    assert excinfo.value.drv_path == _DRV


def test_read_input_drvs_top_level_not_a_single_dict_raises():
    # A JSON list at the top level should be rejected.
    runner, _ = _make_runner(stdout=b"[]")

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert excinfo.value.detail == "unexpected JSON shape"


def test_read_input_drvs_multiple_top_level_keys_raises():
    payload = json.dumps(
        {
            _DRV: {"inputDrvs": {}},
            "/nix/store/extra.drv": {"inputDrvs": {}},
        }
    ).encode("utf-8")
    runner, _ = _make_runner(stdout=payload)

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert excinfo.value.detail == "unexpected JSON shape"


def test_read_input_drvs_entry_not_a_dict_raises():
    payload = json.dumps({_DRV: "garbage"}).encode("utf-8")
    runner, _ = _make_runner(stdout=payload)

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert excinfo.value.detail == "unexpected JSON shape"


def test_read_input_drvs_input_drvs_not_a_dict_raises():
    payload = json.dumps({_DRV: {"inputDrvs": [1, 2, 3]}}).encode("utf-8")
    runner, _ = _make_runner(stdout=payload)

    with pytest.raises(DrvGraphError) as excinfo:
        read_input_drvs(_DRV, run_subprocess=runner)

    assert excinfo.value.detail == "unexpected JSON shape"


# ---------------------------------------------------------------------------
# Default runner — mock.patch on subprocess.run to confirm wiring
# ---------------------------------------------------------------------------


def test_default_runner_invokes_subprocess_run_with_expected_argv():
    payload = _show_derivation_json(_DRV, {_INPUT_A: {}})
    fake_completed = mock.Mock(stdout=payload, stderr=b"", returncode=0)

    with mock.patch(
        "compiler_suit_runner.drv_graph.subprocess.run",
        return_value=fake_completed,
    ) as run_mock:
        result = read_input_drvs(_DRV)

    assert result == {_INPUT_A}
    run_mock.assert_called_once()
    args, kwargs = run_mock.call_args
    assert args[0] == ["nix", "show-derivation", _DRV]
    assert kwargs.get("capture_output") is True
    assert kwargs.get("check") is False

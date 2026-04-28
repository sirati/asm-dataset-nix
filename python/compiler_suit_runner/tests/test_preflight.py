"""Unit tests for ``compiler_suit_runner.preflight``.

All ``nix eval`` invocations are stubbed so the test suite never shells
out to nix. The injection seam is the ``run_subprocess`` parameter on
each public function.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from compiler_suit_runner.preflight import (
    PreflightResult,
    enumerate_toolchains,
    enumerate_variants,
    preflight,
    run_nix_eval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_subprocess(responses: dict[str, object]):
    """Return a fake ``run_subprocess`` that maps attr -> JSON payload.

    The argv looks like ``["nix", "eval", "--extra-experimental-features",
    "nix-command flakes", "--json", "<flake>#<attr>"]``. We extract the
    final ``<flake>#<attr>`` token and look it up in ``responses``.
    Unknown attrs raise to surface mistakes loudly.
    """
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        # Find the flake#attr argument; it's the last positional.
        target = argv[-1]
        if "#" not in target:
            return b"", b"missing #attr", 1
        attr = target.split("#", 1)[1]
        if attr not in responses:
            return b"", f"no fake for {attr}".encode(), 1
        payload = responses[attr]
        if isinstance(payload, bytes):
            return payload, b"", 0
        return json.dumps(payload).encode("utf-8"), b"", 0

    return runner, calls


def _fake_meta() -> dict:
    return {
        "hello": {
            "x86_64": {
                "gcc15-O0-baseline-unhardened": {
                    "variantLabel": "hello-x86_64-gcc15-O0-baseline-unhardened",
                    "package": "hello",
                    "arch": "x86_64",
                    "compiler": "gcc15",
                    "compilerFamily": "gcc",
                    "compilerVersion": "15.2.0",
                    "optimization": "O0",
                    "flags": "baseline",
                    "hardening": "unhardened",
                },
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "hello-x86_64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
            "aarch64": {
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "hello-aarch64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
        },
        "busybox": {
            "x86_64": {
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "busybox-x86_64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
        },
    }


def _fake_drvpaths() -> dict:
    return {
        "hello": {
            "x86_64": {
                "gcc15-O0-baseline-unhardened": "/nix/store/aaa-hello-x86-O0.drv",
                "gcc15-O2-baseline-unhardened": "/nix/store/bbb-hello-x86-O2.drv",
            },
            "aarch64": {
                "gcc15-O2-baseline-unhardened": "/nix/store/ccc-hello-aarch.drv",
            },
        },
        "busybox": {
            "x86_64": {
                "gcc15-O2-baseline-unhardened": "/nix/store/ddd-busybox-x86.drv",
            },
        },
    }


def _fake_toolchains() -> dict:
    return {
        "x86_64": [
            {"compiler": "gcc15", "family": "gcc", "version": "15.2.0"},
            {"compiler": "clang20", "family": "clang", "version": "20.1.8"},
        ],
        "aarch64": [
            {"compiler": "gcc15", "family": "gcc", "version": "15.2.0"},
        ],
    }


def _all_responses(sys_name: str = "x86_64-linux") -> dict[str, object]:
    return {
        f"_meta.{sys_name}": _fake_meta(),
        f"_drvPaths.{sys_name}": _fake_drvpaths(),
        f"_crossToolchainsMeta.{sys_name}": _fake_toolchains(),
    }


# ---------------------------------------------------------------------------
# run_nix_eval
# ---------------------------------------------------------------------------


def test_run_nix_eval_json():
    runner, calls = _make_run_subprocess(_all_responses())
    result = run_nix_eval(".", "_meta.x86_64-linux", run_subprocess=runner)
    assert result == _fake_meta()
    # argv constructed correctly.
    assert calls and calls[0][0] == "nix"
    assert "--json" in calls[0]


def test_run_nix_eval_raw_returns_stdout_string():
    raw_payload = b"/nix/store/abc-foo.drv\n"

    def runner(argv):
        return raw_payload, b"", 0

    result = run_nix_eval(".", "_drvPaths.x86_64-linux.hello", raw=True, run_subprocess=runner)
    assert isinstance(result, str)
    assert result.startswith("/nix/store/abc-foo.drv")


def test_run_nix_eval_failure_raises_runtime_error():
    def runner(argv):
        return b"", b"attribute not found", 1

    with pytest.raises(RuntimeError, match="attribute not found"):
        run_nix_eval(".", "_meta.bogus", run_subprocess=runner)


def test_run_nix_eval_invalid_json_raises():
    def runner(argv):
        return b"not json", b"", 0

    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_nix_eval(".", "_meta.x86_64-linux", run_subprocess=runner)


# ---------------------------------------------------------------------------
# enumerate_variants
# ---------------------------------------------------------------------------


def test_enumerate_variants_full_matrix():
    runner, _calls = _make_run_subprocess(_all_responses())
    variants, drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    # 2 (hello/x86_64) + 1 (hello/aarch64) + 1 (busybox/x86_64) = 4
    assert len(variants) == 4
    labels = {v["label"] for v in variants}
    assert "hello-x86_64-gcc15-O0-baseline-unhardened" in labels
    assert "busybox-x86_64-gcc15-O2-baseline-unhardened" in labels

    # Each variant has the expected fields.
    for v in variants:
        assert v["pkg"]
        assert v["arch"]
        assert v["compiler_id"] == "gcc15"
        assert v["drv"].startswith("/nix/store/")
        assert v["tarball_name"].endswith(".tar.zst")
        assert isinstance(v["tier"], int)

    # toolchain_drvs covers all distinct drv paths.
    assert drvs == frozenset({
        "/nix/store/aaa-hello-x86-O0.drv",
        "/nix/store/bbb-hello-x86-O2.drv",
        "/nix/store/ccc-hello-aarch.drv",
        "/nix/store/ddd-busybox-x86.drv",
    })


def test_enumerate_variants_filter_by_packages():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["hello"],
        run_subprocess=runner,
    )
    assert variants  # not empty
    assert all(v["pkg"] == "hello" for v in variants)
    assert {v["pkg"] for v in variants} == {"hello"}


def test_enumerate_variants_filter_by_archs():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        archs=["aarch64"],
        run_subprocess=runner,
    )
    assert variants  # at least one (hello/aarch64)
    assert all(v["arch"] == "aarch64" for v in variants)


def test_enumerate_variants_combined_filter():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["busybox"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert len(variants) == 1
    assert variants[0]["pkg"] == "busybox"
    assert variants[0]["arch"] == "x86_64"


def test_enumerate_variants_filter_no_match_returns_empty():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["nonexistent-pkg"],
        run_subprocess=runner,
    )
    assert variants == ()
    assert drvs == frozenset()


def test_enumerate_variants_tier_assignment():
    """``hello`` (tier 1), ``busybox`` (tier 1) — verify tier mapping."""
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _ = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    by_pkg = {v["pkg"]: v["tier"] for v in variants}
    assert by_pkg["hello"] == 1
    assert by_pkg["busybox"] == 1


# ---------------------------------------------------------------------------
# enumerate_toolchains
# ---------------------------------------------------------------------------


def test_enumerate_toolchains_full():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(".", "x86_64-linux", run_subprocess=runner)
    assert pairs == (
        ("aarch64", "gcc15"),
        ("x86_64", "clang20"),
        ("x86_64", "gcc15"),
    )


def test_enumerate_toolchains_filter_by_archs():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(
        ".",
        "x86_64-linux",
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert all(arch == "x86_64" for arch, _ in pairs)
    assert ("x86_64", "gcc15") in pairs
    assert ("x86_64", "clang20") in pairs


def test_enumerate_toolchains_unknown_arch_returns_empty():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(
        ".",
        "x86_64-linux",
        archs=["nonexistent"],
        run_subprocess=runner,
    )
    assert pairs == ()


# ---------------------------------------------------------------------------
# preflight composition
# ---------------------------------------------------------------------------


def test_preflight_composes_both_calls():
    runner, calls = _make_run_subprocess(_all_responses())
    result = preflight(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    assert isinstance(result, PreflightResult)
    assert result.sys_name == "x86_64-linux"
    assert len(result.variants) == 4
    assert len(result.toolchain_specs) == 3
    # common_dep_drvs is left empty until phase 1b populates it.
    assert result.common_dep_drvs == ()
    assert isinstance(result.toolchain_drvs, frozenset)
    assert len(result.toolchain_drvs) == 4

    # Three nix eval invocations — meta, drvPaths, toolchains.
    eval_attrs = {
        argv[-1].split("#", 1)[1] for argv in calls
    }
    assert "_meta.x86_64-linux" in eval_attrs
    assert "_drvPaths.x86_64-linux" in eval_attrs
    assert "_crossToolchainsMeta.x86_64-linux" in eval_attrs


def test_preflight_filter_pipes_through():
    runner, _ = _make_run_subprocess(_all_responses())
    result = preflight(
        ".",
        "x86_64-linux",
        packages=["hello"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert all(v["pkg"] == "hello" and v["arch"] == "x86_64" for v in result.variants)
    assert all(arch == "x86_64" for arch, _ in result.toolchain_specs)

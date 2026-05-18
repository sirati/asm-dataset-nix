"""Tests for the ``_classify`` mapping in :mod:`suit_task`.

The mapping is the single source of truth for which dynamic_runner
phase / type / affinity bucket each consumer ``item_class`` lands
in; if it ever drifts, scheduling is silently broken.
"""

from __future__ import annotations

import pytest

from compiler_suit_runner.manifest_gen import ManifestHeader
from compiler_suit_runner.suit_task import _classify


def _header(item_class: str, payload: dict | None = None) -> ManifestHeader:
    return ManifestHeader(
        item_class=item_class,  # type: ignore[arg-type]
        name="test",
        size=1024,
        payload=payload or {},
    )


def test_classify_matrix_eval_uses_binary_affinity() -> None:
    h = _header("matrix_eval", payload={"binary": "hello"})
    assert _classify(h) == ("matrix_eval", "eval", "hello")


def test_classify_matrix_eval_unknown_payload() -> None:
    h = _header("matrix_eval", payload={})
    assert _classify(h) == ("matrix_eval", "eval", "?")


def test_classify_build_compilers_uses_compiler_arch_affinity() -> None:
    h = _header(
        "build_compilers",
        payload={"compiler_label": "gcc15", "arch": "aarch64"},
    )
    assert _classify(h) == (
        "build_compilers", "build_compilers", "gcc15-aarch64",
    )


def test_classify_build_compilers_unknown_payload() -> None:
    """Affinity falls back to the literal '?' marker, never raises."""
    h = _header("build_compilers", payload={})
    assert _classify(h) == ("build_compilers", "build_compilers", "?-?")


def test_classify_toolchain_validate_uses_compiler_arch_affinity() -> None:
    h = _header(
        "toolchain_validate",
        payload={"compiler_label": "gcc15", "arch": "x86_64"},
    )
    assert _classify(h) == ("build", "toolchain_validate", "gcc15-x86_64")


def test_classify_build_common_dep() -> None:
    h = _header(
        "build_common_dep",
        payload={"drv": "/nix/store/glibc.drv", "label": "glibc"},
    )
    assert _classify(h) == ("build", "common_dep", None)


def test_classify_build_variant_uses_compiler_id_arch_affinity() -> None:
    h = _header(
        "build_variant",
        payload={"compiler_id": "gcc15", "arch": "x86_64", "pkg": "hello"},
    )
    assert _classify(h) == ("build", "variant", "gcc15-x86_64")


def test_classify_build_variant_unknown_payload() -> None:
    h = _header("build_variant", payload={})
    assert _classify(h) == ("build", "variant", "?-?")


def test_classify_unknown_item_class_raises() -> None:
    h = _header("phase99_what")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _classify(h)

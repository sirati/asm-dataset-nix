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


def test_classify_phase1a_partition() -> None:
    h = _header("phase1a_partition")
    assert _classify(h) == ("phase1a", "partition", None)


def test_classify_phase1b_merge() -> None:
    h = _header("phase1b_merge")
    assert _classify(h) == ("phase1b", "merge", None)


def test_classify_phase2_toolchain_uses_compiler_arch_affinity() -> None:
    h = _header(
        "phase2_toolchain",
        payload={"compiler_label": "gcc15", "arch": "aarch64"},
    )
    assert _classify(h) == ("phase_build", "toolchain", "gcc15-aarch64")


def test_classify_phase2_toolchain_unknown_payload() -> None:
    """Affinity falls back to the literal '?' marker, never raises."""
    h = _header("phase2_toolchain", payload={})
    assert _classify(h) == ("phase_build", "toolchain", "?-?")


def test_classify_phase2_common_dep() -> None:
    h = _header(
        "phase2_common_dep",
        payload={"drv": "/nix/store/glibc.drv", "label": "glibc"},
    )
    assert _classify(h) == ("phase_build", "common_dep", None)


def test_classify_phase3_variant_uses_compiler_id_arch_affinity() -> None:
    h = _header(
        "phase3_variant",
        payload={"compiler_id": "gcc15", "arch": "x86_64", "pkg": "hello"},
    )
    assert _classify(h) == ("phase_build", "variant", "gcc15-x86_64")


def test_classify_phase3_variant_unknown_payload() -> None:
    h = _header("phase3_variant", payload={})
    assert _classify(h) == ("phase_build", "variant", "?-?")


def test_classify_unknown_item_class_raises() -> None:
    h = _header("phase99_what")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _classify(h)

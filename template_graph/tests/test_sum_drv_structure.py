"""Sanity probes on the sum-drv structure.

These tests assert on the *shape* of the sum-drv fixture: a single
``toolchains`` wrapper, at least one ``matrix-*`` wrapper, the
expected cardinalities (8 cc-wrappers, 16 hello variants), and the
name-extractor collapsing wrapper families within each arch.

Fixtures live in ``conftest.py``.
"""

from __future__ import annotations

import pytest

from template_graph.drv_io import DrvIoError, read_drv_record
from template_graph.tests.conftest import _is_builder_noise, _wrapper_name
from template_graph.tests.dataset_naming import hello_name_extractor


def test_root_has_toolchains_and_at_least_one_matrix(
    root_drv, sum_structure
):
    rec = read_drv_record(root_drv)
    names = {
        _wrapper_name(k) for k in rec["inputDrvs"] if not _is_builder_noise(k)
    }
    assert "toolchains" in names
    assert any(n.startswith("matrix-") for n in names), names
    # And sum_structure agrees.
    assert sum_structure["matrices"], "no matrices discovered"


def test_toolchains_wrapper_lists_all_cc_wrappers(sum_structure):
    # Expect 8 = 4 archs x 2 compilers.
    assert len(sum_structure["toolchain_drvs"]) == 8
    for drv in sum_structure["toolchain_drvs"]:
        basename = drv.rsplit("/", 1)[-1]
        assert "gcc-wrapper" in basename or "clang-wrapper" in basename


def test_matrix_hello_lists_all_variants(hello_matrix):
    # Expect 16 = 4 archs x 2 compilers x 2 opt levels.
    assert len(hello_matrix["all_variants"]) == 16
    for label, drv in hello_matrix["all_variants"].items():
        assert label.count("__") == 1
        assert drv.endswith(".drv")


def test_name_extractor_canonicalises_variant_axes(hello_matrix):
    for label, drv in hello_matrix["all_variants"].items():
        assert hello_name_extractor(drv) == "hello", drv


def test_name_extractor_collapses_wrapper_families_within_arch(
    sum_structure,
):
    by_arch: dict[str, set[str]] = {}
    for d in sum_structure["toolchain_drvs"]:
        basename = d.rsplit("/", 1)[-1]
        if "aarch64" in basename:
            arch = "aarch64"
        elif "armv7l" in basename:
            arch = "armv7l"
        elif "riscv64" in basename:
            arch = "riscv64"
        else:
            arch = "x86_64"
        by_arch.setdefault(arch, set()).add(hello_name_extractor(d))
    for arch, names in by_arch.items():
        assert len(names) == 1, f"arch={arch} got {names}"
        canonical = next(iter(names))
        assert "gcc-wrapper" not in canonical
        assert "clang-wrapper" not in canonical


def test_read_drv_record_raises_on_missing_drv():
    with pytest.raises(DrvIoError):
        read_drv_record("/nix/store/0000000000000000000000000000000-nope.drv")

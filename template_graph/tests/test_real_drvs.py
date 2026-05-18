"""End-to-end tests against ONE real root .drv (the sum-drv fixture).

Fixtures (``root_drv``, ``sum_structure``, ``hello_matrix``,
``non_recursing_drvs``) live in this directory's ``conftest.py``.

The bootstrap-chain hard-error assertions (real nixpkgs trips them
by design) carry the variant labels in the error message, which is
the algorithm's "hard error for now" debugging signal.
"""

from __future__ import annotations

import pytest

from template_graph.core import (
    TemplateGraphAssertError,
    VariantArray,
    build_template_from_closure,
    cowalk_and_index,
)
from template_graph.drv_io import DrvIoError, read_drv_record
from template_graph.tests.conftest import _is_builder_noise, _wrapper_name
from template_graph.tests.dataset_naming import hello_name_extractor


# ---------------------------------------------------------------------------
# Sanity probes on the sum-drv structure
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Algorithm: build template from one real variant
# ---------------------------------------------------------------------------


def test_build_template_from_one_variant(
    sum_structure, hello_matrix, non_recursing_drvs
):
    label, drv = next(iter(sorted(hello_matrix["all_variants"].items())))
    t = build_template_from_closure(
        root_drv=drv,
        get_record=read_drv_record,
        toolchain_drvs=sum_structure["toolchain_drvs"],
        non_recursing_drvs=non_recursing_drvs,
        name_extractor=hello_name_extractor,
        built_from_label=label,
    )
    assert len(t.nodes) > 20
    assert t.nodes[t.root_id].name == "hello"
    assert any(n.is_toolchain for n in t.nodes)
    for n in t.nodes:
        if n.is_toolchain:
            assert n.child_ids == []


# ---------------------------------------------------------------------------
# Algorithm: hard-error mechanism on real nixpkgs closures
# ---------------------------------------------------------------------------


def test_cowalk_detects_bootstrap_chain_collision_with_anchor_context(
    sum_structure, hello_matrix, non_recursing_drvs
):
    x86 = hello_matrix["variants_by_arch"]["x86_64"]
    anchor_label, anchor_drv = x86[0]

    template = build_template_from_closure(
        root_drv=anchor_drv,
        get_record=read_drv_record,
        toolchain_drvs=sum_structure["toolchain_drvs"],
        non_recursing_drvs=non_recursing_drvs,
        name_extractor=hello_name_extractor,
        built_from_label=anchor_label,
    )
    arr = VariantArray(
        template_id=0, arch="x86_64", variants=[anchor_label],
        hashes=[[None] for _ in template.nodes],
    )
    for n in template.nodes:
        n.visit_flag = False

    with pytest.raises(TemplateGraphAssertError) as exc:
        cowalk_and_index(
            template=template, drv_path=anchor_drv,
            get_record=read_drv_record, arr=arr, variant_index=0,
            toolchain_drvs=sum_structure["toolchain_drvs"],
            non_recursing_drvs=non_recursing_drvs,
            current_variant_label=anchor_label,
            name_extractor=hello_name_extractor,
        )
    e = exc.value
    assert e.kind == "dag-revisit-hash-mismatch"
    assert e.failing_variant == anchor_label
    assert anchor_label in e.template_built_from
    rendered = str(e)
    assert anchor_label in rendered
    assert "template built from" in rendered
    assert "failing variant" in rendered
    assert e.details["stored"] != e.details["observed"]


def test_alien_arch_cowalk_fires_with_proper_context(
    sum_structure, hello_matrix, non_recursing_drvs
):
    x86 = hello_matrix["variants_by_arch"]["x86_64"]
    aar = hello_matrix["variants_by_arch"]["aarch64"]
    anchor_label, anchor_drv = x86[0]
    failing_label, failing_drv = aar[0]

    template = build_template_from_closure(
        root_drv=anchor_drv,
        get_record=read_drv_record,
        toolchain_drvs=sum_structure["toolchain_drvs"],
        non_recursing_drvs=non_recursing_drvs,
        name_extractor=hello_name_extractor,
        built_from_label=anchor_label,
    )
    arr = VariantArray(
        template_id=0, arch="x86_64", variants=[],
        hashes=[[] for _ in template.nodes],
    )
    arr.variants.append(failing_label)
    for row in arr.hashes:
        row.append(None)
    for n in template.nodes:
        n.visit_flag = False
    with pytest.raises(TemplateGraphAssertError) as exc:
        cowalk_and_index(
            template=template, drv_path=failing_drv,
            get_record=read_drv_record, arr=arr, variant_index=0,
            toolchain_drvs=sum_structure["toolchain_drvs"],
            non_recursing_drvs=non_recursing_drvs,
            current_variant_label=failing_label,
            name_extractor=hello_name_extractor,
        )
    e = exc.value
    assert e.failing_variant == failing_label
    assert anchor_label in e.template_built_from
    rendered = str(e)
    assert anchor_label in rendered
    assert failing_label in rendered
    assert e.kind in {
        "child-name-mismatch",
        "dag-revisit-hash-mismatch",
        "terminal-shape-mismatch",
        "multi-drv-same-name",
        "closure-missing-drv",
    }

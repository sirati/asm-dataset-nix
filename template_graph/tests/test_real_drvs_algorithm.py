"""Algorithm probes against ONE real root .drv (the sum-drv fixture).

Fixtures (``root_drv``, ``sum_structure``, ``hello_matrix``,
``non_recursing_drvs``) live in this directory's ``conftest.py``.
Structural probes on the sum-drv shape live in
``test_sum_drv_structure.py``.

The bootstrap-chain hard-error assertions (real nixpkgs trips them
by design) carry the variant labels in the error message, which is
the algorithm's "hard error for now" debugging signal.

Every test here needs a live nix store path resolved from the
fixture; runs are skipped when ``nix`` is not on PATH or the
sum-drv has been GC'd.
"""

from __future__ import annotations

import pytest

from template_graph.core import (
    TemplateGraphAssertError,
    VariantArray,
    build_template_from_closure,
    cowalk_and_index,
)
from template_graph.drv_io import read_drv_record
from template_graph.tests.dataset_naming import hello_name_extractor


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
    # 20 = floor for a nontrivial hello closure (stdenv + glibc +
    # gcc-runtime); the real closure is much larger, but this guards
    # against accidentally walking only the toolchain side.
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

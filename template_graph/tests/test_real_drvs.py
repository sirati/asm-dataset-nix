"""End-to-end test against ONE real root .drv (the sum-drv fixture).

The fixture file ``fixtures/hello_sum_drv.txt`` holds a single
``/nix/store/...-sum-root.drv`` path. That drv's ``inputDrvs`` is
the sum tree:

    sum-root
      ├── toolchains           (inputDrvs = 8 cc-wrappers + bash)
      └── matrix-hello         (inputDrvs = 16 variant drvs + bash)
      [ ... additional matrix-<binary> wrappers in future fixtures ]

The test reads the root once via ``read_drv_record``, routes the
single toolchains wrapper and N ``matrix-*`` wrappers by name, then
reads each to get the toolchain / per-binary variant lists.
``bash-interactive`` appears in every wrapper's inputDrvs (it's our
builder reference); we filter it out by name.

Skipped if the fixture doesn't exist or the referenced drvs are
GC'd — rerun ``template_graph/tests/fixtures/generate.py``.

The bootstrap-chain hard-error assertions (real nixpkgs trips them
by design) carry the variant labels in the error message, which is
the algorithm's "hard error for now" debugging signal.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from template_graph.core import (
    TemplateGraphAssertError,
    VariantArray,
    build_template_from_closure,
    cowalk_and_index,
)
from template_graph.drv_io import DrvIoError, read_drv_record
from template_graph.tests.dataset_naming import hello_name_extractor


FIX_DIR = Path(__file__).parent / "fixtures"
SUM_DRV_FILE = FIX_DIR / "hello_sum_drv.txt"


def _have_nix() -> bool:
    return shutil.which("nix") is not None


def _read_single_drv_path(path: Path) -> str:
    if not path.exists():
        pytest.skip(
            f"missing {path.name}; run "
            "template_graph/tests/fixtures/generate.py"
        )
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    pytest.skip(f"{path.name} contains no drv path")
    raise AssertionError  # for the type-checker


def _is_builder_noise(drv_path: str) -> bool:
    """Filter out the bash drv our wrappers reference as builder."""
    basename = drv_path.rsplit("/", 1)[-1]
    # Drop the <hash>- prefix so we match the package name.
    rest = basename.split("-", 1)[1] if "-" in basename else basename
    return rest.startswith("bash-interactive") or rest.startswith("bash-")


@pytest.fixture(scope="module")
def root_drv() -> str:
    if not _have_nix():
        pytest.skip("nix not on PATH")
    drv = _read_single_drv_path(SUM_DRV_FILE)
    if not Path(drv).exists():
        pytest.skip(
            "sum-drv has been GC'd; rerun "
            "template_graph/tests/fixtures/generate.py"
        )
    return drv


def _wrapper_name(drv_path: str) -> str:
    """Return the wrapper's nix `name` field, derived from its basename.

    ``/nix/store/HASH-toolchains.drv`` -> ``"toolchains"``.
    """
    basename = drv_path.rsplit("/", 1)[-1]
    if basename.endswith(".drv"):
        basename = basename[: -len(".drv")]
    return basename.split("-", 1)[1] if "-" in basename else basename


@pytest.fixture(scope="module")
def sum_structure(root_drv) -> dict:
    """Walk root → toolchains_wrapper + N matrix-<binary>_wrappers.

    Returns:
      - ``toolchain_drvs``: set of cc-wrapper drv paths
      - ``matrices``: {binary: {"variants_by_arch": {...},
                                "all_variants": {label: drv}}}
    """
    root = read_drv_record(root_drv)
    tc_wrapper = None
    matrix_wrappers: dict[str, str] = {}  # binary -> wrapper drv
    for k in root["inputDrvs"]:
        if _is_builder_noise(k):
            continue
        name = _wrapper_name(k)
        if name == "toolchains":
            tc_wrapper = k
        elif name.startswith("matrix-"):
            binary = name[len("matrix-"):]
            matrix_wrappers[binary] = k
    assert tc_wrapper is not None, "root missing 'toolchains' wrapper"
    assert matrix_wrappers, "root has no 'matrix-*' wrappers"

    tc_rec = read_drv_record(tc_wrapper)
    toolchain_drvs: set[str] = {
        k for k in tc_rec["inputDrvs"] if not _is_builder_noise(k)
    }

    matrices: dict[str, dict] = {}
    for binary, mx in matrix_wrappers.items():
        mx_rec = read_drv_record(mx)
        variant_drvs = [
            k for k in mx_rec["inputDrvs"] if not _is_builder_noise(k)
        ]

        # Derive (arch, suffix) labels from drv basenames:
        # "<hash>-<binary>-<arch>-<suffix>-elf-folder.drv".
        variants_by_arch: dict[str, list[tuple[str, str]]] = {}
        all_variants: dict[str, str] = {}
        binary_prefix = binary + "-"
        for drv in sorted(variant_drvs):
            basename = drv.rsplit("/", 1)[-1]
            without_hash = basename.split("-", 1)[1]
            assert without_hash.startswith(binary_prefix), basename
            without_pkg = without_hash[len(binary_prefix):]
            assert without_pkg.endswith("-elf-folder.drv"), basename
            body = without_pkg[: -len("-elf-folder.drv")]
            arch_match = None
            for cand in ("armv7l-hf", "armv7l-sf", "x86_64", "aarch64",
                         "riscv64", "i686", "ppc64", "ppc32",
                         "mips64el", "mipsel"):
                if body.startswith(cand + "-"):
                    arch_match = cand
                    break
            assert arch_match is not None, f"unknown arch prefix in {body!r}"
            suffix = body[len(arch_match) + 1:]
            label = f"{arch_match}__{suffix}"
            variants_by_arch.setdefault(arch_match, []).append((label, drv))
            all_variants[label] = drv

        matrices[binary] = {
            "variants_by_arch": variants_by_arch,
            "all_variants": all_variants,
            "wrapper": mx,
        }

    return {
        "toolchain_drvs": toolchain_drvs,
        "matrices": matrices,
        "tc_wrapper": tc_wrapper,
    }


# Convenience accessor — most tests today only exercise the hello matrix.
@pytest.fixture(scope="module")
def hello_matrix(sum_structure) -> dict:
    assert "hello" in sum_structure["matrices"], (
        "fixture does not include a matrix-hello wrapper"
    )
    return sum_structure["matrices"]["hello"]


@pytest.fixture(scope="module")
def non_recursing_drvs(sum_structure, hello_matrix) -> set[str]:
    """toolchains ∪ host-side build helpers from variant 0's top inputs.

    Treat the constant native helpers (``stdenv-linux``, ``bash``,
    ``findutils``, ``file``) as terminals so the algorithm doesn't
    walk all the way into nixpkgs's bootstrap chain through them.
    ``<binary>-variant`` is the only top-level input we KEEP
    walking into — that's where compiler / opt-level diffs live.
    """
    toolchains = sum_structure["toolchain_drvs"]
    extra: set[str] = set()
    sample_drv = next(iter(hello_matrix["all_variants"].values()))
    rec = read_drv_record(sample_drv)
    for input_drv in rec["inputDrvs"]:
        basename = input_drv.rsplit("/", 1)[-1]
        if "hello-variant" in basename:
            continue
        extra.add(input_drv)
    return toolchains | extra


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
    # Expect 8 = 4 archs × 2 compilers.
    assert len(sum_structure["toolchain_drvs"]) == 8
    for drv in sum_structure["toolchain_drvs"]:
        basename = drv.rsplit("/", 1)[-1]
        assert "gcc-wrapper" in basename or "clang-wrapper" in basename


def test_matrix_hello_lists_all_variants(hello_matrix):
    # Expect 16 = 4 archs × 2 compilers × 2 opt levels.
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

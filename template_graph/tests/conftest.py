"""Shared fixtures for the real-drv test suite.

The fixture file ``fixtures/hello_sum_drv.txt`` holds a single
``/nix/store/...-sum-root.drv`` path. That drv's ``inputDrvs`` is
the sum tree:

    sum-root
      |-- toolchains           (inputDrvs = 8 cc-wrappers + bash)
      `-- matrix-hello         (inputDrvs = 16 variant drvs + bash)
      [ ... additional matrix-<binary> wrappers in future fixtures ]

The fixtures here read the root once via ``read_drv_record``, route
the single toolchains wrapper and N ``matrix-*`` wrappers by name,
then read each to get the toolchain / per-binary variant lists.
``bash-interactive`` appears in every wrapper's inputDrvs (it's our
builder reference); we filter it out by name.

Tests are skipped if the fixture file is missing or the referenced
drvs are GC'd -- rerun ``template_graph/tests/fixtures/generate.py``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from template_graph.drv_io import read_drv_record


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


def is_builder_noise(drv_path: str) -> bool:
    """Filter out the bash drv our wrappers reference as builder."""
    basename = drv_path.rsplit("/", 1)[-1]
    # Drop the <hash>- prefix so we match the package name.
    rest = basename.split("-", 1)[1] if "-" in basename else basename
    return rest.startswith("bash-interactive") or rest.startswith("bash-")


def wrapper_name(drv_path: str) -> str:
    """Return the wrapper's nix `name` field, derived from its basename.

    ``/nix/store/HASH-toolchains.drv`` -> ``"toolchains"``.
    """
    basename = drv_path.rsplit("/", 1)[-1]
    if basename.endswith(".drv"):
        basename = basename[: -len(".drv")]
    return basename.split("-", 1)[1] if "-" in basename else basename


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


@pytest.fixture(scope="module")
def sum_structure(root_drv) -> dict:
    """Walk root -> toolchains_wrapper + N matrix-<binary>_wrappers.

    Returns:
      - ``toolchain_drvs``: set of cc-wrapper drv paths
      - ``matrices``: {binary: {"variants_by_arch": {...},
                                "all_variants": {label: drv}}}
    """
    root = read_drv_record(root_drv)
    tc_wrapper = None
    matrix_wrappers: dict[str, str] = {}  # binary -> wrapper drv
    for k in root["inputDrvs"]:
        if is_builder_noise(k):
            continue
        name = wrapper_name(k)
        if name == "toolchains":
            tc_wrapper = k
        elif name.startswith("matrix-"):
            binary = name[len("matrix-"):]
            matrix_wrappers[binary] = k
    assert tc_wrapper is not None, "root missing 'toolchains' wrapper"
    assert matrix_wrappers, "root has no 'matrix-*' wrappers"

    tc_rec = read_drv_record(tc_wrapper)
    toolchain_drvs: set[str] = {
        k for k in tc_rec["inputDrvs"] if not is_builder_noise(k)
    }

    matrices: dict[str, dict] = {}
    for binary, mx in matrix_wrappers.items():
        mx_rec = read_drv_record(mx)
        variant_drvs = [
            k for k in mx_rec["inputDrvs"] if not is_builder_noise(k)
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


# Convenience accessor -- most tests today only exercise the hello matrix.
@pytest.fixture(scope="module")
def hello_matrix(sum_structure) -> dict:
    assert "hello" in sum_structure["matrices"], (
        "fixture does not include a matrix-hello wrapper"
    )
    return sum_structure["matrices"]["hello"]


@pytest.fixture(scope="module")
def non_recursing_drvs(sum_structure, hello_matrix) -> set[str]:
    """toolchains union host-side build helpers from variant 0's top inputs.

    Treat the constant native helpers (``stdenv-linux``, ``bash``,
    ``findutils``, ``file``) as terminals so the algorithm doesn't
    walk all the way into nixpkgs's bootstrap chain through them.
    ``<binary>-variant`` is the only top-level input we KEEP
    walking into -- that's where compiler / opt-level diffs live.
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

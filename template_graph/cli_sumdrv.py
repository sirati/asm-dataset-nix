"""Sum-drv assembly + ``nix-store --query --tree`` helper.

The CLI wraps the matrix variants and toolchain drvs into a single
sum-root via ``make_sum_drv_from_paths`` and then queries the indented
dependency tree of that sum-root. Both steps live here so ``cli.py``
can stay focused on argparse + dispatch.
"""

from __future__ import annotations

import subprocess

from .make_sum_drv import make_sum_drv_from_paths


def _build_sum_drv(
    *,
    binary: str,
    bash_path: str,
    toolchain_drvs: list[str],
    variant_drvs: list[str],
) -> str:
    """Assemble a sum-root .drv via ``make_sum_drv_from_paths``."""
    if not toolchain_drvs:
        raise SystemExit("--toolchain-drvs must list at least one drv")
    if not variant_drvs:
        raise SystemExit("variants file produced no drv paths")
    return make_sum_drv_from_paths(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs={f"matrix-{binary}": variant_drvs},
    )


def _query_drv_tree(sum_drv: str) -> str:
    """``nix-store --query --tree <sum_drv>`` → decoded UTF-8 text."""
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        ["nix-store", "--query", "--tree", sum_drv],
        capture_output=True, check=False, shell=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"nix-store --query --tree {sum_drv} failed "
            f"(rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return proc.stdout.decode("utf-8", errors="replace")

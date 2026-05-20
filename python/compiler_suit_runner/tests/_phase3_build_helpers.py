"""Aggregate-build helpers for the Phase-3 live-matrix smoke test.

Split out of ``_phase3_smoke_helpers.py`` so each helper file stays
under the 300-LOC guideline. The three ``build_*`` aggregate helpers
plus ``query_tree`` are the pure subprocess wrappers around
``template_graph.make_sum_drv`` and ``nix-store --query --tree``; the
eval / discovery helpers stay in ``_phase3_smoke_helpers.py``.

``_phase3_smoke_helpers`` re-exports these symbols so existing
``from compiler_suit_runner.tests._phase3_smoke_helpers import ...``
sites keep working.
"""

from __future__ import annotations

import subprocess


def query_tree(sum_drv: str) -> str:
    """``nix-store --query --tree <sum_drv>`` -> decoded text."""
    proc = subprocess.run(
        ["nix-store", "--query", "--tree", sum_drv],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix-store --query --tree failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return proc.stdout.decode("utf-8", errors="replace")


def build_toolchain_aggregate(leaves: list[str], *, sys_name: str) -> str:
    """Phase-1 mirror: single ``toolchains`` wrapper drv."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    return make_wrapper_drv_from_paths(
        drvs=sorted(leaves), name="toolchains", system=sys_name,
    )


def build_matrix_aggregate(
    toolchain_agg: str, leaves: list[str], *, binary: str, sys_name: str,
) -> str:
    """Phase-2 mirror: ``matrix-<binary>`` wrapper drv (toolchain + leaves)."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    return make_wrapper_drv_from_paths(
        drvs=[toolchain_agg, *sorted(leaves)],
        name=f"matrix-{binary}", system=sys_name,
    )


def build_sum_drv_from_aggregates(
    toolchain_agg: str,
    matrix_aggs: dict[str, str],
    *,
    bash_path: str,
    sys_name: str,
) -> str:
    """Phase-3 mirror: ONE ``make_sum_drv_from_paths`` call."""
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_sum_drv_from_paths,
    )
    return make_sum_drv_from_paths(
        bash_path=bash_path,
        toolchain_drvs=[toolchain_agg],
        matrix_drvs={f"matrix-{b}": [agg] for b, agg in matrix_aggs.items()},
        system=sys_name,
    )

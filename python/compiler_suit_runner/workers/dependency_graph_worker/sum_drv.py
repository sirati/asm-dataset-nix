"""Sum-drv assembly + ``nix-store --query --tree`` walk.

Wraps :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
and the single ``nix-store --query --tree`` invocation that turns the
sum-drv into the indented tree text the streaming planner consumes.
"""

from __future__ import annotations

from typing import Optional

from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "build_sum_drv",
    "build_sum_drv_multi",
    "query_drv_tree",
]


def build_sum_drv(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    binary: str,
    variant_drvs: list[str],
    system: str,
) -> str:
    """Wrap :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
    for ONE binary.

    Kept for back-compat — production now goes through
    :func:`build_sum_drv_multi` so cross-binary template dedup fires
    inside a single streaming pass.
    """
    return build_sum_drv_multi(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs={f"matrix-{binary}": variant_drvs},
        system=system,
    )


def build_sum_drv_multi(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    matrix_drvs: dict[str, list[str]],
    system: str,
) -> str:
    """Wrap :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
    accepting a multi-binary ``matrix_drvs`` map.

    ``matrix_drvs`` maps a matrix wrapper name (``"matrix-<binary>"``)
    to that binary's kept variant-drv list. ``make_sum_drv_from_paths``
    issues exactly ONE ``nix-instantiate`` regardless of how many
    matrices it bundles.
    """
    # Late import: template_graph isn't a stdlib dep and a missing
    # checkout shouldn't crash module-load.
    from template_graph.make_sum_drv import make_sum_drv_from_paths  # noqa: PLC0415

    return make_sum_drv_from_paths(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs=matrix_drvs,
        system=system,
    )


def query_drv_tree(
    sum_drv: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> str:
    """``nix-store --query --tree <sum_drv>`` → decoded UTF-8 text.

    Raises :class:`RuntimeError` on non-zero exit. The output is the
    indented tree the streaming planner consumes line-by-line.
    """
    runner = run_subprocess or default_run_subprocess
    stdout, stderr, rc = runner([
        "nix-store", "--query", "--tree", sum_drv,
    ])
    if rc != 0:
        raise RuntimeError(
            f"nix-store --query --tree {sum_drv} failed (rc={rc}): "
            + stderr.decode("utf-8", errors="replace").strip()
        )
    return stdout.decode("utf-8", errors="replace")

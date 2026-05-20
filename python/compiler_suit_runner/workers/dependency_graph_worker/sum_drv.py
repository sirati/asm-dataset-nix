"""Sum-drv assembly + ``nix-store --query --tree`` walk.

Wraps :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`
and the single ``nix-store --query --tree`` invocation that turns the
sum-drv into the indented tree text the streaming planner consumes.
"""

from __future__ import annotations

import subprocess
from typing import Iterator, Optional

from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "build_sum_drv",
    "build_sum_drv_multi",
    "query_drv_tree",
    "stream_drv_tree",
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


def stream_drv_tree(sum_drv: str) -> Iterator[tuple[int, bytes, str, bool]]:
    """Yield ``(depth, drv_hash, drv_name, is_backref)`` streamed from
    ``nix-store --query --tree <sum_drv>``.

    Spawns ``nix-store``, wraps stdout with
    :func:`template_graph.tree_walker.drv_tree_stream`, and raises
    :class:`RuntimeError` on non-zero exit after stdout drains.
    Letting the planner consume tuples as they're produced overlaps
    nix-store's tree walk with template construction.
    """
    from template_graph.tree_walker import drv_tree_stream  # noqa: PLC0415

    proc = subprocess.Popen(
        ["nix-store", "--query", "--tree", sum_drv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=-1,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        yield from drv_tree_stream(proc.stdout)
    finally:
        rc = proc.wait()
        if rc != 0:
            err = proc.stderr.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"nix-store --query --tree {sum_drv} failed "
                f"(rc={rc}): " + err.strip()
            )

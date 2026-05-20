"""CLI entry point for the dependency_graph_worker package.

Invoked as ``python -m compiler_suit_runner.workers.dependency_graph_worker``;
the CLI surface mirrors what the runner framework's ``suit_task``
subprocess assembler expects.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
from typing import Optional


__all__ = [
    "build_cli_parser",
    "parse_task_id_mappings",
    "main",
]


logger = logging.getLogger("compiler_suit_runner.dependency_graph_worker")


def build_cli_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser; factored so tests can introspect."""
    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.dependency_graph_worker",
        description=(
            "Primary-side dependency_graph worker. Imports per-binary"
            " matrix_eval archives, runs the template_graph streaming"
            " planner, writes _dependency_graph.pkl (plus a"
            " _dependency_graph_summary.txt companion)."
        ),
    )
    parser.add_argument(
        "--matrix-eval-out-dir",
        type=str,
        required=True,
        help=(
            "Directory containing the per-binary <binary>.nix-archive"
            " files (typically <shared_fs>/out/_matrix_eval/)."
        ),
    )
    parser.add_argument(
        "--flake-ref",
        type=str,
        default=".",
        help="Flake reference (currently informational; sum-drv uses paths).",
    )
    parser.add_argument(
        "--bash-path",
        type=str,
        required=True,
        help=(
            "Realised bash store path (e.g. /nix/store/...-bash-5.2-p15);"
            " passed to make_sum_drv_from_paths."
        ),
    )
    parser.add_argument(
        "--toolchain-drv",
        action="append",
        default=[],
        help=(
            "Toolchain .drv path. Repeatable. Required: every active"
            " toolchain whose closure the matrix touches must be listed."
        ),
    )
    parser.add_argument(
        "--toolchain-task-id",
        action="append",
        default=[],
        help=(
            "Toolchain task id mapping in the form"
            " '<hash>-<name>=<task_id>'. Repeatable. Optional but"
            " recommended — wires phase-1 build_compilers ids into"
            " phase-4 variant depends_on."
        ),
    )
    parser.add_argument(
        "--system",
        "--sys-name",
        dest="sys_name",
        type=str,
        default="x86_64-linux",
        help=(
            "Target system attr (default x86_64-linux). The submitter "
            "threads its --system flag to this worker via argv."
        ),
    )
    return parser


def parse_task_id_mappings(raw: list[str]) -> dict[str, str]:
    """Turn ``["abc-foo.drv=task_id_1", ...]`` into ``{ident: task_id}``."""
    out: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            logger.warning(
                "ignoring malformed --toolchain-task-id %r (missing '=')",
                entry,
            )
            continue
        ident, _, task_id = entry.partition("=")
        ident = ident.strip()
        task_id = task_id.strip()
        if not ident or not task_id:
            continue
        out[ident] = task_id
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    Exits 0 on success, nonzero on the first binary's planning failure.
    Configures stdlib logging (INFO) to stderr; production callers may
    redirect via standard shell facilities.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    from .errors import DependencyGraphWorkerError  # noqa: PLC0415
    from .run import run_dependency_graph_task  # noqa: PLC0415

    matrix_eval_out_dir = pathlib.Path(args.matrix_eval_out_dir)
    toolchain_task_ids = parse_task_id_mappings(args.toolchain_task_id)

    try:
        result = run_dependency_graph_task(
            matrix_eval_out_dir=matrix_eval_out_dir,
            bash_path=args.bash_path,
            toolchain_drvs=list(args.toolchain_drv),
            toolchain_task_ids=toolchain_task_ids,
            sys_name=args.sys_name,
        )
    except DependencyGraphWorkerError as exc:
        logger.error("dependency_graph_worker failed: %s", exc)
        return 2
    except Exception:  # noqa: BLE001 - any uncaught is fatal here
        logger.exception("dependency_graph_worker crashed unexpectedly")
        return 1

    logger.info(
        "dependency_graph_worker ok: wrote %s (%d binaries, %d descriptors)"
        " in %.2fs",
        result.output_path, result.binary_count,
        result.descriptor_count, result.duration_seconds,
    )
    return 0

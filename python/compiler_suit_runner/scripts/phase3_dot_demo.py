"""Phase 3 verification driver — in-process dot generation via the
production code path.

Pipeline (no dynamic_runner framework): ``enumerate_toolchains_only``
→ ``make_matrix_eval_header`` → ``run_eval_task`` →
``run_dependency_graph_task`` → ``plan_from_drv_tree`` →
``save_binary_merged_dot``. Writes ``/tmp/phase3-dots/<binary>-merged.dot``
per binary and prints the wall time so the matrix-aggregate refactor's
"trivially fast phase 3" guarantee is regression-testable.

Run via ``python -m compiler_suit_runner.scripts.phase3_dot_demo``.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile
import time
from typing import Optional

from compiler_suit_runner.preflight import (
    enumerate_toolchains_only,
    export_toolchain_archive,
)
from compiler_suit_runner.streamed_spawn import LocalMessageSink
from compiler_suit_runner.workers.dependency_graph_worker import (
    build_sum_drv_multi,
    run_dependency_graph_task,
    stream_drv_tree,
)

from compiler_suit_runner.scripts._phase3_dot_helpers import (
    eval_all_binaries,
    resolve_bash_path,
)


DEFAULT_BINARIES: tuple[str, ...] = ("hello", "busybox")
DEFAULT_ARCHS: tuple[str, ...] = ("x86_64", "aarch64")
DEFAULT_SYS_NAME = "x86_64-linux"
DEFAULT_SAMPLE_SIZE = 2
DEFAULT_SAMPLE_SEED = "phase3-smoke-42"
DEFAULT_OUTPUT_DIR = pathlib.Path("/tmp/phase3-dots")


def _flake_root() -> pathlib.Path:
    """Repo root, walked from this module's location."""
    return pathlib.Path(__file__).resolve().parents[3]


def _render_dot(
    *,
    binary: str,
    matrix_agg: str,
    bash_path: str,
    toolchain_aggregate_drv: str,
    sys_name: str,
    out_path: pathlib.Path,
) -> int:
    """Stream-plan per-binary sum-drv tree, emit merged dot, return size.
    A second ``plan_from_drv_tree`` pass (after the descriptor planning)
    is needed because the renderer wants ``meta_templates`` keys the
    descriptor list does not carry; the cost is wall-negligible on a
    warm tree."""
    from template_graph.dot import save_binary_merged_dot
    from template_graph.streaming import plan_from_drv_tree

    sum_drv = build_sum_drv_multi(
        bash_path=bash_path,
        toolchain_drvs=[toolchain_aggregate_drv],
        matrix_drvs={f"matrix-{binary}": [matrix_agg]},
        system=sys_name,
    )
    result = plan_from_drv_tree(stream_drv_tree(sum_drv), lax=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    save_binary_merged_dot(result, binary, str(out_path))
    return out_path.stat().st_size


def run_demo(
    *,
    binaries: list[str],
    archs: list[str],
    sample_size: int,
    sample_seed: str,
    sys_name: str,
    output_dir: pathlib.Path,
    flake_ref: str,
) -> dict:
    """End-to-end pipeline; returns summary (wall, sizes, counts)."""
    root = _flake_root()
    start = time.perf_counter()

    _pairs, _drv_paths, toolchain_aggregate_drv = enumerate_toolchains_only(
        flake_ref, sys_name=sys_name, archs=archs,
    )
    if not toolchain_aggregate_drv:
        raise RuntimeError(
            "phase3-dot-demo: enumerate_toolchains_only returned an "
            "empty toolchain_aggregate_drv (no toolchain leaves "
            f"resolved for archs={archs}, sys={sys_name})"
        )
    bash_path = resolve_bash_path()

    with tempfile.TemporaryDirectory(prefix="phase3-dot-demo-") as tmp_str:
        archive_dir = pathlib.Path(tmp_str)
        # Point the eval worker's publish staging root into the demo's
        # tmp dir (the container default /app/out-tmp does not exist on
        # a dev box); the _NullTask publish stand-in then moves staged
        # archives onto ``archive_dir``. Saved + restored so the demo
        # doesn't leak a dangling tmp path into the calling process
        # (e.g. an in-process test runner).
        saved_publish_root = os.environ.get("DYNRUNNER_PUBLISH_SRC_ROOT")
        os.environ["DYNRUNNER_PUBLISH_SRC_ROOT"] = str(
            archive_dir / "_publish-stage"
        )
        try:
            # The submitter half of toolchain dedup: produce the shared
            # ``toolchains.drv.archive`` BEFORE the evals (production
            # uploads it to the gateway's out/_matrix_eval/); the eval
            # workers CONSUME it and export only the per-binary diff.
            export_toolchain_archive(toolchain_aggregate_drv, archive_dir)
            matrix_aggregates = eval_all_binaries(
                binaries=binaries, archs=archs, sys_name=sys_name,
                sample_size=sample_size, sample_seed=sample_seed,
                toolchain_aggregate_drv=toolchain_aggregate_drv,
                archive_dir=archive_dir, flake_ref=flake_ref, root=root,
            )
        finally:
            if saved_publish_root is None:
                os.environ.pop("DYNRUNNER_PUBLISH_SRC_ROOT", None)
            else:
                os.environ["DYNRUNNER_PUBLISH_SRC_ROOT"] = saved_publish_root

        # Phase 3: production code path (import archives, build sum-drv,
        # stream-plan, write descriptors) — per-binary dispatch mirrors
        # the framework's dependency_graph task fan-out. The worker
        # streams descriptors via ``task.send_message``; the demo runs
        # with no framework primary, so a LocalMessageSink counts and
        # discards them (the demo consumes the returned result instead).
        print(
            "phase3-dot-demo: local run, no framework primary — "
            "streamed spawn messages are discarded"
        )
        sink = LocalMessageSink()
        total_descriptors = 0
        total_binaries = 0
        for binary, matrix_agg in matrix_aggregates.items():
            dep_result = run_dependency_graph_task(
                task=sink,
                matrix_eval_out_dir=archive_dir,
                bash_path=bash_path,
                toolchain_aggregate_drv=toolchain_aggregate_drv,
                binary=binary,
                matrix_drv=matrix_agg,
                sys_name=sys_name,
            )
            total_descriptors += dep_result.descriptor_count
            total_binaries += dep_result.binary_count

        dot_sizes: dict[str, int] = {}
        for binary, matrix_agg in matrix_aggregates.items():
            out_path = output_dir / f"{binary}-merged.dot"
            dot_sizes[binary] = _render_dot(
                binary=binary, matrix_agg=matrix_agg,
                bash_path=bash_path,
                toolchain_aggregate_drv=toolchain_aggregate_drv,
                sys_name=sys_name, out_path=out_path,
            )

    wall = time.perf_counter() - start
    return {
        "wall_seconds": wall,
        "dot_sizes": dot_sizes,
        "binaries": list(binaries),
        "descriptor_count": total_descriptors,
        "binary_count": total_binaries,
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3 verification driver — emit per-binary "
                    "merged dot graphs via the production code path.",
    )
    p.add_argument("--binary", action="append", dest="binaries",
                   help="repeatable; default: hello, busybox")
    p.add_argument("--arch", action="append", dest="archs",
                   help="repeatable; default: x86_64, aarch64")
    p.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    p.add_argument("--sample-seed", default=DEFAULT_SAMPLE_SEED)
    p.add_argument("--sys", default=DEFAULT_SYS_NAME, dest="sys_name")
    p.add_argument("--output-dir", type=pathlib.Path,
                   default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--flake-ref", default=str(_flake_root()))
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    binaries = list(args.binaries) if args.binaries else list(DEFAULT_BINARIES)
    archs = list(args.archs) if args.archs else list(DEFAULT_ARCHS)
    summary = run_demo(
        binaries=binaries, archs=archs,
        sample_size=args.sample_size, sample_seed=args.sample_seed,
        sys_name=args.sys_name, output_dir=args.output_dir,
        flake_ref=args.flake_ref,
    )
    print(
        f"phase3-dot-demo: wall={summary['wall_seconds']:.2f}s "
        f"binaries={'/'.join(summary['binaries'])} "
        f"descriptors={summary['descriptor_count']}"
    )
    for binary, size in sorted(summary["dot_sizes"].items()):
        out_path = args.output_dir / f"{binary}-merged.dot"
        print(f"  {binary}: {size} bytes -> {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via __main__
    sys.exit(main())

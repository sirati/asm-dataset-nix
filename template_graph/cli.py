"""CLI: two modes.

  run  <variants_file>              — unconstrained walk; silent.
  debug --template-from L1
        [--template-from L2]
        --failing-variant LF
        --variants <file>           — verbose walk: build template from
                                       the anchor(s), then cowalk the
                                       failing variant. Streaming-mode
                                       violations are logged to stderr.

Both modes route through ``template_graph.streaming.plan_from_tree_streaming``:
we wrap the variants + toolchain drvs into a single sum-root via
``template_graph.make_sum_drv.make_sum_drv_from_paths``, run
``nix-store --query --tree`` on it, and feed the resulting indented
tree to the streaming planner. No ``nix derivation show`` calls.

The variants_file has lines ``<label><TAB><drv_path>`` (comments
``#...`` and blanks ignored). The ``--toolchain-drvs`` argument
takes a flat list of toolchain drv paths, one per line. ``--bash-path``
is the realised bash store path the sum-root references for its
builder context.

Input-loading helpers (variants/toolchain parsing, ``--binary``
derivation, streaming-label derivation) live in
``template_graph.cli_io``. Sum-root assembly + ``nix-store --query
--tree`` live in ``template_graph.cli_sumdrv``.

Caveats vs. legacy:

  * The variants-file ``<label>`` value is purely descriptive: the
    streaming planner re-derives a ``<comp>-<opt>`` label from each
    variant's drv name. For ``debug --failing-variant LF`` we look LF
    up in the variants file to find its drv path, then match against
    the streaming-derived label of that drv.

  * Per-node trace logging from the legacy ``debug`` mode is no longer
    produced — the streaming planner emits ``violations`` (in lax
    mode) and raises ``TemplateGraphAssertError`` on hard failures,
    but doesn't narrate every node visit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cli_io import (
    _derive_binary,
    _derive_streaming_label,
    _load_toolchain_drvs,
    _load_variants,
)
from .cli_sumdrv import _build_sum_drv, _query_drv_tree
from .core import TemplateGraphAssertError
from .cowalk import format_histogram, template_shape_histogram
from .streaming import plan_from_tree_streaming


def _stderr_logger(msg: str) -> None:
    print(msg, file=sys.stderr)


def _print_run_summary(result: dict) -> None:
    print(f"templates: {len(result['templates'])}")
    for key, arr in result["variant_arrays"].items():
        print(
            f"  template={key[0]} arch={key[1]} variants={len(arr.variants)}"
        )
    common = result["common_deps_per_arch_template"]
    common_count = sum(
        sum(1 for v in cls.values() if v == "common_dep")
        for cls in common.values()
    )
    print(f"common-dep nodes: {common_count}")


def cmd_run(args: argparse.Namespace) -> int:
    variants = _load_variants(args.variants)
    toolchain_drvs = _load_toolchain_drvs(args.toolchain_drvs)
    binary = _derive_binary(variants, args.binary)
    variant_drvs = list(variants.values())

    sum_drv = _build_sum_drv(
        binary=binary,
        bash_path=args.bash_path,
        toolchain_drvs=toolchain_drvs,
        variant_drvs=variant_drvs,
    )
    tree_text = _query_drv_tree(sum_drv)
    try:
        result = plan_from_tree_streaming(tree_text)
    except TemplateGraphAssertError as e:
        print(
            f"\n=== TEMPLATE GRAPH ASSERTION FAILED ===\n{e}\n",
            file=sys.stderr,
        )
        print(
            "Hint: re-run with `debug --template-from <anchor1> "
            "[--template-from <anchor2>] --failing-variant <failing> "
            f"--variants {args.variants}` for a verbose trace.",
            file=sys.stderr,
        )
        return 1
    _print_run_summary(result)
    if args.histogram:
        print()
        print("=== template shape histogram ===")
        print(format_histogram(template_shape_histogram(result)))
    return 0


def cmd_debug(args: argparse.Namespace) -> int:
    template_from: list[str] = args.template_from
    failing: str = args.failing_variant
    variants = _load_variants(args.variants)
    toolchain_drvs = _load_toolchain_drvs(args.toolchain_drvs)
    if not (1 <= len(template_from) <= 2):
        raise SystemExit("--template-from must be passed 1 or 2 times")
    for lbl in (*template_from, failing):
        if lbl not in variants:
            raise SystemExit(f"label {lbl!r} not in variants file")

    logger = _stderr_logger
    logger(f"[debug] template anchors: {template_from}")
    logger(f"[debug] failing variant:  {failing!r}")

    # Subset to anchor(s) + failing variant. The streaming planner
    # picks a calibration pair from the FIRST two variants of each
    # arch, so we order anchor(s) first and failing last.
    subset_labels = list(template_from) + [failing]
    subset_drvs = [variants[l] for l in subset_labels]
    binary = _derive_binary(
        {l: variants[l] for l in subset_labels}, args.binary,
    )

    sum_drv = _build_sum_drv(
        binary=binary,
        bash_path=args.bash_path,
        toolchain_drvs=toolchain_drvs,
        variant_drvs=subset_drvs,
    )
    tree_text = _query_drv_tree(sum_drv)

    # Streaming-derived label for ``failing`` — used to flag any
    # violation that names the failing variant.
    failing_stream_label = _derive_streaming_label(variants[failing])
    logger(
        f"[debug] failing variant streaming label: "
        f"{failing_stream_label!r}"
    )

    try:
        result = plan_from_tree_streaming(tree_text, lax=True)
    except TemplateGraphAssertError as e:
        print(
            f"\n=== TEMPLATE GRAPH ASSERTION FAILED ===\n{e}\n",
            file=sys.stderr,
        )
        return 1

    violations = result.get("violations", [])
    if not violations:
        logger("[debug] streaming planner completed with no violations.")
        logger(f"[debug] templates: {len(result['templates'])}")
        for key, arr in result["variant_arrays"].items():
            logger(
                f"[debug]   template={key[0]} arch={key[1]} "
                f"variants={arr.variants}"
            )
        return 0

    logger(f"[debug] {len(violations)} violation(s) reported:")
    for v in violations:
        logger(f"[debug]   {v}")
    # Any violation whose label matches the failing variant's streaming
    # label is the strong signal; surface a clear non-zero exit then.
    failing_hits = [
        v for v in violations
        if v.get("label") == failing_stream_label
    ]
    if failing_hits:
        print(
            f"\n=== STREAMING VIOLATIONS NAMING FAILING VARIANT "
            f"{failing_stream_label!r} ===",
            file=sys.stderr,
        )
        for v in failing_hits:
            print(f"  {v}", file=sys.stderr)
        return 1
    logger(
        "[debug] no violation names the failing variant directly — "
        "planner saw inconsistencies but absorbed them in lax mode."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="template_graph",
        description=(
            "Run the streaming template-graph planner on a set of "
            "variant .drv paths."
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Unconstrained walk; silent.")
    run.add_argument("variants", type=Path, help="variants file")
    run.add_argument("--toolchain-drvs", type=Path, default=None)
    run.add_argument(
        "--binary", default=None,
        help="matrix binary name (default: derive from first variant drv)",
    )
    run.add_argument(
        "--bash-path", required=True,
        help="realised bash store path (e.g. /nix/store/...-bash-5.2)",
    )
    run.add_argument(
        "--histogram", action="store_true",
        help=(
            "print a cross-binary template shape histogram after the "
            "run summary (Phase 4 diagnostic; counts shapes by arch + "
            "binary)."
        ),
    )
    run.set_defaults(func=cmd_run)

    debug = sub.add_parser(
        "debug", help="Verbose walk of one or two anchors + failing variant."
    )
    debug.add_argument(
        "--template-from", action="append", required=True,
        dest="template_from",
    )
    debug.add_argument("--failing-variant", required=True)
    debug.add_argument("--variants", type=Path, required=True)
    debug.add_argument("--toolchain-drvs", type=Path, default=None)
    debug.add_argument(
        "--binary", default=None,
        help="matrix binary name (default: derive from first variant drv)",
    )
    debug.add_argument(
        "--bash-path", required=True,
        help="realised bash store path (e.g. /nix/store/...-bash-5.2)",
    )
    debug.set_defaults(func=cmd_debug)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

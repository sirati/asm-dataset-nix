"""CLI: two modes.

  run  <variants_file>              — unconstrained walk; silent.
  debug --template-from L1
        [--template-from L2]
        --failing-variant LF
        --variants <file>           — verbose walk: build template from L1,
                                       optionally cowalk L2 to lock it as
                                       second anchor, then cowalk LF. Every
                                       node visit is logged to stderr.

Both modes feed a streaming ``read_drv_record`` callable into the
algorithm — one ``nix derivation show`` call per drv, no closure
dict ever held.

The variants_file has lines ``<label><TAB><drv_path>`` (comments
``#...`` and blanks ignored). The ``--toolchain-drvs`` argument
takes a flat list of toolchain drv paths, one per line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import (
    TemplateGraphAssertError,
    VariantArray,
    build_template_from_closure,
    cowalk_and_index,
    assert_arch_invariants,
    plan_phase1_graph,
)
from .drv_io import read_drv_record


def _stderr_logger(msg: str) -> None:
    print(msg, file=sys.stderr)


def _noop_logger(_msg: str) -> None:
    return None


def _read_text_lines(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _load_toolchain_drvs(path: Path | None) -> set[str]:
    if path is None:
        return set()
    return set(_read_text_lines(path))


def _load_variants(path: Path) -> dict[str, str]:
    """Return ``{label: drv_path}``."""
    out: dict[str, str] = {}
    for line in _read_text_lines(path):
        if "\t" not in line:
            raise SystemExit(f"variants file line lacks TAB: {line!r}")
        label, drv = line.split("\t", 1)
        out[label.strip()] = drv.strip()
    return out


def _group_by_arch(
    label_to_drv: dict[str, str],
) -> dict[str, list[tuple[str, str]]]:
    by_arch: dict[str, list[tuple[str, str]]] = {}
    for label, drv in sorted(label_to_drv.items()):
        if "__" not in label:
            raise SystemExit(
                f"label {label!r} doesn't match <arch>__<suffix>"
            )
        arch = label.split("__", 1)[0]
        by_arch.setdefault(arch, []).append((label, drv))
    return by_arch


def cmd_run(args: argparse.Namespace) -> int:
    variants = _load_variants(args.variants)
    by_arch = _group_by_arch(variants)
    toolchain_drvs = _load_toolchain_drvs(args.toolchain_drvs)
    binary = args.binary or "<binary>"
    variants_by_binary_arch = {
        (binary, arch): vs for arch, vs in by_arch.items()
    }
    try:
        result = plan_phase1_graph(
            variants_by_binary_arch=variants_by_binary_arch,
            toolchain_drvs=toolchain_drvs,
            get_record=read_drv_record,
            logger=_noop_logger,
        )
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
    return 0


def _cowalk_anchor(
    template, arr, label, drv, toolchain_drvs, logger
) -> None:
    v_pos = len(arr.variants)
    arr.variants.append(label)
    for row in arr.hashes:
        row.append(None)
    for n in template.nodes:
        n.visit_flag = False
    cowalk_and_index(
        template=template,
        drv_path=drv,
        get_record=read_drv_record,
        arr=arr,
        variant_index=v_pos,
        toolchain_drvs=toolchain_drvs,
        current_variant_label=label,
        logger=logger,
    )


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

    anchor1_label = template_from[0]
    anchor1_drv = variants[anchor1_label]
    logger(f"[debug] building template from {anchor1_label!r} ({anchor1_drv})")
    template = build_template_from_closure(
        root_drv=anchor1_drv,
        get_record=read_drv_record,
        toolchain_drvs=toolchain_drvs,
        logger=logger,
        built_from_label=anchor1_label,
    )
    logger(f"[debug] template has {len(template.nodes)} nodes")

    arch = (
        anchor1_label.split("__", 1)[0]
        if "__" in anchor1_label
        else "?"
    )
    arr = VariantArray(
        template_id=0,
        arch=arch,
        variants=[],
        hashes=[[] for _ in template.nodes],
    )

    logger(f"[debug] cowalking anchor1 {anchor1_label!r}")
    try:
        _cowalk_anchor(
            template, arr, anchor1_label, anchor1_drv,
            toolchain_drvs, logger,
        )
    except TemplateGraphAssertError as e:
        print(f"\n=== ASSERTION ON ANCHOR1 ===\n{e}\n", file=sys.stderr)
        return 1

    if len(template_from) == 2:
        a2_label = template_from[1]
        a2_drv = variants[a2_label]
        logger(f"[debug] cowalking anchor2 {a2_label!r}")
        try:
            _cowalk_anchor(
                template, arr, a2_label, a2_drv,
                toolchain_drvs, logger,
            )
        except TemplateGraphAssertError as e:
            print(
                f"\n=== ASSERTION ON ANCHOR2 ===\n{e}\n",
                file=sys.stderr,
            )
            return 1
        template.template_built_from.append(a2_label)

    f_drv = variants[failing]
    logger(f"[debug] cowalking FAILING variant {failing!r} ({f_drv})")
    try:
        _cowalk_anchor(
            template, arr, failing, f_drv, toolchain_drvs, logger,
        )
        logger("[debug] cowalk OK — running end-of-arch invariants")
        assert_arch_invariants(arr, template)
        logger("[debug] invariants PASS — algorithm sees no error here.")
        return 0
    except TemplateGraphAssertError as e:
        print(
            f"\n=== TEMPLATE GRAPH ASSERTION FAILED ===\n{e}\n",
            file=sys.stderr,
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="template_graph",
        description="Run the Phase-1 template-graph algorithm on .drv files.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Unconstrained walk; silent.")
    run.add_argument("variants", type=Path, help="variants file")
    run.add_argument("--toolchain-drvs", type=Path, default=None)
    run.add_argument("--binary", default=None, help="binary label (default '<binary>')")
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
    debug.set_defaults(func=cmd_debug)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

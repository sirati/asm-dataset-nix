"""Build a "sum" .drv that bundles toolchains + per-binary matrices.

Re-usable tool: any caller that wants ONE drv path to feed into the
template-graph algorithm can construct it from a list of toolchain
attr paths plus one or more matrices (each a (name, variant-attr-list)).

Output is a single ``/nix/store/...-<rootName>.drv`` whose
``inputDrvs`` carries:

  - a ``<toolchainsName>`` wrapper (default ``toolchains``), whose
    own inputDrvs is the toolchain attr list;
  - one ``<matrix_name>`` wrapper per matrix, each carrying its
    variant attrs.

No build is invoked — only ``nix-instantiate``. Library + CLI.

Library usage::

    from template_graph.make_sum_drv import make_sum_drv
    drv = make_sum_drv(
        flake_ref="git+file:///repo",
        bash_attr="inputs.nixpkgs.legacyPackages.x86_64-linux.bash",
        toolchain_attrs=[ ... ],
        matrices={
            "matrix-hello":   [ ... variant attrs ... ],
            "matrix-busybox": [ ... ],
        },
    )

CLI usage::

    python3 -m template_graph.make_sum_drv \\
        --flake "git+file://$PWD" \\
        --bash-attr 'inputs.nixpkgs.legacyPackages.x86_64-linux.bash' \\
        --toolchains-file toolchains.list \\
        --matrix matrix-hello=hello.list \\
        --matrix matrix-busybox=busybox.list \\
        --out sum_drv.txt

Each ``--matrix NAME=FILE`` reads ``FILE`` (one flake attr path per
line) and registers it as the matrix wrapper named ``NAME``. Plain
``--toolchain-attr`` / ``--matrix-variant-attr`` arguments are also
accepted; see ``--help``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SUM_DRV_NIX = Path(__file__).parent / "sum_drv.nix"


def _q(s: str) -> str:
    """Render a Python string as a nix string literal (double-quoted)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _join_attrs(attrs: list[str], *, tolerate_missing: bool = False) -> str:
    """Render an attr list as ``[ flake.X flake.Y ... ]``.

    With ``tolerate_missing=True`` each access is wrapped in a thunk
    and run through ``filterValid`` (defined in the prelude) so
    attribute-access errors are silently dropped. Required when the
    caller's matrix references combos that may not all exist in the
    flake (compiler-arch validity gates, etc.).
    """
    if tolerate_missing:
        items = " ".join(f"(_: flake.{a})" for a in attrs)
        return f"(filterValid [ {items} ])"
    return "[ " + " ".join(f"flake.{a}" for a in attrs) + " ]"


def _join_matrices(
    matrices: dict[str, list[str]], *, tolerate_missing: bool = False
) -> str:
    """Render matrices as ``[ { name = "..."; drvs = [...]; } ... ]``."""
    items = []
    for name, attrs in matrices.items():
        items.append(
            "{ name = "
            + _q(name)
            + "; drvs = "
            + _join_attrs(attrs, tolerate_missing=tolerate_missing)
            + "; }"
        )
    return "[ " + " ".join(items) + " ]"


_FILTER_VALID_PRELUDE = (
    "  filterValid = thunks:\n"
    # ``deepSeq drvPath`` inside tryEval forces the derivation's
    # primary-key evaluation now, so deferred throws (e.g. nixpkgs
    # cross-compile derivations that reference a missing
    # ``stdenv.cc.targetPrefix`` for an unsupported target) are
    # caught here instead of escaping when the wrapper later does
    # ``builtins.toString`` on the list.\n"
    "    let forceOne = t:\n"
    "          builtins.tryEval (\n"
    "            let v = t null;\n"
    "            in if builtins.isAttrs v && v ? drvPath\n"
    "               then builtins.deepSeq v.drvPath v\n"
    "               else v);\n"
    "        rs = builtins.map forceOne thunks;\n"
    "    in builtins.map (r: r.value)\n"
    "         (builtins.filter (r: r.success) rs);\n"
)


def _read_list_file(path: Path | None) -> list[str]:
    if path is None:
        return []
    out: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def make_sum_drv(
    *,
    flake_ref: str,
    bash_attr: str,
    toolchain_attrs: list[str],
    matrices: dict[str, list[str]],
    root_name: str = "sum-root",
    toolchains_name: str = "toolchains",
    system: str = "x86_64-linux",
    tolerate_missing: bool = False,
    extra_nix_args: list[str] | None = None,
) -> str:
    """Run ``nix-instantiate`` on the sum-drv wrapper; return root drv path.

    Set ``tolerate_missing=True`` if the caller's attr lists may
    reference flake paths that don't exist (e.g. compiler-arch combos
    the matrix gates as invalid). Missing attrs are silently dropped.
    """
    if not toolchain_attrs:
        raise ValueError("at least one toolchain attr is required")
    if not matrices:
        raise ValueError("at least one matrix is required")
    for name, attrs in matrices.items():
        if not attrs:
            raise ValueError(f"matrix {name!r} has no variant attrs")

    prelude = _FILTER_VALID_PRELUDE if tolerate_missing else ""
    expr = (
        "let\n"
        f"  flake = builtins.getFlake {_q(flake_ref)};\n"
        f"{prelude}"
        f"in import {SUM_DRV_NIX} {{\n"
        f"  bash = flake.{bash_attr};\n"
        f"  toolchains = {_join_attrs(toolchain_attrs, tolerate_missing=tolerate_missing)};\n"
        f"  matrices = {_join_matrices(matrices, tolerate_missing=tolerate_missing)};\n"
        f"  rootName = {_q(root_name)};\n"
        f"  toolchainsName = {_q(toolchains_name)};\n"
        f"  system = {_q(system)};\n"
        f"}}\n"
    )

    # Large matrices push past ARG_MAX when the whole expression is
    # passed via ``-E``. Write to a temp file and let nix-instantiate
    # read it directly. The file is removed after the call.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".nix", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(expr)
        expr_file = tf.name
    try:
        argv = [
            "nix-instantiate",
            "--extra-experimental-features",
            "flakes",
            expr_file,
        ]
        if extra_nix_args:
            argv.extend(extra_nix_args)
        proc = subprocess.run(argv, capture_output=True, check=False)
    finally:
        try:
            os.unlink(expr_file)
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError(
            "nix-instantiate failed:\n"
            + proc.stderr.decode("utf-8", errors="replace")
            + "\n\nExpression was (truncated to 2000 chars):\n"
            + expr[:2000]
            + ("\n...[truncated]" if len(expr) > 2000 else "")
        )
    drv_path = proc.stdout.decode("utf-8", errors="replace").strip()
    lines = [l for l in drv_path.splitlines() if l.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected one drv path from nix-instantiate, got {len(lines)}:\n"
            + drv_path
        )
    return lines[0].strip()


def _parse_matrix_arg(arg: str) -> tuple[str, Path]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            "expected NAME=PATH, e.g. matrix-hello=variants/hello.list"
        )
    name, sep, path = arg.partition("=")
    name = name.strip()
    path = path.strip()
    if not name or not path:
        raise argparse.ArgumentTypeError("NAME and PATH both required")
    return name, Path(path)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make_sum_drv")
    ap.add_argument("--flake", required=True,
                    help="flake reference (e.g. git+file://...)")
    ap.add_argument("--bash-attr", required=True,
                    help="flake attr path for a bash derivation "
                    "(e.g. inputs.nixpkgs.legacyPackages.x86_64-linux.bash)")
    ap.add_argument("--toolchain-attr", action="append", default=[],
                    help="flake attr for a toolchain (repeatable)")
    ap.add_argument("--toolchains-file", type=Path, default=None,
                    help="file with one toolchain attr per line; "
                    "appended to --toolchain-attr")
    ap.add_argument(
        "--matrix", action="append", default=[],
        type=_parse_matrix_arg,
        help="NAME=FILE — register one matrix wrapper called NAME "
        "whose variant attrs are read one per line from FILE. "
        "Repeatable.",
    )
    ap.add_argument("--root-name", default="sum-root")
    ap.add_argument("--toolchains-name", default="toolchains")
    ap.add_argument("--system", default="x86_64-linux")
    ap.add_argument(
        "--tolerate-missing", action="store_true",
        help="silently drop attr paths the flake doesn't expose "
        "(e.g. invalid arch/compiler combinations).",
    )
    ap.add_argument("--out", type=Path, default=None,
                    help="write the root drv path to this file "
                    "(default: stdout)")
    args = ap.parse_args(argv)

    toolchain_attrs = (
        list(args.toolchain_attr) + _read_list_file(args.toolchains_file)
    )

    matrices: dict[str, list[str]] = {}
    for name, path in args.matrix:
        if name in matrices:
            sys.stderr.write(f"warning: matrix {name!r} declared twice\n")
        matrices[name] = _read_list_file(path)

    if not matrices:
        ap.error("at least one --matrix NAME=FILE is required")

    drv = make_sum_drv(
        flake_ref=args.flake,
        bash_attr=args.bash_attr,
        toolchain_attrs=toolchain_attrs,
        matrices=matrices,
        root_name=args.root_name,
        toolchains_name=args.toolchains_name,
        system=args.system,
        tolerate_missing=args.tolerate_missing,
    )

    if args.out:
        args.out.write_text(drv + "\n")
    else:
        print(drv)
    return 0


if __name__ == "__main__":
    sys.exit(_main())

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
WRAPPER_DRV_NIX = Path(__file__).parent / "wrapper_drv.nix"


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


def _run_nix_instantiate(
    expr: str,
    *,
    extra_nix_args: list[str] | None,
    with_flakes: bool,
) -> str:
    """Write ``expr`` to a temp .nix file, run ``nix-instantiate``, return
    the single .drv path it prints. Large matrices push past ARG_MAX when
    passed via ``-E``, so we always go via a file.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".nix", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(expr)
        expr_file = tf.name
    try:
        argv = ["nix-instantiate"]
        if with_flakes:
            argv += ["--extra-experimental-features", "flakes"]
        argv.append(expr_file)
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
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    lines = [l for l in out.splitlines() if l.strip()]
    if len(lines) != 1:
        raise RuntimeError(
            f"expected one drv path from nix-instantiate, got {len(lines)}:\n"
            + out
        )
    return lines[0].strip()


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
    return _run_nix_instantiate(
        expr, extra_nix_args=extra_nix_args, with_flakes=True,
    )


def _shallow_ref(drv_path: str) -> str:
    """Render a single drv path as an ``appendContext``-wrapped empty
    string carrying the drv's shallow ``out`` output context."""
    return (
        '(builtins.appendContext "" { '
        + _q(drv_path)
        + ' = { outputs = [ "out" ]; }; })'
    )


def _join_drvs(paths: list[str]) -> str:
    return "[ " + " ".join(_shallow_ref(p) for p in paths) + " ]"


def _join_matrix_drvs(matrix_drvs: dict[str, list[str]]) -> str:
    items = []
    for name, paths in matrix_drvs.items():
        items.append(
            "{ name = " + _q(name) + "; drvs = " + _join_drvs(paths) + "; }"
        )
    return "[ " + " ".join(items) + " ]"


_TOOLCHAIN_AGGREGATE_MARKER = "toolchain"
_MATRIX_AGGREGATE_MARKER = "matrix"

_SMALL_EVAL_FORBIDDEN_MSG = (
    "make_sum_drv_from_paths rejects {role} path {path!r}: basename "
    "{basename!r} does not contain the expected aggregate marker "
    "{marker!r}. Callers MUST pass the aggregate wrapper drv paths "
    "(the single 'toolchains' drv containing every compiler wrapper, "
    "and one 'matrix-<binary>' drv per binary containing every variant "
    "leaf) — NOT the leaf drvs themselves. Constructing the wrappers "
    "from leaf lists requires a per-leaf flake evaluation, which "
    "re-evaluates the entire flake closure per leaf and takes minutes "
    "instead of seconds on real matrices. The expected contract: ONE "
    "bulk evaluator pass (e.g. ``nix-eval-jobs --force-recurse``) "
    "exposes the aggregate wrappers, and those paths are fed here."
)


def _validate_marker(path: str, *, marker: str, role: str) -> None:
    basename = path.rsplit("/", 1)[-1]
    if marker not in basename:
        raise ValueError(_SMALL_EVAL_FORBIDDEN_MSG.format(
            role=role, path=path, basename=basename, marker=marker,
        ))


def make_sum_drv_from_paths(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    matrix_drvs: dict[str, list[str]],
    root_name: str = "sum-root",
    toolchains_name: str = "toolchains",
    system: str = "x86_64-linux",
    extra_nix_args: list[str] | None = None,
) -> str:
    """Path-based variant of :func:`make_sum_drv`: takes already-instantiated
    raw store paths (toolchain + matrix .drvs + a bash store path) and
    assembles the same layered sum-root via ``builtins.appendContext`` —
    no flake eval, no re-instantiation of the inputs.

    Validates that every ``toolchain_drvs`` entry's basename contains
    ``toolchain`` and every ``matrix_drvs`` entry's basename contains
    ``matrix``. If a caller hands in leaf drvs (e.g. ``-elf-folder.drv``
    variant leaves or ``-wrapper-`` compiler-wrapper leaves), raise
    ``ValueError`` — the contract is that these are AGGREGATE wrapper
    drv paths sourced from one bulk evaluator pass; constructing the
    wrappers from leaf lists is the slow per-leaf flake-eval pattern
    that this function exists to forbid.
    """
    if not toolchain_drvs:
        raise ValueError("at least one toolchain drv path is required")
    if not matrix_drvs:
        raise ValueError("at least one matrix is required")
    for tc in toolchain_drvs:
        _validate_marker(
            tc, marker=_TOOLCHAIN_AGGREGATE_MARKER, role="toolchain",
        )
    for name, paths in matrix_drvs.items():
        if not paths:
            raise ValueError(f"matrix {name!r} has no drv paths")
        for p in paths:
            _validate_marker(
                p, marker=_MATRIX_AGGREGATE_MARKER, role=f"matrix-{name}",
            )

    expr = (
        f"import {SUM_DRV_NIX} {{\n"
        f"  bash = builtins.storePath {_q(bash_path)};\n"
        f"  toolchains = {_join_drvs(toolchain_drvs)};\n"
        f"  matrices = {_join_matrix_drvs(matrix_drvs)};\n"
        f"  rootName = {_q(root_name)};\n"
        f"  toolchainsName = {_q(toolchains_name)};\n"
        f"  system = {_q(system)};\n"
        f"}}\n"
    )
    return _run_nix_instantiate(
        expr, extra_nix_args=extra_nix_args, with_flakes=False,
    )


def make_wrapper_drv_from_paths(
    *,
    drvs: list[str],
    name: str,
    system: str = "x86_64-linux",
    extra_nix_args: list[str] | None = None,
) -> str:
    """Build an aggregate wrapper derivation at runtime via nix-instantiate.

    Calls ``template_graph/wrapper_drv.nix`` with the supplied drv
    paths and returns the resulting .drv path. The wrapper itself is a
    trivial ``bash -c true`` derivation; its only purpose is to carry
    the supplied drv paths as inputDrvs so ``nix-store --export``
    follows them transitively and ``nix-store --query --tree`` sorts
    the aggregate above its members by refcount.

    Used by phase 1 to build the ``toolchains`` aggregate drv, and by
    phase 2 (eval_worker) to build the ``matrix-<binary>`` aggregate
    drv.

    No basename validation here — this is the construction site.
    Validation belongs in :func:`make_sum_drv_from_paths` (the
    assembly site).

    Note: there is no ``bash_path`` parameter. ``wrapper_drv.nix``
    sources ``bash`` from its embedded ``pkgs`` (default nixpkgs)
    rather than taking it as a caller-supplied store path; the
    assembly site (:func:`make_sum_drv_from_paths`) is where a
    specific bash store path gets pinned into the sum-root.
    """
    if not drvs:
        raise ValueError("at least one drv path is required")
    if not name:
        raise ValueError("name is required")

    expr = (
        f"import {WRAPPER_DRV_NIX} {{\n"
        f"  drvs = {_join_drvs(drvs)};\n"
        f"  name = {_q(name)};\n"
        f"  system = {_q(system)};\n"
        f"}}\n"
    )
    return _run_nix_instantiate(
        expr, extra_nix_args=extra_nix_args, with_flakes=False,
    )


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

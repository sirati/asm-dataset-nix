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
SUM_DRV_FROM_AGGREGATES_NIX = (
    Path(__file__).parent / "sum_drv_from_aggregates.nix"
)
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
        stderr_text = proc.stderr.decode("utf-8", errors="replace")
        # Frameworks that wrap the worker in a thin RPC truncate the
        # raised exception's message; mirror it to stderr (always
        # captured into the worker / container log) so the full nix
        # diagnostic survives.
        print(
            "nix-instantiate failed; stderr follows:\n" + stderr_text,
            file=sys.stderr,
            flush=True,
        )
        debug_path = os.environ.get("CSR_NIX_DEBUG_DIR")
        if debug_path:
            try:
                os.makedirs(debug_path, exist_ok=True)
                stamp = f"{os.getpid()}-{abs(hash(expr)) & 0xffff:04x}"
                with open(
                    os.path.join(debug_path, f"nix-instantiate-{stamp}.log"),
                    "w",
                    encoding="utf-8",
                ) as fh:
                    fh.write(stderr_text)
                    fh.write("\n\n--- expression ---\n")
                    fh.write(expr)
                    fh.write("\n\n--- /etc/nix/nix.conf ---\n")
                    try:
                        with open("/etc/nix/nix.conf") as nc:
                            fh.write(nc.read())
                    except OSError as exc:
                        fh.write(f"(read failed: {exc})")
                    fh.write("\n\n--- /etc/nix/peer.conf ---\n")
                    try:
                        with open("/etc/nix/peer.conf") as pc:
                            fh.write(pc.read())
                    except OSError as exc:
                        fh.write(f"(read failed: {exc})")
                    fh.write("\n\n--- harmonia probe (localhost:5005) ---\n")
                    try:
                        from urllib.request import urlopen  # noqa: PLC0415
                        with urlopen(
                            "http://localhost:5005/nix-cache-info",
                            timeout=5,
                        ) as r:
                            fh.write(
                                f"status={r.status} headers={dict(r.headers)}\n"
                                f"body: {r.read().decode('utf-8','replace')[:500]}"
                            )
                    except Exception as exc:  # noqa: BLE001
                        fh.write(f"(probe failed: {exc!r})")
                    fh.write("\n\n--- nix store ping localhost:5005 ---\n")
                    try:
                        probe = subprocess.run(
                            ["nix", "store", "ping",
                             "--extra-experimental-features",
                             "nix-command flakes",
                             "--store", "http://localhost:5005"],
                            capture_output=True, check=False, timeout=10,
                        )
                        fh.write(
                            f"rc={probe.returncode}\n"
                            f"stdout: {probe.stdout.decode('utf-8','replace')}\n"
                            f"stderr: {probe.stderr.decode('utf-8','replace')}"
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        fh.write(f"(nix store ping failed: {exc})")
                    fh.write(
                        "\n\n--- nix path-info on toolchains.drv via "
                        "submitter ---\n",
                    )
                    try:
                        probe = subprocess.run(
                            ["nix", "path-info",
                             "--extra-experimental-features",
                             "nix-command flakes",
                             "--store", "http://localhost:5005",
                             "/nix/store/p1q09cznx974drnxf8jv6m7bcy9rzcqa-toolchains.drv"],
                            capture_output=True, check=False, timeout=15,
                        )
                        fh.write(
                            f"rc={probe.returncode}\n"
                            f"stdout: {probe.stdout.decode('utf-8','replace')}\n"
                            f"stderr: {probe.stderr.decode('utf-8','replace')}"
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        fh.write(f"(nix path-info failed: {exc})")
            except OSError:
                pass
        raise RuntimeError(
            "nix-instantiate failed:\n"
            + stderr_text
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
    system: str = "x86_64-linux",
    extra_nix_args: list[str] | None = None,
) -> str:
    """Assemble a sum-root .drv from PRE-BUILT aggregate drv paths.

    Imports :data:`SUM_DRV_FROM_AGGREGATES_NIX` (the post-aggregate
    assembler) and splices the supplied store paths in via
    ``builtins.appendContext`` — no flake eval, no internal re-wrapping
    of the inputs. The toolchains aggregate and every matrix-<binary>
    aggregate are produced upstream (phase 1 / phase 2) by
    :func:`make_wrapper_drv_from_paths`; this helper merely glues them
    together into one sum-root.

    Length-1 aggregate invariants (enforced here):

    * ``toolchain_drvs`` must be a list of EXACTLY ONE element — the
      single toolchains aggregate drv path. The public signature stays
      ``list[str]`` for source compatibility, but a length other than 1
      raises :class:`ValueError`.
    * Each ``matrix_drvs[binary]`` list must also have EXACTLY ONE
      element — the single matrix-<binary> aggregate drv path. A
      length other than 1 raises :class:`ValueError`.

    The toolchains aggregate's wrapper name is whatever
    :func:`make_wrapper_drv_from_paths` set when it built the aggregate
    upstream — this helper does NOT expose a caller-controllable
    aggregate-name knob, because the post-aggregate
    :data:`SUM_DRV_FROM_AGGREGATES_NIX` assembler doesn't take one.

    Validates that the toolchains drv's basename contains ``toolchain``
    and every matrix drv's basename contains ``matrix``. Leaf drvs
    (``-elf-folder.drv`` variants, ``-wrapper-`` compiler-wrappers,
    etc.) are rejected — the contract is that these are AGGREGATE
    wrapper drv paths sourced from one bulk evaluator pass.
    """
    if not toolchain_drvs:
        raise ValueError("at least one toolchain drv path is required")
    if len(toolchain_drvs) != 1:
        raise ValueError(
            "make_sum_drv_from_paths requires exactly ONE toolchains "
            f"aggregate drv path, got {len(toolchain_drvs)}: "
            f"{toolchain_drvs!r}. Production wraps every compiler "
            "into a single 'toolchains' aggregate upstream; passing a "
            "list of bare toolchain leaves is the legacy small-eval "
            "pattern this helper forbids."
        )
    if not matrix_drvs:
        raise ValueError("at least one matrix is required")
    for name, paths in matrix_drvs.items():
        if not paths:
            raise ValueError(f"matrix {name!r} has no drv paths")
        if len(paths) != 1:
            raise ValueError(
                f"make_sum_drv_from_paths requires exactly ONE matrix "
                f"aggregate drv path per binary, got {len(paths)} for "
                f"matrix {name!r}: {paths!r}. Production wraps every "
                "variant of a binary into a single 'matrix-<binary>' "
                "aggregate upstream; passing a list of bare variant "
                "leaves is the legacy small-eval pattern this helper "
                "forbids."
            )

    (toolchains_drv,) = toolchain_drvs
    _validate_marker(
        toolchains_drv,
        marker=_TOOLCHAIN_AGGREGATE_MARKER,
        role="toolchain",
    )
    matrix_aggregates: list[str] = []
    for name, paths in matrix_drvs.items():
        (one,) = paths
        _validate_marker(
            one, marker=_MATRIX_AGGREGATE_MARKER, role=f"matrix-{name}",
        )
        matrix_aggregates.append(one)

    expr = (
        f"import {SUM_DRV_FROM_AGGREGATES_NIX} {{\n"
        f"  bash = builtins.storePath {_q(bash_path)};\n"
        f"  toolchainsDrv = {_shallow_ref(toolchains_drv)};\n"
        f"  matrixDrvs = {_join_drvs(matrix_aggregates)};\n"
        f"  rootName = {_q(root_name)};\n"
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

"""Phase 1 ``build_compilers`` worker — distributed cross-toolchain build.

One task per ``(arch, compiler)`` cross-toolchain. Runs on a cluster
secondary; for each dispatched task header the worker:

  1. Realises the toolchain via ``nix build --no-link <toolchain_drv>``
     (or its flake attr fallback). The build's output side-effect leaves
     the realised store paths in the building secondary's local
     ``/nix/store/`` — that satisfies the "building secondary's local
     store" half of the plan's contract automatically.
  2. Walks ``nix-store --query --requisites <toolchain_drv> <out_paths>``
     to collect the toolchain's full closure (drv + output sides) and
     pipes it through ``nix-store --export`` into a single
     self-contained archive at::

         <out_network>/_build_compilers/<arch>__<compiler>.nix-archive

     ``out_network`` is the cluster-shared filesystem
     (``<shared_fs>/out`` by default) so the primary — and any other
     peer that needs the toolchain to walk a dependency graph — can
     ``nix-store --import < <file>`` into its own store. The archive
     is the authoritative cross-host transfer artefact, matching the
     ``matrix_eval`` design.

Primary-push policy
-------------------

The plan flagged two options for getting the toolchain's outputs into
the primary's local nix store:

  (a) push from secondary via ``nix copy --to ssh-ng://<primary>``;
  (b) write the archive to ``/out-network/_build_compilers/`` and
      let the primary fetch (``nix-store --import``) when it needs
      the closure for phase-3 ``dependency_graph`` graph walks.

This worker implements **option (b)**. Rationale:

  * The shared-fs archive is already the authoritative artefact for
    matrix_eval; reusing the same pattern keeps two parallel data
    transports (HTTP harmonia + ssh-ng push) from diverging.
  * SSH push requires per-secondary credentials that aren't otherwise
    plumbed today (the existing peer comms layer is HTTP-only — see
    ``peer_push.py``); option (b) needs no new credential surface.
  * The primary-side import is cheap and serialised with the
    phase-3 worker's own work, so we don't add primary-side fanout
    pressure when many secondaries finish at once (which option (a)
    would).

The primary's actual ``nix-store --import`` step lives in the watcher
that runs on the primary at the phase1→phase2 boundary (Phase B.3a
territory); this worker only writes the archive.

Error-type contract (framework integration)
-------------------------------------------

The dynamic_runner harness wraps the worker's exceptions into one of
two framework error types:

  * ``ErrorType::Errored`` — transient / retry-pass eligible. Use for
    ``nix build`` failures (network blip, transient nix daemon error,
    etc.). We raise plain ``RuntimeError`` for those.
  * ``ErrorType::Unfulfillable`` / ``NonRecoverableError`` — permanent
    on this peer or for this task. Use for **export failures after a
    successful realise**: the realise already succeeded so retrying
    won't recover anything, and the archive contract is the
    load-bearing handover to phase-3, so a failed export must surface
    as a hard error rather than silently signalling "done".

Item-class taxonomy
-------------------

This worker handles the new ``"build_compilers"`` item class introduced
in Phase B.1a's manifest_gen rename. Until that rename lands the
constant is referenced as a string literal so this module can be
imported and unit-tested without depending on the renamed
``manifest_gen.ItemClass`` literal.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Optional

from compiler_suit_runner.workers.dependency_graph_worker.subproc import (
    resolve_tool,
)


__all__ = [
    "ITEM_CLASS_BUILD_COMPILERS",
    "BuildCompilersResult",
    "BuildCompilersEnv",
    "parse_manifest_payload",
    "realise_toolchain",
    "export_closure",
    "run_build_compilers_task",
    "main",
]


# String literal — matches the post-B.1a manifest_gen ItemClass value.
# Kept as a bare constant so this module imports cleanly in worktrees
# where manifest_gen still uses the legacy ``phase2_toolchain`` literal.
ITEM_CLASS_BUILD_COMPILERS = "build_compilers"


# Subprocess runner injection point (mirrors build_worker / eval_worker).
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


# Truncation budget for log excerpts on failure (matches build_worker).
_LOG_EXCERPT_BYTE_LIMIT = 8 * 1024


# Default container path. Mirrors the matrix_eval landing dir
# constant introduced for the on-disk taxonomy rename.
DEFAULT_OUT_NETWORK_SUBDIR = "_build_compilers"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BuildCompilersResult:
    """Outcome of a single :func:`run_build_compilers_task` call.

    Mirrors :class:`BuildWorkerResult` so :func:`main` can hand the
    same shape back through the framework's worker harness.

    ``archive_path`` is the absolute path of the written
    ``<arch>__<compiler>.nix-archive`` on the shared filesystem (when
    successful). ``outpaths`` is the list of realised output store
    paths (used downstream for placement-record bookkeeping).
    """

    name: str
    success: bool
    duration_seconds: float
    archive_path: Optional[pathlib.Path] = None
    outpaths: tuple[str, ...] = ()
    drv: Optional[str] = None
    nix_log_excerpt: Optional[str] = None
    error: Optional[str] = None


@dataclasses.dataclass
class BuildCompilersEnv:
    """Process-wide configuration for the build_compilers worker.

    ``out_network`` is the cluster-shared output root (e.g.
    ``/app/out-network`` inside the container, ``<shared_fs>/out`` on
    the host). The worker writes archives under
    ``<out_network>/_build_compilers/``.

    ``flake_ref`` is only used when the manifest payload lacks an
    explicit ``drv`` and we have to fall back to a flake-attribute
    realise. Production manifests carry the drv.

    ``run_subprocess`` and ``clock`` are dependency-injection seams
    for unit tests; defaults invoke real ``subprocess.run`` and
    ``time.monotonic`` respectively.
    """

    flake_ref: str
    out_network: pathlib.Path
    substituters_file: Optional[pathlib.Path] = None
    run_subprocess: Optional[RunSubprocess] = None
    clock: Optional[Callable[[], float]] = None
    log_excerpt_lines: int = 80
    shared_fs: Optional[pathlib.Path] = None
    secondary_id: str = ""


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell.

    ``argv[0]`` is resolved via :func:`resolve_tool` so a bare tool
    name still execs when the respawn environment lost PATH.
    """
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        [resolve_tool(argv[0]), *argv[1:]],
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _read_substituters_file(path: pathlib.Path) -> list[str]:
    """Return the ``nix build`` argv fragment encoded in ``path``.

    Mirrors the helper in :mod:`workers.build_worker`. Each line is a
    literal nix-build argument (``--extra-substituters URL``, etc.).
    Missing or unreadable files return an empty list — peer
    substitution is best-effort.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _excerpt_log(
    stderr: bytes,
    max_lines: int,
    byte_limit: int = _LOG_EXCERPT_BYTE_LIMIT,
) -> Optional[str]:
    """Return the trailing ``max_lines`` of decoded stderr, or ``None``.

    Same shape as the helper in :mod:`workers.build_worker` so failure
    logs stay comparable across worker classes.
    """
    if not stderr:
        return None
    text = stderr.decode("utf-8", errors="replace")
    if not text:
        return None
    lines = text.splitlines()
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    excerpt = "\n".join(lines)
    encoded = excerpt.encode("utf-8")
    if len(encoded) > byte_limit:
        encoded = encoded[-byte_limit:]
        excerpt = encoded.decode("utf-8", errors="replace")
    return excerpt or None


# ---------------------------------------------------------------------------
# Manifest payload parsing
# ---------------------------------------------------------------------------


def parse_manifest_payload(payload: object) -> dict:
    """Validate a ``build_compilers`` payload and return a normalised dict.

    The payload shape is what :func:`manifest_gen.make_build_compilers_header`
    (post-B.1a) emits — at minimum ``sys`` + ``arch`` + ``compiler_label``,
    optionally a pre-resolved ``drv``. The worker also accepts the
    legacy ``phase2_toolchain`` header shape (same fields, plus an
    ``attr``) so the same dispatch can replay manifests from before
    the rename — useful for resume / mixed-version smoke tests.

    Raises :class:`ValueError` on shape errors so the caller can
    distinguish bad-input from realise / export failures.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"build_compilers payload must be a dict, got {type(payload).__name__}"
        )
    sys_name = payload.get("sys")
    arch = payload.get("arch")
    compiler_label = payload.get("compiler_label")
    if not isinstance(sys_name, str) or not sys_name:
        raise ValueError(
            f"build_compilers payload: invalid 'sys' ({sys_name!r})"
        )
    if not isinstance(arch, str) or not arch:
        raise ValueError(
            f"build_compilers payload: invalid 'arch' ({arch!r})"
        )
    if not isinstance(compiler_label, str) or not compiler_label:
        raise ValueError(
            f"build_compilers payload: invalid 'compiler_label' "
            f"({compiler_label!r})"
        )
    drv = payload.get("drv")
    if drv is not None and (not isinstance(drv, str) or not drv.endswith(".drv")):
        raise ValueError(
            f"build_compilers payload: invalid 'drv' ({drv!r}); "
            f"must end in .drv when set"
        )
    attr = payload.get("attr")
    if attr is not None and not isinstance(attr, str):
        raise ValueError(
            f"build_compilers payload: invalid 'attr' ({attr!r})"
        )
    if drv is None and not isinstance(attr, str):
        raise ValueError(
            "build_compilers payload: at least one of 'drv' or 'attr' "
            "must be set"
        )
    return {
        "sys": sys_name,
        "arch": arch,
        "compiler_label": compiler_label,
        "drv": drv,
        "attr": attr,
    }


# ---------------------------------------------------------------------------
# Realise + export
# ---------------------------------------------------------------------------


def _last_nonblank_lines(stdout: bytes) -> list[str]:
    """Return every non-blank line of decoded stdout, in order.

    ``nix build --print-out-paths`` prints one out-path per line; for a
    multi-output drv (gcc has ``out``, ``lib``, ``man``...) all paths
    appear, so we keep all of them for the export step.
    """
    if not stdout:
        return []
    text = stdout.decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def realise_toolchain(
    payload: dict,
    env: BuildCompilersEnv,
) -> tuple[bool, list[str], bytes, bytes]:
    """Run ``nix build`` for the toolchain described by ``payload``.

    Returns ``(success, outpaths, stdout, stderr)`` where ``outpaths``
    is the list of realised store paths parsed from stdout. ``payload``
    is assumed already validated by :func:`parse_manifest_payload`.

    Prefers building by drv (``nix build /nix/store/...drv^*``) when the
    payload carries one — that lets the secondary realise via substituter
    paths without needing the flake source checked out locally. Falls
    back to ``<flake_ref>#<attr>`` for legacy / single-process flows.
    """
    runner = env.run_subprocess or _default_run_subprocess

    argv: list[str] = [
        "nix",
        "build",
        "--no-link",
        "--print-out-paths",
    ]
    if env.substituters_file is not None:
        argv.extend(_read_substituters_file(env.substituters_file))
    drv = payload.get("drv")
    if isinstance(drv, str) and drv.endswith(".drv"):
        # ``^*`` realises every output of the drv (out + lib + man + ...).
        argv.append(f"{drv}^*")
    else:
        attr = payload["attr"]
        argv.append(f"{env.flake_ref}#{attr}")

    stdout, stderr, rc = runner(argv)
    outpaths = _last_nonblank_lines(stdout) if rc == 0 else []
    return rc == 0, outpaths, stdout, stderr


def export_closure(
    archive_path: pathlib.Path,
    seed_paths: list[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[bool, bytes, bytes]:
    """Walk the closure of ``seed_paths`` and pipe it through
    ``nix-store --export`` into ``archive_path``.

    Two subprocess invocations:

      1. ``nix-store --query --requisites <seed_paths...>`` to list every
         store path in the transitive closure (drv + output sides).
      2. ``nix-store --export <closure_paths...>`` whose stdout is the
         self-contained archive byte stream we redirect to disk.

    The archive is written atomically via ``.tmp`` + ``os.replace`` so a
    crash mid-export never leaves a half-written file the primary would
    mis-import.

    Returns ``(success, requisites_stderr, export_stderr)`` so the
    caller can attribute a failure to either subprocess.
    """
    runner = run_subprocess or _default_run_subprocess

    if not seed_paths:
        return False, b"export_closure: no seed paths", b""

    req_argv: list[str] = [
        "nix-store",
        "--query",
        "--requisites",
        *seed_paths,
    ]
    req_stdout, req_stderr, req_rc = runner(req_argv)
    if req_rc != 0:
        return False, req_stderr, b""

    closure = _last_nonblank_lines(req_stdout)
    if not closure:
        return False, b"nix-store --query --requisites returned no paths", b""

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = archive_path.with_suffix(archive_path.suffix + ".tmp")
    if tmp_archive.exists():
        try:
            tmp_archive.unlink()
        except OSError:
            pass

    export_argv: list[str] = [
        "nix-store",
        "--export",
        *closure,
    ]
    # Stream stdout straight to disk to avoid buffering a multi-GiB
    # archive in worker memory. The injected runner is for unit-test
    # ergonomics; tests pass tiny payloads so the default path is
    # what production uses.
    if run_subprocess is None:
        try:
            with open(tmp_archive, "wb") as fh:
                proc = subprocess.run(  # noqa: S603
                    [resolve_tool(export_argv[0]), *export_argv[1:]],
                    stdout=fh,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            exp_rc = proc.returncode
            exp_stderr = proc.stderr or b""
        except OSError as exc:
            return False, req_stderr, str(exc).encode("utf-8")
    else:
        exp_stdout, exp_stderr, exp_rc = runner(export_argv)
        if exp_rc == 0:
            try:
                with open(tmp_archive, "wb") as fh:
                    fh.write(exp_stdout)
            except OSError as exc:
                return False, req_stderr, str(exc).encode("utf-8")
    if exp_rc != 0:
        try:
            tmp_archive.unlink()
        except OSError:
            pass
        return False, req_stderr, exp_stderr

    try:
        os.replace(tmp_archive, archive_path)
    except OSError as exc:
        return False, req_stderr, str(exc).encode("utf-8")
    return True, req_stderr, exp_stderr


def _archive_path_for(
    out_network: pathlib.Path,
    arch: str,
    compiler_label: str,
) -> pathlib.Path:
    """Return the per-(arch, compiler) archive landing path."""
    return (
        out_network
        / DEFAULT_OUT_NETWORK_SUBDIR
        / f"{arch}__{compiler_label}.nix-archive"
    )


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def run_build_compilers_task(
    payload: dict,
    env: BuildCompilersEnv,
    *,
    name: str = "<unknown>",
) -> BuildCompilersResult:
    """Execute one ``build_compilers`` task.

    Steps:

      1. ``parse_manifest_payload`` — bad shape → ``error`` result.
      2. Resume short-circuit: if the archive already exists + is
         non-empty we trust some prior run on (possibly) another
         secondary already wrote it. Fast-no-op the task.
      3. ``realise_toolchain`` — failure raises :class:`RuntimeError`
         in :func:`main` (retry-pass eligible).
      4. ``export_closure`` to ``<out_network>/_build_compilers/<arch>__<compiler>.nix-archive``.
         Failure here is **non-recoverable** (realise succeeded; no
         transient cause for the export to fail short of disk-full
         or permission errors that retrying won't fix in-window).

    Never raises out of this function — :func:`main` consumes the
    result dataclass and translates ``success=False`` into the right
    framework error type.
    """
    clock = env.clock or time.monotonic
    start = clock()

    try:
        parsed = parse_manifest_payload(payload)
    except ValueError as exc:
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error=f"manifest parse failed: {exc}",
        )

    arch = parsed["arch"]
    compiler_label = parsed["compiler_label"]
    drv = parsed["drv"]
    archive_path = _archive_path_for(env.out_network, arch, compiler_label)

    # Resume short-circuit: a non-empty archive on shared-fs is the
    # authoritative artefact and means some prior run already
    # produced it. Realising again is wasteful (nix would no-op if
    # the closure is already in our local store, but the export step
    # still pays the closure-walk cost on a large toolchain).
    try:
        if archive_path.exists() and archive_path.stat().st_size > 0:
            return BuildCompilersResult(
                name=name,
                success=True,
                duration_seconds=max(0.0, clock() - start),
                archive_path=archive_path,
                drv=drv,
            )
    except OSError:
        # Treat stat errors as "not present" — better to redo the work
        # than block on a transient FS hiccup.
        pass

    try:
        success, outpaths, stdout, stderr = realise_toolchain(parsed, env)
    except Exception as exc:  # noqa: BLE001 - never raise out
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            drv=drv,
            error=f"nix build invocation crashed: {exc}",
        )
    if not success:
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            drv=drv,
            nix_log_excerpt=_excerpt_log(stderr, env.log_excerpt_lines),
            error="nix build returned non-zero",
        )
    if not outpaths:
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            drv=drv,
            nix_log_excerpt=_excerpt_log(stderr, env.log_excerpt_lines),
            error="nix build succeeded but produced no out-paths on stdout",
        )

    # Seed the closure walk with the realised output paths AND the
    # drv itself when known: the drv's .drv file lives in /nix/store
    # too and the primary needs it for `nix-store --query --tree` in
    # phase3. ``nix-store --query --requisites`` is idempotent across
    # duplicate seeds so the union is safe.
    seed: list[str] = list(outpaths)
    if drv:
        seed.append(drv)

    try:
        exported, req_err, exp_err = export_closure(
            archive_path, seed,
            run_subprocess=env.run_subprocess,
        )
    except Exception as exc:  # noqa: BLE001 - never raise out
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            drv=drv,
            outpaths=tuple(outpaths),
            error=f"nix-store --export crashed: {exc}",
        )
    if not exported:
        # Surface BOTH stderr streams: ``nix-store --query --requisites``
        # writes to req_err and ``nix-store --export`` writes to exp_err.
        # When the requisites query is the failing step, exp_err is empty
        # (set to b"" by export_closure on the early-return path), so an
        # excerpt of exp_err alone would be useless. Concatenating both
        # gives the operator the actual error class.
        combined = exp_err or req_err or b""
        if exp_err and req_err and req_err != exp_err:
            combined = (
                b"--- requisites stderr ---\n" + req_err
                + b"\n--- export stderr ---\n" + exp_err
            )
        return BuildCompilersResult(
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            drv=drv,
            outpaths=tuple(outpaths),
            nix_log_excerpt=_excerpt_log(combined, env.log_excerpt_lines),
            error="closure export failed",
        )

    return BuildCompilersResult(
        name=name,
        success=True,
        duration_seconds=max(0.0, clock() - start),
        archive_path=archive_path,
        outpaths=tuple(outpaths),
        drv=drv,
    )


# ---------------------------------------------------------------------------
# Subprocess entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Subprocess entry point for the build_compilers worker.

    Spawned by the dynamic_runner framework as
    ``python -m compiler_suit_runner.workers.build_compilers_worker``.
    Per-task wire driving (Ready handshake, command framing,
    SIGTERM → SystemExit) is owned by ``dynamic_runner.worker.run``;
    this module supplies the per-task handler.

    Export failures (a successful realise but a failed
    ``nix-store --export``) raise :class:`NonRecoverableError` — the
    archive contract is load-bearing for phase-3 and retrying an export
    of an already-realised closure won't recover from disk-full or
    permission errors.

    Realise failures raise plain :class:`RuntimeError`; the framework
    harness maps that to ``ErrorType::Errored`` (retry-pass eligible)
    because transient ``nix build`` failures (transient substituter
    network errors, etc.) typically resolve on retry.
    """
    import argparse  # noqa: PLC0415 - parser is a CLI-only concern.

    from dynamic_runner.worker import (  # noqa: PLC0415
        NonRecoverableError,
        Task,
        WorkerOutput,
        run,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.build_compilers_worker",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--flake-ref", type=str, required=True)
    parser.add_argument(
        "--out-network",
        type=str,
        required=True,
        help=(
            "Cluster-shared output root (e.g. /app/out-network). "
            "Toolchain archives land in"
            " <out-network>/_build_compilers/<arch>__<compiler>.nix-archive."
        ),
    )
    parser.add_argument(
        "--substituters-file",
        type=str,
        default=None,
        help=(
            "Optional peer-substituter file written by PeerListWatcher;"
            " spliced into every nix-build invocation."
        ),
    )
    parser.add_argument(
        "--shared-fs",
        type=str,
        default=None,
        help=(
            "NFS root used for placement gossip. Optional — when set,"
            " the worker can record realised toolchain outpaths via"
            " peer_paths.record_self_has so other secondaries pick them"
            " up via the cluster placement map. Currently informational"
            " only (no placement-record write wired)."
        ),
    )
    parser.add_argument(
        "--secondary-id",
        type=str,
        default="",
        help="This worker's secondary id; informational for log lines.",
    )
    args, _ = parser.parse_known_args()

    import logging  # noqa: PLC0415

    log_file = args.log_file or f"/app/log-network/worker_{os.getpid()}.log"
    try:
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        logging.getLogger(
            "compiler_suit_runner.build_compilers_worker.startup"
        ).info(
            "build_compilers_worker subprocess started; pid=%d argv=%r",
            os.getpid(), sys.argv,
        )
    except OSError:
        # If the log path isn't writable (eg unit tests, no mount) let
        # stdlib logging route to stderr (framework will swallow it).
        pass

    env = BuildCompilersEnv(
        flake_ref=args.flake_ref,
        out_network=pathlib.Path(args.out_network),
        substituters_file=(
            pathlib.Path(args.substituters_file)
            if args.substituters_file
            else None
        ),
        shared_fs=(
            pathlib.Path(args.shared_fs) if args.shared_fs else None
        ),
        secondary_id=args.secondary_id or "",
    )

    handle_log = logging.getLogger(
        "compiler_suit_runner.build_compilers_worker.handle"
    )

    def handle(task: Task) -> Optional[WorkerOutput]:
        wrapped = task.payload if isinstance(task.payload, dict) else None
        # The framework hands us the full header dict
        # {item_class, name, size, payload: {...}}. Pull the inner
        # payload + name out before dispatch.
        if not isinstance(wrapped, dict):
            raise NonRecoverableError(
                "build_compilers_worker: task.payload is not a dict;"
                " expected ManifestHeader-shaped wrapper"
            )
        item_class = wrapped.get("item_class")
        if item_class != ITEM_CLASS_BUILD_COMPILERS:
            raise NonRecoverableError(
                f"build_compilers_worker: unexpected item_class={item_class!r};"
                f" this worker only handles {ITEM_CLASS_BUILD_COMPILERS!r}"
            )
        inner = wrapped.get("payload")
        if not isinstance(inner, dict):
            raise NonRecoverableError(
                "build_compilers_worker: header missing 'payload' dict"
            )
        name = str(wrapped.get("name", "<unknown>"))

        handle_log.info(
            "handle: starting build_compilers task name=%r arch=%r compiler=%r",
            name, inner.get("arch"), inner.get("compiler_label"),
        )
        try:
            result = run_build_compilers_task(inner, env, name=name)
        except BaseException as exc:  # noqa: BLE001 - never bubble raw
            handle_log.exception(
                "handle: run_build_compilers_task raised unexpectedly"
            )
            raise NonRecoverableError(
                f"build_compilers crashed: {type(exc).__name__}: {exc}"
            ) from exc

        handle_log.info(
            "handle: result success=%s name=%r archive=%r error=%r",
            result.success, result.name,
            str(result.archive_path) if result.archive_path else None,
            (result.error or "")[:200],
        )
        if not result.success:
            # Distinguish realise failure (retry-eligible) from export
            # failure (non-recoverable). Realise failures land here as
            # ``error == "nix build returned non-zero"`` or
            # ``"nix build invocation crashed: ..."``; export failures
            # land as ``"closure export failed"`` or
            # ``"nix-store --export crashed: ..."``. The realise
            # variant raises RuntimeError so the framework maps it to
            # ErrorType::Errored.
            error_msg = result.error or "build_compilers failed"
            err_lower = error_msg.lower()
            is_realise_failure = (
                "nix build" in err_lower
                or "manifest parse failed" in err_lower
            )
            if is_realise_failure:
                # RuntimeError → framework harness → ErrorType::Errored.
                raise RuntimeError(error_msg)
            raise NonRecoverableError(error_msg)
        return WorkerOutput()

    run(handle, args=args)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# Helpers exported for tests / external diagnostics
# ---------------------------------------------------------------------------


def archive_path_for(
    out_network: pathlib.Path,
    arch: str,
    compiler_label: str,
) -> pathlib.Path:
    """Public alias for :func:`_archive_path_for`.

    Exposed so callers (e.g. the primary-side dependency_graph_worker)
    can compute the same landing path the worker writes to without
    re-encoding the layout convention.
    """
    return _archive_path_for(out_network, arch, compiler_label)


def parse_archive_sidecar(archive_path: pathlib.Path) -> dict:
    """Best-effort metadata reader for a sibling sidecar.

    Some deploys may pair the ``.nix-archive`` with a JSON sidecar
    listing the realised drv / outpaths (for diagnostics). When the
    sidecar exists this returns its parsed dict; absent or unreadable
    sidecar yields an empty dict. The worker does NOT write a sidecar
    today — the archive itself is the authoritative artefact — but the
    reader is provided so the next phase can attach metadata without a
    schema bump.
    """
    sidecar = archive_path.with_suffix(archive_path.suffix + ".json")
    if not sidecar.exists():
        return {}
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# Logger exported so callers can attach handlers / tweak level.
logger = logging.getLogger("compiler_suit_runner.build_compilers_worker")

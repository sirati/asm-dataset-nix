"""Build worker — phase-2 toolchain, phase-2 common-dep, phase-3 variant.

A single worker entry point handles all three nix-build classes; the
class is encoded in the manifest header's ``item_class``. For phase-3
variants the worker additionally copies the resulting ``<label>.tar.zst``
from the realised nix output into the shared dataset directory.

Subprocess execution and the wall clock are dependency-injected so the
test suite stays hermetic — no real ``nix build`` invocations and no
real time elapses.

Errors never escape :func:`build_worker`: any failure (manifest parse,
subprocess crash, missing tarball) lands inside :class:`BuildWorkerResult`
with ``success=False`` and a populated ``error`` field. The orchestrator
relies on this property to keep the dispatch loop alive across bad items.

See ``splendid-snacking-cascade.md`` sections "Phase 2 — compilers +
common host deps" and "Phase 3 — variants".
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Optional

__all__ = [
    "BuildWorkerResult",
    "BuildWorkerEnv",
    "VALID_ITEM_CLASSES",
    "ITEM_CLASS_PHASE2_TOOLCHAIN",
    "ITEM_CLASS_PHASE2_COMMON_DEP",
    "ITEM_CLASS_PHASE3_VARIANT",
    "parse_build_manifest",
    "build_attr",
    "copy_tarball",
    "write_sidecar_metadata",
    "build_worker",
]

# Item-class string tokens (matched against the manifest header). Kept as
# module-level constants so callers (manifest_gen, suit_task) reference
# the same string literals.
ITEM_CLASS_PHASE2_TOOLCHAIN = "phase2_toolchain"
ITEM_CLASS_PHASE2_COMMON_DEP = "phase2_common_dep"
ITEM_CLASS_PHASE3_VARIANT = "phase3_variant"

VALID_ITEM_CLASSES: frozenset[str] = frozenset(
    {
        ITEM_CLASS_PHASE2_TOOLCHAIN,
        ITEM_CLASS_PHASE2_COMMON_DEP,
        ITEM_CLASS_PHASE3_VARIANT,
    }
)

# Truncation limit for log excerpts captured into result.nix_log_excerpt.
# The result is sent through dynamic_runner's error-response transport
# which has a finite payload budget; ~8 KiB is plenty to diagnose a
# failure without blowing it up.
_LOG_EXCERPT_BYTE_LIMIT = 8 * 1024


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BuildWorkerResult:
    """Outcome of a single :func:`build_worker` call."""

    item_class: str
    name: str
    success: bool
    duration_seconds: float
    nix_log_excerpt: Optional[str] = None
    error: Optional[str] = None
    output_path: Optional[pathlib.Path] = None


@dataclasses.dataclass
class BuildWorkerEnv:
    """Process-wide configuration / dependencies for the build worker."""

    flake_ref: str
    dataset_output_dir: pathlib.Path
    # Path to a peer-substituter file written by ``PeerListWatcher``.
    # Each invocation reads it once and splices the contents into
    # ``nix build`` argv. ``None`` (or a missing file) disables peer
    # substitution.
    substituters_file: Optional[pathlib.Path] = None
    # Subprocess injection point. Default uses real subprocess.run().
    # Signature: argv -> (stdout_bytes, stderr_bytes, returncode).
    run_subprocess: Optional[Callable[[list[str]], tuple[bytes, bytes, int]]] = None
    # Wall clock injection (returns seconds, monotonic preferred). Default
    # uses time.monotonic().
    clock: Optional[Callable[[], float]] = None
    # Number of trailing log lines to retain on failure.
    log_excerpt_lines: int = 80


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_build_manifest(manifest_json_path: pathlib.Path) -> dict:
    """Parse a build-manifest JSON file and validate its ``item_class``.

    The manifest is the small JSON-encoded header dispatched to a worker
    by ``suit_task``; for the build worker it carries at minimum:

    .. code-block:: json

        {
          "item_class": "phase2_toolchain" | "phase2_common_dep" | "phase3_variant",
          "name": "<human readable id>",
          "payload": {
            "attr": "<flake attribute path>",
            ...class-specific fields...
          }
        }

    Raises :class:`ValueError` (subclass of Exception that
    :func:`build_worker` catches and folds into ``result.error``) for:

    * missing/unreadable file (re-raises OSError as ValueError to keep a
      single failure surface).
    * malformed JSON.
    * missing or unknown ``item_class``.

    Returns the full parsed dict so the caller can reach into
    ``payload`` without re-reading the file.
    """
    path = pathlib.Path(manifest_json_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read build manifest {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"build manifest {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"build manifest {path} top-level must be a JSON object, "
            f"got {type(data).__name__}"
        )
    item_class = data.get("item_class")
    if item_class not in VALID_ITEM_CLASSES:
        raise ValueError(
            f"build manifest {path} has unknown item_class={item_class!r}; "
            f"expected one of {sorted(VALID_ITEM_CLASSES)}"
        )
    return data


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Run ``argv`` (no shell) and return ``(stdout, stderr, returncode)``.

    Raw bytes are returned so callers can decide on a decoding strategy
    (the worker uses ``errors="replace"`` for log excerpting).
    """
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _read_substituters_file(path: pathlib.Path) -> list[str]:
    """Return the ``nix build`` argv fragment encoded in ``path``.

    The file is written atomically by :class:`PeerListWatcher` whenever
    the peer set changes; each line is a literal nix-build argument
    (``--extra-substituters URL``, ``--extra-trusted-public-keys KEY``,
    ``--substitute-on-destination``...). Missing or unreadable files
    return an empty list — peer substitution is best-effort.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return []
    out: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            out.append(stripped)
    return out


def build_attr(
    attr: str,
    env: BuildWorkerEnv,
    *,
    extra_args: Optional[list[str]] = None,
) -> tuple[bool, bytes, bytes]:
    """Run ``nix build`` for a single attribute.

    Constructs ``nix build --no-link --print-out-paths <flake_ref>#<attr>``
    plus:

    * peer substituter args read fresh from
      ``env.substituters_file`` on each invocation (when set).
    * caller-supplied ``extra_args`` (e.g. ``--skip-existing`` for
      common-deps so the build short-circuits when the output is
      already in the local store).

    Returns ``(success, stdout, stderr)`` where ``success`` is True iff
    the subprocess returned 0. The subprocess inherits the parent
    environment and never goes through a shell.
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
    if extra_args:
        argv.extend(extra_args)
    # ``attr`` is interpreted in two modes:
    #   - absolute drv path  (e.g. /nix/store/...drv) → build by drv,
    #     no flake source needed on the secondary; the drv is fetched
    #     from a peer harmonia via substituters.
    #   - flake attribute    (e.g. _crossToolchainMap.x86_64-linux...) →
    #     legacy single-process / dev-box path; needs flake_ref to
    #     resolve to a checked-out tree.
    if attr.startswith("/nix/store/") and attr.endswith(".drv"):
        argv.append(f"{attr}^*")
    else:
        argv.append(f"{env.flake_ref}#{attr}")

    stdout, stderr, rc = runner(argv)
    return rc == 0, stdout, stderr


# ---------------------------------------------------------------------------
# Tarball copy (phase 3 only)
# ---------------------------------------------------------------------------


def _find_tarball(out_path: pathlib.Path) -> pathlib.Path:
    """Locate the ``.tar.zst`` under (or at) ``out_path``.

    ``out_path`` is what ``nix build --print-out-paths`` printed as the
    realised store path. ``mkBinaryTarball`` packages an ELF directory
    into a directory containing a single ``*.tar.zst`` file, but we
    tolerate the case where the realised path IS the tarball file
    directly (legacy / single-file derivations).

    Raises :class:`FileNotFoundError` if no tarball can be located.
    """
    if out_path.is_file() and out_path.name.endswith(".tar.zst"):
        return out_path
    if out_path.is_dir():
        candidates = sorted(out_path.glob("*.tar.zst"))
        if len(candidates) == 0:
            raise FileNotFoundError(
                f"no *.tar.zst found in build output directory {out_path}"
            )
        if len(candidates) > 1:
            # Multiple tarballs shouldn't happen for the matrix outputs,
            # but if it does we pick the lexicographically first and log
            # nothing — the caller's manifest dictates the destination
            # name, so ambiguity here only matters if the user changed
            # mkBinaryTarball semantics.
            pass
        return candidates[0]
    raise FileNotFoundError(
        f"build output {out_path} is neither a *.tar.zst file nor a directory"
    )


def write_sidecar_metadata(
    dest_dir: pathlib.Path,
    metadata_name: str,
    metadata: dict,
) -> pathlib.Path:
    """Write the variant's sidecar JSON (full param dump).

    Same atomic pattern as :func:`copy_tarball` (``.tmp`` + replace).
    The file mirrors the tarball's name with ``.json`` extension —
    pairs them on disk for easy lookup.
    """
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / metadata_name
    tmp = dest_dir / (metadata_name + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    payload = json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, final)
    return final


def copy_tarball(
    out_path: pathlib.Path,
    dest_dir: pathlib.Path,
    tarball_name: str,
) -> pathlib.Path:
    """Copy the variant's ``.tar.zst`` from the nix store into the dataset dir.

    ``out_path`` is the realised ``nix build`` output (a directory
    containing one ``*.tar.zst``, or the tarball file itself).
    ``dest_dir`` is the run-wide shared dataset directory.
    ``tarball_name`` is the canonical short name
    (``<compiler>_<arch>_<opt>_<hash>.tar.zst``); the sibling JSON
    sidecar is written separately by :func:`write_sidecar_metadata`.

    The copy is atomic from a reader's perspective: we copy to
    ``dest_dir/<tarball_name>.tmp`` first and ``os.replace`` into place.
    Metadata (mtime) is preserved via :func:`shutil.copy2`.

    Raises :class:`FileNotFoundError` when no tarball can be found under
    ``out_path``.
    """
    out_path = pathlib.Path(out_path)
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    src = _find_tarball(out_path)

    final_dest = dest_dir / tarball_name
    tmp_dest = dest_dir / (tarball_name + ".tmp")
    # In case a previous interrupted run left a stale tmp file behind.
    if tmp_dest.exists():
        try:
            tmp_dest.unlink()
        except OSError:
            pass
    shutil.copy2(src, tmp_dest)
    os.replace(tmp_dest, final_dest)
    return final_dest


# ---------------------------------------------------------------------------
# Log excerpt helper
# ---------------------------------------------------------------------------


def _excerpt_log(
    stderr: bytes, max_lines: int, byte_limit: int = _LOG_EXCERPT_BYTE_LIMIT
) -> Optional[str]:
    """Return the trailing ``max_lines`` of decoded stderr, or None if empty.

    Decodes with ``errors="replace"`` so we never blow up on binary
    interleaving. Truncates to ``byte_limit`` UTF-8 bytes (after line
    selection) by chopping from the head, since the *tail* of the log is
    where the failure usually lives.
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
        # Keep the tail (most informative).
        encoded = encoded[-byte_limit:]
        excerpt = encoded.decode("utf-8", errors="replace")
    return excerpt or None


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def _last_nonblank_line(stdout: bytes) -> Optional[str]:
    """Return the last non-blank line of decoded stdout, or None."""
    if not stdout:
        return None
    text = stdout.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return None


def build_worker(
    manifest_json_path: pathlib.Path,
    env: BuildWorkerEnv,
    *,
    manifest_data: Optional[dict] = None,
) -> BuildWorkerResult:
    """Execute the build described by ``manifest_json_path``.

    Steps:

    1. Parse the manifest. Failure -> failed result with the parse error.
    2. Read ``payload.attr``. Missing -> failed result.
    3. For ``item_class == phase2_common_dep`` add ``--skip-existing`` so
       a path that's already in the local store is a no-op (toolchains
       and variants are best-effort substituted but always allowed to
       rebuild — common-deps are the only class where partial pre-build
       progress is expected).
    4. ``build_attr`` to actually invoke nix.
    5. On success for ``item_class == phase3_variant``: locate the
       realised output path from the last stdout line and
       :func:`copy_tarball` to ``env.dataset_output_dir``.
    6. Build the result; capture the log excerpt on failure.

    Never raises out of this function — the caller (a dynamic_runner
    worker process) needs the result to round-trip through its IPC
    transport, and an unhandled exception would tear the secondary down.
    """
    clock = env.clock or time.monotonic
    start = clock()

    # Parse manifest. Use a sentinel name/class so the result object can
    # always be constructed even on read failure. With FR-3 the framework
    # ships the parsed payload over the wire and ``manifest_data`` is set;
    # legacy callers pass a path and the file is read.
    name: str = "<unknown>"
    item_class: str = "<unknown>"
    try:
        if manifest_data is not None:
            if not isinstance(manifest_data, dict):
                raise ValueError(
                    f"manifest_data must be a dict, got {type(manifest_data).__name__}"
                )
            header = manifest_data
            cls = header.get("item_class")
            if cls not in VALID_ITEM_CLASSES:
                raise ValueError(
                    f"manifest_data has unknown item_class={cls!r}; "
                    f"expected one of {sorted(VALID_ITEM_CLASSES)}"
                )
        else:
            header = parse_build_manifest(manifest_json_path)
        item_class = str(header.get("item_class", "<unknown>"))
        name = str(header.get("name", "<unknown>"))
    except Exception as exc:  # noqa: BLE001 - never raise out
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error=f"manifest parse failed: {exc}",
        )

    payload = header.get("payload")
    if not isinstance(payload, dict):
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error="manifest missing 'payload' object",
        )
    # Prefer the absolute drv path when the manifest carries it: lets
    # the secondary build via ``nix build /nix/store/...drv^*`` so the
    # drv (and its closure) substitutes from peer harmonias without
    # needing the flake source on the secondary's filesystem. Fall
    # back to the flake attribute for legacy / single-process flows.
    drv = payload.get("drv")
    attr = drv if isinstance(drv, str) and drv.endswith(".drv") else payload.get("attr")
    if not isinstance(attr, str) or not attr:
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error="manifest payload missing both 'drv' and 'attr'",
        )

    extra_args: list[str] = []
    if item_class == ITEM_CLASS_PHASE2_COMMON_DEP:
        extra_args.append("--skip-existing")

    try:
        success, stdout, stderr = build_attr(attr, env, extra_args=extra_args)
    except Exception as exc:  # noqa: BLE001 - never raise out
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error=f"nix build invocation crashed: {exc}",
        )

    if not success:
        # Write the failure context to a gateway-visible log file.
        # Worker subprocesses have stdin/stdout/stderr silenced by the
        # framework (subprocess_factory.rs:116), so writing to fd 2
        # goes nowhere. ``/app/log-network`` is the secondary
        # container's bind-mount of the gateway's per-run log dir,
        # so files written there land in
        # ``~/BIG/slurm/log/<run_id>/build-failures/`` and are
        # visible from the dispatching machine via SSH.
        excerpt = _excerpt_log(stderr, env.log_excerpt_lines)
        try:
            log_dir = pathlib.Path("/app/log-network/build-failures")
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"{name}.log").write_text(
                f"item_class: {item_class}\n"
                f"attr: {attr}\n"
                f"--- stderr excerpt ---\n{excerpt}\n--- end ---\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            nix_log_excerpt=excerpt,
            error="nix build returned non-zero",
        )

    output_path: Optional[pathlib.Path] = None
    if item_class == ITEM_CLASS_PHASE3_VARIANT:
        last = _last_nonblank_line(stdout)
        if not last:
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                nix_log_excerpt=_excerpt_log(stderr, env.log_excerpt_lines),
                error="nix build succeeded but produced no out-path on stdout",
            )
        out_store = pathlib.Path(last)
        tarball_name = payload.get("tarball_name")
        if not isinstance(tarball_name, str) or not tarball_name:
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                error="phase3_variant manifest missing 'payload.tarball_name'",
            )
        try:
            output_path = copy_tarball(
                out_store, env.dataset_output_dir, tarball_name
            )
        except Exception as exc:  # noqa: BLE001 - never raise out
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                nix_log_excerpt=_excerpt_log(stderr, env.log_excerpt_lines),
                error=f"tarball copy failed: {exc}",
            )
        # Sidecar JSON: full param dump next to the tarball. Filename
        # ``<tarball-stem>.json`` so the pair is trivial to look up.
        # Skipped silently if the manifest didn't carry a metadata_name
        # — older manifests in cached pre-flight dirs won't have it.
        metadata_name = payload.get("metadata_name")
        if isinstance(metadata_name, str) and metadata_name:
            sidecar = {
                "label": payload.get("label"),
                "pkg": payload.get("pkg"),
                "arch": payload.get("arch"),
                "compiler": payload.get("compiler_id"),
                "compiler_family": payload.get("compiler_family"),
                "compiler_version": payload.get("compiler_version"),
                "optimization": payload.get("optimization"),
                "flag_set": payload.get("flag_set"),
                "hardening": payload.get("hardening"),
                "sanitizer": payload.get("sanitizer"),
                "march": payload.get("march"),
                "drv": payload.get("drv"),
                "tarball_name": tarball_name,
            }
            try:
                write_sidecar_metadata(
                    env.dataset_output_dir, metadata_name, sidecar
                )
            except Exception:  # noqa: BLE001 - sidecar is best-effort
                pass

    return BuildWorkerResult(
        item_class=item_class,
        name=name,
        success=True,
        duration_seconds=max(0.0, clock() - start),
        output_path=output_path,
    )


# ---------------------------------------------------------------------------
# Subprocess entry point
#
# Spawned by the dynamic_runner framework as
# ``python -m compiler_suit_runner.workers.build_worker``. Reads one
# manifest path per line from stdin and dispatches each through
# :func:`build_worker`. Phase-2 toolchains, phase-2 common deps, and
# phase-3 variants all share this entry point — the per-manifest
# ``item_class`` field decides the routing inside :func:`build_worker`.
#
# TODO(phase 8 follow-up): wire to ``dynamic_runner.comm`` once the
# comm shape for TaskInfo dispatch is stabilised.


def main() -> int:
    """Subprocess entry point for the build worker.

    Drives the framework's worker protocol via the comm fd
    (``--dynamic_queue`` / ``--socket-path``), routing each manager-
    supplied relative path through :func:`build_worker`. See
    :mod:`compiler_suit_runner.workers._runner_protocol` for the
    line-based protocol details.
    """
    import argparse
    import logging

    from ._runner_protocol import (
        DispatchResult,
        connect_comm,
        run_protocol_loop,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.build_worker",
    )
    parser.add_argument("--dynamic_queue", type=int, default=None)
    parser.add_argument("--socket-path", type=str, default=None)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument(
        "--flake-ref",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--dataset-output-dir",
        type=str,
        required=True,
    )
    parser.add_argument(
        "--substituters-file",
        type=str,
        default=None,
        help=(
            "Path to a peer-substituter file maintained by"
            " PeerListWatcher; read fresh on every nix build."
        ),
    )
    parser.add_argument("--skip-existing", action="store_true")
    args, _ = parser.parse_known_args()

    log = logging.getLogger("compiler_suit_runner.workers.build_worker")

    env = BuildWorkerEnv(
        flake_ref=args.flake_ref,
        dataset_output_dir=pathlib.Path(args.dataset_output_dir),
        substituters_file=(
            pathlib.Path(args.substituters_file)
            if args.substituters_file
            else None
        ),
    )

    sock = connect_comm(
        dynamic_queue=args.dynamic_queue,
        socket_path=args.socket_path,
        log=log,
    )
    if sock is None:
        log.warning("no comm channel supplied; worker exiting (test mode)")
        return 0

    def dispatch(
        manifest_path: pathlib.Path,
        payload: Optional[object] = None,
    ) -> DispatchResult:
        manifest_data = payload if isinstance(payload, dict) else None
        result = build_worker(
            manifest_path, env, manifest_data=manifest_data
        )
        if result.success:
            return DispatchResult.ok()
        return DispatchResult.error(result.error or "build failed")

    return run_protocol_loop(
        sock=sock,
        source=args.source,
        dispatch=dispatch,
        log=log,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())

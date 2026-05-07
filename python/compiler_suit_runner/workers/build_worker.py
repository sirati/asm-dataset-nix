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

# Retry policy for transient ``SQLite database is busy`` aborts during
# concurrent ``nix build`` operations. Total worst-case extra wait =
# 0.5 + 1.0 + 1.5 + 2.0 + 2.5 = 7.5 s, which is short relative to a
# real build but long enough for typical contention windows to clear.
_SQLITE_BUSY_MAX_RETRIES = 6
_SQLITE_BUSY_BACKOFF_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BuildWorkerResult:
    """Outcome of a single :func:`build_worker` call.

    ``staged_outputs`` lists every file the worker wrote into the
    staging root (``BuildWorkerEnv.dataset_output_dir`` — by default
    ``/app/out-tmp/dataset``). The dynamic_runner subprocess wrapper
    in :func:`main` hands these to ``task.publish_all()`` so the
    framework atomically delivers them to the destination NFS root
    (``/app/out-network/dataset``). For phase-2 builds the tuple is
    empty: nix's substituter path moves toolchain / common-dep store
    paths between secondaries, so phase-2 has no per-secondary
    artifact to publish.
    """

    item_class: str
    name: str
    success: bool
    duration_seconds: float
    nix_log_excerpt: Optional[str] = None
    error: Optional[str] = None
    output_path: Optional[pathlib.Path] = None
    staged_outputs: tuple[pathlib.Path, ...] = ()


@dataclasses.dataclass
class BuildWorkerEnv:
    """Process-wide configuration / dependencies for the build worker.

    ``dataset_output_dir`` is the **staging** root the worker writes
    finished tarballs and sidecar JSON into. In production it points at
    the framework's tmpfs staging mount (``/app/out-tmp/dataset``); the
    runtime then hands the staged paths to ``Task.publish_all()`` for
    atomic stage→destination delivery onto the NFS output mount
    (``/app/out-network/dataset``). Tests pass any local tmpdir; no
    publish is invoked from :func:`build_worker` itself, so the worker
    stays decoupled from the framework's runtime in unit tests.
    """

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

    # Retry on transient nix-store init / contention errors. Multiple
    # concurrent workers in the same secondary container all hit the
    # same local nix store; the first invocation has to create the
    # SQLite DB and schema file, and subsequent invocations can race
    # on either the lock (``SQLite database is busy``) or the not-
    # yet-fully-written schema file (``schema is corrupt``). The
    # contention window is short — back off briefly and retry.
    for attempt in range(_SQLITE_BUSY_MAX_RETRIES):
        stdout, stderr, rc = runner(argv)
        if rc == 0:
            return True, stdout, stderr
        is_transient = (
            b"is busy" in stderr
            or b"SQLite" in stderr
            or b"schema" in stderr and b"corrupt" in stderr
        )
        if not is_transient:
            return False, stdout, stderr
        # Last attempt — give up and return the error to the caller.
        if attempt == _SQLITE_BUSY_MAX_RETRIES - 1:
            return False, stdout, stderr
        time.sleep(_SQLITE_BUSY_BACKOFF_SECONDS * (1 + attempt))
    return False, stdout, stderr


# ---------------------------------------------------------------------------
# ELF folder deref-copy (phase 3 only)
# ---------------------------------------------------------------------------


def _find_elf_dir(out_path: pathlib.Path) -> pathlib.Path:
    """Locate the ``elf/`` directory under ``out_path``.

    ``out_path`` is what ``nix build --print-out-paths`` printed as the
    realised store path. ``mkBinaryFolder`` produces a directory shaped
    as ``$out/elf/<basename>`` (symlinks pointing into the variant's
    store path) plus a top-level ``$out/meta.json``.

    Raises :class:`FileNotFoundError` if ``out_path`` is not a directory
    or the ``elf`` subdir is missing.
    """
    if not out_path.is_dir():
        raise FileNotFoundError(
            f"build output {out_path} is not a directory (expected mkBinaryFolder layout)"
        )
    elf_dir = out_path / "elf"
    if not elf_dir.is_dir():
        raise FileNotFoundError(
            f"no 'elf/' subdir found under build output {out_path}"
        )
    return elf_dir


def write_sidecar_metadata(
    dest_dir: pathlib.Path,
    metadata_name: str,
    metadata: dict,
) -> pathlib.Path:
    """Write the variant's sidecar JSON (full param dump).

    Atomic pattern: ``.tmp`` + ``os.replace``. The file lives next to
    the variant's ELF subdir as ``<dest_dir>/<variant_dir>.json``.
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


def copy_elf_folder(
    out_path: pathlib.Path,
    dest_dir: pathlib.Path,
    variant_dir: str,
) -> list[pathlib.Path]:
    """Deref-copy each ELF symlink from the variant's elf-folder into staging.

    ``out_path`` is the realised ``nix build`` output of an
    ``mkBinaryFolder`` derivation (``$out/elf/<basename>`` symlinks +
    ``$out/meta.json``). ``dest_dir`` is the run-wide staging root
    (``BuildWorkerEnv.dataset_output_dir``, e.g.
    ``/app/out-tmp/dataset/<pkg>``). ``variant_dir`` is the
    per-variant subdirectory name carried in the manifest payload.

    Each ELF lands at ``dest_dir/<variant_dir>/<basename>`` with the
    actual bytes (``shutil.copy2(follow_symlinks=True)``), atomically
    placed via ``.tmp`` + ``os.replace``. The framework's
    ``task.publish_all`` then mirrors these staged files onto the
    destination NFS root.

    Returns the list of staged destination paths in deterministic
    (sorted-basename) order. Raises :class:`FileNotFoundError` when
    no ``elf/`` subdir is present under ``out_path``.
    """
    out_path = pathlib.Path(out_path)
    dest_dir = pathlib.Path(dest_dir)
    elf_dir = _find_elf_dir(out_path)

    variant_subdir = dest_dir / variant_dir
    variant_subdir.mkdir(parents=True, exist_ok=True)

    staged: list[pathlib.Path] = []
    for src in sorted(elf_dir.iterdir()):
        if not src.is_file() and not src.is_symlink():
            continue
        basename = src.name
        final_dest = variant_subdir / basename
        tmp_dest = variant_subdir / (basename + ".tmp")
        if tmp_dest.exists():
            try:
                tmp_dest.unlink()
            except OSError:
                pass
        # follow_symlinks=True is the default for copy2, but stating it
        # explicitly here documents the intent: we want the ELF bytes
        # in staging, not a symlink into /nix/store (which the framework
        # cannot follow on the destination side).
        shutil.copy2(src, tmp_dest, follow_symlinks=True)
        os.replace(tmp_dest, final_dest)
        staged.append(final_dest)
    return staged


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
        variant_dir = payload.get("variant_dir")
        if not isinstance(variant_dir, str) or not variant_dir:
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                error="phase3_variant manifest missing 'payload.variant_dir'",
            )
        # Group variants by package: ``dataset/<pkg>/<variant_dir>/<elf>``
        # so an operator can ``ls dataset/hello/`` to see every variant
        # of one package. Subdir name doesn't repeat the pkg — the
        # parent dir carries that, the subdir carries the matrix axes
        # (compiler, arch, opt, hash).
        pkg = payload.get("pkg")
        per_pkg_dir = (
            env.dataset_output_dir / pkg
            if isinstance(pkg, str) and pkg
            else env.dataset_output_dir
        )
        try:
            elf_paths = copy_elf_folder(
                out_store, per_pkg_dir, variant_dir
            )
        except Exception as exc:  # noqa: BLE001 - never raise out
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                nix_log_excerpt=_excerpt_log(stderr, env.log_excerpt_lines),
                error=f"elf folder copy failed: {exc}",
            )
        # ``output_path`` historically pointed at the single tarball file;
        # under the elf-folder layout the equivalent is the per-variant
        # subdirectory we just populated.
        output_path = per_pkg_dir / variant_dir
        staged: list[pathlib.Path] = list(elf_paths)
        # Sidecar JSON: full param dump next to the variant subdir.
        # Filename ``<variant_dir>.json`` so the pair is trivial to look
        # up. Skipped silently if the manifest didn't carry a metadata_name
        # — older manifests in cached pre-flight dirs won't have it.
        metadata_name = payload.get("metadata_name")
        if isinstance(metadata_name, str) and metadata_name:
            sidecar = {
                "label": payload.get("label"),
                "pkg": pkg,
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
                "variant_dir": variant_dir,
            }
            try:
                sidecar_path = write_sidecar_metadata(
                    per_pkg_dir, metadata_name, sidecar
                )
                staged.append(sidecar_path)
            except Exception:  # noqa: BLE001 - sidecar is best-effort
                pass

        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=True,
            duration_seconds=max(0.0, clock() - start),
            output_path=output_path,
            staged_outputs=tuple(staged),
        )

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
# ``python -m compiler_suit_runner.workers.build_worker``. The per-task
# wire driving (Ready handshake, command framing, exception → wire
# mapping, SIGTERM → SystemExit) is owned by ``dynamic_runner.worker.run``;
# this module only supplies the per-task body.


def main() -> int:
    """Subprocess entry point for the build worker.

    Builds a :class:`BuildWorkerEnv` from CLI flags, hands a
    :func:`_handle` closure to :func:`dynamic_runner.worker.run`, and
    lets the framework runtime own the comm channel. Build failures
    are surfaced to the manager as ``error:non_recoverable:`` lines via
    :class:`NonRecoverableError`.
    """
    import argparse

    from dynamic_runner.worker import (
        NonRecoverableError,
        PublishError,
        Task,
        WorkerOutput,
        run,
    )

    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.build_worker",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dynamic_queue", type=int)
    group.add_argument("--socket-path", type=str)
    parser.add_argument("--source", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--log-file", type=str, default=None)
    parser.add_argument("--flake-ref", type=str, required=True)
    parser.add_argument("--dataset-output-dir", type=str, required=True)
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

    env = BuildWorkerEnv(
        flake_ref=args.flake_ref,
        dataset_output_dir=pathlib.Path(args.dataset_output_dir),
        substituters_file=(
            pathlib.Path(args.substituters_file)
            if args.substituters_file
            else None
        ),
    )

    def handle(task: Task) -> Optional[WorkerOutput]:
        manifest_data = task.payload if isinstance(task.payload, dict) else None
        manifest_path = (
            pathlib.Path(task.relative_path)
            if task.relative_path
            else pathlib.Path("<inline>")
        )
        result = build_worker(manifest_path, env, manifest_data=manifest_data)
        if not result.success:
            raise NonRecoverableError(result.error or "build failed")
        # Atomic stage→destination publish via the framework: the
        # worker wrote into ``DYNRUNNER_PUBLISH_SRC_ROOT`` (tmpfs);
        # ``task.publish_all`` mirrors each staged path under
        # ``DYNRUNNER_PUBLISH_DST_ROOT`` (NFS) with a single rename
        # at the destination. Phase-2 builds have nothing staged
        # (toolchains/common-deps fan out via harmonia substitution,
        # not file delivery) so the call is a no-op for those.
        if result.staged_outputs:
            try:
                task.publish_all(*result.staged_outputs)
            except PublishError as exc:
                raise NonRecoverableError(
                    f"publish failed: {exc}"
                ) from exc
        return WorkerOutput()

    run(handle, args=args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

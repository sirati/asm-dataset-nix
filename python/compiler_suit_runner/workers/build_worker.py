"""Build worker — build_common_dep, build_variant, toolchain_validate.

A single worker entry point handles the remaining nix-build classes; the
class is encoded in the manifest header's ``item_class``. For
``build_variant`` items the worker additionally deref-copies the ELF
symlinks from the realised nix output's ``elf/`` subdir (mkBinaryFolder
layout) into the shared dataset directory under
``<pkg>/<variant_dir>/<basename>``. Toolchain compilation now lives in
``build_compilers_worker.py``; this module only handles the rare
``toolchain_validate`` probe (gated by ``--debug-testbuild``).

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
import logging
import os
import pathlib
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Optional

from compiler_suit_runner.workers.dependency_graph_worker.subproc import (
    resolve_tool,
)

# Module logger. The worker subprocess routes stdlib logging to a per-
# worker file (see :func:`main`), so INFO step logging surfaces in
# ``worker_N.log`` — making a build's progress (and any failure point)
# readable rather than silence through the whole ``nix build``.
_LOG = logging.getLogger("compiler_suit_runner.build_worker")

__all__ = [
    "BuildWorkerResult",
    "BuildWorkerEnv",
    "VALID_ITEM_CLASSES",
    "ITEM_CLASS_TOOLCHAIN_VALIDATE",
    "ITEM_CLASS_BUILD_COMMON_DEP",
    "ITEM_CLASS_BUILD_VARIANT",
    "parse_build_manifest",
    "build_attr",
    "copy_elf_folder",
    "write_sidecar_metadata",
    "build_worker",
    "ensure_binary_archive_imported",
    "ensure_toolchain_archive_imported",
    "ensure_common_archive_imported",
    "ensure_toolchain_out_archive_imported",
]


# Module-level cache of binaries whose matrix-aggregate archive has
# already been imported on this worker process. Workers live across
# many ``build_variant`` tasks; importing the same binary's archive
# more than once is wasted I/O (and CPU on the nix-store side). The
# set is keyed by ``payload["pkg"]`` — :func:`ensure_binary_archive_imported`
# treats the first task for binary X as "import then build" and every
# subsequent task on the same worker process as "build only". Resets
# only on process exit; the cache is intentionally per-worker because
# the archive content is per-binary-per-run and the local nix store is
# also per-worker (containers don't share /nix/store).
_imported_binaries: set[str] = set()

# Companion guard for the per-process, once-only import of the shared
# ``toolchains.drv.archive`` (the toolchain-dedup pre-flight artefact).
# With toolchain dedup the per-binary ``matrix-<binary>.drv.archive``
# carries only ``requisites(matrix) − requisites(toolchain)``; the
# toolchain closure ships once via this archive and MUST be imported
# FIRST so the per-binary diff is importable. Like _imported_binaries
# this is per-worker (containers don't share /nix/store) and resets on
# process exit. There is exactly one toolchain archive per run, so a
# bare bool suffices.
_toolchain_imported: bool = False

# Per-process, once-only import guard for the COMMON realized-toolchain
# archive (``toolchains.common.archive``). This archive ships the shared
# closure (glibc, libgcc, …) that every toolchain depends on. It MUST be
# imported before any per-toolchain delta archive.  OPTIONAL — a missing
# archive degrades to substitution and never raises.
_common_archive_imported: bool = False

# Per-process set of toolchain out-paths whose delta archive has already
# been imported on this worker.  Keyed by outpath string so workers handle
# multiple toolchains in one process without redundant imports.
_imported_toolchain_out_paths: set[str] = set()

# Item-class string tokens (matched against the manifest header). Kept as
# module-level constants so callers (manifest_gen, suit_task) reference
# the same string literals. Toolchain *build* is no longer dispatched
# here — ``build_compilers_worker.py`` owns that path.
ITEM_CLASS_TOOLCHAIN_VALIDATE = "toolchain_validate"
ITEM_CLASS_BUILD_COMMON_DEP = "build_common_dep"
ITEM_CLASS_BUILD_VARIANT = "build_variant"

VALID_ITEM_CLASSES: frozenset[str] = frozenset(
    {
        ITEM_CLASS_TOOLCHAIN_VALIDATE,
        ITEM_CLASS_BUILD_COMMON_DEP,
        ITEM_CLASS_BUILD_VARIANT,
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

# Retry policy for transient peer-substituter connectivity failures.
# Secondaries reach peer nix caches through per-node SSH forwards
# (e.g. the submitter's harmonia on localhost:5005); when a forward
# drops, ``nix build`` fails with a curl connect error plus the
# downstream "no substituter that can build it" cascade. The forward
# is rebuilt automatically, so back off long enough to ride out a
# rebuild: 2+4+8+16+32+32 = 94 s worst case (plus up to 25% jitter
# per step).
_SUBSTITUTER_MAX_ATTEMPTS = 7
_SUBSTITUTER_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0, 32.0)
_SUBSTITUTER_JITTER_FRACTION = 0.25

# stderr substrings (lower-cased match) marking a failed CONNECTION to
# a substituter. Deliberately narrow: a bare "no substituter that can
# build it" / "could not be realised" without one of these connect
# markers is a genuinely missing path and stays a permanent failure.
_SUBSTITUTER_CONNECT_MARKERS = (
    b"could not connect to server",
    b"couldn't connect to server",
    b"failed to connect to",
    b"connection refused",
)

# Injectable sleep hook so tests can assert the backoff schedule without
# actually sleeping. Production never overrides it.
_retry_sleep = time.sleep


def _is_substituter_connect_failure(stderr: bytes) -> bool:
    """True iff nix stderr indicates a substituter connect failure."""
    low = stderr.lower()
    return any(marker in low for marker in _SUBSTITUTER_CONNECT_MARKERS)


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
    # Cluster-wide placement-map fields. Populated when the worker
    # realises (or fetches) a store path; consumed by the post-build
    # hook in :func:`build_worker` to register the path with the
    # cluster placement gossip (:mod:`peer_paths`). ``outpath`` is
    # the realised ``/nix/store/<hash>-name`` (NOT the .drv); ``drv``
    # is the drv path the build worker was asked to realise.
    outpath: Optional[str] = None
    drv: Optional[str] = None


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
    # Cluster placement-map plumbing. ``shared_fs`` is the NFS root
    # used by :mod:`peer_paths` for the per-secondary
    # ``_paths_<sid>.jsonl`` gossip file. ``secondary_id`` is this
    # worker's identity. ``placement_watcher`` provides the
    # ``snapshot()`` aggregate; ``peer_watcher`` carries the live
    # peer list for targeted ``nix copy`` fetches and the path-have
    # broadcast. ``signing_public_key`` is the cluster pubkey header
    # used to authenticate the push fan-out. All are ``None`` /
    # empty in unit tests; the worker treats absence as "no
    # placement plumbing", skipping pre-fetch and record-self-has.
    shared_fs: Optional[pathlib.Path] = None
    secondary_id: str = ""
    placement_watcher: Optional["object"] = None
    peer_watcher: Optional["object"] = None
    signing_public_key: str = ""
    # Per-binary matrix-eval archive root. When set, the first
    # ``build_variant`` task targeting binary ``X`` on this worker
    # process imports ``matrix-X.drv.archive`` from this dir before
    # running ``nix build`` (see :func:`ensure_binary_archive_imported`).
    # Unit tests typically leave this ``None`` and the import is a
    # no-op; production invocations via :func:`main` always set it
    # from ``--matrix-eval-out-dir``.
    matrix_eval_out_dir: Optional[pathlib.Path] = None


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_build_manifest(manifest_json_path: pathlib.Path) -> dict:
    """Parse a build-manifest JSON file and validate its ``item_class``.

    The manifest is the small JSON-encoded header dispatched to a worker
    by ``suit_task``; for the build worker it carries at minimum:

    .. code-block:: json

        {
          "item_class": "build_common_dep" | "build_variant" | "toolchain_validate",
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

    ``argv[0]`` is resolved via :func:`resolve_tool` so a bare tool
    name still execs when the respawn environment lost PATH.
    """
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        [resolve_tool(argv[0]), *argv[1:]],
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _resolve_bash_store_path_default() -> Optional[str]:
    """Local fallback for ``nix eval --raw nixpkgs#bash.outPath``.

    Mirrors :func:`suit_task._resolve_bash_store_path`; lives here so
    the dependency_graph dispatch branch inside :func:`main` can resolve
    bash without importing ``suit_task`` (which already imports this
    module — would create a cycle).
    """
    try:
        proc = subprocess.run(  # noqa: S603 - argv is fixed
            [resolve_tool("nix"), "eval", "--raw", "nixpkgs#bash.outPath"],
            check=False,
            capture_output=True,
            shell=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    return out or None


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
    * caller-supplied ``extra_args`` appended verbatim to the ``nix
      build`` argv (a hook for valid per-build nix flags; the caller
      owns validity).

    Returns ``(success, stdout, stderr)`` where ``success`` is True iff
    the subprocess returned 0. The subprocess inherits the parent
    environment and never goes through a shell.

    TRANSIENT failures are retried in-place before the failure is
    surfaced: local nix-store SQLite contention (brief linear backoff)
    and peer-substituter connect failures (exponential backoff +
    jitter on the :data:`_SUBSTITUTER_BACKOFF_SECONDS` ladder, sized
    to outlast an SSH-forward rebuild). A "no substituter that can
    build it" / "could not be realised" error WITHOUT an accompanying
    connect marker is a genuinely missing path and fails immediately.
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

    # Retry on transient errors. Two independent budgets:
    #
    # * nix-store init / contention: multiple concurrent workers in the
    #   same secondary container all hit the same local nix store; the
    #   first invocation has to create the SQLite DB and schema file,
    #   and subsequent invocations can race on either the lock
    #   (``SQLite database is busy``) or the not-yet-fully-written
    #   schema file (``schema is corrupt``). The contention window is
    #   short — back off briefly and retry.
    # * peer-substituter connectivity: a dropped SSH forward makes nix
    #   fail with a connect error (and a misleading "no substituter
    #   that can build it" cascade). The forward self-heals — back off
    #   on the :data:`_SUBSTITUTER_BACKOFF_SECONDS` ladder and retry.
    #
    # Anything else is permanent and returned to the caller unchanged.
    sqlite_failures = 0
    substituter_failures = 0
    while True:
        stdout, stderr, rc = runner(argv)
        if rc == 0:
            return True, stdout, stderr
        if (
            b"is busy" in stderr
            or b"SQLite" in stderr
            or b"schema" in stderr and b"corrupt" in stderr
        ):
            sqlite_failures += 1
            if sqlite_failures >= _SQLITE_BUSY_MAX_RETRIES:
                return False, stdout, stderr
            time.sleep(_SQLITE_BUSY_BACKOFF_SECONDS * sqlite_failures)
            continue
        if _is_substituter_connect_failure(stderr):
            substituter_failures += 1
            if substituter_failures >= _SUBSTITUTER_MAX_ATTEMPTS:
                _LOG.warning(
                    "nix build of %s still failing on substituter "
                    "connectivity after %d attempts — giving up: %s",
                    attr, substituter_failures,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
                return False, stdout, stderr
            base = _SUBSTITUTER_BACKOFF_SECONDS[
                min(
                    substituter_failures - 1,
                    len(_SUBSTITUTER_BACKOFF_SECONDS) - 1,
                )
            ]
            delay = base + random.uniform(
                0, base * _SUBSTITUTER_JITTER_FRACTION
            )
            _LOG.warning(
                "transient substituter connect failure building %s "
                "(attempt %d/%d) — retrying in %.1fs",
                attr, substituter_failures, _SUBSTITUTER_MAX_ATTEMPTS,
                delay,
            )
            _retry_sleep(delay)
            continue
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


def _resolve_placements(env: BuildWorkerEnv) -> dict:
    """Return the cluster placement aggregate accessible to this worker.

    Prefers ``env.placement_watcher.snapshot()`` (in-process flow,
    no disk I/O); falls back to a one-shot read of every
    ``peers/_paths_*.jsonl`` on ``env.shared_fs`` (subprocess flow).
    Empty dict when no source is available.
    """
    if env.placement_watcher is not None:
        try:
            return env.placement_watcher.snapshot()
        except Exception:  # noqa: BLE001
            pass
    if env.shared_fs is not None:
        try:
            from compiler_suit_runner.peer_cache import (
                _read_all_placement_files,
            )
            return _read_all_placement_files(env.shared_fs)
        except Exception:  # noqa: BLE001
            pass
    return {}


def _resolve_peers(env: BuildWorkerEnv) -> list:
    """Return the live peer list accessible to this worker.

    Prefers ``env.peer_watcher.peers``; falls back to a one-shot
    NFS scan of ``peers/`` via :func:`peer_cache.list_peers`.
    Excludes self if ``env.secondary_id`` is set.
    """
    if env.peer_watcher is not None:
        try:
            return list(env.peer_watcher.peers)
        except Exception:  # noqa: BLE001
            pass
    if env.shared_fs is not None:
        try:
            from compiler_suit_runner.peer_cache import list_peers
            return list_peers(
                env.shared_fs,
                exclude_id=env.secondary_id or None,
            )
        except Exception:  # noqa: BLE001
            pass
    return []


def _maybe_record_self_has(
    env: BuildWorkerEnv,
    outpath: Optional[str],
    drv: Optional[str],
    placement_class: str,
) -> None:
    """Best-effort cluster placement record for a realised store path.

    Skipped silently when the placement plumbing is not configured
    (unit tests, single-process flows) or when ``outpath`` is empty.
    The on-disk JSONL gossip file is the source of truth; the push
    fan-out is opportunistic — failures here never propagate to the
    build result.

    Side-effect that drives K=3 cascade: writing the placement record
    makes the manager-process :class:`PathPlacementWatcher` see this
    secondary as a new holder on its next refresh (call
    ``request_refresh`` if the watcher handle is available here, e.g.
    the legacy in-process flow). The watcher's diff callback then
    fires :meth:`ReplicationRepairWorker.on_diff` which initiates the
    cascade push_attempt for toolchain outpaths. The subprocess
    worker flow has no ``placement_watcher`` handle, so the cascade
    latency is bounded by the watcher's tick.
    """
    if not outpath:
        return
    if env.shared_fs is None or not env.secondary_id:
        return
    try:
        from compiler_suit_runner import peer_paths  # local import: keeps
        # import-time graph lean for unit tests that don't load peer_paths.
        peers = _resolve_peers(env) if env.signing_public_key else None
        peer_paths.record_self_has(
            env.shared_fs,
            my_secondary_id=env.secondary_id,
            outpath=outpath,
            drv_path=drv or "",
            item_class=placement_class,
            peers=peers,
            our_pubkey=env.signing_public_key or None,
        )
    except Exception:  # noqa: BLE001 - placement is best-effort
        pass
    # Wake the manager-process placement watcher so the cascade
    # diff-callback fires within a few ms instead of one tick. Only
    # available in the legacy in-process flow (subprocess workers
    # don't carry the watcher handle).
    watcher = getattr(env, "placement_watcher", None)
    if watcher is not None:
        try:
            request_refresh = getattr(watcher, "request_refresh", None)
            if callable(request_refresh):
                request_refresh()
        except Exception:  # noqa: BLE001 - wake is best-effort
            pass


def _validate_toolchain(
    payload: dict,
    env: BuildWorkerEnv,
    *,
    item_class: str,
    name: str,
    start: float,
    clock: Callable[[], float],
) -> BuildWorkerResult:
    """Handle a ``toolchain_validate`` item: fetch instead of build.

    Cheap path-info probe first; on miss, run a single targeted
    ``nix copy --from http://<peer>:<port>`` against the placement
    map (primary preferred). On success, record the outpath in our
    own placement gossip so the next variant looking for this
    toolchain finds us as a candidate.

    Failure to fetch is fatal for the validate item (the toolchain
    is required by every downstream variant); the result surfaces a
    NonRecoverable-equivalent error message that the framework
    treats as a hard failure.
    """
    outpath = payload.get("outpath")
    drv = payload.get("drv")
    if not isinstance(outpath, str) or not outpath:
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error=(
                "toolchain_validate manifest missing"
                " 'payload.outpath'; the primary's emit_all_manifests"
                " should have included it"
            ),
            drv=drv if isinstance(drv, str) else None,
        )

    # Local-import the fetch helpers so unit tests that don't exercise
    # the validate path don't pay the import cost.
    from compiler_suit_runner.peer_paths_fetch import (
        PRIMARY_CANDIDATE_ID,
        fetch_from_peer,
        is_path_locally_valid,
    )

    drv_str = drv if isinstance(drv, str) else None
    if is_path_locally_valid(outpath, run_subprocess=env.run_subprocess):
        _maybe_record_self_has(env, outpath, drv_str, "toolchain")
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=True,
            duration_seconds=max(0.0, clock() - start),
            outpath=outpath,
            drv=drv_str,
        )

    placements = _resolve_placements(env)
    peers = _resolve_peers(env)
    # Wrap run_subprocess so a FAILED validate has the per-candidate
    # argv + rc + stderr captured for triage. fetch_from_peer logs at
    # DEBUG (filtered in production), so without this the only signal
    # is "source is None". We only write the diag file on failure —
    # writing on success would trip the ``build_failures`` invariant
    # which counts entries in ``/app/log-network/build-failures/``.
    fetch_log: list[tuple[list[str], int, bytes]] = []

    def _wrapped(argv: list[str]) -> tuple[bytes, bytes, int]:
        if env.run_subprocess is not None:
            stdout, stderr, rc = env.run_subprocess(argv)
        else:
            import subprocess as _sub
            p = _sub.run(  # noqa: S603
                argv, check=False, capture_output=True, shell=False,
            )
            stdout, stderr, rc = p.stdout, p.stderr, p.returncode
        fetch_log.append((list(argv), rc, stderr[-1500:]))
        return stdout, stderr, rc

    source = fetch_from_peer(
        outpath,
        placements,
        peers,
        prefer=PRIMARY_CANDIDATE_ID,
        run_subprocess=_wrapped,
        check_local=False,  # we just checked
    )
    if source is None:
        # Only on failure: drop a diag log next to the build-failures
        # so an operator can see exactly which candidate(s) refused.
        try:
            log_dir = pathlib.Path("/app/log-network/build-failures")
            log_dir.mkdir(parents=True, exist_ok=True)
            diag_path = log_dir / f"{name}.validate-diag.log"
            candidate_set = placements.get(outpath, set())
            with diag_path.open("w", encoding="utf-8") as f:
                f.write(
                    f"outpath={outpath}\n"
                    f"shared_fs={env.shared_fs}\n"
                    f"secondary_id={env.secondary_id}\n"
                    f"placements_keys_total={len(placements)}\n"
                    f"candidates_for_outpath={sorted(candidate_set)!r}\n"
                    f"peers={[(p.secondary_id, p.hostname, p.port) for p in peers]!r}\n"
                    f"---\nfetch_attempts={len(fetch_log)}\n"
                )
                for argv, rc, err_tail in fetch_log:
                    f.write(f"argv={argv!r}\n")
                    f.write(f"rc={rc}\n")
                    f.write(
                        f"stderr_tail={err_tail.decode('utf-8', errors='replace')!r}\n"
                    )
        except Exception:  # noqa: BLE001 - diagnostic is best-effort
            pass
    if source is None:
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=False,
            duration_seconds=max(0.0, clock() - start),
            error=(
                f"toolchain {outpath} not in local store and"
                " no peer in the placement map could serve it;"
                " primary may not have realised it yet, or"
                " --build-compilers is needed"
            ),
            outpath=outpath,
            drv=drv_str,
        )
    _maybe_record_self_has(env, outpath, drv_str, "toolchain")
    return BuildWorkerResult(
        item_class=item_class,
        name=name,
        success=True,
        duration_seconds=max(0.0, clock() - start),
        outpath=outpath,
        drv=drv_str,
    )


def ensure_binary_archive_imported(
    binary: str,
    matrix_eval_out_dir: Optional[pathlib.Path],
    *,
    run_subprocess: Optional[Callable[..., tuple[bytes, bytes, int]]] = None,
) -> None:
    """Import ``matrix-<binary>.drv.archive`` into the local nix store once.

    Phase 2 (matrix_eval) writes a self-contained
    ``matrix-<binary>.drv.archive`` under ``matrix_eval_out_dir`` whose
    closure carries every variant leaf the cluster sampled for this
    binary plus the toolchain aggregate. Phase 4 (build_variant) on each
    secondary now imports that archive ONCE at the start of the first
    ``build_variant`` task targeting this binary so the subsequent
    ``nix build <drv>^*`` can resolve every leaf drv locally without
    fanning out to a peer harmonia. Subsequent ``build_variant`` tasks
    for the same binary on the same worker process short-circuit via
    :data:`_imported_binaries`.

    Failure to import is escalated to :class:`RuntimeError`; the framework
    wraps that into ``ErrorType::Errored`` (retry-pass eligible). The
    archive is the single point of truth for the matrix-eval drv graph
    on this secondary — without it the build_variant will fail with a
    "missing drv" error a few seconds later, so failing fast here is
    strictly the better surface.

    The ``matrix_eval_out_dir`` argument may be ``None`` (e.g. legacy
    unit tests that don't supply it); in that case the import is a
    no-op so existing fixtures keep working. Production invocations
    via :func:`main` always supply it.
    """
    if not binary:
        return
    if matrix_eval_out_dir is None:
        return
    if binary in _imported_binaries:
        return
    # Late import to keep the module-load graph lean for tests that
    # never touch dependency_graph_worker.
    from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
        archive as _archive,
    )

    archive_path = matrix_eval_out_dir / f"matrix-{binary}.drv.archive"
    ok, err, imported = _archive.import_archive(
        archive_path, run_subprocess=run_subprocess,
    )
    if not ok:
        raise RuntimeError(
            f"build_worker: failed to import matrix-{binary}.drv.archive "
            f"from {archive_path}: "
            + err.decode("utf-8", errors="replace").strip()
        )
    _imported_binaries.add(binary)
    import logging  # noqa: PLC0415 — local import; cheap
    logging.getLogger(
        "compiler_suit_runner.build_worker.archive_import"
    ).info(
        "imported matrix-%s.drv.archive (%d paths)",
        binary, len(imported),
    )


def ensure_toolchain_archive_imported(
    matrix_eval_out_dir: Optional[pathlib.Path],
    *,
    run_subprocess: Optional[Callable[..., tuple[bytes, bytes, int]]] = None,
) -> None:
    """Import the shared ``toolchains.drv.archive`` once per worker process.

    Toolchain-dedup pre-flight writes ONE ``toolchains.drv.archive``
    under ``matrix_eval_out_dir`` carrying the whole compiler-toolchain
    closure. Each per-binary ``matrix-<binary>.drv.archive`` then ships
    only ``requisites(matrix) − requisites(toolchain)`` — so on a
    build-only secondary the toolchain archive MUST be imported FIRST
    (before :func:`ensure_binary_archive_imported`) or the per-binary
    diff is un-importable. The first build task on this worker process
    imports it; subsequent tasks short-circuit via
    :data:`_toolchain_imported`.

    The archive is located by fixed name under ``matrix_eval_out_dir``
    (no new payload field), mirroring the per-binary lookup. Failure to
    import is escalated to :class:`RuntimeError` (the framework wraps it
    into ``ErrorType::Errored``, retry-pass eligible) — without the
    toolchain closure the subsequent ``nix build`` fails with a missing
    drv anyway, so failing fast here is the better surface.

    ``matrix_eval_out_dir`` may be ``None`` (legacy fixtures that don't
    thread it) — in that case the import is a no-op. A zero-byte archive
    is treated as "nothing to import" (the toolchain export wrote an
    empty file) and short-circuits without error.
    """
    global _toolchain_imported
    if matrix_eval_out_dir is None:
        return
    if _toolchain_imported:
        return
    archive_path = matrix_eval_out_dir / "toolchains.drv.archive"
    # A zero-byte (or absent) archive carries nothing to import. We do
    # NOT hard-fail on absence here: the per-binary archive import that
    # follows is the load-bearing fatal check, and an operator running
    # an older submit (no toolchain archive) still gets the full closure
    # from the per-binary archive. Mark imported so we don't re-stat.
    try:
        size = archive_path.stat().st_size
    except OSError:
        _toolchain_imported = True
        return
    if size == 0:
        _toolchain_imported = True
        return
    # Late import to keep the module-load graph lean for tests that
    # never touch dependency_graph_worker.
    from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
        archive as _archive,
    )

    ok, err, imported = _archive.import_archive(
        archive_path, run_subprocess=run_subprocess,
    )
    if not ok:
        raise RuntimeError(
            "build_worker: failed to import toolchains.drv.archive "
            f"from {archive_path}: "
            + err.decode("utf-8", errors="replace").strip()
        )
    _toolchain_imported = True
    import logging  # noqa: PLC0415 — local import; cheap
    logging.getLogger(
        "compiler_suit_runner.build_worker.archive_import"
    ).info(
        "imported toolchains.drv.archive (%d paths)", len(imported),
    )


def _import_archive_soft(
    archive_path: pathlib.Path,
    label: str,
    *,
    run_subprocess: Optional[Callable[..., tuple[bytes, bytes, int]]] = None,
) -> bool:
    """Import ``archive_path`` via ``nix-store --import``, soft-failing.

    Logs a WARNING and returns ``False`` on any error (absent, zero-byte,
    or import failure).  Returns ``True`` on success.  Never raises.
    Shared by :func:`ensure_common_archive_imported` and
    :func:`ensure_toolchain_out_archive_imported`.
    """
    import logging  # noqa: PLC0415 — cheap, called at most once per archive

    log = logging.getLogger("compiler_suit_runner.build_worker.archive_import")
    try:
        size = archive_path.stat().st_size
    except OSError:
        return False  # absent = soft path; caller marks guard + continues
    if size == 0:
        return False
    from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
        archive as _archive,
    )

    ok, err, imported = _archive.import_archive(
        archive_path, run_subprocess=run_subprocess,
    )
    if not ok:
        log.warning(
            "build_worker: failed to import %s from %s "
            "(falling back to substitution): %s",
            label, archive_path,
            err.decode("utf-8", errors="replace").strip(),
        )
        return False
    log.info("imported %s (%d paths)", label, len(imported))
    return True


def ensure_common_archive_imported(
    matrix_eval_out_dir: Optional[pathlib.Path],
    *,
    run_subprocess: Optional[Callable[..., tuple[bytes, bytes, int]]] = None,
) -> None:
    """Import ``toolchains.common.archive`` once per worker process.

    The submitter uploads the COMMON closure (intersection of all toolchain
    closures: glibc, libgcc, …) as a single archive.  Workers import it
    BEFORE any per-toolchain delta archive, since the delta references
    paths that must already be present in the local store.

    OPTIONAL / SOFT FAIL: a missing or unreadable archive logs nothing
    (the submitter may be older or the upload may have failed) and workers
    fall back to substitution as before.  The per-toolchain delta import
    (:func:`ensure_toolchain_out_archive_imported`) is also a soft fail
    for the same reason.

    ``matrix_eval_out_dir`` may be ``None`` (legacy fixtures) — no-op.
    """
    global _common_archive_imported
    if matrix_eval_out_dir is None:
        return
    if _common_archive_imported:
        return
    # Mark done first so any early-return path (absent, import failure)
    # doesn't retry on every subsequent task.
    _common_archive_imported = True
    archive_path = matrix_eval_out_dir / "toolchains.common.archive"
    _import_archive_soft(
        archive_path, "toolchains.common.archive",
        run_subprocess=run_subprocess,
    )


def ensure_toolchain_out_archive_imported(
    toolchain_outpath: Optional[str],
    matrix_eval_out_dir: Optional[pathlib.Path],
    *,
    run_subprocess: Optional[Callable[..., tuple[bytes, bytes, int]]] = None,
) -> None:
    """Import the per-toolchain delta archive once per (outpath, process).

    After :func:`ensure_common_archive_imported` has run, this function
    imports the toolchain-specific delta archive
    (``toolchains.<id>.out.archive``) whose paths complete the full
    compiler closure.  The archive name is derived from
    ``toolchain_outpath`` via
    :func:`compiler_suit_runner.preflight.toolchain_delta_archive_name`,
    which uses the Nix store-hash (the first ``-``-delimited token of the
    basename) — the same derivation the submitter uses so file names
    match without coordination.

    OPTIONAL / SOFT FAIL: an absent or failed archive logs a WARNING and
    returns; the subsequent ``nix build`` will fall back to substitution
    (harmonia peer or cache.nixos.org) exactly as before the split.

    ``toolchain_outpath`` or ``matrix_eval_out_dir`` being ``None``
    (legacy manifests / fixtures) is a no-op.
    """
    if not toolchain_outpath or matrix_eval_out_dir is None:
        return
    if toolchain_outpath in _imported_toolchain_out_paths:
        return
    # Mark done first so failures don't retry on every task.
    _imported_toolchain_out_paths.add(toolchain_outpath)
    from compiler_suit_runner import preflight as _preflight  # noqa: PLC0415

    try:
        archive_name = _preflight.toolchain_delta_archive_name(toolchain_outpath)
    except ValueError:
        import logging  # noqa: PLC0415
        logging.getLogger(
            "compiler_suit_runner.build_worker.archive_import"
        ).warning(
            "build_worker: cannot derive archive name for toolchain outpath "
            "%r; falling back to substitution",
            toolchain_outpath,
        )
        return
    archive_path = matrix_eval_out_dir / archive_name
    _import_archive_soft(
        archive_path, archive_name,
        run_subprocess=run_subprocess,
    )


def _run_import_prelude(
    item_class: str,
    payload: dict,
    env: BuildWorkerEnv,
) -> None:
    """Toolchain-first archive import prelude for build tasks.

    Import order (all soft-fail unless noted):
      1. ``toolchains.common.archive`` (SOFT) — shared runtime closure.
      2. ``toolchains.<id>.out.archive`` (SOFT) — per-toolchain delta;
         the toolchain outpath is read from ``payload["toolchain_outpath"]``.
      3. ``toolchains.drv.archive`` (HARD) — drv graph closure; must
         precede the per-binary diff import below.
      4. ``matrix-<binary>.drv.archive`` (HARD) — per-binary drv diff.

    Steps 1–2 eliminate substitution fan-in at build time. Steps 3–4
    are load-bearing: a missing drv archive means nix cannot resolve the
    variant drv locally and the build fails immediately.
    """
    # 1 & 2: realized toolchain closures (soft-fail).
    ensure_common_archive_imported(
        env.matrix_eval_out_dir,
        run_subprocess=env.run_subprocess,
    )
    toolchain_outpath = payload.get("toolchain_outpath")
    ensure_toolchain_out_archive_imported(
        toolchain_outpath if isinstance(toolchain_outpath, str) else None,
        env.matrix_eval_out_dir,
        run_subprocess=env.run_subprocess,
    )
    # 3 & 4: drv graph archives (hard-fail on missing).
    ensure_toolchain_archive_imported(
        env.matrix_eval_out_dir,
        run_subprocess=env.run_subprocess,
    )
    binary = (
        payload.get("pkg") if isinstance(payload.get("pkg"), str)
        else payload.get("binary") if isinstance(payload.get("binary"), str)
        else ""
    )
    ensure_binary_archive_imported(
        binary or "",
        env.matrix_eval_out_dir,
        run_subprocess=env.run_subprocess,
    )
    del item_class  # reserved for future per-class prelude branching


def _prefetch_variant_inputs(
    payload: dict,
    env: BuildWorkerEnv,
) -> None:
    """Best-effort pre-fetch of every input dep in the placement map.

    Reads ``payload['input_drvs']`` (list[str]) +
    ``payload['input_outpaths']`` (dict[drv, outpath]) emitted by
    :func:`manifest_gen.make_variant_header`. For each input whose
    outpath is in the cluster placement map (i.e. some peer has it)
    and not yet locally valid, issues a targeted ``nix copy --from``
    against that peer. Failures are logged at debug and do not
    affect the build: nix's native substituter resolution
    (cache.nixos.org + the peer federation) handles anything the
    pre-fetch didn't get.
    """
    input_drvs = payload.get("input_drvs")
    input_outpaths = payload.get("input_outpaths")
    if not isinstance(input_drvs, list) or not isinstance(input_outpaths, dict):
        return
    if not input_drvs:
        return
    placements = _resolve_placements(env)
    if not placements:
        return
    peers = _resolve_peers(env)
    if not peers:
        return

    from compiler_suit_runner.peer_paths_fetch import fetch_from_peer

    for input_drv in input_drvs:
        if not isinstance(input_drv, str):
            continue
        outpath = input_outpaths.get(input_drv)
        if not isinstance(outpath, str) or not outpath:
            continue
        if outpath not in placements:
            continue  # nobody has it; let nix's substituters handle it
        try:
            # ``fetch_from_peer`` runs a path-info check before
            # invoking ``nix copy``; absent peers / network errors
            # surface as ``None`` and are silently ignored — the
            # downstream ``nix build`` will fetch via substituters
            # or rebuild from source.
            fetch_from_peer(
                outpath,
                placements,
                peers,
                run_subprocess=env.run_subprocess,
            )
        except Exception:  # noqa: BLE001 - pre-fetch is best-effort
            continue


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
    3. ``build_common_dep`` relies on ``nix build`` being idempotent (an
       output already in the local store is a no-op); variants are
       best-effort substituted but always allowed to rebuild.
    4. ``build_attr`` to actually invoke nix.
    5. On success for ``item_class == build_variant``: locate the
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

    # Validate-only items have a different shape: no ``nix build``,
    # just a path-info probe + targeted ``nix copy`` against the
    # placement map. Branch here so the rest of the dispatch keeps
    # its build-shaped invariants.
    if item_class == ITEM_CLASS_TOOLCHAIN_VALIDATE:
        return _validate_toolchain(
            payload, env,
            item_class=item_class, name=name,
            start=start, clock=clock,
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

    # build_common_dep descriptors carry a bare store basename
    # (``<hash>-<name>.drv``, the ``.drv`` already in the name);
    # reconstruct the absolute drv path so build_attr takes the
    # ``nix build /nix/store/<ident>^*`` branch instead of the flake-ref
    # fallback (which needs a flake.nix on the secondary's /app cwd).
    if (
        item_class == ITEM_CLASS_BUILD_COMMON_DEP
        and attr.endswith(".drv")
        and not attr.startswith("/nix/store/")
    ):
        attr = "/nix/store/" + attr

    _LOG.info("build START item_class=%s name=%s attr=%s", item_class, name, attr)

    # No per-class nix flags: `nix build` is idempotent (an output already
    # in the local store is a no-op), so build_common_dep needs no skip
    # flag. Do NOT re-add `--skip-existing` here — nix rejects it.

    # Build prelude: toolchain-first archive import. Import the shared
    # ``toolchains.drv.archive`` (once per worker process) BEFORE the
    # per-binary ``matrix-<binary>.drv.archive`` so the matrix-aggregate
    # drv graph — every variant AND common-dep ``.drv`` — is local; with
    # toolchain dedup the per-binary archive is a diff against the
    # toolchain closure, so the toolchain import must come first. Then
    # (variants only) pre-fetch input deps the placement map knows about
    # from a single targeted peer. Pre-fetch is best-effort; archive
    # imports are fatal (the drv must exist locally before the nix build
    # below). The common-dep ``.drv`` is part of the variants' drv closure
    # the archive captures, so the same import makes it available.
    if item_class in (ITEM_CLASS_BUILD_VARIANT, ITEM_CLASS_BUILD_COMMON_DEP):
        _LOG.info("build name=%s: importing toolchain+binary archives", name)
        try:
            _run_import_prelude(item_class, payload, env)
        except RuntimeError as exc:
            return BuildWorkerResult(
                item_class=item_class,
                name=name,
                success=False,
                duration_seconds=max(0.0, clock() - start),
                error=str(exc),
            )
        if item_class == ITEM_CLASS_BUILD_VARIANT:
            _prefetch_variant_inputs(payload, env)

    _LOG.info("build name=%s: nix build START attr=%s", name, attr)
    try:
        success, stdout, stderr = build_attr(attr, env)
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

    _LOG.info("build name=%s: nix build DONE (ok)", name)

    output_path: Optional[pathlib.Path] = None
    if item_class == ITEM_CLASS_BUILD_VARIANT:
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
                error="build_variant manifest missing 'payload.variant_dir'",
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

        _LOG.info(
            "build DONE variant=%s (%d outputs staged)", name, len(staged),
        )
        return BuildWorkerResult(
            item_class=item_class,
            name=name,
            success=True,
            duration_seconds=max(0.0, clock() - start),
            output_path=output_path,
            staged_outputs=tuple(staged),
            outpath=str(out_store),
            drv=drv if isinstance(drv, str) else None,
        )

    # Common-dep success branch. Capture the realised outpath from
    # nix's stdout for the placement record; variants and
    # toolchain_validate already returned above with their own outpath
    # wiring. Toolchain *build* placement records are owned by
    # ``build_compilers_worker.py``.
    success_outpath = _last_nonblank_line(stdout)
    drv_str = drv if isinstance(drv, str) else None
    if success_outpath:
        _maybe_record_self_has(
            env, success_outpath, drv_str, "common_dep",
        )

    _LOG.info("build DONE common_dep=%s outpath=%s", name, success_outpath)
    return BuildWorkerResult(
        item_class=item_class,
        name=name,
        success=True,
        duration_seconds=max(0.0, clock() - start),
        output_path=output_path,
        outpath=success_outpath,
        drv=drv_str,
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
    parser.add_argument(
        "--shared-fs",
        type=str,
        default=None,
        help=(
            "NFS root where the cluster placement gossip lives"
            " (peers/_paths_*.jsonl). Enables targeted nix copy"
            " fetches + per-worker placement records. Absent =>"
            " worker runs without the placement map."
        ),
    )
    parser.add_argument(
        "--secondary-id",
        type=str,
        default="",
        help="This worker's secondary id; used as the placement-record author.",
    )
    parser.add_argument(
        "--signing-public-key",
        type=str,
        default="",
        help=(
            "Cluster signing public key. Authenticates the"
            " ``path-have`` push fan-out; without it placement"
            " records are still written to disk but not broadcast."
        ),
    )
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument(
        "--matrix-eval-out-dir",
        type=str,
        default=None,
        help=(
            "Shared bind-mounted directory for matrix_eval resume"
            " markers. Marker is written to"
            " ``<matrix-eval-out-dir>/<binary>/manifest.json``. Required"
            " for matrix_eval tasks; ignored by build_common_dep /"
            " build_variant / toolchain_validate types."
        ),
    )
    args, _ = parser.parse_known_args()

    # Route the worker subprocess's stdlib-logging output to a file so
    # framework runtime INFO/WARN/ERROR records aren't swallowed by the
    # silenced worker stdio. Prefer ``--log-file`` if the framework
    # passed it (multi-computer local factory does); otherwise write to
    # ``/app/log-network/worker_<pid>.log`` which is the run-wide log
    # bind-mount under the SLURM wrapper. ``force=True`` overrides any
    # handlers an import-time side-effect installed.
    import logging  # noqa: PLC0415 — late import is intentional: keep
    # the module import path lean for unit tests.
    _worker_log = args.log_file or f"/app/log-network/worker_{os.getpid()}.log"
    try:
        logging.basicConfig(
            filename=_worker_log,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            force=True,
        )
        logging.getLogger("compiler_suit_runner.build_worker.startup").info(
            "build_worker subprocess started; pid=%d argv=%r",
            os.getpid(), sys.argv,
        )
    except OSError:
        # Best-effort — if the path isn't writable (eg outside the
        # SLURM wrapper, or running unit tests with no mount), fall
        # through and let stdlib logging route to stderr (which the
        # framework silences anyway in this case). No tracebacks.
        pass

    env = BuildWorkerEnv(
        flake_ref=args.flake_ref,
        dataset_output_dir=pathlib.Path(args.dataset_output_dir),
        substituters_file=(
            pathlib.Path(args.substituters_file)
            if args.substituters_file
            else None
        ),
        shared_fs=(
            pathlib.Path(args.shared_fs) if args.shared_fs else None
        ),
        secondary_id=args.secondary_id or "",
        signing_public_key=args.signing_public_key or "",
        matrix_eval_out_dir=(
            pathlib.Path(args.matrix_eval_out_dir)
            if args.matrix_eval_out_dir
            else None
        ),
    )

    # ------------------------------------------------------------------
    # Phase 0 eval plumbing
    #
    # The framework picks a single ``worker_module`` per secondary's pool
    # (the first registered one in :func:`_phase_specs` wins), so every
    # task — matrix_eval, toolchain_validate, build_common_dep, and
    # build_variant — funnels through this unified entry point. The
    # handle closure below sniffs ``task.payload`` to decide which
    # dispatch path to take:
    #
    #   * matrix_eval payloads carry a ``binary`` + ``attr`` top-level
    #     pair (matches ``manifest_gen.make_matrix_eval_header``);
    #     dispatched to :func:`eval_worker.run_eval_task`. The eval
    #     worker writes the per-binary ``matrix-<binary>.drv.archive``
    #     to ``--matrix-eval-out-dir`` which the build_variant branch
    #     (below) imports lazily.
    #   * everything else is a build manifest;
    #     dispatched to :func:`build_worker` (this module).
    from compiler_suit_runner.workers import (  # noqa: PLC0415
        build_compilers_worker as _build_compilers_worker,
        eval_worker as _eval_worker,
    )
    from compiler_suit_runner.workers.dependency_graph_worker import (  # noqa: PLC0415
        run as _dependency_graph_run,
    )

    _handle_log = logging.getLogger("compiler_suit_runner.build_worker.handle")

    def _extract_matrix_eval_payload(payload: object) -> Optional[dict]:
        """Return the inner matrix_eval payload dict, or None if the
        task is not a matrix_eval task.

        The framework wraps the ``ManifestHeader`` into
        ``TaskInfo.payload`` so ``task.payload`` is the header_dict
        ``{item_class, name, size, payload: {...}}`` (see
        :func:`suit_task._header_to_task_info`). The inner payload is
        what :func:`eval_worker.run_eval_task` consumes; the
        ``item_class == "matrix_eval"`` marker is the
        unambiguous signal that this task targets the eval path.
        """
        if not isinstance(payload, dict):
            return None
        if payload.get("item_class") != "matrix_eval":
            return None
        inner = payload.get("payload")
        if not isinstance(inner, dict):
            return None
        # Defensive: the inner payload should carry ``binary`` + ``attr``
        # + ``sys`` per :func:`manifest_gen.make_matrix_eval_header`.
        if "binary" not in inner or "attr" not in inner:
            return None
        return inner

    def _extract_class_payload(
        payload: object, item_class: str
    ) -> Optional[dict]:
        """Return the inner payload dict if the wrapper ``item_class``
        matches; otherwise None. Used by the build_compilers branch
        below."""
        if not isinstance(payload, dict):
            return None
        if payload.get("item_class") != item_class:
            return None
        inner = payload.get("payload")
        return inner if isinstance(inner, dict) else None

    def handle(task: Task) -> Optional[WorkerOutput]:
        payload = task.payload if isinstance(task.payload, dict) else None
        # build_compilers branch — phase1 toolchain build. Owned by
        # build_compilers_worker; we delegate so the framework's single
        # ``worker_module`` per PhaseSpec stays satisfied.
        bc_payload = _extract_class_payload(payload, "build_compilers")
        if bc_payload is not None:
            bc_env = _build_compilers_worker.BuildCompilersEnv(
                flake_ref=args.flake_ref,
                out_network=(
                    pathlib.Path(args.shared_fs) / "out"
                    if args.shared_fs
                    else pathlib.Path("/app/out-network")
                ),
                substituters_file=(
                    pathlib.Path(args.substituters_file)
                    if args.substituters_file
                    else None
                ),
                shared_fs=(
                    pathlib.Path(args.shared_fs)
                    if args.shared_fs
                    else None
                ),
                secondary_id=args.secondary_id or "",
            )
            bc_name = (
                payload.get("name") if isinstance(payload, dict) else None
            ) or "<unknown>"
            result = _build_compilers_worker.run_build_compilers_task(
                bc_payload, bc_env, name=bc_name,
            )
            if not result.success:
                # Persist the nix_log_excerpt to the gateway-visible
                # build-failures dir so operators can attribute failures
                # to a specific toolchain. Mirrors the build_worker
                # standard path; worker subprocess stderr is silenced by
                # the framework so this is the only off-process surface.
                if result.nix_log_excerpt:
                    try:
                        log_dir = pathlib.Path(
                            "/app/log-network/build-failures"
                        )
                        log_dir.mkdir(parents=True, exist_ok=True)
                        (log_dir / f"{bc_name}.log").write_text(
                            f"item_class: build_compilers\n"
                            f"name: {bc_name}\n"
                            f"error: {result.error or 'unknown'}\n"
                            f"--- nix log excerpt ---\n"
                            f"{result.nix_log_excerpt}\n"
                            f"--- end ---\n",
                            encoding="utf-8",
                        )
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
                raise NonRecoverableError(
                    f"build_compilers failed: {result.error or 'unknown'}"
                )
            return WorkerOutput()

        # Phase 0 eval branch — sniff the wrapper header; if its
        # ``item_class`` matches ``matrix_eval`` the inner payload is
        # dispatched to :func:`eval_worker.run_eval_task` instead of
        # the build path.
        eval_payload = _extract_matrix_eval_payload(payload)
        if eval_payload is not None:
            if not args.matrix_eval_out_dir:
                # matrix_eval marker dir must be passed explicitly —
                # it's the bind-mounted shared output, distinct from
                # the per-secondary scratch ``--shared-fs``. The eval
                # worker writes the per-binary drv archive here for
                # phase 3 + phase 4 consumption.
                raise NonRecoverableError(
                    "matrix_eval requires --matrix-eval-out-dir (shared"
                    " bind-mounted marker dir); refusing to proceed"
                    " without it"
                )
            out_dir = pathlib.Path(args.matrix_eval_out_dir)
            _handle_log.info(
                "handle: dispatching matrix_eval task binary=%r archs=%r",
                eval_payload.get("binary"), eval_payload.get("archs"),
            )
            try:
                # Attribute lookup at call-time so tests can
                # monkeypatch ``eval_worker.run_eval_task`` and have
                # the build_worker subprocess pick up the stub.
                _eval_worker.run_eval_task(
                    eval_payload,
                    out_dir=out_dir,
                    task=task,
                )
            except RuntimeError as exc:
                _handle_log.exception(
                    "handle: run_eval_task raised RuntimeError"
                    " (retry-eligible)"
                )
                raise NonRecoverableError(
                    f"matrix_eval failed: {exc}"
                ) from exc
            except BaseException as exc:  # noqa: BLE001
                _handle_log.exception(
                    "handle: run_eval_task raised unexpectedly"
                )
                raise NonRecoverableError(
                    f"matrix_eval crashed: {type(exc).__name__}: {exc}"
                ) from exc
            return WorkerOutput()

        # dependency_graph branch — primary-affined planning task. The
        # worker pulls the matrix_aggregate drv from the predecessor
        # matrix_eval task's keyed outputs (framework-routed via
        # ``task.predecessor_outputs`` since dynamic-runner 58931e4),
        # resolves bash on the fly, then invokes
        # :func:`run_dependency_graph_task` against this single binary.
        #
        # ``matrix_eval_out_dir`` is the **container-view** archive root
        # (e.g. ``/app/out-network/_matrix_eval``) and is supplied to
        # this process via ``--matrix-eval-out-dir`` — the same flag
        # the matrix_eval branch above consults. The submitter-host
        # path is NOT valid inside the secondary container, so the
        # dep_graph payload deliberately does NOT carry it; reading
        # the host path here would make ``discover_archives`` return
        # an empty list and silently degrade phase-4 to 0/0.
        dg_payload = _extract_class_payload(payload, "dependency_graph")
        if dg_payload is not None:
            sys_name = dg_payload.get("sys") or "x86_64-linux"
            tc_drv = dg_payload.get("toolchain_aggregate_drv")
            if not isinstance(tc_drv, str) or not tc_drv:
                raise NonRecoverableError(
                    "dependency_graph payload missing 'toolchain_aggregate_drv'"
                )
            if env.matrix_eval_out_dir is None:
                raise NonRecoverableError(
                    "dependency_graph requires --matrix-eval-out-dir"
                    " (container-view shared archive root); refusing"
                    " to proceed without it"
                )
            out_dir = env.matrix_eval_out_dir
            # Single all-binaries dependency_graph task: gather the
            # matrix_aggregate_drv from EVERY matrix_eval predecessor
            # (one per binary) out of ``task.predecessor_outputs``.
            # The dependency_graph task depends ONLY on matrix_eval
            # tasks, whose task_id IS the bare binary name (a phase-local
            # id — the MATRIX_EVAL phase disambiguates, so the phase is
            # not embedded in the id). So each predecessor key is the
            # binary directly, and its published ``matrix_aggregate_drv``
            # (set by the upstream eval task via ``Task.publish_string``)
            # is what we need — the worker never needs a per-binary
            # payload.
            matrix_drvs: dict[str, str] = {}
            for pred_id, preds in task.predecessor_outputs.items():
                if not isinstance(pred_id, str) or not pred_id:
                    continue
                bname = pred_id
                entry = preds.get("matrix_aggregate_drv", {}) if isinstance(
                    preds, dict
                ) else {}
                value = entry.get("value") if isinstance(entry, dict) else None
                if not value:
                    raise NonRecoverableError(
                        "build_worker dep_graph: no matrix_aggregate_drv "
                        f"from predecessor {pred_id!r}; available keys: "
                        f"{sorted(preds.keys()) if isinstance(preds, dict) else preds!r}"
                    )
                matrix_drvs[bname] = value
            if not matrix_drvs:
                raise NonRecoverableError(
                    "build_worker dep_graph: no matrix_eval predecessor "
                    "outputs found; available predecessor task-ids: "
                    f"{sorted(task.predecessor_outputs.keys())}"
                )
            bash_path = _resolve_bash_store_path_default() or ""
            if not bash_path:
                raise NonRecoverableError(
                    "dependency_graph: bash store path unresolved"
                    " (`_resolve_bash_store_path_default()` returned"
                    " empty); check the worker environment for `nix`"
                    " on PATH and a usable nixpkgs channel"
                )
            _handle_log.info(
                "handle: dispatching dependency_graph task over %d binaries: %r",
                len(matrix_drvs), sorted(matrix_drvs),
            )
            try:
                _dependency_graph_run.run_dependency_graph_task(
                    task=task,
                    matrix_eval_out_dir=out_dir,
                    bash_path=bash_path,
                    toolchain_aggregate_drv=tc_drv,
                    matrix_drvs=matrix_drvs,
                    sys_name=sys_name,
                )
            except BaseException as exc:  # noqa: BLE001
                _handle_log.exception(
                    "handle: run_dependency_graph_task raised unexpectedly"
                )
                raise NonRecoverableError(
                    f"dependency_graph failed: {type(exc).__name__}: {exc}"
                ) from exc
            return WorkerOutput()

        # Build manifest branch — original build_worker dispatch path.
        manifest_data = payload
        manifest_path = (
            pathlib.Path(task.relative_path)
            if task.relative_path
            else pathlib.Path("<inline>")
        )
        _handle_log.info(
            "handle: starting task relative_path=%r resolved_path=%r",
            task.relative_path, task.resolved_path,
        )
        # Wrap the consumer body in a broad-catch and re-raise as
        # NonRecoverableError so we ALWAYS opt into Bug A's exit-1
        # contract on build failure. The plain ``raise
        # NonRecoverableError`` below is what we INTEND to hit, but if
        # build_worker itself (somehow) re-raises a CalledProcessError
        # or similar, the framework's runtime classifier would map it
        # to Recoverable and Bug A wouldn't fire. Catch-and-re-raise
        # makes the exception class explicit at this seam.
        try:
            result = build_worker(manifest_path, env, manifest_data=manifest_data)
        except BaseException as exc:  # noqa: BLE001
            _handle_log.exception(
                "handle: build_worker raised unexpectedly (re-raising as NonRecoverable)"
            )
            raise NonRecoverableError(
                f"build_worker crashed: {type(exc).__name__}: {exc}"
            ) from exc
        _handle_log.info(
            "handle: build_worker returned success=%s name=%r error=%r",
            result.success, result.name, (result.error or "")[:200],
        )
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

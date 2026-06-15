"""Archive discovery, variant-lookup derivation, presence probe + import.

Per-binary variant-lookup derivation
------------------------------------

The phase-2 ``matrix_eval`` worker writes a self-contained
``matrix-<binary>.drv.archive`` whose root is the ``matrix-<binary>``
aggregate drv. The filename mirrors the ``matrix-<binary>.drv``
storename pattern so the archive is self-identifying as a matrix-
aggregate drv export. Phase 3 imports the archive into the local
store and then enumerates the variant leaves by walking ONE local
``nix-store --query --references <matrix-<binary>.drv>`` —
cheap, local-store-only, no flake re-evaluation. Filtering the
references to ``*-elf-folder.drv`` recovers the variant roots; every
other reference (toolchain aggregate, bash, ...) is dropped.

The earlier per-archive JSON sidecar and the
``matrix_eval__<binary>.json`` defensive secondary discovery have
been retired, along with the legacy stdout-walking
``discover_kept_drvs_from_imported_store`` helper; callers MUST use
:func:`derive_variant_lookup_from_aggregate`.
"""

from __future__ import annotations

import errno
import json
import logging
import pathlib
import random
import subprocess
import time
from typing import Optional

from template_graph.tree_walker import VARIANT_SUFFIX, parse_variant_path

from compiler_suit_runner.preflight import _short_dataset_name

from .subproc import RunSubprocess, default_run_subprocess, resolve_tool


__all__ = [
    "derive_variant_lookup_from_aggregate",
    "discover_archives",
    "import_archive",
    "is_path_locally_present",
    "binary_from_archive_name",
    "toolchain_archive_path",
    "DRV_MAP_SIDECAR",
    "write_drv_map_sidecar",
    "read_drv_map_sidecar",
    "load_drv_map_from_sidecars",
]


_ARCHIVE_PREFIX = "matrix-"
_ARCHIVE_SUFFIX = ".drv.archive"

# Sidecar JSON written by the matrix_eval worker alongside each
# ``matrix-<binary>.drv.archive``.  Persists the ``matrix_aggregate_drv``
# path so a ``--prestaged-matrix-eval`` re-run can recover the
# binary → drv mapping without re-running matrix_eval.  The file is
# named to be clearly associated with its binary and not confused with an
# archive or any other per-run artefact.
DRV_MAP_SIDECAR = "matrix-{binary}.drv_map.json"

# The shared toolchain archive (toolchain-dedup pre-flight artefact).
# Named OUTSIDE the ``matrix-`` prefix so :func:`discover_archives`
# never picks it up as a per-binary matrix archive; consumers import it
# explicitly (toolchain-first) via :func:`toolchain_archive_path`.
TOOLCHAIN_ARCHIVE_NAME = "toolchains.drv.archive"


def toolchain_archive_path(out_dir: pathlib.Path) -> pathlib.Path:
    """Return the shared ``toolchains.drv.archive`` path under ``out_dir``.

    Fixed-name lookup mirroring the per-binary
    ``matrix-<binary>.drv.archive`` convention. The toolchain-dedup
    pre-flight writes this once per run; every consumer imports it
    FIRST (before any ``matrix-*`` diff archive).
    """
    return out_dir / TOOLCHAIN_ARCHIVE_NAME


def binary_from_archive_name(archive: pathlib.Path) -> str:
    """Recover ``<binary>`` from a ``matrix-<binary>.drv.archive`` path.

    Returns the substring between the ``matrix-`` prefix and the
    ``.drv.archive`` suffix. If the filename does not match the
    expected shape the full ``archive.name`` is returned unchanged —
    callers use the result for log / error context only, never for
    correctness-critical routing.
    """
    name = archive.name
    if name.startswith(_ARCHIVE_PREFIX) and name.endswith(_ARCHIVE_SUFFIX):
        return name[len(_ARCHIVE_PREFIX):-len(_ARCHIVE_SUFFIX)]
    return name


# ---------------------------------------------------------------------------
# Per-binary matrix_aggregate_drv sidecar (``--prestaged-matrix-eval``)
# ---------------------------------------------------------------------------


def write_drv_map_sidecar(
    out_dir: pathlib.Path,
    binary: str,
    matrix_aggregate_drv: str,
) -> None:
    """Write the per-binary drv-map sidecar JSON atomically.

    Persists the ``{binary: matrix_aggregate_drv}`` pair so a subsequent
    ``--prestaged-matrix-eval`` run can recover the drv map without
    re-running the matrix_eval phase.  Written to
    ``<out_dir>/matrix-<binary>.drv_map.json`` (alongside the
    corresponding ``matrix-<binary>.drv.archive``).

    Writes atomically (temp file → rename) so a reader never sees a
    partial sidecar on a shared filesystem.  Raises :class:`OSError` on
    write failure — the caller (``run_eval_task``) logs and continues so
    a write failure here never fails the matrix_eval task itself.
    """
    sidecar_path = out_dir / DRV_MAP_SIDECAR.format(binary=binary)
    payload = {"binary": binary, "matrix_aggregate_drv": matrix_aggregate_drv}
    tmp = sidecar_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(sidecar_path)


def read_drv_map_sidecar(
    out_dir: pathlib.Path,
    binary: str,
) -> Optional[str]:
    """Read back the ``matrix_aggregate_drv`` for ``binary`` from its sidecar.

    Returns the drv path string on success, or ``None`` when the sidecar
    is absent, unreadable, or malformed — callers raise a clear error
    when ``None`` is returned in prestaged mode.
    """
    sidecar_path = out_dir / DRV_MAP_SIDECAR.format(binary=binary)
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    drv = data.get("matrix_aggregate_drv")
    if not isinstance(drv, str) or not drv:
        return None
    return drv


def load_drv_map_from_sidecars(
    out_dir: pathlib.Path,
    wanted_binaries: set[str],
) -> dict[str, str]:
    """Load the ``{binary: matrix_aggregate_drv}`` map from sidecar files.

    Reads ``matrix-<binary>.drv_map.json`` for every binary in
    ``wanted_binaries`` and returns the populated dict.  Raises
    :class:`FileNotFoundError` (with a clear message) for any binary
    whose sidecar is absent or unreadable — callers in prestaged mode
    treat a missing sidecar as a fatal configuration error (the user
    must have run matrix_eval at least once before using
    ``--prestaged-matrix-eval``).
    """
    drv_map: dict[str, str] = {}
    for binary in sorted(wanted_binaries):
        drv = read_drv_map_sidecar(out_dir, binary)
        if drv is None:
            sidecar = out_dir / DRV_MAP_SIDECAR.format(binary=binary)
            raise FileNotFoundError(
                f"--prestaged-matrix-eval: sidecar missing or unreadable"
                f" for binary {binary!r}: {sidecar}"
                " — run matrix_eval at least once (without"
                " --prestaged-matrix-eval) to populate the sidecars."
            )
        drv_map[binary] = drv
    return drv_map


logger = logging.getLogger("compiler_suit_runner.dependency_graph_worker")


# ---------------------------------------------------------------------------
# Transient-failure retry policy for ``import_archive``
#
# Production evidence (run_20260611_123632): under fork/pids pressure or
# network-mount backpressure, spawning ``nix-store --import`` raises
# ``[Errno 11] Resource temporarily unavailable``. EAGAIN is TRANSIENT,
# but a single failed import used to kill the worker NonRecoverable; the
# respawned worker re-ran the import (the memo flags are per-process),
# hit EAGAIN again, and died again — a respawn loop that took out whole
# secondaries. Retrying INSIDE import_archive lets every caller
# (toolchain import, per-binary import, eval path, dep_graph run) ride
# out a transient blip, while a 30s-persistent condition still fails
# loud exactly as before.
# ---------------------------------------------------------------------------

# 1 initial attempt + 5 retries; sleeps between attempts follow the
# schedule below (plus up to 25% jitter), ~31s total worst case.
_IMPORT_MAX_ATTEMPTS = 6
_IMPORT_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0)
_IMPORT_JITTER_FRACTION = 0.25

# OSError errnos that indicate a transient spawn/read condition (fork
# pressure, signal interruption, momentary memory pressure) rather than
# a permanent error like a missing or unreadable archive.
_TRANSIENT_ERRNOS = frozenset({
    errno.EAGAIN,
    errno.EWOULDBLOCK,  # == EAGAIN on Linux; kept for portability
    errno.EINTR,
    errno.ENOMEM,
})

# stderr substrings (lower-cased match) from a non-zero ``nix-store
# --import`` that indicate the SAME transient conditions surfacing
# inside the child instead of at spawn time. Deliberately narrow:
# "does not exist" / corrupt-archive / any other nix error stays
# permanent and fails fast.
_TRANSIENT_STDERR_MARKERS = (
    b"resource temporarily unavailable",
    b"cannot fork",
    b"unable to fork",
    b"cannot allocate memory",
    b"interrupted system call",
)

# Injectable sleep hook so tests can assert the backoff schedule without
# actually sleeping. Production never overrides it.
_retry_sleep = time.sleep


def _is_transient_oserror(exc: OSError) -> bool:
    """True iff the OSError errno marks a retryable transient condition."""
    return exc.errno in _TRANSIENT_ERRNOS


def _is_transient_stderr(stderr: bytes) -> bool:
    """True iff a non-zero rc's stderr indicates a transient condition."""
    low = stderr.lower()
    return any(marker in low for marker in _TRANSIENT_STDERR_MARKERS)


def discover_archives(
    matrix_eval_out_dir: pathlib.Path,
    *,
    wanted_binaries: Optional[set[str]] = None,
) -> list[pathlib.Path]:
    """Return ``matrix-<binary>.drv.archive`` files under ``matrix_eval_out_dir``.

    Sorted by filename for deterministic processing order so the
    resulting plan and any operator log line is stable across runs. The
    ``matrix-<binary>.drv.archive`` filename mirrors the matrix-aggregate
    drv's storename so the archive is self-identifying without
    out-of-band metadata.

    ``wanted_binaries`` scopes the result to a run's OWN binaries. The
    ``out/_matrix_eval/`` directory persists across runs (a more-packages
    run submitted into an existing dataset's shared FS reuses it), so it
    can hold STALE archives from prior runs — exported by a different
    image whose cross-image ``nix-store --import`` fails — that this run
    neither needs nor can import. Passing the run's binary set drops them
    before the import step. ``None`` (the default) returns every archive,
    for ad-hoc / single-run callers whose directory holds only their own.
    """
    if not matrix_eval_out_dir.is_dir():
        return []
    # The ``matrix-`` prefix filter intentionally EXCLUDES the shared
    # ``toolchains.drv.archive`` (toolchain-dedup artefact) — it is
    # imported explicitly toolchain-first by the consumers, never as a
    # per-binary matrix archive.
    return sorted(
        p for p in matrix_eval_out_dir.iterdir()
        if p.is_file()
        and p.name.startswith("matrix-")
        and p.name.endswith(".drv.archive")
        and (wanted_binaries is None
             or binary_from_archive_name(p) in wanted_binaries)
    )


def is_path_locally_present(
    store_path: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> bool:
    """Return True iff ``store_path`` is in the local nix store.

    Uses ``nix path-info`` (cheap; no realise side effect). Anything
    non-zero counts as "not present" and triggers an import.
    """
    runner = run_subprocess or default_run_subprocess
    _stdout, _stderr, rc = runner([
        "nix", "path-info", "--store", "/nix/store", store_path,
    ])
    return rc == 0


def import_archive(
    archive: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> tuple[bool, bytes, list[str]]:
    """``nix-store --import < <archive>`` into the local store.

    Returns ``(success, stderr_bytes, imported_paths)``.

    ``nix-store --import`` prints one freshly-imported store path per
    line on stdout. We capture those and surface them as
    ``imported_paths`` so the caller can derive the kept-drv list
    (variant ``*-elf-folder.drv`` roots) without a sidecar JSON.

    On any failure path (missing archive, OSError, non-zero rc), the
    ``imported_paths`` list is empty. Streams the file into stdin to
    avoid loading multi-GiB archives into Python memory.

    ``run_subprocess`` is honoured only for the argv-sniffing test
    stub (when it is callable AND advertises the ``_stdin_aware``
    attribute set to True); otherwise the production
    ``subprocess.run`` with explicit stdin is always used, even when
    callers thread a runner through for other helpers (e.g. the
    ``is_path_locally_present`` probe). Previously this branched on
    ``run_subprocess is None`` alone and a production caller passing
    a real subprocess wrapper would take the argv-stub path, handing
    nix-store a literal ``<<N bytes>>`` positional that triggers
    ``error: no arguments expected``.

    TRANSIENT failures (EAGAIN/EWOULDBLOCK/EINTR/ENOMEM from spawning
    or reading, or a non-zero rc whose stderr indicates fork/memory
    pressure — see :data:`_TRANSIENT_STDERR_MARKERS`) are retried with
    exponential backoff + jitter (up to :data:`_IMPORT_MAX_ATTEMPTS`
    attempts, ~31s total) before the failure is surfaced. Permanent
    errors (archive missing, corrupt archive, nix "does not exist")
    fail fast on the first attempt. The retry lives HERE so every
    caller (toolchain import, per-binary import, eval path, dep_graph
    run) inherits it.
    """
    if not archive.is_file():
        return False, f"archive not found: {archive}".encode("utf-8"), []

    for attempt in range(1, _IMPORT_MAX_ATTEMPTS + 1):
        success, stderr, imported, transient = _import_archive_once(
            archive, run_subprocess,
        )
        if success or not transient:
            return success, stderr, imported
        if attempt == _IMPORT_MAX_ATTEMPTS:
            logger.warning(
                "import of %s still failing transiently after %d "
                "attempts — giving up: %s",
                archive, attempt,
                stderr.decode("utf-8", errors="replace").strip(),
            )
            return success, stderr, imported
        base = _IMPORT_BACKOFF_SECONDS[
            min(attempt - 1, len(_IMPORT_BACKOFF_SECONDS) - 1)
        ]
        delay = base + random.uniform(0, base * _IMPORT_JITTER_FRACTION)
        logger.warning(
            "transient failure importing %s (attempt %d/%d): %s — "
            "retrying in %.1fs",
            archive, attempt, _IMPORT_MAX_ATTEMPTS,
            stderr.decode("utf-8", errors="replace").strip(), delay,
        )
        _retry_sleep(delay)
    raise AssertionError("unreachable: retry loop must return")


def _import_archive_once(
    archive: pathlib.Path,
    run_subprocess: Optional[RunSubprocess],
) -> tuple[bool, bytes, list[str], bool]:
    """One ``nix-store --import`` attempt.

    Returns ``(success, stderr_bytes, imported_paths, transient)``
    where ``transient`` marks a retryable failure (always ``False`` on
    success). Shared by both the ``_stdin_aware`` test-stub branch and
    the production direct-subprocess branch so the retry classification
    is identical for unit tests and real workers.
    """
    if run_subprocess is not None and getattr(
        run_subprocess, "_stdin_aware", False,
    ):
        try:
            contents = archive.read_bytes()
        except OSError as exc:
            return (
                False, str(exc).encode("utf-8"), [],
                _is_transient_oserror(exc),
            )
        stdout, stderr, rc = run_subprocess([
            "nix-store", "--import", f"<<{len(contents)}bytes>>",
        ])
        if rc != 0:
            return False, stderr, [], _is_transient_stderr(stderr or b"")
        return True, stderr, _split_import_stdout(stdout), False

    try:
        with open(archive, "rb") as fh:
            proc = subprocess.run(  # noqa: S603
                [resolve_tool("nix-store"), "--import"],
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
    except OSError as exc:
        return (
            False, str(exc).encode("utf-8"), [],
            _is_transient_oserror(exc),
        )
    if proc.returncode != 0:
        stderr = proc.stderr or b""
        return False, stderr, [], _is_transient_stderr(stderr)
    return (
        True, proc.stderr or b"",
        _split_import_stdout(proc.stdout or b""), False,
    )


def _split_import_stdout(stdout: bytes) -> list[str]:
    """Parse ``nix-store --import`` stdout into a list of store paths.

    Empty lines are skipped; non-empty lines are returned in the
    order ``nix-store`` emitted them. ``nix-store --import`` prints
    one absolute store path per line, e.g.
    ``/nix/store/<hash>-<drv_basename>``.
    """
    text = stdout.decode("utf-8", errors="replace") if stdout else ""
    return [line for line in (raw.strip() for raw in text.splitlines()) if line]


# ---------------------------------------------------------------------------
# Variant-lookup derivation from a matrix aggregate drv
# ---------------------------------------------------------------------------


def derive_variant_lookup_from_aggregate(
    aggregate_drv_path: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
    toolchain_outpaths_map: Optional[dict[str, str]] = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Enumerate variant leaves referenced by a ``matrix-<binary>`` drv.

    Runs ONE local ``nix-store --query --references <aggregate>`` —
    cheap, local-store-only, no flake re-evaluation. Filters to
    ``*-elf-folder.drv`` leaves (the variant outputs), parses each
    via :func:`template_graph.tree_walker.parse_variant_path`, and
    composes the ``(arch, label)`` key the same way the streaming
    planner's ``cur_label`` does in
    :func:`template_graph.streaming.dispatch._on_matrix_depth2`:
    ``label = f"{binary}__{arch}__{suffix}"`` where ``suffix`` is the
    drv-basename substring between ``<binary>-<arch>-`` and
    ``-elf-folder.drv``.

    Returns ``{(arch, label): <variant_spec>}`` where each
    ``variant_spec`` is the full worker-visible mapping built by
    :func:`_variant_spec_from_parsed` — ``drv``, ``pkg``, ``arch``,
    ``label``, ``suffix``, ``compiler_id``, ``compiler_family``,
    ``compiler_version``, ``optimization``, ``variant_dir``,
    ``metadata_name``, and ``toolchain_outpath``. ``variant_dir`` is
    what the build worker uses for ELF placement, so it MUST be
    non-empty; deriving it here (not leaving it for the descriptor to
    default to ``""``) is what keeps the spawned ``build_variant`` task
    complete. Non-elf-folder references (toolchain aggregate, bash, ...)
    are filtered out. Leaves whose basename fails ``parse_variant_path``
    are skipped with a WARN log line — they are not legitimate variant
    roots, so they cannot land in the lookup.

    ``toolchain_outpaths_map`` is an optional ``{"<arch>/<comp>": outpath}``
    dict (built by the submitter from the same drv→outpath table used to
    produce the split archives). When provided, the per-variant
    ``toolchain_outpath`` is looked up by ``"<arch>/<comp>"`` and stored
    in the spec so the build worker can import the correct per-toolchain
    delta archive without a separate payload field injection step.

    Raises:
      RuntimeError: if ``nix-store --query --references`` exits
        non-zero (the aggregate drv is missing locally, or nix-store
        itself is broken). The stderr text is surfaced verbatim.
      ValueError: if two distinct leaves collide on the same
        ``(arch, label)`` key. The matrix is supposed to deduplicate
        at the sampling stage; a collision here is a contract
        violation upstream that must be surfaced loudly rather than
        silently overwriting an entry.
    """
    runner = run_subprocess or default_run_subprocess
    stdout, stderr, rc = runner([
        "nix-store", "--query", "--references", aggregate_drv_path,
    ])
    if rc != 0:
        raise RuntimeError(
            "nix-store --query --references failed (rc="
            f"{rc}): {stderr.decode('utf-8', errors='replace').strip()}"
        )

    references = [
        line for line in (
            raw.strip()
            for raw in stdout.decode("utf-8", errors="replace").splitlines()
        )
        if line
    ]

    lookup: dict[tuple[str, str], dict[str, str]] = {}
    for path in references:
        if not path.endswith(VARIANT_SUFFIX):
            continue
        drv_basename = _post_hash_basename(path)
        if drv_basename is None:
            continue
        try:
            binary, arch, comp, opt = parse_variant_path(drv_basename)
        except Exception as exc:  # noqa: BLE001 - TreeWalkError + future kinds
            logger.warning(
                "skipping unparseable variant drv %r: %s", path, exc,
            )
            continue
        suffix = drv_basename[
            len(binary) + 1 + len(arch) + 1 : -len(VARIANT_SUFFIX)
        ]
        label = f"{binary}__{arch}__{suffix}"
        key = (arch, label)
        if key in lookup:
            raise ValueError(
                f"duplicate variant key {key!r} from aggregate "
                f"{aggregate_drv_path!r}: {lookup[key]['drv']!r} vs "
                f"{path!r} — the matrix is supposed to deduplicate "
                "at sampling time"
            )
        tc_outpath = ""
        if toolchain_outpaths_map:
            tc_outpath = toolchain_outpaths_map.get(f"{arch}/{comp}", "")
        lookup[key] = _variant_spec_from_parsed(
            path=path,
            binary=binary,
            arch=arch,
            comp=comp,
            opt=opt,
            label=label,
            suffix=suffix,
            toolchain_outpath=tc_outpath,
        )
    return lookup


def _variant_spec_from_parsed(
    *,
    path: str,
    binary: str,
    arch: str,
    comp: str,
    opt: str,
    label: str,
    suffix: str,
    toolchain_outpath: str = "",
) -> dict[str, str]:
    """Build the worker-visible variant_spec from a parsed variant drv.

    The legacy JSON / preflight path filled ``variant_dir``,
    ``compiler_id`` and the rest from the Nix-side ``_meta`` map. On
    the dependency-graph path the matrix-aggregate references give us
    only the drv store path, so we recover every field the build
    worker reads from the post-hash basename instead:

    * ``compiler_id`` / ``optimization`` come straight from
      :func:`parse_variant_path` (``comp`` / ``opt``).
    * ``compiler_family`` is the alphabetic prefix of ``comp``
      (``gcc15`` -> ``gcc``); ``compiler_version`` is the remainder
      (``15``), with underscores normalised to dots (``4_4`` -> ``4.4``)
      to match the dotted versions the metadata sidecar expects.
    * ``variant_dir`` / ``metadata_name`` use the SAME
      :func:`compiler_suit_runner.preflight._short_dataset_name`
      formatter the legacy path uses, hashed over ``label`` so the
      ELF placement subdir is stable + collision-free
      (``dataset/<pkg>/<variant_dir>/<elf>``).
    * ``toolchain_outpath`` is the realized ``/nix/store/<hash>-<name>``
      output of the variant's cross-toolchain derivation. The build
      worker uses it to import the correct per-toolchain delta archive
      (``toolchains.<hash>.out.archive``).  Empty string means the
      outpath wasn't supplied (older dispatch path or unresolved drv).

    ``flag_set`` / ``hardening`` / ``sanitizer`` / ``march`` are not
    recoverable from the drv basename (they collapse into ``suffix``);
    they stay empty here and only feed the best-effort sidecar JSON,
    never build correctness.
    """
    family = comp.rstrip("0123456789_")
    version = comp[len(family):].lstrip("-_").replace("_", ".")
    variant_dir = _short_dataset_name(
        compiler_id=comp, arch=arch, optimization=opt, full_label=label,
    )
    return {
        "drv": path,
        "pkg": binary,
        "arch": arch,
        "label": label,
        "suffix": suffix,
        "compiler_id": comp,
        "compiler_family": family,
        "compiler_version": version,
        "optimization": opt,
        "variant_dir": variant_dir,
        "metadata_name": f"{variant_dir}.json",
        "toolchain_outpath": toolchain_outpath,
    }


def _post_hash_basename(store_path: str) -> Optional[str]:
    """Return the post-hash basename of a ``/nix/store/<hash>-<name>`` path.

    Drops the ``/nix/store/`` prefix and the ``<hash>-`` prefix
    (32-char base32 hash + dash). Returns ``None`` on shape
    mismatch — production callers either skip such entries or log
    them upstream.
    """
    prefix = "/nix/store/"
    if not store_path.startswith(prefix):
        return None
    rest = store_path[len(prefix):]
    # nix base32 hash is 32 chars followed by a dash.
    if len(rest) < 33 or rest[32] != "-":
        return None
    return rest[33:]

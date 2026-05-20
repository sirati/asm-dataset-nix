"""Archive discovery, variant-lookup derivation, presence probe + import.

Per-binary variant-lookup derivation
------------------------------------

The phase-2 ``matrix_eval`` worker writes a self-contained
``<binary>.nix-archive`` whose root is the ``matrix-<binary>``
aggregate drv. Phase 3 imports the archive into the local store and
then enumerates the variant leaves by walking ONE local
``nix-store --query --references <matrix-<binary>.drv>`` —
cheap, local-store-only, no flake re-evaluation. Filtering the
references to ``*-elf-folder.drv`` recovers the variant roots; every
other reference (toolchain aggregate, bash, ...) is dropped.

The earlier ``<binary>.nix-archive.json`` sidecar and the
``matrix_eval__<binary>.json`` defensive secondary discovery have
been retired, along with the legacy stdout-walking
``discover_kept_drvs_from_imported_store`` helper; callers MUST use
:func:`derive_variant_lookup_from_aggregate`.
"""

from __future__ import annotations

import logging
import pathlib
import subprocess
from typing import Optional

from template_graph.tree_walker import VARIANT_SUFFIX, parse_variant_path

from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "derive_variant_lookup_from_aggregate",
    "discover_archives",
    "import_archive",
    "is_path_locally_present",
]


logger = logging.getLogger("compiler_suit_runner.dependency_graph_worker")


def discover_archives(matrix_eval_out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``.

    Sorted by binary name (== file stem) for deterministic processing
    order so the resulting ``_dependency_graph.pkl`` and any operator
    log line is stable across runs.
    """
    if not matrix_eval_out_dir.is_dir():
        return []
    return sorted(
        p for p in matrix_eval_out_dir.iterdir()
        if p.is_file() and p.suffix == ".nix-archive"
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
    """
    if not archive.is_file():
        return False, f"archive not found: {archive}".encode("utf-8"), []

    if run_subprocess is not None and getattr(
        run_subprocess, "_stdin_aware", False,
    ):
        try:
            contents = archive.read_bytes()
        except OSError as exc:
            return False, str(exc).encode("utf-8"), []
        stdout, stderr, rc = run_subprocess([
            "nix-store", "--import", f"<<{len(contents)}bytes>>",
        ])
        if rc != 0:
            return False, stderr, []
        return True, stderr, _split_import_stdout(stdout)

    try:
        with open(archive, "rb") as fh:
            proc = subprocess.run(  # noqa: S603
                ["nix-store", "--import"],
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0:
            return False, proc.stderr or b"", []
        return True, proc.stderr or b"", _split_import_stdout(proc.stdout or b"")
    except OSError as exc:
        return False, str(exc).encode("utf-8"), []


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

    Returns ``{(arch, label): {"drv": <store_path>, "arch": <arch>,
    "label": <label>, "suffix": <suffix>}}``. Non-elf-folder
    references (toolchain aggregate, bash, ...) are filtered out.
    Leaves whose basename fails ``parse_variant_path`` are skipped
    with a WARN log line — they are not legitimate variant roots, so
    they cannot land in the lookup.

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
            binary, arch, _comp, _opt = parse_variant_path(drv_basename)
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
        lookup[key] = {
            "drv": path,
            "arch": arch,
            "label": label,
            "suffix": suffix,
        }
    return lookup


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

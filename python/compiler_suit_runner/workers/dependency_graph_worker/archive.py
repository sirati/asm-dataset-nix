"""Archive discovery, kept-drv discovery, presence probe + import.

Per-binary kept-drv discovery
-----------------------------

The phase-2 ``matrix_eval`` worker writes a self-contained
``<binary>.nix-archive`` PLUS a sidecar JSON
``<binary>.nix-archive.json`` that lists the ROOT (kept) drvs via
``variant_drvs``. The closure walk via
``nix-store --query --requisites`` would conflate roots with
transitive dependencies, so the sidecar is the canonical source.

Two discovery modes, tried in order (first hit wins):

  * **Sidecar JSON** at ``<binary>.nix-archive.json`` — primary,
    written by the matrix_eval worker alongside the archive.
  * **Per-binary matrix_eval header** at
    ``<manifest_dir>/matrix_eval__<binary>.json`` — DEFENSIVE.
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess
from typing import Optional

from .subproc import RunSubprocess, default_run_subprocess


__all__ = [
    "discover_archives",
    "is_path_locally_present",
    "import_archive",
    "discover_kept_drvs",
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
) -> tuple[bool, bytes]:
    """``nix-store --import < <archive>`` into the local store.

    Returns ``(success, stderr_bytes)``. Failure surfaces stderr so the
    caller can include it in a per-binary error message.

    Streams the file into stdin to avoid loading multi-GiB archives
    into Python memory. ``run_subprocess`` is honoured only for the
    argv-sniffing test stub (when it is callable AND advertises the
    ``_stdin_aware`` attribute set to True); otherwise the production
    ``subprocess.run`` with explicit stdin is always used, even when
    callers thread a runner through for other helpers (e.g. the
    ``is_path_locally_present`` probe). Previously this branched on
    ``run_subprocess is None`` alone and a production caller passing a
    real subprocess wrapper would take the argv-stub path, handing
    nix-store a literal ``<<N bytes>>`` positional that triggers
    ``error: no arguments expected``.
    """
    if not archive.is_file():
        return False, f"archive not found: {archive}".encode("utf-8")

    if run_subprocess is not None and getattr(
        run_subprocess, "_stdin_aware", False,
    ):
        try:
            contents = archive.read_bytes()
        except OSError as exc:
            return False, str(exc).encode("utf-8")
        _stdout, stderr, rc = run_subprocess([
            "nix-store", "--import", f"<<{len(contents)}bytes>>",
        ])
        return rc == 0, stderr

    try:
        with open(archive, "rb") as fh:
            proc = subprocess.run(  # noqa: S603
                ["nix-store", "--import"],
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        return proc.returncode == 0, proc.stderr or b""
    except OSError as exc:
        return False, str(exc).encode("utf-8")


# ---------------------------------------------------------------------------
# Kept-drv discovery
# ---------------------------------------------------------------------------


def _load_sidecar(archive: pathlib.Path) -> Optional[dict]:
    """Read ``<archive>.json`` if it exists, else None."""
    sidecar = archive.with_suffix(archive.suffix + ".json")
    if not sidecar.exists():
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _load_matrix_eval_header(
    manifest_dir: pathlib.Path, binary: str,
) -> Optional[dict]:
    """Read ``<manifest_dir>/matrix_eval__<binary>.json`` if present.

    Defensive secondary discovery — the sidecar is the primary source
    (see :func:`discover_kept_drvs`). Today's
    :func:`manifest_gen.make_matrix_eval_header` doesn't include
    ``variant_drvs`` (the header is built at submit-time, before drvs
    are realised) so this path returns ``None`` in production unless a
    future header iteration adds the field.
    """
    path = manifest_dir / f"matrix_eval__{binary}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_drvs_from_payload(payload: object) -> tuple[list[str], dict]:
    """Pull a kept-drv list + a variant-lookup dict out of a payload.

    Accepts:

      * ``payload["variant_drvs"]`` — flat list of store paths (post-B.1a
        canonical shape).
      * ``payload["variants"]`` — list of ``{label, drv, ...}`` dicts
        (legacy phase0_eval marker shape).

    Returns ``(variant_drvs, variant_lookup_by_arch_label)``. The
    lookup is keyed by ``(arch, label)`` matching
    ``BinaryPlanInput.variant_lookup``. When ``variants`` is absent the
    lookup is empty.
    """
    if not isinstance(payload, dict):
        return [], {}
    variant_lookup: dict[tuple[str, str], dict] = {}
    drvs: list[str] = []
    raw_drvs = payload.get("variant_drvs")
    if isinstance(raw_drvs, list):
        drvs = [d for d in raw_drvs if isinstance(d, str) and d.endswith(".drv")]
    raw_variants = payload.get("variants")
    if isinstance(raw_variants, list):
        for entry in raw_variants:
            if not isinstance(entry, dict):
                continue
            drv = entry.get("drv")
            arch = entry.get("arch")
            label = entry.get("label") or entry.get("suffix")
            if isinstance(drv, str) and drv.endswith(".drv"):
                drvs.append(drv)
            if isinstance(arch, str) and isinstance(label, str):
                variant_lookup[(arch, str(label))] = dict(entry)
    # Deduplicate while preserving deterministic order.
    seen: set[str] = set()
    deduped: list[str] = []
    for d in drvs:
        if d in seen:
            continue
        seen.add(d)
        deduped.append(d)
    return deduped, variant_lookup


def discover_kept_drvs(
    archive: pathlib.Path,
    manifest_dir: Optional[pathlib.Path],
) -> tuple[list[str], dict[tuple[str, str], dict]]:
    """Locate the per-binary kept-drv list + variant lookup.

    Tries the sidecar JSON first, then the matrix_eval header. Returns
    ``([], {})`` when neither source surfaces a drv list — the caller
    treats that as "binary has no plannable variants" and skips it.
    """
    binary = archive.stem  # ``<binary>.nix-archive`` → ``<binary>``

    sidecar = _load_sidecar(archive)
    if sidecar is not None:
        drvs, lookup = _extract_drvs_from_payload(sidecar)
        if drvs:
            return drvs, lookup

    if manifest_dir is not None:
        header = _load_matrix_eval_header(manifest_dir, binary)
        if header is not None:
            inner = header.get("payload") if isinstance(header, dict) else None
            drvs, lookup = _extract_drvs_from_payload(inner)
            if drvs:
                return drvs, lookup

    logger.warning(
        "no kept-drv source found for binary %r at %r; skipping",
        binary, str(archive),
    )
    return [], {}

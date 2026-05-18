"""Phase 3 ``dependency_graph`` worker — primary-only template-graph adapter.

After all phase-2 ``matrix_eval`` tasks quiesce, the watcher on the
primary calls this worker to translate the per-binary ``.nix-archive``
artefacts into a phase-4 task plan (build_common_dep + build_variant).

Worker flow per binary:

  1. ``nix-store --import < <matrix_eval_out_dir>/<binary>.nix-archive``
     loads the kept variant drvs + their transitive input closure into
     the primary's local store. Skipped per-binary when every kept-drv
     in the archive's sidecar is already present locally (resume
     fast-path).
  2. Discover the kept variant-drv list. See "Per-binary kept-drv
     discovery" below.
  3. ``template_graph.make_sum_drv.make_sum_drv_from_paths`` glues
     toolchains + per-binary kept-drvs into a single sum-root drv;
     ``nix-store --query --tree`` walks that into the line-by-line
     tree the streaming planner consumes.
  4. ``template_graph.streaming.plan_from_tree_streaming`` produces the
     classified template graph.
  5. ``dependency_graph_planner.plan_phase4_from_graph`` adapts the
     streaming result into a flat list of
     :class:`Phase4Descriptor` records.
  6. The descriptor list is JSON-dumped to
     ``<matrix_eval_out_dir>/_dependency_graph.json`` for the
     primary-side spawn-tasks step to pick up.

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
    written by the matrix_eval worker alongside the archive (see
    :mod:`workers.eval_worker._write_sidecar`). Contains
    ``variant_drvs`` and optionally a ``variants`` list of
    ``{label, drv, arch, suffix, ...}`` dicts for variant_lookup
    population.
  * **Per-binary matrix_eval header** at
    ``<manifest_dir>/matrix_eval__<binary>.json`` — DEFENSIVE.
    Today's ``make_matrix_eval_header`` doesn't include
    ``variant_drvs`` (the header is built at submit-time, before drvs
    are realised) but a future iteration may; if so, the worker reads
    it without source changes. Used only when the sidecar is missing
    or has no kept drvs.

The legacy ``phase0_eval__<binary>.json`` fallback was removed per
A6's hard wire-protocol cutover — any pre-rename run leftover on disk
is operator triage territory, not a code path the worker should keep
alive.

Cycle handling
--------------

``dependency_graph_planner.plan_phase4_from_graph`` raises
:class:`DependencyGraphCycleError` if a template's child-id walk
detects a back-edge. The watcher re-raises that as a hard failure
(nix drv graphs are DAGs by construction; a cycle means the matrix
output is corrupt and no retry will help). We let it propagate.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, Optional


__all__ = [
    "DependencyGraphWorkerError",
    "DependencyGraphResult",
    "discover_archives",
    "discover_kept_drvs",
    "import_archive",
    "is_path_locally_present",
    "build_sum_drv",
    "query_drv_tree",
    "plan_binary",
    "write_dependency_graph_json",
    "run_dependency_graph_task",
    "main",
]


logger = logging.getLogger("compiler_suit_runner.dependency_graph_worker")


# Output filename written under ``<matrix_eval_out_dir>``.
DEPENDENCY_GRAPH_JSON = "_dependency_graph.json"


# Subprocess runner injection point (mirrors other workers).
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DependencyGraphWorkerError(Exception):
    """Raised on any binary's planning failure.

    The worker's exit code is nonzero on any one binary's failure;
    this exception carries a structured ``binary`` + ``stage`` so the
    watcher can attach context to its log line.
    """

    def __init__(
        self,
        binary: str,
        stage: str,
        message: str,
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(f"binary={binary!r} stage={stage!r}: {message}")
        self.binary = binary
        self.stage = stage
        self.cause = cause


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DependencyGraphResult:
    """Outcome of a single :func:`run_dependency_graph_task` invocation."""

    output_path: pathlib.Path
    binary_count: int
    descriptor_count: int
    duration_seconds: float


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Archive discovery + import
# ---------------------------------------------------------------------------


def discover_archives(matrix_eval_out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``.

    Sorted by binary name (== file stem) for deterministic processing
    order so the resulting ``_dependency_graph.json`` and any operator
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
    non-zero counts as "not present" and triggers an import. Mirrors
    :func:`peer_paths_fetch.is_path_locally_valid` but with a smaller
    surface — we don't care about the nars / signature checks here.
    """
    runner = run_subprocess or _default_run_subprocess
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
    into Python memory. The injected runner branch (unit tests) is for
    small payloads only.
    """
    if not archive.is_file():
        return False, f"archive not found: {archive}".encode("utf-8")

    if run_subprocess is None:
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

    try:
        contents = archive.read_bytes()
    except OSError as exc:
        return False, str(exc).encode("utf-8")
    # The injected runner doesn't carry stdin natively; tests stub the
    # whole import call by argv-sniffing so we just hand them the
    # cmdline and they decide. Production never hits this branch.
    _stdout, stderr, rc = run_subprocess([
        "nix-store", "--import", f"<<{len(contents)}bytes>>",
    ])
    return rc == 0, stderr


# ---------------------------------------------------------------------------
# Per-binary kept-drv discovery
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

    Legacy ``phase0_eval__<binary>.json`` fallback was removed per A6's
    hard wire-protocol cutover.
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
    lookup is empty — the planner just won't have per-variant payload
    fields (compiler_id, flag_set, etc.) for that arch/label and will
    skip those variants.
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

    ``manifest_dir`` may be None when the worker runs without access to
    the framework's per-task header directory (eg unit tests); only the
    sidecar mode applies in that case.
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


# ---------------------------------------------------------------------------
# sum-drv + tree walk
# ---------------------------------------------------------------------------


def build_sum_drv(
    *,
    bash_path: str,
    toolchain_drvs: list[str],
    binary: str,
    variant_drvs: list[str],
    system: str,
) -> str:
    """Wrap :func:`template_graph.make_sum_drv.make_sum_drv_from_paths`.

    Builds a sum-root drv whose ``inputDrvs`` carries the toolchains
    wrapper + one matrix wrapper for ``binary``. The matrix wrapper's
    name is ``matrix-<binary>`` per the streaming planner's
    ``_MATRIX_RE`` convention.
    """
    # Late import: template_graph isn't a stdlib dep and a missing
    # checkout shouldn't crash module-load (unit tests may not have
    # it on PYTHONPATH).
    from template_graph.make_sum_drv import make_sum_drv_from_paths  # noqa: PLC0415

    return make_sum_drv_from_paths(
        bash_path=bash_path,
        toolchain_drvs=toolchain_drvs,
        matrix_drvs={f"matrix-{binary}": variant_drvs},
        system=system,
    )


def query_drv_tree(
    sum_drv: str,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> str:
    """``nix-store --query --tree <sum_drv>`` → decoded UTF-8 text.

    Raises :class:`RuntimeError` on non-zero exit. The output is the
    indented tree the streaming planner consumes line-by-line.
    """
    runner = run_subprocess or _default_run_subprocess
    stdout, stderr, rc = runner([
        "nix-store", "--query", "--tree", sum_drv,
    ])
    if rc != 0:
        raise RuntimeError(
            f"nix-store --query --tree {sum_drv} failed (rc={rc}): "
            + stderr.decode("utf-8", errors="replace").strip()
        )
    return stdout.decode("utf-8", errors="replace")


def plan_binary(
    *,
    binary: str,
    tree_text: str,
    variant_lookup: dict[tuple[str, str], dict],
    toolchain_task_ids: dict[str, str],
    sys_name: str,
) -> list[Any]:
    """Run the streaming planner + dependency_graph_planner adapter
    against ``tree_text`` for one binary.

    Returns the list of :class:`Phase4Descriptor` records. Raises
    :class:`DependencyGraphCycleError` (from the adapter) on cycle
    detection; the caller logs + propagates.
    """
    from template_graph.streaming import plan_from_tree_streaming  # noqa: PLC0415
    from compiler_suit_runner.dependency_graph_planner import (  # noqa: PLC0415
        BinaryPlanInput,
        plan_phase4_from_graph,
    )

    streaming_result = plan_from_tree_streaming(tree_text)
    inp = BinaryPlanInput(
        binary=binary,
        streaming_result=streaming_result,
        variant_lookup=variant_lookup,
        toolchain_task_ids=toolchain_task_ids,
    )
    return plan_phase4_from_graph([inp], sys_name=sys_name)


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


def write_dependency_graph_json(
    out_path: pathlib.Path, descriptors: list[Any],
) -> pathlib.Path:
    """Atomically write the Phase 4 descriptor list to ``out_path``.

    Each descriptor is :func:`dataclasses.asdict`-converted. Tuples
    become lists in JSON (depends_on); the consumer
    (dependency_graph_planner spawn step) re-tuples on read.
    """
    serialised: list[dict] = []
    for d in descriptors:
        if dataclasses.is_dataclass(d):
            entry = dataclasses.asdict(d)
        elif isinstance(d, dict):
            entry = dict(d)
        else:
            entry = {"opaque": repr(d)}
        # Tuples in dataclasses.asdict become tuples — JSON wants lists.
        if "depends_on" in entry and isinstance(entry["depends_on"], tuple):
            entry["depends_on"] = list(entry["depends_on"])
        serialised.append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    encoded = json.dumps(
        {"phase4_descriptors": serialised}, indent=2, sort_keys=True,
    ).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Public top-level entry point
# ---------------------------------------------------------------------------


def run_dependency_graph_task(
    *,
    matrix_eval_out_dir: pathlib.Path,
    manifest_dir: Optional[pathlib.Path],
    bash_path: str,
    toolchain_drvs: list[str],
    toolchain_task_ids: Optional[dict[str, str]] = None,
    sys_name: str = "x86_64-linux",
    run_subprocess: Optional[RunSubprocess] = None,
    clock: Optional[Callable[[], float]] = None,
    skip_import_when_present: bool = True,
) -> DependencyGraphResult:
    """Walk every ``<binary>.nix-archive`` under ``matrix_eval_out_dir``
    and produce ``_dependency_graph.json``.

    Per-binary failures raise :class:`DependencyGraphWorkerError`; the
    caller's main loop translates that into a non-zero exit code.
    """
    clock_fn = clock or time.monotonic
    start = clock_fn()

    archives = discover_archives(matrix_eval_out_dir)
    if not archives:
        # Empty matrix_eval output — write a trivial dependency_graph.json
        # so the watcher knows the planner ran (vs no-op).
        out_path = write_dependency_graph_json(
            matrix_eval_out_dir / DEPENDENCY_GRAPH_JSON, []
        )
        return DependencyGraphResult(
            output_path=out_path,
            binary_count=0,
            descriptor_count=0,
            duration_seconds=max(0.0, clock_fn() - start),
        )

    runner = run_subprocess or _default_run_subprocess
    tc_ids: dict[str, str] = dict(toolchain_task_ids or {})
    all_descriptors: list[Any] = []
    binary_count = 0

    for archive in archives:
        binary = archive.stem
        kept_drvs, variant_lookup = discover_kept_drvs(archive, manifest_dir)
        if not kept_drvs:
            # Logged inside discover_kept_drvs; treat as skip.
            continue

        need_import = True
        if skip_import_when_present:
            need_import = not all(
                is_path_locally_present(p, run_subprocess=runner)
                for p in kept_drvs
            )

        if need_import:
            ok, err = import_archive(archive, run_subprocess=runner)
            if not ok:
                raise DependencyGraphWorkerError(
                    binary=binary, stage="import",
                    message=(
                        "nix-store --import failed: "
                        + err.decode("utf-8", errors="replace").strip()
                    ),
                )

        try:
            sum_drv = build_sum_drv(
                bash_path=bash_path,
                toolchain_drvs=toolchain_drvs,
                binary=binary,
                variant_drvs=kept_drvs,
                system=sys_name,
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyGraphWorkerError(
                binary=binary, stage="sum_drv",
                message=f"sum-drv assembly failed: {exc}",
                cause=exc,
            ) from exc

        try:
            tree_text = query_drv_tree(sum_drv, run_subprocess=runner)
        except RuntimeError as exc:
            raise DependencyGraphWorkerError(
                binary=binary, stage="query_tree",
                message=str(exc), cause=exc,
            ) from exc

        try:
            descriptors = plan_binary(
                binary=binary,
                tree_text=tree_text,
                variant_lookup=variant_lookup,
                toolchain_task_ids=tc_ids,
                sys_name=sys_name,
            )
        except Exception as exc:  # noqa: BLE001
            raise DependencyGraphWorkerError(
                binary=binary, stage="plan",
                message=f"plan_phase4 failed: {exc}",
                cause=exc,
            ) from exc

        all_descriptors.extend(descriptors)
        binary_count += 1

    out_path = write_dependency_graph_json(
        matrix_eval_out_dir / DEPENDENCY_GRAPH_JSON, all_descriptors
    )
    return DependencyGraphResult(
        output_path=out_path,
        binary_count=binary_count,
        descriptor_count=len(all_descriptors),
        duration_seconds=max(0.0, clock_fn() - start),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_cli_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser; factored so tests can introspect."""
    parser = argparse.ArgumentParser(
        prog="compiler_suit_runner.workers.dependency_graph_worker",
        description=(
            "Primary-side dependency_graph worker. Imports per-binary"
            " matrix_eval archives, runs the template_graph streaming"
            " planner, writes _dependency_graph.json."
        ),
    )
    parser.add_argument(
        "--matrix-eval-out-dir",
        type=str,
        required=True,
        help=(
            "Directory containing the per-binary <binary>.nix-archive"
            " files (typically <shared_fs>/out/_matrix_eval/)."
        ),
    )
    parser.add_argument(
        "--manifest-dir",
        type=str,
        default=None,
        help=(
            "Framework manifests directory; consulted for"
            " matrix_eval__<binary>.json headers when no sidecar JSON"
            " accompanies the archive."
        ),
    )
    parser.add_argument(
        "--flake-ref",
        type=str,
        default=".",
        help="Flake reference (currently informational; sum-drv uses paths).",
    )
    parser.add_argument(
        "--bash-path",
        type=str,
        required=True,
        help=(
            "Realised bash store path (e.g. /nix/store/...-bash-5.2-p15);"
            " passed to make_sum_drv_from_paths."
        ),
    )
    parser.add_argument(
        "--toolchain-drv",
        action="append",
        default=[],
        help=(
            "Toolchain .drv path. Repeatable. Required: every active"
            " toolchain whose closure the matrix touches must be listed."
        ),
    )
    parser.add_argument(
        "--toolchain-task-id",
        action="append",
        default=[],
        help=(
            "Toolchain task id mapping in the form"
            " '<hash>-<name>=<task_id>'. Repeatable. Optional but"
            " recommended — wires phase-1 build_compilers ids into"
            " phase-4 variant depends_on."
        ),
    )
    parser.add_argument(
        "--sys-name",
        type=str,
        default="x86_64-linux",
        help="Target system attr (default x86_64-linux).",
    )
    parser.add_argument(
        "--no-skip-import-when-present",
        action="store_true",
        help=(
            "Always run nix-store --import even when all kept drvs are"
            " already locally present (default: skip)."
        ),
    )
    return parser


def _parse_task_id_mappings(raw: list[str]) -> dict[str, str]:
    """Turn ``["abc-foo.drv=task_id_1", ...]`` into ``{ident: task_id}``."""
    out: dict[str, str] = {}
    for entry in raw:
        if "=" not in entry:
            logger.warning(
                "ignoring malformed --toolchain-task-id %r (missing '=')",
                entry,
            )
            continue
        ident, _, task_id = entry.partition("=")
        ident = ident.strip()
        task_id = task_id.strip()
        if not ident or not task_id:
            continue
        out[ident] = task_id
    return out


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    Exits 0 on success, nonzero on the first binary's planning failure.
    Configures stdlib logging (INFO) to stderr; production callers may
    redirect via standard shell facilities.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    matrix_eval_out_dir = pathlib.Path(args.matrix_eval_out_dir)
    manifest_dir = (
        pathlib.Path(args.manifest_dir) if args.manifest_dir else None
    )
    toolchain_task_ids = _parse_task_id_mappings(args.toolchain_task_id)

    try:
        result = run_dependency_graph_task(
            matrix_eval_out_dir=matrix_eval_out_dir,
            manifest_dir=manifest_dir,
            bash_path=args.bash_path,
            toolchain_drvs=list(args.toolchain_drv),
            toolchain_task_ids=toolchain_task_ids,
            sys_name=args.sys_name,
            skip_import_when_present=not args.no_skip_import_when_present,
        )
    except DependencyGraphWorkerError as exc:
        logger.error(
            "dependency_graph_worker failed: %s", exc,
        )
        return 2
    except Exception:  # noqa: BLE001 - any uncaught is fatal here
        logger.exception(
            "dependency_graph_worker crashed unexpectedly"
        )
        return 1

    logger.info(
        "dependency_graph_worker ok: wrote %s (%d binaries, %d descriptors)"
        " in %.2fs",
        result.output_path, result.binary_count,
        result.descriptor_count, result.duration_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

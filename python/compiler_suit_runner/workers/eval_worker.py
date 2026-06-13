"""Matrix-eval distributed-eval worker — one task per binary.

Runs on a cluster secondary. Given a manifest payload built by
:func:`compiler_suit_runner.manifest_gen.make_matrix_eval_header`, the
worker:

1. Resolves the per-(arch, suffix) drv set by invoking a SINGLE
   ``nix-eval-jobs --flake <attr>`` against the local /nix/store
   that intersects every requested arch AND every sampled suffix
   in one pass (``builtins.intersectAttrs`` at both the arch and
   suffix levels, plus ``--force-recurse`` so the walker descends
   through both nested levels). The worker applies the deterministic
   :func:`sample_suffix_attrs` sampling (keyed on the payload's
   ``variant_seed``) BEFORE the bulk eval so primary and secondaries
   agree on the variant subset without the submitter ever shipping
   the list.
2. Builds the per-binary ``matrix-<binary>`` aggregate drv via
   :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths`,
   passing the phase-1 toolchain aggregate AND every sampled leaf
   as inputDrvs. The aggregate is a trivial ``bash -c true`` wrapper
   whose only purpose is to carry the input set so phase 3's
   ``nix-store --query --tree`` refcount-sort floats the toolchain
   layer to a sensible position relative to the matrix layer.
3. Exports the aggregate closure to
   ``<matrix_eval_out_dir>/matrix-<binary>.drv.archive`` via
   ``nix-store --query --requisites`` + ``nix-store --export`` so the
   ``dependency_graph_worker`` (phase 3) AND every secondary's
   ``build_worker`` (phase 4) can
   re-import the full drv graph (toolchain aggregate + matrix
   aggregate + every leaf) into their local store without re-
   evaluating the flake. The archive filename mirrors the
   ``matrix-<binary>.drv`` storename so the on-disk artefact is self-
   identifying — readers recognise it without out-of-band metadata.
   The ``dependency_graph_worker`` discovers the ROOT (kept) drvs by
   parsing the variant path of each imported .drv directly
   (``parse_variant_path``) — no JSON sidecar is emitted.

The archive on shared-fs is the ONLY route by which other phases get
the matrix-eval drv graph: the previous per-drv broadcast loop
(``peer_replication`` → ``/peer/path-broadcast-offer``) was removed
because phase 4 (build_worker) now imports the per-binary archive
once and the broadcasts were duplicating work the archive already
covered (~30-60 min per binary saved on the LMU smoke matrix).

Re-execution
------------

The worker always re-runs every step on every invocation; there is no
resume fast-path. In-run "second attempts" are failure restarts
where any cached on-disk archive cannot be trusted (the writer never
fully completed). The per-binary archive is delivered via the
framework publish API (stage under ``DYNRUNNER_PUBLISH_SRC_ROOT`` then
``Task.publish(dst=...)``), whose concurrency-safe atomic cross-FS
rename guarantees readers never see a half-written file and concurrent
publishers of the same shared destination never collide — a fresh run
simply re-publishes over the prior archive in place.

Error-type contract (framework integration)
-------------------------------------------

The dynamic_runner framework distinguishes two failure types:

* ``ErrorType::Errored`` — transient / recoverable. The task is
  retry-eligible against the **retry-pass budget**. Use this for
  nix-eval-jobs subprocess failures, archive-write failures, and
  similar.

* ``ErrorType::Unfulfillable`` — permanent / structurally impossible
  on this peer. The task is NOT retried; it transitions back to
  Pending so another peer can pick it up. Use this **only** for
  signals that this peer cannot ever satisfy the task (e.g. missing
  toolchain output that should have been pre-installed via Phase
  -1 — explicitly NOT raised by this module today; that wiring
  belongs in the framework dispatch layer once Phase -1 is live).

This worker raises ``RuntimeError`` on any transient failure. The
framework's worker harness wraps that into an ``Errored`` task
state, so eval failures correctly count against the retry-pass
budget instead of the dispatch (Unfulfillable→Pending) budget.

Why one task per binary (not one per arch)
------------------------------------------

The plan deliberately keeps the partition coarse: each binary's
eval walks every requested arch in a single bulk ``nix-eval-jobs``
invocation, all inside one task. Two reasons:

* The cross-arch toolchain closures share enormous overlap; a
  single ``nix-eval-jobs`` process can reuse the in-memory eval
  cache across archs. The matrix-aggregate refactor collapsed the
  earlier per-arch loop into one bulk eval per binary so that
  sharing is realised.
* Phase 1 task spawn waits for the per-binary archive file; a
  binary's archive is single-writer so we never race on it.

See ``~/.claude/plans/lively-beaming-summit.md`` Part B for the
full rationale and the Phase 1 planner that consumes the marker.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import random
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any, Optional

from dynamic_runner.worker import Task

from compiler_suit_runner.workers.dependency_graph_worker.subproc import (
    resolve_tool,
)


# Module logger. The worker subprocess routes stdlib logging to a per-
# worker file (build_worker.main configures the root handler), so INFO
# step logging here surfaces in ``worker_N.log`` — making an eval's
# progress (and any failure point) readable rather than a ~6-min silence
# between "dispatching matrix_eval" and the result. Naming mirrors the
# build_worker convention (``compiler_suit_runner.build_worker.handle``).
_LOG = logging.getLogger("compiler_suit_runner.eval_worker")


# ``run_subprocess`` accepts argv (list[str]) plus an optional
# ``input`` kwarg of bytes piped to stdin, and returns a tuple of
# (stdout_bytes, stderr_bytes, returncode). Mirrors the
# ``RunSubprocess`` callable shape in ``preflight.py`` (its
# ``_default_run_subprocess`` likewise accepts ``input=None``) so the
# same fakes can be reused. ``Callable[..., ...]`` keeps the alias
# permissive for callers that don't pass ``input=``.
RunSubprocess = Callable[..., tuple[bytes, bytes, int]]


MATRIX_EVAL_ITEM_CLASS = "matrix_eval"
"""Item class string this worker handles (matches manifest_gen)."""


_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _default_run_subprocess(
    argv: list[str],
    *,
    input: Optional[bytes] = None,
) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell.

    ``input`` is forwarded to ``subprocess.run(input=...)`` so callers
    can stream large filter payloads / store-path lists via stdin
    instead of stuffing them into argv (which trips MAX_ARG_STRLEN at
    full LMU scale).

    ``argv[0]`` is resolved via :func:`resolve_tool` so a bare tool
    name still execs when the respawn environment lost PATH.
    """
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        [resolve_tool(argv[0]), *argv[1:]],
        check=False,
        capture_output=True,
        shell=False,
        input=input,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Deterministic variant sampling
# ---------------------------------------------------------------------------


def sample_suffix_attrs(
    suffix_attrs: dict,
    *,
    arch: str,
    sample_size: int,
    seed: str,
) -> dict:
    """Deterministically down-sample ``{suffix: meta_entry}``.

    Picks at most ``sample_size`` entries per ``(compiler,
    optimization)`` group, seeded on
    ``f"{seed}:{compiler}:{arch}:{opt}"`` so the same operator seed
    produces the same sample on every peer.

    Suffixes with non-dict meta or missing compiler/optimization
    metadata are passed through unchanged (we cannot group them).

    Every peer that needs the sampled subset (eval worker, phase-3
    dot helper, smoke harness) imports this same function so all
    sites agree on the variant subset without the submitter ever
    shipping the list.
    """
    if sample_size <= 0:
        return suffix_attrs
    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    passthrough: dict[str, dict] = {}
    for suffix, meta_entry in suffix_attrs.items():
        if not isinstance(meta_entry, dict):
            passthrough[suffix] = meta_entry
            continue
        compiler = meta_entry.get("compiler")
        opt = meta_entry.get("optimization")
        if not isinstance(compiler, str) or not isinstance(opt, str):
            passthrough[suffix] = meta_entry
            continue
        groups.setdefault((compiler, opt), []).append((suffix, meta_entry))

    sampled: dict[str, dict] = dict(passthrough)
    for (compiler, opt), candidates in groups.items():
        candidates.sort(key=lambda kv: kv[0])
        rng = random.Random(f"{seed}:{compiler}:{arch}:{opt}")
        chosen = rng.sample(candidates, min(sample_size, len(candidates)))
        for suffix, meta_entry in chosen:
            sampled[suffix] = meta_entry
    return sampled


# ---------------------------------------------------------------------------
# Manifest payload parsing
# ---------------------------------------------------------------------------


def parse_payload(payload: dict) -> dict[str, Any]:
    """Validate a matrix_eval payload (as produced by
    :func:`manifest_gen.make_matrix_eval_header`) and return a
    normalised dict.

    Raises :class:`ValueError` on shape errors so callers (and tests)
    can distinguish bad-input from transient-eval failures.
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"matrix_eval payload must be a dict, got {type(payload).__name__}"
        )
    binary = payload.get("binary")
    sys_name = payload.get("sys")
    archs = payload.get("archs")
    attr = payload.get("attr")
    if not isinstance(binary, str) or not binary:
        raise ValueError(f"matrix_eval payload: invalid 'binary' ({binary!r})")
    if not isinstance(sys_name, str) or not sys_name:
        raise ValueError(f"matrix_eval payload: invalid 'sys' ({sys_name!r})")
    if not isinstance(archs, list) or not all(isinstance(a, str) for a in archs):
        raise ValueError(f"matrix_eval payload: invalid 'archs' ({archs!r})")
    suffixes_raw = payload.get("suffixes")
    if suffixes_raw is None:
        suffixes = []
    elif isinstance(suffixes_raw, list) and all(
        isinstance(s, str) for s in suffixes_raw
    ):
        for s in suffixes_raw:
            if not _SAFE_SUFFIX_RE.match(s):
                raise ValueError(
                    f"matrix_eval payload: unsafe suffix {s!r} — refusing to splice"
                )
        suffixes = list(suffixes_raw)
    else:
        raise ValueError(
            f"matrix_eval payload: invalid 'suffixes' ({suffixes_raw!r})"
        )
    if not isinstance(attr, str) or not attr:
        raise ValueError(f"matrix_eval payload: invalid 'attr' ({attr!r})")

    variant_sample = payload.get("variant_sample")
    if variant_sample is not None and not isinstance(variant_sample, int):
        raise ValueError(
            f"matrix_eval payload: invalid 'variant_sample' ({variant_sample!r})"
        )
    variant_seed = payload.get("variant_seed")
    if variant_seed is not None and not isinstance(variant_seed, str):
        raise ValueError(
            f"matrix_eval payload: invalid 'variant_seed' ({variant_seed!r})"
        )

    # Phase B wired the phase-1 toolchain aggregate drv path into every
    # matrix_eval header. eval_worker now uses it as an inputDrv when
    # building the per-binary ``matrix-<binary>`` aggregate so phase 3's
    # ``nix-store --query --tree`` refcount-sorts the toolchain layer
    # next to the matrix layer rather than burying it under one leaf.
    toolchain_aggregate_drv = payload.get("toolchain_aggregate_drv")
    if (
        not isinstance(toolchain_aggregate_drv, str)
        or not toolchain_aggregate_drv
    ):
        raise ValueError(
            "matrix_eval payload: invalid 'toolchain_aggregate_drv'"
            f" ({toolchain_aggregate_drv!r})"
        )

    # Toolchain-dedup feature flag (default ON when absent so legacy
    # headers behave as dedup). When True the export subtracts the
    # toolchain closure from the per-binary archive; when False the
    # full closure is exported (today's behaviour / rollback).
    toolchain_dedup_raw = payload.get("toolchain_dedup", True)
    if not isinstance(toolchain_dedup_raw, bool):
        raise ValueError(
            "matrix_eval payload: invalid 'toolchain_dedup'"
            f" ({toolchain_dedup_raw!r})"
        )

    return {
        "binary": binary,
        "sys": sys_name,
        "archs": list(archs),
        "suffixes": list(suffixes),
        "attr": attr,
        "variant_sample": variant_sample,
        "variant_seed": variant_seed,
        "toolchain_aggregate_drv": toolchain_aggregate_drv,
        "toolchain_dedup": toolchain_dedup_raw,
    }


# ---------------------------------------------------------------------------
# nix-eval-jobs invocation
# ---------------------------------------------------------------------------


def _eval_jobs_for_binary(
    attr: str,
    sampled_by_arch: dict[str, list[str]],
    *,
    flake_ref: str,
    run_subprocess: RunSubprocess,
) -> dict[tuple[str, str], str]:
    """Run ONE ``nix-eval-jobs`` invocation against ``<flake_ref>#<attr>``
    that intersects every requested ``(arch, suffix)`` pair in a single
    pass and returns ``{(arch, suffix): drvPath}``.

    ``sampled_by_arch`` is the per-arch sampled-suffix map: archs with
    an empty suffix list are dropped from the select expression entirely
    (a missing key matches no cells). The filter is materialised to a
    JSON tmpfile under :func:`tempfile.gettempdir` and the ``--select``
    lambda reads it via ``builtins.fromJSON (builtins.readFile
    "<absolute path>")`` so argv stays bounded regardless of variant
    count. ``--impure`` is passed because absolute-path reads are
    forbidden in pure-eval mode (flake context defaults to pure); the
    rest of the dataset eval is unaffected by impure mode.

    The earlier ``arg-from-stdin`` shape was reverted because
    nix-eval-jobs binds ``--arg`` names at the top-level expression
    scope, NOT inside the ``--select`` lambda — referencing the
    arg-name from inside the lambda raises ``undefined variable``.
    The tmpfile lives only for the duration of the subprocess and is
    unlinked in a ``finally`` block so we never leak.

    The ``--select`` lambda walks the nested ``{ arch: { suffix: drv } }``
    layout and filters BOTH levels in one go:

    * outer ``intersectAttrs`` against the arch keys we want;
    * inner ``mapAttrs`` per surviving arch that intersectAttrs against
      that arch's sampled suffixes (i.e. each arch can keep a different
      sample, which the deterministic group-aware sampler routinely
      produces).

    ``--force-recurse`` makes nix-eval-jobs descend through the two
    nested levels and emit one JSONL line per surviving leaf with
    ``attrPath = [arch, suffix]`` plus ``drvPath``.

    The collapse to a single subprocess invocation is the core of the
    matrix-aggregate refactor: previously the worker ran one
    ``nix-eval-jobs`` per arch, each paying the flake-closure
    re-evaluation cost; now the entire ``dataset.<sys>.<binary>``
    subtree is walked once and the eval cache is shared across archs
    inside that single process.

    On any non-zero exit code raises :class:`RuntimeError`; the
    framework worker harness converts that into
    ``ErrorType::Errored`` (retry-eligible). Per-leaf eval errors are
    surfaced via JSONL ``{"error": ...}`` lines from nix-eval-jobs and
    silently skipped here — they would already have been gated out by
    support-table / known-bad filtering on the submitter side.
    """
    # Drop archs with no sampled suffixes — nothing to ask for.
    effective: dict[str, list[str]] = {
        a: sorted(set(s)) for a, s in sampled_by_arch.items() if s
    }
    if not effective:
        return {}

    # Validate every (arch, suffix) before splicing — the originator
    # already validated, but a worker should never trust a manifest
    # payload blindly when it's about to construct a nix expression.
    for arch, suffixes in effective.items():
        if not _SAFE_SUFFIX_RE.match(arch):
            raise RuntimeError(
                f"eval_worker: unsafe arch {arch!r} in payload — refusing"
                " to splice into nix-eval-jobs --select"
            )
        for s in suffixes:
            if not _SAFE_SUFFIX_RE.match(s):
                raise RuntimeError(
                    f"eval_worker: unsafe suffix {s!r} in payload — refusing"
                    " to splice into nix-eval-jobs --select"
                )

    # Materialise the per-arch suffix filter to a JSON tmpfile so argv
    # stays well under MAX_ARG_STRLEN even at full-LMU variant counts
    # (where the inline-attrset form pushed argv past 128 KB and exec
    # failed with ``Argument list too long``). The path is embedded
    # inline in the --select expression as a Nix string literal; the
    # lambda reads it via builtins.readFile. ``--impure`` is required
    # because absolute-path reads are restricted in pure-eval mode
    # (which flake context enters by default).
    payload_bytes = json.dumps(effective, sort_keys=True).encode("utf-8")
    tmp = tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".json",
        prefix="csr-eval-filter-",
        delete=False,
    )
    try:
        tmp.write(payload_bytes)
        tmp.flush()
    finally:
        tmp.close()
    filter_path = tmp.name

    # The select lambda: read+decode the JSON filter map, then filter
    # outer archs first and per surviving arch intersectAttrs against
    # THAT arch's sampled suffixes. The ``filter.${a}`` lookup is safe
    # because the outer intersectAttrs guarantees ``a`` is one of our
    # keys. Quoting the path with a JSON-encoded string literal escapes
    # any backslash / double-quote (NamedTemporaryFile names normally
    # don't contain those, but defence in depth is cheap here).
    select_expr = f"""\
m: let
  archMap = builtins.fromJSON (builtins.readFile {json.dumps(filter_path)});
  toSet  = lst: builtins.listToAttrs
    (builtins.map (s: {{ name = s; value = null; }}) lst);
  filter = builtins.mapAttrs (_: toSet) archMap;
in
  builtins.mapAttrs
    (a: v: builtins.intersectAttrs filter.${{a}} v)
    (builtins.intersectAttrs filter m)\
"""

    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{flake_ref}#{attr}",
        "--impure",
        "--select",
        select_expr,
        "--force-recurse",
        "--max-jobs",
        "1",
    ]
    try:
        stdout, stderr, rc = run_subprocess(argv)
    finally:
        try:
            os.unlink(filter_path)
        except OSError:
            # Best-effort cleanup; a leftover tmpfile in /tmp is not
            # worth raising over (and would mask the real eval error).
            pass
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        # RuntimeError → framework harness maps to ErrorType::Errored
        # (retry-pass eligible) — NOT Unfulfillable, since
        # nix-eval-jobs failures are typically transient (eval-time
        # OOM, transient substituter network failure, etc).
        raise RuntimeError(
            f"nix-eval-jobs {attr} failed (rc={rc}): {decoded_err}"
        )

    drvs: dict[tuple[str, str], str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # nix-eval-jobs interleaves status lines; ignore anything
            # that isn't a valid JSON object.
            continue
        if not isinstance(entry, dict):
            continue
        attr_path = entry.get("attrPath")
        drv = entry.get("drvPath")
        if (
            isinstance(attr_path, list)
            and len(attr_path) == 2
            and isinstance(attr_path[0], str)
            and isinstance(attr_path[1], str)
            and isinstance(drv, str)
            and drv
        ):
            drvs[(attr_path[0], attr_path[1])] = drv
    return drvs


def _eval_meta_for_arch(
    sys_name: str,
    binary: str,
    arch: str,
    *,
    flake_ref: str,
    run_subprocess: RunSubprocess,
) -> dict[str, Any]:
    """Read ``_meta.<sys>.<binary>.<arch>`` so we can re-apply the
    same deterministic sampling the submitter applied.

    Returns the parsed JSON object (a ``{suffix: meta_entry}`` map).
    On non-dict response or eval failure, raises :class:`RuntimeError`
    (transient — retry-pass eligible).
    """
    argv: list[str] = [
        "nix",
        "eval",
        "--extra-experimental-features",
        "nix-command flakes",
        # Multiple workers inside one secondary container share
        # /root/.cache/nix and would otherwise race on the eval-cache
        # SQLite write lock; disabling the cache trades a little CPU
        # for forward progress.
        "--no-eval-cache",
        "--json",
        f"{flake_ref}#_meta.{sys_name}.{binary}.{arch}",
    ]
    stdout, stderr, rc = run_subprocess(argv)
    if rc != 0:
        decoded_err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} failed "
            f"(rc={rc}): {decoded_err}"
        )
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} produced invalid JSON:"
            f" {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"nix eval _meta.{sys_name}.{binary}.{arch} returned non-object"
        )
    return parsed


# ---------------------------------------------------------------------------
# Archive (matrix-aggregate drv export — quiesce signal for the watcher)
# ---------------------------------------------------------------------------


TOOLCHAIN_ARCHIVE_NAME = "toolchains.drv.archive"
"""Shared toolchain-archive filename (mirrors preflight + dep-graph)."""


def _archive_path(out_dir: pathlib.Path, binary: str) -> pathlib.Path:
    """Per-binary archive path under the matrix-eval output dir.

    ``out_dir`` is the matrix-eval-specific dir (e.g. ``_matrix_eval``
    on the host, ``/app/out-network/_matrix_eval`` in the container);
    each binary's kept-variant closure lands at
    ``<out_dir>/matrix-<binary>.drv.archive``. The filename mirrors the
    ``matrix-<binary>.drv`` storename pattern so the archive is self-
    identifying — readers recognise it as the matrix-aggregate drv
    export without out-of-band metadata.
    """
    return out_dir / f"matrix-{binary}.drv.archive"


def _import_toolchain_archive(
    out_dir: pathlib.Path,
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> None:
    """Import the shared ``toolchains.drv.archive`` into the worker store.

    Toolchain dedup makes the eval worker a CONSUMER of the shared
    toolchain archive: the submitter produces + uploads ONE
    ``toolchains.drv.archive`` (under ``out_dir`` =
    ``matrix_eval_out_dir`` = the shared gateway mount) at setup, and
    the per-binary diff export subtracts its closure. The toolchain
    closure must therefore be RESIDENT in this worker's store before
    the subtract recompute (``exclude_seed`` requisites) — so we import
    it first.

    Delegates to
    :func:`workers.build_worker.ensure_toolchain_archive_imported` (the
    same process-memoised, dep_graph-``import_archive``-backed helper
    the build worker uses). Imported lazily so eval_worker pays the
    cross-worker import cost only on the dedup path; the helper's own
    dep_graph import is likewise lazy, so there is no import cycle.

    Hard-fails (:class:`RuntimeError`) when the archive is MISSING or
    ZERO-BYTE on the dedup path: a missing toolchain archive means the
    submitter upload did not happen, so the per-binary diff subtract
    would be computed against an absent closure and the resulting
    archive would be un-importable downstream. (``ensure_toolchain_
    archive_imported`` itself treats absent/empty as a no-op — the
    operator-older-submit tolerance the build worker wants — so the
    strict presence check lives HERE, gated on dedup by the caller,
    before delegating.)
    """
    archive = out_dir / TOOLCHAIN_ARCHIVE_NAME
    try:
        size = archive.stat().st_size
    except OSError:
        size = -1
    if size <= 0:
        raise RuntimeError(
            "toolchain dedup is ON but the shared toolchain archive is "
            f"missing or zero-byte at {archive!s} (size={size}); the "
            "submitter setup must produce + upload it before dispatch — "
            "the per-binary diff subtract would otherwise be computed "
            "against an absent toolchain closure"
        )
    # Lazy: the cross-worker import is only needed on the dedup path,
    # and build_worker's own dep_graph import is lazy too (no cycle).
    from compiler_suit_runner.workers.build_worker import (  # noqa: PLC0415
        ensure_toolchain_archive_imported,
    )

    ensure_toolchain_archive_imported(out_dir, run_subprocess=run_subprocess)
    _LOG.info(
        "toolchain archive: imported %s (%d bytes) before dedup subtract",
        archive.name, size,
    )


def _publish_src_root() -> pathlib.Path:
    """Worker-local STAGING root for files headed to the shared mount.

    The framework's publish API moves a file from a per-worker staging
    tree (``DYNRUNNER_PUBLISH_SRC_ROOT``, default ``/app/out-tmp``)
    onto the shared destination mount (``/app/out-network``) with a
    concurrency-safe PID+nanos-unique sibling tmp → fsync → atomic
    rename → fsync-parent. Workers stage under this root, then call
    :meth:`Task.publish` (mirrors build_worker's ``copy_elf_folder``
    → ``task.publish_all`` pattern).
    """
    return pathlib.Path(
        os.environ.get("DYNRUNNER_PUBLISH_SRC_ROOT", "/app/out-tmp")
    )


def _staged_archive_path(archive: pathlib.Path) -> pathlib.Path:
    """Worker-local staging path for the per-binary ``archive``.

    Mirrors the archive's basename under ``<src_root>/_matrix_eval/``
    so the staged file is unambiguous + co-located by binary. The
    publish step delivers it to the explicit ``dst=archive`` on the
    shared mount; the staging path itself is never read by consumers.
    """
    return _publish_src_root() / "_matrix_eval" / archive.name


def _publish_archive(
    task: Task,
    staged: pathlib.Path,
    archive: pathlib.Path,
) -> None:
    """Deliver ``staged`` to ``archive`` via the framework publish API.

    ``task.publish(staged, dst=archive)`` performs the concurrency-safe
    cross-FS delivery — concurrent publishers of the same ``dst`` never
    collide (the native ``publish_one`` renames a PID+nanos-unique
    sibling tmp). Replaces the prior hand-rolled fixed-``.tmp`` +
    ``os.replace`` straight onto the shared mount, which raced when
    multiple workers published the same shared archive. Wraps
    :class:`PublishError` into :class:`RuntimeError` so the failure is
    retry-pass eligible per the module's error contract.
    """
    from dynamic_runner.worker import PublishError  # noqa: PLC0415

    try:
        task.publish(staged, dst=archive)
    except PublishError as exc:
        raise RuntimeError(
            f"publishing {staged!s} -> {archive!s} failed: {exc}"
        ) from exc


def _export_kept_closure(
    archive: pathlib.Path,
    kept_drvs: list[str],
    *,
    task: Task,
    run_subprocess: RunSubprocess,
    exclude_seed: Optional[str] = None,
) -> None:
    """Export the closure of ``kept_drvs`` into ``archive``.

    Two (or three, with ``exclude_seed``) subprocess invocations, each
    fed its store-path list as newline-delimited bytes on stdin via
    ``--stdin`` so argv stays bounded at production scale:

      1. ``nix-store --query --requisites --stdin`` to enumerate
         every store path in the transitive closure of ``kept_drvs``.
      2. (toolchain dedup only) ``nix-store --query --requisites
         --stdin`` over ``exclude_seed`` (the toolchain aggregate drv),
         set-subtracted from the matrix closure so the exported archive
         carries only ``requisites(matrix) − requisites(toolchain)`` —
         the toolchain ships once via the pre-flight
         ``toolchains.drv.archive``. The toolchain closure is already
         local on the worker (it is inputDrv #0 of the matrix aggregate),
         so this is recomputed here rather than shipping a manifest.
      3. ``nix-store --export --stdin`` whose stdout is the
         self-contained archive byte stream we stage to a worker-local
         file then publish.

    When ``exclude_seed`` is ``None`` (toolchain dedup OFF / rollback)
    the full matrix closure is exported, i.e. today's behaviour.

    The export bytes are written to a worker-local STAGING path (under
    ``DYNRUNNER_PUBLISH_SRC_ROOT``) and then delivered to ``archive`` on
    the shared mount via :meth:`Task.publish` — the framework owns the
    concurrency-safe atomic cross-FS rename, so concurrent publishers of
    the same shared ``archive`` never collide (the prior hand-rolled
    fixed-``.tmp`` + ``os.replace`` raced and ENOENT'd). Mirrors
    build_worker's stage→publish pattern; the injected ``run_subprocess``
    seam mirrors the rest of this module. Raises :class:`RuntimeError`
    on any subprocess or publish failure (retry-pass eligible).
    """
    if not kept_drvs:
        # No kept drvs ⇒ no archive. Still publish an empty file so the
        # primary's quiesce-watcher (archive-presence based) stays
        # consistent — zero variants is a valid outcome for a binary
        # with all archs gated out by the support table. Downstream
        # readers (build_worker.ensure_binary_archive_imported /
        # dep_graph discover_archives) treat a zero-byte archive as "no
        # variants".
        staged = _staged_archive_path(archive)
        staged.parent.mkdir(parents=True, exist_ok=True)
        with open(staged, "wb") as fh:
            fh.write(b"")
        _publish_archive(task, staged, archive)
        return

    req_argv: list[str] = [
        "nix-store",
        "--query",
        "--requisites",
        "--stdin",
    ]
    req_stdin = "\n".join(kept_drvs).encode("utf-8")
    req_stdout, req_stderr, req_rc = run_subprocess(req_argv, input=req_stdin)
    if req_rc != 0:
        raise RuntimeError(
            f"nix-store --query --requisites failed (rc={req_rc}): "
            + req_stderr.decode("utf-8", errors="replace").strip()
        )

    closure: list[str] = [
        line.strip()
        for line in req_stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if not closure:
        raise RuntimeError(
            "nix-store --query --requisites returned no paths for "
            f"kept_drvs={kept_drvs!r}"
        )
    _LOG.info(
        "closure of %d seed drv(s): %d paths", len(kept_drvs), len(closure),
    )

    # Toolchain dedup: subtract the toolchain aggregate's requisites from
    # the matrix closure so the per-binary archive carries only the
    # binary-specific diff. Order-preserving filter (keeps the closure's
    # deterministic ordering) over a set of excluded paths. The toolchain
    # closure is recomputed locally — it is already resident as inputDrv
    # #0 of the matrix aggregate, so no manifest needs shipping.
    if exclude_seed:
        excl_argv: list[str] = [
            "nix-store", "--query", "--requisites", "--stdin",
        ]
        excl_stdout, excl_stderr, excl_rc = run_subprocess(
            excl_argv, input=exclude_seed.encode("utf-8"),
        )
        if excl_rc != 0:
            raise RuntimeError(
                "nix-store --query --requisites (toolchain exclude) failed "
                f"(rc={excl_rc}): "
                + excl_stderr.decode("utf-8", errors="replace").strip()
            )
        excluded = {
            line.strip()
            for line in excl_stdout.decode(
                "utf-8", errors="replace",
            ).splitlines()
            if line.strip()
        }
        before = len(closure)
        closure = [p for p in closure if p not in excluded]
        _LOG.info(
            "toolchain-dedup: excluded %d paths, diff = %d paths",
            before - len(closure), len(closure),
        )
        if not closure:
            raise RuntimeError(
                "toolchain-dedup closure subtraction left nothing to export "
                f"for kept_drvs={kept_drvs!r} (exclude_seed={exclude_seed!r})"
            )

    staged = _staged_archive_path(archive)
    staged.parent.mkdir(parents=True, exist_ok=True)

    export_argv: list[str] = [
        "nix-store",
        "--export",
        "--stdin",
    ]
    _LOG.info("exporting %d paths -> %s", len(closure), archive.name)
    exp_stdin = "\n".join(closure).encode("utf-8")
    exp_stdout, exp_stderr, exp_rc = run_subprocess(export_argv, input=exp_stdin)
    if exp_rc != 0:
        raise RuntimeError(
            f"nix-store --export failed (rc={exp_rc}): "
            + exp_stderr.decode("utf-8", errors="replace").strip()
        )
    try:
        with open(staged, "wb") as fh:
            fh.write(exp_stdout)
    except OSError as exc:
        raise RuntimeError(
            f"writing nix-store --export stdout to {staged!s} failed: {exc}"
        ) from exc
    _publish_archive(task, staged, archive)
    _LOG.info("published %s (%d bytes)", archive.name, len(exp_stdout))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


_CONTAINER_FLAKE_ROOT = "/app/flake"
_CONTAINER_FLAKE_ENV = "CSR_FLAKE_DIR"


def _resolve_flake_ref(flake_ref: str) -> str:
    """When the worker runs inside the dynamic_runner secondary container
    the CWD is ``/app`` and there is no flake.nix there. The image
    bakes the flake source at ``/app/flake`` (see
    ``nix/docker-image.nix``'s ``flakeFiles`` stage) and exposes the
    underlying store path via the ``CSR_FLAKE_DIR`` env var. Prefer
    the env-var value so nix-eval-jobs sees the store path directly
    and skips its copy-into-sandbox step (which would otherwise nest
    the path under another ``/nix/store/...`` layer and 404 on
    ``flake.nix``). Fall back to ``/app/flake`` if the env var is
    missing (legacy images / unrelated container runtimes); honour
    non-default flake_ref values verbatim.

    Absolute filesystem paths get a ``path:`` prefix so ``nix eval`` and
    ``nix-eval-jobs`` both parse them as direct flake locations rather
    than "search up" for the nearest ``flake.nix``. Without the prefix
    ``nix eval /nix/store/<hash>/sub/dir#attr`` strips the trailing
    components and tries to evaluate the bare store-path root, which
    fails because the flake lives in the subdirectory.
    """
    if flake_ref != ".":
        return _as_installable(flake_ref)
    env_path = os.environ.get(_CONTAINER_FLAKE_ENV)
    if env_path and os.path.isfile(os.path.join(env_path, "flake.nix")):
        return _as_installable(env_path)
    if os.path.isdir(_CONTAINER_FLAKE_ROOT) and \
            os.path.isfile(os.path.join(_CONTAINER_FLAKE_ROOT, "flake.nix")):
        return _as_installable(_CONTAINER_FLAKE_ROOT)
    return flake_ref


def _as_installable(flake_ref: str) -> str:
    if flake_ref.startswith("/") and not flake_ref.startswith("path:"):
        return f"path:{flake_ref}"
    return flake_ref


def _sample_per_arch(
    archs: list[str],
    suffixes: list[str],
    variant_sample: Optional[int],
    variant_seed: Optional[str],
    sys_name: str,
    binary: str,
    *,
    flake_ref: str,
    run_subprocess: RunSubprocess,
) -> dict[str, list[str]]:
    """Step 1: per-arch sampled-suffix map.

    Reads ``_meta.<sys>.<binary>.<arch>`` ONCE per arch via
    :func:`_eval_meta_for_arch`, applies ``is_supported`` +
    ``is_known_bad_combo`` to drop combos the matrix predicate
    surfaces but the Python filter rejects, then deterministically
    samples via :func:`sample_suffix_attrs` when ``variant_sample > 0``.

    When ``variant_sample`` is falsy the (filtered) full set passes
    through. When ``suffixes`` is non-empty (legacy payload) it is
    honoured as a ceiling — only suffixes present in the legacy list
    survive the filter; this preserves rolling-restart behaviour.
    """
    from compiler_suit_runner.support_table import (  # noqa: PLC0415
        is_supported,
        load_support_table,
    )
    from compiler_suit_runner.preflight import is_known_bad_combo  # noqa: PLC0415

    support = load_support_table()
    legacy_ceiling = set(suffixes) if suffixes else None
    sampled_by_arch: dict[str, list[str]] = {}
    for arch in archs:
        meta = _eval_meta_for_arch(
            sys_name, binary, arch,
            flake_ref=flake_ref,
            run_subprocess=run_subprocess,
        )
        filtered: dict[str, dict] = {}
        for suffix, meta_entry in meta.items():
            if legacy_ceiling is not None and suffix not in legacy_ceiling:
                continue
            if not isinstance(meta_entry, dict):
                continue
            if not is_supported(
                support, meta_entry.get("compiler", ""), arch
            ):
                continue
            if is_known_bad_combo(meta_entry) is not None:
                continue
            filtered[suffix] = meta_entry
        if variant_sample and variant_sample > 0 and variant_seed:
            sampled = sample_suffix_attrs(
                filtered,
                arch=arch,
                sample_size=variant_sample,
                seed=variant_seed,
            )
            sampled_by_arch[arch] = sorted(sampled.keys())
        else:
            sampled_by_arch[arch] = sorted(filtered.keys())
    return sampled_by_arch


def _variants_from_bulk(
    bulk_drvs: dict[tuple[str, str], str],
    binary: str,
) -> list[dict[str, str]]:
    """Materialise the per-leaf summary list from the bulk-eval result.

    Returns the ``variants`` summary the result dict surfaces, sorted
    by ``(arch, suffix)`` so consumers see a deterministic ordering
    across runs. The previous companion ``_enqueue_broadcasts`` /
    ``_wait_for_broadcasts`` pair fanned each leaf out to every peer
    via :mod:`peer_replication`; that loop was retired once phase 4
    (build_worker) gained per-binary archive import. The archive
    carries the closure to every secondary in one shot.
    """
    variants: list[dict[str, str]] = []
    for (arch, suffix), drv in sorted(bulk_drvs.items()):
        label = f"{binary}__{arch}__{suffix}"
        variants.append({"label": label, "drv": drv, "arch": arch,
                         "suffix": suffix})
    return variants


def _build_matrix_aggregate(
    toolchain_aggregate_drv: str,
    kept_drvs: list[str],
    binary: str,
    sys_name: str,
) -> Optional[str]:
    """Step 5: build the per-binary ``matrix-<binary>`` aggregate drv.

    The aggregate carries the phase-1 toolchain aggregate AND every
    sampled variant leaf as inputDrvs. Downstream phase 3 walks
    ``nix-store --query --tree <matrix_agg>`` whose refcount-sort
    then floats the toolchain layer to a sensible position relative
    to the matrix layer instead of burying it under a single leaf.
    Same ``variant_seed`` + ``variant_sample`` + ``sampled_by_arch``
    → same sorted leaf list → same wrapper-drv hash.

    Returns ``None`` when ``kept_drvs`` is empty (binary with all
    archs gated out) — the wrapper helper would otherwise raise on
    an empty drv list.
    """
    if not kept_drvs:
        return None
    # Imported lazily so unit tests that monkeypatch this symbol see
    # the indirection (and so the heavy nix-instantiate subprocess
    # only runs when there is actually a non-empty matrix to wrap).
    from template_graph.make_sum_drv import (  # noqa: PLC0415
        make_wrapper_drv_from_paths,
    )
    return make_wrapper_drv_from_paths(
        drvs=[toolchain_aggregate_drv, *kept_drvs],
        name=f"matrix-{binary}",
        system=sys_name,
    )


def _export_matrix_archive(
    matrix_aggregate_drv: Optional[str],
    archive: pathlib.Path,
    *,
    task: Task,
    run_subprocess: RunSubprocess,
    exclude_seed: Optional[str] = None,
) -> None:
    """Step 6: export the matrix-aggregate closure into the per-binary
    archive.

    Because the aggregate carries every kept leaf AND the toolchain
    aggregate as inputDrvs, exporting its closure via ``nix-store
    --query --requisites`` + ``nix-store --export`` carries the whole
    drv graph in a single archive — phase 3 can ``nix-store --import``
    the archive on the primary and walk the full graph without
    re-evaluating the flake. When ``matrix_aggregate_drv`` is ``None``
    (a binary with all archs gated out) we still write an empty archive
    so the primary's archive-presence quiesce signal stays consistent.

    ``exclude_seed`` (the toolchain aggregate drv) enables toolchain
    dedup: the exported closure is ``requisites(matrix) −
    requisites(toolchain)`` and the toolchain ships once via the
    pre-flight ``toolchains.drv.archive``. ``None`` exports the full
    closure (rollback). The toolchain stays an inputDrv of the matrix
    aggregate either way — only the exported BYTES are trimmed.
    """
    export_seeds = [matrix_aggregate_drv] if matrix_aggregate_drv else []
    _export_kept_closure(
        archive, export_seeds,
        task=task,
        run_subprocess=run_subprocess,
        exclude_seed=exclude_seed,
    )


def run_eval_task(
    payload: dict,
    out_dir: pathlib.Path,
    task: Task,
    run_subprocess: Optional[RunSubprocess] = None,
    *,
    flake_ref: str = ".",
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """Matrix-eval per-binary eval dispatch entry point.

    See the module docstring for the protocol layout. The only on-disk
    artefact is the binary archive at
    ``out_dir/matrix-<binary>.drv.archive``; the returned dict is an
    in-process summary. Every invocation re-runs the full pipeline —
    there is no resume fast-path. An in-run second attempt is always a
    failure restart (the cached on-disk archive cannot be trusted), and
    the framework publish step (stage → ``Task.publish(dst=...)``) re-
    delivers over any prior file atomically. Failure modes raise
    :class:`RuntimeError`
    so the harness surfaces ``ErrorType::Errored`` (retry-pass) — never
    ``Unfulfillable`` (that belongs to toolchain-validate).
    """
    clock = now or time.time
    runner = run_subprocess or _default_run_subprocess
    flake_ref = _resolve_flake_ref(flake_ref)
    parsed = parse_payload(payload)
    binary = parsed["binary"]
    sys_name = parsed["sys"]
    archive = _archive_path(out_dir, binary)
    _LOG.info(
        "matrix_eval START binary=%s sys=%s archs=%d",
        binary, sys_name, len(parsed["archs"]),
    )

    # Step 1: sample per arch.
    sampled_by_arch = _sample_per_arch(
        parsed["archs"], parsed["suffixes"],
        parsed["variant_sample"], parsed["variant_seed"],
        sys_name, binary,
        flake_ref=flake_ref, run_subprocess=runner,
    )
    # Step 2: bulk nix-eval-jobs for every (arch, suffix) in one pass.
    bulk_drvs = _eval_jobs_for_binary(
        parsed["attr"], sampled_by_arch,
        flake_ref=flake_ref, run_subprocess=runner,
    )
    # Step 3: materialise the per-leaf summary list (sorted for stable
    # consumer ordering). The previous broadcast loop is gone — phase 4
    # imports the per-binary archive directly.
    variants = _variants_from_bulk(bulk_drvs, binary)
    # Step 4: build the per-binary matrix-aggregate drv.
    kept_drvs: list[str] = sorted({v["drv"] for v in variants})
    _LOG.info(
        "matrix_eval binary=%s: eval-jobs produced %d variant drvs"
        " (kept %d unique)",
        binary, len(variants), len(kept_drvs),
    )
    _LOG.info(
        "matrix_eval binary=%s: building matrix aggregate over %d kept drvs"
        " (+toolchain agg)",
        binary, len(kept_drvs),
    )
    # Import the shared ``toolchains.drv.archive`` BEFORE building the
    # matrix aggregate. The aggregate's ``nix-instantiate`` references the
    # toolchain aggregate drv (``toolchain_aggregate_drv``, inputDrv #0),
    # so ``toolchains.drv`` must be resident in this worker's store FIRST
    # or the instantiate fails "path '…-toolchains.drv' is required, but
    # there is no substituter that can build it". A warm store may already
    # hold it (so this is idempotent), but a cold store needs the import
    # here — importing after the aggregate build (as before) is too late.
    # Skipped in dedup-OFF mode (no shared archive; the toolchain must be
    # locally realised / substitutable).
    if parsed["toolchain_dedup"] and parsed["toolchain_aggregate_drv"] is not None:
        _LOG.info(
            "matrix_eval binary=%s: importing shared toolchains.drv.archive",
            binary,
        )
        _import_toolchain_archive(out_dir, run_subprocess=runner)
    matrix_aggregate_drv = _build_matrix_aggregate(
        parsed["toolchain_aggregate_drv"], kept_drvs, binary, sys_name,
    )
    _LOG.info(
        "matrix_eval binary=%s: matrix_aggregate=%s",
        binary, matrix_aggregate_drv,
    )
    # Step 5: export the matrix-aggregate closure into the archive. With
    # toolchain dedup ON (default) subtract the toolchain aggregate's
    # closure so the per-binary archive carries only the binary-specific
    # diff; the toolchain ships once via the pre-flight
    # ``toolchains.drv.archive``. OFF (rollback) => full closure.
    _LOG.info(
        "matrix_eval binary=%s: toolchain_dedup=%s",
        binary, parsed["toolchain_dedup"],
    )
    exclude_seed = (
        parsed["toolchain_aggregate_drv"]
        if parsed["toolchain_dedup"] else None
    )
    # The shared ``toolchains.drv.archive`` was already imported ABOVE,
    # before the matrix-aggregate instantiate, so the toolchain closure is
    # resident for BOTH the aggregate build and the ``exclude_seed``
    # requisites subtract below (a stranded/missing upload hard-fails at
    # that import point, before any per-binary work).
    _LOG.info(
        "matrix_eval binary=%s: exporting per-binary archive %s (dedup diff)",
        binary, archive.name,
    )
    _export_matrix_archive(
        matrix_aggregate_drv, archive, task=task, run_subprocess=runner,
        exclude_seed=exclude_seed,
    )
    # Step 5b: publish the matrix_aggregate drv path as a keyed task
    # output. The framework's keyed-outputs API (Task.publish_string,
    # added in dynamic-runner 58931e4) threads the value through the
    # DoneResponse wire frame and surfaces it to the dependency_graph
    # task via predecessor_outputs[task_id]["matrix_aggregate_drv"],
    # replacing the prior per-binary JSON sidecar drop.
    task.publish_string("matrix_aggregate_drv", matrix_aggregate_drv)
    _LOG.info(
        "matrix_eval DONE binary=%s: %d variants, matrix_aggregate=%s",
        binary, len(variants), matrix_aggregate_drv,
    )
    # Step 6: in-process summary. ``variant_drvs`` is kept for
    # backwards compatibility with legacy consumers (retired by D.1b).
    return {
        "binary": binary,
        "sys": sys_name,
        "produced_at": float(clock()),
        "variant_drvs": kept_drvs,
        "variants": variants,
        "matrix_aggregate_drv": matrix_aggregate_drv,
    }


__all__ = [
    "MATRIX_EVAL_ITEM_CLASS",
    "RunSubprocess",
    "parse_payload",
    "read_peer_push_urls",
    "run_eval_task",
    "sample_suffix_attrs",
]


# ---------------------------------------------------------------------------
# Peer push URL enumeration (public helper)
#
# Historically used by :func:`workers.build_worker.main` to construct
# the per-drv broadcast sender in :mod:`peer_replication`. That loop
# was retired in favour of per-binary archive import (see module
# docstring); the helper is retained because consumers outside this
# module still reference it as a peer-URL lookup.


def read_peer_push_urls(
    shared_fs: Optional[pathlib.Path],
    self_secondary_id: str,
) -> list[str]:
    """Enumerate peer push URLs from ``<shared_fs>/peers/*.json``.

    Reads the gossip directory each time it is called so a worker
    picks up peers that joined after process start. Returns the push
    URLs (``http://<host>:<harmonia_port + PUSH_PORT_OFFSET>``) for
    every peer except ``self_secondary_id``.

    Returns an empty list when ``shared_fs`` is unset or the
    ``peers/`` directory does not exist yet — this gracefully covers
    the submitter-peer timing race (workers hit toolchain_validate
    before the submitter gossip lands) so callers degrade rather
    than raising. TODO(#submitter-gossip-race): once the submitter
    publishes its peer file BEFORE any toolchain_validate task is
    queued, this empty-list path becomes purely a single-peer
    cluster fallback.
    """
    if shared_fs is None:
        return []
    # Late import so unit tests that mock the peer-url provider do not
    # have to import peer_cache transitively.
    from compiler_suit_runner import peer_cache  # noqa: PLC0415
    from compiler_suit_runner.peer_push import push_port_for  # noqa: PLC0415

    try:
        peers = peer_cache.list_peers(
            pathlib.Path(shared_fs),
            exclude_id=self_secondary_id or None,
        )
    except Exception:  # noqa: BLE001 — gossip is best-effort
        return []
    urls: list[str] = []
    for p in peers:
        urls.append(f"http://{p.hostname}:{push_port_for(p.port)}")
    return urls


# Backwards-compatible private alias. ``_read_peer_push_urls`` was the
# original name when this helper lived next to the (now removed)
# ``eval_worker.main`` entry point; existing unit tests reference the
# underscored form. The public symbol is :func:`read_peer_push_urls`
# (exported via ``__all__``).
_read_peer_push_urls = read_peer_push_urls

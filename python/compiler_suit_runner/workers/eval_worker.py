"""Matrix-eval distributed-eval worker — one task per binary.

Runs on a cluster secondary. Given a manifest payload built by
:func:`compiler_suit_runner.manifest_gen.make_matrix_eval_header`, the
worker:

1. Resolves the per-(arch, suffix) drv set by invoking a SINGLE
   ``nix-eval-jobs --flake <attr>`` against the local /nix/store
   that intersects every requested arch AND every sampled suffix
   in one pass (``builtins.intersectAttrs`` at both the arch and
   suffix levels, plus ``--force-recurse`` so the walker descends
   through both nested levels). The worker re-applies the same
   deterministic ``_sample_suffix_attrs`` sampling as the submitter
   (keyed on the payload's ``variant_seed``) BEFORE the bulk eval
   so primary and secondaries agree on the variant subset without
   the submitter ever shipping the list.
2. Broadcasts each produced ``.drv`` path to every peer in the
   cluster via :class:`peer_replication.BroadcastSender` (which
   posts ``/peer/path-broadcast-offer`` and lets the receiver flood
   onward). Each receiver substitutes the drv into its local store
   so Phase 1+ tasks scheduled anywhere in the cluster can read the
   graph immediately.
3. Builds the per-binary ``matrix-<binary>`` aggregate drv via
   :func:`template_graph.make_sum_drv.make_wrapper_drv_from_paths`,
   passing the phase-1 toolchain aggregate AND every sampled leaf
   as inputDrvs. The aggregate is a trivial ``bash -c true`` wrapper
   whose only purpose is to carry the input set so phase 3's
   ``nix-store --query --tree`` refcount-sort floats the toolchain
   layer to a sensible position relative to the matrix layer.
4. Exports the aggregate closure to
   ``<matrix_eval_out_dir>/<binary>.nix-archive`` via
   ``nix-store --query --requisites`` + ``nix-store --export`` so the
   primary's ``_MatrixEvalQuiesceWatcher`` + ``dependency_graph_worker``
   can re-import the full drv graph (toolchain aggregate + matrix
   aggregate + every leaf) into the primary's local store without
   re-evaluating the flake. The ``dependency_graph_worker`` discovers
   the ROOT (kept) drvs by parsing the variant path of each imported
   .drv directly (``parse_variant_path``) — no JSON sidecar is
   emitted.

Resume marker
-------------

The archive itself IS the resume marker. A short-circuit fires when
the archive exists and is non-empty: re-execution skips eval +
broadcast + export. The hard-cutover design uses archive presence as
the single source of truth; the dependency_graph_worker derives
variant_lookup from the imported .drv paths via ``parse_variant_path``,
so no JSON sidecar is emitted.

Error-type contract (framework integration)
-------------------------------------------

The dynamic_runner framework distinguishes two failure types:

* ``ErrorType::Errored`` — transient / recoverable. The task is
  retry-eligible against the **retry-pass budget**. Use this for
  nix-eval-jobs subprocess failures, network failures during the
  broadcast fan-out, and similar.

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
* Phase 1 task spawn waits for the per-binary marker file; a
  binary's resume marker is single-writer so we never race on it.

See ``~/.claude/plans/lively-beaming-summit.md`` Part B for the
full rationale and the Phase 1 planner that consumes the marker.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any, Optional

from compiler_suit_runner.peer_paths import ITEM_CLASS_MATRIX_EVAL_DRV
from compiler_suit_runner.peer_replication import BroadcastSender


# ``run_subprocess`` accepts argv (list[str]) and returns a tuple of
# (stdout_bytes, stderr_bytes, returncode). Mirrors the
# ``RunSubprocess`` callable shape in ``preflight.py`` so the same
# fakes can be reused.
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


MATRIX_EVAL_ITEM_CLASS = "matrix_eval"
"""Item class string this worker handles (matches manifest_gen)."""


_SAFE_SUFFIX_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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
# Deterministic variant sampling
# ---------------------------------------------------------------------------


def sample_suffix_attrs(
    suffix_attrs: dict,
    *,
    arch: str,
    sample_size: int,
    seed: str,
) -> dict:
    """Re-implementation of ``preflight._sample_suffix_attrs``.

    Deterministically down-samples ``{suffix: meta_entry}`` to
    ``sample_size`` per ``(compiler, optimization)`` group, seeded on
    ``f"{seed}:{compiler}:{arch}:{opt}"`` so the same operator seed
    produces the same sample on every peer.

    Suffixes with non-dict meta or missing compiler/optimization
    metadata are passed through unchanged (we cannot group them).

    The implementation MUST stay bit-for-bit in lock-step with the
    submitter-side ``_sample_suffix_attrs``; any drift would let
    Phase 0 workers and the submitter disagree on the variant
    subset, breaking Phase 1 dedup. The two implementations are
    intentionally kept side-by-side rather than imported so that a
    future refactor of one cannot silently change the wire
    contract of the other.
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
    suffixes = payload.get("suffixes")
    attr = payload.get("attr")
    if not isinstance(binary, str) or not binary:
        raise ValueError(f"matrix_eval payload: invalid 'binary' ({binary!r})")
    if not isinstance(sys_name, str) or not sys_name:
        raise ValueError(f"matrix_eval payload: invalid 'sys' ({sys_name!r})")
    if not isinstance(archs, list) or not all(isinstance(a, str) for a in archs):
        raise ValueError(f"matrix_eval payload: invalid 'archs' ({archs!r})")
    if not isinstance(suffixes, list) or not all(
        isinstance(s, str) for s in suffixes
    ):
        raise ValueError(
            f"matrix_eval payload: invalid 'suffixes' ({suffixes!r})"
        )
    if not isinstance(attr, str) or not attr:
        raise ValueError(f"matrix_eval payload: invalid 'attr' ({attr!r})")
    for s in suffixes:
        if not _SAFE_SUFFIX_RE.match(s):
            raise ValueError(
                f"matrix_eval payload: unsafe suffix {s!r} — refusing to splice"
            )

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

    return {
        "binary": binary,
        "sys": sys_name,
        "archs": list(archs),
        "suffixes": list(suffixes),
        "attr": attr,
        "variant_sample": variant_sample,
        "variant_seed": variant_seed,
        "toolchain_aggregate_drv": toolchain_aggregate_drv,
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
    (a missing key matches no cells).

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

    # Build the per-arch suffix filter map, e.g.
    #   { "x86_64" = { "O0" = null; "O2" = null; };
    #     "aarch64" = { "O0" = null; }; }
    arch_blocks: list[str] = []
    for arch in sorted(effective):
        keys = " ".join(f'"{s}" = null;' for s in effective[arch])
        arch_blocks.append(f'"{arch}" = {{ {keys} }};')
    arch_filter = "{ " + " ".join(arch_blocks) + " }"

    # The select lambda: filter outer archs first, then per surviving
    # arch intersectAttrs against THAT arch's sampled suffixes. The
    # ``filter`` attrset lookup ``filter.${a}`` is safe because the
    # outer intersectAttrs guarantees ``a`` is one of our keys.
    select_expr = (
        f"m: let filter = {arch_filter}; in "
        "builtins.mapAttrs (a: v: builtins.intersectAttrs filter.${a} v) "
        "(builtins.intersectAttrs filter m)"
    )

    argv: list[str] = [
        "nix-eval-jobs",
        "--flake",
        f"{flake_ref}#{attr}",
        "--select",
        select_expr,
        "--force-recurse",
        "--max-jobs",
        "1",
    ]
    stdout, stderr, rc = run_subprocess(argv)
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


def _drv_size(drv_path: str) -> int:
    """Best-effort byte size for the broadcast offer.

    The receiver uses this for an opportunistic disk-budget check
    only; an inaccurate value is non-fatal. Missing files give 0 so
    we still broadcast (the receiver will resolve via substitution).
    """
    try:
        return os.path.getsize(drv_path)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Archive (resume marker for post-A3 hard-cutover format)
# ---------------------------------------------------------------------------


def _archive_path(out_dir: pathlib.Path, binary: str) -> pathlib.Path:
    """Per-binary archive path under the matrix-eval output dir.

    ``out_dir`` is the matrix-eval-specific dir (e.g. ``_matrix_eval``
    on the host, ``/app/out-network/_matrix_eval`` in the container);
    each binary's kept-variant closure lands at ``<out_dir>/<binary>.nix-archive``.
    """
    return out_dir / f"{binary}.nix-archive"


def _export_kept_closure(
    archive: pathlib.Path,
    kept_drvs: list[str],
    *,
    run_subprocess: RunSubprocess,
) -> None:
    """Export the closure of ``kept_drvs`` into ``archive``.

    Two subprocess invocations:

      1. ``nix-store --query --requisites <kept_drvs...>`` to enumerate
         every store path in the transitive closure.
      2. ``nix-store --export <closure_paths...>`` whose stdout is the
         self-contained archive byte stream we redirect to disk.

    The archive is written atomically via ``.tmp`` + ``os.replace`` so a
    crash mid-export never leaves a half-written file the primary would
    mis-import.

    Mirrors :func:`workers.build_compilers_worker.export_closure` but
    in-module so eval_worker stays free of cross-worker imports and the
    injected ``run_subprocess`` seam mirrors the rest of this module.

    Raises :class:`RuntimeError` on any subprocess failure (retry-pass
    eligible per the worker's error-type contract).
    """
    if not kept_drvs:
        # No kept drvs ⇒ no archive. Still produce an empty file so the
        # primary's archive presence-check resume marker stays consistent
        # (zero variants is a valid outcome for a binary with all archs
        # gated out by the support table).
        archive.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_suffix(archive.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(b"")
        os.replace(tmp, archive)
        return

    req_argv: list[str] = [
        "nix-store",
        "--query",
        "--requisites",
        *kept_drvs,
    ]
    req_stdout, req_stderr, req_rc = run_subprocess(req_argv)
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

    archive.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = archive.with_suffix(archive.suffix + ".tmp")
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
    exp_stdout, exp_stderr, exp_rc = run_subprocess(export_argv)
    if exp_rc != 0:
        try:
            tmp_archive.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"nix-store --export failed (rc={exp_rc}): "
            + exp_stderr.decode("utf-8", errors="replace").strip()
        )
    try:
        with open(tmp_archive, "wb") as fh:
            fh.write(exp_stdout)
        os.replace(tmp_archive, archive)
    except OSError as exc:
        raise RuntimeError(
            f"writing nix-store --export stdout to {archive!s} failed: {exc}"
        ) from exc


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
    """
    if flake_ref != ".":
        return flake_ref
    env_path = os.environ.get(_CONTAINER_FLAKE_ENV)
    if env_path and os.path.isfile(os.path.join(env_path, "flake.nix")):
        return env_path
    if os.path.isdir(_CONTAINER_FLAKE_ROOT) and \
            os.path.isfile(os.path.join(_CONTAINER_FLAKE_ROOT, "flake.nix")):
        return _CONTAINER_FLAKE_ROOT
    return flake_ref


def run_eval_task(
    payload: dict,
    out_dir: pathlib.Path,
    broadcast_sender: BroadcastSender,
    run_subprocess: Optional[RunSubprocess] = None,
    *,
    flake_ref: str = ".",
    broadcast_timeout: float = 10.0,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """Matrix-eval per-binary eval dispatch entry point.

    See the module docstring for the protocol. The function returns
    an in-process summary dict (binary/sys/variant_drvs/variants/
    broadcasts/produced_at) for caller introspection; the only on-disk
    artefact is the binary archive at ``out_dir/<binary>.nix-archive``.
    The dependency_graph_worker derives variant lookup from the
    imported .drv paths via ``parse_variant_path`` — no JSON sidecar
    is written.

    Failure modes raise :class:`RuntimeError` — the framework worker
    harness then surfaces ``ErrorType::Errored`` to the primary,
    which charges the failure to the retry-pass budget. We
    deliberately do NOT raise ``Unfulfillable`` from this layer; a
    secondary that cannot fulfil matrix_eval (e.g. permanently missing
    toolchain) should signal that via toolchain-validate's task-dispatch
    refusal, not by mutating a matrix_eval task's error type.

    Parameters
    ----------
    payload :
        The matrix_eval manifest payload (see
        :func:`manifest_gen.make_matrix_eval_header`).
    out_dir :
        Matrix-eval-specific output directory (the bind-mounted shared
        path). The archive is written to
        ``out_dir / <binary>.nix-archive``.
    broadcast_sender :
        :class:`peer_replication.BroadcastSender` instance owned by
        the worker process — lifecycle management (start/stop) is
        the caller's responsibility.
    run_subprocess :
        Optional injected subprocess runner; defaults to a real
        ``subprocess.run``. Tests override.
    flake_ref :
        Flake reference passed to ``nix eval`` for the ``_meta``
        lookup. Defaults to ``.`` (current directory) so the
        worker resolves against the flake checkout shipped by the
        framework into the secondary's working directory.
    broadcast_timeout :
        Seconds to wait on each :meth:`BroadcastSender.wait_for_completion`
        call. A timeout is non-fatal — we record the broadcast id
        and continue. The flood-fill protocol is opportunistic.
    now :
        Injected clock for the ``produced_at`` timestamp. Defaults
        to :func:`time.time`.
    """
    clock = now or time.time
    runner = run_subprocess or _default_run_subprocess
    flake_ref = _resolve_flake_ref(flake_ref)
    parsed = parse_payload(payload)
    binary = parsed["binary"]
    sys_name = parsed["sys"]
    archs = parsed["archs"]
    suffixes = parsed["suffixes"]
    attr = parsed["attr"]
    variant_sample = parsed["variant_sample"]
    variant_seed = parsed["variant_seed"]
    toolchain_aggregate_drv = parsed["toolchain_aggregate_drv"]

    archive = _archive_path(out_dir, binary)

    # Step 0: resume short-circuit. If the archive exists and is
    # non-empty we trust that some prior run of this task (perhaps on
    # a different secondary that previously held it) already broadcast
    # every drv to the cluster AND exported the kept-variant closure.
    # The primary's _MatrixEvalQuiesceWatcher uses archive presence as
    # the matrix_eval-quiesce signal, so the contract is "archive
    # present == matrix_eval done". The consumer
    # (dependency_graph_worker) derives variant lookup from the
    # imported .drv paths directly, so re-running this worker would
    # only duplicate work.
    if archive.exists() and archive.stat().st_size > 0:
        # Resumed runs do not re-build the matrix aggregate (the archive
        # already carries it). The downstream watcher derives variant
        # lookup from the imported .drv paths via parse_variant_path so
        # the aggregate-drv field is informational here; we omit it so
        # callers don't misread a non-empty value as "freshly built".
        return {
            "binary": binary,
            "sys": sys_name,
            "produced_at": float(clock()),
            "variant_drvs": [],
            "variants": [],
            "broadcasts": [],
            "matrix_aggregate_drv": None,
            "resumed": True,
        }

    # Step 1: sample per arch. If sampling is in play we first read
    # ``_meta`` to drive the deterministic group-aware sample
    # (matching the submitter's ``_sample_suffix_attrs`` logic), so
    # the sampled subset stays consistent across re-runs and across
    # peers without the submitter shipping the list. Each arch may
    # end up with a different sample.
    sampled_by_arch: dict[str, list[str]] = {}
    for arch in archs:
        if variant_sample and variant_sample > 0 and variant_seed:
            meta = _eval_meta_for_arch(
                sys_name, binary, arch,
                flake_ref=flake_ref,
                run_subprocess=runner,
            )
            # Filter meta to the suffixes named in the payload — the
            # submitter has already applied support-table + known-bad
            # filtering, so a meta entry NOT in payload['suffixes']
            # means the submitter has dropped it for that arch.
            allowed = set(suffixes)
            scoped = {s: m for s, m in meta.items() if s in allowed}
            sampled = sample_suffix_attrs(
                scoped,
                arch=arch,
                sample_size=variant_sample,
                seed=variant_seed,
            )
            sampled_by_arch[arch] = sorted(sampled.keys())
        else:
            sampled_by_arch[arch] = list(suffixes)

    # Step 2: one bulk ``nix-eval-jobs`` invocation that intersects
    # every requested (arch, suffix) pair in a single pass. This
    # replaces the previous per-arch loop and is the central
    # performance win of the matrix-aggregate refactor — the
    # ``dataset.<sys>.<binary>`` subtree is evaluated once, sharing
    # the eval cache across archs inside that single nix-eval-jobs
    # process.
    bulk_drvs = _eval_jobs_for_binary(
        attr,
        sampled_by_arch,
        flake_ref=flake_ref,
        run_subprocess=runner,
    )

    # Step 3: enqueue a broadcast for each (arch, suffix → drv). The
    # broadcast is non-blocking; the worker thread inside
    # BroadcastSender fans the offer out to every peer.
    variants: list[dict[str, str]] = []
    broadcast_ids: list[tuple[str, str]] = []
    for (arch, suffix), drv in sorted(bulk_drvs.items()):
        label = f"{binary}__{arch}__{suffix}"
        variants.append({"label": label, "drv": drv, "arch": arch,
                         "suffix": suffix})
        bid = broadcast_sender.enqueue_broadcast(
            drv,
            _drv_size(drv),
            item_class=ITEM_CLASS_MATRIX_EVAL_DRV,
        )
        broadcast_ids.append((label, bid))

    # Step 4: wait for each broadcast to complete (or time out).
    # Timeout is non-fatal: the flood-fill protocol is best-effort
    # and a slow peer will eventually pull via substitution when
    # the Phase 1 task lands on it. We still record the result so
    # operators can diagnose churn.
    broadcast_results: list[dict[str, Any]] = []
    for label, bid in broadcast_ids:
        result = broadcast_sender.wait_for_completion(
            bid, timeout=broadcast_timeout
        )
        entry: dict[str, Any] = {"label": label, "broadcast_id": bid}
        if result is None:
            entry["status"] = "timeout"
        else:
            entry["status"] = "ok"
            entry["success_count"] = result.success_count
            entry["fail_count"] = result.fail_count
            entry["failed_peers"] = list(result.failed_peers)
        broadcast_results.append(entry)

    # Step 5: build the per-binary ``matrix-<binary>`` aggregate drv.
    # The aggregate carries the phase-1 toolchain aggregate AND every
    # sampled variant leaf as inputDrvs. Downstream phase 3 walks
    # ``nix-store --query --tree <matrix_agg>`` whose refcount-sort
    # then floats the toolchain layer to a sensible position relative
    # to the matrix layer instead of burying it under a single leaf.
    # Same ``variant_seed`` + ``variant_sample`` + ``sampled_by_arch``
    # → same sorted leaf list → same wrapper-drv hash.
    kept_drvs: list[str] = sorted({v["drv"] for v in variants})
    matrix_aggregate_drv: Optional[str] = None
    if kept_drvs:
        # Imported lazily so unit tests that monkeypatch this symbol
        # see the indirection (and so the heavy nix-instantiate
        # subprocess only runs when there is actually a non-empty
        # matrix to wrap).
        from template_graph.make_sum_drv import (  # noqa: PLC0415
            make_wrapper_drv_from_paths,
        )
        matrix_aggregate_drv = make_wrapper_drv_from_paths(
            drvs=[toolchain_aggregate_drv, *kept_drvs],
            name=f"matrix-{binary}",
            system=sys_name,
        )

    # Step 6: export the matrix-aggregate closure into the per-binary
    # archive. Because the aggregate carries every kept leaf AND the
    # toolchain aggregate as inputDrvs, exporting its closure via
    # ``nix-store --query --requisites`` + ``nix-store --export``
    # carries the whole drv graph in a single archive — phase 3 can
    # ``nix-store --import`` the archive on the primary and walk the
    # full graph without re-evaluating the flake. When ``kept_drvs``
    # is empty (a binary with all archs gated out) we still write an
    # empty archive so the primary's presence-based resume marker
    # stays consistent.
    export_seeds = [matrix_aggregate_drv] if matrix_aggregate_drv else []
    _export_kept_closure(archive, export_seeds, run_subprocess=runner)

    # Step 7: return an in-process summary for caller introspection.
    # No sidecar JSON is written — the archive itself is the resume
    # marker, and the dependency_graph worker derives variant lookup
    # from the imported .drv paths via ``parse_variant_path``. The
    # primary's _MatrixEvalQuiesceWatcher uses archive-presence as the
    # matrix_eval-quiesce gate. ``matrix_aggregate_drv`` is the new
    # field consumed by the watcher (D.1d) to pass into phase 3 via
    # argv; ``variant_drvs`` is kept for backwards compatibility with
    # legacy consumers and will be retired once D.1b lands.
    return {
        "binary": binary,
        "sys": sys_name,
        "produced_at": float(clock()),
        "variant_drvs": kept_drvs,
        "variants": variants,
        "broadcasts": broadcast_results,
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
# Exported so :func:`workers.build_worker.main` can construct a
# :class:`BroadcastSender` configured to fan matrix_eval drv broadcasts
# out to the cluster. The unified build_worker entry point owns the
# subprocess CLI shape now; ``eval_worker`` is a pure library module
# (``run_eval_task`` + this helper).


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
    before the submitter gossip lands) so the BroadcastSender no-ops
    instead of raising. TODO(#submitter-gossip-race): once the
    submitter publishes its peer file BEFORE any toolchain_validate
    task is queued, this empty-list path becomes purely a single-peer
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

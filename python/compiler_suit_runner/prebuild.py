"""Pre-build a list of drv paths locally on the dev box.

The runner classifies the variant matrix into "shared" drvs (cross
toolchains, libc, common host deps) and "leaf" drvs (per-variant
builds). Workstream 1 produces the shared list; this module realises it
on the dev box so harmonia can serve every closure path. Each secondary
then ``nix build``s its leaf variant against the populated dev-box
substituter and downloads instead of rebuilding.

The actual realisation is done by spawning ``nix build <drv>^* --no-link
--print-out-paths`` once per drv, in parallel up to ``jobs`` workers.
Each invocation is independent so a single broken drv does not poison
the rest. Failures are captured into the result, never re-raised.

Subprocess execution is dependency-injected (via ``run_subprocess``) so
unit tests stay hermetic. The default runner uses ``subprocess.run`` and
is thread-safe; the ``ThreadPoolExecutor`` invokes it concurrently from
worker threads with no shared mutable state.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Optional

__all__ = [
    "PrebuildResult",
    "RunSubprocess",
    "prebuild_drvs",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# Mirrors ``compiler_suit_runner.preflight.RunSubprocess``. Re-declared
# (not imported) to keep this module independent of preflight's heavier
# import surface — the prebuild step runs early, before the matrix is
# enumerated, so only the type alias is needed.
RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


@dataclasses.dataclass(frozen=True)
class PrebuildResult:
    """Outcome of one :func:`prebuild_drvs` call.

    ``succeeded`` lists ``(drv, out_path)`` for every drv whose
    ``nix build`` returned 0; ``out_path`` is the realised store path
    parsed from the last non-blank line of stdout. For multi-output
    drvs (``out``, ``dev``, ...) ``nix build`` prints one path per
    line; we keep only the last one so the field type stays simple
    (the dev-box harmonia will serve every path in the closure
    regardless of which we recorded).

    ``failed`` lists ``(drv, error_excerpt)`` for every drv whose
    invocation crashed or returned non-zero. ``error_excerpt`` is a
    decoded, truncated tail of stderr (~4 KiB).

    ``total_duration_seconds`` is wall-clock time across the whole
    parallel batch, not the sum of per-drv durations.
    """

    succeeded: tuple[tuple[str, str], ...]
    failed: tuple[tuple[str, str], ...]
    total_duration_seconds: float


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


# Truncation limit for per-drv error excerpts. The result is only used
# for human diagnostics (logs + a final summary line); 4 KiB per failure
# keeps the in-memory result bounded even for thousands of drvs.
_ERROR_EXCERPT_BYTE_LIMIT = 4 * 1024


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell.

    Thread-safe — each call is an independent OS process. The
    ``ThreadPoolExecutor`` in :func:`prebuild_drvs` calls this from
    multiple worker threads simultaneously.
    """
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _excerpt_stderr(stderr: bytes, byte_limit: int = _ERROR_EXCERPT_BYTE_LIMIT) -> str:
    """Decode and tail-truncate stderr to ``byte_limit`` UTF-8 bytes.

    Decodes with ``errors="replace"`` so binary interleaving doesn't
    raise. The *tail* of the log is where nix prints the build failure
    summary, so we keep that side.
    """
    if not stderr:
        return ""
    if len(stderr) > byte_limit:
        stderr = stderr[-byte_limit:]
    return stderr.decode("utf-8", errors="replace")


def _last_nonblank_line(stdout: bytes) -> str:
    """Return the last non-blank line of decoded stdout, or ``""``.

    ``nix build --print-out-paths`` writes one path per line for
    multi-output drvs; we use the last (alphabetical-by-output-name on
    modern nix) as the canonical realised path. Empty stdout means
    nothing was realised — caller treats that as success-with-empty,
    not as failure.
    """
    if not stdout:
        return ""
    text = stdout.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return ""


def _build_one(
    drv: str,
    *,
    extra_args: list[str],
    runner: RunSubprocess,
    log: logging.Logger,
) -> tuple[str, Optional[str], Optional[str], float]:
    """Run one ``nix build <drv>^* --no-link --print-out-paths`` invocation.

    Returns ``(drv, out_path_or_None, error_excerpt_or_None, duration)``.
    Exactly one of ``out_path`` / ``error_excerpt`` is non-None when
    the subprocess ran; on a runner exception the subprocess crash is
    folded into the error path with a synthesised excerpt.
    """
    argv: list[str] = [
        "nix",
        "build",
        "--no-link",
        "--print-out-paths",
    ]
    argv.extend(extra_args)
    argv.append(f"{drv}^*")

    start = time.monotonic()
    try:
        stdout, stderr, rc = runner(argv)
    except Exception as exc:  # noqa: BLE001 - never raise out
        duration = time.monotonic() - start
        return drv, None, f"runner crashed: {exc}", duration

    duration = time.monotonic() - start

    if rc != 0:
        excerpt = _excerpt_stderr(stderr)
        if not excerpt:
            excerpt = f"nix build returned rc={rc} with empty stderr"
        log.warning("prebuild failed for %s in %.2fs: %s", drv, duration, excerpt)
        return drv, None, excerpt, duration

    out_path = _last_nonblank_line(stdout)
    log.info("prebuilt %s in %.2fs", drv, duration)
    return drv, out_path, None, duration


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def prebuild_drvs(
    drv_paths: Iterable[str],
    *,
    jobs: int | None = None,
    extra_args: Optional[list[str]] = None,
    run_subprocess: Optional[RunSubprocess] = None,
    log: Optional[logging.Logger] = None,
) -> PrebuildResult:
    """Realise a list of drvs locally with parallel ``nix build``.

    Spawns one ``nix build <drv>^* --no-link --print-out-paths`` per
    drv with the configured job count (defaults to ``os.cpu_count()``).
    Each invocation is independent so failures are isolated -- one
    broken drv doesn't tank the rest.

    Returns a :class:`PrebuildResult` with the split between succeeded
    and failed drvs and the total wall-clock duration.
    """
    runner = run_subprocess or _default_run_subprocess
    log = log or logging.getLogger(__name__)

    drvs: list[str] = []
    for d in drv_paths:
        if isinstance(d, str) and d:
            drvs.append(d)

    start = time.monotonic()

    if not drvs:
        return PrebuildResult(
            succeeded=(),
            failed=(),
            total_duration_seconds=0.0,
        )

    # Default jobs to os.cpu_count(); fall back to 1 if that returns None.
    if jobs is None:
        jobs = os.cpu_count() or 1
    jobs = max(1, jobs)
    max_workers = min(jobs, len(drvs))

    extras = list(extra_args) if extra_args else []

    succeeded: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _build_one,
                drv,
                extra_args=extras,
                runner=runner,
                log=log,
            )
            for drv in drvs
        ]
        for fut in concurrent.futures.as_completed(futures):
            drv, out_path, err, _duration = fut.result()
            if err is None:
                succeeded.append((drv, out_path or ""))
            else:
                failed.append((drv, err))

    # Sort by drv path so the result order is reproducible regardless of
    # which futures completed first.
    succeeded.sort(key=lambda x: x[0])
    failed.sort(key=lambda x: x[0])

    total = time.monotonic() - start
    return PrebuildResult(
        succeeded=tuple(succeeded),
        failed=tuple(failed),
        total_duration_seconds=total,
    )

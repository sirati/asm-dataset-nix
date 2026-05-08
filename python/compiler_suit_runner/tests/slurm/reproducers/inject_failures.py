"""Failure-injection helpers for the slurm test slice.

This module hosts the SHARED helper functions used by the failure-
injection rows of the SLURM test matrix (T5, T6, T8, T10). Each helper
is self-contained — the rows that do not need a given helper simply do
not import it, and helpers added by sibling tests append to this file
without touching their neighbours.

Currently exported helpers:

* :func:`kill_secondary_when_first_variant_completes` — watch a target
  secondary's ``slurm_<jobid>.out`` for the first variant-completion
  log line and SIGKILL its slurm job once observed. Owned by T5.

Convention for future helpers added by C3.3 / C3.4 / C3.5:

* Each helper is a stand-alone function (or function + dataclass pair).
* Each helper carries its own docstring naming the failure-injection
  scenario it implements.
* Each helper SCOPES scancel by ``--jobname=<pattern>`` and / or by
  explicit jobid. NEVER ``--user=kruppb`` (kruppb is shared with the
  asm-tokenizer peer in the test env per memory
  ``feedback_scancel_scope.md``).
* No global state; no module-level mutable singletons.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------


_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")
"""Strip ANSI SGR sequences from the framework's tracing output.

The Rust ``tracing`` layer wraps every field marker in colour codes
(``[3msecondary[0m=secondary-0``); substring patterns must run AFTER
ANSI strip or they miss the tokens. Mirrors the same constant in
``invariants.py`` and ``broken_toolchain.py``.
"""


# Match a ``task completed ... task_type=variant`` line. We accept BOTH
# the ``task_type=Some("variant")`` form (current Rust ``Option<&str>``
# rendering) and the bare ``task_type=variant`` form so a future
# framework version that drops the ``Some(...)`` wrapper still matches.
# We additionally insist on ``success=true`` so a failed-variant log
# line does not falsely trigger the kill (the post-promotion-with-failure
# class of bugs has its own dedicated reproducer in
# ``broken_toolchain.py``).
_VARIANT_COMPLETED_RE: re.Pattern[str] = re.compile(
    r"task completed.*?task_type=(?:Some\(\"variant\"\)|variant)"
    r".*?success=true"
)


# ``Job ID: <int>`` line emitted by the slurm wrapper at job start; used
# as a fallback path when filename parsing yields no jobid (shouldn't
# happen with the current ``slurm_<jobid>.{out,err}`` convention, but
# defensive parsing is cheap).
_JOB_ID_RE: re.Pattern[str] = re.compile(r"^Job ID:\s*(\d+)\s*$", re.MULTILINE)


# ``slurm_<jobid>.out`` filename shape; the jobid is what scancel needs.
_SLURM_OUT_FILE_RE: re.Pattern[str] = re.compile(r"^slurm_(\d+)\.out$")


# ---------------------------------------------------------------------------
# Probe protocol (loose-typed so the import doesn't drag cluster_probe in)
# ---------------------------------------------------------------------------


class _GatewayRunner(Protocol):
    """Minimal interface :func:`kill_secondary_when_first_variant_completes`
    needs from a cluster probe.

    A :class:`compiler_suit_runner.tests.slurm.cluster_probe.ClusterProbe`
    satisfies this protocol; the tests that mock subprocess pass a
    duck-typed stub instead.
    """

    def gateway_ssh(  # pragma: no cover - protocol only
        self,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class KillResult:
    """Outcome of one
    :func:`kill_secondary_when_first_variant_completes` call.

    ``triggered``: ``True`` iff the watcher saw a variant-completion line
    AND issued the scancel call. A ``False`` here means the watcher
    timed out before any variant completion was logged for the target
    secondary; the test caller can decide whether that is itself a
    failure (typical for T5 — if no variant ever completes there is no
    work to interrupt) or a benign pass-through (the run finished too
    fast to inject a kill).

    ``jobid``: the SLURM job id parsed from the secondary's slurm_*.out
    filename or its ``Job ID:`` header line. ``None`` if the watcher
    never resolved a jobid for the target secondary.

    ``killed_at``: UTC timestamp captured immediately after scancel
    returned. ``None`` if no scancel was issued.

    ``watched_for_s``: monotonic wall-clock duration spent in the watch
    loop, regardless of outcome.

    ``scancel_rc``: return code of the scancel subprocess. ``None`` if
    no scancel was issued.

    ``scancel_stdout`` / ``scancel_stderr``: captured output for triage
    when a non-zero ``scancel_rc`` surfaces.
    """

    triggered: bool
    jobid: str | None = None
    killed_at: datetime | None = None
    watched_for_s: float = 0.0
    scancel_rc: int | None = None
    scancel_stdout: str = ""
    scancel_stderr: str = ""
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _read_text_safely(path: pathlib.Path) -> str:
    """Read ``path`` and return its content, ANSI-stripped.

    Returns an empty string for any OS-level failure (the file may not
    yet exist, or be mid-flush). The watch loop handles "not yet
    present" by re-polling, so we never raise from the read path.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _strip_ansi(raw)


def _resolve_secondary_out(
    run_log_dir: pathlib.Path,
    target_secondary_id: str,
) -> Optional[pathlib.Path]:
    """Find the ``slurm_*.out`` belonging to ``target_secondary_id``.

    The framework emits one ``slurm_<jobid>.out`` per dispatched
    secondary (the slurm wrapper writes its full stdout there). We
    associate a ``slurm_*.out`` with a secondary by reading the
    ``connection_info/<secondary_id>.info`` companion file: the ``info``
    file's filename embeds the secondary id, and its ``hostname=`` line
    pins the worker, while the parent ``run_log_dir`` carries the slurm
    log files for ALL of that run's secondaries. The `slurm_*.out`'s
    own header carries ``Node: <hostname>`` so we cross-check by
    hostname rather than by reading any volatile pid.

    Returns ``None`` if no candidate file is found. The caller polls
    until either a hit shows up or the watch deadline fires.
    """
    info_path = run_log_dir / "connection_info" / f"{target_secondary_id}.info"
    target_host = _parse_info_hostname(info_path)

    candidates = sorted(run_log_dir.glob("slurm_*.out"))
    if not candidates:
        return None

    if target_host is None:
        # Fallback: a run with a single secondary has exactly one
        # slurm_*.out, so we accept it unconditionally. With multiple
        # secondaries the absence of an info file means the secondary
        # has not announced itself yet — return None and let the watch
        # loop retry.
        if len(candidates) == 1:
            return candidates[0]
        return None

    for path in candidates:
        text = _read_text_safely(path)
        if not text:
            continue
        # The slurm wrapper writes ``Node: <hostname>`` very early —
        # before any podman pull — so we can match on hostname.
        if f"Node: {target_host}" in text:
            return path
    return None


def _parse_info_hostname(info_path: pathlib.Path) -> Optional[str]:
    """Read ``hostname=<host>`` from a ``connection_info/*.info`` file."""
    if not info_path.is_file():
        return None
    try:
        text = info_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("hostname="):
            continue
        return line.partition("=")[2].strip() or None
    return None


def _jobid_from_filename(slurm_out_path: pathlib.Path) -> Optional[str]:
    """Extract ``<jobid>`` from a ``slurm_<jobid>.out`` filename."""
    m = _SLURM_OUT_FILE_RE.match(slurm_out_path.name)
    if m is None:
        return None
    return m.group(1)


def _jobid_from_header(text: str) -> Optional[str]:
    """Extract the SLURM job id from a slurm wrapper ``Job ID: <int>``
    header line."""
    m = _JOB_ID_RE.search(text)
    if m is None:
        return None
    return m.group(1)


def _saw_variant_completion(text: str) -> bool:
    """Return ``True`` iff ``text`` carries one or more
    successful-variant ``task completed`` lines."""
    return _VARIANT_COMPLETED_RE.search(text) is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def kill_secondary_when_first_variant_completes(
    probe: _GatewayRunner,
    *,
    run_log_dir: pathlib.Path,
    target_secondary_id: str,
    jobname_pattern: str = "asm-secondary-*",
    poll_interval_s: float = 2.0,
    timeout_s: float = 600.0,
    scancel_timeout_s: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> KillResult:
    """Watch ``target_secondary_id`` and SIGKILL its slurm job mid-build.

    Polls the run log directory every ``poll_interval_s`` seconds until
    the target secondary's ``slurm_<jobid>.out`` carries the first
    successful-variant ``task completed`` line. Once observed the
    helper resolves the jobid (from the filename, with a fallback to
    the wrapper's ``Job ID:`` header line) and runs::

        scancel --signal=KILL --jobname=<jobname_pattern> <jobid>

    on the gateway. The scope is BOTH a job-name glob AND an explicit
    jobid: the glob keeps us from killing unrelated jobs the
    asm-tokenizer peer might queue under the same kruppb account, and
    the explicit jobid is the conservative narrow-scope filter even
    when the test env has only one runner.

    CRITICAL (memory ``feedback_scancel_scope.md``): we MUST NOT pass
    ``--user=kruppb``. The kruppb account is shared with the
    asm-tokenizer peer in the test env.

    Parameters:

    * ``probe``: a :class:`ClusterProbe`-shaped object with a
      ``gateway_ssh`` method. Loose typing (Protocol) keeps this module
      free of the cluster_probe import at module-collection time.
    * ``run_log_dir``: the per-run log directory under
      ``SLURM_TEST_ENV_LOG_ROOT`` that the test harness drove the
      dispatch into.
    * ``target_secondary_id``: the framework's ``secondary-<n>`` id
      string. Resolved against ``connection_info/<id>.info`` to find the
      worker hostname, which we then match against the
      ``slurm_<jobid>.out`` header.
    * ``jobname_pattern``: glob passed to ``scancel --jobname=``.
      Default ``asm-secondary-*`` matches the framework's slurm
      submission shape; override only if a future test injects a
      different job-name family.
    * ``poll_interval_s``: how long to sleep between log polls. ``2.0``
      keeps CPU low while still detecting the trigger within seconds
      of the framework flushing its tracing buffer.
    * ``timeout_s``: total watch budget. After this elapses the helper
      gives up and returns a :class:`KillResult` with
      ``triggered=False``.
    * ``scancel_timeout_s``: per-call timeout for the scancel subprocess.
      Independent of the watch budget so a hung gateway during scancel
      doesn't drown out the watch result.
    * ``clock`` / ``sleep`` / ``now_utc``: injectable for unit tests.

    Returns a :class:`KillResult` describing the outcome. The helper is
    fail-safe: it captures every conceivable subprocess exception into
    ``KillResult.notes`` rather than propagating, so the test thread
    that calls it (typically a sidecar :class:`threading.Thread`) does
    not crash the main test on a transient SSH hiccup.
    """
    started = clock()
    deadline = started + max(timeout_s, 0.0)
    notes: list[str] = []

    resolved_path: Optional[pathlib.Path] = None
    while clock() < deadline:
        candidate = _resolve_secondary_out(
            run_log_dir, target_secondary_id,
        )
        if candidate is not None:
            text = _read_text_safely(candidate)
            if text and _saw_variant_completion(text):
                resolved_path = candidate
                break
        sleep(poll_interval_s)

    if resolved_path is None:
        return KillResult(
            triggered=False,
            jobid=None,
            killed_at=None,
            watched_for_s=clock() - started,
            scancel_rc=None,
            notes=tuple(
                notes
                + [
                    "watcher timed out before observing a variant "
                    "completion for "
                    f"{target_secondary_id!r} under {run_log_dir!s}",
                ],
            ),
        )

    # Resolve the jobid. Filename is the primary source ("slurm_18.out"
    # -> "18"); header is the fallback for any future framework version
    # that adopts a different filename convention.
    jobid = _jobid_from_filename(resolved_path)
    if jobid is None:
        text = _read_text_safely(resolved_path)
        jobid = _jobid_from_header(text)
    if jobid is None:
        return KillResult(
            triggered=False,
            jobid=None,
            killed_at=None,
            watched_for_s=clock() - started,
            scancel_rc=None,
            notes=tuple(
                notes
                + [
                    f"could not resolve jobid for {resolved_path.name!r} "
                    "(no Job ID: line and filename did not match "
                    "slurm_<jobid>.out)",
                ],
            ),
        )

    # Build the scancel argv. Filtering by jobname AND jobid is the
    # narrow-scope safe form per ``feedback_scancel_scope.md``.
    # ``--signal=KILL`` -> SIGKILL (the framework wrapper's EXIT/TERM
    # trap doesn't run, which is the failure mode we WANT to inject:
    # SLURM kills the secondary's wrapper without the wrapper getting a
    # chance to stop the podman container cleanly).
    scancel_cmd = (
        "scancel "
        f"--signal=KILL "
        f"--jobname={jobname_pattern} "
        f"{jobid}"
    )

    scancel_rc: Optional[int] = None
    scancel_stdout: str = ""
    scancel_stderr: str = ""
    killed_at: Optional[datetime] = None
    try:
        cp = probe.gateway_ssh(scancel_cmd, timeout=scancel_timeout_s)
        scancel_rc = cp.returncode
        scancel_stdout = cp.stdout or ""
        scancel_stderr = cp.stderr or ""
        killed_at = now_utc()
    except subprocess.TimeoutExpired as exc:
        notes.append(f"scancel timed out: {exc}")
    except OSError as exc:
        notes.append(f"scancel SSH error: {exc}")

    return KillResult(
        triggered=killed_at is not None,
        jobid=jobid,
        killed_at=killed_at,
        watched_for_s=clock() - started,
        scancel_rc=scancel_rc,
        scancel_stdout=scancel_stdout,
        scancel_stderr=scancel_stderr,
        notes=tuple(notes),
    )


__all__ = [
    "KillResult",
    "kill_secondary_when_first_variant_completes",
]

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
    "DisconnectResult",
    "KillResult",
    "kill_local_primary_driver",
    "kill_secondary_when_first_variant_completes",
]


# ---------------------------------------------------------------------------
# T6 — local primary-driver disconnect
# ---------------------------------------------------------------------------


# Matches the Rust ``tracing`` line emitted by
# ``dynrunner_manager_distributed::secondary::setup`` when a secondary has
# pulled its initial assignment from the local primary. We use this as the
# arming trigger for the disconnect helper below: once at least one
# secondary has reached this point the framework's normal dispatch loop is
# in flight and a SIGINT against the local primary exercises the
# post-disconnect promotion path (rather than aborting the dispatch before
# any secondary ever onboarded).
_INITIAL_ASSIGNMENT_RE: re.Pattern[str] = re.compile(
    r"received initial assignment"
)


@dataclasses.dataclass(frozen=True, slots=True)
class DisconnectResult:
    """Outcome of a :func:`kill_local_primary_driver` call.

    ``triggered``: ``True`` iff the watcher saw the
    ``received initial assignment`` log line on at least one
    secondary's ``slurm_*.out`` AND issued the kill against the local
    primary driver. A ``False`` here means the watcher timed out
    before any secondary onboarded, which is itself a test failure for
    T6 (the disconnect path can only be exercised once at least one
    secondary has joined).

    ``primary_pid``: the PID of the local primary driver the helper
    targeted. ``None`` only when ``triggered`` is ``False`` AND no PID
    was ever resolved (e.g. ``primary_pid=None`` argument and pgrep
    returned nothing). When the kill itself fails (e.g. the process
    exited between resolution and ``os.kill``) the PID is still
    populated so the caller can correlate with framework logs.

    ``killed_at``: UTC timestamp captured immediately after the kill
    syscall returned. ``None`` when no kill was issued.

    ``watched_for_s``: monotonic wall-clock duration spent in the watch
    loop, regardless of outcome.

    ``signal``: name of the signal sent (e.g. ``"SIGINT"``). Carried so
    the test's failure message has a self-contained record of what was
    attempted.

    ``notes``: free-form per-call notes (errors during pgrep, EPERM on
    kill, etc.). The helper is intentionally fail-safe and surfaces
    every conceivable error this way rather than raising, mirroring
    :class:`KillResult`.
    """

    triggered: bool
    primary_pid: int | None = None
    killed_at: datetime | None = None
    watched_for_s: float = 0.0
    signal: str = "SIGINT"
    notes: tuple[str, ...] = ()


def _saw_initial_assignment(text: str) -> bool:
    """Return ``True`` iff ``text`` carries an
    ``received initial assignment`` log line.

    Matches both the bare structured-log form
    (``... received initial assignment``) and any future framework
    version that adds field markers around the message; we only
    require the substring after ANSI strip.
    """
    return _INITIAL_ASSIGNMENT_RE.search(text) is not None


def _any_secondary_onboarded(run_log_dir: pathlib.Path) -> bool:
    """Scan every ``slurm_*.out`` under ``run_log_dir`` for the trigger.

    Returns ``True`` as soon as ONE secondary's slurm log carries the
    ``received initial assignment`` line. T6 only needs at least one
    secondary online; killing the primary at that point exercises the
    framework's promotion election regardless of which (or how many)
    secondaries have onboarded.
    """
    for path in sorted(run_log_dir.glob("slurm_*.out")):
        text = _read_text_safely(path)
        if text and _saw_initial_assignment(text):
            return True
    return False


def _signum_from_name(signal_name: str) -> int:
    """Resolve a ``"SIGFOO"`` string to its integer signum.

    Falls back to ``signal.SIGINT`` for an unknown name; a helper that
    silently substitutes the wrong signal would be a footgun, but the
    type of input we accept here is always one of ``SIGINT``,
    ``SIGTERM``, ``SIGKILL`` — three names that exist on every
    POSIX. A validation error is raised at call-site instead.
    """
    import signal as _signal  # noqa: PLC0415 — local import keeps top clean

    sig = getattr(_signal, signal_name, None)
    if sig is None:
        raise ValueError(
            f"unknown signal name {signal_name!r}; "
            "expected one of SIGINT/SIGTERM/SIGKILL"
        )
    return int(sig)


def _discover_primary_driver_pid(
    *,
    pgrep_pattern: str = "compiler_suit_runner",
    pgrep_extra_args: tuple[str, ...] = ("-f", "-U"),
    runner: Callable[
        [list[str]], subprocess.CompletedProcess[str]
    ] | None = None,
) -> Optional[int]:
    """Best-effort discovery of the local primary driver PID via ``pgrep``.

    Used as a FALLBACK when the test harness does not pass an explicit
    ``primary_pid``; the preferred path is to capture the
    :class:`subprocess.Popen` handle from the test's own dispatch
    invocation and pass its ``pid`` directly.

    The default ``pgrep_pattern`` matches the long-form
    ``python -m compiler_suit_runner submit`` argv emitted by the run
    helper. We constrain to the current UID via ``-U <uid>`` so the
    helper never picks a PID belonging to another user (the test-env
    gateway shares the kruppb account with the asm-tokenizer peer; on
    the host the harness owns the entire process tree, but the
    constraint is cheap and matches the policy used by the scancel
    side of this module — never operate beyond the current user).

    Returns the youngest matching PID (``-n``) on success and ``None``
    when pgrep finds no matches OR fails for any reason. The helper
    NEVER raises; pgrep's exit status of ``1`` ("no match") is treated
    as a non-error.

    ``runner`` is injectable for unit tests. The default invokes
    :func:`subprocess.run` synchronously on the host.
    """
    if runner is None:
        def _default_runner(
            argv: list[str],
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )

        runner = _default_runner

    import os as _os  # noqa: PLC0415 — local import keeps top clean

    argv: list[str] = ["pgrep", "-n"]  # newest match
    argv.extend(pgrep_extra_args)
    if "-U" in pgrep_extra_args:
        argv.append(str(_os.getuid()))
    argv.append(pgrep_pattern)
    try:
        cp = runner(argv)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode not in (0, 1):
        return None
    pid_text = (cp.stdout or "").strip().splitlines()
    if not pid_text:
        return None
    try:
        return int(pid_text[0].strip())
    except ValueError:
        return None


def kill_local_primary_driver(
    *,
    primary_pid: int | None = None,
    run_log_dir: pathlib.Path,
    signal_name: str = "SIGINT",
    arm_timeout_s: float = 60.0,
    poll_interval_s: float = 0.5,
    kill_fn: Callable[[int, int], None] | None = None,
    discover_pid: Callable[[], Optional[int]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DisconnectResult:
    """Watch ``run_log_dir`` for secondary onboarding then SIGINT the
    local primary driver.

    Polls ``run_log_dir`` every ``poll_interval_s`` seconds until at
    least one ``slurm_*.out`` carries a ``received initial assignment``
    line — the framework's marker for "this secondary has fetched its
    initial assignment from the local primary". Once observed the
    helper sends ``signal_name`` (default ``SIGINT``) to
    ``primary_pid`` (the local python ``compiler_suit_runner submit``
    process), simulating an operator-initiated disconnect.

    The kill is issued via :func:`os.kill`; the local primary driver
    runs as a child of the pytest process, NOT a slurm-managed job, so
    the gateway-side ``scancel`` path (used by
    :func:`kill_secondary_when_first_variant_completes`) does not
    apply here. SIGINT specifically — rather than SIGKILL — gives the
    framework's primary loop a chance to flush its disconnect logic
    so the secondaries see the FIN and the test exercises the
    "primary disconnected; secondary promotes" path the plan calls
    for. Override ``signal_name`` to ``"SIGTERM"`` / ``"SIGKILL"`` if
    a future test variant wants the harder paths.

    Parameters:

    * ``primary_pid``: PID of the local ``compiler_suit_runner submit``
      process. Strongly preferred over ``discover_pid``: the test
      harness should capture the :class:`subprocess.Popen` handle for
      the dispatch and pass its ``pid`` here. ``None`` triggers the
      best-effort pgrep fallback.
    * ``run_log_dir``: per-run log directory under
      ``SLURM_TEST_ENV_LOG_ROOT`` that the dispatch is writing into.
    * ``signal_name``: signal to send. ``"SIGINT"`` (default) is the
      gentlest that still triggers the framework's disconnect
      handling. Names are resolved via :mod:`signal`.
    * ``arm_timeout_s``: total budget for the arming watch loop. After
      this elapses with no ``received initial assignment`` line
      observed the helper returns a :class:`DisconnectResult` with
      ``triggered=False`` so the test can flag the timeout.
    * ``poll_interval_s``: how often to scan the slurm logs while
      arming. ``0.5`` keeps latency tight; T6's tiny workload completes
      its onboarding within seconds of the secondaries starting.
    * ``kill_fn``: injectable for unit tests. Defaults to
      :func:`os.kill`. Called as ``kill_fn(pid, signum)``.
    * ``discover_pid``: injectable PID-discovery callback. Defaults to
      :func:`_discover_primary_driver_pid`. Tests pass a stub.
    * ``clock`` / ``sleep`` / ``now_utc``: injectable for unit tests.

    Returns a :class:`DisconnectResult`. The helper never raises;
    every conceivable error (pgrep failure, kill returning EPERM /
    ESRCH, an unparseable signal name) lands in ``notes``.

    CRITICAL: this helper operates on the LOCAL HOST process tree. It
    NEVER SSHes to the gateway — the local primary driver lives on the
    machine running pytest, not on the test-env. ``scancel`` is the
    wrong tool here (the local primary is not a slurm job) and would
    accidentally cancel the secondaries' slurm jobs, defeating the
    test.
    """
    started = clock()
    deadline = started + max(arm_timeout_s, 0.0)
    notes: list[str] = []

    # Resolve signal early so a typo surfaces as a fast notes entry
    # rather than a late surprise during the kill call.
    try:
        signum = _signum_from_name(signal_name)
    except ValueError as exc:
        return DisconnectResult(
            triggered=False,
            primary_pid=primary_pid,
            killed_at=None,
            watched_for_s=clock() - started,
            signal=signal_name,
            notes=(f"{exc}",),
        )

    # Default kill / discovery fns wired here so the helper stays a
    # pure function under test (the unit test passes stubs and never
    # actually shells out).
    if kill_fn is None:
        import os as _os  # noqa: PLC0415 — local import keeps top clean

        kill_fn = _os.kill
    if discover_pid is None:
        discover_pid = _discover_primary_driver_pid

    # Arming loop: poll the slurm logs for ``received initial
    # assignment``. We do NOT require the assignment line to belong to
    # any specific secondary id — any secondary's onboarding is
    # sufficient to exercise the disconnect path.
    armed = False
    while clock() < deadline:
        if _any_secondary_onboarded(run_log_dir):
            armed = True
            break
        sleep(poll_interval_s)

    if not armed:
        notes.append(
            "watcher timed out before any secondary onboarded under "
            f"{run_log_dir!s}; the disconnect path could not be "
            "exercised"
        )
        return DisconnectResult(
            triggered=False,
            primary_pid=primary_pid,
            killed_at=None,
            watched_for_s=clock() - started,
            signal=signal_name,
            notes=tuple(notes),
        )

    # Resolve the PID. Explicit > pgrep fallback. We do NOT call pgrep
    # if the caller supplied a PID — passing an explicit pid is a
    # promise that it is the right process to interrupt.
    resolved_pid = primary_pid
    if resolved_pid is None:
        try:
            resolved_pid = discover_pid()
        except Exception as exc:  # noqa: BLE001 — surface as a note
            notes.append(f"discover_pid raised: {exc!r}")
            resolved_pid = None
        if resolved_pid is None:
            notes.append(
                "no primary_pid passed and pgrep returned no match; "
                "cannot disconnect"
            )
            return DisconnectResult(
                triggered=False,
                primary_pid=None,
                killed_at=None,
                watched_for_s=clock() - started,
                signal=signal_name,
                notes=tuple(notes),
            )

    # Issue the kill. We capture EPERM / ESRCH / OSError into notes so
    # the caller can decide how to surface them; a kill against an
    # already-exited primary (ESRCH) is itself a passing path because
    # the framework "disconnect" we want to exercise has already
    # happened.
    killed_at: Optional[datetime] = None
    try:
        kill_fn(int(resolved_pid), signum)
        killed_at = now_utc()
    except ProcessLookupError as exc:
        # ESRCH — the local primary already exited. From the
        # secondaries' point of view this is indistinguishable from a
        # disconnect, so we still flag the helper as ``triggered=True``
        # and let the test's invariant audit decide whether the
        # post-disconnect drain succeeded.
        notes.append(
            f"primary already exited (ESRCH) before kill: {exc}"
        )
        killed_at = now_utc()
    except PermissionError as exc:
        notes.append(f"kill denied (EPERM): {exc}")
    except OSError as exc:
        notes.append(f"kill OSError: {exc}")

    return DisconnectResult(
        triggered=killed_at is not None,
        primary_pid=int(resolved_pid),
        killed_at=killed_at,
        watched_for_s=clock() - started,
        signal=signal_name,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# T8 — single-worker memory-cap (cgroup) injection
# ---------------------------------------------------------------------------


# Worker-side script that locates the secondary's rootless podman
# container and constrains its ``memory.max`` cgroup. Three things make
# this non-trivial:
#
# 1. The framework's slurm wrapper builds a per-job ``/tmp/asm-<hash>``
#    podman ``--root`` AND ``--runroot``; there is no fixed system-wide
#    path to query. We glob ``/tmp/asm-*/storage`` for storage roots and
#    ``/tmp/asm-*/run`` for runtime roots; the most-recently-modified
#    pair owns the live container.
# 2. The container itself is unnamed (the framework doesn't pass
#    ``--name``); we take the FIRST id from ``podman ps -q`` against the
#    discovered storage. With one secondary per worker (the framework's
#    invariant) this is unambiguous.
# 3. The cgroup that bounds the container's memory has different paths
#    on cgroup-v1 vs cgroup-v2. We probe ``/sys/fs/cgroup/cgroup.controllers``
#    to detect v2 (presence of that file is the v2 marker). On v2 the
#    target is ``/sys/fs/cgroup<podman_cgroup_path>/memory.max``; on v1
#    it is ``/sys/fs/cgroup/memory<podman_cgroup_path>/memory.limit_in_bytes``.
#    Both paths are derived from podman's ``inspect --format
#    {{.State.CgroupPath}}``.
#
# The script writes a single line of ``key=value`` pairs to stdout so
# the helper can parse the outcome without ambiguity. Every error is
# trapped (``set +e``) and surfaced as ``ERROR=<short message>``; the
# helper turns that into a :class:`OomResult` note rather than raising.
#
# Note: the script INTERPOLATES ``memory_max`` and ``storage_glob`` ONLY
# - both shell-quoted at call-site. We do not interpolate caller input
# elsewhere; a remote shell injection by way of an unsanitized field is
# specifically guarded against.
_OOM_CGROUP_SCRIPT_TMPL: str = r"""
set +e
MEM_MAX=__MEMORY_MAX_Q__
STORAGE_GLOB=__STORAGE_GLOB_Q__

# Discover the per-job podman storage + runtime roots.
STORAGE_ROOT=""
RUN_ROOT=""
NEWEST=0
for s in $STORAGE_GLOB; do
  if [ -d "$s" ]; then
    parent=$(dirname "$s")
    r="$parent/run"
    if [ -d "$r" ]; then
      mtime=$(stat -c '%Y' "$s" 2>/dev/null || echo 0)
      if [ "$mtime" -gt "$NEWEST" ]; then
        NEWEST=$mtime
        STORAGE_ROOT="$s"
        RUN_ROOT="$r"
      fi
    fi
  fi
done
if [ -z "$STORAGE_ROOT" ]; then
  echo "ERROR=no_storage_root_found"
  echo "STORAGE_GLOB=$STORAGE_GLOB"
  exit 0
fi
echo "STORAGE_ROOT=$STORAGE_ROOT"
echo "RUN_ROOT=$RUN_ROOT"

PODMAN="podman --root $STORAGE_ROOT --runroot $RUN_ROOT"
CID=$($PODMAN ps -q 2>/dev/null | head -n1)
if [ -z "$CID" ]; then
  echo "ERROR=no_running_container"
  exit 0
fi
echo "CID=$CID"

CGROUP_PATH=$($PODMAN inspect --format '{{.State.CgroupPath}}' "$CID" 2>/dev/null)
if [ -z "$CGROUP_PATH" ]; then
  # Some podman versions populate ``CgroupParent`` but not ``CgroupPath``;
  # fall back to ``CgroupParent``/<cid>.
  PARENT=$($PODMAN inspect --format '{{.HostConfig.CgroupParent}}' "$CID" 2>/dev/null)
  if [ -n "$PARENT" ]; then
    CGROUP_PATH="$PARENT/$CID"
  fi
fi
if [ -z "$CGROUP_PATH" ]; then
  echo "ERROR=no_cgroup_path"
  exit 0
fi
echo "CGROUP_PATH=$CGROUP_PATH"

# cgroup-v2 detection: presence of /sys/fs/cgroup/cgroup.controllers.
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  TARGET=/sys/fs/cgroup${CGROUP_PATH}/memory.max
  echo "CGROUP_VERSION=2"
else
  TARGET=/sys/fs/cgroup/memory${CGROUP_PATH}/memory.limit_in_bytes
  echo "CGROUP_VERSION=1"
fi
echo "TARGET=$TARGET"

if [ ! -f "$TARGET" ]; then
  echo "ERROR=target_missing"
  exit 0
fi

if printf '%s\n' "$MEM_MAX" > "$TARGET" 2>/dev/null; then
  echo "WROTE=$MEM_MAX"
else
  echo "ERROR=write_failed"
  CURRENT=$(cat "$TARGET" 2>/dev/null)
  echo "CURRENT=$CURRENT"
fi
"""


@dataclasses.dataclass(frozen=True, slots=True)
class OomResult:
    """Outcome of an :func:`oom_one_worker_via_cgroup` call.

    ``triggered``: ``True`` iff the helper SSHed into ``target_worker``
    AND successfully wrote ``memory_max`` to the secondary's
    ``memory.max`` (cgroup-v2) / ``memory.limit_in_bytes`` (cgroup-v1)
    file. A ``False`` here means either the run dir / connection info
    never appeared in time, or one of the worker-side discovery steps
    failed (no storage root, no container, no cgroup path). The
    ``notes`` tuple carries the short error label so the test caller
    can decide whether to skip or fail.

    ``target_worker``: the worker hostname the helper drove its SSH
    against, even on failure. When the caller passed ``target_worker=
    None`` and the helper resolved one from ``connection_info/
    <secondary>.info``, that resolved hostname is what shows up here.

    ``container_id``: the rootless podman container id the helper
    constrained, or ``None`` if discovery failed. Truncated to whatever
    ``podman ps -q`` returns (typically the 12-char short id).

    ``cgroup_path``: the cgroup path string returned by ``podman
    inspect --format {{.State.CgroupPath}}``, or ``None`` if the lookup
    failed. Useful for triage: a non-None ``cgroup_path`` paired with
    ``triggered=False`` means the write itself failed (likely a
    permissions or controller-delegation issue), not container
    discovery.

    ``cgroup_version``: ``2`` for cgroup-v2, ``1`` for cgroup-v1, or
    ``None`` if the helper could not detect either. Carried so the test
    failure surface includes which cgroup ABI we exercised.

    ``applied_at``: UTC timestamp captured immediately after the worker
    script returned with a ``WROTE=`` line. ``None`` when the constrain
    step never executed.

    ``notes``: free-form per-call notes (errors during ssh, parse
    failures, the worker-side ``ERROR=...`` short label, etc.). The
    helper is intentionally fail-safe and surfaces every conceivable
    error this way rather than raising, mirroring :class:`KillResult`
    and :class:`DisconnectResult`.
    """

    triggered: bool
    target_worker: str
    container_id: str | None = None
    cgroup_path: str | None = None
    cgroup_version: int | None = None
    applied_at: datetime | None = None
    notes: tuple[str, ...] = ()


class _WorkerRunner(Protocol):
    """Minimal interface :func:`oom_one_worker_via_cgroup` needs.

    A :class:`compiler_suit_runner.tests.slurm.cluster_probe.ClusterProbe`
    satisfies this protocol via its ``worker_ssh`` method. The unit test
    passes a duck-typed stub that records the argv shape without
    actually shelling out to a worker.
    """

    def worker_ssh(  # pragma: no cover - protocol only
        self,
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


def _wait_for_run_dir(
    log_root: pathlib.Path,
    *,
    baseline: set[str],
    deadline: float,
    poll_interval_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Optional[pathlib.Path]:
    """Poll ``log_root`` for a fresh ``run_<TS>`` directory.

    Mirrors the same baseline-diff handshake T5/T6 use. Returns the
    newly-created directory on success; ``None`` on timeout. ``baseline``
    is the set of ``run_*`` names that already existed before the
    dispatch started; we wait for any name not in that set.
    """
    while clock() < deadline:
        now = {p.name for p in log_root.glob("run_*")}
        new = sorted(now - baseline)
        if new:
            return log_root / new[-1]
        sleep(poll_interval_s)
    return None


def _wait_for_secondary_info(
    run_log_dir: pathlib.Path,
    *,
    target_secondary_id: str,
    deadline: float,
    poll_interval_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Optional[str]:
    """Poll ``run_log_dir/connection_info/<id>.info`` for ``hostname=``.

    Returns the resolved worker hostname or ``None`` on timeout.
    """
    info_path = run_log_dir / "connection_info" / f"{target_secondary_id}.info"
    while clock() < deadline:
        host = _parse_info_hostname(info_path)
        if host is not None:
            return host
        sleep(poll_interval_s)
    return None


def _parse_oom_script_output(stdout: str) -> dict[str, str]:
    """Parse the worker-side OOM script's ``key=value`` line output.

    Tolerates extra whitespace and any non ``=`` line (silently
    ignored). Repeated keys are last-wins (the script does not emit
    duplicates by design, but defensive parsing is cheap).
    """
    pairs: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def oom_one_worker_via_cgroup(
    probe: _WorkerRunner,
    *,
    run_log_root: pathlib.Path,
    target_worker: str | None = None,
    target_secondary_id: str | None = None,
    memory_max: str = "512M",
    arm_after_run_dir_appears: bool = True,
    baseline_run_dirs: set[str] | None = None,
    storage_glob: str = "/tmp/asm-*/storage",
    arm_timeout_s: float = 300.0,
    info_timeout_s: float = 180.0,
    ssh_timeout_s: float = 30.0,
    poll_interval_s: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OomResult:
    """Constrain one secondary's container to ``memory_max`` mid-run.

    The helper drives a four-step path on a single target worker:

    1. (optional) wait for a fresh ``run_<TS>`` directory under
       ``run_log_root`` (the framework creates it before any secondary
       starts; baseline diff against ``baseline_run_dirs`` selects the
       NEW dir even if older runs are present);
    2. (optional) resolve ``target_worker`` from ``connection_info/
       <target_secondary_id>.info`` if the caller did not pass it
       explicitly. Either ``target_worker`` OR ``target_secondary_id``
       MUST be supplied; passing both lets the caller pin both.
    3. SSH into the resolved worker and run :data:`_OOM_CGROUP_SCRIPT_TMPL`
       to:
       a. discover the per-job rootless podman storage+runtime roots
          via ``/tmp/asm-*/storage`` glob;
       b. find the secondary's container id (``podman ps -q | head``);
       c. read its ``State.CgroupPath`` (with a ``HostConfig.CgroupParent``
          fallback);
       d. detect cgroup-v1 vs cgroup-v2 via the presence of
          ``/sys/fs/cgroup/cgroup.controllers``;
       e. write ``memory_max`` to the corresponding ``memory.max``
          (v2) or ``memory.limit_in_bytes`` (v1) file.
    4. Parse the script's stdout, mapping ``WROTE=...`` to ``triggered=True``
       and any ``ERROR=...`` to a note + ``triggered=False``.

    The kernel OOM-killer fires asynchronously once the container's
    RSS exceeds ``memory_max``; the test assertion side polls the
    secondary's slurm_*.out for the kernel's
    ``Killed process`` / ``out of memory`` markers. THIS HELPER ONLY
    INSTALLS THE CONSTRAINT — it does not wait for the kill to land.

    Parameters:

    * ``probe``: a :class:`ClusterProbe`-shaped object exposing
      ``worker_ssh``. Loose-typed via :class:`_WorkerRunner` so the unit
      test can pass a stub.
    * ``run_log_root``: the host-side log mount (matches
      :data:`run_helpers.SLURM_TEST_ENV_LOG_ROOT`).
    * ``target_worker``: explicit worker hostname (e.g.
      ``"slurm-worker3"``). When ``None``, the helper resolves it from
      the secondary's connection_info file.
    * ``target_secondary_id``: framework secondary id to resolve to a
      hostname. Ignored when ``target_worker`` is set; required when
      ``target_worker`` is ``None``.
    * ``memory_max``: value written verbatim to ``memory.max``. The
      kernel accepts ``512M``, ``536870912``, or ``max``; we shell-
      quote the value at call-site to keep the script safe under any
      future caller input.
    * ``arm_after_run_dir_appears``: when ``True`` (default) the helper
      first waits for a fresh ``run_<TS>`` directory under
      ``run_log_root``. ``False`` is for tests that already have a
      pinned ``run_log_dir`` (we then expect ``target_worker`` to be
      passed directly so info-resolution is also skipped).
    * ``baseline_run_dirs``: pre-existing ``run_*`` names; defaults to
      a fresh snapshot taken at call entry, which is correct when the
      caller invokes the helper BEFORE starting the dispatch. Tests
      that need to start the dispatch first then arm later should pass
      a snapshot taken pre-dispatch.
    * ``storage_glob``: glob for the per-job podman storage roots. The
      slurm-test-env's wrapper writes ``/tmp/asm-<hash>/storage`` so
      the default matches; override only for a future framework
      version that uses a different parent.
    * ``arm_timeout_s``: budget for the run-dir wait + info wait
      combined. The framework typically writes
      ``connection_info/<secondary>.info`` within seconds of the
      secondary onboarding; ``300`` is a generous ceiling.
    * ``info_timeout_s``: per-step budget for the info file wait
      (deducted from ``arm_timeout_s``). Useful when the test wants a
      short connection_info wait but a long run-dir wait, or vice
      versa.
    * ``ssh_timeout_s``: per-call SSH timeout for the worker-side
      script. The script itself runs in <1s under normal conditions;
      30 covers slow Podman lookups on a busy worker.
    * ``clock`` / ``sleep`` / ``now_utc``: injectable for unit tests.

    Returns an :class:`OomResult`. The helper never raises; every
    conceivable error (ssh timeout, missing storage root, missing
    container, write-permission failure) lands in ``notes``.

    CRITICAL: this helper SSHes into a worker and writes to the worker's
    cgroup filesystem. The write only succeeds if the SSH user owns
    the relevant cgroup hierarchy (rootless podman with systemd memory
    delegation). On the slurm-test-env the secondary runs under the
    same login as the gateway SSH user, so the delegation is in place;
    a future test env with a different rootless setup may need
    ``sudo`` or a different attack vector.
    """
    if not arm_after_run_dir_appears and target_worker is None:
        return OomResult(
            triggered=False,
            target_worker="",
            notes=(
                "arm_after_run_dir_appears=False requires an explicit "
                "target_worker; cannot resolve via connection_info "
                "without first locating the run dir",
            ),
        )
    if target_worker is None and target_secondary_id is None:
        return OomResult(
            triggered=False,
            target_worker="",
            notes=(
                "either target_worker or target_secondary_id must be "
                "supplied; helper cannot pick a worker on its own",
            ),
        )

    started = clock()
    deadline = started + max(arm_timeout_s, 0.0)
    notes: list[str] = []

    # Step 1: resolve the run dir if the caller did not pin it.
    run_log_dir: Optional[pathlib.Path] = None
    if arm_after_run_dir_appears:
        baseline = (
            set(baseline_run_dirs)
            if baseline_run_dirs is not None
            else {p.name for p in run_log_root.glob("run_*")}
        )
        run_log_dir = _wait_for_run_dir(
            run_log_root,
            baseline=baseline,
            deadline=deadline,
            poll_interval_s=poll_interval_s,
            clock=clock,
            sleep=sleep,
        )
        if run_log_dir is None:
            notes.append(
                "no fresh run_<TS> directory appeared under "
                f"{run_log_root!s} within {arm_timeout_s:.0f}s"
            )
            return OomResult(
                triggered=False,
                target_worker=target_worker or "",
                notes=tuple(notes),
            )

    # Step 2: resolve target_worker if the caller did not pin it. We
    # cap the info-file wait at ``info_timeout_s`` (or the remaining
    # arm budget, whichever is smaller).
    resolved_worker = target_worker
    if resolved_worker is None:
        info_deadline = min(
            clock() + max(info_timeout_s, 0.0), deadline,
        )
        assert run_log_dir is not None  # guarded above
        assert target_secondary_id is not None  # guarded above
        resolved_worker = _wait_for_secondary_info(
            run_log_dir,
            target_secondary_id=target_secondary_id,
            deadline=info_deadline,
            poll_interval_s=poll_interval_s,
            clock=clock,
            sleep=sleep,
        )
        if resolved_worker is None:
            notes.append(
                f"connection_info/{target_secondary_id}.info did not "
                f"materialise within {info_timeout_s:.0f}s under "
                f"{run_log_dir!s}; cannot resolve target worker"
            )
            return OomResult(
                triggered=False,
                target_worker="",
                notes=tuple(notes),
            )

    # Step 3: drive the worker-side script.
    import shlex as _shlex  # noqa: PLC0415 — local import keeps top clean

    # Use literal-token substitution rather than ``.format()`` because
    # the embedded shell script uses ``{`` / ``}`` syntax (Go-template
    # ``{{.State.CgroupPath}}``, shell parameter expansion ``${VAR}``)
    # that would collide with str.format's brace grammar.
    script = (
        _OOM_CGROUP_SCRIPT_TMPL
        .replace("__MEMORY_MAX_Q__", _shlex.quote(memory_max))
        .replace("__STORAGE_GLOB_Q__", _shlex.quote(storage_glob))
    )

    cgroup_path: Optional[str] = None
    cgroup_version: Optional[int] = None
    container_id: Optional[str] = None
    applied_at: Optional[datetime] = None

    try:
        cp = probe.worker_ssh(
            resolved_worker, script, timeout=ssh_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        notes.append(f"ssh worker_ssh timed out: {exc}")
        return OomResult(
            triggered=False,
            target_worker=resolved_worker,
            notes=tuple(notes),
        )
    except OSError as exc:
        notes.append(f"ssh worker_ssh OSError: {exc}")
        return OomResult(
            triggered=False,
            target_worker=resolved_worker,
            notes=tuple(notes),
        )

    pairs = _parse_oom_script_output(cp.stdout or "")
    container_id = pairs.get("CID") or None
    cgroup_path = pairs.get("CGROUP_PATH") or None
    raw_version = pairs.get("CGROUP_VERSION")
    if raw_version is not None:
        try:
            cgroup_version = int(raw_version)
        except ValueError:
            cgroup_version = None

    if cp.returncode != 0:
        notes.append(
            f"worker_ssh rc={cp.returncode}; "
            f"stderr={(cp.stderr or '').strip()[:200]!r}"
        )

    error_label = pairs.get("ERROR")
    wrote_value = pairs.get("WROTE")
    if wrote_value is not None:
        applied_at = now_utc()
    if error_label is not None:
        notes.append(f"worker reported error: {error_label}")
        # Surface auxiliary diagnostic fields if the script captured
        # them (e.g. ``CURRENT=...`` on a write failure).
        if "CURRENT" in pairs:
            notes.append(f"current memory limit: {pairs['CURRENT']}")
        if "STORAGE_GLOB" in pairs:
            notes.append(f"storage glob: {pairs['STORAGE_GLOB']}")

    return OomResult(
        triggered=applied_at is not None and error_label is None,
        target_worker=resolved_worker,
        container_id=container_id,
        cgroup_path=cgroup_path,
        cgroup_version=cgroup_version,
        applied_at=applied_at,
        notes=tuple(notes),
    )


__all__ += [
    "OomResult",
    "oom_one_worker_via_cgroup",
]


# ---------------------------------------------------------------------------
# T10 — port-bind collision injection
# ---------------------------------------------------------------------------


# Worker-side script that pre-binds a port and prints the listener PID.
#
# Implementation choice: a Python one-liner that creates a socket,
# binds, listens, and then ``os.read``s on stdin to keep the process
# alive until the controlling SSH channel sends ``SIGTERM`` /
# ``SIGKILL`` via ``release_port_grab``. We pick Python over ``nc``
# because:
#
# 1. ``nc -l`` flavours differ (BSD vs OpenBSD vs ncat); the BSD
#    variant in nixpkgs occasionally lacks ``-k`` and exits on first
#    accept, defeating the hold.
# 2. We can capture the bound process's PID from Python directly via
#    ``os.getpid()`` and emit it with a deterministic prefix that the
#    helper parses; ``nc``'s pid would have to be extracted from
#    ``$$`` echoed via the parent shell, which is fragile across
#    shells and the SSH server's choice of login shell.
# 3. SO_REUSEADDR=0 (Python's HTTPServer default is True; we
#    override with ``setsockopt(SOL_SOCKET, SO_REUSEADDR, 0)`` for
#    parity with the secondary's bind) guarantees the secondary's
#    bind WILL collide; with SO_REUSEADDR=1 the kernel might allow
#    the second bind on some kernel versions, defeating the test.
#
# The script writes ``LISTENER_PID=<pid>`` and ``LISTENER_BOUND=<port>``
# to stdout BEFORE starting the recv loop so the helper can return
# early once it sees those markers. We use ``sys.stdout.flush()``
# explicitly because Python buffers stdout when its parent is a pipe.
#
# Lifetime: the listener process is detached via ``setsid`` + double-
# fork so it survives the SSH channel close that follows once the
# helper has captured the PID. Without the detach, openssh's
# session-leader cleanup would kill the listener as soon as the SSH
# command returned, releasing the port before the dispatch even
# starts.
#
# Cleanup: the helper or the test's teardown SSHes back into the
# worker and runs ``kill -TERM <pid>`` (then ``-KILL``) using the
# captured PID; see :func:`release_port_grab`.
_PORT_GRAB_SCRIPT_TMPL: str = r"""
set +e
PORT=__PORT__
TIMEOUT_S=__TIMEOUT_S__
PIDFILE=__PIDFILE_Q__

# Prefer python3, fall back to python (the slurm-test-env image
# carries python3 by default).
PYBIN=$(command -v python3 || command -v python)
if [ -z "$PYBIN" ]; then
  echo "ERROR=no_python_on_worker"
  exit 0
fi

# Detach the listener from the SSH session so closing the channel
# does not kill it. ``setsid`` reparents to PID-1 (the cgroup leader);
# ``nohup`` plus ``> /dev/null 2>&1 < /dev/null`` keeps the FDs
# clean. The PID we publish is the listener's, NOT the shell's, so
# we read it back from $PIDFILE after the python process flushes it.
rm -f "$PIDFILE"

# The python child writes its own PID to $PIDFILE then enters a sleep
# loop bounded by $TIMEOUT_S so a forgotten release_port_grab cannot
# leak the listener forever. The default cap is generous (matches
# the test's wall-clock cap so a timeout in the dispatch never out-
# lives the listener) but keeps us from accumulating zombie
# listeners across CI runs.
nohup setsid "$PYBIN" -c "
import os, socket, sys, time
pid = os.getpid()
with open('$PIDFILE', 'w') as fh:
    fh.write(str(pid))
    fh.flush()
    os.fsync(fh.fileno())
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
# SO_REUSEADDR=0 forces the colliding bind to fail with EADDRINUSE.
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
s.bind(('0.0.0.0', $PORT))
s.listen(8)
print(f'LISTENER_PID={pid}', flush=True)
print(f'LISTENER_BOUND=$PORT', flush=True)
sys.stdout.flush()
deadline = time.monotonic() + $TIMEOUT_S
while time.monotonic() < deadline:
    time.sleep(1.0)
" > /dev/null 2>&1 < /dev/null &

# Wait for the child to write the pidfile (bounded so a startup
# failure surfaces as ERROR=pidfile_missing rather than hanging).
WAIT=0
while [ ! -s "$PIDFILE" ] && [ $WAIT -lt 50 ]; do
  WAIT=$((WAIT + 1))
  sleep 0.1
done
if [ ! -s "$PIDFILE" ]; then
  echo "ERROR=pidfile_missing"
  exit 0
fi
LISTENER_PID=$(cat "$PIDFILE")
echo "LISTENER_PID=$LISTENER_PID"
echo "LISTENER_BOUND=$PORT"
echo "PIDFILE=$PIDFILE"

# Verify the bind landed: ``ss -lntp`` should show our PID. We do
# this best-effort; a missing ss binary just leaves BIND_VERIFIED
# unset and the helper logs a softer note rather than failing.
if command -v ss >/dev/null 2>&1; then
  if ss -lntp 2>/dev/null | awk -v p="$PORT" '
      $4 ~ ":"p"$" {found=1} END {exit found?0:1}'; then
    echo "BIND_VERIFIED=1"
  else
    echo "BIND_VERIFIED=0"
  fi
fi
"""


# Worker-side cleanup script: kill the listener PID we captured.
# ``kill -0`` first probes liveness so we can report whether the
# listener was already gone (a benign cleanup state) vs whether the
# kill succeeded (the typical state) vs whether the kill failed
# (e.g. EPERM, which would indicate something else is using that
# PID and we should NOT proceed). We escalate to SIGKILL after a
# brief grace window because the python listener catches no
# signals and SIGTERM alone is sufficient, but the escalation
# guards against an interpreter wedged on shutdown.
_PORT_GRAB_RELEASE_SCRIPT_TMPL: str = r"""
set +e
PID=__PID__
PIDFILE=__PIDFILE_Q__
PORT=__PORT__

if ! kill -0 "$PID" 2>/dev/null; then
  echo "STATE=already_gone"
  rm -f "$PIDFILE"
  exit 0
fi
kill -TERM "$PID" 2>/dev/null
# Brief grace; the listener is a one-line python sleep loop, so it
# typically exits within a tick.
for i in 1 2 3 4 5; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "STATE=terminated"
    rm -f "$PIDFILE"
    exit 0
  fi
  sleep 0.2
done
kill -KILL "$PID" 2>/dev/null
for i in 1 2 3 4 5; do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "STATE=killed"
    rm -f "$PIDFILE"
    exit 0
  fi
  sleep 0.2
done
echo "STATE=stuck"
echo "PORT=$PORT"
"""


@dataclasses.dataclass(frozen=True, slots=True)
class PortGrabResult:
    """Outcome of a :func:`prebind_port_on_worker` call.

    ``bound``: ``True`` iff the helper SSHed into ``target_worker``
    AND the worker-side script published a ``LISTENER_PID=`` line
    (and a ``LISTENER_BOUND=`` line matching ``port``). A ``False``
    here means either SSH itself failed or the worker reported one
    of the script's ``ERROR=...`` short labels (e.g. python missing
    or pidfile never appeared); ``notes`` carries the diagnostic.

    ``target_worker``: the worker hostname the helper drove its SSH
    against, recorded even on failure so the test's failure surface
    surfaces which worker we tried.

    ``port``: the port the helper attempted to pre-bind. Mirrored
    from the input so the caller does not have to thread its own
    constant through.

    ``listener_pid``: the python listener's PID on the target
    worker, captured from the ``LISTENER_PID=`` line. ``None`` when
    ``bound`` is ``False``. Required input to :func:`release_port_grab`.

    ``pidfile``: the path on the target worker where the listener
    wrote its PID. The release helper deletes this file on success;
    surfaced for triage when a release fails to clear it.

    ``started_at``: UTC timestamp captured immediately after the
    worker script returned with the ``LISTENER_PID=`` line. ``None``
    when no listener ever started.

    ``bind_verified``: ``True`` iff the worker-side ``ss`` probe
    confirmed the listener was bound at the requested port.
    ``False`` means the bind landed but the verification step
    couldn't confirm it (e.g. ``ss`` missing); ``None`` means the
    verification step did not run at all (grow path: future
    framework version that drops the ``ss`` probe).

    ``notes``: free-form per-call notes (SSH errors, ``ERROR=...``
    labels surfaced by the worker script, etc.). The helper is
    intentionally fail-safe and surfaces every conceivable error
    this way rather than raising, mirroring :class:`KillResult`,
    :class:`DisconnectResult`, and :class:`OomResult`.
    """

    bound: bool
    target_worker: str
    port: int
    listener_pid: str | None = None
    pidfile: str | None = None
    started_at: datetime | None = None
    bind_verified: bool | None = None
    notes: tuple[str, ...] = ()


def _parse_port_grab_output(stdout: str) -> dict[str, str]:
    """Parse the worker-side port-grab script's ``key=value`` line output.

    Mirrors :func:`_parse_oom_script_output` exactly — extra whitespace
    and any non ``=`` line is silently ignored; repeated keys are
    last-wins (the script does not emit duplicates by design).
    """
    pairs: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def prebind_port_on_worker(
    probe: _WorkerRunner,
    *,
    target_worker: str = "slurm-worker1",
    port: int = 5050,
    pidfile: str | None = None,
    listener_timeout_s: float = 600.0,
    ssh_timeout_s: float = 30.0,
    now_utc: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PortGrabResult:
    """Pre-bind ``port`` on ``target_worker`` via a detached python listener.

    Drives a single SSH call into ``target_worker`` and runs
    :data:`_PORT_GRAB_SCRIPT_TMPL`, which:

    1. spawns a detached (``nohup setsid``) python listener bound to
       ``port`` with ``SO_REUSEADDR=0`` so the secondary's bind WILL
       collide with EADDRINUSE;
    2. writes the listener's PID to ``pidfile`` on the worker;
    3. emits ``LISTENER_PID=<pid>`` / ``LISTENER_BOUND=<port>`` /
       ``PIDFILE=<path>`` lines on stdout AND, when ``ss`` is
       available, a ``BIND_VERIFIED=1|0`` line confirming the bind.

    The helper returns AS SOON AS the listener process has flushed
    its PID; the listener itself stays alive on the worker until
    :func:`release_port_grab` SSHes back to terminate it OR until
    its internal ``listener_timeout_s`` deadline fires (whichever
    comes first). The internal timeout is the safety net against a
    forgotten ``release_port_grab`` leaking a port-holder across CI
    runs.

    Parameters:

    * ``probe``: a :class:`ClusterProbe`-shaped object exposing
      ``worker_ssh``. Loose-typed via :class:`_WorkerRunner` so the
      unit test can pass a stub.
    * ``target_worker``: hostname to SSH into. Defaults to
      ``slurm-worker1`` per the plan; tests on a cluster where
      worker1 is down should pass ``slurm-worker2``.
    * ``port``: TCP port to pre-bind. Default ``5050`` matches the
      port the smoke16 retrospective documented as the leaky one
      (``DEFAULT_CLEANUP_PORTS = (5000, 5050)``); tests probing a
      different framework version should override.
    * ``pidfile``: absolute path on the target worker where the
      listener writes its PID. ``None`` (default) picks a unique
      ``/tmp/asm-portgrab-<port>.pid`` path; pass an explicit value
      only when two parallel pre-binds need disambiguation.
    * ``listener_timeout_s``: internal lifetime cap for the worker-
      side listener (in seconds). Even if the test forgets to call
      :func:`release_port_grab`, the listener self-terminates after
      this elapses. Default ``600`` matches the T10 wall-clock cap.
    * ``ssh_timeout_s``: per-call SSH timeout for the spawn step.
      The script runs in <2s under normal conditions; ``30`` covers
      a slow-spawning worker without leaving the harness blocked
      indefinitely on a hung SSH.
    * ``now_utc``: injectable for unit tests.

    Returns a :class:`PortGrabResult`. The helper never raises; every
    conceivable error (ssh timeout, ``ERROR=...`` label, missing
    PID line) lands in :attr:`PortGrabResult.notes`.

    CRITICAL: this helper SSHes into a worker and spawns a detached
    process. The process's lifetime is bounded by ``listener_timeout_s``
    OR by an explicit :func:`release_port_grab` call. Tests MUST
    schedule the release call in their teardown (typically via a
    pytest finalizer / try-finally block) — the test's ``cleanup_cluster``
    fixture does NOT know about the listener's PID, so it cannot
    automatically clean it up; the listener_timeout_s safety net is
    the only line of defence against a forgotten release.
    """
    if pidfile is None:
        pidfile = f"/tmp/asm-portgrab-{int(port)}.pid"

    notes: list[str] = []

    # Build the worker-side script. We use literal-token substitution
    # rather than ``.format()`` because the embedded shell + python
    # script uses ``{`` / ``}`` syntax that would collide with
    # str.format's brace grammar.
    import shlex as _shlex  # noqa: PLC0415 — local import keeps top clean

    script = (
        _PORT_GRAB_SCRIPT_TMPL
        .replace("__PORT__", str(int(port)))
        .replace("__TIMEOUT_S__", str(int(max(listener_timeout_s, 0))))
        .replace("__PIDFILE_Q__", _shlex.quote(pidfile))
    )

    try:
        cp = probe.worker_ssh(
            target_worker, script, timeout=ssh_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        notes.append(f"ssh worker_ssh timed out: {exc}")
        return PortGrabResult(
            bound=False,
            target_worker=target_worker,
            port=int(port),
            pidfile=pidfile,
            notes=tuple(notes),
        )
    except OSError as exc:
        notes.append(f"ssh worker_ssh OSError: {exc}")
        return PortGrabResult(
            bound=False,
            target_worker=target_worker,
            port=int(port),
            pidfile=pidfile,
            notes=tuple(notes),
        )

    pairs = _parse_port_grab_output(cp.stdout or "")

    if cp.returncode != 0:
        notes.append(
            f"worker_ssh rc={cp.returncode}; "
            f"stderr={(cp.stderr or '').strip()[:200]!r}"
        )

    error_label = pairs.get("ERROR")
    if error_label is not None:
        notes.append(f"worker reported error: {error_label}")
        return PortGrabResult(
            bound=False,
            target_worker=target_worker,
            port=int(port),
            pidfile=pidfile,
            notes=tuple(notes),
        )

    listener_pid = pairs.get("LISTENER_PID") or None
    bound_port_str = pairs.get("LISTENER_BOUND")
    bound_port: Optional[int] = None
    if bound_port_str is not None:
        try:
            bound_port = int(bound_port_str)
        except ValueError:
            bound_port = None

    bind_verified: Optional[bool] = None
    raw_verified = pairs.get("BIND_VERIFIED")
    if raw_verified is not None:
        bind_verified = raw_verified == "1"

    if listener_pid is None or bound_port is None:
        notes.append(
            "worker did not publish LISTENER_PID and LISTENER_BOUND; "
            f"stdout tail: {(cp.stdout or '').strip()[-200:]!r}"
        )
        return PortGrabResult(
            bound=False,
            target_worker=target_worker,
            port=int(port),
            listener_pid=listener_pid,
            pidfile=pidfile,
            bind_verified=bind_verified,
            notes=tuple(notes),
        )

    if bound_port != int(port):
        notes.append(
            f"worker reported LISTENER_BOUND={bound_port} but "
            f"requested port={port}"
        )
        return PortGrabResult(
            bound=False,
            target_worker=target_worker,
            port=int(port),
            listener_pid=listener_pid,
            pidfile=pidfile,
            bind_verified=bind_verified,
            notes=tuple(notes),
        )

    return PortGrabResult(
        bound=True,
        target_worker=target_worker,
        port=int(port),
        listener_pid=listener_pid,
        pidfile=pairs.get("PIDFILE") or pidfile,
        started_at=now_utc(),
        bind_verified=bind_verified,
        notes=tuple(notes),
    )


def release_port_grab(
    probe: _WorkerRunner,
    grab: PortGrabResult,
    *,
    ssh_timeout_s: float = 30.0,
) -> tuple[bool, tuple[str, ...]]:
    """Release a listener previously installed by :func:`prebind_port_on_worker`.

    SSHes back into ``grab.target_worker`` and runs
    :data:`_PORT_GRAB_RELEASE_SCRIPT_TMPL` against ``grab.listener_pid``.
    Sends SIGTERM, waits up to ~1s for graceful shutdown, then
    escalates to SIGKILL if the listener is still alive.

    Returns ``(released, notes)`` where ``released`` is ``True`` iff
    the worker reported one of ``STATE=terminated`` /
    ``STATE=killed`` / ``STATE=already_gone``. ``STATE=stuck`` (the
    listener survived SIGKILL — usually means the PID was reused
    by something else) leaves ``released=False``; the caller's
    teardown should escalate to a manual investigation.

    The helper is fail-safe: a non-bound ``grab`` (i.e.
    ``grab.bound=False`` or ``grab.listener_pid=None``) returns
    ``(True, ())`` immediately because there is nothing to release.
    Per project policy nothing here ever raises; any subprocess
    exception is folded into the returned ``notes`` tuple.

    Tests typically wire this via ``try/finally`` so the release
    runs on both pass and fail paths:

    .. code-block:: python

        grab = prebind_port_on_worker(probe, target_worker="slurm-worker1")
        try:
            ...  # run the dispatch
        finally:
            release_port_grab(probe, grab)
    """
    if not grab.bound or grab.listener_pid is None:
        return True, ()

    notes: list[str] = []

    pidfile = grab.pidfile or f"/tmp/asm-portgrab-{int(grab.port)}.pid"

    import shlex as _shlex  # noqa: PLC0415 — local import keeps top clean

    script = (
        _PORT_GRAB_RELEASE_SCRIPT_TMPL
        .replace("__PID__", _shlex.quote(grab.listener_pid))
        .replace("__PIDFILE_Q__", _shlex.quote(pidfile))
        .replace("__PORT__", str(int(grab.port)))
    )

    try:
        cp = probe.worker_ssh(
            grab.target_worker, script, timeout=ssh_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        notes.append(f"release ssh worker_ssh timed out: {exc}")
        return False, tuple(notes)
    except OSError as exc:
        notes.append(f"release ssh worker_ssh OSError: {exc}")
        return False, tuple(notes)

    if cp.returncode != 0:
        notes.append(
            f"release worker_ssh rc={cp.returncode}; "
            f"stderr={(cp.stderr or '').strip()[:200]!r}"
        )

    pairs = _parse_port_grab_output(cp.stdout or "")
    state = pairs.get("STATE")
    if state in ("terminated", "killed", "already_gone"):
        return True, tuple(notes)
    notes.append(
        f"listener pid={grab.listener_pid!r} did not release "
        f"(state={state!r}); stdout tail: "
        f"{(cp.stdout or '').strip()[-200:]!r}"
    )
    return False, tuple(notes)


__all__ += [
    "PortGrabResult",
    "prebind_port_on_worker",
    "release_port_grab",
]

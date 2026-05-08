"""File-only and SSH-driven per-run invariant checks for SLURM smoke runs.

This module consumes the locally-mounted run-log tree at
``/home/sirati/.local/state/slurm-test-env/ds-test/home/sirati/slurm/log/run_<TS>/``
to validate each smoke run end-to-end. Two tiers of checks are
implemented:

* **File-only (1-4)** — parse ``slurm_<jobid>.{out,err}``, ``manifests/``
  and ``build-failures/``. No SSH, no live cluster needed.
* **Cluster (5-7)** — SSH-driven probes via :class:`ClusterProbe` to
  confirm the cluster does not retain any state from the run after the
  job exits (no stray podman containers, no listener leaks on
  harmonia/peer_push ports, no orphaned PPID=1 processes from this
  run's window).

The cluster checks short-circuit gracefully when SLURM still has jobs
queued (returning a "still-running" status) and when the gateway is
unreachable (returning a "skip - live cluster unavailable" status with
``passed=False`` so CI callers can decide error-vs-warn).

The module is also runnable as ``python -m
compiler_suit_runner.tests.slurm.invariants <run_dir>`` as a
post-flight check (exits 0 if all pass, 1 otherwise). Pass
``--cluster --workers slurm-worker1,...`` to also run checks 5-7.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only used for typing
    from compiler_suit_runner.tests.slurm.cluster_probe import (
        ClusterProbe,
        ListenerRow,
        PodmanRow,
        ProcessRow,
    )

__all__ = [
    "InvariantResult",
    "RunArtifacts",
    "check_build_failures",
    "check_clean_exit",
    "check_manifest_count_matches",
    "check_no_bind_errors",
    "check_no_leaked_containers",
    "check_no_leaked_listener_ports",
    "check_no_leaked_processes",
    "run_all_invariants",
    "run_cluster_invariants",
    "run_file_invariants",
    "wait_squeue_empty",
]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Each line of the structured Rust log carries ANSI escape codes around
# field markers, e.g.
#     ... task completed [3msecondary[0m=secondary-0 ...
# Strip ANSI before regex-matching so the patterns stay readable.
_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")

# We require BOTH markers in the file (the literal last line is the
# job-cleanup banner, not the success markers).
_CLEAN_EXIT_MARKERS: tuple[str, ...] = (
    "secondary finished successfully",
    "Container exited with code: 0",
)

_BIND_ERROR_RE: re.Pattern[str] = re.compile(
    r"Address already in use|EADDRINUSE"
)

# Match `task completed ... task_type=Some("variant") ... success=true`.
# The structured logger emits keys as `task_type=Some("variant")` (Rust
# `Option<&str>`); we accept the bare `task_type=variant` form too in case
# a future framework version drops the `Some(...)` wrapper.
_VARIANT_COMPLETED_RE: re.Pattern[str] = re.compile(
    r"task completed.*?task_type=(?:Some\(\"variant\"\)|variant)"
    r".*?success=true"
)

# ``run_<YYYYMMDD>_<HHMMSS>`` (framework emits underscores, see
# ``dynamic_runner.packaging.pipeline._make_run_id``). The trailing
# group is the timestamp portion we parse.
_RUN_ID_RE: re.Pattern[str] = re.compile(
    r"^run_(\d{8})_(\d{6})$"
)

# ``[D-]HH:MM:SS`` from ``ps -o etime``. Days separator is ``-``; the
# HH may be one or two digits depending on whether ps decided to
# zero-pad.
_ETIME_RE: re.Pattern[str] = re.compile(
    r"^(?:(?P<days>\d+)-)?"
    r"(?:(?P<hours>\d{1,2}):)?"
    r"(?P<mins>\d{1,2}):(?P<secs>\d{2})$"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunArtifacts:
    """Filesystem paths inside one run-log directory.

    The directory layout matches the dynamic_runner SLURM container's
    bind-mount: ``run_<TS>/`` containing ``slurm_<jobid>.{out,err}``,
    ``manifests/``, ``build-failures/``, etc.

    ``run_id`` and ``started_at`` are derived from the directory name
    (``run_YYYYMMDD_HHMMSS``); see
    :meth:`from_dir` for the parsing entry-point. Constructing the
    dataclass directly with only ``run_dir`` will leave the derived
    fields at their default sentinel values (empty string / ``None``),
    which keeps the file-only checks backwards-compatible.
    """

    run_dir: Path
    run_id: str = ""
    started_at: datetime | None = None
    shared_fs: Path | None = None
    """Optional ``--shared-fs`` root passed to the dispatch.

    When set, :attr:`manifests_dir` resolves to ``shared_fs/manifests/``
    (where the framework actually writes manifests). When ``None``, the
    legacy ``run_dir/manifests/`` is used to keep older callers working.
    """

    @classmethod
    def from_dir(
        cls, run_dir: Path, *, shared_fs: Path | None = None,
    ) -> RunArtifacts:
        """Build a :class:`RunArtifacts` from a ``run_<TS>`` directory.

        ``run_id`` is the directory name verbatim. ``started_at`` is
        parsed from the timestamp embedded in the name, or left as
        ``None`` if the name does not match the framework's
        ``run_YYYYMMDD_HHMMSS`` template. ``shared_fs`` is forwarded to
        the dataclass so :attr:`manifests_dir` can resolve correctly.
        """
        name = run_dir.name
        m = _RUN_ID_RE.match(name)
        if m is None:
            return cls(
                run_dir=run_dir, run_id=name, started_at=None,
                shared_fs=shared_fs,
            )
        date_s, time_s = m.group(1), m.group(2)
        try:
            ts = datetime.strptime(
                f"{date_s}_{time_s}", "%Y%m%d_%H%M%S",
            )
        except ValueError:
            ts = None
        return cls(
            run_dir=run_dir, run_id=name, started_at=ts,
            shared_fs=shared_fs,
        )

    @property
    def manifests_dir(self) -> Path:
        if self.shared_fs is not None:
            return self.shared_fs / "manifests"
        return self.run_dir / "manifests"

    @property
    def build_failures_dir(self) -> Path:
        return self.run_dir / "build-failures"

    def slurm_out_files(self) -> list[Path]:
        """All ``slurm_<jobid>.out`` files in ``run_dir`` (sorted)."""
        return sorted(self.run_dir.glob("slurm_*.out"))

    def slurm_err_files(self) -> list[Path]:
        """All ``slurm_<jobid>.err`` files in ``run_dir`` (sorted)."""
        return sorted(self.run_dir.glob("slurm_*.err"))


@dataclass(frozen=True)
class InvariantResult:
    """Outcome of a single invariant check.

    ``detail`` is a short, human-readable description of the failure
    (or a confirmation when ``passed`` is True). On failure, the detail
    quotes the offending line or names the missing artefact so the
    caller can act on it without re-reading the logs.

    The cluster checks (5-7) additionally use ``status`` to distinguish
    a true pass/fail from a soft-skip (cluster unreachable, jobs still
    running). Status values: ``"pass"``, ``"fail"``, ``"skip"``,
    ``"still-running"``. ``passed`` is ``True`` only for ``"pass"``.
    """

    name: str
    passed: bool
    detail: str = ""
    status: str = field(default="")
    rows: tuple[object, ...] = field(default_factory=tuple)

    def __str__(self) -> str:  # pragma: no cover - trivial formatter
        if self.status and self.status not in {"pass", "fail"}:
            tag = self.status.upper()
        else:
            tag = "PASS" if self.passed else "FAIL"
        if self.detail:
            return f"[{tag}] {self.name}: {self.detail}"
        return f"[{tag}] {self.name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _read_text(path: Path) -> str:
    """Read a file, stripping ANSI escape sequences.

    Returns an empty string for an unreadable file; the calling
    invariant decides whether that is itself a failure.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _strip_ansi(raw)


def _count_dir_entries(path: Path) -> int:
    """Count direct children of ``path`` (files and subdirs).

    Returns 0 if the directory is missing - the parser is defined to
    handle empty/missing dirs gracefully per the harness contract.
    """
    if not path.is_dir():
        return 0
    return sum(1 for _ in path.iterdir())


def _count_variant_manifests(path: Path) -> int:
    """Count variant-manifest JSON files in ``path``.

    Excludes the run-level ``_meta.json`` and any ``toolchain_*.json``
    sidecars - only files representing one *built variant* count toward
    the manifest invariant. Returns 0 if the directory is missing.
    """
    if not path.is_dir():
        return 0
    return sum(
        1
        for entry in path.iterdir()
        if entry.is_file()
        and entry.suffix == ".json"
        and not entry.name.startswith("_")
        and not entry.name.startswith("toolchain_")
    )


def _parse_etime_seconds(etime: str) -> int | None:
    """Convert a ``ps -o etime`` value to seconds.

    Accepts ``MM:SS``, ``HH:MM:SS`` and ``D-HH:MM:SS``. Returns
    ``None`` if the input does not match (we then conservatively treat
    the row as in-window so a parse glitch does not mask a real leak).
    """
    m = _ETIME_RE.match(etime.strip())
    if m is None:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    mins = int(m.group("mins"))
    secs = int(m.group("secs"))
    return ((days * 24 + hours) * 60 + mins) * 60 + secs


def _container_matches_run(row: PodmanRow, run_id: str) -> bool:
    """Decide whether a ``PodmanRow`` belongs to ``run_id``.

    The framework's SLURM script (see
    ``dynamic_runner/packaging/job_manager.py`` ``generate_wrapper_script``)
    does not currently set a ``run_id`` label or use ``--name`` with the
    run id; the random ``/tmp/asm-<rnd>`` directory is the only
    per-job identifier baked into the wrapper. To stay forward-
    compatible with a framework patch that adds either, we accept
    BOTH:

    * ``labels["run_id"] == run_id`` (preferred, future-proof);
    * ``run_id`` substring in the container ``name``.

    Callers fall back to a started-at-window match when neither
    matches; see :func:`check_no_leaked_containers`.
    """
    if not run_id:
        return False
    labels = getattr(row, "labels", {}) or {}
    if labels.get("run_id") == run_id:
        return True
    name = getattr(row, "name", "") or ""
    return run_id in name


# ---------------------------------------------------------------------------
# Individual invariants - file-only (1-4)
# ---------------------------------------------------------------------------


def check_clean_exit(artifacts: RunArtifacts) -> InvariantResult:
    """Invariant 1: every ``slurm_*.out`` carries the success markers."""
    name = "clean_exit"
    out_files = artifacts.slurm_out_files()
    if not out_files:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"no slurm_*.out files under {artifacts.run_dir}",
            status="fail",
        )

    failures: list[str] = []
    for path in out_files:
        text = _read_text(path)
        if not text:
            failures.append(f"{path.name} is empty or unreadable")
            continue
        missing = [m for m in _CLEAN_EXIT_MARKERS if m not in text]
        if missing:
            failures.append(
                f"{path.name} missing markers: {missing!r}"
            )

    if failures:
        return InvariantResult(
            name=name,
            passed=False,
            detail="; ".join(failures),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{len(out_files)} slurm_*.out file(s) clean",
        status="pass",
    )


def check_no_bind_errors(artifacts: RunArtifacts) -> InvariantResult:
    """Invariant 2: zero ``Address already in use``/``EADDRINUSE`` matches."""
    name = "no_bind_errors"
    err_files = artifacts.slurm_err_files()
    if not err_files:
        # Absent .err is not itself a bind error - skip cleanly. A
        # missing .out is an exit-marker failure instead.
        return InvariantResult(
            name=name,
            passed=True,
            detail=f"no slurm_*.err files under {artifacts.run_dir}",
            status="pass",
        )

    offenders: list[str] = []
    for path in err_files:
        text = _read_text(path)
        for line in text.splitlines():
            if _BIND_ERROR_RE.search(line):
                offenders.append(f"{path.name}: {line.strip()!r}")

    if offenders:
        return InvariantResult(
            name=name,
            passed=False,
            detail="; ".join(offenders),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{len(err_files)} slurm_*.err file(s) clean",
        status="pass",
    )


def check_manifest_count_matches(
    artifacts: RunArtifacts,
) -> InvariantResult:
    """Invariant 3: ``len(manifests/)`` equals completed-variant count."""
    name = "manifest_count_matches"
    manifest_count = _count_variant_manifests(artifacts.manifests_dir)

    completed = 0
    for path in artifacts.slurm_out_files():
        text = _read_text(path)
        completed += len(_VARIANT_COMPLETED_RE.findall(text))

    if manifest_count != completed:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"manifests/ has {manifest_count} entries but "
                f"slurm_*.out reports {completed} completed variant(s)"
            ),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"{manifest_count} manifest(s) match completed variants",
        status="pass",
    )


def check_build_failures(
    artifacts: RunArtifacts, expected_count: int
) -> InvariantResult:
    """Invariant 4: ``build-failures/`` matches caller's expected count.

    For clean-path tests pass ``expected_count=0``. For failure-
    injection tests the caller knows how many failures it injected.
    """
    name = "build_failures"
    if expected_count < 0:
        raise ValueError(
            f"expected_count must be >= 0, got {expected_count}"
        )
    actual = _count_dir_entries(artifacts.build_failures_dir)
    if actual != expected_count:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"build-failures/ has {actual} entries but "
                f"expected {expected_count}"
            ),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"build-failures/ has {actual} entries (matches expected)"
        ),
        status="pass",
    )


# ---------------------------------------------------------------------------
# Individual invariants - cluster-driven (5-7)
# ---------------------------------------------------------------------------


def _skip_unreachable(name: str) -> InvariantResult:
    return InvariantResult(
        name=name,
        passed=False,
        detail="live cluster unavailable",
        status="skip",
    )


def check_no_leaked_containers(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
) -> InvariantResult:
    """Invariant 5: ``podman ps -a`` returns no rows tagged with this run.

    A row is "tagged with this run" when *either* its labels carry
    ``run_id == artifacts.run_id`` OR its name contains the run id; see
    :func:`_container_matches_run`. The framework today emits neither
    explicitly, so this check ALSO accepts a started-at fallback: a
    container whose ``StartedAt`` is at or after ``artifacts.started_at``
    is treated as in-window and reported as a leak. Callers can opt out
    of the fallback by passing a :class:`RunArtifacts` whose
    ``started_at`` is ``None`` (the default for the bare-constructor
    path), in which case only the explicit-tag rule applies.
    """
    name = "no_leaked_containers"
    if not probe.is_reachable():
        return _skip_unreachable(name)

    leaked_rows: list[PodmanRow] = []
    descriptions: list[str] = []

    started_at = artifacts.started_at

    for worker in workers:
        for row in probe.podman_ps(worker):
            tagged = _container_matches_run(row, artifacts.run_id)
            in_window = False
            if not tagged and started_at is not None:
                # ``StartedAt`` may be empty (created but never run) -
                # we trust podman's RFC3339 ``2026-05-08T...`` form
                # and silently ignore unparseable values.
                in_window = _is_after(row.started_at, started_at)
            if tagged or in_window:
                leaked_rows.append(row)
                descriptions.append(
                    f"{worker}:{row.name or row.id[:12]} "
                    f"(state={row.state}, started_at={row.started_at!r})"
                )

    if leaked_rows:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"{len(leaked_rows)} leaked container(s): "
            + "; ".join(descriptions),
            status="fail",
            rows=tuple(leaked_rows),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=f"no leaked containers across {len(workers)} worker(s)",
        status="pass",
    )


def check_no_leaked_listener_ports(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
    ports: list[int] | None = None,
) -> InvariantResult:
    """Invariant 6: ``ss -lntp`` shows no listener on harmonia/peer_push
    ports bound by the run's UID.

    Defaults: ports 5000 (harmonia) and 5050 (peer_push) per
    smoke16-class leak postmortem. The check fetches the test runner's
    UID from the cluster (probe convention: same SSH user as the
    gateway login) and flags any listener whose ``uid`` matches AND
    whose port is in the watch-list.

    Listeners with no PID/UID surfaced (``ss`` may emit such rows for
    kernel-level listeners) are NOT flagged - they cannot belong to
    the test run.
    """
    name = "no_leaked_listener_ports"
    if ports is None:
        ports = [5000, 5050]
    if not probe.is_reachable():
        return _skip_unreachable(name)

    runner_uid = _gateway_uid(probe)
    leaked_rows: list[ListenerRow] = []
    descriptions: list[str] = []

    for worker in workers:
        for row in probe.port_listeners(worker, ports):
            if row.uid is None or runner_uid is None:
                # Without a UID we cannot attribute the listener; skip
                # rather than blame an unrelated process.
                continue
            if row.uid != runner_uid:
                continue
            leaked_rows.append(row)
            descriptions.append(
                f"{worker}:{row.local_port} "
                f"({row.process or '<unknown>'} pid={row.pid})"
            )

    if leaked_rows:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"{len(leaked_rows)} leaked listener(s): "
            + "; ".join(descriptions),
            status="fail",
            rows=tuple(leaked_rows),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"no listeners on {ports} owned by uid={runner_uid} "
            f"across {len(workers)} worker(s)"
        ),
        status="pass",
    )


def check_no_leaked_processes(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
    pattern: str = r"compiler_suit_runner|harmonia-cache|peer_push",
) -> InvariantResult:
    """Invariant 7: no PPID=1 ``compiler_suit_runner|harmonia-cache|peer_push``
    processes whose ``etime`` puts them inside this run's window.

    The check requires :attr:`RunArtifacts.started_at` to be populated
    (i.e. ``RunArtifacts.from_dir`` was used). Without a window
    boundary we cannot attribute orphaned processes to *this* run vs
    a previous one, so the check fails open with a clear "missing
    started_at" detail (treated as ``status="skip"``).
    """
    name = "no_leaked_processes"
    if not probe.is_reachable():
        return _skip_unreachable(name)

    if artifacts.started_at is None:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                "run window unknown (artifacts.started_at is None) - "
                "cannot determine which processes belong to this run"
            ),
            status="skip",
        )

    now = datetime.now()
    window_seconds = max(0, int((now - artifacts.started_at).total_seconds()))
    # Add a small slack so a process that started right at the run
    # boundary is still attributed to this run; clock skew between
    # the harness host and the worker is the dominant error source.
    window_seconds += 30

    leaked_rows: list[ProcessRow] = []
    descriptions: list[str] = []

    for worker in workers:
        for row in probe.processes_by_pattern(worker, pattern):
            if row.ppid != 1:
                continue
            etime_s = _parse_etime_seconds(row.etime)
            # Unparseable etime -> treat as in-window (conservative).
            if etime_s is not None and etime_s > window_seconds:
                continue
            leaked_rows.append(row)
            descriptions.append(
                f"{worker}:pid={row.pid} ({row.user}) "
                f"etime={row.etime} cmd={row.cmd[:60]!r}"
            )

    if leaked_rows:
        return InvariantResult(
            name=name,
            passed=False,
            detail=f"{len(leaked_rows)} leaked PPID=1 process(es): "
            + "; ".join(descriptions),
            status="fail",
            rows=tuple(leaked_rows),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"no leaked PPID=1 processes matching {pattern!r} "
            f"across {len(workers)} worker(s) (window={window_seconds}s)"
        ),
        status="pass",
    )


# ---------------------------------------------------------------------------
# Cluster-side helpers
# ---------------------------------------------------------------------------


def _gateway_uid(probe: ClusterProbe) -> int | None:
    """Run ``id -u`` on the gateway and parse the integer result.

    Used by :func:`check_no_leaked_listener_ports` to attribute
    listener rows to the test runner's UID. Returns ``None`` if the
    SSH call fails or the output is not an integer.
    """
    try:
        cp = probe.gateway_ssh("id -u", timeout=10.0)
    except Exception:  # noqa: BLE001 - any subprocess error -> unknown UID
        return None
    if cp.returncode != 0:
        return None
    text = cp.stdout.strip()
    try:
        return int(text)
    except ValueError:
        return None


def _is_after(rfc3339_ts: str, boundary: datetime) -> bool:
    """Return ``True`` if ``rfc3339_ts`` is at or after ``boundary``.

    Tolerates podman's ``2026-05-08T16:31:42.123456789Z`` form (nanos
    truncated to micros) and the alternative ``+00:00`` offset. An
    unparseable input returns ``True`` (conservative: treat as
    in-window so a parse glitch does not mask a leak).
    """
    if not rfc3339_ts:
        return False
    s = rfc3339_ts.strip()
    # Strip trailing Z and any sub-second precision beyond microseconds.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        # Tail looks like ``123456789+00:00`` - keep up to 6 digits of
        # subsecs and re-attach the timezone suffix.
        digits = ""
        rest = tail
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[len(digits):]
                break
        else:
            rest = ""
        s = f"{head}.{digits[:6]}{rest}"
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return True
    # Strip tzinfo for comparison if boundary is naive (the framework
    # emits naive datetimes - run_id stamps are local-time).
    if ts.tzinfo is not None and boundary.tzinfo is None:
        ts = ts.replace(tzinfo=None)
    elif ts.tzinfo is None and boundary.tzinfo is not None:
        boundary = boundary.replace(tzinfo=None)
    return ts >= boundary


def wait_squeue_empty(
    probe: ClusterProbe, *, timeout_s: float = 60.0,
    poll_interval_s: float = 2.0,
) -> bool:
    """Poll ``squeue --me`` until the runner has no jobs queued or running.

    Returns ``True`` once squeue is empty and ``False`` if ``timeout_s``
    elapses with jobs still present. Used by
    :func:`run_cluster_invariants` to gate the leak checks on a
    quiesced cluster - running checks 5-7 with active jobs would
    flag in-flight resources as leaks.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if probe.squeue_me() == []:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Top-level runners
# ---------------------------------------------------------------------------


def run_file_invariants(
    artifacts: RunArtifacts, expected_failure_count: int = 0
) -> list[InvariantResult]:
    """Run the four file-only invariants and return their results.

    Results are returned in deterministic order regardless of which
    one(s) failed - the caller can iterate or filter as it pleases.
    """
    return [
        check_clean_exit(artifacts),
        check_no_bind_errors(artifacts),
        check_manifest_count_matches(artifacts),
        check_build_failures(artifacts, expected_failure_count),
    ]


def run_cluster_invariants(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
    *,
    squeue_timeout_s: float = 60.0,
) -> list[InvariantResult]:
    """Run the three cluster-driven invariants (5, 6, 7).

    Order of operations:

    1. Quick :meth:`ClusterProbe.is_reachable` gate. If down, all three
       checks return a ``"skip"`` result (``passed=False``).
    2. :func:`wait_squeue_empty` poll. If still-running at deadline,
       all three checks return a ``"still-running"`` result
       (``passed=False``).
    3. Otherwise, dispatch checks 5, 6, 7 in order.
    """
    if not probe.is_reachable():
        return [
            _skip_unreachable("no_leaked_containers"),
            _skip_unreachable("no_leaked_listener_ports"),
            _skip_unreachable("no_leaked_processes"),
        ]

    if not wait_squeue_empty(probe, timeout_s=squeue_timeout_s):
        still = lambda n: InvariantResult(  # noqa: E731 - tiny local helper
            name=n,
            passed=False,
            detail=(
                f"squeue --me still has jobs after {squeue_timeout_s:.0f}s; "
                "skipping leak checks"
            ),
            status="still-running",
        )
        return [
            still("no_leaked_containers"),
            still("no_leaked_listener_ports"),
            still("no_leaked_processes"),
        ]

    return [
        check_no_leaked_containers(artifacts, probe, workers),
        check_no_leaked_listener_ports(artifacts, probe, workers),
        check_no_leaked_processes(artifacts, probe, workers),
    ]


def run_all_invariants(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
    *,
    expected_failure_count: int = 0,
    squeue_timeout_s: float = 60.0,
) -> list[InvariantResult]:
    """Run file-only (1-4) followed by cluster (5-7). Returns all 7
    results in numerical order regardless of which failed."""
    return run_file_invariants(
        artifacts, expected_failure_count=expected_failure_count,
    ) + run_cluster_invariants(
        artifacts, probe, workers, squeue_timeout_s=squeue_timeout_s,
    )


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def _format_results(results: list[InvariantResult]) -> str:
    return "\n".join(str(r) for r in results)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m compiler_suit_runner.tests.slurm.invariants",
        description=(
            "Run per-run SLURM invariant checks against a run-log "
            "directory. By default only the file-only checks (1-4) "
            "run; pass --cluster to also run the SSH-driven checks "
            "(5-7)."
        ),
    )
    parser.add_argument(
        "run_dir",
        type=Path,
        help="Path to the run_<TS> directory under the slurm log mount",
    )
    parser.add_argument(
        "--expected-failures",
        type=int,
        default=0,
        help=(
            "Expected entry count under build-failures/ (default 0 "
            "for clean-path tests)"
        ),
    )
    parser.add_argument(
        "--cluster",
        action="store_true",
        help=(
            "Also run cluster checks 5-7. Requires a reachable "
            "gateway and at least one --workers entry."
        ),
    )
    parser.add_argument(
        "--workers",
        type=str,
        default="",
        help=(
            "Comma-separated worker hostnames (e.g. "
            "'slurm-worker1,slurm-worker2') for the cluster checks"
        ),
    )
    parser.add_argument(
        "--squeue-timeout",
        type=float,
        default=60.0,
        help=(
            "Seconds to wait for squeue --me to drain before running "
            "cluster checks (default 60)"
        ),
    )
    return parser


def _main(argv: list[str]) -> int:
    # Backwards-compatible legacy positional CLI: ``invariants <run_dir>
    # [expected_failure_count]``. argparse handles the modern form
    # below; keep this branch so existing callers don't break.
    if (
        len(argv) >= 2
        and argv[1] not in {"-h", "--help"}
        and not any(a.startswith("--") for a in argv[2:])
        and len(argv) <= 3
    ):
        # Legacy path
        run_dir = Path(argv[1])
        try:
            expected = int(argv[2]) if len(argv) > 2 else 0
        except ValueError:
            expected = 0
        if not run_dir.is_dir():
            print(
                f"error: run_dir does not exist or is not a directory: {run_dir}",
                file=sys.stderr,
            )
            return 2
        artifacts = RunArtifacts.from_dir(run_dir)
        results = run_file_invariants(
            artifacts, expected_failure_count=expected,
        )
        print(_format_results(results))
        return 0 if all(r.passed for r in results) else 1

    parser = _build_arg_parser()
    args = parser.parse_args(argv[1:])

    target_dir: Path = args.run_dir
    if not target_dir.is_dir():
        print(
            f"error: run_dir does not exist or is not a directory: {target_dir}",
            file=sys.stderr,
        )
        return 2

    artifacts = RunArtifacts.from_dir(target_dir)
    results = run_file_invariants(
        artifacts, expected_failure_count=args.expected_failures,
    )

    if args.cluster:
        # Local import keeps the file-only path free of the SSH
        # dependency (which itself only needs stdlib subprocess, but
        # the import keeps the layering visible).
        from compiler_suit_runner.tests.slurm.cluster_probe import (
            ClusterProbe,
        )

        workers = [w.strip() for w in args.workers.split(",") if w.strip()]
        if not workers:
            print(
                "error: --cluster requires --workers <comma-separated list>",
                file=sys.stderr,
            )
            return 2
        probe = ClusterProbe()
        results.extend(
            run_cluster_invariants(
                artifacts,
                probe,
                workers,
                squeue_timeout_s=args.squeue_timeout,
            ),
        )

    print(_format_results(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(_main(sys.argv))

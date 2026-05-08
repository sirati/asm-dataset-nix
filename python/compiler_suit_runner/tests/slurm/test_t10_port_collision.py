"""End-to-end T10 reproducer: pre-bind port 5050 on a worker.

One secondary, tiny workload, no broken toolchain. BEFORE the
dispatch starts, a sidecar arming step SSHes into ``slurm-worker1``
(or ``slurm-worker2`` if worker1 is down) and spawns a detached
python listener bound to port 5050 with ``SO_REUSEADDR=0``. The
secondary's container then tries to bind that same port (the
peer_push / harmonia listener family identified in the smoke16
retrospective) and SHOULD fail fast with EADDRINUSE.

The expected framework behaviour is documented in the test plan
("Test matrix" row T10 + "Failure-injection mechanics" + sub-sub-task
plays "C3.5 T10 (port-bind collision)" T10-α/β):

* the secondary's slurm_*.err carries ``Address already in use`` /
  ``EADDRINUSE`` from the bind site (the peer_push HTTPServer or
  harmonia listener — whichever the framework binds first);
* the dispatch exits non-zero (the framework propagates the
  secondary's bind failure to the local primary);
* the secondary's wrapper script DOES clean up the half-started
  container/processes despite the failure — invariants 5/6/7 must
  pass. ANY leak here is exactly the smoke16-class bug the plan is
  trying to surface and is the KEY assertion of T10.

The 7-invariant audit is intentionally CUSTOMISED for T10:

* invariant 1 (clean exit): SKIPPED — the secondary is expected to
  fail before reaching the success markers;
* invariant 2 (no bind errors): EXPECTED to find at least one
  ``Address already in use`` match (the WHOLE point of the
  injection); we therefore assert ``count >= 1`` as a positive
  check that the injection actually landed, rather than the
  standard "must be zero" check;
* invariants 3 (manifest count) and 4 (build-failures): SKIPPED —
  no variants complete because the secondary fails before its
  build phase;
* invariants 5 / 6 / 7 (no leaked containers / listener ports /
  PPID=1 processes): the KEY assertions. Invariant 6 is filtered
  to exclude the listener WE pre-bound (matching by listener PID,
  with port-only fallback) so our own grabbed listener does not
  trip the leak detector.

Per project memory (``feedback_ssh_debug_key.md`` /
``feedback_scancel_scope.md``):

* the slurm-test-env SSH key is ephemeral and is passed via ``-i``
  on every probe (never via ``ssh-agent`` / ``~/.ssh/``);
* the cleanup harness (run by the ``cleanup_cluster`` fixture)
  scopes scancel to ``--jobname=asm-secondary-*``; T10 itself does
  not issue a scancel.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
import subprocess
import sys
from typing import Callable, Optional

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    ClusterProbe,
    GatewayConfig,
    ListenerRow,
)
from compiler_suit_runner.tests.slurm.invariants import (
    InvariantResult,
    RunArtifacts,
    check_no_leaked_containers,
    check_no_leaked_processes,
    wait_squeue_empty,
)
from compiler_suit_runner.tests.slurm.reproducers.inject_failures import (
    PortGrabResult,
    prebind_port_on_worker,
    release_port_grab,
)
from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_LOG_ROOT,
    RunResult,
    default_invocation_for_smoke,
    resolve_log_dir,
)


# Path to the live test-env SSH key. Per project memory the key is
# ephemeral: never added to ssh-agent or ``~/.ssh/``; always passed
# via ``-i``.
LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"

# All four worker hostnames in the local slurm-test-env. Invariants
# 5-7 walk this list to look for leaks; T10 cares about leaks on the
# pre-bound worker AS WELL AS on any other worker that may have been
# transiently used by the framework before the bind error surfaced.
WORKERS: list[str] = [
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
]

# Default wall-clock cap. T10 fails fast (the secondary's bind error
# surfaces during startup, before the build phase); 300s is a generous
# ceiling. Override via ``T10_TIMEOUT_S=<seconds>`` for slow CI.
DEFAULT_TIMEOUT_S = 300.0

# Pre-bind target. The plan calls T10 against ``slurm-worker1``;
# if that worker is down we fall back to ``slurm-worker2``.
PREFERRED_TARGET_WORKER = "slurm-worker1"
FALLBACK_TARGET_WORKER = "slurm-worker2"

# Pre-bind port. Per the plan this is the harmonia/peer_push port
# family identified in the smoke16 retrospective; the cleanup
# harness's ``DEFAULT_CLEANUP_PORTS = (5000, 5050)`` matches.
PORT_GRAB_PORT = 5050

# Number of secondaries this row dispatches. One is the minimum and
# matches the plan's ``--jobs 1`` recipe; T10 is N=1 by design so the
# pre-bind on a SINGLE worker is sufficient to collide with the only
# secondary's bind.
N_SECONDARIES = 1


_ANSI_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*m")
_BIND_ERROR_RE: re.Pattern[str] = re.compile(
    r"Address already in use|EADDRINUSE",
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _live_probe() -> ClusterProbe:
    """Build a :class:`ClusterProbe` with the explicit identity file.

    Same rationale as the other live tests in this slice: the conftest
    fixture's probe is constructed without a key; we instantiate
    locally so SSH probes work via the explicit ``-i`` key as project
    policy demands.
    """
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )


def _resolve_timeout() -> float:
    """Read ``T10_TIMEOUT_S`` from the environment, falling back to default."""
    raw = os.environ.get("T10_TIMEOUT_S")
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _format_results(results: list[InvariantResult]) -> str:
    """Render every invariant result for a failed assertion message."""
    lines: list[str] = []
    for r in results:
        tag = r.status.upper() if r.status else ("PASS" if r.passed else "FAIL")
        row_suffix = f" rows={len(r.rows)}" if r.rows else ""
        detail = r.detail or "(no detail)"
        lines.append(f"  [{tag}] {r.name}: {detail}{row_suffix}")
    return "\n".join(lines)


def _bind_error_present(artifacts: RunArtifacts) -> InvariantResult:
    """Custom invariant for T10: assert the EXPECTED bind error landed.

    The standard :func:`check_no_bind_errors` invariant is "must be
    zero"; for T10 we INVERT that — we EXPECT at least one
    ``Address already in use`` match in the secondary's slurm_*.err.
    Without this, the pre-bind injection silently no-op'd (e.g. the
    secondary used a different port family) and the test would pass
    on a false positive without exercising the EADDRINUSE path.
    """
    name = "expected_bind_error_present"
    err_files = artifacts.slurm_err_files()
    if not err_files:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"no slurm_*.err files under {artifacts.run_dir}; "
                "cannot verify the bind injection landed"
            ),
            status="fail",
        )

    matches: list[str] = []
    for path in err_files:
        try:
            text = _strip_ansi(
                path.read_text(encoding="utf-8", errors="replace"),
            )
        except OSError as exc:
            matches.append(f"{path.name} unreadable: {exc}")
            continue
        for line in text.splitlines():
            m = _BIND_ERROR_RE.search(line)
            if m is not None:
                matches.append(f"{path.name}: {line.strip()[:200]!r}")

    if not matches:
        return InvariantResult(
            name=name,
            passed=False,
            detail=(
                f"no Address already in use / EADDRINUSE match across "
                f"{len(err_files)} slurm_*.err file(s) in "
                f"{artifacts.run_dir}; the pre-bind injection did NOT "
                "land — the secondary may have bound a different port "
                "family"
            ),
            status="fail",
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"{len(matches)} bind-error match(es) (injection landed): "
            + "; ".join(matches[:5])
        ),
        status="pass",
    )


def _filtered_listener_leak_check(
    artifacts: RunArtifacts,
    probe: ClusterProbe,
    workers: list[str],
    grab: PortGrabResult,
) -> InvariantResult:
    """Wrap :func:`check_no_leaked_listener_ports`, excluding our grab.

    The standard listener leak invariant flags ANY listener owned by
    the runner UID on the watch-list ports. T10 deliberately holds a
    listener on ``grab.port`` for the duration of the dispatch, so the
    standard check would always fail. We re-implement the check here
    with an additional filter: a listener row matching ``grab.port``
    AND whose ``pid`` matches ``grab.listener_pid`` is OUR listener,
    not a leak.

    A row matching ``grab.port`` whose PID DOES NOT match
    ``grab.listener_pid`` is still flagged as a leak — it would mean
    the framework managed to bind that port despite our pre-bind, OR
    that a different runner-UID process (e.g. an orphaned harmonia)
    grabbed the port after our listener died early.
    """
    name = "no_leaked_listener_ports"
    if not probe.is_reachable():
        return InvariantResult(
            name=name,
            passed=False,
            detail="live cluster unavailable",
            status="skip",
        )

    # Re-derive the runner UID via the same gateway probe the standard
    # invariant uses. We re-implement a minimal version here rather
    # than importing private helpers.
    try:
        cp = probe.gateway_ssh("id -u", timeout=10.0)
        runner_uid = (
            int(cp.stdout.strip())
            if cp.returncode == 0 and cp.stdout.strip().isdigit()
            else None
        )
    except (subprocess.TimeoutExpired, OSError):
        runner_uid = None

    # The listener PID our prebind helper captured (worker-side string).
    grab_pid_int: Optional[int] = None
    if grab.listener_pid is not None:
        try:
            grab_pid_int = int(grab.listener_pid)
        except ValueError:
            grab_pid_int = None

    leaked_rows: list[ListenerRow] = []
    descriptions: list[str] = []
    excluded: list[str] = []

    for worker in workers:
        for row in probe.port_listeners(worker, [5000, PORT_GRAB_PORT]):
            if row.uid is None or runner_uid is None:
                continue
            if row.uid != runner_uid:
                continue

            # Filter: if this is OUR pre-bound listener (matched by PID
            # OR by worker+port if PID unknown), exclude it. We prefer
            # PID-match because a reused PID on the same worker+port is
            # unlikely under our brief timeline; the worker+port
            # fallback covers the case where the prebind helper failed
            # to surface the PID but the listener IS ours by virtue of
            # hostname/port.
            is_our_grab = False
            if (
                worker == grab.target_worker
                and row.local_port == grab.port
            ):
                if grab_pid_int is not None and row.pid == grab_pid_int:
                    is_our_grab = True
                elif grab_pid_int is None:
                    # Without a captured PID we assume the only
                    # listener on (target_worker, grab.port) is ours.
                    is_our_grab = True

            if is_our_grab:
                excluded.append(
                    f"{worker}:{row.local_port} "
                    f"({row.process or '<unknown>'} pid={row.pid}) "
                    "[ours, excluded]"
                )
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
            detail=(
                f"{len(leaked_rows)} leaked listener(s): "
                + "; ".join(descriptions)
                + (
                    f"; excluded our grab: {'; '.join(excluded)}"
                    if excluded else ""
                )
            ),
            status="fail",
            rows=tuple(leaked_rows),
        )
    return InvariantResult(
        name=name,
        passed=True,
        detail=(
            f"no leaked listeners on ports [5000, {PORT_GRAB_PORT}] "
            f"owned by uid={runner_uid} across {len(workers)} worker(s) "
            + (
                f"(our grab excluded: {'; '.join(excluded)})"
                if excluded else "(our grab not visible — already gone?)"
            )
        ),
        status="pass",
    )


def _select_target_worker(probe: ClusterProbe) -> Optional[str]:
    """Pick an idle worker for the pre-bind, preferring slurm-worker1.

    Returns ``None`` if neither the preferred nor the fallback is
    idle. Same shape the other failure-injection tests use for
    pre-flight worker selection.
    """
    sinfo_rows = probe.sinfo_nodes()
    by_node = {row.node: row for row in sinfo_rows}
    for candidate in (PREFERRED_TARGET_WORKER, FALLBACK_TARGET_WORKER):
        info = by_node.get(candidate)
        if info is not None and info.state.startswith("idle"):
            return candidate
    return None


@pytest.mark.slurm_live
def test_t10_port_collision(
    cluster_probe: ClusterProbe,  # noqa: ARG001 — fixture used for ordering
    slurm_log_root: pathlib.Path,  # noqa: ARG001 — documented as fixture-driven
    fresh_run: Callable[..., RunResult],
    cleanup_cluster: None,  # noqa: ARG001 — wired via the B2 cleanup harness
) -> None:
    """N=1 tiny dispatch with port 5050 pre-bound on the target worker.

    Pre-flight: gateway reachable, ``squeue --me`` empty, the preferred
    target worker (``slurm-worker1``) is idle (else fall back to
    ``slurm-worker2`` — skip if neither is idle).

    Inject: SSH into the resolved worker and spawn a detached python
    listener bound to ``5050`` with ``SO_REUSEADDR=0`` so the
    secondary's bind WILL collide. The pre-bind happens BEFORE the
    dispatch starts so the port is genuinely held when the secondary's
    peer_push.py / harmonia listener tries to bind.

    Dispatch: tiny workload via ``fresh_run`` so the incremental cache
    is wiped both sides of the call. ``compiler_suit_runner submit``
    is expected to exit non-zero — the secondary's bind failure
    propagates to the local primary's exit code.

    Post-flight:

    * resolve the run's log dir;
    * release the port grab (this happens in the finally block — we
      release REGARDLESS of test outcome so a failed test does not
      leave a port-holder running);
    * wait for ``squeue --me`` to drain;
    * assert the dispatch failed (non-zero exit code) — anything else
      means the injection silently no-op'd;
    * run the file invariants (custom: assert the EXPECTED bind
      error IS present, contrary to the standard "no bind errors"
      check);
    * run the cluster-side leak invariants (5/6/7) with invariant 6
      filtered to exclude OUR pre-bound listener — these are the KEY
      assertions; ANY leak here is exactly the smoke16-class bug.

    Failure surface: the assertion message lists every invariant's
    name + detail + row count, plus the helper's PortGrabResult, so a
    triage path is obvious without re-reading the gateway logs by hand.
    """
    probe = _live_probe()

    # ---- pre-flight ---------------------------------------------------
    if not probe.is_reachable():
        pytest.skip(
            "live slurm-test-env gateway unreachable at "
            "ssh://sirati@localhost:2244 (set up the env or run with "
            "-m 'not slurm_live')",
        )

    queued = probe.squeue_me()
    assert queued == [], (
        f"squeue --me must be empty at T10 start; found "
        f"{len(queued)} job(s): {queued!r}"
    )

    target_worker = _select_target_worker(probe)
    if target_worker is None:
        pytest.skip(
            f"T10 needs at least one idle worker among "
            f"{[PREFERRED_TARGET_WORKER, FALLBACK_TARGET_WORKER]}; "
            "neither is idle (sinfo) — cluster busy or workers down",
        )

    # ---- arm the port grab BEFORE the dispatch starts ---------------
    timeout_s = _resolve_timeout()
    grab = prebind_port_on_worker(
        probe,
        target_worker=target_worker,
        port=PORT_GRAB_PORT,
        # Internal listener lifetime cap mirrors the test's wall-clock
        # cap so a forgotten release_port_grab cannot leak the listener
        # past this test's budget.
        listener_timeout_s=timeout_s,
    )

    # We MUST guarantee the release fires regardless of the test's
    # outcome. The cleanup_cluster fixture's pkill sweeps target the
    # framework's process families (compiler_suit_runner / harmonia-cache
    # / peer_push), NOT our python listener — so without an explicit
    # release call the listener would survive the cluster cleanup pass
    # and only die when its internal listener_timeout_s elapses.
    try:
        # Up-front sanity: if the grab itself failed (e.g. the worker
        # is missing python or the SSH call timed out), there's no
        # point in driving the dispatch. Surface a clear test failure.
        assert grab.bound, (
            f"prebind_port_on_worker did NOT bind {PORT_GRAB_PORT} on "
            f"{target_worker}; cannot exercise T10. "
            f"PortGrabResult={grab!r}"
        )
        # If the worker-side ``ss`` probe ran AND returned 0 (bind
        # not visible), surface that as a hard failure — the
        # secondary's bind cannot collide with a listener that isn't
        # actually bound.
        if grab.bind_verified is False:
            pytest.fail(
                f"prebind on {target_worker}:{PORT_GRAB_PORT} reported "
                f"BIND_VERIFIED=0 (ss could not see the listener); "
                f"PortGrabResult={grab!r}"
            )

        # ---- compose invocation ---------------------------------------
        invocation = dataclasses.replace(
            default_invocation_for_smoke(jobs=N_SECONDARIES, workload="tiny"),
            ssh_identity_file=pathlib.Path(LIVE_KEY_PATH),
            slurm_cpus_per_task=2,
        )

        # ---- run the dispatch -----------------------------------------
        result = fresh_run(invocation, timeout_s=timeout_s)

        detail = (
            f"exit={result.exit_code} "
            f"wall={result.wall_time_s:.1f}s "
            f"run_id={result.run_id!r} "
            f"jobs={N_SECONDARIES} "
            f"target_worker={target_worker!r} "
            f"port={PORT_GRAB_PORT} "
            f"log_dir={result.log_dir!s}"
        )

        # ---- assert the dispatch failed --------------------------------
        # The secondary's bind failure SHOULD propagate to the local
        # primary's exit code. A zero exit here means the injection
        # silently no-op'd; the test would otherwise pass on a false
        # positive without exercising the EADDRINUSE path.
        assert result.exit_code != 0, (
            f"compiler_suit_runner submit exited 0 even though we "
            f"pre-bound {target_worker}:{PORT_GRAB_PORT}; the "
            f"injection did not land. ({detail})"
        )

        # ---- run_id / log_dir resolution -------------------------------
        if result.run_id is None or result.log_dir is None:
            pytest.fail(
                f"compiler_suit_runner submit did not produce a run_id "
                f"({detail}). stderr tail:\n{result.stderr[-2000:]}"
            )
        log_dir = result.log_dir
        if not log_dir.is_dir():
            log_dir = resolve_log_dir(
                result.run_id, log_root=SLURM_TEST_ENV_LOG_ROOT,
            )
        assert log_dir.is_dir(), (
            f"expected run log dir {log_dir} after dispatch ({detail})"
        )

        # ---- drain SLURM before invariant audit -----------------------
        drained = wait_squeue_empty(probe, timeout_s=120.0)
        assert drained, (
            f"squeue --me did not drain within 120s after the bind "
            f"failure ({detail}); SLURM should have reaped the failed "
            f"secondary's job within a couple of seconds"
        )

        # ---- invariant audit ------------------------------------------
        artifacts = RunArtifacts.from_dir(
            log_dir, shared_fs=invocation.shared_fs,
        )

        # File invariants:
        #
        # * SKIP standard 1 (clean exit) — the secondary FAILED by
        #   design;
        # * INVERT standard 2 (no bind errors) — we EXPECT at least
        #   one ``Address already in use`` match;
        # * SKIP standard 3 (manifest count) — no variants complete;
        # * SKIP standard 4 (build-failures) — the failure happens
        #   before the build phase, so build-failures/ may be empty
        #   even though the run failed. The KEY assertions are the
        #   cluster-side leak checks (5/6/7).
        bind_error_present = _bind_error_present(artifacts)

        # Cluster invariants (the KEY assertions for T10):
        #
        # * 5: no leaked containers — SLURM's proctrack + the
        #   framework's wrapper script must tear down the half-started
        #   secondary container despite the bind error;
        # * 6: no leaked listener ports — filtered to exclude OUR
        #   pre-bound 5050 listener via PID-match;
        # * 7: no leaked PPID=1 processes — the wrapper must clean up
        #   the secondary coordinator + any spawned subprocesses
        #   despite the failure.
        leaked_containers = check_no_leaked_containers(
            artifacts, probe, WORKERS,
        )
        leaked_listeners = _filtered_listener_leak_check(
            artifacts, probe, WORKERS, grab=grab,
        )
        leaked_processes = check_no_leaked_processes(
            artifacts, probe, WORKERS,
        )

        results: list[InvariantResult] = [
            bind_error_present,
            leaked_containers,
            leaked_listeners,
            leaked_processes,
        ]

        failed = [r for r in results if not r.passed]
        assert not failed, (
            f"invariant(s) failed for {detail}\n"
            f"grab={grab!r}\n"
            f"results:\n{_format_results(results)}"
        )
    finally:
        # Release the port grab regardless of test outcome. The
        # listener_timeout_s cap on the worker is the safety net, but
        # an explicit release frees the port immediately so subsequent
        # tests (or manual cluster pokes) don't have to wait.
        released, release_notes = release_port_grab(probe, grab)
        if not released:
            # Note: we do NOT raise here because the test may already
            # be carrying a primary failure. Surfacing as a soft
            # warning via stderr keeps the primary failure visible
            # while still flagging the leftover listener.
            sys.stderr.write(
                f"WARNING: T10 release_port_grab did not confirm "
                f"release on {grab.target_worker}:{grab.port} "
                f"(pid={grab.listener_pid!r}); notes={release_notes!r}. "
                f"The worker-side timeout will reap it within "
                f"{int(timeout_s)}s.\n"
            )


# ---------------------------------------------------------------------------
# Helper-shape unit test (offline; no live cluster needed)
# ---------------------------------------------------------------------------


def test_prebind_port_on_worker_argv_shape() -> None:
    """Mock-test that the helper drives the expected SSH wire shape.

    Verifies the wire-shape of :func:`prebind_port_on_worker` without
    touching the live cluster:

    * the worker_ssh call targets the requested worker hostname;
    * the worker-side script carries the requested port literal AND
      the shell-quoted pidfile path;
    * a successful ``LISTENER_PID=...`` / ``LISTENER_BOUND=<port>``
      response yields ``bound=True`` and populates ``listener_pid``,
      ``pidfile``, ``started_at``, ``bind_verified``.
    """

    captured: list[tuple[str, str, float | None]] = []

    def _fake_worker_ssh(
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,  # noqa: ARG001 — unused in stub
    ) -> subprocess.CompletedProcess[str]:
        captured.append((worker, cmd, timeout))
        stdout = (
            "LISTENER_PID=12345\n"
            "LISTENER_BOUND=5050\n"
            "PIDFILE=/tmp/asm-portgrab-5050.pid\n"
            "BIND_VERIFIED=1\n"
        )
        return subprocess.CompletedProcess(
            args=["ssh", worker, cmd],
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    grab = prebind_port_on_worker(
        _StubProbe(),
        target_worker="slurm-worker1",
        port=5050,
    )

    assert grab.bound is True, grab
    assert grab.target_worker == "slurm-worker1", grab
    assert grab.port == 5050, grab
    assert grab.listener_pid == "12345", grab
    assert grab.pidfile == "/tmp/asm-portgrab-5050.pid", grab
    assert grab.bind_verified is True, grab
    assert grab.started_at is not None, grab
    assert len(captured) == 1, captured
    worker, cmd, _timeout = captured[0]
    assert worker == "slurm-worker1", captured
    # The helper substitutes the literal port into the script and
    # shell-quotes the pidfile path. ``shlex.quote`` only adds quotes
    # when the path contains shell-significant characters — the
    # default ``/tmp/asm-portgrab-5050.pid`` is plain enough that
    # quoting reduces to the bare path. We assert on the bare path.
    assert "PORT=5050" in cmd, cmd
    assert "PIDFILE=/tmp/asm-portgrab-5050.pid" in cmd, cmd
    # The script must spawn a python listener — the actual binary
    # name is decided at runtime, but the call shape must be present.
    assert "python" in cmd, cmd
    assert "SO_REUSEADDR" in cmd, cmd


def test_prebind_port_on_worker_surfaces_worker_error() -> None:
    """When the worker script reports ``ERROR=...``, surface it as a note."""

    def _fake_worker_ssh(
        worker: str,  # noqa: ARG001 — unused
        cmd: str,  # noqa: ARG001 — unused
        *,
        timeout: float | None = None,  # noqa: ARG001 — unused
        check: bool = False,  # noqa: ARG001 — unused
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ERROR=no_python_on_worker\n",
            stderr="",
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    grab = prebind_port_on_worker(
        _StubProbe(),
        target_worker="slurm-worker1",
        port=5050,
    )

    assert grab.bound is False, grab
    assert grab.target_worker == "slurm-worker1", grab
    assert grab.port == 5050, grab
    assert grab.listener_pid is None, grab
    assert any("no_python_on_worker" in n for n in grab.notes), grab.notes


def test_release_port_grab_argv_shape() -> None:
    """Mock-test that release_port_grab targets the captured PID + path."""

    captured: list[tuple[str, str]] = []

    def _fake_worker_ssh(
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,  # noqa: ARG001 — unused
        check: bool = False,  # noqa: ARG001 — unused
    ) -> subprocess.CompletedProcess[str]:
        captured.append((worker, cmd))
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="STATE=terminated\n",
            stderr="",
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    from datetime import datetime, timezone

    grab = PortGrabResult(
        bound=True,
        target_worker="slurm-worker1",
        port=5050,
        listener_pid="12345",
        pidfile="/tmp/asm-portgrab-5050.pid",
        started_at=datetime.now(timezone.utc),
        bind_verified=True,
    )

    released, notes = release_port_grab(_StubProbe(), grab)

    assert released is True, (released, notes)
    assert notes == (), notes
    assert len(captured) == 1, captured
    worker, cmd = captured[0]
    assert worker == "slurm-worker1", captured
    # The release script must carry both the captured PID (shell-
    # quoted) and the pidfile path; the port is interpolated as a
    # literal int. ``shlex.quote`` only adds quotes when the value
    # contains shell-significant characters — both ``"12345"`` and
    # the default pidfile path are plain, so the bare tokens are
    # what shows up.
    assert "PID=12345" in cmd, cmd
    assert "PIDFILE=/tmp/asm-portgrab-5050.pid" in cmd, cmd
    assert "PORT=5050" in cmd, cmd
    # Both signal escalations must be present (SIGTERM first, SIGKILL
    # as the safety net).
    assert "kill -TERM" in cmd, cmd
    assert "kill -KILL" in cmd, cmd


def test_release_port_grab_skips_when_grab_not_bound() -> None:
    """``release_port_grab`` is a no-op when the grab never bound."""

    def _fake_worker_ssh(
        worker: str,  # noqa: ARG001 — unused
        cmd: str,  # noqa: ARG001 — unused
        *,
        timeout: float | None = None,  # noqa: ARG001 — unused
        check: bool = False,  # noqa: ARG001 — unused
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(
            "worker_ssh must not be called when grab.bound=False"
        )

    class _StubProbe:
        worker_ssh = staticmethod(_fake_worker_ssh)

    grab = PortGrabResult(
        bound=False,
        target_worker="slurm-worker1",
        port=5050,
    )

    released, notes = release_port_grab(_StubProbe(), grab)
    assert released is True, (released, notes)
    assert notes == (), notes


# Skip pytest's collection of the live test on non-linux just to keep
# the unit tests above runnable on developer machines that don't have
# the slurm-test-env. The pytest marker ``slurm_live`` already gates
# the live test off-by-default; this is belt-and-braces.
if sys.platform != "linux":  # pragma: no cover - platform guard
    pytestmark = pytest.mark.skip(
        reason="slurm-test-env is linux-only",
    )

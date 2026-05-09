"""SSH-based probes for the local SLURM test environment.

Most of this module is read-only - the probes are a thin wrapper over
the OpenSSH client and cover the primitives the per-run invariant
checker (and downstream preflight code) needs:

- gateway / worker shell execution via explicit ``-i`` key (never via
  ``ssh-agent`` or ``~/.ssh/`` per project policy);
- ``squeue``/``sinfo`` parsing scoped to the current SSH user;
- ``podman ps -a`` enumeration with full label set on each worker;
- listener / process inspection (``ss``, ``ps``) for leak detection.

The single mutating entry point is :meth:`ClusterProbe.cleanup`, which
performs the between-test cleanup harness described in the slurm test
plan: ``scancel`` filtered by job-name pattern (NEVER by user, since
the test-env's ``kruppb`` account is shared with the asm-tokenizer
peer), per-worker podman stop+rm, ``pkill`` for known-leaky processes,
and a poll loop on harmonia/peer_push listener ports.

All commands are dispatched through :class:`subprocess.run` with
``shell=False`` and an explicit argv vector so neither the gateway nor
the workers ever see a shell-quoted string assembled from caller input.
The few remote-shell expressions we DO assemble (the podman compound
on each worker) only interpolate constants - never caller input - so
they're safe under the implicit ``bash -lc`` SSH applies remotely.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
import time
from typing import Any, Final, Iterable, Mapping, Sequence

__all__ = [
    "GatewayConfig",
    "ClusterProbe",
    "CleanupReport",
    "WorkerCleanupResult",
    "SqueueRow",
    "SinfoRow",
    "PodmanRow",
    "ListenerRow",
    "ProcessRow",
    "DEFAULT_CLEANUP_WORKERS",
    "DEFAULT_CLEANUP_PORTS",
    "DEFAULT_CLEANUP_PKILL_PATTERN",
    "DEFAULT_SCANCEL_PATTERN",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SSH_TIMEOUT: Final[float] = 30.0
"""Default per-call timeout (seconds) for an SSH probe."""

DEFAULT_GATEWAY_PORT: Final[int] = 2244
"""Default SSH port for the local SLURM test gateway."""

DEFAULT_GATEWAY_HOST: Final[str] = "sirati@localhost"
"""Default ``user@host`` form for the gateway."""

# OpenSSH options shared by every probe call. ``IdentitiesOnly=yes`` is
# REQUIRED so the explicit ``-i`` key wins - otherwise the user's
# ssh-agent (which must NOT carry the test-env key per project policy)
# could be tried first. ``StrictHostKeyChecking=no`` is acceptable here
# because the test cluster runs locally and is rebuilt frequently.
_BASE_SSH_OPTS: Final[tuple[str, ...]] = (
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    # ControlMaster multiplexing: a burst of probe SSH calls (squeue +
    # 4 worker ProxyJumps + listener probes) was tripping the gateway
    # sshd's MaxStartups rate limit, surfacing as "Connection closed
    # by ::1 port 2244" on the next call. Reusing one master connection
    # per (host, port, user) tuple avoids that and is also faster.
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/asm-cluster-probe-%C",
    "-o", "ControlPersist=60s",
)


# ---------------------------------------------------------------------------
# Cleanup defaults
# ---------------------------------------------------------------------------

DEFAULT_CLEANUP_WORKERS: Final[tuple[str, ...]] = (
    "slurm-worker1",
    "slurm-worker2",
    "slurm-worker3",
    "slurm-worker4",
)
"""Worker hostnames the cleanup harness sweeps by default.

Matches the live local test-env topology (4 workers; see plan section
"Confirmed cluster topology"). Override via the ``workers=`` kwarg on
:meth:`ClusterProbe.cleanup` if a future test env adds/removes nodes.
"""

DEFAULT_CLEANUP_PORTS: Final[tuple[int, ...]] = (5000, 5050)
"""Listener ports the cleanup harness polls for release.

5000 is harmonia-cache, 5050 is peer_push - the two leaky listeners
identified in the smoke16/17/18 retrospective. Per-port poll keeps a
stale binder from breaking the next run with ``EADDRINUSE``.
"""

DEFAULT_CLEANUP_PKILL_PATTERN: Final[str] = (
    "compiler_suit_runner|harmonia-cache|peer_push"
)
"""Regex passed to ``pkill -KILL -f`` on each worker.

Three families catch the entire post-promotion leak surface:
``compiler_suit_runner`` covers the secondary coordinator + every
build_worker; ``harmonia-cache`` covers the binary-cache server;
``peer_push`` covers the per-secondary HTTP push server.
"""

DEFAULT_SCANCEL_PATTERN: Final[str] = "asm-secondary-*"
"""Glob passed to ``scancel --jobname=`` by default.

CRITICAL (memory ``feedback_scancel_scope.md``): the test-env's
``kruppb`` account is shared with the asm-tokenizer peer, so we must
NEVER pass ``--user=kruppb``. Filtering by our own job-name glob is
the only safe way to scope scancel.
"""

DEFAULT_CLEANUP_TIMEOUT: Final[float] = 90.0
"""Default end-to-end timeout (seconds) for :meth:`ClusterProbe.cleanup`.

Budget breakdown: 60s ``squeue --me`` poll + 30s ports poll, mirroring
the pseudocode in plan section "Cleanup harness". Individual SSH calls
keep their own short timeouts so a single hung command can't exhaust
the budget.
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkerProbeError(RuntimeError):
    """Raised by leak-check probes when the worker SSH itself fails.

    Treating ``rc != 0`` as "no rows" silently hides leaks when the
    probe path itself is broken (stale known_hosts, missing pubkey,
    network partition). The leak invariants must surface this as a
    failure rather than declare the worker clean. Lenient callers
    (cleanup polling, repro-helper container lookup) can catch this
    and degrade explicitly.
    """

    def __init__(self, worker: str, command: str, rc: int, stderr: str) -> None:
        self.worker = worker
        self.command = command
        self.rc = rc
        self.stderr = stderr
        super().__init__(
            f"worker_ssh to {worker!r} failed: rc={rc} cmd={command!r} "
            f"stderr={stderr.strip()!r}"
        )


# ---------------------------------------------------------------------------
# Structured row types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Connection config for the SLURM-test-env gateway.

    The gateway is the single SSH endpoint; workers are reached via
    ``ssh -J`` (ProxyJump) through it. Defaults match the live local
    test env documented in the project plan.
    """

    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    identity_file: str | None = None
    """Path to the private key (``-i`` value). ``None`` means do not
    pass ``-i`` - useful only in tests that fully mock subprocess."""

    timeout: float = DEFAULT_SSH_TIMEOUT


@dataclasses.dataclass(frozen=True, slots=True)
class SqueueRow:
    """One parsed row from ``squeue --me``."""

    jobid: str
    partition: str
    name: str
    state: str
    user: str
    nodelist: str


@dataclasses.dataclass(frozen=True, slots=True)
class SinfoRow:
    """One parsed row from ``sinfo -N``: a ``(node, partition, state)``
    triple."""

    node: str
    partition: str
    state: str


@dataclasses.dataclass(frozen=True, slots=True)
class PodmanRow:
    """One parsed row from ``podman ps -a --format=json``.

    Only the fields useful for leak detection are surfaced; the raw
    JSON object is preserved under :attr:`raw` so callers that need
    obscure attributes can dig in without re-parsing.
    """

    id: str
    name: str
    image: str
    state: str
    started_at: str
    labels: Mapping[str, str]
    raw: Mapping[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class ListenerRow:
    """One parsed row from ``ss -lntp``."""

    proto: str
    local_address: str
    local_port: int
    pid: int | None
    process: str | None
    uid: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class ProcessRow:
    """One parsed row from ``ps -eo pid,ppid,user,etime,cmd``."""

    pid: int
    ppid: int
    user: str
    etime: str
    cmd: str


@dataclasses.dataclass(slots=True)
class WorkerCleanupResult:
    """Per-worker accounting for one :meth:`ClusterProbe.cleanup` pass.

    Containers are counted in two phases (stop, then rm) so a partial
    failure in either phase is visible. ``processes_killed`` counts
    PIDs the post-pkill probe could no longer find with the cleanup
    pattern - the literal ``pkill`` exit code isn't a useful signal
    (it returns 1 when nothing matched, which is the desired terminal
    state).
    """

    worker: str
    containers_stopped: int = 0
    containers_removed: int = 0
    processes_killed: int = 0
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(slots=True)
class CleanupReport:
    """Diagnosable record of one :meth:`ClusterProbe.cleanup` pass.

    The report carries enough signal to decide whether the cluster is
    actually clean (used by the test fixture to assert "no errors at
    end-of-test") AND to surface what was wrong if it isn't (used by
    a human reading the test failure).

    Fields:

    - ``reachable``: gateway health probe before scancel; if ``False``
      every later step is skipped and ``errors`` carries
      ``"cluster unreachable"``.
    - ``jobs_canceled``: rows in ``squeue --me`` immediately before
      scancel (so 0 means the queue was already empty).
    - ``squeue_drained``: ``True`` iff ``squeue --me`` reached empty
      within ``timeout_s`` after scancel.
    - ``ports_released``: list of (worker, port) pairs whose listener
      was confirmed gone. Pairs that never released within the timeout
      land in ``ports_still_bound`` instead.
    - ``per_worker``: per-worker container/process counts so a failure
      on a single worker doesn't hide behind aggregate numbers.
    - ``errors``: collected, never raised - the fixture decides whether
      to assert empty.
    """

    reachable: bool = True
    jobs_canceled: int = 0
    squeue_drained: bool = False
    per_worker: dict[str, WorkerCleanupResult] = dataclasses.field(
        default_factory=dict,
    )
    ports_released: list[tuple[str, int]] = dataclasses.field(
        default_factory=list,
    )
    ports_still_bound: list[tuple[str, int]] = dataclasses.field(
        default_factory=list,
    )
    errors: list[str] = dataclasses.field(default_factory=list)
    duration_s: float = 0.0

    @property
    def containers_stopped(self) -> int:
        """Sum of containers stopped across all workers."""
        return sum(w.containers_stopped for w in self.per_worker.values())

    @property
    def containers_removed(self) -> int:
        """Sum of containers removed across all workers."""
        return sum(w.containers_removed for w in self.per_worker.values())

    @property
    def processes_killed(self) -> int:
        """Sum of leaky processes killed across all workers."""
        return sum(w.processes_killed for w in self.per_worker.values())


# ---------------------------------------------------------------------------
# ClusterProbe
# ---------------------------------------------------------------------------


class ClusterProbe:
    """Read-only SSH probes against the SLURM test gateway and workers.

    The class is intentionally small - it returns structured rows and
    never raises on a *missing* cluster, only on parse / programming
    errors. Callers that want a "skip the test if the cluster is down"
    flow should call :meth:`is_reachable` first.
    """

    def __init__(self, gateway: GatewayConfig | None = None) -> None:
        self.gateway: Final[GatewayConfig] = gateway or GatewayConfig()

    # -- low-level command construction --------------------------------

    def _gateway_argv(self, remote_cmd: str) -> list[str]:
        """Build an ``ssh ... <gateway> <remote_cmd>`` argv vector."""
        argv: list[str] = ["ssh"]
        if self.gateway.identity_file is not None:
            argv += ["-i", self.gateway.identity_file]
        argv += ["-p", str(self.gateway.port)]
        argv += list(_BASE_SSH_OPTS)
        argv.append(self.gateway.host)
        argv.append(remote_cmd)
        return argv

    def _worker_argv(self, worker: str, remote_cmd: str) -> list[str]:
        """Build an ``ssh ... <worker> <remote_cmd>`` argv that hops via
        the gateway.

        Use ``ProxyCommand`` rather than ``-J`` so the gateway-hop SSH
        inherits the same hardened options (StrictHostKeyChecking=no,
        BatchMode=yes, IdentitiesOnly=yes, …) as the outer worker SSH;
        ``-J`` runs an inner ``ssh`` that picks up only the user's
        default config, which on a freshly re-keyed slurm-test-env
        surfaces as ``Host key verification failed`` for the inner hop.

        ``worker`` is the bare hostname; the SLURM-test-env DNS resolves
        ``slurm-worker{1..4}`` from inside the gateway namespace.
        """
        # Build the inner (gateway-hop) ssh argv WITH Control* multiplexing
        # so the burst of probe + cleanup SSH calls (squeue, sinfo, podman
        # ps, port scans, cleanup pkills) reuses one master per (host, port,
        # user) rather than racing the gateway sshd's MaxStartups limiter.
        # Then escape every ``%`` in the inner argv to ``%%`` so the outer
        # ssh's ProxyCommand percent-expansion (which only accepts
        # ``%h/%p/%r/%u``) emits the literal ``%`` for the inner ssh —
        # otherwise ``ControlPath=…%C`` fails with
        # ``vdollar_percent_expand: unknown key %C`` before the proxy
        # connect even starts. The ``-W %h:%p`` stream-forward token is
        # appended AFTER escaping so the outer ssh substitutes the worker
        # hostname/port itself.
        inner_argv: list[str] = ["ssh"]
        if self.gateway.identity_file is not None:
            inner_argv += ["-i", self.gateway.identity_file]
        inner_argv += [
            "-p", str(self.gateway.port),
            *list(_BASE_SSH_OPTS),
            self.gateway.host,
        ]
        inner_str = " ".join(shlex.quote(a) for a in inner_argv)
        inner_str_escaped = inner_str.replace("%", "%%")
        proxy_cmd = f"{inner_str_escaped} -W %h:%p"

        argv: list[str] = ["ssh"]
        if self.gateway.identity_file is not None:
            argv += ["-i", self.gateway.identity_file]
        argv += list(_BASE_SSH_OPTS)
        argv += ["-o", f"ProxyCommand={proxy_cmd}"]
        argv.append(worker)
        argv.append(remote_cmd)
        return argv

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` with text I/O and capture stdout/stderr.

        ``timeout`` defaults to the gateway config's timeout. ``check``
        defaults to ``False`` because the probes prefer surfacing a
        non-zero return code via the structured row's caller rather
        than raising mid-parse.
        """
        eff_timeout = self.gateway.timeout if timeout is None else timeout
        return subprocess.run(  # noqa: S603 - argv is fully programmatic
            argv,
            capture_output=True,
            text=True,
            timeout=eff_timeout,
            check=check,
        )

    # -- public command runners ----------------------------------------

    def gateway_ssh(
        self,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd`` on the gateway. ``cmd`` is a single shell string
        - it IS evaluated by the remote shell (that is the OpenSSH
        client's contract). Callers must shell-quote any user-derived
        substrings via :func:`shlex.quote` themselves; this module
        constructs every command from constants and explicitly quoted
        identifiers."""
        return self._run(self._gateway_argv(cmd), timeout=timeout, check=check)

    def worker_ssh(
        self,
        worker: str,
        cmd: str,
        *,
        timeout: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd`` on ``worker`` via gateway ProxyJump."""
        return self._run(
            self._worker_argv(worker, cmd), timeout=timeout, check=check,
        )

    # -- health check --------------------------------------------------

    def is_reachable(self, *, timeout: float = 5.0) -> bool:
        """Quick gateway health probe. Returns ``True`` iff
        ``ssh ... echo ok`` succeeds AND prints ``ok``. Suppresses
        every conceivable subprocess failure (timeout, connection
        refused, key rejection) - this is the gate other tests use to
        decide between live-cluster mode and skip."""
        try:
            cp = self.gateway_ssh("echo ok", timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return cp.returncode == 0 and cp.stdout.strip() == "ok"

    # -- squeue --------------------------------------------------------

    def squeue_me(
        self, *, timeout: float | None = None,
    ) -> list[SqueueRow]:
        """List the current SSH user's SLURM jobs.

        Uses ``squeue --me --noheader -o`` with a stable format string
        so we don't depend on the local SLURM defaults. Returns an
        empty list when the user has no jobs (the typical clean-state
        case).
        """
        fmt = "%i|%P|%j|%T|%u|%R"
        cmd = f"squeue --me --noheader -o {shlex.quote(fmt)}"
        cp = self.gateway_ssh(cmd, timeout=timeout)
        rows: list[SqueueRow] = []
        if cp.returncode != 0:
            # squeue with no jobs returns 0 anyway; any non-zero is a
            # real error. Surface it as an empty list - the caller
            # checks reachability separately.
            return rows
        for line in cp.stdout.splitlines():
            parts = line.split("|")
            if len(parts) != 6:
                continue
            jobid, partition, name, state, user, nodelist = parts
            rows.append(
                SqueueRow(
                    jobid=jobid,
                    partition=partition,
                    name=name,
                    state=state,
                    user=user,
                    nodelist=nodelist,
                ),
            )
        return rows

    # -- sinfo ---------------------------------------------------------

    def sinfo_nodes(
        self, *, timeout: float | None = None,
    ) -> list[SinfoRow]:
        """Per-node SLURM state. Uses ``sinfo -N --noheader -o`` with a
        custom format so the parse stays stable across SLURM versions."""
        fmt = "%N|%R|%t"
        cmd = f"sinfo -N --noheader -o {shlex.quote(fmt)}"
        cp = self.gateway_ssh(cmd, timeout=timeout)
        rows: list[SinfoRow] = []
        if cp.returncode != 0:
            return rows
        for line in cp.stdout.splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            node, partition, state = parts
            rows.append(SinfoRow(node=node, partition=partition, state=state))
        return rows

    # -- podman --------------------------------------------------------

    def podman_ps(
        self,
        worker: str,
        *,
        rootless: bool = True,
        timeout: float | None = None,
    ) -> list[PodmanRow]:
        """Enumerate every container on ``worker`` (running + exited).

        ``--format=json`` returns a JSON array; rootless secondaries
        keep their storage under ``/run/user/$UID``, so we point podman
        at it explicitly. The caller filters by labels (run id, etc.)
        - this method intentionally returns the full set so a leak
        check can flag rows that don't carry the expected run-id
        label.
        """
        if rootless:
            # ``$(id -u)`` is evaluated by the remote shell - cheap and
            # avoids a separate round-trip to discover the UID.
            podman = (
                "podman --root /run/user/$(id -u)/storage "
                "--runroot /run/user/$(id -u)/runtime"
            )
        else:
            podman = "podman"
        cmd = f"{podman} ps -a --format=json"
        cp = self.worker_ssh(worker, cmd, timeout=timeout)
        if cp.returncode != 0:
            raise WorkerProbeError(worker, cmd, cp.returncode, cp.stderr)
        return _parse_podman_json(cp.stdout)

    # -- ss / port listeners ------------------------------------------

    def port_listeners(
        self,
        worker: str,
        ports: Iterable[int],
        *,
        timeout: float | None = None,
    ) -> list[ListenerRow]:
        """List TCP listeners on ``worker`` filtered to ``ports``.

        Uses ``ss -lntpe`` (numeric, listening only, with PID + UID).
        ``ports`` is a small iterable of ints (e.g. ``[5000, 5050]`` for
        harmonia + peer_push); we filter client-side because ``ss
        -e``'s ``( sport == :N )`` syntax is awkward to combine across
        multiple ports without invoking a shell expression.
        """
        port_set = {int(p) for p in ports}
        if not port_set:
            return []
        cmd = "ss -lntpe"
        cp = self.worker_ssh(worker, cmd, timeout=timeout)
        if cp.returncode != 0:
            raise WorkerProbeError(worker, cmd, cp.returncode, cp.stderr)
        return [
            row for row in _parse_ss_lntpe(cp.stdout)
            if row.local_port in port_set
        ]

    # -- ps ------------------------------------------------------------

    # -- cleanup harness ----------------------------------------------

    def cleanup(
        self,
        *,
        scancel_pattern: str = DEFAULT_SCANCEL_PATTERN,
        workers: Sequence[str] | None = None,
        ports: Sequence[int] = DEFAULT_CLEANUP_PORTS,
        pkill_pattern: str = DEFAULT_CLEANUP_PKILL_PATTERN,
        timeout_s: float = DEFAULT_CLEANUP_TIMEOUT,
        squeue_poll_interval: float = 1.0,
        ports_poll_interval: float = 1.0,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> CleanupReport:
        """Between-test cleanup of the SLURM test environment.

        Performs the four steps in plan section "Cleanup harness":

        1. ``scancel --jobname=<scancel_pattern> --user=$(whoami)`` on
           the gateway. Note: ``$(whoami)`` is the SSH user (``sirati``
           in the local test-env), NOT the production ``kruppb``
           account. We MUST NOT pass ``--user=kruppb`` because that
           account is shared with the asm-tokenizer peer per memory
           ``feedback_scancel_scope.md``.
        2. Poll ``squeue --me`` until empty (or 60s of the budget).
        3. On each worker: stop+rm rootless podman containers and
           ``pkill -KILL -f`` the leaky processes.
        4. Poll the harmonia/peer_push listener ports until released
           (or 30s of the budget).

        All steps capture, never raise. Errors land in
        :attr:`CleanupReport.errors`. The caller (the
        ``cleanup_cluster`` pytest fixture) decides whether to assert
        empty errors at end-of-test.

        Parameters control the safety surface:

        - ``scancel_pattern``: glob passed to ``--jobname=``. Default
          scopes to ``asm-secondary-*``. Override only for tests that
          inject a different job-name family.
        - ``workers``: hostnames swept; defaults to the 4-node test
          env. Pass an empty sequence to skip the per-worker pass.
        - ``ports``: listener ports to poll for release. Defaults to
          ``(5000, 5050)``.
        - ``pkill_pattern``: regex passed to ``pkill -KILL -f``.
          Defaults to the leaky-process families.
        - ``timeout_s``: total budget. The squeue poll consumes up to
          60s, the per-worker pass is bounded by per-call SSH
          timeouts, and the ports poll consumes the remainder.
        - ``clock`` / ``sleep``: injectable for test mocking.

        Returns a :class:`CleanupReport`. Inspect ``errors``,
        ``squeue_drained``, ``ports_still_bound`` to decide whether
        the cluster is actually clean.
        """
        report = CleanupReport()
        worker_list = (
            tuple(workers) if workers is not None else DEFAULT_CLEANUP_WORKERS
        )
        start = clock()
        deadline = start + max(timeout_s, 0.0)

        # ---- step 0: reachability gate -------------------------------
        if not self.is_reachable(timeout=min(self.gateway.timeout, 5.0)):
            report.reachable = False
            report.errors.append("cluster unreachable")
            report.duration_s = max(0.0, clock() - start)
            return report

        # ---- step 1: scancel by job-name pattern ---------------------
        # CRITICAL: filter by --jobname=<pattern> AND scope to
        # ``--user=$(whoami)``. NEVER hard-code ``--user=kruppb``: the
        # kruppb account is shared with the asm-tokenizer peer in the
        # production cluster, and the local SSH user (``sirati``) is
        # what $(whoami) resolves to in the test-env shell.
        try:
            squeue_before = self.squeue_me(
                timeout=min(self.gateway.timeout, 10.0),
            )
            report.jobs_canceled = sum(
                1 for r in squeue_before
                if _matches_glob(r.name, scancel_pattern)
            )
            scancel_cmd = (
                "scancel --jobname="
                f"{shlex.quote(scancel_pattern)} "
                "--user=\"$(whoami)\""
            )
            self.gateway_ssh(
                scancel_cmd, timeout=min(self.gateway.timeout, 15.0),
            )
        except subprocess.TimeoutExpired as exc:
            report.errors.append(f"scancel: timeout ({exc})")
        except OSError as exc:
            report.errors.append(f"scancel: {exc}")

        # ---- step 2: poll squeue --me until empty --------------------
        squeue_deadline = min(clock() + 60.0, deadline)
        report.squeue_drained = self._poll_until(
            check=lambda: not self._squeue_has_jobs_safely(),
            deadline=squeue_deadline,
            interval=squeue_poll_interval,
            clock=clock,
            sleep=sleep,
        )
        if not report.squeue_drained:
            report.errors.append(
                "squeue --me did not drain within timeout",
            )

        # ---- step 3: per-worker container + process cleanup ---------
        for worker in worker_list:
            worker_result = self._cleanup_worker(
                worker=worker,
                pkill_pattern=pkill_pattern,
            )
            report.per_worker[worker] = worker_result
            for err in worker_result.errors:
                report.errors.append(f"{worker}: {err}")

        # ---- step 4: poll listener ports until released --------------
        ports_deadline = min(clock() + 30.0, deadline)
        released, still_bound = self._poll_ports(
            workers=worker_list,
            ports=ports,
            deadline=ports_deadline,
            interval=ports_poll_interval,
            clock=clock,
            sleep=sleep,
        )
        report.ports_released = released
        report.ports_still_bound = still_bound
        if still_bound:
            report.errors.append(
                "ports still bound after cleanup: "
                + ", ".join(f"{w}:{p}" for w, p in still_bound),
            )

        report.duration_s = max(0.0, clock() - start)
        return report

    def _squeue_has_jobs_safely(self) -> bool:
        """Predicate for the squeue drain loop. Surfaces *exceptions*
        as 'not yet drained' rather than aborting cleanup - a transient
        SSH hiccup must not break the poll."""
        try:
            return bool(self.squeue_me(timeout=min(self.gateway.timeout, 5.0)))
        except (subprocess.TimeoutExpired, OSError):
            return True  # conservative: assume not drained

    def _cleanup_worker(
        self,
        *,
        worker: str,
        pkill_pattern: str,
    ) -> WorkerCleanupResult:
        """Run the per-worker container+process cleanup compound.

        The remote shell expression interpolates only constants and
        the ``pkill_pattern`` argument (which we shell-quote). It runs
        under the ssh-default remote shell - typically bash with
        ``-c``. We deliberately use ``set +e`` semantics inside the
        compound (no global ``set -e``) so a missing podman binary or
        empty container list doesn't abort downstream pkill.
        """
        result = WorkerCleanupResult(worker=worker)
        # Pre-quote the pkill pattern. ``pkill_pattern`` is a regex; we
        # only need shell-quoting (the regex is interpreted by pkill
        # itself, NOT by the shell). shlex.quote is sufficient even
        # for adversarial input - we still own the call site, but
        # belt-and-braces safety here costs nothing.
        quoted_pattern = shlex.quote(pkill_pattern)
        # Per-worker shell compound. Note: every command is
        # whitespace-bounded; no semicolons are interpolated from
        # caller input. ``$(id -u)`` runs on the worker as the SSH
        # user (rootless podman storage path).
        script = (
            "set +e\n"
            "ROOT_ARGS=\"--root /run/user/$(id -u)/storage "
            "--runroot /run/user/$(id -u)/runtime\"\n"
            "STOPPED_COUNT=0\n"
            "REMOVED_COUNT=0\n"
            "IDS=$(podman $ROOT_ARGS ps -aq 2>/dev/null)\n"
            "if [ -n \"$IDS\" ]; then\n"
            "  STOPPED_COUNT=$(printf '%s\\n' \"$IDS\" | "
            "xargs -r podman $ROOT_ARGS stop --time=5 2>/dev/null | "
            "wc -l)\n"
            "fi\n"
            "IDS=$(podman $ROOT_ARGS ps -aq 2>/dev/null)\n"
            "if [ -n \"$IDS\" ]; then\n"
            "  REMOVED_COUNT=$(printf '%s\\n' \"$IDS\" | "
            "xargs -r podman $ROOT_ARGS rm -f 2>/dev/null | "
            "wc -l)\n"
            "fi\n"
            f"pkill -KILL -f {quoted_pattern} 2>/dev/null\n"
            "PKILL_RC=$?\n"
            "echo STOPPED=$STOPPED_COUNT\n"
            "echo REMOVED=$REMOVED_COUNT\n"
            "echo PKILL_RC=$PKILL_RC\n"
        )
        try:
            cp = self.worker_ssh(
                worker, script, timeout=min(self.gateway.timeout, 30.0),
            )
        except subprocess.TimeoutExpired as exc:
            result.errors.append(f"cleanup compound timed out ({exc})")
            return result
        except OSError as exc:
            result.errors.append(f"cleanup compound: {exc}")
            return result

        if cp.returncode != 0 and not cp.stdout.strip():
            result.errors.append(
                f"cleanup compound rc={cp.returncode} "
                f"stderr={cp.stderr.strip()[:200]}",
            )
            return result

        # Parse the trailing ``key=value`` lines. The compound emits
        # exactly three; tolerate any extra noise (e.g. xargs warnings)
        # by ignoring lines without the expected ``=`` shape.
        for line in cp.stdout.splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            try:
                ivalue = int(value)
            except ValueError:
                continue
            if key == "STOPPED":
                result.containers_stopped = ivalue
            elif key == "REMOVED":
                result.containers_removed = ivalue
            elif key == "PKILL_RC":
                # pkill rc: 0 = matched (and killed), 1 = no match,
                # 2 = syntax error, 3 = fatal. 1 is the desired
                # terminal state. Anything else is an error worth
                # surfacing.
                if ivalue == 0:
                    result.processes_killed = 1
                elif ivalue == 1:
                    result.processes_killed = 0
                else:
                    result.errors.append(
                        f"pkill returned rc={ivalue}",
                    )
        return result

    def _poll_ports(
        self,
        *,
        workers: Sequence[str],
        ports: Sequence[int],
        deadline: float,
        interval: float,
        clock: Any,
        sleep: Any,
    ) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
        """Poll each ``(worker, port)`` until its listener is gone.

        Returns ``(released, still_bound)``. A pair lands in
        ``released`` as soon as a single probe shows it free; pairs
        that never go free by ``deadline`` end up in ``still_bound``.
        Errors during a single probe are treated as 'still bound' for
        that iteration (conservative: don't claim release on partial
        info).
        """
        if not workers or not ports:
            return [], []
        port_list = list(ports)
        targets: list[tuple[str, int]] = [
            (w, p) for w in workers for p in port_list
        ]
        released: list[tuple[str, int]] = []
        pending: list[tuple[str, int]] = list(targets)
        # Polled per-worker because port_listeners is per-worker.
        while pending and clock() < deadline:
            still_pending: list[tuple[str, int]] = []
            # Group pending by worker for one ss call per worker.
            by_worker: dict[str, list[int]] = {}
            for w, p in pending:
                by_worker.setdefault(w, []).append(p)
            for worker, w_ports in by_worker.items():
                try:
                    rows = self.port_listeners(
                        worker, w_ports,
                        timeout=min(self.gateway.timeout, 5.0),
                    )
                except (subprocess.TimeoutExpired, OSError, WorkerProbeError):
                    # Treat probe failure as 'still bound' for now;
                    # next iteration retries. Cleanup is best-effort and
                    # explicitly tolerates a transient probe failure
                    # here (the strict invariants enforce it elsewhere).
                    for p in w_ports:
                        still_pending.append((worker, p))
                    continue
                bound_ports = {r.local_port for r in rows}
                for p in w_ports:
                    if p in bound_ports:
                        still_pending.append((worker, p))
                    else:
                        released.append((worker, p))
            pending = still_pending
            if pending and clock() < deadline:
                sleep(interval)
        return released, list(pending)

    @staticmethod
    def _poll_until(
        *,
        check: Any,
        deadline: float,
        interval: float,
        clock: Any,
        sleep: Any,
    ) -> bool:
        """Generic poll: call ``check()`` until truthy or ``deadline``
        passes. Returns ``True`` iff ``check()`` returned truthy.
        ``sleep(interval)`` is invoked between iterations (NOT before
        the first call - the first probe should be immediate)."""
        while True:
            if check():
                return True
            if clock() >= deadline:
                return False
            sleep(interval)

    # -- read-only probes (cont.) -------------------------------------

    def processes_by_pattern(
        self,
        worker: str,
        pattern: str,
        *,
        timeout: float | None = None,
    ) -> list[ProcessRow]:
        """Run ``ps -eo pid,ppid,user,etime,cmd`` on ``worker`` and
        return rows whose ``cmd`` column matches ``pattern`` (Python
        regex, applied client-side).

        We do NOT pipe through ``grep -E`` server-side because (a) it
        introduces a shell dependency we'd have to quote ``pattern``
        for, and (b) doing the filter in Python keeps the SSH command
        a constant string and lets the caller use richer regex
        features without worrying about ``grep -E`` portability.
        """
        import re

        cmd = "ps -eo pid,ppid,user,etime,cmd --no-headers"
        cp = self.worker_ssh(worker, cmd, timeout=timeout)
        if cp.returncode != 0:
            raise WorkerProbeError(worker, cmd, cp.returncode, cp.stderr)
        rx = re.compile(pattern)
        rows: list[ProcessRow] = []
        for line in cp.stdout.splitlines():
            row = _parse_ps_row(line)
            if row is None:
                continue
            if rx.search(row.cmd):
                rows.append(row)
        return rows


# ---------------------------------------------------------------------------
# Parsers (module-private)
# ---------------------------------------------------------------------------


def _matches_glob(name: str, pattern: str) -> bool:
    """Glob-match for the scancel ``--jobname=`` pattern.

    Used purely for the `jobs_canceled` accounting in
    :class:`CleanupReport`: scancel itself does the real matching on
    the gateway. Defers to :mod:`fnmatch` so the semantics line up
    with what SLURM passes through to its glob layer.
    """
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)


def _parse_podman_json(stdout: str) -> list[PodmanRow]:
    """Parse ``podman ps --format=json`` output.

    Empty / whitespace stdout yields ``[]`` (podman emits ``[]\\n`` for
    no containers but older builds occasionally emit an empty body).
    """
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    rows: list[PodmanRow] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # podman's JSON shape is fairly stable: ``Id``, ``Names``,
        # ``Image``, ``State``, ``StartedAt`` (string), ``Labels``
        # (object). Older versions emit ``ID`` / ``Created`` instead;
        # we accept either.
        cid = str(entry.get("Id") or entry.get("ID") or "")
        names = entry.get("Names") or []
        if isinstance(names, list) and names:
            name = str(names[0])
        else:
            name = str(entry.get("Name") or "")
        image = str(entry.get("Image") or "")
        state = str(entry.get("State") or entry.get("Status") or "")
        started_at = str(
            entry.get("StartedAt") or entry.get("Created") or "",
        )
        raw_labels = entry.get("Labels") or {}
        if isinstance(raw_labels, dict):
            labels = {str(k): str(v) for k, v in raw_labels.items()}
        else:
            labels = {}
        rows.append(
            PodmanRow(
                id=cid,
                name=name,
                image=image,
                state=state,
                started_at=started_at,
                labels=labels,
                raw=entry,
            ),
        )
    return rows


def _parse_ss_lntpe(stdout: str) -> list[ListenerRow]:
    """Parse ``ss -lntpe`` output.

    Format (header line dropped):

    ::

        State Recv-Q Send-Q Local Address:Port Peer Address:Port Process

    The Process column may be empty (kernel listener), absent (no
    matching ``users:((...))`` field), or carry one or more
    ``users:(("name",pid=NNN,fd=NN))`` triples plus key=value pairs
    (``ino:``, ``uid:``, ``sk:``, ...). We extract the first matching
    process tuple plus the optional ``uid:`` field.
    """
    import re

    rows: list[ListenerRow] = []
    user_rx = re.compile(
        r'users:\(\("([^"]+)",pid=(\d+),fd=\d+\)',
    )
    uid_rx = re.compile(r'\buid:(\d+)\b')
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("State") or line.startswith("Netid"):
            continue
        # ``ss -lntpe`` columns are whitespace-separated; the Process
        # field is the rest of the line. We only care about the local
        # ``addr:port`` and the trailing process info.
        parts = line.split(None, 5)
        if len(parts) < 4:
            continue
        # When ``-n`` and ``-l`` are both set the first column is
        # ``State`` (e.g. ``LISTEN``). Older ``ss`` (-lntp without -e)
        # drops Recv-Q/Send-Q; tolerate both shapes.
        local: str
        if parts[0].upper() == "LISTEN" and len(parts) >= 5:
            local = parts[3]
            tail = parts[5] if len(parts) > 5 else ""
        else:
            # Defensive: take the 4th whitespace token as local addr
            # if the layout doesn't match what we expect.
            local = parts[3]
            tail = parts[5] if len(parts) > 5 else ""
        if ":" not in local:
            continue
        addr, _, port_s = local.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        m = user_rx.search(tail)
        if m is not None:
            proc: str | None = m.group(1)
            pid: int | None = int(m.group(2))
        else:
            proc, pid = None, None
        m_uid = uid_rx.search(tail)
        uid = int(m_uid.group(1)) if m_uid else None
        rows.append(
            ListenerRow(
                proto="tcp",
                local_address=addr,
                local_port=port,
                pid=pid,
                process=proc,
                uid=uid,
            ),
        )
    return rows


def _parse_ps_row(line: str) -> ProcessRow | None:
    """Parse one ``ps -eo pid,ppid,user,etime,cmd --no-headers`` row.

    The ``cmd`` column may contain whitespace; we do a 5-way split
    (``maxsplit=4``) so it's preserved verbatim.
    """
    parts = line.strip().split(None, 4)
    if len(parts) < 5:
        return None
    pid_s, ppid_s, user, etime, cmd = parts
    try:
        pid = int(pid_s)
        ppid = int(ppid_s)
    except ValueError:
        return None
    return ProcessRow(
        pid=pid, ppid=ppid, user=user, etime=etime, cmd=cmd,
    )

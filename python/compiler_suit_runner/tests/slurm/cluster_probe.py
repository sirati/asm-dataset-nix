"""SSH-based read-only probes for the local SLURM test environment.

The probes here are a thin wrapper over the OpenSSH client. They cover
the read-side primitives the per-run invariant checker (and downstream
preflight code) needs:

- gateway / worker shell execution via explicit ``-i`` key (never via
  ``ssh-agent`` or ``~/.ssh/`` per project policy);
- ``squeue``/``sinfo`` parsing scoped to the current SSH user;
- ``podman ps -a`` enumeration with full label set on each worker;
- listener / process inspection (``ss``, ``ps``) for leak detection.

Cleanup / mutating actions (``scancel``, ``podman rm``, ``pkill``) are
deliberately **not** part of this module - the cleanup harness lives in
a separate file owned by another batch and re-uses ``ClusterProbe`` for
its read-side checks.

All commands are dispatched through :class:`subprocess.run` with
``shell=False`` and an explicit argv vector so neither the gateway nor
the workers ever see a shell-quoted string assembled from caller input.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
from typing import Any, Final, Iterable, Mapping

__all__ = [
    "GatewayConfig",
    "ClusterProbe",
    "SqueueRow",
    "SinfoRow",
    "PodmanRow",
    "ListenerRow",
    "ProcessRow",
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
        """Build an ``ssh -J <gateway> <worker> <remote_cmd>`` argv.

        ``worker`` is the bare hostname; the SLURM-test-env DNS resolves
        ``slurm-worker{1..4}`` from inside the gateway namespace.
        """
        argv: list[str] = ["ssh"]
        if self.gateway.identity_file is not None:
            argv += ["-i", self.gateway.identity_file]
        # ProxyJump target carries its own port: ``user@host:port``.
        proxy = f"{self.gateway.host}:{self.gateway.port}"
        argv += ["-J", proxy]
        argv += list(_BASE_SSH_OPTS)
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
            return []
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
        cp = self.worker_ssh(worker, "ss -lntpe", timeout=timeout)
        if cp.returncode != 0:
            return []
        return [
            row for row in _parse_ss_lntpe(cp.stdout)
            if row.local_port in port_set
        ]

    # -- ps ------------------------------------------------------------

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

        cp = self.worker_ssh(
            worker,
            "ps -eo pid,ppid,user,etime,cmd --no-headers",
            timeout=timeout,
        )
        if cp.returncode != 0:
            return []
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

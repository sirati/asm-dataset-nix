"""Unit + opt-in live tests for :mod:`cluster_probe`.

Most assertions verify *command construction* (so the cluster never
sees a malformed ssh invocation, ProxyJump string, or shell-quoted
identifier) and *output parsing* (so a SLURM / podman / ss minor
version bump that breaks the format is caught here, not five layers
deep in an invariant check).

A single live test runs ``squeue --me`` against the real local SLURM
test cluster IF :meth:`ClusterProbe.is_reachable` returns ``True``.
When the cluster is down (its bring-up is asynchronous w.r.t. this
test run) the live test skips with a clear message instead of
erroring.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from compiler_suit_runner.tests.slurm.cluster_probe import (
    DEFAULT_CLEANUP_PKILL_PATTERN,
    DEFAULT_CLEANUP_PORTS,
    DEFAULT_CLEANUP_WORKERS,
    DEFAULT_SCANCEL_PATTERN,
    CleanupReport,
    ClusterProbe,
    GatewayConfig,
    PodmanRow,
    SinfoRow,
    SqueueRow,
    WorkerCleanupResult,
    _parse_podman_json,
    _parse_ps_row,
    _parse_ss_lntpe,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

LIVE_KEY_PATH = "/home/sirati/devel/nix/asm-dataset-nix/.ssh-debug/id_ed25519"
"""Path to the live test-env SSH key.

Per project memory the key is ephemeral, must NOT be added to
``ssh-agent`` or ``~/.ssh/``, and must be passed to every ssh call via
``-i``.
"""


def _make_probe(identity: str | None = "/tmp/dummy-key") -> ClusterProbe:
    """Build a probe with a deterministic gateway config for unit tests."""
    return ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=identity,
            timeout=7.5,
        ),
    )


def _completed(
    stdout: str = "",
    stderr: str = "",
    rc: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Command construction: gateway
# ---------------------------------------------------------------------------


def test_gateway_argv_uses_explicit_identity_and_port() -> None:
    probe = _make_probe(identity="/tmp/key")
    argv = probe._gateway_argv("echo ok")
    # IdentityFile must be the very first option after ``ssh`` so it
    # cannot be shadowed by ssh-agent (paired with IdentitiesOnly=yes).
    assert argv[0] == "ssh"
    assert argv[1] == "-i"
    assert argv[2] == "/tmp/key"
    # Port must be passed via ``-p``, NOT embedded in the host string,
    # so OpenSSH applies it correctly even with ssh_config overrides.
    assert "-p" in argv
    p_idx = argv.index("-p")
    assert argv[p_idx + 1] == "2244"
    # IdentitiesOnly is critical - without it the agent could win.
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=no" in argv
    assert "BatchMode=yes" in argv
    # The remote command is the last element, not shell-joined into
    # the SSH options.
    assert argv[-1] == "echo ok"
    assert argv[-2] == "sirati@localhost"


def test_gateway_argv_no_identity_when_unset() -> None:
    probe = _make_probe(identity=None)
    argv = probe._gateway_argv("hostname")
    assert "-i" not in argv


def test_gateway_argv_does_not_invoke_shell() -> None:
    """The remote command travels as a single argv element; nothing in
    the argv vector itself is a shell metachar that would be expanded
    locally. (OpenSSH then re-evaluates the remote string under the
    target shell - that's its contract.)"""
    probe = _make_probe()
    argv = probe._gateway_argv("$(rm -rf /); echo done")
    assert argv[-1] == "$(rm -rf /); echo done"
    # No element starts with a leading shell-pipe / redirection char,
    # which would indicate someone constructed a shell string by
    # accident.
    for el in argv[:-1]:
        assert not el.startswith("|")
        assert not el.startswith(">")


# ---------------------------------------------------------------------------
# Command construction: worker (ProxyJump)
# ---------------------------------------------------------------------------


def test_worker_argv_uses_proxycommand_with_port() -> None:
    probe = _make_probe(identity="/tmp/k")
    argv = probe._worker_argv("slurm-worker3", "uname -a")
    # We use ``ProxyCommand`` rather than ``-J`` so the gateway-hop
    # SSH inherits the same hardened options (StrictHostKeyChecking=no,
    # BatchMode=yes, IdentitiesOnly=yes, …) as the outer worker SSH;
    # the inner ``-J`` ssh would otherwise pick up only the user's
    # default config, which on a freshly re-keyed slurm-test-env
    # surfaces as ``Host key verification failed`` for the inner hop.
    pc_idx = argv.index("ProxyCommand=", 0, len(argv) - 2) if False else None
    pc_entries = [e for e in argv if e.startswith("ProxyCommand=")]
    assert len(pc_entries) == 1
    pc = pc_entries[0]
    # The proxy command must encode the gateway port; OpenSSH falls
    # back to 22 silently when no port is present.
    assert "-p 2244" in pc
    assert "sirati@localhost" in pc
    # And it must end with -W %h:%p so the outer ssh forwards the
    # final hop's stream through the gateway.
    assert "-W %h:%p" in pc

    # The final hop is the bare worker hostname; the gateway resolves
    # it via internal DNS.
    assert argv[-2] == "slurm-worker3"
    assert argv[-1] == "uname -a"
    # The identity flag is still on the outer hop - OpenSSH applies
    # it to the worker hop because IdentitiesOnly=yes is shared.
    assert argv[1] == "-i"
    assert argv[2] == "/tmp/k"


def test_worker_argv_does_not_pass_p_for_worker() -> None:
    """``-p`` is only meaningful for the *direct* hop. The worker hop
    uses default port 22; the gateway hop's port lives in the
    ``ProxyCommand`` string."""
    probe = _make_probe()
    argv = probe._worker_argv("slurm-worker1", "true")
    # No ``-p`` on the outer ssh argv (the ``-p 2244`` lives inside
    # the ``ProxyCommand=...`` string, not as an outer option).
    outer_only = [
        a for a in argv if not a.startswith("ProxyCommand=")
    ]
    assert "-p" not in outer_only


# ---------------------------------------------------------------------------
# subprocess.run integration
# ---------------------------------------------------------------------------


def test_run_uses_capture_text_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(stdout="hello\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    cp = probe.gateway_ssh("echo hello")
    assert cp.stdout == "hello\n"
    kw = captured["kwargs"]
    assert kw["capture_output"] is True
    assert kw["text"] is True
    # Default timeout flows from GatewayConfig (7.5s in our fixture).
    assert kw["timeout"] == pytest.approx(7.5)
    # ``check`` must default to False so probes can return non-zero
    # exit codes for parse-and-decide handling upstream.
    assert kw["check"] is False


def test_run_explicit_timeout_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return _completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    _make_probe().gateway_ssh("hostname", timeout=2.0)
    assert captured["timeout"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# is_reachable
# ---------------------------------------------------------------------------


def test_is_reachable_true_when_echo_returns_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout="ok\n"),
    )
    assert _make_probe().is_reachable() is True


def test_is_reachable_false_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout="", stderr="refused", rc=255),
    )
    assert _make_probe().is_reachable() is False


def test_is_reachable_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", boom)
    assert _make_probe().is_reachable() is False


def test_is_reachable_false_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("ssh binary missing")

    monkeypatch.setattr(subprocess, "run", boom)
    assert _make_probe().is_reachable() is False


def test_is_reachable_false_on_unexpected_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout="something else\n"),
    )
    assert _make_probe().is_reachable() is False


# ---------------------------------------------------------------------------
# squeue parsing
# ---------------------------------------------------------------------------


SQUEUE_SAMPLE = (
    "1234|debug|asm-secondary-0|RUNNING|kruppb|slurm-worker1\n"
    "1235|debug|asm-secondary-1|PENDING|kruppb|(Resources)\n"
)


def test_squeue_me_parses_pipe_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout=SQUEUE_SAMPLE)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = _make_probe().squeue_me()
    assert len(rows) == 2
    assert rows[0] == SqueueRow(
        jobid="1234",
        partition="debug",
        name="asm-secondary-0",
        state="RUNNING",
        user="kruppb",
        nodelist="slurm-worker1",
    )
    assert rows[1].state == "PENDING"
    assert rows[1].nodelist == "(Resources)"
    # Format string flows through unmodified - no shell injection
    # gymnastics, just the documented squeue placeholders. Confirm
    # ``--me`` is set so we never accidentally widen the query.
    last = captured["argv"][-1]
    assert "--me" in last
    assert "--noheader" in last
    assert "%i|%P|%j|%T|%u|%R" in last


def test_squeue_me_empty_when_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _completed(stdout=""))
    assert _make_probe().squeue_me() == []


def test_squeue_me_returns_empty_on_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(rc=255, stderr="connection refused"),
    )
    assert _make_probe().squeue_me() == []


def test_squeue_me_skips_malformed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wrong field count -> dropped silently rather than crashing the
    # whole probe. Both rows here are short; only the well-formed
    # second sample below is kept.
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(
            stdout="short|row\n1|debug|name|RUNNING|user|node\nincomplete|3fields\n",
        ),
    )
    rows = _make_probe().squeue_me()
    assert len(rows) == 1
    assert rows[0].jobid == "1"


# ---------------------------------------------------------------------------
# sinfo parsing
# ---------------------------------------------------------------------------


SINFO_SAMPLE = (
    "slurm-worker1|debug*|idle\n"
    "slurm-worker2|debug*|idle\n"
    "slurm-worker3|debug*|alloc\n"
    "slurm-worker4|debug*|down*\n"
)


def test_sinfo_nodes_parses_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout=SINFO_SAMPLE),
    )
    rows = _make_probe().sinfo_nodes()
    assert [r.node for r in rows] == [
        "slurm-worker1",
        "slurm-worker2",
        "slurm-worker3",
        "slurm-worker4",
    ]
    assert rows[2] == SinfoRow(
        node="slurm-worker3", partition="debug*", state="alloc",
    )
    assert rows[3].state == "down*"


# ---------------------------------------------------------------------------
# podman parsing
# ---------------------------------------------------------------------------


PODMAN_JSON_SAMPLE = json.dumps(
    [
        {
            "Id": "abc123",
            "Names": ["asm-secondary-0"],
            "Image": "localhost/slurm-test:latest",
            "State": "running",
            "StartedAt": "2026-05-08T10:00:00Z",
            "Labels": {
                "asm.run_id": "run_20260508_100000",
                "asm.role": "secondary",
            },
        },
        {
            "Id": "def456",
            "Names": ["leftover-from-smoke16"],
            "Image": "localhost/slurm-test:latest",
            "State": "exited",
            "StartedAt": "2026-05-07T22:13:00Z",
            "Labels": None,
        },
    ],
)


def test_podman_ps_parses_full_set(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout=PODMAN_JSON_SAMPLE)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = _make_probe().podman_ps("slurm-worker2")
    # ProxyCommand form must be present: rules out an accidental direct
    # connect to the worker (which would not be reachable).
    assert any(
        a.startswith("ProxyCommand=") for a in captured["argv"]
    ), captured["argv"]
    assert len(rows) == 2
    first = rows[0]
    assert isinstance(first, PodmanRow)
    assert first.id == "abc123"
    assert first.name == "asm-secondary-0"
    assert first.state == "running"
    assert first.labels["asm.run_id"] == "run_20260508_100000"
    # Exited container with Labels=null falls back to {}.
    assert rows[1].labels == {}
    # The ``raw`` payload survives as the original dict so callers
    # can read fields we don't surface explicitly.
    assert rows[0].raw["Image"].endswith("slurm-test:latest")


def test_podman_ps_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout="[]\n"),
    )
    assert _make_probe().podman_ps("slurm-worker1") == []


def test_podman_ps_uses_rootless_paths_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _make_probe().podman_ps("slurm-worker1")
    cmd = captured["argv"][-1]
    assert "/run/user/$(id -u)/storage" in cmd
    assert "/run/user/$(id -u)/runtime" in cmd
    assert cmd.endswith("podman ps -a --format=json") or "ps -a --format=json" in cmd


def test_podman_ps_rootful_skips_root_runroot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _make_probe().podman_ps("slurm-worker1", rootless=False)
    cmd = captured["argv"][-1]
    assert "/run/user" not in cmd
    assert cmd == "podman ps -a --format=json"


def test_parse_podman_json_handles_missing_id_field() -> None:
    # Some podman versions emit ``ID`` rather than ``Id``.
    raw = json.dumps([{"ID": "xyz", "Names": ["x"], "State": "running"}])
    rows = _parse_podman_json(raw)
    assert rows[0].id == "xyz"


def test_parse_podman_json_handles_garbage() -> None:
    assert _parse_podman_json("not json") == []
    assert _parse_podman_json("") == []
    assert _parse_podman_json("{}") == []  # object, not list


# ---------------------------------------------------------------------------
# port_listeners parsing
# ---------------------------------------------------------------------------


SS_LNTPE_SAMPLE = (
    "State    Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    "LISTEN   0      4096   0.0.0.0:5000       0.0.0.0:*         users:((\"harmonia\",pid=12345,fd=7)) uid:1000 ino:99 sk:abcd\n"  # noqa: E501
    "LISTEN   0      4096   0.0.0.0:5050       0.0.0.0:*         users:((\"peer_push\",pid=23456,fd=4)) uid:1000\n"  # noqa: E501
    "LISTEN   0      128    127.0.0.1:631      0.0.0.0:*         \n"
    "LISTEN   0      511    *:22               *:*               users:((\"sshd\",pid=1024,fd=3)) uid:0\n"  # noqa: E501
)


def test_parse_ss_lntpe_extracts_pid_and_uid() -> None:
    rows = _parse_ss_lntpe(SS_LNTPE_SAMPLE)
    by_port = {r.local_port: r for r in rows}
    assert 5000 in by_port
    assert by_port[5000].process == "harmonia"
    assert by_port[5000].pid == 12345
    assert by_port[5000].uid == 1000
    assert 5050 in by_port
    assert by_port[5050].process == "peer_push"
    # Kernel-only listener (no users:(()) -> no pid/process.
    assert by_port[631].pid is None
    assert by_port[631].process is None
    assert by_port[22].uid == 0


def test_port_listeners_filters_by_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(stdout=SS_LNTPE_SAMPLE),
    )
    rows = _make_probe().port_listeners(
        "slurm-worker1", ports=[5000, 5050],
    )
    assert {r.local_port for r in rows} == {5000, 5050}
    # Filter must drop port 22 / 631 even though they're listening.
    for r in rows:
        assert r.local_port in {5000, 5050}


def test_port_listeners_empty_when_no_ports_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Important: passing an empty iterable must short-circuit BEFORE
    # running the SSH command (saves one round-trip + handles the
    # silly case explicitly).
    called: list[bool] = []

    def fake_run(*_a: Any, **_kw: Any) -> Any:
        called.append(True)
        return _completed(stdout=SS_LNTPE_SAMPLE)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = _make_probe().port_listeners("slurm-worker1", ports=[])
    assert rows == []
    assert called == []


def test_port_listeners_raises_worker_probe_error_on_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leak-check probes must surface ``rc != 0`` as a hard error so
    the invariants don't silent-pass when the SSH path itself is
    broken (stale known_hosts, missing pubkey, network partition).
    Cleanup polling and other lenient callers catch the exception
    explicitly.
    """
    from compiler_suit_runner.tests.slurm.cluster_probe import (
        WorkerProbeError,
    )

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(rc=255, stderr="boom"),
    )
    with pytest.raises(WorkerProbeError) as excinfo:
        _make_probe().port_listeners("slurm-worker1", ports=[5000])
    assert excinfo.value.worker == "slurm-worker1"
    assert excinfo.value.rc == 255
    assert "boom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# processes_by_pattern parsing
# ---------------------------------------------------------------------------


PS_SAMPLE = (
    "  123     1 sirati 00:01:23 /usr/bin/python3 -m compiler_suit_runner.suit_task\n"
    "  456    66972 sirati 00:00:55 /usr/local/bin/harmonia-cache --config /etc/harmonia.toml\n"  # noqa: E501
    "  789     1 root   12:34:56 /usr/sbin/sshd -D\n"
)


def test_parse_ps_row_handles_multitoken_cmd() -> None:
    row = _parse_ps_row(
        "  123     1 sirati 00:01:23 /usr/bin/python3 -m compiler_suit_runner.suit_task",
    )
    assert row is not None
    assert row.pid == 123
    assert row.ppid == 1
    assert row.user == "sirati"
    assert row.etime == "00:01:23"
    assert row.cmd.endswith("compiler_suit_runner.suit_task")


def test_parse_ps_row_rejects_short() -> None:
    assert _parse_ps_row("just two tokens") is None
    assert _parse_ps_row("") is None


def test_processes_by_pattern_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout=PS_SAMPLE)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rows = _make_probe().processes_by_pattern(
        "slurm-worker1",
        r"compiler_suit_runner|harmonia-cache",
    )
    assert len(rows) == 2
    assert {r.pid for r in rows} == {123, 456}
    # ProcessRow surfaces the PPID so a "no PPID=1 leak" check is
    # trivial upstream.
    pid_456 = next(r for r in rows if r.pid == 456)
    assert pid_456.ppid == 66972
    # ``ps`` runs without a server-side grep - the SSH command stays
    # constant which lets the regex be passed without quoting tricks.
    assert captured["argv"][-1] == (
        "ps -eo pid,ppid,user,etime,cmd --no-headers"
    )


def test_processes_by_pattern_raises_worker_probe_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same hard-fail contract as :func:`port_listeners`: a probe SSH
    failure must surface as ``WorkerProbeError`` so the leak invariant
    can't silent-pass when the SSH path is broken."""
    from compiler_suit_runner.tests.slurm.cluster_probe import (
        WorkerProbeError,
    )

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(rc=1, stderr="oops"),
    )
    with pytest.raises(WorkerProbeError) as excinfo:
        _make_probe().processes_by_pattern(
            "slurm-worker1", r"compiler_suit_runner",
        )
    assert excinfo.value.worker == "slurm-worker1"
    assert excinfo.value.rc == 1
    assert "oops" in str(excinfo.value)


# ---------------------------------------------------------------------------
# argv assembly: end-to-end smoke through subprocess.run mock
# ---------------------------------------------------------------------------


def test_gateway_ssh_calls_subprocess_run_with_full_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe(identity="/etc/secret-key")
    probe.gateway_ssh("hostname")
    argv = captured["argv"]
    # Spot-check the assembled order to lock the contract: ssh, -i KEY,
    # -p PORT, then options, then host, then remote cmd.
    assert argv[0] == "ssh"
    assert (argv[1], argv[2]) == ("-i", "/etc/secret-key")
    p = argv.index("-p")
    assert argv[p + 1] == "2244"
    assert argv[-2] == "sirati@localhost"
    assert argv[-1] == "hostname"


# ---------------------------------------------------------------------------
# Cleanup harness
# ---------------------------------------------------------------------------


class _CleanupRecorder:
    """Replaces :func:`subprocess.run` for cleanup tests.

    Returns scripted responses keyed by what kind of remote command
    the cluster probe issued (gateway echo / scancel / squeue, or a
    per-worker compound). Captures every argv passed so tests can
    assert the safety-critical bits (no ``--user=kruppb``, rootless
    podman paths, full pkill regex).
    """

    def __init__(
        self,
        *,
        is_reachable: bool = True,
        squeue_initial: str = "1234|debug|asm-secondary-0|RUNNING|sirati|slurm-worker1\n",
        squeue_after_drain: str = "",
        worker_compound_stdout: str = (
            "STOPPED=2\nREMOVED=2\nPKILL_RC=0\n"
        ),
        port_listeners_seq: list[str] | None = None,
    ) -> None:
        self.is_reachable = is_reachable
        self.squeue_initial = squeue_initial
        self.squeue_after_drain = squeue_after_drain
        self.worker_compound_stdout = worker_compound_stdout
        # ``port_listeners_seq`` is consumed in order: first call returns
        # element 0, second returns element 1, ... The default sequence
        # has each port unbound from the start.
        self.port_listeners_seq = list(
            port_listeners_seq or [""] * 32,
        )
        self.calls: list[dict[str, Any]] = []
        self._squeue_call_count = 0

    def __call__(
        self, argv: list[str], **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        # Every probe invokes ssh; the actual remote command is the
        # last argv element (the only one that's a single shell
        # string). Classify based on its content.
        remote = argv[-1] if argv else ""
        record = {"argv": argv, "kwargs": kwargs, "remote": remote}
        self.calls.append(record)

        if remote == "echo ok":
            if self.is_reachable:
                return _completed(stdout="ok\n")
            return _completed(stdout="", stderr="refused", rc=255)

        if remote.startswith("squeue --me"):
            self._squeue_call_count += 1
            if self._squeue_call_count == 1:
                return _completed(stdout=self.squeue_initial)
            return _completed(stdout=self.squeue_after_drain)

        if remote.startswith("scancel"):
            return _completed(stdout="", rc=0)

        if remote == "ss -lntpe":
            stdout = (
                self.port_listeners_seq.pop(0)
                if self.port_listeners_seq
                else ""
            )
            return _completed(stdout=stdout)

        # Anything else is the per-worker cleanup compound.
        return _completed(stdout=self.worker_compound_stdout)


def _instant_clock() -> Any:
    """Monotonic clock that advances 0.1s per call (enough to exit
    poll loops promptly without any real sleep)."""
    state = {"t": 0.0}

    def _now() -> float:
        state["t"] += 0.1
        return state["t"]

    return _now


def test_cleanup_unreachable_returns_error_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder(is_reachable=False)
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    sleeps: list[float] = []
    report = probe.cleanup(
        clock=_instant_clock(),
        sleep=lambda s: sleeps.append(s),
    )
    assert isinstance(report, CleanupReport)
    assert report.reachable is False
    assert report.errors == ["cluster unreachable"]
    # No scancel / worker / port ssh calls were issued: only the
    # reachability echo.
    assert len(rec.calls) == 1
    assert rec.calls[0]["remote"] == "echo ok"


def test_cleanup_scancel_uses_jobname_filter_not_user_kruppb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Safety-critical: ``--user=kruppb`` would scancel the
    asm-tokenizer peer's jobs. The cleanup MUST scope by job-name
    glob and ``$(whoami)``, never by hard-coded user."""
    rec = _CleanupRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    probe.cleanup(
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
        workers=(),  # skip worker pass for this isolated check
    )

    scancel_calls = [
        c for c in rec.calls if c["remote"].startswith("scancel")
    ]
    assert len(scancel_calls) == 1
    cmd = scancel_calls[0]["remote"]
    # Job-name pattern: literal, shell-quoted via shlex (no spaces in
    # the default pattern, so quoting is a no-op except for the wrap).
    assert "--jobname=" in cmd
    assert "asm-secondary-*" in cmd
    # User scoping comes from $(whoami), evaluated remote-side.
    assert "$(whoami)" in cmd
    # The forbidden form: hard-coded user.
    assert "--user=kruppb" not in cmd
    assert "kruppb" not in cmd


def test_cleanup_scancel_pattern_override_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    probe.cleanup(
        scancel_pattern="asm-other-*",
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
        workers=(),
    )
    scancel_calls = [
        c for c in rec.calls if c["remote"].startswith("scancel")
    ]
    assert len(scancel_calls) == 1
    assert "asm-other-*" in scancel_calls[0]["remote"]


def test_cleanup_worker_compound_uses_rootless_podman_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    probe.cleanup(
        workers=("slurm-worker1",),
        ports=(),  # skip port poll - covered separately
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    # Find the worker-compound call (last 5 slot of argv won't match
    # echo/scancel/squeue/ss above).
    compound_calls = [
        c for c in rec.calls
        if c["remote"].startswith("set +e")
    ]
    assert len(compound_calls) == 1
    script = compound_calls[0]["remote"]
    # Rootless podman storage path AND runroot - the test-env uses
    # rootless; rootful paths would silently target the wrong store.
    assert "/run/user/$(id -u)/storage" in script
    assert "/run/user/$(id -u)/runtime" in script
    # Container stop+rm sequence (xargs -r so empty input is a no-op).
    assert "podman $ROOT_ARGS stop --time=5" in script
    assert "podman $ROOT_ARGS rm -f" in script
    assert "xargs -r" in script
    # pkill targets the leaky-process families.
    assert "pkill -KILL -f" in script
    for needle in (
        "compiler_suit_runner",
        "harmonia-cache",
        "peer_push",
    ):
        assert needle in script
    # The compound is reached via ProxyCommand; the worker hop carries
    # no ``-p`` (worker port is default 22 inside the gateway
    # namespace).
    argv = compound_calls[0]["argv"]
    assert any(a.startswith("ProxyCommand=") for a in argv), argv
    assert argv[-2] == "slurm-worker1"


def test_cleanup_pkill_pattern_override_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    probe.cleanup(
        workers=("slurm-worker1",),
        ports=(),
        pkill_pattern="my-extra-pattern",
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    compound = next(
        c for c in rec.calls if c["remote"].startswith("set +e")
    )
    # shlex.quote leaves metachar-free strings unquoted; what matters
    # is that the literal pattern string is part of the pkill argv
    # word and the default pkill families are no longer present.
    assert "pkill -KILL -f my-extra-pattern" in compound["remote"]
    # The three default-family needles must NOT be there - we
    # overrode the pattern, the override must take effect.
    assert "compiler_suit_runner" not in compound["remote"]
    assert "harmonia-cache" not in compound["remote"]
    assert "peer_push" not in compound["remote"]


def test_cleanup_pkill_pattern_with_dangerous_metachars_is_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even though we own the call site, an injection-safe quoting
    contract avoids accidents during future refactors."""
    rec = _CleanupRecorder()
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    nasty = "foo'; rm -rf /; echo 'pwned"
    probe.cleanup(
        workers=("slurm-worker1",),
        ports=(),
        pkill_pattern=nasty,
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    compound = next(
        c for c in rec.calls if c["remote"].startswith("set +e")
    )
    script = compound["remote"]
    # The literal substring ``rm -rf /`` should not appear OUTSIDE a
    # quoted region. The simplest invariant: ``rm -rf /`` is preceded
    # by a ``'\''`` shell-quote-escape, which is what shlex.quote
    # emits when the input contains a literal single quote.
    assert "rm -rf /" in script  # the string IS in the script ...
    # ... but only as part of the shell-quoted argument to pkill.
    # Confirm pkill sees the whole thing as a single shell word by
    # checking that every "rm -rf /" instance is sandwiched between
    # the shlex.quote sentinel "'\\''" (ANSI C-style escape pieces).
    # shlex.quote on "foo'; rm -rf /; echo 'pwned" yields
    # "'foo'\\''; rm -rf /; echo '\\''pwned'".
    expected_quoted = shlex_quote_helper(nasty)
    assert expected_quoted in script
    # And the script does NOT contain the metachars un-escaped at the
    # top level - every dangerous semicolon must come from inside
    # the quoted form.
    occurrences = script.count("rm -rf /")
    assert occurrences == expected_quoted.count("rm -rf /")


def shlex_quote_helper(s: str) -> str:
    """Local shadow of :func:`shlex.quote` used in the test - pulled
    in via a tiny helper so the test reads as 'compare to what the
    code SHOULD have produced' rather than re-implementing quoting
    inline."""
    import shlex as _shlex

    return _shlex.quote(s)


def test_cleanup_parses_compound_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder(
        worker_compound_stdout=(
            "STOPPED=4\nREMOVED=4\nPKILL_RC=1\n"  # rc=1: nothing matched
        ),
    )
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    report = probe.cleanup(
        workers=("slurm-worker1", "slurm-worker2"),
        ports=(),
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    assert set(report.per_worker.keys()) == {
        "slurm-worker1", "slurm-worker2",
    }
    # Both workers report 4 stopped + 4 removed; aggregate properties
    # must sum.
    assert report.containers_stopped == 8
    assert report.containers_removed == 8
    # PKILL_RC=1 means nothing matched (terminal state, not an error).
    assert report.processes_killed == 0
    # No worker errors when the compound succeeded.
    for worker_result in report.per_worker.values():
        assert worker_result.errors == []
        assert isinstance(worker_result, WorkerCleanupResult)


def test_cleanup_aggregates_pkill_rc0_as_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _CleanupRecorder(
        worker_compound_stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=0\n",
    )
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    report = probe.cleanup(
        workers=("slurm-worker1",),
        ports=(),
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    # PKILL_RC=0 means at least one process matched → killed=1 per worker.
    assert report.processes_killed == 1


def test_cleanup_compound_failure_recorded_per_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the per-worker compound fails (rc != 0 and empty stdout),
    the error attaches to that worker AND to the aggregate
    ``errors`` list - so cleanup of OTHER workers still proceeds."""
    call_count = {"n": 0}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(stdout="")
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            call_count["n"] += 1
            if "slurm-worker2" in argv:
                # Simulate ssh-side failure: rc!=0 with empty stdout.
                return _completed(stdout="", stderr="boom", rc=255)
            return _completed(stdout="STOPPED=1\nREMOVED=1\nPKILL_RC=0\n")
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    report = probe.cleanup(
        workers=("slurm-worker1", "slurm-worker2"),
        ports=(),
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    assert call_count["n"] == 2  # we did NOT short-circuit
    assert report.per_worker["slurm-worker1"].containers_stopped == 1
    assert report.per_worker["slurm-worker2"].containers_stopped == 0
    assert any(
        "slurm-worker2" in e for e in report.errors
    )
    # slurm-worker1 succeeded; no worker-level error for it.
    assert report.per_worker["slurm-worker1"].errors == []


def test_cleanup_squeue_drain_polled_until_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain loop calls ``squeue --me`` repeatedly. We seed three
    non-empty responses then an empty one - the loop must report
    ``squeue_drained=True`` once it sees empty."""
    squeue_responses = [
        "1|debug|asm-secondary-0|RUNNING|sirati|slurm-worker1\n",
        "1|debug|asm-secondary-0|RUNNING|sirati|slurm-worker1\n",
        "",  # drained
    ]

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(
                stdout=squeue_responses.pop(0)
                if squeue_responses else "",
            )
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            return _completed(stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n")
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    sleeps: list[float] = []
    report = probe.cleanup(
        workers=(),
        ports=(),
        timeout_s=120.0,
        squeue_poll_interval=0.5,
        clock=_instant_clock(),
        sleep=lambda s: sleeps.append(s),
    )
    assert report.squeue_drained is True
    # We slept between the first non-empty probe and the next, so at
    # least one sleep should have been issued.
    assert any(s == pytest.approx(0.5) for s in sleeps)


def test_cleanup_squeue_drain_respects_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If squeue stays non-empty past the budget, the drain reports
    failure but cleanup still proceeds."""

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(
                stdout="1|debug|asm-secondary-0|RUNNING|sirati|slurm-worker1\n",
            )
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            return _completed(stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n")
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    # Tight clock: each call advances by 0.5s; the squeue inner
    # deadline = start_after_scancel + 60s, so we'd need ~120 ticks
    # to exhaust it. Keep it small.
    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 30.0  # huge step so the 60s budget exhausts fast
        return ticks["t"]

    report = probe.cleanup(
        workers=(),
        ports=(),
        timeout_s=200.0,
        squeue_poll_interval=0.1,
        clock=clock,
        sleep=lambda _: None,
    )
    assert report.squeue_drained is False
    # Aggregate errors carry a clear marker.
    assert any("squeue" in e for e in report.errors)


def test_cleanup_ports_polled_until_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ports must be checked repeatedly. Seed: first probe shows port
    bound, second shows it free."""
    ss_seq = [
        # First probe: 5000 still bound by harmonia.
        "State    Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        "LISTEN   0      4096   0.0.0.0:5000       0.0.0.0:*         "
        "users:((\"harmonia\",pid=1,fd=3))\n"
        "LISTEN   0      4096   0.0.0.0:5050       0.0.0.0:*         \n",
        # Second probe: clean.
        "State    Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n",
    ]

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(stdout="")
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            return _completed(stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n")
        if remote == "ss -lntpe":
            return _completed(stdout=ss_seq.pop(0) if ss_seq else "")
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    sleeps: list[float] = []
    report = probe.cleanup(
        workers=("slurm-worker1",),
        ports=(5000, 5050),
        timeout_s=60.0,
        ports_poll_interval=0.5,
        clock=_instant_clock(),
        sleep=lambda s: sleeps.append(s),
    )
    # Both ports eventually released; ports_still_bound is empty.
    assert report.ports_still_bound == []
    # 5050 was released on the first probe; 5000 on the second.
    assert ("slurm-worker1", 5000) in report.ports_released
    assert ("slurm-worker1", 5050) in report.ports_released


def test_cleanup_ports_timeout_records_still_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a port never releases within the budget, it lands in
    ``ports_still_bound`` AND adds an error to the aggregate."""
    bound = (
        "State    Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        "LISTEN   0      4096   0.0.0.0:5000       0.0.0.0:*         "
        "users:((\"harmonia\",pid=1,fd=3))\n"
    )

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(stdout="")
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            return _completed(stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n")
        if remote == "ss -lntpe":
            return _completed(stdout=bound)
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    # Force the ports phase to exhaust quickly: clock jumps 60s/call.
    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 60.0
        return ticks["t"]

    report = probe.cleanup(
        workers=("slurm-worker1",),
        ports=(5000,),
        timeout_s=600.0,
        ports_poll_interval=0.1,
        clock=clock,
        sleep=lambda _: None,
    )
    assert ("slurm-worker1", 5000) in report.ports_still_bound
    assert any("still bound" in e for e in report.errors)


def test_cleanup_default_workers_is_four_node_topology() -> None:
    """The default worker list matches the live test-env topology
    (4 workers per plan section 'Confirmed cluster topology'). A
    silent shrink would mean later tests skip worker3/4 cleanup."""
    assert DEFAULT_CLEANUP_WORKERS == (
        "slurm-worker1",
        "slurm-worker2",
        "slurm-worker3",
        "slurm-worker4",
    )


def test_cleanup_default_ports_are_harmonia_and_peer_push() -> None:
    # 5000 = harmonia bind (DEFAULT_HARMONIA_PORT in suit_task config);
    # 6000 = peer_push at ``harmonia_port + PUSH_PORT_OFFSET`` per
    # :func:`compiler_suit_runner.peer_push.push_port_for`.
    assert DEFAULT_CLEANUP_PORTS == (5000, 6000)


def test_cleanup_default_pkill_pattern_includes_all_three_families() -> None:
    """Regression guard: the default pkill must catch the three known
    leaky process families. A future refactor that drops one would
    silently leave that class behind."""
    for needle in ("compiler_suit_runner", "harmonia-cache", "peer_push"):
        assert needle in DEFAULT_CLEANUP_PKILL_PATTERN


def test_cleanup_default_scancel_pattern_targets_asm_secondary() -> None:
    assert DEFAULT_SCANCEL_PATTERN == "asm-secondary-*"


def test_cleanup_jobs_canceled_count_matches_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``jobs_canceled`` reflects how many queued rows matched the
    scancel glob - so a test of e.g. T5 (kill secondary) can assert
    'we canceled exactly 1 job'."""
    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        remote = argv[-1]
        if remote == "echo ok":
            return _completed(stdout="ok\n")
        if remote.startswith("squeue"):
            return _completed(
                stdout=(
                    "1|debug|asm-secondary-0|RUNNING|sirati|slurm-worker1\n"
                    "2|debug|asm-secondary-1|RUNNING|sirati|slurm-worker2\n"
                    # An unrelated job that must NOT be counted.
                    "3|debug|tokenizer-train|RUNNING|sirati|slurm-worker3\n"
                ),
            )
        if remote.startswith("scancel"):
            return _completed()
        if remote.startswith("set +e"):
            return _completed(stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n")
        return _completed(stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    probe = _make_probe()
    report = probe.cleanup(
        workers=(),
        ports=(),
        timeout_s=2.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    # Two of the three rows match ``asm-secondary-*``; the
    # ``tokenizer-train`` job stays out of our count (as it must -
    # that's the asm-tokenizer peer's workload, the very thing
    # ``feedback_scancel_scope.md`` says we must not touch).
    assert report.jobs_canceled == 2


def test_cleanup_returns_clean_report_when_already_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path: queue empty, no containers, no listener
    ports - cleanup should return a report with no errors."""
    rec = _CleanupRecorder(
        squeue_initial="",
        worker_compound_stdout="STOPPED=0\nREMOVED=0\nPKILL_RC=1\n",
        port_listeners_seq=[""],
    )
    monkeypatch.setattr(subprocess, "run", rec)
    probe = _make_probe()
    report = probe.cleanup(
        workers=("slurm-worker1",),
        ports=(5000,),
        timeout_s=10.0,
        clock=_instant_clock(),
        sleep=lambda _: None,
    )
    assert report.errors == []
    assert report.reachable is True
    assert report.squeue_drained is True
    assert report.jobs_canceled == 0
    assert report.containers_stopped == 0
    assert report.containers_removed == 0
    assert report.processes_killed == 0
    assert report.ports_still_bound == []
    assert report.ports_released == [("slurm-worker1", 5000)]


# ---------------------------------------------------------------------------
# Live test (opt-in via reachability gate)
# ---------------------------------------------------------------------------


def test_squeue_me_against_live_cluster() -> None:
    """Smoke against the real test env: parse must not raise even if
    the cluster has zero queued jobs.

    This is gated on :meth:`is_reachable` because the cluster bring-up
    is asynchronous - if it's not up yet, the test skips with a clear
    "live cluster unavailable" message rather than failing the suite.
    """
    probe = ClusterProbe(
        GatewayConfig(
            host="sirati@localhost",
            port=2244,
            identity_file=LIVE_KEY_PATH,
            timeout=8.0,
        ),
    )
    if not probe.is_reachable():
        pytest.skip("live cluster unavailable")

    rows = probe.squeue_me()
    # The list may be empty (clean cluster) - that's fine; we're
    # checking that the parse path returns a list of SqueueRow without
    # raising.
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, SqueueRow)
        assert row.jobid != ""

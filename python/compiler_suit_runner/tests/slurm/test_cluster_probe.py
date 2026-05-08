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
    ClusterProbe,
    GatewayConfig,
    PodmanRow,
    SinfoRow,
    SqueueRow,
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


def test_worker_argv_uses_proxyjump_with_port() -> None:
    probe = _make_probe(identity="/tmp/k")
    argv = probe._worker_argv("slurm-worker3", "uname -a")
    assert "-J" in argv
    j_idx = argv.index("-J")
    # The ProxyJump target carries user, host AND port: when no port
    # is encoded, OpenSSH falls back to 22 and silently fails against
    # the test gateway (which listens on 2244).
    assert argv[j_idx + 1] == "sirati@localhost:2244"
    # The final hop is the bare worker hostname; the gateway resolves
    # it via internal DNS.
    assert argv[-2] == "slurm-worker3"
    assert argv[-1] == "uname -a"
    # The identity flag is still there - OpenSSH applies it to every
    # hop because IdentitiesOnly=yes is shared.
    assert argv[1] == "-i"
    assert argv[2] == "/tmp/k"


def test_worker_argv_does_not_pass_p_for_worker() -> None:
    """``-p`` is only meaningful for the *direct* hop. The worker hop
    uses default port 22; the gateway hop's port lives in the ``-J``
    target string."""
    probe = _make_probe()
    argv = probe._worker_argv("slurm-worker1", "true")
    assert "-p" not in argv


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
    # ProxyJump form must be present: rules out an accidental direct
    # connect to the worker (which would not be reachable).
    assert "-J" in captured["argv"]
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


def test_port_listeners_returns_empty_on_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(rc=255, stderr="boom"),
    )
    assert _make_probe().port_listeners(
        "slurm-worker1", ports=[5000],
    ) == []


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


def test_processes_by_pattern_returns_empty_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _completed(rc=1, stderr="oops"),
    )
    rows = _make_probe().processes_by_pattern(
        "slurm-worker1", r"compiler_suit_runner",
    )
    assert rows == []


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

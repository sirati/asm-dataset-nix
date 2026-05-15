"""Shared fixtures for the slurm test slice.

Per the test plan ("Pre-flight checks before each test"), every test
in this directory expects:

* a working ``ClusterProbe`` handle (sibling A2 owns the module;
  imported lazily here so a missing module degrades to a skip rather
  than an import error at collection time),
* the host-side log mount path,
* a cleanup hook that runs the between-test cleanup harness on demand,
* a ``fresh_run`` callable that wraps :func:`run_compiler_suit` with
  a pre-/post- :func:`clear_incremental_cache` so each row exercises
  the full cache-cold path.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time
from typing import Any, Callable, Iterator, Optional

import pytest

from compiler_suit_runner.tests.slurm.run_helpers import (
    SLURM_TEST_ENV_LOG_ROOT,
    SLURM_TEST_ENV_SSH_CONFIG,
    SLURM_TEST_ENV_SSH_CONTROL_PATH,
    RunInvocation,
    RunResult,
    clear_incremental_cache,
    run_compiler_suit,
)


CleanupCallable = Callable[..., Any]
"""Type alias for the ``cleanup_cluster`` fixture's yielded callable.

Concretely returns a :class:`CleanupReport` from
``compiler_suit_runner.tests.slurm.cluster_probe`` but typed loosely
here so a missing ``cluster_probe`` module (during incremental
implementation) doesn't produce import errors at collection time.
"""


def _write_cluster_ssh_config(
    config_path: pathlib.Path,
    *,
    host_alias: str,
    hostname: str,
    port: int,
    user: str,
    identity_file: pathlib.Path,
) -> None:
    """Generate the per-cluster ssh_config consumed by the framework.

    The Host alias is what worker containers DNS-resolve over the
    podman bridge; the SSH client redirects it to ``localhost:<port>``
    on the dispatcher's host. ``IdentityAgent=none`` keeps the
    ephemeral test-env key out of every agent (1Password, gnome-
    keyring, the operator agent — see project memory
    ``feedback_ssh_debug_key.md`` and the slurm-test-env owner's
    hard rule from 26-05-09 02:56).
    """
    config_path.write_text(
        f"""Host {host_alias}
    HostName {hostname}
    Port {port}
    User {user}
    IdentityFile {identity_file}
    IdentitiesOnly yes
    IdentityAgent none
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ServerAliveInterval 30
    ConnectTimeout 10
"""
    )
    config_path.chmod(0o600)


def _wait_for_socket(path: pathlib.Path, timeout_s: float = 10.0) -> bool:
    """Poll until the master socket appears (or timeout)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def ssh_master() -> Iterator[pathlib.Path]:
    """Pre-spawn an SSH master so the framework can reuse it.

    The framework's own SSH master spawn dies after ~2 min when
    driven from a tokio runtime nested inside a Python CLI process
    (migration doc §"Known issue: SSH master under tokio"). The
    documented escape hatch: pre-spawn the master via plain
    ``subprocess`` (which doesn't have the issue) and export
    ``DYNRUNNER_SSH_CONTROL_PATH``; the framework's ``connect()``
    detects the env var and reuses the existing master via
    ``ssh -O forward -R …`` instead of spawning its own.

    Scope is session so a single master serves the whole test run;
    teardown asks the master to exit cleanly via ``ssh -O exit``.
    Yields the socket path so individual tests can probe it if
    needed (most tests just rely on the env var).
    """
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    default_key = repo_root / ".ssh-debug" / "id_ed25519"
    identity_file = pathlib.Path(
        os.environ.get("ASM_CLUSTER_PROBE_KEY", str(default_key))
    )
    host_alias = "slurm-gateway"

    # Best-effort: clear any orphan dev-box harmonia squatting on the
    # SubmitterPeer's port. The orphan slips past
    # ``SubmitterPeer.start()``'s bind-failure retry when the existing
    # harmonia answers ``/nix-cache-info`` faster than our child can
    # exit with EADDRINUSE — the probe's 200 OK sets ``bound_ok=True``
    # and the retry never fires, leaving the orphan in charge with
    # mismatched signing keys. Killing here is cheap and safe (the
    # only ``harmonia-cache`` we ever start on the dev box is our
    # own SubmitterPeer's; cluster-side harmonia runs inside
    # containers and isn't visible to a host pkill).
    subprocess.run(
        ["pkill", "-KILL", "-f", "harmonia-cache"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )

    _write_cluster_ssh_config(
        SLURM_TEST_ENV_SSH_CONFIG,
        host_alias=host_alias,
        hostname="localhost",
        port=2244,
        user="sirati",
        identity_file=identity_file,
    )

    # Best-effort cleanup of any prior socket before starting; ssh
    # refuses to bind a ControlPath that already exists.
    try:
        SLURM_TEST_ENV_SSH_CONTROL_PATH.unlink()
    except FileNotFoundError:
        pass

    # Clear gateway-side orphan sshd-session children left behind when
    # a previous run died ungracefully (e.g. SIGKILL on pytest before
    # ``ssh -O exit``). The framework's gateway code adds its reverse
    # forwards via ``ssh -O forward -R 0.0.0.0:5005:localhost:5005``;
    # the bound socket lives in a child sshd-session on the gateway.
    # If that session leaks, the next run's `ssh -O forward` fails
    # with "remote port forwarding failed for listen port 5005" before
    # dispatch can even start. Use a one-shot SSH (not the master we
    # are about to spawn) to pkill our own orphans.
    subprocess.run(
        [
            "ssh", "-F", str(SLURM_TEST_ENV_SSH_CONFIG),
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            host_alias,
            "pkill -KILL -u $USER -f 'sshd-session.*notty' || true",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )

    spawn_cmd = [
        "setsid", "-f", "--",
        "ssh",
        "-F", str(SLURM_TEST_ENV_SSH_CONFIG),
        "-M", "-N", "-f",
        "-o", f"ControlPath={SLURM_TEST_ENV_SSH_CONTROL_PATH}",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=yes",
        host_alias,
    ]
    subprocess.run(
        spawn_cmd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not _wait_for_socket(SLURM_TEST_ENV_SSH_CONTROL_PATH, timeout_s=10.0):
        pytest.skip(
            "ssh master socket did not appear at "
            f"{SLURM_TEST_ENV_SSH_CONTROL_PATH} within 10s; check "
            "that slurm-test-env is up and reachable",
        )

    os.environ["DYNRUNNER_SSH_CONTROL_PATH"] = str(
        SLURM_TEST_ENV_SSH_CONTROL_PATH
    )

    try:
        yield SLURM_TEST_ENV_SSH_CONTROL_PATH
    finally:
        os.environ.pop("DYNRUNNER_SSH_CONTROL_PATH", None)
        subprocess.run(
            [
                "ssh",
                "-F", str(SLURM_TEST_ENV_SSH_CONFIG),
                "-O", "exit",
                "-o", f"ControlPath={SLURM_TEST_ENV_SSH_CONTROL_PATH}",
                host_alias,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            SLURM_TEST_ENV_SSH_CONTROL_PATH.unlink()
        except FileNotFoundError:
            pass


@pytest.fixture(scope="session")
def cluster_probe() -> Any:
    """Session-scoped :class:`ClusterProbe` handle.

    The identity-file path is read from ``ASM_CLUSTER_PROBE_KEY`` (env
    var); when unset, falls back to the canonical
    ``.ssh-debug/id_ed25519`` checked into this repo. The fixture
    constructs the probe with a ``GatewayConfig`` carrying that key so
    SSH probes authenticate against the live test-env.
    """
    import os

    try:
        from compiler_suit_runner.tests.slurm import (  # type: ignore[attr-defined]
            cluster_probe as cluster_probe_mod,
        )
    except ImportError as exc:
        pytest.skip(
            f"cluster_probe module not available yet ({exc})"
        )
    probe_cls = getattr(cluster_probe_mod, "ClusterProbe", None)
    if probe_cls is None:
        pytest.skip("ClusterProbe not exported from cluster_probe")

    gateway_cfg_cls = getattr(cluster_probe_mod, "GatewayConfig", None)
    repo_root = pathlib.Path(__file__).resolve().parents[4]
    default_key = repo_root / ".ssh-debug" / "id_ed25519"
    key_path = os.environ.get("ASM_CLUSTER_PROBE_KEY", str(default_key))
    if gateway_cfg_cls is None:
        return probe_cls()
    return probe_cls(gateway=gateway_cfg_cls(identity_file=key_path))


@pytest.fixture
def slurm_log_root() -> pathlib.Path:
    """Host-side mount path of the slurm-test-env's log directory.

    Plain value (not autouse): tests that need the path import this
    fixture; tests that don't, ignore it.
    """
    return SLURM_TEST_ENV_LOG_ROOT


@pytest.fixture
def cleanup_cluster(
    cluster_probe: Any,
) -> Iterator[CleanupCallable]:
    """Per-test cleanup hook implementing plan section "Cleanup harness".

    The fixture runs cleanup at fixture START (before yielding to the
    test), yields a callable so the test can invoke an extra cleanup
    pass mid-test if it needs to (e.g. a failure-injection test that
    wants to assert the cluster recovered before continuing), then
    runs cleanup at fixture END.

    The yielded callable returns a fresh
    :class:`compiler_suit_runner.tests.slurm.cluster_probe.CleanupReport`
    on each call so the test can assert on what got cleaned up.

    The fixture is NOT autouse - tests opt in by listing it (or by
    depending on :func:`fresh_run`, which wires it transitively). This
    matters because the cleanup harness mutates the cluster; tests
    that only inspect read-only state (e.g. parser unit tests) must
    not pay for it.
    """
    cleanup_fn = getattr(cluster_probe, "cleanup", None)
    if cleanup_fn is None:
        pytest.skip(
            "ClusterProbe.cleanup not available; B2 cleanup harness "
            "not yet wired",
        )

    def _run_cleanup(**kwargs: Any) -> Any:
        """Invoke the cleanup harness. ``kwargs`` flow through to
        :meth:`ClusterProbe.cleanup` so individual tests can override
        e.g. ``scancel_pattern`` for failure-injection workloads."""
        return cleanup_fn(**kwargs)

    # START: drain any leftover state from the previous test or run.
    _run_cleanup()

    try:
        yield _run_cleanup
    finally:
        # END: leave the cluster clean regardless of test outcome.
        # Errors here are NOT raised (they land in the
        # CleanupReport.errors list) so a failed cleanup doesn't mask
        # a more interesting test failure.
        _run_cleanup()


@pytest.fixture
def fresh_run(
    cleanup_cluster: CleanupCallable,  # noqa: ARG001 — drives ordering only
    ssh_master: pathlib.Path,  # noqa: ARG001 — sets DYNRUNNER_SSH_CONTROL_PATH
) -> Callable[..., RunResult]:
    """Run :func:`run_compiler_suit` with cache-cold guarantees.

    The cache is wiped before the call (so pre-flight always
    re-evaluates) and after (so the next test starts from a clean
    state regardless of this run's exit status). Extra kwargs flow
    through to :func:`run_compiler_suit`.
    """

    def _run(
        invocation: RunInvocation,
        *,
        timeout_s: float = 1800.0,
        env: Optional[dict[str, str]] = None,
    ) -> RunResult:
        clear_incremental_cache()
        try:
            return run_compiler_suit(
                invocation, timeout_s=timeout_s, env=env,
            )
        finally:
            clear_incremental_cache()

    return _run

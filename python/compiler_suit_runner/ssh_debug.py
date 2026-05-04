"""In-container sshd helpers — opt-in debug back-door for any
SLURM-spawned container in the cluster.

Why it lives in :mod:`compiler_suit_runner` and not
:mod:`ssh_debug_runner`: the actual compilation workload is the
primary thing operators may want to attach to mid-run for live
debugging (failing nix builds, harmonia state, peer cache
contents, …). The dedicated ``ssh_debug_runner`` task is just a
"container with sshd and nothing else"; the real value comes from
running this same sshd alongside the compiler-suit workers.

Public surface:

  * :func:`start_sshd_detached` — spawn sshd as a session leader
    that survives parent (worker) death. Idempotent (no-op if a
    sshd_config already exists in the runtime dir).
  * :func:`publish_ready_marker` — drop a ``host:port`` ready file
    on the gateway-readable mount so operators know where to ssh.

Image contract: the image must bake openssh + a usable
authorized_keys (see ``nix/docker-image.nix:rootAuthorizedKeys``).
Host keys are STAGED at runtime into a writable tmpfs dir because
the nix-store-baked keys come out at 0444, which sshd refuses
("UNPROTECTED PRIVATE KEY FILE").

Connect form (with ``--network host``, container's :PORT is bound on
the compute-node host directly)::

    ssh -i .ssh-debug/id_ed25519 \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o IdentitiesOnly=yes \
        -p <port> -J kruppb@<gateway> root@<compute-node>

`-o IdentitiesOnly=yes` is required when the local ssh-agent has
multiple keys (else MaxAuthTries hits before the right key is tried).
"""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import socket
import subprocess
import time


LOG = logging.getLogger("compiler_suit_runner.ssh_debug")

DEFAULT_SSHD_PORT = 22222
DEFAULT_RUNTIME_DIR = "/tmp/ssh-debug"
SSHD_LOG_FILENAME = "sshd.log"
READY_FILENAME = "ready"

# Standard NFS mount the framework's job_manager.py drops into every
# SLURM container — backed by ``<slurm-root>/log/<run_id>/`` on the
# gateway, so ready-markers written here are visible from outside.
LOG_NETWORK_DIR = "/app/log-network"


def _ts() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")


def _sshd_binary() -> str:
    found = shutil.which("sshd")
    if found:
        return found
    for candidate in (
        "/run/current-system/sw/bin/sshd",
        "/usr/bin/sshd",
        "/bin/sshd",
    ):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("could not locate sshd binary")


def _sftp_server_path() -> str:
    for cand in (
        "/run/current-system/sw/libexec/sftp-server",
        "/usr/libexec/sftp-server",
        "/usr/lib/openssh/sftp-server",
    ):
        if os.path.exists(cand):
            return cand
    sshd = _sshd_binary()
    candidate = os.path.join(
        os.path.dirname(os.path.dirname(sshd)),
        "libexec",
        "sftp-server",
    )
    return candidate if os.path.exists(candidate) else ""


def _stage_host_keys(runtime_dir: str) -> str:
    """Copy ``/etc/ssh/ssh_host_*`` to ``<runtime_dir>/etc/ssh/`` with
    0600 mode on the secret half. Image-baked keys come out 0444 (nix
    store mode) which sshd rejects.
    """
    etc_ssh_src = "/etc/ssh"
    etc_ssh_dst = os.path.join(runtime_dir, "etc", "ssh")
    os.makedirs(etc_ssh_dst, exist_ok=True)
    for name in os.listdir(etc_ssh_src):
        if not name.startswith("ssh_host_"):
            continue
        src = os.path.join(etc_ssh_src, name)
        dst = os.path.join(etc_ssh_dst, name)
        with open(src, "rb") as fr, open(dst, "wb") as fw:
            fw.write(fr.read())
        if name.endswith("_key"):
            os.chmod(dst, 0o600)
        else:
            os.chmod(dst, 0o644)
    return etc_ssh_dst


def _prepare_runtime_dir(port: int, runtime_dir: str) -> str:
    """Stage host keys + write a self-contained sshd_config under
    ``runtime_dir``. Returns the path to the generated sshd_config.

    Each container gets its own short-lived host keys at boot;
    clients should pass ``-o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null`` to skip TOFU prompts.
    """
    os.makedirs(runtime_dir, exist_ok=True)
    # sshd's privilege-separated child chroots to /var/empty (compiled
    # in to current OpenSSH). The image doesn't ship it; create at boot.
    os.makedirs("/var/empty", mode=0o755, exist_ok=True)

    etc_ssh = _stage_host_keys(runtime_dir)
    sftp_path = _sftp_server_path()
    sftp_line = f"Subsystem sftp {sftp_path}\n" if sftp_path else ""

    config_path = os.path.join(runtime_dir, "sshd_config")
    config = f"""\
Port {port}
ListenAddress 0.0.0.0
AddressFamily any

HostKey {etc_ssh}/ssh_host_rsa_key
HostKey {etc_ssh}/ssh_host_ecdsa_key
HostKey {etc_ssh}/ssh_host_ed25519_key

# `yes` (vs prohibit-password) is required because /etc/shadow lists
# root with empty password — sshd's locked-account check otherwise
# fires before auth methods are even tried under prohibit-password.
# PasswordAuthentication=no still blocks password auth entirely.
PermitRootLogin yes
PasswordAuthentication no
PubkeyAuthentication yes
KbdInteractiveAuthentication no

UsePAM no
AuthorizedKeysFile /root/.ssh/authorized_keys

PidFile {runtime_dir}/sshd.pid
LogLevel VERBOSE

ClientAliveInterval 60
ClientAliveCountMax 1440

PermitUserEnvironment yes
{sftp_line}"""
    with open(config_path, "w") as f:
        f.write(config)
    return config_path


def start_sshd_detached(
    port: int = DEFAULT_SSHD_PORT,
    runtime_dir: str = DEFAULT_RUNTIME_DIR,
    log: logging.Logger = LOG,
) -> int | None:
    """Spawn ``sshd -D`` as a detached session leader on the given
    port. Idempotent: if a process is already listening, returns
    ``None`` without spawning a duplicate.

    ``start_new_session=True`` keeps sshd alive across the parent
    process's death — important when the caller is a worker that
    the framework may cycle. When the SLURM container is torn down
    at wallclock-limit time, all PIDs (including this sshd) get
    SIGTERM'd by podman.
    """
    # Refuse to double-bind: probe the port first.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            log.info("sshd already listening on :%d; not respawning", port)
            return None
    except OSError:
        pass

    config_path = _prepare_runtime_dir(port, runtime_dir)
    cmd = [_sshd_binary(), "-D", "-f", config_path, "-e"]
    log.info("spawning detached sshd: %s", " ".join(cmd))

    sshd_log_path = os.path.join(runtime_dir, SSHD_LOG_FILENAME)
    sshd_log = open(sshd_log_path, "ab", buffering=0)
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=sshd_log,
        stderr=sshd_log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    # Poll up to ~3s for sshd to bind so callers (and ready markers)
    # don't reflect a dead listener.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            log.warning(
                "sshd PID=%d exited early rc=%d; tail of %s:",
                proc.pid, proc.returncode, sshd_log_path,
            )
            try:
                with open(sshd_log_path, encoding="utf-8") as f:
                    for line in f.read().splitlines()[-20:]:
                        log.warning("  sshd> %s", line)
            except OSError:
                pass
            return None
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                log.info("sshd PID=%d listening on :%d", proc.pid, port)
                return proc.pid
        except OSError:
            time.sleep(0.1)
    log.warning("sshd PID=%d did not bind within 3s", proc.pid)
    return proc.pid


def spawn_sshd_attached(
    port: int = DEFAULT_SSHD_PORT,
    runtime_dir: str = DEFAULT_RUNTIME_DIR,
    log: logging.Logger = LOG,
) -> subprocess.Popen | None:
    """Spawn ``sshd -D`` as a CHILD of the current process — opposite
    of :func:`start_sshd_detached`.

    Used by ``ssh_debug_runner.worker`` where the framework's
    runner-protocol task lifecycle is the sshd lifecycle: the worker
    holds the comm channel open while sshd runs, and emits
    ``done:rc:0`` once sshd exits (or is reaped after ``stop``).
    Detaching here would prevent the worker from reaping the child,
    so we leave the session relationship intact.

    Returns the :class:`subprocess.Popen` handle (caller polls /
    terminates it), or ``None`` if a process is already listening on
    the port (in which case the caller should refuse to dispatch).
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            log.info("sshd already listening on :%d; not respawning", port)
            return None
    except OSError:
        pass

    config_path = _prepare_runtime_dir(port, runtime_dir)
    cmd = [_sshd_binary(), "-D", "-f", config_path, "-e"]
    log.info("spawning attached sshd: %s", " ".join(cmd))

    sshd_log_path = os.path.join(runtime_dir, SSHD_LOG_FILENAME)
    sshd_log = open(sshd_log_path, "ab", buffering=0)
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=sshd_log,
        stderr=sshd_log,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            log.warning(
                "sshd PID=%d exited early rc=%d; tail of %s:",
                proc.pid, proc.returncode, sshd_log_path,
            )
            try:
                with open(sshd_log_path, encoding="utf-8") as f:
                    for line in f.read().splitlines()[-20:]:
                        log.warning("  sshd> %s", line)
            except OSError:
                pass
            return None
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                log.info("sshd PID=%d listening on :%d", proc.pid, port)
                return proc
        except OSError:
            time.sleep(0.1)
    log.warning("sshd PID=%d did not bind within 3s", proc.pid)
    return proc


def publish_ready_marker(
    host: str,
    port: int,
    *,
    output_dir: str | None = None,
    log_network_dir: str = LOG_NETWORK_DIR,
    runtime_dir: str = DEFAULT_RUNTIME_DIR,
    worker_id: int = 0,
    log: logging.Logger = LOG,
) -> None:
    """Drop a `host:port` marker file at every gateway-reachable path
    so operators can find the listener.

    Writes (best-effort, none fatal):
      - ``<runtime_dir>/ready`` (container-local, podman-exec readable)
      - ``<log_network_dir>/ssh-debug.<host>.<port>.ready`` (gateway-
        readable as ``~/BIG/slurm/log/<run_id>/ssh-debug.*.ready``);
        only if the mount exists (i.e. SLURM context).
      - ``<output_dir>/ssh-debug.<host>.<port>.ready`` (per-worker
        output dir, container-local under SLURM but useful for non-
        SLURM dispatch).
    """
    line = (
        f"host={host} port={port} pid={os.getpid()} "
        f"worker_id={worker_id} ts={_ts()}\n"
    )
    paths: list[str] = [os.path.join(runtime_dir, READY_FILENAME)]
    if log_network_dir and os.path.isdir(log_network_dir):
        paths.append(
            os.path.join(
                log_network_dir, f"ssh-debug.{host}.{port}.ready"
            )
        )
    if output_dir:
        paths.append(
            os.path.join(output_dir, f"ssh-debug.{host}.{port}.ready")
        )
    for p in paths:
        try:
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(line)
            log.info("ssh-debug ready-marker written: %s", p)
        except OSError as exc:  # noqa: BLE001
            log.warning("ready-marker write to %s failed: %s", p, exc)

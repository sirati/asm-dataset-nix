"""Unit tests for :mod:`compiler_suit_runner.peer_cache`.

Stdlib + pytest only. Tests that would require the ``nix`` CLI or
``harmonia`` binary are gated with ``pytest.mark.skipif``.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import time
from typing import Optional

import pytest

from compiler_suit_runner import peer_cache
from compiler_suit_runner.peer_cache import (
    HarmoniaProcess,
    PeerInfo,
    PeerListWatcher,
    announce_self,
    assemble_substituter_env,
    build_nix_extra_args,
    generate_signing_key,
    list_peers,
    prune_stale,
    withdraw_self,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_fs(tmp_path: pathlib.Path) -> pathlib.Path:
    """A scratch shared-FS root for one test."""
    root = tmp_path / "shared"
    root.mkdir()
    return root


def _mk_peer(i: int) -> PeerInfo:
    return PeerInfo(
        secondary_id=f"sec{i}",
        hostname=f"host{i}.example",
        port=5000 + i,
        public_key=f"key{i}:AAAA{'A' * (40 + i)}",
    )


# ---------------------------------------------------------------------------
# announce / withdraw / list
# ---------------------------------------------------------------------------


def test_announce_self_writes_atomic_json(shared_fs: pathlib.Path) -> None:
    info = _mk_peer(1)
    written = announce_self(shared_fs, info)
    assert written == shared_fs / "peers" / "sec1.json"
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data == {
        "secondary_id": "sec1",
        "hostname": "host1.example",
        "port": 5001,
        "public_key": info.public_key,
    }
    # No leftover per-writer .tmp file.
    leftovers = [
        p for p in (shared_fs / "peers").iterdir() if p.suffix == ".tmp"
    ]
    assert leftovers == []


def test_announce_rejects_reserved_id(shared_fs: pathlib.Path) -> None:
    bad = PeerInfo(
        secondary_id="__nope",
        hostname="h",
        port=1,
        public_key="k:AAAA",
    )
    with pytest.raises(ValueError):
        announce_self(shared_fs, bad)


def test_list_peers_round_trip(shared_fs: pathlib.Path) -> None:
    a, b, c = _mk_peer(1), _mk_peer(2), _mk_peer(3)
    for p in (a, b, c):
        announce_self(shared_fs, p)
    peers = list_peers(shared_fs)
    ids = sorted(p.secondary_id for p in peers)
    assert ids == ["sec1", "sec2", "sec3"]


def test_list_peers_skips_reserved_files(shared_fs: pathlib.Path) -> None:
    announce_self(shared_fs, _mk_peer(1))
    # Drop a reserved file alongside.
    (shared_fs / "peers" / "__signing-key").write_bytes(b"deadbeef")
    (shared_fs / "peers" / "__public-key").write_text("name:AAA")
    peers = list_peers(shared_fs)
    assert [p.secondary_id for p in peers] == ["sec1"]


def test_list_peers_excludes_self(shared_fs: pathlib.Path) -> None:
    announce_self(shared_fs, _mk_peer(1))
    announce_self(shared_fs, _mk_peer(2))
    peers = list_peers(shared_fs, exclude_id="sec1")
    assert [p.secondary_id for p in peers] == ["sec2"]


def test_list_peers_skips_malformed(shared_fs: pathlib.Path) -> None:
    announce_self(shared_fs, _mk_peer(1))
    (shared_fs / "peers" / "broken.json").write_text("{not json")
    (shared_fs / "peers" / "incomplete.json").write_text(
        json.dumps({"secondary_id": "x"})  # missing fields
    )
    peers = list_peers(shared_fs)
    assert [p.secondary_id for p in peers] == ["sec1"]


def test_list_peers_missing_dir_returns_empty(tmp_path: pathlib.Path) -> None:
    # No peers/ dir created yet; list_peers should auto-create it and return [].
    root = tmp_path / "fresh"
    root.mkdir()
    assert list_peers(root) == []


def test_withdraw_self_removes_file(shared_fs: pathlib.Path) -> None:
    announce_self(shared_fs, _mk_peer(1))
    assert (shared_fs / "peers" / "sec1.json").exists()
    withdraw_self(shared_fs, "sec1")
    assert not (shared_fs / "peers" / "sec1.json").exists()


def test_withdraw_self_missing_is_ok(shared_fs: pathlib.Path) -> None:
    # No file present — must not raise.
    withdraw_self(shared_fs, "ghost")


# ---------------------------------------------------------------------------
# Substituter env / nix extra args
# ---------------------------------------------------------------------------


def test_assemble_substituter_env_empty() -> None:
    env = assemble_substituter_env([])
    assert env == {
        "NIX_CONFIG_EXTRA_SUBSTITUTERS": "",
        "NIX_CONFIG_EXTRA_TRUSTED_PUBLIC_KEYS": "",
    }


def test_assemble_substituter_env_populated() -> None:
    peers = [_mk_peer(1), _mk_peer(2)]
    env = assemble_substituter_env(peers)
    assert env["NIX_CONFIG_EXTRA_SUBSTITUTERS"] == (
        "http://host1.example:5001 http://host2.example:5002"
    )
    assert env["NIX_CONFIG_EXTRA_TRUSTED_PUBLIC_KEYS"] == (
        f"{peers[0].public_key} {peers[1].public_key}"
    )


def test_build_nix_extra_args_empty() -> None:
    assert build_nix_extra_args([]) == []


def test_build_nix_extra_args_substitute_on_destination_arg_is_ignored() -> None:
    """``--substitute-on-destination`` is a ``nix copy`` flag, not a
    ``nix build`` flag — earlier versions of this function injected it
    unconditionally and ``nix build`` aborted with
    ``error: unrecognised flag '--substitute-on-destination'``. The
    parameter is preserved on the function signature for API stability
    but the flag is no longer emitted regardless of the value.
    """
    peers = [_mk_peer(1)]
    args = build_nix_extra_args(peers, substitute_on_destination=True)
    assert "--extra-substituters" in args
    assert "http://host1.example:5001" in args
    assert "--extra-trusted-public-keys" in args
    assert peers[0].public_key in args
    assert "--substitute-on-destination" not in args


def test_build_nix_extra_args_without_destination() -> None:
    peers = [_mk_peer(1)]
    args = build_nix_extra_args(peers, substitute_on_destination=False)
    assert "--substitute-on-destination" not in args
    # Still has substituter + key flags.
    assert "--extra-substituters" in args
    assert "--extra-trusted-public-keys" in args


# ---------------------------------------------------------------------------
# Stale pruning
# ---------------------------------------------------------------------------


def test_prune_stale_removes_old_keeps_fresh_ignores_reserved(
    shared_fs: pathlib.Path,
) -> None:
    announce_self(shared_fs, _mk_peer(1))
    announce_self(shared_fs, _mk_peer(2))
    reserved = shared_fs / "peers" / "__signing-key"
    reserved.write_bytes(b"x")

    now = time.time()
    # Backdate sec1 to 10 minutes ago, keep sec2 fresh, backdate reserved
    # to also be stale (it should still be ignored).
    old_t = now - 600.0
    os.utime(shared_fs / "peers" / "sec1.json", (old_t, old_t))
    os.utime(reserved, (old_t, old_t))

    removed = prune_stale(shared_fs, max_age_seconds=300.0, now=now)
    assert removed == 1
    assert not (shared_fs / "peers" / "sec1.json").exists()
    assert (shared_fs / "peers" / "sec2.json").exists()
    assert reserved.exists()


def test_prune_stale_missing_dir_returns_zero(tmp_path: pathlib.Path) -> None:
    # No peers/ dir yet; prune must cope.
    root = tmp_path / "fresh"
    root.mkdir()
    # remove the peers dir between create and prune to provoke the
    # FileNotFoundError branch.
    pathlib.Path(root / "peers").mkdir()
    pathlib.Path(root / "peers").rmdir()
    # _peers_dir will recreate the directory; prune just sees an empty one.
    assert prune_stale(root, max_age_seconds=10.0) == 0


# ---------------------------------------------------------------------------
# PeerListWatcher
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 2.0, poll: float = 0.02) -> bool:
    """Spin-wait for *predicate* to become truthy. Returns the final value."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return bool(predicate())


def _read_substituters_file(path: pathlib.Path) -> list[str]:
    """Helper: load the watcher-published substituters file."""
    if not path.exists():
        return []
    return [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_peerlist_watcher_picks_up_writes_and_deletions(
    shared_fs: pathlib.Path,
) -> None:
    watcher = PeerListWatcher(
        shared_fs, exclude_id="self", tick_seconds=0.05
    )
    # Initial state: no peers.
    assert watcher.peers == []
    assert _read_substituters_file(watcher.substituters_path) == []
    watcher.start()
    try:
        announce_self(shared_fs, _mk_peer(1))
        assert _wait_until(
            lambda: len(watcher.peers) == 1, timeout=2.0
        ), watcher.peers
        snap = watcher.peers
        assert snap[0].secondary_id == "sec1"
        # Returned list is a copy (mutating it doesn't affect the watcher).
        snap.append(_mk_peer(99))  # type: ignore[arg-type]
        assert len(watcher.peers) == 1

        announce_self(shared_fs, _mk_peer(2))
        assert _wait_until(lambda: len(watcher.peers) == 2, timeout=2.0)
        # The published substituters file should now reflect both peers.
        assert _wait_until(
            lambda: any(
                "host2.example:5002" in line
                for line in _read_substituters_file(watcher.substituters_path)
            ),
            timeout=2.0,
        )
        args = _read_substituters_file(watcher.substituters_path)
        assert "--extra-substituters" in args

        # Self-announcement is filtered out via exclude_id.
        announce_self(
            shared_fs,
            PeerInfo(
                secondary_id="self",
                hostname="me.example",
                port=5000,
                public_key="me:AAAA",
            ),
        )
        # Wait a few ticks; "self" should never appear.
        time.sleep(0.3)
        assert "self" not in [p.secondary_id for p in watcher.peers]

        withdraw_self(shared_fs, "sec1")
        assert _wait_until(
            lambda: [p.secondary_id for p in watcher.peers] == ["sec2"],
            timeout=2.0,
        )
    finally:
        watcher.stop()
        watcher.join(timeout=2.0)
        assert not watcher.is_alive()


def test_peerlist_watcher_publishes_empty_file_when_no_peers(
    shared_fs: pathlib.Path,
) -> None:
    watcher = PeerListWatcher(shared_fs, tick_seconds=0.05)
    # The prime-on-init refresh writes (or overwrites) an empty file.
    assert watcher.substituters_path.exists()
    assert _read_substituters_file(watcher.substituters_path) == []


# ---------------------------------------------------------------------------
# Signing key generation
# ---------------------------------------------------------------------------


_HAS_NIX = shutil.which("nix") is not None


@pytest.mark.skipif(not _HAS_NIX, reason="nix CLI not available")
def test_generate_signing_key_idempotent(shared_fs: pathlib.Path) -> None:
    key1 = generate_signing_key(shared_fs, run_id="testrun")
    assert key1.secret_path.exists()
    assert key1.public_path.exists()
    # 0600 / 0644 file modes.
    assert (key1.secret_path.stat().st_mode & 0o777) == 0o600
    assert (key1.public_path.stat().st_mode & 0o777) == 0o644
    secret_bytes = key1.secret_path.read_bytes()
    public_bytes = key1.public_path.read_bytes()

    key2 = generate_signing_key(shared_fs, run_id="testrun")
    # Second call must reload the existing key, not regenerate.
    assert key2.public_key == key1.public_key
    assert key2.secret_path.read_bytes() == secret_bytes
    assert key2.public_path.read_bytes() == public_bytes


def test_generate_signing_key_missing_nix(
    shared_fs: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force shutil.which to find no nix.
    monkeypatch.setattr(peer_cache.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError):
        generate_signing_key(shared_fs, run_id="x")


def test_generate_signing_key_concurrent_writers_agree(
    shared_fs: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N concurrent secondaries must all return the SAME on-disk keypair.

    Regression test for the federation bug: when multiple SLURM
    secondaries hit a fresh shared FS at once, every caller used to
    generate its OWN keypair and last-writer-wins clobbered the
    others on disk while each kept its in-memory copy. Peers then
    advertised mismatched ``public_key`` values in ``<id>.json``,
    breaking signature verification on substitution.

    We stub ``nix key`` so each "secondary" produces a unique
    keypair and run them in parallel; the function must converge on
    a single on-disk pair AND every returned :class:`SigningKey` must
    match those on-disk bytes.
    """
    import threading

    peers = shared_fs / "peers"

    counter = {"n": 0}
    lock = threading.Lock()

    def fake_run(cmd, *args, **kwargs):
        if "generate-secret" in cmd:
            with lock:
                counter["n"] += 1
                tag = counter["n"]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"secret-{tag}".encode(), stderr=b""
            )
        if "convert-secret-to-public" in cmd:
            secret = kwargs.get("input", b"")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=f"asm-suit-cluster-rid:{secret.decode()}".encode(),
                stderr=b"",
            )
        raise AssertionError(f"unexpected nix call: {cmd}")

    monkeypatch.setattr(peer_cache.subprocess, "run", fake_run)
    monkeypatch.setattr(peer_cache.shutil, "which", lambda _name: "/usr/bin/nix")

    results: list[peer_cache.SigningKey] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=2.0)
            results.append(generate_signing_key(shared_fs, run_id="rid"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert errors == [], errors
    assert len(results) == 8
    on_disk_secret = (peers / "__signing-key").read_bytes()
    on_disk_public = (peers / "__public-key").read_text().strip()
    # Every caller's in-memory public_key must match what landed on disk.
    assert all(k.public_key == on_disk_public for k in results), [
        k.public_key for k in results
    ]
    # And the in-memory key must correspond to the on-disk secret —
    # i.e. public is "asm-suit-cluster-rid:<secret>" by stub construction.
    expected_pub = f"asm-suit-cluster-rid:{on_disk_secret.decode()}"
    assert on_disk_public == expected_pub
    # No tmp leftovers.
    leftovers = [p for p in peers.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_generate_signing_key_reloads_existing_without_nix(
    shared_fs: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If keys already exist on disk we should reload them even if nix is gone."""
    peers = shared_fs / "peers"
    peers.mkdir()
    (peers / "__signing-key").write_bytes(b"fake-secret")
    os.chmod(peers / "__signing-key", 0o600)
    (peers / "__public-key").write_text("asm-suit-cluster-existing:AAAA")
    os.chmod(peers / "__public-key", 0o644)

    monkeypatch.setattr(peer_cache.shutil, "which", lambda _name: None)
    key = generate_signing_key(shared_fs, run_id="ignored-run")
    assert key.public_key == "asm-suit-cluster-existing:AAAA"


# ---------------------------------------------------------------------------
# HarmoniaProcess (no real spawn)
# ---------------------------------------------------------------------------


class _StubPopen:
    """Drop-in stub for :class:`subprocess.Popen` for harmonia tests."""

    instances: list["_StubPopen"] = []

    def __init__(
        self,
        cmd,
        env=None,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session: bool = False,
        close_fds: bool = False,
    ) -> None:
        # Tracks the same kwargs HarmoniaProcess.start() passes to
        # subprocess.Popen: stdin/stdout/stderr redirections plus the
        # session/fd flags we use to detach the harmonia daemon from
        # the worker's group. Accepting them all (rather than only the
        # ones the test asserts on) keeps the stub forward-compatible
        # with future kwargs HarmoniaProcess might need to pass.
        self.cmd = list(cmd)
        self.env = dict(env) if env is not None else None
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = stdin
        self.start_new_session = start_new_session
        self.close_fds = close_fds
        self.signals_sent: list[int] = []
        self.killed = False
        self._returncode: Optional[int] = None
        self.returncode: Optional[int] = None
        _StubPopen.instances.append(self)

    def poll(self) -> Optional[int]:
        return self._returncode

    def send_signal(self, sig: int) -> None:
        self.signals_sent.append(sig)
        # On SIGTERM, simulate clean exit.
        if sig == signal.SIGTERM:
            self._returncode = 0
            self.returncode = 0

    def wait(self, timeout: Optional[float] = None) -> int:
        if self._returncode is None:
            self._returncode = 0
            self.returncode = 0
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self.returncode = -9


@pytest.fixture(autouse=False)
def stub_popen(monkeypatch: pytest.MonkeyPatch):
    _StubPopen.instances.clear()
    monkeypatch.setattr(peer_cache.subprocess, "Popen", _StubPopen)
    # Pretend harmonia exists in PATH.
    monkeypatch.setattr(
        peer_cache.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"harmonia", "nix-serve"} else None,
    )
    yield _StubPopen
    _StubPopen.instances.clear()


def test_harmonia_start_invokes_subprocess(
    tmp_path: pathlib.Path, stub_popen
) -> None:
    key = tmp_path / "secret"
    key.write_bytes(b"k")
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=key,
        runtime_dir=tmp_path / "harmonia",
    )
    proc.start()
    assert len(stub_popen.instances) == 1
    spawned = stub_popen.instances[0]
    assert spawned.cmd[0].endswith("harmonia")
    # Harmonia 3.x dropped the ``--bind`` argv flag; bind goes in a
    # TOML config file referenced by ``CONFIG_FILE``. The cmd is just
    # the binary path, no extra argv.
    assert "--bind" not in spawned.cmd
    assert spawned.env is not None
    assert spawned.env.get("SIGN_KEY_PATH") == str(key)
    assert spawned.env.get("NIX_SECRET_KEY_FILE") == str(key)
    assert spawned.env.get("SIGN_KEY_PATHS") == str(key)
    cfg_path = pathlib.Path(spawned.env["CONFIG_FILE"])
    assert cfg_path.exists()
    # Per-node TOML lives under runtime_dir, NOT next to the NFS
    # signing key — see HarmoniaProcess docstring.
    assert cfg_path.parent == tmp_path / "harmonia"
    assert cfg_path.parent != key.parent
    assert 'bind = "0.0.0.0:5000"' in cfg_path.read_text()
    assert proc.is_running

    rc = proc.stop()
    assert rc == 0
    assert signal.SIGTERM in spawned.signals_sent
    assert not proc.is_running

    # Calling stop() twice is a no-op.
    assert proc.stop() is None


def test_harmonia_double_start_is_idempotent(
    tmp_path: pathlib.Path, stub_popen
) -> None:
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=tmp_path / "harmonia",
    )
    proc.start()
    proc.start()
    assert len(stub_popen.instances) == 1
    proc.stop()


def test_harmonia_context_manager(tmp_path: pathlib.Path, stub_popen) -> None:
    with HarmoniaProcess(
        bind_addr="127.0.0.1:5000",
        signing_key_path=tmp_path / "k",
        extra_args=["--workers", "8"],
        runtime_dir=tmp_path / "harmonia",
    ) as proc:
        assert proc.is_running
        assert "--workers" in stub_popen.instances[0].cmd
        assert "8" in stub_popen.instances[0].cmd
    assert not proc.is_running


def test_harmonia_falls_back_to_nix_serve(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _StubPopen.instances.clear()
    monkeypatch.setattr(peer_cache.subprocess, "Popen", _StubPopen)
    monkeypatch.setattr(
        peer_cache.shutil,
        "which",
        lambda name: "/usr/bin/nix-serve" if name == "nix-serve" else None,
    )
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=tmp_path / "harmonia",
    )
    proc.start()
    try:
        assert _StubPopen.instances[0].cmd[0].endswith("nix-serve")
    finally:
        proc.stop()


def test_harmonia_missing_binary_raises(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(peer_cache.shutil, "which", lambda _name: None)
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=tmp_path / "harmonia",
    )
    with pytest.raises(FileNotFoundError):
        proc.start()


def test_harmonia_kill_on_timeout(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the server doesn't exit on SIGTERM in time, fall back to SIGKILL."""

    class _StuckPopen(_StubPopen):
        def __init__(self, cmd, *args, **kwargs):
            super().__init__(cmd, *args, **kwargs)

        def send_signal(self, sig: int) -> None:
            # Record but don't update returncode: we're "stuck".
            self.signals_sent.append(sig)

        def wait(self, timeout: Optional[float] = None) -> int:
            if self._returncode is None and timeout is not None:
                raise subprocess.TimeoutExpired(cmd=self.cmd, timeout=timeout)
            return super().wait(timeout=timeout)

    _StubPopen.instances.clear()
    monkeypatch.setattr(peer_cache.subprocess, "Popen", _StuckPopen)
    monkeypatch.setattr(
        peer_cache.shutil,
        "which",
        lambda name: "/usr/bin/harmonia" if name == "harmonia" else None,
    )
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=tmp_path / "harmonia",
    )
    proc.start()
    rc = proc.stop(timeout=0.05)
    assert rc == -9
    inst = _StubPopen.instances[0]
    assert inst.killed is True


def test_harmonia_runtime_dir_not_under_signing_key_parent(
    tmp_path: pathlib.Path, stub_popen
) -> None:
    """Regression: per-node files must NOT land next to the NFS-shared
    signing key (causes 6 secondaries to race on the same TOML/log).

    Ensures both the TOML and the log are written to ``runtime_dir``
    by default, not to ``signing_key_path.parent``. Default
    ``runtime_dir`` is ``/tmp/harmonia/``; we override here so the
    test doesn't pollute the host's ``/tmp``.
    """
    nfs_dir = tmp_path / "nfs_peers"
    nfs_dir.mkdir()
    key = nfs_dir / "__signing-key"
    key.write_bytes(b"k")

    runtime = tmp_path / "container_local"
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=key,
        runtime_dir=runtime,
    )
    proc.start()
    try:
        # Neither file should appear under the NFS-shared dir.
        assert not (nfs_dir / "harmonia.toml").exists()
        assert not (nfs_dir / "harmonia.log").exists()
        # Both should be under runtime_dir.
        assert (runtime / "harmonia.toml").exists()
        assert (runtime / "harmonia.log").exists()
    finally:
        proc.stop()


def test_harmonia_explicit_log_path_lands_off_runtime_dir(
    tmp_path: pathlib.Path, stub_popen
) -> None:
    """When ``log_path`` is given, the log lives there (not in
    runtime_dir). Models the suit-task secondary writing harmonia
    output to ``/app/log-network/harmonia-<secondary_id>.log`` so
    operators can read it off the gateway while the TOML stays
    container-local.
    """
    runtime = tmp_path / "container_local"
    log_dir = tmp_path / "log_network"
    explicit_log = log_dir / "harmonia-secA.log"
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=runtime,
        log_path=explicit_log,
    )
    proc.start()
    try:
        assert (runtime / "harmonia.toml").exists()
        # Log lands at the explicit path; default location stays empty.
        assert explicit_log.exists()
        assert not (runtime / "harmonia.log").exists()
    finally:
        proc.stop()


def test_harmonia_workers_param_lands_in_toml(
    tmp_path: pathlib.Path, stub_popen
) -> None:
    proc = HarmoniaProcess(
        bind_addr="0.0.0.0:5000",
        signing_key_path=tmp_path / "k",
        runtime_dir=tmp_path / "harmonia",
        workers=8,
    )
    proc.start()
    try:
        cfg_path = pathlib.Path(stub_popen.instances[0].env["CONFIG_FILE"])
        assert "workers = 8" in cfg_path.read_text()
    finally:
        proc.stop()

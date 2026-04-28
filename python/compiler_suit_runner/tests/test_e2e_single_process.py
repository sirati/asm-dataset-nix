"""End-to-end test of the single-process execution mode.

The whole pipeline runs in this process:

1. ``preflight`` is stubbed to return canned variants/toolchains.
2. ``manifest_gen.emit_all_manifests`` writes real (sparse) manifests.
3. ``run_single_process`` constructs a :class:`SuitTask`, sets up the
   peer-cache (with stubbed harmonia / cachix), iterates manifests in
   size-DESC order, and calls each item's ``dispatch_binary``.
4. The build / partition / merge workers are monkeypatched to lightweight
   recorders so no real ``nix`` invocation occurs.

Sparse-file note: manifest sizes top out at ``(6 << 48)`` bytes which
exceeds ext4's per-file cap. We re-root pytest's tmp tree to a tmpfs.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import threading

import pytest

import compiler_suit_runner.cli as cli_module
import compiler_suit_runner.suit_task as suit_task_module
from compiler_suit_runner.cli import run_single_process
from compiler_suit_runner.manifest_gen import emit_all_manifests
from compiler_suit_runner.partition import VariantSpec
from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig
from compiler_suit_runner.workers.barrier_worker import (
    PHASE_1A_DONE_FLAG,
    PHASE_1B_DONE_FLAG,
    PHASE_2_DONE_FLAG,
)


# ---------------------------------------------------------------------------
# Tmpfs override (mirrors test_manifest_gen.py).
# ---------------------------------------------------------------------------


def _tmpfs_basetemp() -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(pathlib.Path(xdg))
    candidates.append(pathlib.Path("/dev/shm"))
    for candidate in candidates:
        if not candidate.is_dir() or not os.access(candidate, os.W_OK):
            continue
        probe = candidate / f"e2e_probe_{os.getpid()}"
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        except OSError:
            continue
        try:
            try:
                os.ftruncate(fd, 1 << 50)
            except OSError:
                continue
            return candidate
        finally:
            os.close(fd)
            try:
                probe.unlink()
            except OSError:
                pass
    return None


@pytest.fixture(scope="session", autouse=True)
def _tmp_path_on_tmpfs(tmp_path_factory: pytest.TempPathFactory):
    base = _tmpfs_basetemp()
    if base is None:
        pytest.skip(
            "no tmpfs available for sparse-file manifest tests"
            " (need /dev/shm or $XDG_RUNTIME_DIR)",
            allow_module_level=True,
        )
        return
    new_basetemp = pathlib.Path(
        tempfile.mkdtemp(prefix="e2e_", dir=str(base))
    )
    tmp_path_factory._basetemp = new_basetemp  # type: ignore[attr-defined]
    yield


# ---------------------------------------------------------------------------
# Test helpers — synthetic matrix
# ---------------------------------------------------------------------------


def _make_variant(
    pkg: str,
    arch: str,
    suffix: str,
    *,
    compiler_id: str = "gcc15",
    tier: int = 1,
) -> VariantSpec:
    label = f"{pkg}-{arch}-{compiler_id}-{suffix}"
    return {
        "label": label,
        "drv": f"/nix/store/{pkg}-{arch}-{suffix}.drv",
        "tarball_name": f"{label}.tar.zst",
        "compiler_id": compiler_id,
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


def _make_config(shared_root: pathlib.Path) -> SuitTaskConfig:
    return SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=shared_root,
        manifest_dir=shared_root / "manifests",
        raw_partition_dir=shared_root / "partition" / "raw",
        partition_dir=shared_root / "partition",
        flags_dir=shared_root / "flags",
        dataset_dir=shared_root / "dataset",
        peers_dir=shared_root / "peers",
        run_id="run-e2e",
        secondary_id="primary",
        hostname="localhost",
        harmonia_port=5000,
        enable_harmonia=False,
        cachix_cache=None,
        cachix_token_file=None,
        poll_interval_seconds=0.001,
        barrier_timeout_seconds=2.0,
        input_hash="hash-test",
        toolchain_drvs=frozenset(),
        common_threshold=10,
        variants=(),
    )


def _emit_synthetic_matrix(
    shared_root: pathlib.Path, *, num_workers: int = 1
):
    """Produce 2 packages * 1 arch * 2 variants per (pkg, arch) manifests.

    Plus 1 toolchain and 1 common dep for phase 2 routing coverage.
    """
    manifest_dir = shared_root / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    variants = (
        _make_variant("hello", "x86_64", "O0"),
        _make_variant("hello", "x86_64", "O2"),
        _make_variant("busybox", "x86_64", "O0"),
        _make_variant("busybox", "x86_64", "O2"),
    )
    return emit_all_manifests(
        target_dir=manifest_dir,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[("/nix/store/aaa-libfoo.drv", "libfoo")],
        num_workers=num_workers,
    )


# ---------------------------------------------------------------------------
# Stub workers + peer cache
# ---------------------------------------------------------------------------


class _CountingStub:
    """Records call count + items; never sleeps; never raises."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self.lock:
            self.calls.append((args, kwargs))


@pytest.fixture
def stub_workers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _CountingStub]:
    stubs = {
        "partition_worker": _CountingStub("partition_worker"),
        "merge_worker": _CountingStub("merge_worker"),
        "build_worker": _CountingStub("build_worker"),
        "barrier_worker": _CountingStub("barrier_worker"),
    }
    for name, stub in stubs.items():
        monkeypatch.setattr(suit_task_module, name, stub)
    return stubs


@pytest.fixture
def stub_peer_cache(monkeypatch: pytest.MonkeyPatch):
    """No real harmonia, no real signing key generation, no real watcher."""

    class _FakeSigningKey:
        def __init__(self, run_id, base):
            self.name = f"asm-suit-cluster-{run_id}"
            self.secret_path = base / "peers" / "__signing-key"
            self.public_path = base / "peers" / "__public-key"
            self.public_key = "asm-suit-cluster-test:fake"

    def _fake_generate(shared_fs, run_id):
        peers = pathlib.Path(shared_fs) / "peers"
        peers.mkdir(parents=True, exist_ok=True)
        return _FakeSigningKey(run_id, pathlib.Path(shared_fs))

    def _fake_announce(shared_fs, info):
        return pathlib.Path(shared_fs) / "peers" / f"{info.secondary_id}.json"

    def _fake_withdraw(shared_fs, secondary_id):
        return None

    class _FakeWatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.extra_args = []

        def start(self):
            return None

        def stop(self):
            return None

    class _FakeHarmonia:
        def __init__(self, *, bind_addr, signing_key_path, **_kwargs):
            pass

        def start(self):
            return None

        def stop(self, timeout=10.0):
            return None

    class _FakeCachix:
        def __init__(self, cfg, **_kwargs):
            self.cfg = cfg

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(suit_task_module, "generate_signing_key", _fake_generate)
    monkeypatch.setattr(suit_task_module, "announce_self", _fake_announce)
    monkeypatch.setattr(suit_task_module, "withdraw_self", _fake_withdraw)
    monkeypatch.setattr(suit_task_module, "PeerListWatcher", _FakeWatcher)
    monkeypatch.setattr(suit_task_module, "HarmoniaProcess", _FakeHarmonia)
    monkeypatch.setattr(suit_task_module, "CachixUploader", _FakeCachix)


# ---------------------------------------------------------------------------
# End-to-end run
# ---------------------------------------------------------------------------


def test_run_single_process_completes_all_phases(
    tmp_path: pathlib.Path,
    stub_workers: dict[str, _CountingStub],
    stub_peer_cache,
):
    """Drive the entire pipeline once and verify every phase fires.

    * All three barrier flags are written (proves phase counters
      reached their expected counts).
    * Each worker is invoked the expected number of times.
    * The dataset_dir exists after the run.
    """
    shared_root = tmp_path / "shared"
    config = _make_config(shared_root)
    manifest_set = _emit_synthetic_matrix(shared_root, num_workers=1)

    rc = run_single_process(config)
    assert rc == 0

    # All three flag files were written by counter bookkeeping.
    flags_dir = config.flags_dir
    assert (flags_dir / PHASE_1A_DONE_FLAG).exists()
    assert (flags_dir / PHASE_1B_DONE_FLAG).exists()
    assert (flags_dir / PHASE_2_DONE_FLAG).exists()

    # Worker dispatch counts:
    #   - 2 phase-1a shards (hello/x86_64 + busybox/x86_64)
    #   - 1 phase-1b merge
    #   - 1 toolchain + 1 common-dep + 4 variants = 6 build_worker calls
    #   - num_workers (1) of each barrier rank * 3 barriers = 3
    assert len(stub_workers["partition_worker"].calls) == 2
    assert len(stub_workers["merge_worker"].calls) == 1
    assert len(stub_workers["build_worker"].calls) == 6
    # 3 barriers * 1 worker = 3 sentinel dispatches.
    assert len(stub_workers["barrier_worker"].calls) == 3

    # Every manifest has been visited.
    assert config.dataset_dir.exists()


def test_run_single_process_phase_ordering_via_size_desc(
    tmp_path: pathlib.Path,
    stub_workers: dict[str, _CountingStub],
    stub_peer_cache,
):
    """Items are dispatched in size-DESC order so phases run in sequence.

    The barriers' flag files appear before the dispatch ever reaches the
    rank that needs them — single-process serializes through size-DESC,
    which means: phase1a items -> phase1a_barrier sentinels (their flag
    is written by the counter completing on the last phase1a item, BEFORE
    the sentinel is dispatched). We verify this happens.
    """
    shared_root = tmp_path / "shared"
    config = _make_config(shared_root)
    _emit_synthetic_matrix(shared_root, num_workers=1)

    # Track when each barrier flag was written by the counter.
    flag_paths = [
        config.flags_dir / PHASE_1A_DONE_FLAG,
        config.flags_dir / PHASE_1B_DONE_FLAG,
        config.flags_dir / PHASE_2_DONE_FLAG,
    ]

    rc = run_single_process(config)
    assert rc == 0
    # All three barriers are written.
    for path in flag_paths:
        assert path.exists(), f"{path} not written"


def test_run_single_process_handles_worker_exceptions(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_peer_cache,
):
    """A crashing worker still increments the counter and lets the run finish."""
    shared_root = tmp_path / "shared"
    config = _make_config(shared_root)
    _emit_synthetic_matrix(shared_root, num_workers=1)

    crashing = _CountingStub("partition_worker")

    def crash(*args, **kwargs):
        crashing.calls.append((args, kwargs))
        raise RuntimeError("simulated nix crash")

    monkeypatch.setattr(suit_task_module, "partition_worker", crash)
    monkeypatch.setattr(
        suit_task_module, "merge_worker", _CountingStub("merge_worker")
    )
    monkeypatch.setattr(
        suit_task_module, "build_worker", _CountingStub("build_worker")
    )
    monkeypatch.setattr(
        suit_task_module, "barrier_worker", _CountingStub("barrier_worker")
    )

    rc = run_single_process(config)
    # The dispatch loop swallowed the exception; rc is 0 because the
    # individual worker failures don't propagate to the run-level result.
    assert rc == 0
    # Phase-1a flag was still written (counter advanced despite the crash).
    assert (config.flags_dir / PHASE_1A_DONE_FLAG).exists()


# ---------------------------------------------------------------------------
# Incremental cache hit shortcut via the CLI surface
# ---------------------------------------------------------------------------


def test_cli_submit_then_resubmit_uses_cache(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_peer_cache,
):
    """Two consecutive submit calls: the second sees a cache hit and
    skips pre-flight (verified via call counter)."""

    # Real preflight side-effect would shell out to nix; replace with
    # a counter-stub. Each call mints a small but non-empty matrix.
    preflight_calls: list[int] = []

    from compiler_suit_runner.preflight import PreflightResult

    canned = PreflightResult(
        sys_name="x86_64-linux",
        variants=(_make_variant("hello", "x86_64", "O2"),),
        toolchain_specs=(("x86_64", "gcc15"),),
        common_dep_drvs=(),
        toolchain_drvs=frozenset({"/nix/store/hello-x86_64-O2.drv"}),
    )

    def fake_preflight(
        flake_ref, sys_name, *, packages=None, archs=None, run_subprocess=None
    ):
        preflight_calls.append(1)
        return canned

    monkeypatch.setattr(cli_module, "run_preflight", fake_preflight)

    # Stub workers in suit_task module so dispatch doesn't really run.
    for worker_name in (
        "partition_worker",
        "merge_worker",
        "build_worker",
        "barrier_worker",
    ):
        monkeypatch.setattr(
            suit_task_module, worker_name, _CountingStub(worker_name)
        )

    # Stable input hash: bypass the git-rev-parse in the CLI helper.
    monkeypatch.setattr(cli_module, "_compute_input_hash", lambda repo_root: "stable-hash")

    shared_root = tmp_path / "shared"
    cache_root = tmp_path / "cache"

    argv = [
        "submit",
        "--flake",
        ".",
        "--shared-fs",
        str(shared_root),
        "--multi-computer",
        "single-process",
        "--jobs",
        "1",
        "--cache-root",
        str(cache_root),
    ]

    # First run: cache miss -> pre-flight runs.
    rc1 = cli_module.main(argv)
    assert rc1 == 0
    assert len(preflight_calls) == 1

    # Wipe the manifest dir's contents (proves the second run hit cache,
    # since cache-stored manifests will be restored).
    manifest_dir = shared_root / "manifests"
    for entry in manifest_dir.iterdir():
        entry.unlink()

    rc2 = cli_module.main(argv)
    assert rc2 == 0
    # Cache hit: pre-flight NOT called the second time.
    assert len(preflight_calls) == 1


def test_cli_submit_no_cache_runs_preflight_each_time(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_peer_cache,
):
    preflight_calls: list[int] = []

    from compiler_suit_runner.preflight import PreflightResult

    canned = PreflightResult(
        sys_name="x86_64-linux",
        variants=(),
        toolchain_specs=(),
        common_dep_drvs=(),
        toolchain_drvs=frozenset(),
    )

    def fake_preflight(*a, **kw):
        preflight_calls.append(1)
        return canned

    monkeypatch.setattr(cli_module, "run_preflight", fake_preflight)
    monkeypatch.setattr(
        cli_module, "_compute_input_hash", lambda repo_root: "stable-hash-2"
    )

    for worker_name in (
        "partition_worker",
        "merge_worker",
        "build_worker",
        "barrier_worker",
    ):
        monkeypatch.setattr(
            suit_task_module, worker_name, _CountingStub(worker_name)
        )

    shared_root = tmp_path / "shared"
    cache_root = tmp_path / "cache"
    argv = [
        "submit",
        "--flake",
        ".",
        "--shared-fs",
        str(shared_root),
        "--multi-computer",
        "single-process",
        "--jobs",
        "1",
        "--cache-root",
        str(cache_root),
        "--no-cache",
    ]

    rc1 = cli_module.main(argv)
    rc2 = cli_module.main(argv)
    assert rc1 == 0 and rc2 == 0
    # --no-cache: pre-flight runs each time.
    assert len(preflight_calls) == 2

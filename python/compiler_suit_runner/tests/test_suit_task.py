"""Unit tests for ``compiler_suit_runner.suit_task``.

Hermetic: no real subprocess, no real time elapses, no real harmonia
or Cachix. Worker functions are monkeypatched into recording stubs that
let us assert which item_class triggers which worker.

Sparse-file note: a subset of tests writes real manifests through
``manifest_gen.write_manifest``, which extends each file with
``ftruncate`` to its encoded size (potentially > 1 PiB for phase-1a
ranks). ext4 caps single-file size at 16 TiB; tmpfs is bound only by
RAM. We re-root pytest's tmp tree onto a tmpfs (XDG_RUNTIME_DIR or
/dev/shm) for those tests, mirroring ``test_manifest_gen.py``.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading

import pytest

from compiler_suit_runner.manifest_gen import (
    ManifestSet,
    emit_all_manifests,
    make_common_dep_header,
    make_merge_barrier_header,
    make_merge_header,
    make_partition_barrier_header,
    make_partition_shard_header,
    make_phase2_barrier_header,
    make_toolchain_header,
    make_variant_header,
    write_manifest,
)
from compiler_suit_runner.memory_budget import (
    MEMORY_FLOOR_BYTES,
    PHASE_2_BUILD,
    PHASE_3_VARIANT,
    decode_size,
    encode_size,
)
from compiler_suit_runner.partition import Shard, VariantSpec
from compiler_suit_runner.workers.barrier_worker import (
    PHASE_1A_DONE_FLAG,
    PHASE_1B_DONE_FLAG,
    PHASE_2_DONE_FLAG,
)

import compiler_suit_runner.suit_task as suit_task_module
from compiler_suit_runner.suit_task import (
    PhaseCounter,
    SuitTask,
    SuitTaskConfig,
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
        probe = candidate / f"suit_task_probe_{os.getpid()}"
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
        tempfile.mkdtemp(prefix="suit_task_", dir=str(base))
    )
    tmp_path_factory._basetemp = new_basetemp  # type: ignore[attr-defined]
    yield


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_variant(
    pkg: str = "hello",
    arch: str = "x86_64",
    suffix: str = "O2",
    *,
    compiler_id: str = "gcc15",
    tier: int = 1,
) -> VariantSpec:
    label = f"{pkg}-{arch}-{compiler_id}-{suffix}"
    return {
        "label": label,
        "drv": f"/nix/store/{label}.drv",
        "tarball_name": f"{label}.tar.zst",
        "compiler_id": compiler_id,
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


def _make_config(tmp_path: pathlib.Path, **overrides) -> SuitTaskConfig:
    """Build a SuitTaskConfig rooted at ``tmp_path``."""
    shared = tmp_path / "shared"
    defaults = dict(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=shared,
        manifest_dir=shared / "manifests",
        raw_partition_dir=shared / "partition" / "raw",
        partition_dir=shared / "partition",
        flags_dir=shared / "flags",
        dataset_dir=shared / "dataset",
        peers_dir=shared / "peers",
        run_id="test-run",
        secondary_id="sec-0",
        hostname="localhost",
        harmonia_port=5000,
        enable_harmonia=False,
        cachix_cache=None,
        cachix_token_file=None,
        poll_interval_seconds=0.1,
        barrier_timeout_seconds=10.0,
        input_hash="hash-0",
        toolchain_drvs=frozenset(),
        common_threshold=10,
        variants=(),
    )
    defaults.update(overrides)
    return SuitTaskConfig(**defaults)


class _Recorder:
    """Records calls; can be substituted for any worker function."""

    def __init__(self, name: str, raise_exc: BaseException | None = None) -> None:
        self.name = name
        self.calls: list[tuple] = []
        self.raise_exc = raise_exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        return None


def _emit_minimal_manifest_set(
    tmp_path: pathlib.Path, *, num_workers: int = 2
) -> ManifestSet:
    """Emit a small manifest set covering every item_class.

    One pkg, one arch, two variants, one toolchain, one common dep.
    Barrier sentinels are emitted per worker count.
    """
    target = tmp_path / "shared" / "manifests"
    target.mkdir(parents=True, exist_ok=True)
    variants = (
        _make_variant(suffix="O0"),
        _make_variant(suffix="O2"),
    )
    return emit_all_manifests(
        target_dir=target,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[("/nix/store/aaa-libfoo.drv", "libfoo")],
        num_workers=num_workers,
    )


# ---------------------------------------------------------------------------
# find_binaries
# ---------------------------------------------------------------------------


def test_find_binaries_returns_one_per_manifest(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path)

    # Sanity: emit_all_manifests wrote >= 5 files of varying sizes.
    files = sorted(config.manifest_dir.glob("*.json"))
    assert len(files) == len(manifest_set.headers)

    task = SuitTask(config)
    binaries = task.find_binaries()

    # One BinaryInfo per manifest file on disk.
    assert len(binaries) == len(files)

    # Each .size matches the apparent on-disk size, and each .path is a
    # real file in the manifest dir.
    for binary in binaries:
        path = pathlib.Path(binary.path)
        assert path.parent == config.manifest_dir
        assert binary.size == path.stat().st_size

    # Sorting by .size DESC matches what the framework would do; the
    # five highest-rank items (rank 6 — phase1a_partition shards) come
    # first.
    sorted_desc = sorted(binaries, key=lambda b: b.size, reverse=True)
    rank_order = [decode_size(b.size)[0] for b in sorted_desc]
    # The manifest_set has one shard (1 (pkg,arch) pair). After it we get
    # the barriers/merge/etc in descending rank order.
    assert rank_order == sorted(rank_order, reverse=True)


def test_find_binaries_diverse_ranks_present(tmp_path: pathlib.Path):
    """At least 5 manifests across multiple ranks."""
    config = _make_config(tmp_path)
    _emit_minimal_manifest_set(tmp_path, num_workers=2)

    task = SuitTask(config)
    binaries = task.find_binaries()

    assert len(binaries) >= 5
    ranks = {decode_size(b.size)[0] for b in binaries}
    # We expect ranks: 6 (phase1a), 5 (phase1a_barrier), 4 (phase1b),
    # 3 (phase1b_barrier), 2 (phase2 build), 1 (phase2_barrier),
    # 0 (phase3_variant). Some may be skipped if zero items at that
    # rank, but we have at least 4 distinct ranks here.
    assert len(ranks) >= 4


def test_find_binaries_missing_dir(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    # Don't create manifest_dir.
    task = SuitTask(config)
    assert task.find_binaries() == []


def test_find_binaries_explicit_dir_override(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    other = tmp_path / "other"
    task = SuitTask(config)
    assert task.find_binaries(other) == []


# ---------------------------------------------------------------------------
# estimate_memory
# ---------------------------------------------------------------------------


def test_estimate_memory_decodes_low_bits(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    task = SuitTask(config)

    two_gib = 2 * 1024 * 1024 * 1024
    encoded = encode_size(3, two_gib)  # rank 3 here is irrelevant
    assert task.estimate_memory(encoded) == two_gib


def test_estimate_memory_floor_enforced(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    task = SuitTask(config)

    # Pack rank 0 with a tiny memory request; the encode step floors at
    # MEMORY_FLOOR_BYTES, and estimate_memory re-clamps defensively.
    encoded = encode_size(0, 1024)
    assert task.estimate_memory(encoded) == MEMORY_FLOOR_BYTES


def test_estimate_memory_negative_returns_floor(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    task = SuitTask(config)
    # decode_size raises on negative input; we floor.
    assert task.estimate_memory(-1) == MEMORY_FLOOR_BYTES


# ---------------------------------------------------------------------------
# dispatch_binary — routing
# ---------------------------------------------------------------------------


def _patch_workers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    """Replace each worker with a Recorder; return the recorders by name."""
    recorders = {
        "partition_worker": _Recorder("partition_worker"),
        "merge_worker": _Recorder("merge_worker"),
        "build_worker": _Recorder("build_worker"),
        "barrier_worker": _Recorder("barrier_worker"),
    }
    # Patch the names imported into the suit_task module's namespace.
    for name, recorder in recorders.items():
        monkeypatch.setattr(suit_task_module, name, recorder)
    return recorders


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Recorder]:
    return _patch_workers(monkeypatch)


def _binaries_by_name(
    task: SuitTask,
) -> dict[str, object]:
    return {pathlib.Path(b.path).name: b for b in task.find_binaries()}


@pytest.mark.parametrize(
    "filename_prefix,recorder_key",
    [
        ("hello__x86_64", "partition_worker"),
        ("phase1a_barrier_0000", "barrier_worker"),
        ("phase1b_merge", "merge_worker"),
        ("phase1b_barrier_0000", "barrier_worker"),
        ("toolchain__x86_64__gcc15", "build_worker"),
        ("common_dep__libfoo", "build_worker"),
        ("phase2_barrier_0000", "barrier_worker"),
    ],
)
def test_dispatch_binary_routes_correctly(
    tmp_path: pathlib.Path,
    recorders: dict[str, _Recorder],
    filename_prefix: str,
    recorder_key: str,
):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path, num_workers=2)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    binaries = _binaries_by_name(task)
    matches = [b for name, b in binaries.items() if name.startswith(filename_prefix)]
    assert matches, f"no manifest found with prefix {filename_prefix!r}"
    binary = matches[0]

    task.dispatch_binary(binary)

    recorder = recorders[recorder_key]
    assert len(recorder.calls) == 1, (
        f"expected exactly one call to {recorder_key}, got {recorder.calls}"
    )

    # Every other recorder is untouched.
    for key, other in recorders.items():
        if key == recorder_key:
            continue
        assert not other.calls, f"unexpected dispatch to {key}: {other.calls}"


def test_dispatch_binary_routes_phase3_variant(
    tmp_path: pathlib.Path,
    recorders: dict[str, _Recorder],
):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    binaries = _binaries_by_name(task)
    # Variant labels look like "<pkg>-<arch>-<compiler>-<suffix>".
    variant_files = [
        b for name, b in binaries.items()
        if name.startswith("hello-x86_64-gcc15-")
    ]
    assert variant_files, "no phase-3 variant manifest emitted"
    task.dispatch_binary(variant_files[0])
    assert len(recorders["build_worker"].calls) == 1
    assert not recorders["partition_worker"].calls
    assert not recorders["barrier_worker"].calls


# ---------------------------------------------------------------------------
# dispatch_binary — counter increment + flag write
# ---------------------------------------------------------------------------


def test_dispatch_binary_increments_counter(
    tmp_path: pathlib.Path,
    recorders: dict[str, _Recorder],
):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    binaries = _binaries_by_name(task)

    # Find the rank-6 (phase1a_partition) counter — there is exactly one
    # shard in our minimal set, so dispatching it should both increment
    # the counter and write the phase1a_done flag.
    flag_path = config.flags_dir / PHASE_1A_DONE_FLAG
    assert not flag_path.exists()

    shard = [b for name, b in binaries.items() if name.startswith("hello__")][0]
    task.dispatch_binary(shard)

    counter = task._counters[6]
    assert counter.completed == 1
    assert counter.is_complete
    assert flag_path.exists()


def test_dispatch_binary_failure_still_increments(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    crashing = _Recorder(
        "partition_worker", raise_exc=RuntimeError("nix crashed")
    )
    monkeypatch.setattr(suit_task_module, "partition_worker", crashing)
    # Other workers are also stubbed (so non-target items don't try to
    # do real work).
    for name in ("merge_worker", "build_worker", "barrier_worker"):
        monkeypatch.setattr(suit_task_module, name, _Recorder(name))

    binaries = _binaries_by_name(task)
    shard = [b for name, b in binaries.items() if name.startswith("hello__")][0]
    task.dispatch_binary(shard)

    # Worker raised, but the counter still advanced — and the flag file
    # is still written, so phase-1a-barrier sentinels can unblock.
    counter = task._counters[6]
    assert counter.completed == 1
    assert (config.flags_dir / PHASE_1A_DONE_FLAG).exists()


def test_dispatch_binary_phase2_shared_counter(
    tmp_path: pathlib.Path,
    recorders: dict[str, _Recorder],
):
    """Phase-2 toolchain + common-dep items share counter rank 2."""
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    # The minimal set has 1 toolchain + 1 common dep at rank 2.
    counter = task._counters[2]
    assert counter.expected == 2
    assert counter.flag_name == PHASE_2_DONE_FLAG

    flag_path = config.flags_dir / PHASE_2_DONE_FLAG
    assert not flag_path.exists()

    binaries = _binaries_by_name(task)
    toolchain = [
        b for name, b in binaries.items() if name.startswith("toolchain__")
    ][0]
    common = [
        b for name, b in binaries.items() if name.startswith("common_dep__")
    ][0]

    task.dispatch_binary(toolchain)
    assert not flag_path.exists()  # only 1 of 2 done
    task.dispatch_binary(common)
    assert flag_path.exists()  # all done


# ---------------------------------------------------------------------------
# initialize_counters
# ---------------------------------------------------------------------------


def test_initialize_counters_from_manifest_set(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path, num_workers=3)
    task = SuitTask(config)
    task.initialize_counters(manifest_set)

    # Counters by rank: 6 (1 shard), 5 (3 phase1a barriers), 4 (1 merge),
    # 3 (3 phase1b barriers), 2 (1 toolchain + 1 common = 2),
    # 1 (3 phase2 barriers), 0 (2 variants).
    expected = {6: 1, 5: 3, 4: 1, 3: 3, 2: 2, 1: 3, 0: 2}
    actual = {rank: counter.expected for rank, counter in task._counters.items()}
    assert actual == expected

    # Barrier-bound ranks (those that *write* a flag when complete)
    # have flag_name set.
    assert task._counters[6].flag_name == PHASE_1A_DONE_FLAG
    assert task._counters[4].flag_name == PHASE_1B_DONE_FLAG
    assert task._counters[2].flag_name == PHASE_2_DONE_FLAG
    # Sentinel ranks never write a flag of their own.
    assert task._counters[5].flag_name is None
    assert task._counters[3].flag_name is None
    assert task._counters[1].flag_name is None
    assert task._counters[0].flag_name is None


def test_initialize_counters_from_directory(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    manifest_set = _emit_minimal_manifest_set(tmp_path, num_workers=2)

    task = SuitTask(config)
    task.initialize_counters(config.manifest_dir)

    assert sum(c.expected for c in task._counters.values()) == len(
        manifest_set.headers
    )


# ---------------------------------------------------------------------------
# setup_peer_cache / teardown
# ---------------------------------------------------------------------------


@pytest.fixture
def peer_cache_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Replace generate_signing_key / announce_self / PeerListWatcher /
    HarmoniaProcess / CachixUploader with sequence-recording fakes that
    never spawn a real process."""

    state: dict[str, list] = {
        "generate_signing_key": [],
        "announce_self": [],
        "withdraw_self": [],
        "watcher_started": [],
        "watcher_stopped": [],
        "harmonia_started": [],
        "harmonia_stopped": [],
        "cachix_started": [],
        "cachix_stopped": [],
    }

    class _FakeSigningKey:
        def __init__(self, run_id: str, base: pathlib.Path) -> None:
            self.name = f"asm-suit-cluster-{run_id}"
            self.secret_path = base / "peers" / "__signing-key"
            self.public_path = base / "peers" / "__public-key"
            self.public_key = "asm-suit-cluster-test:fake-pubkey"

    def _fake_generate_signing_key(shared_fs, run_id):
        state["generate_signing_key"].append((shared_fs, run_id))
        # Match the real behaviour: ensure the peers/ subdir exists.
        peers = pathlib.Path(shared_fs) / "peers"
        peers.mkdir(parents=True, exist_ok=True)
        return _FakeSigningKey(run_id, pathlib.Path(shared_fs))

    def _fake_announce_self(shared_fs, info):
        state["announce_self"].append((shared_fs, info))
        return pathlib.Path(shared_fs) / "peers" / f"{info.secondary_id}.json"

    def _fake_withdraw_self(shared_fs, secondary_id):
        state["withdraw_self"].append((shared_fs, secondary_id))

    class _FakeWatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.extra_args = []

        def start(self):
            state["watcher_started"].append(self.kwargs)

        def stop(self):
            state["watcher_stopped"].append(self.kwargs)

    class _FakeHarmonia:
        def __init__(self, *, bind_addr, signing_key_path, **_kwargs):
            self.bind_addr = bind_addr
            self.signing_key_path = signing_key_path

        def start(self):
            state["harmonia_started"].append(self.bind_addr)

        def stop(self, timeout: float = 10.0):
            state["harmonia_stopped"].append(self.bind_addr)

    class _FakeCachix:
        def __init__(self, cfg, **_kwargs):
            self.cfg = cfg

        def start(self):
            state["cachix_started"].append(self.cfg.cache_name)

        def stop(self):
            state["cachix_stopped"].append(self.cfg.cache_name)

    monkeypatch.setattr(
        suit_task_module, "generate_signing_key", _fake_generate_signing_key
    )
    monkeypatch.setattr(
        suit_task_module, "announce_self", _fake_announce_self
    )
    monkeypatch.setattr(
        suit_task_module, "withdraw_self", _fake_withdraw_self
    )
    monkeypatch.setattr(
        suit_task_module, "PeerListWatcher", _FakeWatcher
    )
    monkeypatch.setattr(
        suit_task_module, "HarmoniaProcess", _FakeHarmonia
    )
    monkeypatch.setattr(
        suit_task_module, "CachixUploader", _FakeCachix
    )

    return state


def test_setup_peer_cache_idempotent(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    config = _make_config(
        tmp_path, enable_harmonia=False, cachix_cache=None
    )
    task = SuitTask(config)
    task.setup_peer_cache()
    task.setup_peer_cache()  # second call must be a no-op

    assert len(peer_cache_stubs["generate_signing_key"]) == 1
    assert len(peer_cache_stubs["announce_self"]) == 1
    assert len(peer_cache_stubs["watcher_started"]) == 1


def test_setup_peer_cache_skips_harmonia_when_disabled(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    config = _make_config(tmp_path, enable_harmonia=False)
    task = SuitTask(config)
    task.setup_peer_cache()
    assert peer_cache_stubs["harmonia_started"] == []


def test_setup_peer_cache_skips_harmonia_when_binary_missing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    peer_cache_stubs: dict[str, list],
):
    """If HarmoniaProcess.start raises FileNotFoundError, swallow it."""

    class _MissingHarmonia:
        def __init__(self, *, bind_addr, signing_key_path, **_kwargs):
            pass

        def start(self):
            raise FileNotFoundError("harmonia not on PATH")

        def stop(self, timeout: float = 10.0):
            pass

    monkeypatch.setattr(suit_task_module, "HarmoniaProcess", _MissingHarmonia)

    config = _make_config(tmp_path, enable_harmonia=True)
    task = SuitTask(config)
    task.setup_peer_cache()  # must not raise

    # Harmonia was attempted but failed; teardown still works.
    task.teardown()


def test_setup_peer_cache_skips_cachix_when_unset(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    config = _make_config(tmp_path, cachix_cache=None)
    task = SuitTask(config)
    task.setup_peer_cache()
    assert peer_cache_stubs["cachix_started"] == []


def test_setup_peer_cache_starts_cachix_when_configured(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    token = tmp_path / "token"
    token.write_text("secret")
    os.chmod(token, 0o600)
    config = _make_config(
        tmp_path, cachix_cache="asm-dataset-test", cachix_token_file=token
    )
    task = SuitTask(config)
    task.setup_peer_cache()
    assert peer_cache_stubs["cachix_started"] == ["asm-dataset-test"]


def test_teardown_idempotent_without_setup(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    """teardown() must be safe even if setup_peer_cache() never ran."""
    config = _make_config(tmp_path)
    task = SuitTask(config)
    task.teardown()
    task.teardown()  # double-teardown also fine.
    # No watcher/harmonia/cachix to stop, but withdraw_self runs on
    # both calls — that's fine, withdraw_self handles missing files.
    assert len(peer_cache_stubs["withdraw_self"]) >= 2


def test_teardown_after_setup_stops_components(
    tmp_path: pathlib.Path, peer_cache_stubs: dict[str, list]
):
    token = tmp_path / "token"
    token.write_text("secret")
    os.chmod(token, 0o600)
    config = _make_config(
        tmp_path,
        enable_harmonia=True,
        cachix_cache="asm-dataset-test",
        cachix_token_file=token,
    )
    task = SuitTask(config)
    task.setup_peer_cache()
    task.teardown()

    assert peer_cache_stubs["watcher_stopped"]
    assert peer_cache_stubs["harmonia_stopped"]
    assert peer_cache_stubs["cachix_stopped"]
    assert peer_cache_stubs["withdraw_self"]


# ---------------------------------------------------------------------------
# Framework Protocol surface
# ---------------------------------------------------------------------------


def test_protocol_default_methods_are_safe(tmp_path: pathlib.Path):
    config = _make_config(tmp_path)
    task = SuitTask(config)

    assert task.get_stages() == []
    assert task.organize_and_sort_items([1, 2, 3]) == [1, 2, 3]
    assert isinstance(task.get_worker_module(), str)
    assert task.add_task_arguments(None) is None  # type: ignore[arg-type]
    assert task.build_worker_command_args(None, tmp_path, tmp_path, False) == []
    assert task.get_output_filename_pattern("foo.json") == "foo.json"
    assert task.get_reserved_memory_per_worker() == 0


# ---------------------------------------------------------------------------
# PhaseCounter
# ---------------------------------------------------------------------------


def test_phase_counter_is_complete():
    counter = PhaseCounter(expected=3, completed=0)
    assert not counter.is_complete
    counter.completed = 2
    assert not counter.is_complete
    counter.completed = 3
    assert counter.is_complete
    counter.completed = 4
    assert counter.is_complete  # over-count is still complete


def test_phase_counter_zero_expected_never_complete():
    counter = PhaseCounter(expected=0, completed=0)
    assert not counter.is_complete  # avoids spurious flag write at startup

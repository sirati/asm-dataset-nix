"""Tests for the local incremental partition cache."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import tarfile

import pytest

from compiler_suit_runner.incremental_cache import (
    CacheEntry,
    IncrementalCache,
    InputHashInputs,
    InvocationAxes,
    collect_input_hash_inputs,
    compute_input_hash,
)


# --------------------------------------------------------------------- helpers


@dataclasses.dataclass
class FakeCompletedProcess:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


def _make_run_subprocess(
    *,
    rev_returncode: int = 0,
    rev_stdout: bytes = b"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n",
    rev_stderr: bytes = b"",
    diff_returncode: int = 0,
    diff_stdout: bytes = b"",
    diff_stderr: bytes = b"",
):
    calls = []

    def run_subprocess(cmd, *, cwd):
        calls.append((tuple(cmd), pathlib.Path(cwd)))
        if cmd[:2] == ["git", "rev-parse"]:
            return FakeCompletedProcess(
                returncode=rev_returncode,
                stdout=rev_stdout,
                stderr=rev_stderr,
            )
        if cmd[:2] == ["git", "diff"]:
            return FakeCompletedProcess(
                returncode=diff_returncode,
                stdout=diff_stdout,
                stderr=diff_stderr,
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    return run_subprocess, calls


def _populate_pre_flight_inputs(tmp_path: pathlib.Path) -> tuple[
    pathlib.Path, pathlib.Path, pathlib.Path
]:
    """Create the three sources for ``IncrementalCache.store`` and return
    the partition path, the manifests directory, and the meta path."""
    partition_path = tmp_path / "partition.json"
    partition_path.write_text(json.dumps({"version": 1, "variants": ["x"]}))

    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "a.json").write_text(json.dumps({"a": 1}))
    (manifests_dir / "b.json").write_text(json.dumps({"b": 2}))

    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({"meta": "info"}))
    return partition_path, manifests_dir, meta_path


# ---------------------------------------------------------- compute_input_hash


def test_compute_input_hash_deterministic():
    inputs = InputHashInputs(
        flake_lock=b"lock-contents",
        git_rev="a" * 40,
        git_diff=b"diff lines\n",
    )
    h1 = compute_input_hash(inputs)
    h2 = compute_input_hash(inputs)
    assert h1 == h2
    # sha256 hex is 64 chars
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_compute_input_hash_changes_with_each_field():
    base = InputHashInputs(
        flake_lock=b"lock", git_rev="a" * 40, git_diff=b"diff"
    )
    h_base = compute_input_hash(base)
    h_lock = compute_input_hash(dataclasses.replace(base, flake_lock=b"other"))
    h_rev = compute_input_hash(dataclasses.replace(base, git_rev="b" * 40))
    h_diff = compute_input_hash(dataclasses.replace(base, git_diff=b"other"))
    # Changing any single field changes the hash.
    assert len({h_base, h_lock, h_rev, h_diff}) == 4


def test_compute_input_hash_boundary_swap_distinct():
    # Without length prefixes, both inputs would concatenate to the same
    # bytes. With prefixes, they must differ.
    a = InputHashInputs(flake_lock=b"abc", git_rev="def", git_diff=b"")
    b = InputHashInputs(flake_lock=b"abcdef", git_rev="", git_diff=b"")
    assert compute_input_hash(a) != compute_input_hash(b)

    c = InputHashInputs(flake_lock=b"", git_rev="", git_diff=b"abcdef")
    d = InputHashInputs(flake_lock=b"abc", git_rev="", git_diff=b"def")
    assert compute_input_hash(c) != compute_input_hash(d)


# --------------------------------------------------------- InvocationAxes


def _axes(**overrides) -> InvocationAxes:
    base = dict(
        packages=["zlib", "lz4"],
        archs=["x86_64", "aarch64"],
        variant_sample=2,
        variant_seed="42",
        sys_name="x86_64-linux",
        build_compilers=False,
        debug_testbuild=None,
        toolchain_dedup=True,
    )
    base.update(overrides)
    return InvocationAxes.from_values(**base)


def _hash_with_axes(axes: InvocationAxes) -> str:
    return compute_input_hash(
        InputHashInputs(
            flake_lock=b"lock",
            git_rev="a" * 40,
            git_diff=b"",
            invocation=axes.canonical_bytes(),
        )
    )


def test_invocation_axes_canonicalize_order_and_duplicates():
    a = InvocationAxes.from_values(
        packages=["zlib", "lz4", "zlib"], archs=["x86_64", "aarch64"]
    )
    b = InvocationAxes.from_values(
        packages=["lz4", "zlib"], archs=["aarch64", "x86_64"]
    )
    assert a == b
    assert a.canonical_bytes() == b.canonical_bytes()


def test_invocation_axes_none_distinct_from_explicit_list():
    all_pkgs = _axes(packages=None)
    some_pkgs = _axes(packages=["zlib"])
    assert all_pkgs.canonical_bytes() != some_pkgs.canonical_bytes()
    assert _hash_with_axes(all_pkgs) != _hash_with_axes(some_pkgs)


def test_input_hash_identical_invocation_is_stable():
    h1 = _hash_with_axes(_axes(packages=["zlib", "lz4"]))
    h2 = _hash_with_axes(_axes(packages=["lz4", "zlib"]))
    assert h1 == h2


def test_input_hash_changes_with_packages():
    nano = _hash_with_axes(_axes(packages=["zlib"]))
    full = _hash_with_axes(
        _axes(packages=["zlib", "lz4", "xz", "bzip2"])
    )
    assert nano != full


def test_input_hash_changes_with_archs():
    h_two = _hash_with_axes(_axes(archs=["x86_64", "aarch64"]))
    h_all = _hash_with_axes(_axes(archs=None))
    h_one = _hash_with_axes(_axes(archs=["x86_64"]))
    assert len({h_two, h_all, h_one}) == 3


def test_input_hash_changes_with_variant_sample():
    assert _hash_with_axes(_axes(variant_sample=2)) != _hash_with_axes(
        _axes(variant_sample=0)
    )


def test_input_hash_changes_with_each_remaining_axis():
    base = _hash_with_axes(_axes())
    variations = {
        "variant_seed": _hash_with_axes(_axes(variant_seed="43")),
        "sys_name": _hash_with_axes(_axes(sys_name="aarch64-linux")),
        "build_compilers": _hash_with_axes(_axes(build_compilers=True)),
        "debug_testbuild": _hash_with_axes(_axes(debug_testbuild="hello")),
        "toolchain_dedup": _hash_with_axes(_axes(toolchain_dedup=False)),
    }
    hashes = {base, *variations.values()}
    assert len(hashes) == 1 + len(variations)


def test_input_hash_invocation_distinct_from_repo_only():
    """A keyed-with-axes hash never collides with the axes-free hash of
    the same repo state (the contamination scenario)."""
    repo_only = compute_input_hash(
        InputHashInputs(flake_lock=b"lock", git_rev="a" * 40, git_diff=b"")
    )
    assert repo_only != _hash_with_axes(_axes())


# ---------------------------------------------------- collect_input_hash_inputs


def test_collect_input_hash_inputs_happy_path(tmp_path: pathlib.Path):
    flake_lock_bytes = b'{"nodes":{}}'
    read_calls = []

    def read_bytes(path: pathlib.Path) -> bytes:
        read_calls.append(path)
        return flake_lock_bytes

    run_subprocess, calls = _make_run_subprocess(
        rev_stdout=b"1234567890abcdef1234567890abcdef12345678\n",
        diff_stdout=b"diff --git a/x b/x\n",
    )

    result = collect_input_hash_inputs(
        tmp_path,
        run_subprocess=run_subprocess,
        read_bytes=read_bytes,
    )

    assert result.flake_lock == flake_lock_bytes
    assert result.git_rev == "1234567890abcdef1234567890abcdef12345678"
    assert result.git_diff == b"diff --git a/x b/x\n"

    # flake.lock was read from the right path
    assert read_calls == [tmp_path / "flake.lock"]
    # both git commands ran in repo_root
    assert all(cwd == tmp_path for _, cwd in calls)
    cmds = [cmd for cmd, _ in calls]
    assert ("git", "rev-parse", "HEAD") in cmds
    assert ("git", "diff") in cmds


def test_collect_input_hash_inputs_carries_invocation(tmp_path: pathlib.Path):
    """``invocation=`` lands as canonical bytes; omitted -> empty."""

    def read_bytes(path: pathlib.Path) -> bytes:
        return b"lock"

    run_subprocess, _ = _make_run_subprocess()
    axes = _axes()

    with_axes = collect_input_hash_inputs(
        tmp_path,
        invocation=axes,
        run_subprocess=run_subprocess,
        read_bytes=read_bytes,
    )
    assert with_axes.invocation == axes.canonical_bytes()

    without_axes = collect_input_hash_inputs(
        tmp_path,
        run_subprocess=run_subprocess,
        read_bytes=read_bytes,
    )
    assert without_axes.invocation == b""
    assert compute_input_hash(with_axes) != compute_input_hash(without_axes)


def test_collect_input_hash_inputs_subprocess_failure(tmp_path: pathlib.Path):
    """Non-zero rev-parse returncode raises RuntimeError."""

    def read_bytes(path: pathlib.Path) -> bytes:
        return b"lock"

    run_subprocess, _ = _make_run_subprocess(
        rev_returncode=128,
        rev_stderr=b"fatal: not a git repository\n",
    )

    with pytest.raises(RuntimeError, match="git rev-parse"):
        collect_input_hash_inputs(
            tmp_path,
            run_subprocess=run_subprocess,
            read_bytes=read_bytes,
        )


def test_collect_input_hash_inputs_diff_failure(tmp_path: pathlib.Path):
    def read_bytes(path: pathlib.Path) -> bytes:
        return b"lock"

    run_subprocess, _ = _make_run_subprocess(
        diff_returncode=1,
        diff_stderr=b"oops\n",
    )

    with pytest.raises(RuntimeError, match="git diff"):
        collect_input_hash_inputs(
            tmp_path,
            run_subprocess=run_subprocess,
            read_bytes=read_bytes,
        )


def test_collect_input_hash_inputs_missing_git(tmp_path: pathlib.Path):
    """``git`` not on PATH surfaces as ``RuntimeError``."""

    def read_bytes(path: pathlib.Path) -> bytes:
        return b"lock"

    def run_subprocess(cmd, *, cwd):
        raise FileNotFoundError("git")

    with pytest.raises(RuntimeError, match="git not found"):
        collect_input_hash_inputs(
            tmp_path,
            run_subprocess=run_subprocess,
            read_bytes=read_bytes,
        )


def test_collect_input_hash_inputs_missing_flake_lock(tmp_path: pathlib.Path):
    def read_bytes(path: pathlib.Path) -> bytes:
        raise FileNotFoundError(str(path))

    run_subprocess, _ = _make_run_subprocess()

    with pytest.raises(RuntimeError, match="flake.lock"):
        collect_input_hash_inputs(
            tmp_path,
            run_subprocess=run_subprocess,
            read_bytes=read_bytes,
        )


# --------------------------------------------------------- IncrementalCache


def test_lookup_missing_dir_returns_none(tmp_path: pathlib.Path):
    cache = IncrementalCache(tmp_path / "cache")
    assert cache.lookup("deadbeef") is None


def test_lookup_partial_entry_returns_none(tmp_path: pathlib.Path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    entry_dir = cache_root / "abc123"
    entry_dir.mkdir()
    # Only 2 of 3 files
    (entry_dir / "partition.json").write_text("{}")
    (entry_dir / "meta.json").write_text("{}")

    cache = IncrementalCache(cache_root)
    assert cache.lookup("abc123") is None


def test_lookup_complete_returns_entry(tmp_path: pathlib.Path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    entry_dir = cache_root / "abc123"
    entry_dir.mkdir()
    (entry_dir / "partition.json").write_text(json.dumps({"v": 1}))
    (entry_dir / "manifests.tar").write_bytes(b"")
    (entry_dir / "meta.json").write_text("{}")

    cache = IncrementalCache(cache_root)
    entry = cache.lookup("abc123")
    assert entry is not None
    assert entry.input_hash == "abc123"
    assert entry.partition_path == entry_dir / "partition.json"
    assert entry.manifests_archive == entry_dir / "manifests.tar"
    assert entry.meta_path == entry_dir / "meta.json"
    assert entry.is_complete is True


def test_is_complete_reflects_files(tmp_path: pathlib.Path):
    entry = CacheEntry(
        input_hash="x",
        partition_path=tmp_path / "p.json",
        manifests_archive=tmp_path / "m.tar",
        meta_path=tmp_path / "meta.json",
    )
    assert entry.is_complete is False
    entry.partition_path.write_text("{}")
    assert entry.is_complete is False
    entry.manifests_archive.write_bytes(b"")
    assert entry.is_complete is False
    entry.meta_path.write_text("{}")
    assert entry.is_complete is True


def test_store_round_trip(tmp_path: pathlib.Path):
    partition_path, manifests_dir, meta_path = _populate_pre_flight_inputs(
        tmp_path
    )

    cache = IncrementalCache(tmp_path / "cache")
    stored = cache.store("h1", partition_path, manifests_dir, meta_path)

    # Lookup returns a populated entry
    looked_up = cache.lookup("h1")
    assert looked_up is not None
    assert looked_up.is_complete
    assert looked_up.partition_path == stored.partition_path

    # Partition contents readable
    data = json.loads(looked_up.partition_path.read_text())
    assert data == {"version": 1, "variants": ["x"]}

    # meta.json contents readable
    meta = json.loads(looked_up.meta_path.read_text())
    assert meta == {"meta": "info"}

    # manifests.tar extractable and contains the manifest files
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(looked_up.manifests_archive) as tf:
        tf.extractall(extract_dir, filter="data")

    extracted_files = sorted(p.name for p in (extract_dir / "manifests").iterdir())
    assert extracted_files == ["a.json", "b.json"]
    assert json.loads(
        (extract_dir / "manifests" / "a.json").read_text()
    ) == {"a": 1}


def test_store_does_not_clobber_existing(tmp_path: pathlib.Path):
    partition_path, manifests_dir, meta_path = _populate_pre_flight_inputs(
        tmp_path
    )

    cache_root = tmp_path / "cache"
    cache = IncrementalCache(cache_root)

    # Pre-create the target dir with sentinel content (simulates a race).
    target = cache_root / "h1"
    target.mkdir(parents=True)
    sentinel_partition = target / "partition.json"
    sentinel_partition.write_text(json.dumps({"sentinel": True}))
    (target / "manifests.tar").write_bytes(b"sentinel-tar")
    (target / "meta.json").write_text(json.dumps({"sentinel": True}))

    cache.store("h1", partition_path, manifests_dir, meta_path)

    # Existing content is preserved, NOT overwritten.
    assert json.loads(sentinel_partition.read_text()) == {"sentinel": True}
    assert (target / "manifests.tar").read_bytes() == b"sentinel-tar"

    # The tmp dir is cleaned up — no .tmp.* siblings left behind.
    leftovers = [
        p for p in cache_root.iterdir() if ".tmp" in p.name
    ]
    assert leftovers == []


def test_invalidate_removes_entry(tmp_path: pathlib.Path):
    partition_path, manifests_dir, meta_path = _populate_pre_flight_inputs(
        tmp_path
    )
    cache = IncrementalCache(tmp_path / "cache")
    cache.store("h1", partition_path, manifests_dir, meta_path)
    assert cache.lookup("h1") is not None

    cache.invalidate("h1")
    assert cache.lookup("h1") is None
    assert not (tmp_path / "cache" / "h1").exists()


def test_invalidate_idempotent(tmp_path: pathlib.Path):
    cache = IncrementalCache(tmp_path / "cache")
    # Calling on a missing entry must not raise.
    cache.invalidate("never-existed")
    cache.invalidate("never-existed")  # twice for good measure


def test_clear_counts_and_removes(tmp_path: pathlib.Path):
    partition_path, manifests_dir, meta_path = _populate_pre_flight_inputs(
        tmp_path
    )
    cache_root = tmp_path / "cache"
    cache = IncrementalCache(cache_root)
    cache.store("h1", partition_path, manifests_dir, meta_path)
    cache.store("h2", partition_path, manifests_dir, meta_path)

    # Drop a non-entry file at the root; it must NOT be counted.
    cache_root.mkdir(exist_ok=True)
    (cache_root / "stray.txt").write_text("not an entry")
    # And a leftover tmp dir from a hypothetical aborted run.
    (cache_root / "abc.tmp.999").mkdir()

    n = cache.clear()
    assert n == 2
    assert not cache_root.exists()


def test_clear_on_empty_cache(tmp_path: pathlib.Path):
    cache = IncrementalCache(tmp_path / "never-created")
    assert cache.clear() == 0


def test_clear_ignores_non_entry_files(tmp_path: pathlib.Path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "junk.txt").write_text("hi")
    (cache_root / "h1").mkdir()
    (cache_root / "h1" / "partition.json").write_text("{}")
    (cache_root / "h1" / "manifests.tar").write_bytes(b"")
    (cache_root / "h1" / "meta.json").write_text("{}")

    cache = IncrementalCache(cache_root)
    n = cache.clear()
    assert n == 1
    assert not cache_root.exists()

"""Tests for the local enumeration-memoization cache."""
from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from compiler_suit_runner.incremental_cache import (
    IncrementalCache,
    InputHashInputs,
    ToolchainAxes,
    VariantAxes,
    collect_input_hash_inputs,
    compute_input_hash,
    compute_subentry_key,
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


_REPO = InputHashInputs(
    flake_lock=b"lock", git_rev="a" * 40, git_diff=b""
)

_TC_PAIRS = (("aarch64", "clang18"), ("x86_64", "gcc15"))
_TC_DRVS = {
    ("aarch64", "clang18"): "/nix/store/clang18.drv",
    ("x86_64", "gcc15"): "/nix/store/gcc15.drv",
}


def _tc_agg(tmp_path: pathlib.Path) -> str:
    """An aggregate drv path that actually exists on disk.

    ``lookup_toolchains`` verifies the aggregate drv still exists (the
    GC-staleness guard) before returning a hit, so hit-asserting tests
    must store a real file rather than a fake ``/nix/store/...`` path.
    """
    path = tmp_path / "toolchains.drv"
    if not path.exists():
        path.write_text("")
    return str(path)


_VARIANTS = {
    "hello": {
        "archs": ["aarch64", "x86_64"],
        "sample_size": 2,
        "sample_seed": "42",
        "tier": 1,
    },
    "zlib": {
        "archs": ["x86_64"],
        "sample_size": 0,
        "sample_seed": "7",
        "tier": 2,
    },
}


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


# ------------------------------------------------- ToolchainAxes / VariantAxes


def test_toolchain_axes_canonicalize_order_and_duplicates():
    a = ToolchainAxes.from_values(archs=["x86_64", "aarch64", "x86_64"])
    b = ToolchainAxes.from_values(archs=["aarch64", "x86_64"])
    assert a == b
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.archs == ("aarch64", "x86_64")


def test_variant_axes_canonicalize_order_and_duplicates():
    a = VariantAxes.from_values(
        packages=["zlib", "lz4", "zlib"], archs=["x86_64", "aarch64"]
    )
    b = VariantAxes.from_values(
        packages=["lz4", "zlib"], archs=["aarch64", "x86_64"]
    )
    assert a == b
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.packages == ("lz4", "zlib")
    assert a.archs == ("aarch64", "x86_64")


def test_axes_none_distinct_from_explicit_list():
    assert (
        ToolchainAxes.from_values(archs=None).canonical_bytes()
        != ToolchainAxes.from_values(archs=["x86_64"]).canonical_bytes()
    )
    assert (
        VariantAxes.from_values(packages=None).canonical_bytes()
        != VariantAxes.from_values(packages=["zlib"]).canonical_bytes()
    )


def test_axes_kind_discriminator_separates_namespaces():
    """A toolchains key can never collide with a variants key, even when
    the shared fields coincide — the canonical bytes embed a kind tag
    (and the field sets differ anyway)."""
    tc = ToolchainAxes.from_values(sys_name="x86_64-linux", archs=["x86_64"])
    var = VariantAxes.from_values(sys_name="x86_64-linux", archs=["x86_64"])
    assert tc.canonical_bytes() != var.canonical_bytes()
    assert compute_subentry_key(_REPO, tc) != compute_subentry_key(_REPO, var)


def test_subentry_key_stable_for_identical_invocation():
    k1 = compute_subentry_key(
        _REPO, VariantAxes.from_values(packages=["zlib", "lz4"])
    )
    k2 = compute_subentry_key(
        _REPO, VariantAxes.from_values(packages=["lz4", "zlib"])
    )
    assert k1 == k2


def test_packages_change_misses_variants_but_not_toolchains():
    """``--packages`` is a variants-only axis: the toolchains key is
    unchanged while the variants key splits."""
    tc_a = compute_subentry_key(_REPO, ToolchainAxes.from_values())
    tc_b = compute_subentry_key(_REPO, ToolchainAxes.from_values())
    var_a = compute_subentry_key(
        _REPO, VariantAxes.from_values(packages=["zlib"])
    )
    var_b = compute_subentry_key(
        _REPO, VariantAxes.from_values(packages=["zlib", "lz4"])
    )
    assert tc_a == tc_b
    assert var_a != var_b


def test_archs_change_misses_both_subentries():
    tc_a = compute_subentry_key(
        _REPO, ToolchainAxes.from_values(archs=["x86_64"])
    )
    tc_b = compute_subentry_key(_REPO, ToolchainAxes.from_values(archs=None))
    var_a = compute_subentry_key(
        _REPO, VariantAxes.from_values(archs=["x86_64"])
    )
    var_b = compute_subentry_key(_REPO, VariantAxes.from_values(archs=None))
    assert tc_a != tc_b
    assert var_a != var_b


def test_repo_state_change_misses_both_subentries():
    other_repo = dataclasses.replace(_REPO, git_rev="b" * 40)
    for axes in (ToolchainAxes.from_values(), VariantAxes.from_values()):
        assert compute_subentry_key(_REPO, axes) != compute_subentry_key(
            other_repo, axes
        )


def test_variant_axes_sample_and_seed_split_the_key():
    base = compute_subentry_key(
        _REPO, VariantAxes.from_values(variant_sample=2, variant_seed="42")
    )
    sample = compute_subentry_key(
        _REPO, VariantAxes.from_values(variant_sample=0, variant_seed="42")
    )
    seed = compute_subentry_key(
        _REPO, VariantAxes.from_values(variant_sample=2, variant_seed="43")
    )
    sys = compute_subentry_key(
        _REPO,
        VariantAxes.from_values(
            variant_sample=2, variant_seed="42", sys_name="aarch64-linux"
        ),
    )
    assert len({base, sample, seed, sys}) == 4


def test_toolchain_axes_sys_name_splits_the_key():
    assert compute_subentry_key(
        _REPO, ToolchainAxes.from_values(sys_name="x86_64-linux")
    ) != compute_subentry_key(
        _REPO, ToolchainAxes.from_values(sys_name="aarch64-linux")
    )


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
    # The repo-state collection carries no axes; sub-entry keys are
    # derived later via compute_subentry_key.
    assert result.invocation == b""

    # flake.lock was read from the right path
    assert read_calls == [tmp_path / "flake.lock"]
    # both git commands ran in repo_root
    assert all(cwd == tmp_path for _, cwd in calls)
    cmds = [cmd for cmd, _ in calls]
    assert ("git", "rev-parse", "HEAD") in cmds
    assert ("git", "diff") in cmds


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


def _cache(tmp_path: pathlib.Path) -> IncrementalCache:
    return IncrementalCache(tmp_path / "cache")


def test_lookup_missing_returns_none(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    assert cache.lookup_toolchains("deadbeef") is None
    assert cache.lookup_variants("deadbeef") is None


def test_toolchains_round_trip_exact(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)

    restored = cache.lookup_toolchains("k1")
    assert restored is not None
    pairs, drvs, aggregate = restored
    # Exact round-trip: tuple-of-tuples, dict keyed by (arch, compiler)
    # tuples in original order, aggregate string verbatim.
    assert pairs == _TC_PAIRS
    assert drvs == _TC_DRVS
    assert list(drvs.items()) == list(_TC_DRVS.items())
    assert aggregate == agg


def test_toolchains_round_trip_empty_shapes(tmp_path: pathlib.Path):
    """The legit-empty return of ``enumerate_toolchains_only`` (no
    leaves resolved -> ``((), {}, "")``) round-trips as-is."""
    cache = _cache(tmp_path)
    cache.store_toolchains("k1", (), {}, "")
    assert cache.lookup_toolchains("k1") == ((), {}, "")


def test_lookup_toolchains_gcd_aggregate_drv_is_miss(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """A shape-valid entry whose aggregate drv was GC'd from the local
    store since the entry was written is a loudly-logged miss, and the
    entry dir is discarded so the re-store after the forced
    re-enumeration self-heals it (the store path's keep-the-existing-
    entry race guard would otherwise preserve the stale entry forever)."""
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    assert cache.lookup_toolchains("k1") == (_TC_PAIRS, _TC_DRVS, agg)

    # GC the aggregate drv out from under the entry.
    pathlib.Path(agg).unlink()
    with caplog.at_level("WARNING"):
        assert cache.lookup_toolchains("k1") is None
    assert any(
        "gone from the local store" in r.getMessage()
        for r in caplog.records
    )
    # Entry discarded so a re-store self-heals.
    assert not (tmp_path / "cache" / "toolchains" / "k1").exists()
    agg2 = _tc_agg(tmp_path)  # re-enumeration re-instantiates the drv
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg2)
    assert cache.lookup_toolchains("k1") == (_TC_PAIRS, _TC_DRVS, agg2)


def test_variants_round_trip_exact(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    cache.store_variants("k1", _VARIANTS)
    restored = cache.lookup_variants("k1")
    assert restored == _VARIANTS
    # Empty dict (no plannable binaries) round-trips too.
    cache.store_variants("k2", {})
    assert cache.lookup_variants("k2") == {}


def test_namespaces_are_independent(tmp_path: pathlib.Path):
    """Storing under one namespace never produces a hit in the other,
    even for the same key string."""
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    assert cache.lookup_variants("k1") is None
    cache.store_variants("k2", _VARIANTS)
    assert cache.lookup_toolchains("k2") is None


def test_store_leaves_no_tmp_dirs(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    cache.store_variants("k1", _VARIANTS)
    leftovers = [
        p
        for ns in ("toolchains", "variants")
        for p in (tmp_path / "cache" / ns).iterdir()
        if ".tmp" in p.name
    ]
    assert leftovers == []


def test_store_does_not_clobber_existing(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    entry = tmp_path / "cache" / "toolchains" / "k1" / "entry.json"
    sentinel = entry.read_text()

    # A second store for the same key keeps the first entry (memoized
    # values for one key are identical by construction).
    cache.store_toolchains("k1", (), {}, "")
    assert entry.read_text() == sentinel
    assert cache.lookup_toolchains("k1") == (_TC_PAIRS, _TC_DRVS, agg)


def test_lookup_rejects_legacy_entry_dir(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    """An entry dir in the old ``manifests.tar`` format (no entry.json)
    is a miss — legacy entries are never replayed — and the dir is
    discarded so the re-store after the forced miss can replace it."""
    cache = _cache(tmp_path)
    legacy = tmp_path / "cache" / "toolchains" / "k1"
    legacy.mkdir(parents=True)
    (legacy / "partition.json").write_text("{}")
    (legacy / "manifests.tar").write_bytes(b"")
    (legacy / "meta.json").write_text("{}")
    with caplog.at_level("WARNING"):
        assert cache.lookup_toolchains("k1") is None
    assert not legacy.exists()
    # Self-heal: a subsequent store repopulates the entry.
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    assert cache.lookup_toolchains("k1") == (_TC_PAIRS, _TC_DRVS, agg)


def test_lookup_rejects_corrupt_json(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    cache = _cache(tmp_path)
    entry_dir = tmp_path / "cache" / "variants" / "k1"
    entry_dir.mkdir(parents=True)
    (entry_dir / "entry.json").write_text("{not json")
    with caplog.at_level("WARNING"):
        assert cache.lookup_variants("k1") is None
    assert any("treating as miss" in r.getMessage() for r in caplog.records)
    # The corrupt entry was discarded so a store can self-heal it.
    assert not entry_dir.exists()
    cache.store_variants("k1", _VARIANTS)
    assert cache.lookup_variants("k1") == _VARIANTS


def test_lookup_rejects_wrong_version(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    cache = _cache(tmp_path)
    entry_dir = tmp_path / "cache" / "toolchains" / "k1"
    for bad_version in (0, 2, "1", None):
        entry_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "version": bad_version,
            "tc_pairs": [],
            "tc_drvs": [],
            "tc_aggregate_drv": "",
        }
        (entry_dir / "entry.json").write_text(json.dumps(body))
        with caplog.at_level("WARNING"):
            assert cache.lookup_toolchains("k1") is None
        # Discarded for self-heal.
        assert not entry_dir.exists()


def test_lookup_rejects_missing_or_malformed_keys(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    cache = _cache(tmp_path)
    entry_dir = tmp_path / "cache" / "toolchains" / "k1"

    bad_payloads = [
        # required keys absent entirely
        {"version": 1},
        # tc_pairs entries malformed (wrong arity / non-strings)
        {
            "version": 1,
            "tc_pairs": [["x86_64"]],
            "tc_drvs": [],
            "tc_aggregate_drv": "",
        },
        {
            "version": 1,
            "tc_pairs": [["x86_64", 7]],
            "tc_drvs": [],
            "tc_aggregate_drv": "",
        },
        # tc_drvs entries malformed
        {
            "version": 1,
            "tc_pairs": [],
            "tc_drvs": [["x86_64", "gcc15"]],
            "tc_aggregate_drv": "",
        },
        # aggregate not a string
        {
            "version": 1,
            "tc_pairs": [],
            "tc_drvs": [],
            "tc_aggregate_drv": None,
        },
    ]
    for payload in bad_payloads:
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "entry.json").write_text(json.dumps(payload))
        with caplog.at_level("WARNING"):
            assert cache.lookup_toolchains("k1") is None, payload


def test_lookup_variants_rejects_malformed_metadata(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
):
    cache = _cache(tmp_path)
    entry_dir = tmp_path / "cache" / "variants" / "k1"

    bad_payloads = [
        {"version": 1},  # per_binary_meta_raw absent
        {"version": 1, "per_binary_meta_raw": []},  # not a dict
        # metadata value not a dict
        {"version": 1, "per_binary_meta_raw": {"hello": "x"}},
        # required metadata keys missing
        {"version": 1, "per_binary_meta_raw": {"hello": {"archs": ["x"]}}},
        # archs not a list of strings
        {
            "version": 1,
            "per_binary_meta_raw": {
                "hello": {
                    "archs": "x86_64",
                    "sample_size": 0,
                    "sample_seed": "42",
                    "tier": 1,
                }
            },
        },
    ]
    for payload in bad_payloads:
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "entry.json").write_text(json.dumps(payload))
        with caplog.at_level("WARNING"):
            assert cache.lookup_variants("k1") is None, payload


def test_invalidate_removes_key_from_all_namespaces(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    cache.store_variants("k1", _VARIANTS)
    # Plus a legacy top-level entry dir under the same key.
    legacy = tmp_path / "cache" / "k1"
    legacy.mkdir()
    (legacy / "manifests.tar").write_bytes(b"")

    cache.invalidate("k1")
    assert cache.lookup_toolchains("k1") is None
    assert cache.lookup_variants("k1") is None
    assert not legacy.exists()


def test_invalidate_idempotent(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    # Calling on a missing entry must not raise.
    cache.invalidate("never-existed")
    cache.invalidate("never-existed")  # twice for good measure


def test_invalidate_namespace_name_does_not_drop_namespace(
    tmp_path: pathlib.Path,
):
    """``invalidate("toolchains")`` must not rmtree the whole toolchains
    namespace dir via the legacy-cleanup path."""
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    cache.invalidate("toolchains")
    assert cache.lookup_toolchains("k1") is not None


def test_clear_counts_and_removes(tmp_path: pathlib.Path):
    cache = _cache(tmp_path)
    agg = _tc_agg(tmp_path)
    cache.store_toolchains("k1", _TC_PAIRS, _TC_DRVS, agg)
    cache.store_variants("k1", _VARIANTS)
    cache.store_variants("k2", {})

    cache_root = tmp_path / "cache"
    # Legacy top-level entry dir: counted (so the operator sees it
    # cleared) but never consulted by lookups.
    legacy = cache_root / ("f" * 64)
    legacy.mkdir()
    (legacy / "manifests.tar").write_bytes(b"")
    # Strays: not counted.
    (cache_root / "stray.txt").write_text("not an entry")
    (cache_root / "toolchains" / "abc.tmp.999").mkdir()

    n = cache.clear()
    assert n == 4  # 1 toolchains + 2 variants + 1 legacy
    assert not cache_root.exists()


def test_clear_on_empty_cache(tmp_path: pathlib.Path):
    cache = IncrementalCache(tmp_path / "never-created")
    assert cache.clear() == 0

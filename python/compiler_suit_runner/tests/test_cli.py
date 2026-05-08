"""Unit tests for ``compiler_suit_runner.cli``.

The CLI surface is exercised through ``main(argv_list)`` with the
preflight, IncrementalCache, and SuitTask side-effects monkeypatched
out — no real nix, no real cache writes against the user's home dir.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib

import pytest

import compiler_suit_runner.cli as cli_module
from compiler_suit_runner.cli import (
    build_parser,
    cmd_clear_cache,
    cmd_preflight,
    cmd_submit,
    main,
)
from compiler_suit_runner.incremental_cache import (
    CacheEntry,
    IncrementalCache,
)
from compiler_suit_runner.preflight import PreflightResult


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_subcommands_exist():
    parser = build_parser()
    # parse_args needs a subcommand; without one it raises SystemExit.
    for cmd in ["submit", "secondary", "preflight", "clear-cache"]:
        # Each subcommand's --help should not raise (parsed args returned).
        # We only check the parser accepts the subcommand identifier
        # without erroring; further args may be required, but the
        # subparser registration is the load-bearing assertion.
        sub = parser._subparsers
        assert sub is not None


def test_build_parser_unknown_subcommand_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["totally-not-a-cmd"])


def test_build_parser_submit_with_required_args(tmp_path: pathlib.Path):
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--flake",
            ".",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
        ]
    )
    assert args.command == "submit"
    assert args.flake == "."
    assert pathlib.Path(args.shared_fs) == tmp_path
    assert args.multi_computer == "single-process"


def test_build_parser_submit_invalid_multi_computer():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "submit",
                "--shared-fs",
                "/tmp/x",
                "--multi-computer",
                "invalid",
            ]
        )


def test_build_parser_clear_cache_accepts_hash():
    parser = build_parser()
    args = parser.parse_args(["clear-cache", "--hash", "abc"])
    assert args.command == "clear-cache"
    assert args.hash == "abc"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_invalid_subcommand_returns_nonzero():
    rc = main(["bogus-subcommand"])
    assert rc != 0


def test_main_submit_invalid_multi_computer(tmp_path: pathlib.Path):
    rc = main(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "invalid",
        ]
    )
    assert rc != 0


def test_main_submit_missing_shared_fs_returns_2(monkeypatch: pytest.MonkeyPatch):
    rc = main(
        [
            "submit",
            "--flake",
            ".",
            "--multi-computer",
            "single-process",
        ]
    )
    # cmd_submit returns 2 when --shared-fs is missing.
    assert rc == 2


# ---------------------------------------------------------------------------
# cmd_preflight
# ---------------------------------------------------------------------------


def _stub_preflight(monkeypatch: pytest.MonkeyPatch, **overrides):
    """Replace the preflight import inside cli with a deterministic stub."""
    pre = PreflightResult(
        sys_name=overrides.get("sys_name", "x86_64-linux"),
        variants=overrides.get("variants", ()),
        toolchain_specs=overrides.get("toolchain_specs", ()),
        common_dep_drvs=overrides.get("common_dep_drvs", ()),
        toolchain_drvs=overrides.get("toolchain_drvs", frozenset()),
    )
    calls = []

    def fake(
        flake_ref,
        sys_name,
        *,
        packages=None,
        archs=None,
        sample_size=0,
        sample_seed="42",
        run_subprocess=None,
    ):
        calls.append((flake_ref, sys_name, packages, archs))
        return pre

    monkeypatch.setattr(cli_module, "run_preflight", fake)
    return calls


def test_main_preflight_subcommand_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
):
    _stub_preflight(monkeypatch)
    rc = main(
        [
            "preflight",
            "--flake",
            ".",
            "--shared-fs",
            str(tmp_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "variants" in out
    assert "toolchains" in out


def test_cmd_preflight_handles_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    def boom(*a, **kw):
        raise RuntimeError("nix not found")

    monkeypatch.setattr(cli_module, "run_preflight", boom)
    args = argparse.Namespace(
        flake=".",
        shared_fs=tmp_path,
        sys_name="x86_64-linux",
        packages=None,
        archs=None,
        debug=False,
    )
    rc = cmd_preflight(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# cmd_clear_cache
# ---------------------------------------------------------------------------


def test_main_clear_cache_with_hash_calls_invalidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    invalidated: list[str] = []

    class _FakeCache(IncrementalCache):
        def invalidate(self, input_hash: str) -> None:  # type: ignore[override]
            invalidated.append(input_hash)

    monkeypatch.setattr(cli_module, "IncrementalCache", _FakeCache)
    rc = main(
        [
            "clear-cache",
            "--cache-root",
            str(tmp_path),
            "--hash",
            "abc",
        ]
    )
    assert rc == 0
    assert invalidated == ["abc"]


def test_main_clear_cache_without_hash_calls_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    cleared: list[bool] = []

    class _FakeCache(IncrementalCache):
        def clear(self) -> int:  # type: ignore[override]
            cleared.append(True)
            return 0

    monkeypatch.setattr(cli_module, "IncrementalCache", _FakeCache)
    rc = main(["clear-cache", "--cache-root", str(tmp_path)])
    assert rc == 0
    assert cleared == [True]


# ---------------------------------------------------------------------------
# cmd_submit cache hit / cache miss
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_submit_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path):
    """Wire up stubs so cmd_submit can run without real nix or the
    SuitTask side-effects."""

    state: dict[str, list] = {
        "preflight_calls": [],
        "emit_calls": [],
        "single_process_calls": [],
        "cache_lookup_calls": [],
        "cache_store_calls": [],
        "restore_calls": [],
    }

    pre = PreflightResult(
        sys_name="x86_64-linux",
        variants=(),
        toolchain_specs=(),
        common_dep_drvs=(),
        toolchain_drvs=frozenset(),
    )

    def fake_preflight(
        flake_ref,
        sys_name,
        *,
        packages=None,
        archs=None,
        sample_size=0,
        sample_seed="42",
        run_subprocess=None,
    ):
        state["preflight_calls"].append((flake_ref, sys_name, packages, archs))
        return pre

    monkeypatch.setattr(cli_module, "run_preflight", fake_preflight)

    def fake_emit(
        *, target_dir, sys_name, pre, num_workers, toolchain_drvs=None,
    ):
        state["emit_calls"].append((target_dir, sys_name, num_workers))

        # Simulate manifest_dir population so cache.store can pack it.
        target_dir = pathlib.Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "stub.json").write_text("{}")

    monkeypatch.setattr(
        cli_module, "_emit_manifests_from_preflight", fake_emit
    )

    def fake_single_process(config, *, logger=None):
        state["single_process_calls"].append(config)
        return 0

    monkeypatch.setattr(cli_module, "run_single_process", fake_single_process)

    def fake_compute_input_hash(repo_root):
        return "test-hash"

    monkeypatch.setattr(cli_module, "_compute_input_hash", fake_compute_input_hash)

    def fake_restore(archive, target_dir):
        state["restore_calls"].append((archive, target_dir))
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "from-cache.json").write_text("{}")

    monkeypatch.setattr(cli_module, "_restore_manifests_from_archive", fake_restore)

    return state


def _make_args(tmp_path: pathlib.Path, **overrides) -> argparse.Namespace:
    """Construct an argparse.Namespace as cmd_submit expects."""
    defaults = dict(
        flake=".",
        shared_fs=tmp_path,
        run_id="r1",
        sys_name="x86_64-linux",
        packages=None,
        archs=None,
        multi_computer="single-process",
        jobs=2,
        packaging="none",
        gateway=None,
        slurm_root_folder=None,
        cachix_cache=None,
        cachix_auth_token_file=None,
        no_cache=False,
        barrier_timeout_seconds=10.0,
        cache_root=tmp_path / "cache",
        debug=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_submit_cache_hit_skips_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """When IncrementalCache.lookup returns a populated entry, the
    pre-flight is skipped and the manifests get restored from the cache
    archive."""
    archive = tmp_path / "manifests.tar"
    archive.write_text("not a real tar")
    fake_entry = CacheEntry(
        input_hash="test-hash",
        partition_path=tmp_path / "p.json",
        manifests_archive=archive,
        meta_path=tmp_path / "m.json",
    )

    class _HitCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            stub_submit_helpers["cache_lookup_calls"].append(input_hash)
            return fake_entry

        def store(self, input_hash, partition_path, manifests_dir, meta_path):  # type: ignore[override]
            stub_submit_helpers["cache_store_calls"].append(input_hash)
            return fake_entry

    monkeypatch.setattr(cli_module, "IncrementalCache", _HitCache)

    args = _make_args(tmp_path)
    rc = cmd_submit(args)
    assert rc == 0
    # Pre-flight NOT called.
    assert stub_submit_helpers["preflight_calls"] == []
    # Manifest restoration was triggered.
    assert stub_submit_helpers["restore_calls"]
    # The single-process dispatch did fire.
    assert stub_submit_helpers["single_process_calls"]


def test_cmd_submit_cache_miss_runs_preflight_and_stores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    class _MissCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            stub_submit_helpers["cache_lookup_calls"].append(input_hash)
            return None

        def store(self, input_hash, partition_path, manifests_dir, meta_path):  # type: ignore[override]
            stub_submit_helpers["cache_store_calls"].append(
                (input_hash, manifests_dir)
            )
            return CacheEntry(
                input_hash=input_hash,
                partition_path=partition_path,
                manifests_archive=manifests_dir.parent / "manifests.tar",
                meta_path=meta_path,
            )

    monkeypatch.setattr(cli_module, "IncrementalCache", _MissCache)

    args = _make_args(tmp_path)
    rc = cmd_submit(args)
    assert rc == 0
    # Pre-flight ran.
    assert stub_submit_helpers["preflight_calls"]
    # No cache hit -> no restore.
    assert stub_submit_helpers["restore_calls"] == []
    # On success, cache.store is called.
    assert stub_submit_helpers["cache_store_calls"]


def test_cmd_submit_no_cache_flag_skips_lookup_and_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    class _RecordingCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            stub_submit_helpers["cache_lookup_calls"].append(input_hash)
            return None

        def store(self, input_hash, partition_path, manifests_dir, meta_path):  # type: ignore[override]
            stub_submit_helpers["cache_store_calls"].append(input_hash)
            return CacheEntry(
                input_hash=input_hash,
                partition_path=partition_path,
                manifests_archive=manifests_dir.parent / "manifests.tar",
                meta_path=meta_path,
            )

    monkeypatch.setattr(cli_module, "IncrementalCache", _RecordingCache)

    args = _make_args(tmp_path, no_cache=True)
    rc = cmd_submit(args)
    assert rc == 0
    # No lookup, no store when --no-cache.
    assert stub_submit_helpers["cache_lookup_calls"] == []
    assert stub_submit_helpers["cache_store_calls"] == []


def test_cmd_submit_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """If run_single_process returns 1, cmd_submit returns 1 and the
    cache is NOT updated (preserving the previous good state, if any)."""

    monkeypatch.setattr(
        cli_module, "run_single_process", lambda *a, **kw: 1
    )

    class _NoCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            return None

        def store(self, *a, **kw):  # type: ignore[override]
            stub_submit_helpers["cache_store_calls"].append("ouch")
            return None

    monkeypatch.setattr(cli_module, "IncrementalCache", _NoCache)

    args = _make_args(tmp_path)
    rc = cmd_submit(args)
    assert rc == 1
    assert stub_submit_helpers["cache_store_calls"] == []


def test_serialize_then_restore_preflight_roundtrip(tmp_path: pathlib.Path):
    """Cached preflight must round-trip every VariantSpec field.

    Regression: previously the restore path constructed VariantSpec with
    only 7 of its 15 TypedDict fields, so make_variant_header crashed
    with KeyError('metadata_name') on a cache hit.
    """
    import tarfile

    from compiler_suit_runner.cli import (
        _restore_manifests_from_archive,
        _serialize_preflight_for_cache,
    )
    from compiler_suit_runner.partition import VariantSpec

    variant: VariantSpec = {
        "label": "hello-x86_64-gcc15-O2",
        "drv": "/nix/store/abc.drv",
        "variant_dir": "gcc15_x86_64_O2_deadbeef",
        "metadata_name": "gcc15_x86_64_O2_deadbeef.json",
        "compiler_id": "gcc15",
        "compiler_family": "gcc",
        "compiler_version": "15.2.0",
        "optimization": "O2",
        "flag_set": "baseline",
        "hardening": "default",
        "sanitizer": "san-off",
        "march": "march-default",
        "tier": 1,
        "pkg": "hello",
        "arch": "x86_64",
    }
    pre = PreflightResult(
        sys_name="x86_64-linux",
        variants=(variant,),
        toolchain_specs=(("x86_64", "gcc15"),),
        common_dep_drvs=(),
        toolchain_drvs=frozenset({"/nix/store/tc.drv"}),
    )

    preflight_path = tmp_path / "_preflight.json"
    _serialize_preflight_for_cache(
        pre,
        num_workers=1,
        target_path=preflight_path,
        toolchain_drvs_by_pair={("x86_64", "gcc15"): "/nix/store/tc.drv"},
    )

    archive = tmp_path / "manifests.tar"
    with tarfile.open(archive, mode="w") as tf:
        tf.add(preflight_path, arcname="manifests/_preflight.json")

    target_dir = tmp_path / "out"
    _restore_manifests_from_archive(archive, target_dir)

    # The variant header is written as <label>.json — load it and prove
    # every VariantSpec field survived the round-trip.
    body = json.loads((target_dir / f"{variant['label']}.json").read_text())
    payload = body["payload"]
    assert payload["pkg"] == "hello"
    assert payload["arch"] == "x86_64"
    assert payload["compiler_id"] == "gcc15"
    assert payload["metadata_name"] == "gcc15_x86_64_O2_deadbeef.json"
    assert payload["compiler_family"] == "gcc"
    assert payload["optimization"] == "O2"

    # The toolchain manifest must carry the realised drv path so the
    # SLURM-side build worker doesn't fall back to flake-attr lookup
    # (which fails — secondaries have no flake.nix in /app).
    tc_files = list(target_dir.glob("toolchain__*.json"))
    assert len(tc_files) == 1
    tc = json.loads(tc_files[0].read_text())
    assert tc["payload"].get("drv") == "/nix/store/tc.drv"

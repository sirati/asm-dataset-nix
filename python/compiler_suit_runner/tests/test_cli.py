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


def test_build_parser_submit_build_compilers_defaults_false(
    tmp_path: pathlib.Path,
):
    """The ``--build-compilers`` flag (post-rename successor to
    ``--allow-toolchain-build``) must default to False — that's the
    whole point of the no-build-on-secondaries default. Anyone flipping
    the default trips this test."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process"],
    )
    assert getattr(args, "build_compilers", None) is False


def test_build_parser_submit_build_compilers_can_be_set(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process",
         "--build-compilers"],
    )
    assert args.build_compilers is True


def test_build_parser_submit_build_compiler_workers_defaults_to_one(
    tmp_path: pathlib.Path,
):
    """``--build-compiler-workers`` defaults to 1 — single in-flight
    toolchain build per secondary."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process"],
    )
    assert args.build_compiler_workers == 1


def test_build_parser_submit_build_compiler_workers_parses_int(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process",
         "--build-compiler-workers", "4"],
    )
    assert args.build_compiler_workers == 4


def test_build_parser_submit_debug_testbuild_defaults_none(
    tmp_path: pathlib.Path,
):
    """``--debug-testbuild`` defaults to None — no phase-1.5
    toolchain_validate emitted."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process"],
    )
    assert args.debug_testbuild is None


def test_build_parser_submit_debug_testbuild_accepts_binary(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process",
         "--debug-testbuild", "hello"],
    )
    assert args.debug_testbuild == "hello"


def test_build_parser_submit_distributed_eval_flag_removed(
    tmp_path: pathlib.Path,
):
    """``--distributed-eval`` was hard-deleted (distributed eval is the
    only mode now). Argparse must reject any leftover invocation rather
    than silently accept it as a no-op."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["submit", "--shared-fs", str(tmp_path),
             "--multi-computer", "single-process",
             "--distributed-eval"],
        )


def test_build_parser_submit_allow_toolchain_build_flag_removed(
    tmp_path: pathlib.Path,
):
    """``--allow-toolchain-build`` was hard-cut over to
    ``--build-compilers``; argparse must reject the old name so stale
    operator scripts surface the breakage loudly."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["submit", "--shared-fs", str(tmp_path),
             "--multi-computer", "single-process",
             "--allow-toolchain-build"],
        )


def test_build_parser_submit_system_defaults_to_x86_64_linux(
    tmp_path: pathlib.Path,
):
    """``--system`` defaults to ``x86_64-linux`` so submitters on the
    common case don't have to spell it out."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process"],
    )
    assert args.sys_name == "x86_64-linux"


def test_build_parser_submit_system_overrides_default(
    tmp_path: pathlib.Path,
):
    """``--system aarch64-linux`` must land on ``args.sys_name`` so the
    submit-side manifest emission and worker dispatch see the override."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process",
         "--system", "aarch64-linux"],
    )
    assert args.sys_name == "aarch64-linux"


def test_build_parser_submit_sys_alias_still_accepted(
    tmp_path: pathlib.Path,
):
    """``--sys`` remains a back-compat alias for ``--system`` so existing
    operator scripts and forwarded-argv stripping logic keep working."""
    parser = build_parser()
    args = parser.parse_args(
        ["submit", "--shared-fs", str(tmp_path),
         "--multi-computer", "single-process",
         "--sys", "aarch64-linux"],
    )
    assert args.sys_name == "aarch64-linux"


def test_cmd_submit_system_propagates_to_manifest_emit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """``cmd_submit`` must thread the parsed ``--system`` value into the
    matrix-eval manifest emission (so the manifest header's ``sys_name``
    field matches the submitter's CLI choice)."""

    class _MissCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            return None

        def store(self, input_hash, partition_path, manifests_dir, meta_path):  # type: ignore[override]
            return CacheEntry(
                input_hash=input_hash,
                partition_path=partition_path,
                manifests_archive=manifests_dir.parent / "manifests.tar",
                meta_path=meta_path,
            )

    monkeypatch.setattr(cli_module, "IncrementalCache", _MissCache)
    args = _make_args(tmp_path, sys_name="aarch64-linux")
    rc = cmd_submit(args)
    assert rc == 0
    # preflight (enumerate_toolchains_only) saw the override.
    assert stub_submit_helpers["preflight_calls"]
    assert stub_submit_helpers["preflight_calls"][0][1] == "aarch64-linux"
    # emit_all_manifests saw the override.
    assert stub_submit_helpers["emit_calls"]
    assert stub_submit_helpers["emit_calls"][0][1] == "aarch64-linux"


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
    SuitTask side-effects.

    cmd_submit drives the submit-time pre-flight:
    ``enumerate_toolchains_only`` + ``enumerate_variants`` (per-binary
    metadata) + ``emit_all_manifests``. The legacy composite preflight
    is no longer wired into cmd_submit; ``preflight_calls`` here counts
    the toolchain enumeration as a proxy for "pre-flight ran" so the
    existing test contracts remain meaningful.
    """

    state: dict = {
        "preflight_calls": [],
        "emit_calls": [],
        "emit_per_binary_metadata": [],
        "single_process_calls": [],
        "cache_lookup_calls": [],
        "cache_store_calls": [],
        "restore_calls": [],
        # Mutable so individual tests can override what the stubbed
        # enumerate_* helpers return without rewriting the whole
        # fixture.
        "toolchain_return": ((), {}, ""),
        "variants_return": {},
    }

    def fake_enumerate_toolchains_only(
        flake_ref, sys_name, *, archs=None, run_subprocess=None,
    ):
        state["preflight_calls"].append((flake_ref, sys_name, None, archs))
        # Third element is the aggregate drv path; empty string by
        # default mirrors the "no leaves resolved → no aggregate"
        # return shape.
        return state["toolchain_return"]

    monkeypatch.setattr(
        cli_module, "enumerate_toolchains_only", fake_enumerate_toolchains_only,
    )

    def fake_enumerate_variants(
        flake_ref, sys_name, *, packages=None, archs=None,
        sample_size=0, sample_seed="42", run_subprocess=None,
    ):
        del flake_ref, sys_name, packages, archs, sample_size, sample_seed
        return state["variants_return"]

    monkeypatch.setattr(
        cli_module, "enumerate_variants", fake_enumerate_variants,
    )

    def fake_emit(
        *, target_dir, sys_name, variants, toolchain_specs, common_deps,
        num_workers, toolchain_drvs=None, allow_toolchain_build=False,
        per_binary_metadata=None, drv_outpaths=None, stages=None,
        **_kw,
    ):
        del (
            variants, toolchain_specs, common_deps, toolchain_drvs,
            allow_toolchain_build, drv_outpaths, stages,
        )
        state["emit_calls"].append((target_dir, sys_name, num_workers))
        state["emit_per_binary_metadata"].append(per_binary_metadata)

        # Simulate manifest_dir population so cache.store can pack it.
        target_dir = pathlib.Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "stub.json").write_text("{}")

    monkeypatch.setattr(cli_module, "emit_all_manifests", fake_emit)

    # Stub the toolchain availability + outpath helpers so tests can
    # drive ``cmd_submit`` with a non-empty ``tc_drvs`` without needing
    # /nix/store entries to actually exist.
    monkeypatch.setattr(
        cli_module, "check_toolchains_locally", lambda drvs: frozenset(),
    )
    monkeypatch.setattr(
        cli_module, "build_toolchains_locally", lambda drvs: None,
    )
    monkeypatch.setattr(
        cli_module, "eval_drv_outpaths",
        lambda drvs: {d: f"{d}.out" for d in drvs},
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
        return {}

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


def test_cmd_submit_fails_fast_on_empty_toolchain_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
    caplog: pytest.LogCaptureFixture,
):
    """When preflight returns an empty toolchain aggregate but
    ``enumerate_variants`` reports binaries queued for matrix_eval,
    cmd_submit must abort BEFORE manifest emission — otherwise
    ``emit_matrix_eval_manifests`` raises ``ValueError`` deep inside
    the emit step and the user-visible failure hides the real root
    cause (toolchain enumeration produced no aggregate)."""

    class _MissCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            return None

        def store(self, *a, **kw):  # type: ignore[override]
            stub_submit_helpers["cache_store_calls"].append("stored")
            return None

    monkeypatch.setattr(cli_module, "IncrementalCache", _MissCache)

    # Preflight returns an empty aggregate (third element of tuple).
    stub_submit_helpers["toolchain_return"] = ((), {}, "")
    # But variants enumeration reports binaries queued for matrix_eval.
    stub_submit_helpers["variants_return"] = {
        "hello": {
            "archs": ["x86_64"],
            "sample_size": 2,
            "sample_seed": 42,
            "tier": 1,
        },
    }

    args = _make_args(tmp_path)
    with caplog.at_level("ERROR"):
        rc = cmd_submit(args)

    assert rc == 1
    # The guard short-circuits BEFORE emit_all_manifests runs.
    assert stub_submit_helpers["emit_calls"] == []
    # And BEFORE cache.store fires.
    assert stub_submit_helpers["cache_store_calls"] == []
    # The error log explicitly names the empty aggregate as the cause.
    assert any(
        "toolchain aggregate is empty" in rec.getMessage()
        for rec in caplog.records
    )


def test_cmd_submit_populates_per_binary_toolchain_aggregate_drv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """When preflight returns a non-empty toolchain aggregate AND
    enumerate_variants returns binary metadata, cmd_submit's
    construction loop must thread the aggregate drv path into every
    per-binary metadata entry — and the fail-fast guard must NOT
    fire."""

    class _MissCache(IncrementalCache):
        def lookup(self, input_hash: str):  # type: ignore[override]
            return None

        def store(self, input_hash, partition_path, manifests_dir, meta_path):  # type: ignore[override]
            return CacheEntry(
                input_hash=input_hash,
                partition_path=partition_path,
                manifests_archive=manifests_dir.parent / "manifests.tar",
                meta_path=meta_path,
            )

    monkeypatch.setattr(cli_module, "IncrementalCache", _MissCache)

    aggregate_drv = "/nix/store/fake-toolchains.drv"
    stub_submit_helpers["toolchain_return"] = (
        (("x86_64", "gcc15"),),
        {("x86_64", "gcc15"): "/nix/store/fake-gcc15.drv"},
        aggregate_drv,
    )
    stub_submit_helpers["variants_return"] = {
        "hello": {
            "archs": ["x86_64"],
            "sample_size": 2,
            "sample_seed": 42,
            "tier": 1,
        },
    }

    args = _make_args(tmp_path)
    rc = cmd_submit(args)
    assert rc == 0

    # emit_all_manifests received per_binary_metadata threaded through
    # from the construction loop.
    assert stub_submit_helpers["emit_per_binary_metadata"]
    pbm = stub_submit_helpers["emit_per_binary_metadata"][-1]
    assert pbm is not None
    assert "hello" in pbm
    # New flatten shape: archs, variant_sample, variant_seed, tier,
    # toolchain_aggregate_drv. Per-arch suffix selection now lives in
    # eval_worker, not the submit-time flatten loop.
    assert "suffixes" not in pbm["hello"]
    assert set(pbm["hello"].keys()) == {
        "archs",
        "variant_sample",
        "variant_seed",
        "tier",
        "toolchain_aggregate_drv",
    }
    assert pbm["hello"]["toolchain_aggregate_drv"] == aggregate_drv
    assert pbm["hello"]["archs"] == ["x86_64"]
    assert pbm["hello"]["variant_sample"] == 2
    assert pbm["hello"]["variant_seed"] == 42
    assert pbm["hello"]["tier"] == 1


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
        toolchain_aggregate_drv="/nix/store/aaaa-toolchains.drv",
    )

    preflight_path = tmp_path / "_preflight.json"
    _serialize_preflight_for_cache(
        pre,
        num_workers=1,
        target_path=preflight_path,
        toolchain_drvs_by_pair={("x86_64", "gcc15"): "/nix/store/tc.drv"},
    )

    # The aggregate drv path round-trips through the cache JSON
    # verbatim: same handle written → same handle readable. Read it
    # off-disk before the archive packs it, so we cover the
    # write-side contract (the read side is exercised by
    # _restore_manifests_from_archive below not crashing on the
    # new field).
    cached_payload = json.loads(preflight_path.read_text())
    assert cached_payload["toolchain_aggregate_drv"] == (
        "/nix/store/aaaa-toolchains.drv"
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
    # (which fails — secondaries have no flake.nix in /app). The
    # default (--build-compilers off) emits
    # ``toolchain_validate__*.json``; opting in to in-cluster builds
    # emits ``build_compilers__*.json``. Both carry the drv path,
    # which is what this regression test guards.
    tc_files = list(target_dir.glob("build_compilers__*.json")) + list(
        target_dir.glob("toolchain_validate__*.json")
    )
    assert len(tc_files) == 1
    tc = json.loads(tc_files[0].read_text())
    assert tc["payload"].get("drv") == "/nix/store/tc.drv"


# ---------------------------------------------------------------------------
# --unfulfillable-reinject-max-per-task
# ---------------------------------------------------------------------------


def test_unfulfillable_reinject_flag_defaults_to_none(
    tmp_path: pathlib.Path,
):
    """Without the flag the parsed namespace should carry ``None`` —
    matching the framework default of an unbounded per-task reinject
    budget."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
        ]
    )
    assert args.unfulfillable_reinject_max_per_task is None


def test_unfulfillable_reinject_flag_parses_positive_int(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
            "--unfulfillable-reinject-max-per-task=10",
        ]
    )
    assert args.unfulfillable_reinject_max_per_task == 10


def test_unfulfillable_reinject_flag_accepts_zero(
    tmp_path: pathlib.Path,
):
    """Zero is a valid cap — means 'don't auto-reinject at all'. The
    framework's matcher honours zero distinct from None."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
            "--unfulfillable-reinject-max-per-task=0",
        ]
    )
    assert args.unfulfillable_reinject_max_per_task == 0


def test_unfulfillable_reinject_flag_rejects_negative(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "submit",
                "--shared-fs",
                str(tmp_path),
                "--multi-computer",
                "single-process",
                "--unfulfillable-reinject-max-per-task=-1",
            ]
        )


def test_unfulfillable_reinject_flag_rejects_non_int(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "submit",
                "--shared-fs",
                str(tmp_path),
                "--multi-computer",
                "single-process",
                "--unfulfillable-reinject-max-per-task=not-an-int",
            ]
        )


def test_unfulfillable_reinject_plumbs_into_suit_task_config(
    tmp_path: pathlib.Path,
):
    """``_config_from_args`` must propagate the parsed value verbatim
    onto :class:`SuitTaskConfig` (otherwise the framework default
    sticks regardless of what the operator passed)."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
            "--unfulfillable-reinject-max-per-task=7",
        ]
    )

    config = cli_module._config_from_args(
        args,
        run_id="r1",
        secondary_id="primary",
    )
    assert config.unfulfillable_reinject_max_per_task == 7


def test_unfulfillable_reinject_default_plumbs_none_into_config(
    tmp_path: pathlib.Path,
):
    parser = build_parser()
    args = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
        ]
    )
    config = cli_module._config_from_args(
        args,
        run_id="r1",
        secondary_id="primary",
    )
    assert config.unfulfillable_reinject_max_per_task is None


def test_unfulfillable_reinject_not_stripped_from_framework_argv():
    """The flag must NOT be in the CSR-only strip set: the framework's
    own argparse parses ``--unfulfillable-reinject-max-per-task`` and
    plumbs it into PrimaryCoordinator(__init__). If we strip it before
    handing argv to the framework, the operator's value silently
    reverts to the framework default."""
    forwarded = cli_module._strip_csr_argv_for_framework(
        [
            "submit",
            "--shared-fs",
            "/tmp/x",
            "--multi-computer",
            "slurm",
            "--unfulfillable-reinject-max-per-task",
            "5",
        ]
    )
    assert "--unfulfillable-reinject-max-per-task" in forwarded
    # Adjacent value must survive too (argparse consumes the next token).
    idx = forwarded.index("--unfulfillable-reinject-max-per-task")
    assert forwarded[idx + 1] == "5"


def test_apply_unfulfillable_reinject_cap_calls_setter_when_set(
    tmp_path: pathlib.Path,
):
    """SuitTask helper should invoke the framework setter exactly once
    with the configured value when the cap is not None."""
    from unittest.mock import MagicMock

    from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

    config = SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "m",
        dataset_dir=tmp_path / "d",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="h",
        unfulfillable_reinject_max_per_task=4,
    )
    task = SuitTask(config)
    handle = MagicMock()
    task.apply_unfulfillable_reinject_cap(handle)
    handle.set_unfulfillable_reinject_max_per_task.assert_called_once_with(4)


def test_apply_unfulfillable_reinject_cap_skips_when_none(
    tmp_path: pathlib.Path,
):
    """With ``None`` (the default) the setter must NOT be called — that
    lets the framework keep its own default semantics (unbounded)."""
    from unittest.mock import MagicMock

    from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

    config = SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "m",
        dataset_dir=tmp_path / "d",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="h",
        unfulfillable_reinject_max_per_task=None,
    )
    task = SuitTask(config)
    handle = MagicMock()
    task.apply_unfulfillable_reinject_cap(handle)
    handle.set_unfulfillable_reinject_max_per_task.assert_not_called()


def test_apply_unfulfillable_reinject_cap_accepts_zero(
    tmp_path: pathlib.Path,
):
    """Zero is a valid cap (means 'don't auto-reinject') — the helper
    must distinguish it from None and call the setter with 0."""
    from unittest.mock import MagicMock

    from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

    config = SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "m",
        dataset_dir=tmp_path / "d",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="h",
        unfulfillable_reinject_max_per_task=0,
    )
    task = SuitTask(config)
    handle = MagicMock()
    task.apply_unfulfillable_reinject_cap(handle)
    handle.set_unfulfillable_reinject_max_per_task.assert_called_once_with(0)


def test_apply_unfulfillable_reinject_cap_handles_missing_setter(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
):
    """If the framework version doesn't expose the setter, the helper
    must log a warning + continue rather than raising."""

    from compiler_suit_runner.suit_task import SuitTask, SuitTaskConfig

    config = SuitTaskConfig(
        flake_ref=".",
        sys_name="x86_64-linux",
        shared_fs=tmp_path,
        manifest_dir=tmp_path / "m",
        dataset_dir=tmp_path / "d",
        peers_dir=tmp_path / "peers",
        run_id="r1",
        secondary_id="primary",
        hostname="h",
        unfulfillable_reinject_max_per_task=4,
    )
    task = SuitTask(config)

    class _LegacyHandle:
        pass

    # Must not raise.
    task.apply_unfulfillable_reinject_cap(_LegacyHandle())

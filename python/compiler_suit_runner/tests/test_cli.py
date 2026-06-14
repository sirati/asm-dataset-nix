"""Unit tests for ``compiler_suit_runner.cli``.

The CLI surface is exercised through ``main(argv_list)`` with the
nix/git-touching helpers and the SuitTask side-effects monkeypatched
out. The IncrementalCache runs for REAL against a per-test
``cache_root`` under tmp_path (no real nix, no cache writes against
the user's home dir) so the enumeration memoization is exercised
end-to-end.
"""

from __future__ import annotations

import argparse
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
    IncrementalCache,
    InputHashInputs,
    ToolchainAxes,
    VariantAxes,
    compute_subentry_key,
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
    """Wire up stubs so cmd_submit can run without real nix, git, or
    the SuitTask side-effects.

    cmd_submit drives the submit-time pre-flight:
    ``enumerate_toolchains_only`` + ``enumerate_variants`` (per-binary
    metadata, both memoized via the REAL :class:`IncrementalCache`
    rooted at the per-test ``cache_root``) + ``emit_all_manifests``.
    ``preflight_calls`` counts the toolchain enumeration as a proxy for
    "pre-flight ran"; ``variant_calls`` counts the variant enumeration.
    Only the repo-state collection is faked (``state["repo_inputs"]``;
    mutate it to simulate a repo-state change between invocations).
    """

    state: dict = {
        "preflight_calls": [],
        "variant_calls": [],
        "emit_calls": [],
        "emit_per_binary_metadata": [],
        "single_process_calls": [],
        "collect_calls": [],
        # Mutable so individual tests can override what the stubbed
        # enumerate_* helpers return without rewriting the whole
        # fixture.
        "toolchain_return": ((), {}, ""),
        "variants_return": {},
        # Repo-state half of the memoization keys; swap for a different
        # InputHashInputs to simulate a flake.lock / git change.
        "repo_inputs": InputHashInputs(
            flake_lock=b"lock", git_rev="a" * 40, git_diff=b"",
        ),
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
        state["variant_calls"].append((flake_ref, sys_name, packages, archs))
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

        # Simulate manifest_dir population.
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

    def fake_single_process(config, *, args=None, logger=None):
        state["single_process_calls"].append(config)
        return 0

    monkeypatch.setattr(cli_module, "run_single_process", fake_single_process)

    def fake_collect_input_hash_inputs(repo_root, **_kw):
        state["collect_calls"].append(repo_root)
        return state["repo_inputs"]

    monkeypatch.setattr(
        cli_module,
        "collect_input_hash_inputs",
        fake_collect_input_hash_inputs,
    )

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


def test_toolchain_axes_from_args_normalizes(tmp_path: pathlib.Path):
    """The toolchains sub-entry axes derived from the namespace are
    canonical: arch ordering and duplicates don't matter; defaults
    resolve like the enumeration call site resolves them."""
    args_a = _make_args(tmp_path, archs=["x86_64", "aarch64", "x86_64"])
    args_b = _make_args(tmp_path, archs=["aarch64", "x86_64"])
    axes_a = cli_module._toolchain_axes_from_args(args_a)
    axes_b = cli_module._toolchain_axes_from_args(args_b)
    assert isinstance(axes_a, ToolchainAxes)
    assert axes_a == axes_b
    assert axes_a.archs == ("aarch64", "x86_64")
    assert axes_a.sys_name == "x86_64-linux"
    # packages are NOT a toolchains axis.
    assert not hasattr(axes_a, "packages")


def test_variant_axes_from_args_normalizes(tmp_path: pathlib.Path):
    """The variants sub-entry axes derived from the namespace are
    canonical: package/arch ordering and duplicates don't matter;
    defaults resolve like the enumeration call site resolves them
    (``variant_sample or 0`` / ``variant_seed or "42"``)."""
    args_a = _make_args(
        tmp_path,
        packages=["zlib", "lz4", "zlib"],
        archs=["x86_64", "aarch64"],
        variant_sample=2,
    )
    args_b = _make_args(
        tmp_path,
        packages=["lz4", "zlib"],
        archs=["aarch64", "x86_64"],
        variant_sample=2,
    )
    axes_a = cli_module._variant_axes_from_args(args_a)
    axes_b = cli_module._variant_axes_from_args(args_b)
    assert isinstance(axes_a, VariantAxes)
    assert axes_a == axes_b
    assert axes_a.packages == ("lz4", "zlib")
    assert axes_a.archs == ("aarch64", "x86_64")
    assert axes_a.variant_sample == 2
    # Namespace fields absent from _make_args fall back to the same
    # defaults cmd_submit's enumeration call site uses.
    assert axes_a.variant_seed == "42"
    assert axes_a.sys_name == "x86_64-linux"


def test_subentry_keys_vary_with_their_own_axes(tmp_path: pathlib.Path):
    """Same repo state, different invocation => the RIGHT sub-entry key
    changes: ``--packages`` splits only the variants key (the
    nano-vs-16-binary contamination), ``--archs`` splits both."""
    repo = InputHashInputs(flake_lock=b"lock", git_rev="a" * 40, git_diff=b"")

    def keys(args):
        return (
            compute_subentry_key(
                repo, cli_module._toolchain_axes_from_args(args)
            ),
            compute_subentry_key(
                repo, cli_module._variant_axes_from_args(args)
            ),
        )

    tc_nano, var_nano = keys(_make_args(tmp_path, packages=["zlib"]))
    tc_full, var_full = keys(
        _make_args(
            tmp_path,
            packages=[
                "bzip2", "lz4", "xz", "zlib", "cjson", "expat", "libyaml",
                "xxhash", "libb2", "mujs", "duktape", "m4", "bc", "dash",
                "ed", "mawk",
            ],
        )
    )
    # --packages: toolchains key unchanged, variants key split.
    assert tc_nano == tc_full
    assert var_nano != var_full

    # --archs: both keys split.
    tc_archs, var_archs = keys(
        _make_args(tmp_path, packages=["zlib"], archs=["x86_64"])
    )
    assert tc_archs != tc_nano
    assert var_archs != var_nano

    # --variant-sample: variants key only.
    tc_sample, var_sample = keys(
        _make_args(tmp_path, packages=["zlib"], variant_sample=5)
    )
    assert tc_sample == tc_nano
    assert var_sample != var_nano

    # Identical invocation (re-parsed, different objects) is stable.
    assert keys(_make_args(tmp_path, packages=["zlib"])) == (
        tc_nano, var_nano,
    )


def test_cmd_submit_second_invocation_hits_memoized_enumerations(
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """A second identical invocation hits both sub-entries (no
    enumeration re-runs) but still runs the WHOLE downstream
    state-building path — manifest emission and dispatch fire again."""
    assert cmd_submit(_make_args(tmp_path)) == 0
    assert len(stub_submit_helpers["preflight_calls"]) == 1
    assert len(stub_submit_helpers["variant_calls"]) == 1

    assert cmd_submit(_make_args(tmp_path)) == 0
    # Enumerations memoized: not called again.
    assert len(stub_submit_helpers["preflight_calls"]) == 1
    assert len(stub_submit_helpers["variant_calls"]) == 1
    # The unified path ran both times.
    assert len(stub_submit_helpers["emit_calls"]) == 2
    assert len(stub_submit_helpers["single_process_calls"]) == 2


def test_cmd_submit_miss_stores_both_subentries(
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    assert cmd_submit(_make_args(tmp_path)) == 0
    # Pre-flight ran.
    assert stub_submit_helpers["preflight_calls"]
    assert stub_submit_helpers["variant_calls"]
    # Both sub-entries persisted (memoized at enumeration time).
    cache_root = tmp_path / "cache"
    tc_entries = list((cache_root / "toolchains").iterdir())
    var_entries = list((cache_root / "variants").iterdir())
    assert len(tc_entries) == 1
    assert len(var_entries) == 1
    assert (tc_entries[0] / "entry.json").is_file()
    assert (var_entries[0] / "entry.json").is_file()


def test_cmd_submit_subentry_keying(
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """Per-sub-entry keying through the full cmd_submit flow: a
    --packages change re-enumerates only variants; an --archs change
    re-enumerates both; a repo-state change re-enumerates both."""
    state = stub_submit_helpers

    assert cmd_submit(_make_args(tmp_path, packages=["zlib"])) == 0
    assert len(state["preflight_calls"]) == 1
    assert len(state["variant_calls"]) == 1

    # --packages change: toolchains HIT, variants MISS.
    assert cmd_submit(
        _make_args(tmp_path, packages=["zlib", "lz4"])
    ) == 0
    assert len(state["preflight_calls"]) == 1
    assert len(state["variant_calls"]) == 2

    # --archs change: BOTH miss.
    assert cmd_submit(
        _make_args(tmp_path, packages=["zlib"], archs=["x86_64"])
    ) == 0
    assert len(state["preflight_calls"]) == 2
    assert len(state["variant_calls"]) == 3

    # Repo-state change (same invocation as run 1): BOTH miss.
    state["repo_inputs"] = InputHashInputs(
        flake_lock=b"lock", git_rev="b" * 40, git_diff=b"",
    )
    assert cmd_submit(_make_args(tmp_path, packages=["zlib"])) == 0
    assert len(state["preflight_calls"]) == 3
    assert len(state["variant_calls"]) == 4


def test_cmd_submit_treats_corrupt_entry_as_miss_and_reheals(
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """A legacy/corrupt entry under the new key is never replayed: the
    enumeration re-runs and the entry is re-stored, after which the
    next invocation hits again."""
    state = stub_submit_helpers
    assert cmd_submit(_make_args(tmp_path)) == 0
    assert len(state["preflight_calls"]) == 1

    # Corrupt both sub-entries in place: legacy-shaped toolchains dir
    # (no entry.json) + wrong-version variants entry.
    cache_root = tmp_path / "cache"
    (tc_entry,) = (cache_root / "toolchains").iterdir()
    (tc_entry / "entry.json").unlink()
    (tc_entry / "manifests.tar").write_bytes(b"legacy")
    (var_entry,) = (cache_root / "variants").iterdir()
    body = json.loads((var_entry / "entry.json").read_text())
    body["version"] = 0
    (var_entry / "entry.json").write_text(json.dumps(body))

    # Run 2: both treated as a miss; entries re-stored.
    assert cmd_submit(_make_args(tmp_path)) == 0
    assert len(state["preflight_calls"]) == 2
    assert len(state["variant_calls"]) == 2

    # Run 3: healed — both hit again.
    assert cmd_submit(_make_args(tmp_path)) == 0
    assert len(state["preflight_calls"]) == 2
    assert len(state["variant_calls"]) == 2


def test_cmd_submit_hit_with_missing_toolchains_fails_like_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """Stale-store safety: the local toolchain availability check runs
    on the UNIFIED path, so a cache hit whose drvs were GC'd since the
    store behaves exactly like a miss would (abort before emission when
    --build-compilers is off)."""
    state = stub_submit_helpers
    # The aggregate drv must be a real file: lookup_toolchains verifies
    # its existence (GC-staleness guard) before returning a hit, and
    # this test exercises the per-toolchain-OUTPUT staleness path.
    agg = tmp_path / "fake-toolchains.drv"
    agg.write_text("")
    state["toolchain_return"] = (
        (("x86_64", "gcc15"),),
        {("x86_64", "gcc15"): "/nix/store/fake-gcc15.drv"},
        str(agg),
    )
    state["variants_return"] = {
        "hello": {
            "archs": ["x86_64"],
            "sample_size": 2,
            "sample_seed": "42",
            "tier": 1,
        },
    }
    assert cmd_submit(_make_args(tmp_path)) == 0
    assert len(state["emit_calls"]) == 1

    # The local store "loses" the toolchain outputs.
    monkeypatch.setattr(
        cli_module, "check_toolchains_locally", lambda drvs: frozenset(drvs),
    )

    # Cache-hit invocation: aborts before emission/dispatch...
    rc_hit = cmd_submit(_make_args(tmp_path))
    assert len(state["preflight_calls"]) == 1  # enumeration was a hit
    # ...exactly like the forced-miss invocation does.
    rc_miss = cmd_submit(_make_args(tmp_path, no_cache=True))
    assert len(state["preflight_calls"]) == 2  # forced miss re-enumerated
    assert rc_hit == rc_miss == 1
    assert len(state["emit_calls"]) == 1
    assert len(state["single_process_calls"]) == 1


# ---------------------------------------------------------------------------
# Submitter-side toolchain-archive produce + gateway upload (dedup, SLURM)
# ---------------------------------------------------------------------------


class _FakeGateway:
    """Records connect / create_directory / upload_file / disconnect.

    Stands in for the framework's ``Gateway`` so the submitter-side
    toolchain-archive upload can be asserted without a real SSH hop.
    ``file_exists`` + ``download_file`` back the realized-archive sidecar
    skip check (mirroring the real Gateway protocol); callers that want to
    simulate a matching sidecar can set ``sidecar_content`` on the instance.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.connected = False
        self.created_dirs: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.disconnected = False
        # Set to a hex string to simulate an existing sidecar on the remote.
        self.sidecar_content: str = ""

    def connect(self) -> None:
        self.connected = True

    def create_directory(self, remote_dir) -> None:
        self.created_dirs.append(str(remote_dir))

    def upload_file(self, local, remote) -> None:
        self.uploads.append((str(local), str(remote)))

    def file_exists(self, remote) -> bool:
        """True iff a sidecar was pre-configured (empty string = absent)."""
        return bool(self.sidecar_content)

    def download_file(self, remote, local) -> None:
        """Write the pre-configured sidecar content to the local path."""
        pathlib.Path(local).write_text(self.sidecar_content)

    def disconnect(self) -> None:
        self.disconnected = True


def _patch_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Patch the lazily-imported gateway API + the toolchain export.

    Returns a state dict capturing the created gateway, the parsed
    GatewayConfig (so auth threading can be asserted), and the
    export_toolchain_archive call args.
    """
    from dynamic_runner.packaging.gateway import GatewayConfig

    state: dict = {"gateway": None, "config": None, "export_calls": []}

    def fake_parse(url):
        # Mirror the real parser's shape: auth fields start unset.
        cfg = GatewayConfig(
            mode="ssh", ssh_user="kruppb", ssh_host="localhost",
            ssh_port=2222,
        )
        state["config"] = cfg
        return cfg

    def fake_create(cfg):
        gw = _FakeGateway(cfg)
        state["gateway"] = gw
        return gw

    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.parse_gateway_url", fake_parse,
    )
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.create_gateway", fake_create,
    )

    def fake_export(tc_agg, out_dir, *, run_subprocess=None):
        state["export_calls"].append((tc_agg, pathlib.Path(out_dir)))
        archive = pathlib.Path(out_dir) / "toolchains.drv.archive"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"NIX_EXPORT:toolchain")
        return archive

    monkeypatch.setattr(
        "compiler_suit_runner.preflight.export_toolchain_archive", fake_export,
    )
    return state


def test_produce_and_upload_toolchain_archive_happy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """The submitter produces the toolchain archive locally then
    create_directory + upload_file it to the gateway out/_matrix_eval,
    threading --ssh-identity-file / --ssh-config onto the GatewayConfig
    (parse_gateway_url leaves them None) so the upload authenticates the
    same way the rest of the dispatch does."""
    import logging

    state = _patch_gateway(monkeypatch)
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
        ssh_identity_file="/tmp/id_ed25519",
        ssh_config="/tmp/ssh_config",
    )
    rc = cli_module._produce_and_upload_toolchain_archive(
        args, "/nix/store/tc-agg.drv", logging.getLogger("t"),
    )
    assert rc == 0
    # Produced locally (submitter tmp dir, NOT the gateway path).
    assert state["export_calls"]
    assert state["export_calls"][0][0] == "/nix/store/tc-agg.drv"
    # Uploaded to the gateway out/_matrix_eval (bind-mounts into
    # /app/out-network on secondaries).
    gw = state["gateway"]
    assert gw.connected and gw.disconnected
    assert gw.created_dirs == ["/data/slurm/asm-dataset/out/_matrix_eval"]
    assert len(gw.uploads) == 1
    local, remote = gw.uploads[0]
    assert local.endswith("/toolchains.drv.archive")
    assert remote == (
        "/data/slurm/asm-dataset/out/_matrix_eval/toolchains.drv.archive"
    )
    # AUTH (R4): identity/config threaded onto the parsed config.
    assert state["config"].ssh_identity_file == "/tmp/id_ed25519"
    assert state["config"].ssh_config_file == "/tmp/ssh_config"


def test_produce_and_upload_toolchain_archive_export_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """A failed local produce returns 1 (abort dispatch) and never
    touches the gateway."""
    import logging

    state = _patch_gateway(monkeypatch)

    def boom(tc_agg, out_dir, *, run_subprocess=None):
        raise RuntimeError("nix-store export failed")

    monkeypatch.setattr(
        "compiler_suit_runner.preflight.export_toolchain_archive", boom,
    )
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
        ssh_identity_file=None,
        ssh_config=None,
    )
    rc = cli_module._produce_and_upload_toolchain_archive(
        args, "/nix/store/tc-agg.drv", logging.getLogger("t"),
    )
    assert rc == 1
    # Gateway was never created (produce failed first).
    assert state["gateway"] is None


def test_produce_and_upload_toolchain_archive_upload_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """A gateway upload failure returns 1 and still disconnects."""
    import logging

    state = _patch_gateway(monkeypatch)

    def fake_create(cfg):
        gw = _FakeGateway(cfg)

        def _boom(local, remote):
            raise RuntimeError("scp refused")

        gw.upload_file = _boom  # type: ignore[method-assign]
        state["gateway"] = gw
        return gw

    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.create_gateway", fake_create,
    )
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
        ssh_identity_file=None,
        ssh_config=None,
    )
    rc = cli_module._produce_and_upload_toolchain_archive(
        args, "/nix/store/tc-agg.drv", logging.getLogger("t"),
    )
    assert rc == 1
    assert state["gateway"].disconnected


def test_cmd_submit_no_cache_flag_disables_memoization(
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """--no-cache: the repo state is never collected, nothing is stored
    and every invocation re-enumerates."""
    state = stub_submit_helpers
    assert cmd_submit(_make_args(tmp_path, no_cache=True)) == 0
    assert cmd_submit(_make_args(tmp_path, no_cache=True)) == 0
    assert len(state["preflight_calls"]) == 2
    assert len(state["variant_calls"]) == 2
    assert state["collect_calls"] == []
    assert not (tmp_path / "cache").exists()


def test_cmd_submit_propagates_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    stub_submit_helpers: dict,
):
    """If run_single_process returns 1, cmd_submit returns 1. The
    enumeration memoization is dispatch-independent (the memoized
    values are pure functions of the repo state), so a follow-up
    invocation still hits the cache."""

    monkeypatch.setattr(
        cli_module, "run_single_process", lambda *a, **kw: 1
    )

    assert cmd_submit(_make_args(tmp_path)) == 1
    assert cmd_submit(_make_args(tmp_path)) == 1
    # Second run hit the memoized enumerations despite the failed
    # dispatch of the first.
    assert len(stub_submit_helpers["preflight_calls"]) == 1
    assert len(stub_submit_helpers["variant_calls"]) == 1


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
    # The error log explicitly names the empty aggregate as the cause.
    assert any(
        "toolchain aggregate is empty" in rec.getMessage()
        for rec in caplog.records
    )
    # The fail-fast guard runs on the UNIFIED path: a memoized re-run
    # (cache hit on both sub-entries) aborts identically.
    with caplog.at_level("ERROR"):
        assert cmd_submit(_make_args(tmp_path)) == 1
    assert len(stub_submit_helpers["preflight_calls"]) == 1  # hit
    assert stub_submit_helpers["emit_calls"] == []


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
    # toolchain_aggregate_drv, toolchain_dedup. Per-arch suffix selection
    # now lives in eval_worker, not the submit-time flatten loop. The
    # dep_graph archive root is NOT threaded via per_binary_metadata
    # anymore — the worker reads it from BuildWorkerEnv (container view),
    # so this dict carries no path field. ``toolchain_dedup`` IS carried
    # per-binary so each matrix_eval worker knows whether to subtract the
    # toolchain closure from its exported diff archive (it defaults ON;
    # the eval worker produces the shared toolchains.drv.archive — the
    # submitter no longer exports it).
    assert "suffixes" not in pbm["hello"]
    assert "matrix_eval_out_dir" not in pbm["hello"]
    assert set(pbm["hello"].keys()) == {
        "archs",
        "variant_sample",
        "variant_seed",
        "tier",
        "toolchain_aggregate_drv",
        "toolchain_dedup",
    }
    assert pbm["hello"]["toolchain_aggregate_drv"] == aggregate_drv
    assert pbm["hello"]["toolchain_dedup"] is True
    assert pbm["hello"]["archs"] == ["x86_64"]
    assert pbm["hello"]["variant_sample"] == 2
    assert pbm["hello"]["variant_seed"] == 42
    assert pbm["hello"]["tier"] == 1


# ---------------------------------------------------------------------------
# Hit-state == miss-state (the unified state-building path)
# ---------------------------------------------------------------------------


_EQ_TC_PAIRS = (("aarch64", "clang18"), ("x86_64", "gcc15"))
_EQ_TC_DRVS = {
    ("aarch64", "clang18"): "/nix/store/fake-clang18.drv",
    ("x86_64", "gcc15"): "/nix/store/fake-gcc15.drv",
}
def _equivalence_stubs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> dict:
    """Fake ONLY the nix/git-touching helpers; the cache, stage
    selection, manifest emission (REAL ``emit_all_manifests``) and
    config building all run for real, so the captured state reflects
    the production state-building path.

    The toolchain aggregate drv is a real on-disk file (under
    ``tmp_path``) because ``lookup_toolchains`` verifies its existence
    (GC-staleness guard) before returning a hit."""
    tc_agg = tmp_path / "fake-toolchains.drv"
    tc_agg.write_text("")
    state: dict = {
        "preflight_calls": 0,
        "variant_calls": 0,
        "configs": [],
        "eval_calls": [],
        "tc_agg": str(tc_agg),
    }

    def fake_tc(flake_ref, sys_name, *, archs=None, run_subprocess=None):
        state["preflight_calls"] += 1
        return _EQ_TC_PAIRS, dict(_EQ_TC_DRVS), state["tc_agg"]

    def fake_var(
        flake_ref, sys_name, *, packages=None, archs=None,
        sample_size=0, sample_seed="42", run_subprocess=None,
    ):
        state["variant_calls"] += 1
        pkgs = sorted(packages) if packages else ["hello", "zlib"]
        return {
            p: {
                "archs": ["aarch64", "x86_64"],
                "sample_size": int(sample_size or 0),
                "sample_seed": sample_seed,
                "tier": 1,
            }
            for p in pkgs
        }

    def fake_eval(drvs):
        call = {d: f"{d}.out" for d in drvs}
        state["eval_calls"].append(tuple(sorted(call.items())))
        return call

    def fake_single_process(config, *, args=None, logger=None):
        state["configs"].append(config)
        return 0

    monkeypatch.setattr(cli_module, "enumerate_toolchains_only", fake_tc)
    monkeypatch.setattr(cli_module, "enumerate_variants", fake_var)
    monkeypatch.setattr(
        cli_module, "check_toolchains_locally", lambda drvs: frozenset(),
    )
    monkeypatch.setattr(
        cli_module, "build_toolchains_locally", lambda drvs: None,
    )
    monkeypatch.setattr(cli_module, "eval_drv_outpaths", fake_eval)
    monkeypatch.setattr(cli_module, "run_single_process", fake_single_process)
    monkeypatch.setattr(
        cli_module,
        "collect_input_hash_inputs",
        lambda repo_root, **_kw: InputHashInputs(
            flake_lock=b"lock", git_rev="a" * 40, git_diff=b"",
        ),
    )
    return state


def _manifest_snapshot(manifest_dir: pathlib.Path) -> set:
    """The audit's manifest-set shape: (filename, item_class, payload)."""
    snap = set()
    for path in manifest_dir.glob("*.json"):
        if path.name.startswith(("_", ".")):
            continue
        body = json.loads(path.read_text())
        snap.add(
            (
                path.name,
                body["item_class"],
                json.dumps(body["payload"], sort_keys=True),
            )
        )
    return snap


def _run_miss_then_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    **arg_overrides,
) -> dict:
    """Run cmd_submit twice (miss, then hit) against one cache root and
    two separate shared-fs dirs; return the harness state."""
    state = _equivalence_stubs(monkeypatch, tmp_path)
    common = dict(cache_root=tmp_path / "cache", **arg_overrides)

    args_miss = _make_args(tmp_path / "miss", **common)
    assert cmd_submit(args_miss) == 0
    assert state["preflight_calls"] == 1
    assert state["variant_calls"] == 1

    args_hit = _make_args(tmp_path / "hit", **common)
    assert cmd_submit(args_hit) == 0
    # The second invocation hit BOTH sub-entries.
    assert state["preflight_calls"] == 1
    assert state["variant_calls"] == 1

    state["manifest_dirs"] = (
        pathlib.Path(args_miss.shared_fs) / "manifests",
        pathlib.Path(args_hit.shared_fs) / "manifests",
    )
    return state


@pytest.mark.parametrize("packages", [None, ["hello"]])
@pytest.mark.parametrize("debug_testbuild", [None, "hello"])
@pytest.mark.parametrize("build_compilers", [False, True])
def test_cmd_submit_hit_state_equals_miss_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    packages,
    debug_testbuild,
    build_compilers,
):
    """State-equivalence: a cache hit reaches dispatch with EXACTLY the
    state the miss built (one unified state-building path).

    Production consequences of the old divergence this guards against:
    a hit planned ZERO matrix_eval/dependency_graph tasks
    (per_binary_metadata stayed empty), dropped the toolchain
    aggregate (skipping the dedup-archive upload), and re-emitted
    manifest classes the original invocation's stage gate suppressed.
    """
    state = _run_miss_then_hit(
        monkeypatch,
        tmp_path,
        packages=packages,
        debug_testbuild=debug_testbuild,
        build_compilers=build_compilers,
    )
    cfg_miss, cfg_hit = state["configs"]

    # In-memory planning state identical and NON-empty on the hit.
    assert cfg_hit.per_binary_metadata == cfg_miss.per_binary_metadata
    assert cfg_hit.per_binary_metadata
    for meta in cfg_hit.per_binary_metadata.values():
        # Upload-gate input: the slurm-path gate is
        # ``toolchain_dedup and tc_aggregate_drv and gateway and
        # slurm_root_folder``; args are shared between the runs, the
        # dedup flag derives from args, so gate equality reduces to the
        # aggregate drv riding the hit path too.
        assert meta["toolchain_aggregate_drv"] == state["tc_agg"]

    # Manifest sets identical: same files, same classes, same payloads
    # (the stage gate applied on both paths).
    snap_miss, snap_hit = (
        _manifest_snapshot(d) for d in state["manifest_dirs"]
    )
    assert snap_hit == snap_miss
    if build_compilers or debug_testbuild:
        assert snap_miss  # toolchain stage emitted on both paths
    else:
        assert snap_miss == set()  # stage gate suppressed JSON classes

    # Placement inputs identical: outpath resolution ran on both paths
    # over the same drv set (placements derive 1:1 from these).
    assert state["eval_calls"][0] == state["eval_calls"][1]


def test_cmd_submit_hit_task_plan_equals_miss_task_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
):
    """TaskInfo-plan equivalence: discover_items over the hit-built
    config yields the same multiset of (task_id, phase_id, type_id,
    depends_on) as over the miss-built config."""
    from compiler_suit_runner.suit_task import SuitTask

    state = _run_miss_then_hit(
        monkeypatch,
        tmp_path,
        build_compilers=True,
        debug_testbuild="hello",
    )
    cfg_miss, cfg_hit = state["configs"]

    def dep_key(dep):
        if isinstance(dep, str):
            return ("", dep)
        return (
            str(getattr(dep, "phase_id", "")),
            str(getattr(dep, "task_id", "")),
        )

    def plan(config):
        infos = list(SuitTask(config).discover_items())
        return sorted(
            (
                str(getattr(ti, "task_id", "")),
                str(getattr(ti, "phase_id", "")),
                str(getattr(ti, "type_id", "")),
                tuple(
                    sorted(
                        dep_key(d)
                        for d in tuple(
                            getattr(ti, "task_depends_on", ()) or ()
                        )
                    )
                ),
            )
            for ti in infos
        )

    plan_miss = plan(cfg_miss)
    plan_hit = plan(cfg_hit)
    assert plan_hit == plan_miss
    # Sanity: the equal plans actually contain the phase-2/3 tasks the
    # old hit path silently dropped.
    task_ids = [t[0] for t in plan_miss]
    assert "dependency_graph" in task_ids
    assert len(task_ids) > 3  # matrix_evals + dep_graph + toolchain tasks


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
    """The flag is framework-owned (add_framework_arguments). Since the
    count-flag validation fix it rejects a negative at parse time via a
    non_negative_int converter — a negative budget is meaningless and
    would otherwise hit the PyO3 u32 boundary as an OverflowError. Zero
    stays valid: it disables the budget the flag gates."""
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
    ok = parser.parse_args(
        [
            "submit",
            "--shared-fs",
            str(tmp_path),
            "--multi-computer",
            "single-process",
            "--unfulfillable-reinject-max-per-task=0",
        ]
    )
    assert ok.unfulfillable_reinject_max_per_task == 0


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


def test_unfulfillable_reinject_lands_on_framework_namespace():
    """Post-migration there is no CSR-only strip pass: the consumer's
    submit subparser registers the framework flags directly (via
    add_framework_arguments) and passes the SAME parsed namespace to
    run(args=ns). So ``--unfulfillable-reinject-max-per-task 5`` must
    land on ``args.unfulfillable_reinject_max_per_task`` — that's the
    value the framework reads off the namespace (it is no longer
    re-parsed from argv, so a lost flag would silently revert to the
    framework default)."""
    parser = build_parser()
    args = parser.parse_args(
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
    assert args.unfulfillable_reinject_max_per_task == 5


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


# ---------------------------------------------------------------------------
# _produce_and_upload_toolchain_split_archives
# ---------------------------------------------------------------------------


def _patch_gateway_for_split(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sidecar_content: str = "",
) -> dict:
    """Patch the gateway + split export for split-archive tests.

    ``sidecar_content`` pre-sets the remote sidecar content (empty = upload
    not skipped).
    """
    from dynamic_runner.packaging.gateway import GatewayConfig

    state: dict = {
        "gateway": None, "config": None,
        "compute_calls": [], "export_calls": [],
    }

    def fake_parse(url):
        cfg = GatewayConfig(
            mode="ssh", ssh_user="kruppb", ssh_host="localhost", ssh_port=2222,
        )
        state["config"] = cfg
        return cfg

    def fake_create(cfg):
        gw = _FakeGateway(cfg)
        gw.sidecar_content = sidecar_content
        state["gateway"] = gw
        return gw

    monkeypatch.setattr("dynamic_runner.packaging.gateway.parse_gateway_url", fake_parse)
    monkeypatch.setattr("dynamic_runner.packaging.gateway.create_gateway", fake_create)

    from compiler_suit_runner.preflight import TOOLCHAIN_COMMON_ARCHIVE_NAME, ToolchainSplit

    def fake_compute(out_paths, **_kw):
        state["compute_calls"].append(list(out_paths))
        return ToolchainSplit(
            common_paths=frozenset({"/nix/store/glibc"}),
            delta_paths={out_paths[0]: (out_paths[0],)},
        )

    def fake_export(split, out_dir, **_kw):
        state["export_calls"].append(split)
        out_dir = pathlib.Path(out_dir)
        archives = {}
        common = out_dir / TOOLCHAIN_COMMON_ARCHIVE_NAME
        common.write_bytes(b"NIX_COMMON")
        archives[TOOLCHAIN_COMMON_ARCHIVE_NAME] = common
        for outpath in split.delta_paths:
            from compiler_suit_runner.preflight import toolchain_delta_archive_name
            name = toolchain_delta_archive_name(outpath)
            p = out_dir / name
            p.write_bytes(b"NIX_DELTA")
            archives[name] = p
        return archives

    monkeypatch.setattr("compiler_suit_runner.preflight.compute_toolchain_split", fake_compute)
    monkeypatch.setattr("compiler_suit_runner.preflight.export_toolchain_split", fake_export)
    return state


def test_produce_and_upload_split_archives_happy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
):
    """The submitter computes the split, exports common+delta archives, and
    uploads all to gateway out/_matrix_eval; returns 0 on success."""
    import logging

    state = _patch_gateway_for_split(monkeypatch)
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
        ssh_identity_file="/tmp/id_ed25519",
        ssh_config="/tmp/ssh_config",
    )
    rc = cli_module._produce_and_upload_toolchain_split_archives(
        args, ["/nix/store/abc123hhhhhhhhhhhhhhhhhhhhhhhhh-gcc15"], logging.getLogger("t"),
    )
    assert rc == 0
    assert state["compute_calls"]
    assert state["export_calls"]
    gw = state["gateway"]
    assert gw.connected and gw.disconnected
    assert "/data/slurm/asm-dataset/out/_matrix_eval" in gw.created_dirs
    remote_names = [r for _l, r in gw.uploads]
    assert any("toolchains.common.archive" in r for r in remote_names)
    # At least one delta archive uploaded.
    assert any("out.archive" in r and "common" not in r for r in remote_names)
    # SHA-256 sidecars uploaded.
    assert sum(1 for r in remote_names if r.endswith(".sha256")) >= 2
    assert state["config"].ssh_identity_file == "/tmp/id_ed25519"
    assert state["config"].ssh_config_file == "/tmp/ssh_config"


def test_produce_and_upload_split_archives_empty_paths_hard_aborts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
):
    """An empty out_paths list logs error and returns 1 (hard abort) without
    touching the gateway."""
    import logging

    from dynamic_runner.packaging.gateway import GatewayConfig
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.parse_gateway_url",
        lambda url: GatewayConfig(mode="ssh", ssh_user="u", ssh_host="h", ssh_port=22),
    )
    gateway_created: list = []
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.create_gateway",
        lambda cfg: gateway_created.append(cfg) or _FakeGateway(cfg),
    )
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
    )
    with caplog.at_level(logging.ERROR):
        rc = cli_module._produce_and_upload_toolchain_split_archives(
            args, [], logging.getLogger("t"),
        )
    assert rc == 1
    assert gateway_created == []
    assert any("aborting dispatch" in r.message for r in caplog.records)


def test_produce_and_upload_split_archives_compute_failure_hard_aborts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
):
    """A failed compute_toolchain_split logs error and returns 1 (hard abort)."""
    import logging

    def boom_compute(out_paths, **_kw):
        raise RuntimeError("requisites failed")

    monkeypatch.setattr("compiler_suit_runner.preflight.compute_toolchain_split", boom_compute)
    gateway_created: list = []
    from dynamic_runner.packaging.gateway import GatewayConfig
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.parse_gateway_url",
        lambda url: GatewayConfig(mode="ssh", ssh_user="u", ssh_host="h", ssh_port=22),
    )
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.create_gateway",
        lambda cfg: gateway_created.append(cfg) or _FakeGateway(cfg),
    )
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
    )
    with caplog.at_level(logging.ERROR):
        rc = cli_module._produce_and_upload_toolchain_split_archives(
            args, ["/nix/store/abc123-gcc"], logging.getLogger("t"),
        )
    assert rc == 1
    assert gateway_created == []
    assert any("aborting dispatch" in r.message for r in caplog.records)


def test_produce_and_upload_split_archives_export_failure_hard_aborts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
):
    """A failed export_toolchain_split logs error and returns 1 (hard abort)."""
    import logging

    from compiler_suit_runner.preflight import TOOLCHAIN_COMMON_ARCHIVE_NAME, ToolchainSplit

    monkeypatch.setattr(
        "compiler_suit_runner.preflight.compute_toolchain_split",
        lambda out_paths, **_kw: ToolchainSplit(
            common_paths=frozenset(), delta_paths={},
        ),
    )

    def boom_export(split, out_dir, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("compiler_suit_runner.preflight.export_toolchain_split", boom_export)
    gateway_created: list = []
    from dynamic_runner.packaging.gateway import GatewayConfig
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.parse_gateway_url",
        lambda url: GatewayConfig(mode="ssh", ssh_user="u", ssh_host="h", ssh_port=22),
    )
    monkeypatch.setattr(
        "dynamic_runner.packaging.gateway.create_gateway",
        lambda cfg: gateway_created.append(cfg) or _FakeGateway(cfg),
    )
    args = _make_args(
        tmp_path,
        multi_computer="slurm",
        gateway="ssh://kruppb@localhost:2222",
        slurm_root_folder="/data/slurm/asm-dataset",
    )
    with caplog.at_level(logging.ERROR):
        rc = cli_module._produce_and_upload_toolchain_split_archives(
            args, ["/nix/store/abc123-gcc"], logging.getLogger("t"),
        )
    assert rc == 1
    assert gateway_created == []
    assert any("aborting dispatch" in r.message for r in caplog.records)

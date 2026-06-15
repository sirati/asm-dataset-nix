"""Unit tests for ``compiler_suit_runner.preflight``.

All ``nix eval`` invocations are stubbed so the test suite never shells
out to nix. The injection seam is the ``run_subprocess`` parameter on
each public function.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Optional

import pytest

from compiler_suit_runner.preflight import (
    PreflightError,
    PreflightResult,
    TOOLCHAIN_COMMON_ARCHIVE_NAME,
    ToolchainSplit,
    build_toolchains_locally,
    check_toolchains_locally,
    compute_toolchain_split,
    enumerate_toolchains,
    enumerate_toolchains_only,
    enumerate_variants,
    export_toolchain_split,
    filter_existing_variants,
    path_info_batch,
    preflight,
    query_initial_toolchain_placement,
    toolchain_delta_archive_name,
    toolchain_id_for_outpath,
    run_nix_eval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_subprocess(responses: dict[str, object]):
    """Return a fake ``run_subprocess`` that maps attr -> JSON payload.

    The argv looks like ``["nix", "eval", "--extra-experimental-features",
    "nix-command flakes", "--json", "<flake>#<attr>"]``. We extract the
    final ``<flake>#<attr>`` token and look it up in ``responses``.
    Unknown attrs raise to surface mistakes loudly.
    """
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        # nix-eval-jobs has its own JSONL output format; intercept it
        # here and synthesise from the same fake response table by
        # mapping ``dataset.<sys>.<pkg>.<arch>`` to the matching
        # ``_drvPaths.<sys>.<pkg>.<arch>`` entry the fixture defined.
        if argv and pathlib.Path(argv[0]).name == "nix-eval-jobs":
            flake_idx = argv.index("--flake") if "--flake" in argv else -1
            select_idx = argv.index("--select") if "--select" in argv else -1
            if flake_idx == -1 or select_idx == -1:
                return b"", b"missing --flake or --select", 1
            flake_attr = argv[flake_idx + 1].split("#", 1)[1]
            # dataset.<sys>.<pkg>.<arch> → _drvPaths.<sys>.<pkg>.<arch>
            if not flake_attr.startswith("dataset."):
                return b"", f"unexpected eval-jobs attr {flake_attr}".encode(), 1
            drv_attr = "_drvPaths." + flake_attr[len("dataset."):]
            payload = responses.get(drv_attr)
            if not isinstance(payload, dict):
                return b"", f"no fake for {drv_attr}".encode(), 1
            select_expr = argv[select_idx + 1]
            wanted = re.findall(r'"([A-Za-z0-9._-]+)"\s*=\s*null', select_expr)
            wanted_set = set(wanted) if wanted else set(payload)
            lines = []
            for suffix, drv in payload.items():
                if suffix not in wanted_set:
                    continue
                if not isinstance(drv, str):
                    continue
                lines.append(json.dumps({"attr": suffix, "drvPath": drv}))
            return ("\n".join(lines) + "\n").encode("utf-8"), b"", 0
        # Find the flake#attr argument: it's the last token containing "#".
        # ``--apply`` mode shifts the lambda string to the very end of argv,
        # but the flake#attr token still appears earlier in the call.
        target = next(
            (tok for tok in reversed(argv) if "#" in tok),
            argv[-1],
        )
        # Detect --apply mode: returning the full inner attrset is fine
        # for tests because the fixture's responses are flat dicts and
        # the lambda's "m: { ... = m.X; ... }" projection picks valid keys.
        apply_expr: Optional[str] = None
        if "--apply" in argv:
            try:
                apply_expr = argv[argv.index("--apply") + 1]
            except IndexError:
                apply_expr = None
        if "#" not in target:
            return b"", b"missing #attr", 1
        attr = target.split("#", 1)[1]
        if attr not in responses:
            return b"", f"no fake for {attr}".encode(), 1
        payload = responses[attr]
        if isinstance(payload, bytes):
            return payload, b"", 0
        # When ``--apply`` is in play, mimic nix's behaviour of running
        # the lambda on the resolved attrset. The lambdas we emit are
        # one of:
        #   - ``m: { "S1" = m."S1"; ... }`` — projection by suffix list
        #   - ``m: builtins.mapAttrs (_: a: builtins.attrNames a) m``
        #     — pkg → [arch] index for the per-pkg arch query against
        #     ``dataset.<sys>``
        #   - ``m: builtins.attrNames m`` — top-level key list for the
        #     package-name query against ``dataset.<sys>``
        if apply_expr and isinstance(payload, dict):
            if "mapAttrs" in apply_expr and "attrNames" in apply_expr:
                projected = {
                    pkg: list(arches.keys()) if isinstance(arches, dict) else []
                    for pkg, arches in payload.items()
                }
                return json.dumps(projected).encode("utf-8"), b"", 0
            if "attrNames" in apply_expr:
                return json.dumps(list(payload.keys())).encode("utf-8"), b"", 0
            suffixes = re.findall(r'"([A-Za-z0-9._-]+)"\s*=\s*m\."', apply_expr)
            if suffixes:
                projected = {s: payload[s] for s in suffixes if s in payload}
                return json.dumps(projected).encode("utf-8"), b"", 0
        return json.dumps(payload).encode("utf-8"), b"", 0

    return runner, calls


def _fake_meta() -> dict:
    return {
        "hello": {
            "x86_64": {
                "gcc15-O0-baseline-unhardened": {
                    "variantLabel": "hello-x86_64-gcc15-O0-baseline-unhardened",
                    "package": "hello",
                    "arch": "x86_64",
                    "compiler": "gcc15",
                    "compilerFamily": "gcc",
                    "compilerVersion": "15.2.0",
                    "optimization": "O0",
                    "flags": "baseline",
                    "hardening": "unhardened",
                },
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "hello-x86_64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
            "aarch64": {
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "hello-aarch64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
        },
        "busybox": {
            "x86_64": {
                "gcc15-O2-baseline-unhardened": {
                    "variantLabel": "busybox-x86_64-gcc15-O2-baseline-unhardened",
                    "compiler": "gcc15",
                },
            },
        },
    }


def _fake_drvpaths() -> dict:
    return {
        "hello": {
            "x86_64": {
                "gcc15-O0-baseline-unhardened": "/nix/store/aaa-hello-x86-O0.drv",
                "gcc15-O2-baseline-unhardened": "/nix/store/bbb-hello-x86-O2.drv",
            },
            "aarch64": {
                "gcc15-O2-baseline-unhardened": "/nix/store/ccc-hello-aarch.drv",
            },
        },
        "busybox": {
            "x86_64": {
                "gcc15-O2-baseline-unhardened": "/nix/store/ddd-busybox-x86.drv",
            },
        },
    }


def _fake_toolchains() -> dict:
    return {
        "x86_64": [
            {"compiler": "gcc15", "family": "gcc", "version": "15.2.0"},
            {"compiler": "clang20", "family": "clang", "version": "20.1.8"},
        ],
        "aarch64": [
            {"compiler": "gcc15", "family": "gcc", "version": "15.2.0"},
        ],
    }


def _all_responses(sys_name: str = "x86_64-linux") -> dict[str, object]:
    """Build the fake nix-eval response table.

    Includes both the legacy full-system path (``_drvPaths.<sys>``,
    consumed when no --packages/--archs filter is set) and the
    per-(pkg, arch) scoped paths (``_drvPaths.<sys>.<pkg>.<arch>``,
    consumed when filters are set — matches the production code
    path that avoids touching broken matrix combos like
    gcc5+mips64el).
    """
    drvs = _fake_drvpaths()
    meta = _fake_meta()
    responses: dict[str, object] = {
        f"_meta.{sys_name}": meta,
        f"_drvPaths.{sys_name}": drvs,
        f"dataset.{sys_name}": drvs,
        f"_crossToolchainsMeta.{sys_name}": _fake_toolchains(),
    }
    for pkg, arch_map in drvs.items():
        for arch, suffix_map in arch_map.items():
            responses[f"_drvPaths.{sys_name}.{pkg}.{arch}"] = suffix_map
    # Per-(pkg, arch) meta cells — the production code now scopes
    # the meta eval the same way the drv-path eval is scoped, to
    # avoid forcing the full ~420k-entry tree on big package lists.
    for pkg, arch_map in meta.items():
        for arch, suffix_map in arch_map.items():
            responses[f"_meta.{sys_name}.{pkg}.{arch}"] = suffix_map
    return responses


# ---------------------------------------------------------------------------
# run_nix_eval
# ---------------------------------------------------------------------------


def test_run_nix_eval_json():
    runner, calls = _make_run_subprocess(_all_responses())
    result = run_nix_eval(".", "_meta.x86_64-linux", run_subprocess=runner)
    assert result == _fake_meta()
    # argv constructed correctly.
    assert calls and calls[0][0] == "nix"
    assert "--json" in calls[0]


def test_run_nix_eval_raw_returns_stdout_string():
    raw_payload = b"/nix/store/abc-foo.drv\n"

    def runner(argv):
        return raw_payload, b"", 0

    result = run_nix_eval(".", "_drvPaths.x86_64-linux.hello", raw=True, run_subprocess=runner)
    assert isinstance(result, str)
    assert result.startswith("/nix/store/abc-foo.drv")


def test_run_nix_eval_failure_raises_runtime_error():
    def runner(argv):
        return b"", b"attribute not found", 1

    with pytest.raises(RuntimeError, match="attribute not found"):
        run_nix_eval(".", "_meta.bogus", run_subprocess=runner)


def test_run_nix_eval_invalid_json_raises():
    def runner(argv):
        return b"not json", b"", 0

    with pytest.raises(RuntimeError, match="invalid JSON"):
        run_nix_eval(".", "_meta.x86_64-linux", run_subprocess=runner)


# ---------------------------------------------------------------------------
# enumerate_variants
# ---------------------------------------------------------------------------


def test_enumerate_variants_full_matrix():
    runner, _calls = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    # Per-binary metadata for both fixture packages.
    assert set(result.keys()) == {"hello", "busybox"}
    assert sorted(result["hello"]["archs"]) == ["aarch64", "x86_64"]
    assert result["busybox"]["archs"] == ["x86_64"]
    # New shape: no per-arch suffix lists — those are enumerated on the
    # cluster inside matrix_eval. The submitter only surfaces the four
    # per-pkg metadata fields below.
    assert set(result["hello"]) == {
        "archs", "sample_size", "sample_seed", "tier",
    }


def test_enumerate_variants_filter_by_packages():
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["hello"],
        run_subprocess=runner,
    )
    assert set(result.keys()) == {"hello"}


def test_enumerate_variants_filter_by_archs():
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        archs=["aarch64"],
        run_subprocess=runner,
    )
    # The submitter no longer probes per-(pkg, arch) cells, so every
    # package retains the caller's arch filter verbatim — the matrix_eval
    # worker is responsible for dropping (pkg, arch) cells the matrix
    # doesn't actually expose.
    assert set(result.keys()) == {"hello", "busybox"}
    assert result["hello"]["archs"] == ["aarch64"]
    assert result["busybox"]["archs"] == ["aarch64"]


def test_enumerate_variants_combined_filter():
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["busybox"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert set(result.keys()) == {"busybox"}
    assert result["busybox"]["archs"] == ["x86_64"]


def test_enumerate_variants_filter_no_match_returns_empty():
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["nonexistent-pkg"],
        run_subprocess=runner,
    )
    assert result == {}


def test_enumerate_variants_tier_assignment():
    """``hello`` (tier 1), ``busybox`` (tier 1) — verify tier mapping."""
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    assert result["hello"]["tier"] == 1
    assert result["busybox"]["tier"] == 1


# ---------------------------------------------------------------------------
# enumerate_toolchains
# ---------------------------------------------------------------------------


def test_enumerate_toolchains_full():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(".", "x86_64-linux", run_subprocess=runner)
    assert pairs == (
        ("aarch64", "gcc15"),
        ("x86_64", "clang20"),
        ("x86_64", "gcc15"),
    )


def test_enumerate_toolchains_filter_by_archs():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(
        ".",
        "x86_64-linux",
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert all(arch == "x86_64" for arch, _ in pairs)
    assert ("x86_64", "gcc15") in pairs
    assert ("x86_64", "clang20") in pairs


def test_enumerate_toolchains_unknown_arch_returns_empty():
    runner, _ = _make_run_subprocess(_all_responses())
    pairs = enumerate_toolchains(
        ".",
        "x86_64-linux",
        archs=["nonexistent"],
        run_subprocess=runner,
    )
    assert pairs == ()


# ---------------------------------------------------------------------------
# preflight composition
# ---------------------------------------------------------------------------


def test_preflight_returns_toolchains_only():
    """Composite now returns toolchains-only: variants and toolchain_drvs
    are populated by matrix_eval workers on secondaries, not by the
    submitter."""
    runner, calls = _make_run_subprocess(_all_responses())
    result = preflight(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    assert isinstance(result, PreflightResult)
    assert result.sys_name == "x86_64-linux"
    # Variants are deferred to the cluster; the composite returns empty.
    assert result.variants == ()
    assert result.toolchain_drvs == frozenset()
    assert result.common_dep_drvs == ()
    # Toolchain specs come from _crossToolchainsMeta and are populated.
    assert len(result.toolchain_specs) == 3

    # Only the toolchains attr is forced (no _meta/_drvPaths walk).
    eval_attrs = set()
    for argv in calls:
        target = next((tok for tok in argv if "#" in tok), None)
        if target is not None:
            eval_attrs.add(target.split("#", 1)[1])
    assert "_crossToolchainsMeta.x86_64-linux" in eval_attrs


def test_preflight_filter_pipes_through():
    runner, _ = _make_run_subprocess(_all_responses())
    result = preflight(
        ".",
        "x86_64-linux",
        packages=["hello"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert all(arch == "x86_64" for arch, _ in result.toolchain_specs)


# ---------------------------------------------------------------------------
# Variant sampling
# ---------------------------------------------------------------------------


def _wide_meta() -> dict:
    """A meta fixture with 6 (flag, hardening) combinations per
    (compiler, arch, opt) group — needed to exercise sampling.
    """
    out: dict[str, dict[str, dict[str, dict]]] = {"hello": {"x86_64": {}}}
    for flag in ("baseline", "frameptr", "noinline"):
        for hardening in ("hardened", "unhardened"):
            for opt in ("O0", "O2"):
                suffix = f"gcc15-{opt}-{flag}-{hardening}"
                out["hello"]["x86_64"][suffix] = {
                    "variantLabel": f"hello-x86_64-{suffix}",
                    "compiler": "gcc15",
                    "optimization": opt,
                    "flags": flag,
                    "hardening": hardening,
                }
    return out


def _wide_drvpaths() -> dict:
    out: dict[str, dict[str, dict[str, str]]] = {"hello": {"x86_64": {}}}
    for flag in ("baseline", "frameptr", "noinline"):
        for hardening in ("hardened", "unhardened"):
            for opt in ("O0", "O2"):
                suffix = f"gcc15-{opt}-{flag}-{hardening}"
                out["hello"]["x86_64"][suffix] = f"/nix/store/{suffix}.drv"
    return out


def _wide_responses(sys_name: str = "x86_64-linux") -> dict[str, object]:
    drvs = _wide_drvpaths()
    meta = _wide_meta()
    responses: dict[str, object] = {
        f"_meta.{sys_name}": meta,
        f"_drvPaths.{sys_name}": drvs,
        f"dataset.{sys_name}": drvs,
        f"_crossToolchainsMeta.{sys_name}": _fake_toolchains(),
    }
    for pkg, arch_map in drvs.items():
        for arch, suffix_map in arch_map.items():
            responses[f"_drvPaths.{sys_name}.{pkg}.{arch}"] = suffix_map
            for suffix, drv in suffix_map.items():
                responses[f"_drvPaths.{sys_name}.{pkg}.{arch}.{suffix}"] = drv
    for pkg, arch_map in meta.items():
        for arch, suffix_map in arch_map.items():
            responses[f"_meta.{sys_name}.{pkg}.{arch}"] = suffix_map
    return responses


def test_enumerate_variants_threads_sample_args_to_worker():
    """The submitter no longer pre-samples — sampling is the worker's
    job (``eval_worker._sample_per_arch``). ``enumerate_variants`` just
    threads the caller's ``sample_size`` and ``sample_seed`` through to
    each pkg entry so the wire manifest carries them verbatim."""
    runner, _ = _make_run_subprocess(_wide_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        sample_size=2,
        sample_seed="alpha",
        run_subprocess=runner,
    )
    # The arguments flow straight through; no rewrite to 0.
    assert result["hello"]["sample_size"] == 2
    assert result["hello"]["sample_seed"] == "alpha"


# ---------------------------------------------------------------------------
# filter_existing_variants
# ---------------------------------------------------------------------------


def _make_variant(label: str) -> dict:
    return {
        "label": label,
        "drv": f"/nix/store/{label}.drv",
        "variant_dir": label,
        "metadata_name": f"{label}.json",
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


def test_filter_existing_returns_all_when_dataset_dir_absent(tmp_path: pathlib.Path):
    variants = (_make_variant("a"), _make_variant("b"))
    kept, skipped = filter_existing_variants(
        variants, dataset_dir=tmp_path / "missing"
    )
    assert kept == variants
    assert skipped == 0


def test_filter_existing_drops_variants_with_existing_sidecar(
    tmp_path: pathlib.Path,
):
    """Skip-existing matches by sidecar JSON ``label`` (not filename)."""
    import json as _json

    dataset = tmp_path / "dataset"
    pkg_dir = dataset / "hello"
    pkg_dir.mkdir(parents=True)
    # Sidecars for "a" and "c" exist; "b" is not built yet.
    (pkg_dir / "a.json").write_text(_json.dumps({"label": "a"}))
    (pkg_dir / "c.json").write_text(_json.dumps({"label": "c"}))
    variants = (
        _make_variant("a"),
        _make_variant("b"),
        _make_variant("c"),
    )
    kept, skipped = filter_existing_variants(variants, dataset_dir=dataset)
    assert skipped == 2
    assert {v["label"] for v in kept} == {"b"}


def test_filter_existing_handles_legacy_flat_layout(
    tmp_path: pathlib.Path,
):
    """Variants with no ``pkg`` field fall back to scanning
    ``dataset_dir`` itself (legacy flat layout)."""
    import json as _json

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "a.json").write_text(_json.dumps({"label": "a"}))

    def _v(label: str) -> dict:
        v = _make_variant(label)
        v.pop("pkg", None)
        return v

    variants = (_v("a"), _v("b"))
    kept, skipped = filter_existing_variants(variants, dataset_dir=dataset)
    assert skipped == 1
    assert [v["label"] for v in kept] == ["b"]


# ---------------------------------------------------------------------------
# check_toolchains_locally / build_toolchains_locally
# ---------------------------------------------------------------------------


def test_check_toolchains_locally_returns_only_missing():
    """``nix path-info <drv>^out`` returns 0 when ``out`` is locally
    realised or reachable via a substituter, non-zero otherwise. The
    helper aggregates and returns the failing subset. ``^out`` (not
    ``^*``) is the right probe — auxiliary outputs (info / man) may
    legitimately exist only in the binary cache and aren't needed by
    the cluster's build_worker."""
    realised = "/nix/store/aaa.drv"
    missing = "/nix/store/bbb.drv"
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        drv_arg = argv[-1]
        # The helper probes the ``out`` output specifically.
        assert drv_arg.endswith("^out"), drv_arg
        if drv_arg.startswith(realised):
            return b"valid\n", b"", 0
        return b"", b"path is not valid\n", 1

    out = check_toolchains_locally(
        frozenset({realised, missing}), run_subprocess=runner,
    )
    assert out == frozenset({missing})
    # One probe per drv, regardless of order.
    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "nix"
        assert "path-info" in argv


def test_check_toolchains_locally_handles_empty_set():
    def runner(_argv):
        raise AssertionError("runner must not be called for empty input")

    assert check_toolchains_locally(frozenset(), run_subprocess=runner) == frozenset()


def test_check_toolchains_locally_skips_falsy_entries():
    """A drv path that is the empty string is a no-op (defensive: the
    primary's eval may produce ``""`` for an unresolvable attr; we
    don't want to count those as missing toolchains)."""

    def runner(_argv):
        return b"", b"", 0

    # An empty string is silently dropped; no probe issued for it.
    out = check_toolchains_locally(
        frozenset({""}), run_subprocess=runner,
    )
    assert out == frozenset()


def test_build_toolchains_locally_runs_nix_build_per_drv():
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return b"", b"", 0

    build_toolchains_locally(
        frozenset({"/nix/store/aaa.drv", "/nix/store/bbb.drv"}),
        run_subprocess=runner,
    )
    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "nix"
        assert "build" in argv
        assert "--no-link" in argv
        # ``<drv>^*`` realises every output.
        assert argv[-2].endswith("^*")


def test_build_toolchains_locally_raises_preflight_error_on_failure():
    def runner(_argv):
        return b"", b"compilation failed: missing /lib/foo\n", 1

    with pytest.raises(PreflightError, match="rc=1"):
        build_toolchains_locally(
            frozenset({"/nix/store/aaa.drv"}), run_subprocess=runner,
        )


def test_build_toolchains_locally_stops_at_first_failure():
    """Once one build fails, the helper raises immediately — the
    rest of the queue is implicitly abandoned because the operator
    needs to investigate the first failure before continuing."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        # First drv fails; second would succeed if we got there.
        if calls and len(calls) == 1:
            return b"", b"boom\n", 1
        return b"", b"", 0

    # ``sorted`` order matters: the helper iterates sorted(drv_set).
    drvs = frozenset({"/nix/store/aaa.drv", "/nix/store/bbb.drv"})
    with pytest.raises(PreflightError):
        build_toolchains_locally(drvs, run_subprocess=runner)
    assert len(calls) == 1




def test_enumerate_variants_returns_metadata_only():
    """``enumerate_variants`` returns per-binary metadata without
    forcing drv instantiation; the slow ``nix-eval-jobs`` work is
    deferred to matrix_eval workers on secondaries."""
    runner, calls = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        sample_size=3,
        sample_seed="alpha",
        run_subprocess=runner,
    )
    # Return shape: dict[pkg, metadata_dict] with exactly four fields
    # per pkg — no ``suffixes`` / ``suffixes_by_arch`` (those are
    # enumerated on the cluster).
    assert isinstance(result, dict)
    assert set(result.keys()) == {"hello", "busybox"}

    hello = result["hello"]
    assert set(hello) == {"archs", "sample_size", "sample_seed", "tier"}
    assert sorted(hello["archs"]) == ["aarch64", "x86_64"]
    assert hello["sample_size"] == 3
    assert hello["sample_seed"] == "alpha"
    assert hello["tier"] == 1

    busybox = result["busybox"]
    assert set(busybox) == {"archs", "sample_size", "sample_seed", "tier"}
    assert busybox["archs"] == ["x86_64"]
    assert busybox["sample_size"] == 3
    assert busybox["sample_seed"] == "alpha"

    # Regression guard: the slow drv-instantiation path is never hit.
    # No call should target ``_drvPaths.<sys>`` (the cached full-matrix
    # eval) and no nix-eval-jobs subprocess should be spawned. Also
    # neither ``_meta.<sys>`` nor any per-(pkg, arch) cell — those
    # belong to the matrix_eval worker.
    for argv in calls:
        if argv and pathlib.Path(argv[0]).name == "nix-eval-jobs":
            raise AssertionError(
                f"nix-eval-jobs spawned: {argv}"
            )
        for tok in argv:
            if "#_drvPaths." in tok or "#_meta." in tok:
                raise AssertionError(
                    f"variant-side eval triggered: {tok}"
                )


def test_enumerate_variants_honours_filters():
    """``packages`` / ``archs`` filters narrow the per-binary metadata."""
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["hello"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert set(result.keys()) == {"hello"}
    assert result["hello"]["archs"] == ["x86_64"]


# ---------------------------------------------------------------------------
# enumerate_toolchains_only
# ---------------------------------------------------------------------------


def test_enumerate_toolchains_only_returns_pairs_drvs_and_aggregate(
    monkeypatch: pytest.MonkeyPatch,
):
    """``enumerate_toolchains_only`` is the submitter-side toolchain
    bootstrap surface; it returns ``(pairs, drv_paths, aggregate_drv)``
    and never touches variants."""
    responses = _all_responses()
    # Stub drv resolution for every toolchain pair the fixture exposes.
    for arch, comp in (("x86_64", "gcc15"), ("x86_64", "clang20"),
                       ("aarch64", "gcc15")):
        responses[
            f"_crossToolchainMap.x86_64-linux.{arch}.{comp}.drvPath"
        ] = b"/nix/store/" + f"{arch}-{comp}.drv".encode()

    captured: dict[str, object] = {}

    def fake_make_wrapper(*, drvs, name, system, extra_nix_args=None):
        captured["drvs"] = list(drvs)
        captured["name"] = name
        captured["system"] = system
        return "/nix/store/aaaa-toolchains.drv"

    monkeypatch.setattr(
        "template_graph.make_sum_drv.make_wrapper_drv_from_paths",
        fake_make_wrapper,
    )

    runner, calls = _make_run_subprocess(responses)
    pairs, drv_paths, aggregate = enumerate_toolchains_only(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    assert pairs == (
        ("aarch64", "gcc15"),
        ("x86_64", "clang20"),
        ("x86_64", "gcc15"),
    )
    assert drv_paths == {
        ("aarch64", "gcc15"): "/nix/store/aarch64-gcc15.drv",
        ("x86_64", "clang20"): "/nix/store/x86_64-clang20.drv",
        ("x86_64", "gcc15"): "/nix/store/x86_64-gcc15.drv",
    }
    assert aggregate == "/nix/store/aaaa-toolchains.drv"
    # The aggregate-builder must see a SORTED leaf list (determinism:
    # same set of leaves → same wrapper-drv hash regardless of dict
    # iteration order).
    assert captured["drvs"] == sorted(drv_paths.values())
    assert captured["name"] == "toolchains"
    assert captured["system"] == "x86_64-linux"
    # No variant-side eval — we never look at _meta or _drvPaths.
    for argv in calls:
        for tok in argv:
            if "#_meta." in tok or "#_drvPaths." in tok:
                raise AssertionError(
                    f"toolchains-only path touched variant attrs: {tok}"
                )


def test_enumerate_toolchains_only_sorts_leaf_drvs_for_aggregate(
    monkeypatch: pytest.MonkeyPatch,
):
    """The aggregate-builder must receive leaf drv paths in sorted
    order so the wrapper-drv hash is stable across runs regardless of
    dict iteration order from ``eval_toolchain_drvs``."""
    responses = _all_responses()
    # Pick fake drv paths whose lexical sort order differs from the
    # default insertion order of the toolchain pairs fixture.
    responses["_crossToolchainMap.x86_64-linux.aarch64.gcc15.drvPath"] = (
        b"/nix/store/zzz-aarch64-gcc15.drv"
    )
    responses["_crossToolchainMap.x86_64-linux.x86_64.gcc15.drvPath"] = (
        b"/nix/store/aaa-x86_64-gcc15.drv"
    )
    responses["_crossToolchainMap.x86_64-linux.x86_64.clang20.drvPath"] = (
        b"/nix/store/mmm-x86_64-clang20.drv"
    )

    captured_drvs: list[list[str]] = []

    def fake_make_wrapper(*, drvs, name, system, extra_nix_args=None):
        captured_drvs.append(list(drvs))
        return "/nix/store/aggregate.drv"

    monkeypatch.setattr(
        "template_graph.make_sum_drv.make_wrapper_drv_from_paths",
        fake_make_wrapper,
    )

    runner, _calls = _make_run_subprocess(responses)
    _pairs, _drv_paths, _agg = enumerate_toolchains_only(
        ".", "x86_64-linux", run_subprocess=runner,
    )
    assert len(captured_drvs) == 1
    assert captured_drvs[0] == sorted(captured_drvs[0])
    # Sanity: the sorted list is what we actually expect for the
    # responses above (lexical drv-path order, not pair order).
    assert captured_drvs[0] == [
        "/nix/store/aaa-x86_64-gcc15.drv",
        "/nix/store/mmm-x86_64-clang20.drv",
        "/nix/store/zzz-aarch64-gcc15.drv",
    ]


def test_enumerate_toolchains_only_empty_pairs_skip_drv_eval(
    monkeypatch: pytest.MonkeyPatch,
):
    """If no toolchain pairs survive filtering, drv eval is skipped
    and the aggregate is the empty string."""
    wrapper_calls: list[object] = []

    def fake_make_wrapper(*, drvs, name, system, extra_nix_args=None):
        wrapper_calls.append((list(drvs), name, system))
        return "/nix/store/should-not-be-called.drv"

    monkeypatch.setattr(
        "template_graph.make_sum_drv.make_wrapper_drv_from_paths",
        fake_make_wrapper,
    )

    runner, calls = _make_run_subprocess(_all_responses())
    pairs, drv_paths, aggregate = enumerate_toolchains_only(
        ".",
        "x86_64-linux",
        archs=["nonexistent"],
        run_subprocess=runner,
    )
    assert pairs == ()
    assert drv_paths == {}
    assert aggregate == ""
    # No leaves → no wrapper-drv construction.
    assert wrapper_calls == []
    # Only the _crossToolchainsMeta eval ran; no per-pair drv resolve.
    for argv in calls:
        for tok in argv:
            if "_crossToolchainMap" in tok:
                raise AssertionError(
                    f"drv eval ran on empty pairs: {tok}"
                )


# ---------------------------------------------------------------------------
# path_info_batch — one subprocess for many drvs
# ---------------------------------------------------------------------------


def test_path_info_batch_uses_single_subprocess_call():
    """All drvs go through ONE ``nix path-info`` invocation, not N."""
    captured: list[list[str]] = []

    def runner(argv):
        captured.append(list(argv))
        # Modern nix shape: output path keyed, with ``deriver`` field.
        payload = {
            "/nix/store/out-a": {"deriver": "/nix/store/aaa.drv"},
            "/nix/store/out-b": {"deriver": "/nix/store/bbb.drv"},
            "/nix/store/out-c": {"deriver": "/nix/store/ccc.drv"},
        }
        return json.dumps(payload).encode("utf-8"), b"", 0

    drvs = [
        "/nix/store/aaa.drv",
        "/nix/store/bbb.drv",
        "/nix/store/ccc.drv",
    ]
    out = path_info_batch(drvs, run_subprocess=runner)

    # Single subprocess call covered every drv.
    assert len(captured) == 1
    argv = captured[0]
    assert argv[0] == "nix"
    assert argv[1] == "path-info"
    # Every drv shows up as a ``<drv>^*`` argument in the one call.
    joined = " ".join(argv)
    for drv in drvs:
        assert f"{drv}^*" in joined

    # Resolved outpaths flow back as drv -> outpath.
    assert out == {
        "/nix/store/aaa.drv": "/nix/store/out-a",
        "/nix/store/bbb.drv": "/nix/store/out-b",
        "/nix/store/ccc.drv": "/nix/store/out-c",
    }


def test_path_info_batch_empty_input_skips_subprocess():
    """Empty drv list short-circuits without spawning nix."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return b"{}", b"", 0

    assert path_info_batch([], run_subprocess=runner) == {}
    assert calls == []


def test_path_info_batch_legacy_array_shape():
    """Legacy ``nix path-info --json`` array output parses correctly."""

    def runner(argv):
        payload = [
            {
                "path": "/nix/store/out-a",
                "deriver": "/nix/store/aaa.drv",
                "valid": True,
            },
            {
                "path": "/nix/store/out-b",
                "deriver": "/nix/store/bbb.drv",
                "valid": True,
            },
        ]
        return json.dumps(payload).encode("utf-8"), b"", 0

    out = path_info_batch(
        ["/nix/store/aaa.drv", "/nix/store/bbb.drv"],
        run_subprocess=runner,
    )
    assert out == {
        "/nix/store/aaa.drv": "/nix/store/out-a",
        "/nix/store/bbb.drv": "/nix/store/out-b",
    }


def test_path_info_batch_nonzero_returns_empty():
    """Subprocess failure surfaces as an empty result (callers treat
    missing keys as 'not in local store')."""

    def runner(argv):
        return b"", b"some error", 2

    out = path_info_batch(
        ["/nix/store/aaa.drv"],
        run_subprocess=runner,
    )
    assert out == {}


# ---------------------------------------------------------------------------
# query_initial_toolchain_placement — submitter seed for placement map
# ---------------------------------------------------------------------------


def _make_check_validity_runner(invalid_outpaths: set[str]):
    """Fake ``nix-store --check-validity --print-invalid`` runner.

    Captures every argv it sees so the caller can assert one-call
    batching. Returns the configured invalid-subset of the queried
    outpaths on stdout (newline-delimited), rc=0. Refuses any argv that
    isn't a ``check-validity --print-invalid`` invocation so accidental
    fallthrough to per-outpath probes loudly fails.
    """
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        assert argv[0] == "nix-store", argv
        assert "--check-validity" in argv, argv
        assert "--print-invalid" in argv, argv
        # Everything after the flags is an outpath argument.
        queried = [
            a for a in argv[1:]
            if not a.startswith("--")
        ]
        out_lines = [op for op in queried if op in invalid_outpaths]
        return ("\n".join(out_lines) + ("\n" if out_lines else "")).encode("utf-8"), b"", 0

    return runner, calls


def test_query_initial_toolchain_placement_all_local_seeds_submitter():
    """Every toolchain outpath is in the local store -> every entry
    seeded with ``["submitter"]``. One batched subprocess call."""
    drv_outpaths = {
        "/nix/store/aaa.drv": "/nix/store/aaa-out",
        "/nix/store/bbb.drv": "/nix/store/bbb-out",
        "/nix/store/ccc.drv": "/nix/store/ccc-out",
    }
    drvs = frozenset(drv_outpaths.keys())
    # invalid_outpaths is empty: all queried outpaths are present.
    runner, calls = _make_check_validity_runner(invalid_outpaths=set())

    result = query_initial_toolchain_placement(
        drvs, drv_outpaths, run_subprocess=runner,
    )

    assert len(calls) == 1
    assert result == {
        "/nix/store/aaa-out": ["submitter"],
        "/nix/store/bbb-out": ["submitter"],
        "/nix/store/ccc-out": ["submitter"],
    }


def test_query_initial_toolchain_placement_none_local_returns_empty_lists():
    """Submitter has none of the toolchains locally -> every entry's
    placement list is empty."""
    drv_outpaths = {
        "/nix/store/aaa.drv": "/nix/store/aaa-out",
        "/nix/store/bbb.drv": "/nix/store/bbb-out",
    }
    drvs = frozenset(drv_outpaths.keys())
    invalid = set(drv_outpaths.values())  # everything missing
    runner, calls = _make_check_validity_runner(invalid_outpaths=invalid)

    result = query_initial_toolchain_placement(
        drvs, drv_outpaths, run_subprocess=runner,
    )

    assert len(calls) == 1
    assert result == {
        "/nix/store/aaa-out": [],
        "/nix/store/bbb-out": [],
    }


def test_query_initial_toolchain_placement_mixed_local_and_missing():
    """Submitter has some toolchains but not others -> the locally-
    present subset gets ``["submitter"]``, the rest get ``[]``."""
    drv_outpaths = {
        "/nix/store/aaa.drv": "/nix/store/aaa-out",  # local
        "/nix/store/bbb.drv": "/nix/store/bbb-out",  # missing
        "/nix/store/ccc.drv": "/nix/store/ccc-out",  # local
        "/nix/store/ddd.drv": "/nix/store/ddd-out",  # missing
    }
    drvs = frozenset(drv_outpaths.keys())
    invalid = {"/nix/store/bbb-out", "/nix/store/ddd-out"}
    runner, calls = _make_check_validity_runner(invalid_outpaths=invalid)

    result = query_initial_toolchain_placement(
        drvs, drv_outpaths, run_subprocess=runner,
    )

    assert len(calls) == 1
    assert result == {
        "/nix/store/aaa-out": ["submitter"],
        "/nix/store/bbb-out": [],
        "/nix/store/ccc-out": ["submitter"],
        "/nix/store/ddd-out": [],
    }


def test_query_initial_toolchain_placement_empty_input_skips_subprocess():
    """Empty drv set -> empty result dict, runner never called."""

    def runner(_argv):
        raise AssertionError("runner must not be called on empty input")

    result = query_initial_toolchain_placement(
        frozenset(), {}, run_subprocess=runner,
    )
    assert result == {}


def test_query_initial_toolchain_placement_drops_drvs_without_outpath():
    """Drvs missing from ``drv_outpaths`` are silently dropped — the
    submitter's drv->outpath table was incomplete for them and no
    placement record can be seeded."""
    drv_outpaths = {
        "/nix/store/aaa.drv": "/nix/store/aaa-out",
        # bbb.drv intentionally absent.
    }
    drvs = frozenset({"/nix/store/aaa.drv", "/nix/store/bbb.drv"})
    runner, calls = _make_check_validity_runner(invalid_outpaths=set())

    result = query_initial_toolchain_placement(
        drvs, drv_outpaths, run_subprocess=runner,
    )

    assert len(calls) == 1
    # Only the outpath that resolved appears in the result.
    assert result == {"/nix/store/aaa-out": ["submitter"]}


def test_query_initial_toolchain_placement_probe_failure_falls_back_to_empty():
    """nix-store probe rc!=0 -> conservative fallback: every entry is
    ``[]`` (no known holders). Cascade will repopulate later."""

    def runner(_argv):
        return b"", b"daemon down\n", 1

    drv_outpaths = {
        "/nix/store/aaa.drv": "/nix/store/aaa-out",
        "/nix/store/bbb.drv": "/nix/store/bbb-out",
    }
    result = query_initial_toolchain_placement(
        frozenset(drv_outpaths.keys()), drv_outpaths, run_subprocess=runner,
    )
    assert result == {
        "/nix/store/aaa-out": [],
        "/nix/store/bbb-out": [],
    }


# ---------------------------------------------------------------------------
# toolchain_id_for_outpath / toolchain_delta_archive_name
# ---------------------------------------------------------------------------


def test_toolchain_id_for_outpath_extracts_store_hash() -> None:
    """The store-hash (first '-'-delimited token) is returned as-is."""
    assert toolchain_id_for_outpath("/nix/store/abc123xyz-gcc-14.2.0") == "abc123xyz"


def test_toolchain_id_for_outpath_bare_hash_only() -> None:
    """A path with no trailing name component still works."""
    assert toolchain_id_for_outpath("/nix/store/hash0000-name") == "hash0000"


def test_toolchain_id_for_outpath_raises_for_invalid() -> None:
    """An empty or non-path-like string raises ValueError."""
    with pytest.raises(ValueError):
        toolchain_id_for_outpath("")


def test_toolchain_delta_archive_name() -> None:
    """Archive name is ``toolchains.<hash>.out.archive``."""
    name = toolchain_delta_archive_name("/nix/store/abc123xyz-gcc-14.2.0")
    assert name == "toolchains.abc123xyz.out.archive"


# ---------------------------------------------------------------------------
# common_dep_id_for_ident / common_dep_archive_name (affine common_dep gate)
# ---------------------------------------------------------------------------


def test_common_dep_id_for_ident_extracts_hash() -> None:
    """The leading store-hash token of a ``<hash>-<name>`` ident is the id."""
    from compiler_suit_runner.preflight import common_dep_id_for_ident  # noqa: PLC0415

    assert common_dep_id_for_ident("abc123xyz-flex-2.6.4") == "abc123xyz"


def test_common_dep_id_for_ident_tolerates_drv_suffix() -> None:
    """A ``.drv`` suffix on the name half does not change the hash token."""
    from compiler_suit_runner.preflight import common_dep_id_for_ident  # noqa: PLC0415

    assert common_dep_id_for_ident("hash0000-flex.drv") == "hash0000"


def test_common_dep_id_for_ident_idempotent_on_bare_hash() -> None:
    """A bare hash (no dash) round-trips to itself — the import action passes
    the already-stripped hash back through this helper."""
    from compiler_suit_runner.preflight import common_dep_id_for_ident  # noqa: PLC0415

    assert common_dep_id_for_ident("abc123xyz") == "abc123xyz"


def test_common_dep_id_for_ident_raises_for_leading_dash() -> None:
    """An ident with no extractable hash (leading dash / empty) raises."""
    from compiler_suit_runner.preflight import common_dep_id_for_ident  # noqa: PLC0415

    with pytest.raises(ValueError):
        common_dep_id_for_ident("-flex-2.6.4")
    with pytest.raises(ValueError):
        common_dep_id_for_ident("")


def test_common_dep_archive_name() -> None:
    """Archive name is ``common-<hash>.out.archive``."""
    from compiler_suit_runner.preflight import common_dep_archive_name  # noqa: PLC0415

    assert common_dep_archive_name("abc123xyz-flex-2.6.4") == "common-abc123xyz.out.archive"
    # Derivable from the already-stripped hash too (import-action side).
    assert common_dep_archive_name("abc123xyz") == "common-abc123xyz.out.archive"


# ---------------------------------------------------------------------------
# compute_toolchain_split
# ---------------------------------------------------------------------------


def _make_split_runner(closures: dict[str, list[str]]) -> "object":
    """Return a run_subprocess stub that answers requisites queries from
    ``closures`` (mapping outpath → list of closure paths)."""
    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            key = argv[3]
            paths = closures.get(key, [])
            return "\n".join(paths).encode() + b"\n", b"", 0
        if argv[:2] == ["nix-store", "--export"]:
            return b"NIX_EXPORT:common", b"", 0
        return b"", b"unexpected call", 1
    return runner


def test_compute_toolchain_split_common_is_freq_ge2() -> None:
    """COMMON = paths appearing in >=2 toolchain closures (freq>=2).

    With two toolchains, freq>=2 matches the strict intersection, so the
    expected set is the same as the old intersection test.
    """
    closures = {
        "/nix/store/aaa-gcc": ["/nix/store/aaa-gcc", "/nix/store/glibc", "/nix/store/libgcc"],
        "/nix/store/bbb-clang": ["/nix/store/bbb-clang", "/nix/store/glibc"],
    }
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)
    assert split.common_paths == frozenset({"/nix/store/glibc"})


def test_compute_toolchain_split_delta_is_closure_minus_common() -> None:
    """delta(Ti) = closure(Ti) - COMMON."""
    closures = {
        "/nix/store/aaa-gcc": ["/nix/store/aaa-gcc", "/nix/store/glibc", "/nix/store/libgcc"],
        "/nix/store/bbb-clang": ["/nix/store/bbb-clang", "/nix/store/glibc"],
    }
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)
    assert set(split.delta_paths["/nix/store/aaa-gcc"]) == {
        "/nix/store/aaa-gcc", "/nix/store/libgcc"
    }
    assert set(split.delta_paths["/nix/store/bbb-clang"]) == {"/nix/store/bbb-clang"}


def test_compute_toolchain_split_deltas_disjoint_from_common() -> None:
    """No delta path should appear in COMMON."""
    closures = {
        "/nix/store/tc1": ["/nix/store/tc1", "/nix/store/shared", "/nix/store/only1"],
        "/nix/store/tc2": ["/nix/store/tc2", "/nix/store/shared", "/nix/store/only2"],
    }
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)
    for delta in split.delta_paths.values():
        assert not (set(delta) & split.common_paths)


def test_compute_toolchain_split_single_toolchain() -> None:
    """With one toolchain, COMMON = its full closure and delta is empty."""
    closures = {"/nix/store/only": ["/nix/store/only", "/nix/store/dep"]}
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)
    assert split.common_paths == frozenset({"/nix/store/only", "/nix/store/dep"})
    assert split.delta_paths["/nix/store/only"] == ()


def test_compute_toolchain_split_raises_on_empty_paths() -> None:
    """Empty out-path list raises RuntimeError without calling nix."""
    with pytest.raises(RuntimeError, match="no toolchain out-paths"):
        compute_toolchain_split([])


def test_compute_toolchain_split_raises_on_requisites_failure() -> None:
    """All paths failing requisites queries raises RuntimeError (no valid closure)."""
    def runner(argv):
        return b"", b"nix daemon down", 1
    with pytest.raises(RuntimeError, match="no toolchain produced a valid closure"):
        compute_toolchain_split(["/nix/store/aaa-gcc"], run_subprocess=runner)


# ---------------------------------------------------------------------------
# export_toolchain_split
# ---------------------------------------------------------------------------


def test_export_toolchain_split_writes_common_and_deltas(
    tmp_path: pathlib.Path,
) -> None:
    """export_toolchain_split writes toolchains.common.archive plus one
    delta archive per toolchain with a non-empty delta set."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            return b"/nix/store/shared\n", b"", 0
        if argv[:2] == ["nix-store", "--export"]:
            key = "-".join(argv[2:])
            return f"NIX_EXPORT:{key}".encode(), b"", 0
        return b"", b"unexpected", 1

    split = ToolchainSplit(
        common_paths=frozenset({"/nix/store/shared"}),
        delta_paths={
            "/nix/store/abc123-gcc": ("/nix/store/abc123-gcc",),
            "/nix/store/def456-clang": ("/nix/store/def456-clang",),
        },
    )
    written = export_toolchain_split(split, tmp_path, run_subprocess=runner)

    # Common archive always written.
    assert TOOLCHAIN_COMMON_ARCHIVE_NAME in written
    assert written[TOOLCHAIN_COMMON_ARCHIVE_NAME].exists()

    # Per-toolchain delta archives.
    assert toolchain_delta_archive_name("/nix/store/abc123-gcc") in written
    assert toolchain_delta_archive_name("/nix/store/def456-clang") in written

    # Verify exact-paths export (no --query --requisites for deltas).
    req_calls = [c for c in calls if "--query" in c and "--requisites" in c]
    export_calls = [c for c in calls if "--export" in c]
    # Common uses export_closure (with requisites); deltas use export_closure_exact.
    # Common: 1 requisites call + 1 export call. Deltas: 1 export call each (no requisites).
    assert len(req_calls) == 1  # only for common
    assert len(export_calls) == 3  # common + 2 deltas


def test_export_toolchain_split_skips_empty_delta(
    tmp_path: pathlib.Path,
) -> None:
    """A toolchain whose delta is empty (its closure is entirely COMMON)
    produces no delta archive."""
    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            return b"/nix/store/shared\n", b"", 0
        if argv[:2] == ["nix-store", "--export"]:
            return b"NIX_EXPORT:common", b"", 0
        return b"", b"", 0

    split = ToolchainSplit(
        common_paths=frozenset({"/nix/store/shared"}),
        delta_paths={"/nix/store/abc123-gcc": ()},  # empty delta
    )
    written = export_toolchain_split(split, tmp_path, run_subprocess=runner)
    # Only the common archive is written (empty delta skipped).
    assert TOOLCHAIN_COMMON_ARCHIVE_NAME in written
    assert toolchain_delta_archive_name("/nix/store/abc123-gcc") not in written


def test_export_toolchain_split_raises_on_common_export_failure(
    tmp_path: pathlib.Path,
) -> None:
    """A failure in the common archive export raises RuntimeError."""
    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            return b"/nix/store/shared\n", b"", 0
        return b"", b"disk full", 1  # export fails

    split = ToolchainSplit(
        common_paths=frozenset({"/nix/store/shared"}),
        delta_paths={},
    )
    with pytest.raises(RuntimeError, match="common archive"):
        export_toolchain_split(split, tmp_path, run_subprocess=runner)


# ---------------------------------------------------------------------------
# compute_toolchain_split — multi-era and property tests
# ---------------------------------------------------------------------------


def test_compute_toolchain_split_multi_era_common_nonempty() -> None:
    """With two toolchain eras that share glibc within each era but not
    across eras, freq>=2 produces a nonempty COMMON from the intra-era
    paths while the cross-era strict intersection would be empty.

    Example: gcc-old and clang-old share glibc-2.26; gcc-new and clang-new
    share glibc-2.42. The strict ALL-toolchains intersection is empty
    (no path is in all 4); freq>=2 captures glibc-2.26 (freq=2) and
    glibc-2.42 (freq=2) as COMMON.
    """
    closures = {
        "/nix/store/aaa-gcc-old": [
            "/nix/store/aaa-gcc-old", "/nix/store/glibc-2.26", "/nix/store/libgcc-old"
        ],
        "/nix/store/bbb-clang-old": [
            "/nix/store/bbb-clang-old", "/nix/store/glibc-2.26"
        ],
        "/nix/store/ccc-gcc-new": [
            "/nix/store/ccc-gcc-new", "/nix/store/glibc-2.42", "/nix/store/libgcc-new"
        ],
        "/nix/store/ddd-clang-new": [
            "/nix/store/ddd-clang-new", "/nix/store/glibc-2.42"
        ],
    }
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)
    # Both era-specific glibcs appear in >=2 closures → both in COMMON.
    assert "/nix/store/glibc-2.26" in split.common_paths
    assert "/nix/store/glibc-2.42" in split.common_paths
    # Toolchain-specific paths appear in only one closure → NOT in COMMON.
    assert "/nix/store/libgcc-old" not in split.common_paths
    assert "/nix/store/libgcc-new" not in split.common_paths


def test_compute_toolchain_split_dedup_union_property() -> None:
    """COMMON + union(deltas) == union(all closures) (no path lost, no path duplicated).

    Total unique paths across closures = |COMMON ∪ Σ delta_i|.
    Each path is in exactly one of: COMMON or exactly one delta.
    """
    closures = {
        "/nix/store/tc1": ["/nix/store/tc1", "/nix/store/shared", "/nix/store/only1"],
        "/nix/store/tc2": ["/nix/store/tc2", "/nix/store/shared", "/nix/store/only2"],
        "/nix/store/tc3": ["/nix/store/tc3", "/nix/store/shared", "/nix/store/only3"],
    }
    runner = _make_split_runner(closures)
    split = compute_toolchain_split(list(closures), run_subprocess=runner)

    union_all: set[str] = set()
    for paths in closures.values():
        union_all.update(paths)

    reconstructed: set[str] = set(split.common_paths)
    for delta in split.delta_paths.values():
        reconstructed.update(delta)

    assert reconstructed == union_all


def test_compute_toolchain_split_skip_bad_path() -> None:
    """An unrealized (non-absolute) or requisites-failing path is skipped
    with a warning, and computation succeeds for the remaining paths."""
    good_closures = {
        "/nix/store/aaa-gcc": ["/nix/store/aaa-gcc", "/nix/store/glibc"],
        "/nix/store/bbb-clang": ["/nix/store/bbb-clang", "/nix/store/glibc"],
    }

    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            key = argv[3]
            if key in good_closures:
                paths = good_closures[key]
                return "\n".join(paths).encode() + b"\n", b"", 0
            # bad path: requisites fails
            return b"", b"path not found", 1
        return b"", b"unexpected", 1

    # Mix one bad path (not in good_closures) with two good ones.
    all_paths = ["/nix/store/bad-unrealized", *good_closures.keys()]
    split = compute_toolchain_split(all_paths, run_subprocess=runner)
    # Bad path skipped; good paths produce a nonempty COMMON.
    assert "/nix/store/glibc" in split.common_paths
    # Good toolchains are in delta_paths; bad one is absent.
    assert "/nix/store/aaa-gcc" in split.delta_paths
    assert "/nix/store/bbb-clang" in split.delta_paths
    assert "/nix/store/bad-unrealized" not in split.delta_paths


# ---------------------------------------------------------------------------
# compute_build_deps_closure
# ---------------------------------------------------------------------------


from compiler_suit_runner.preflight import (  # noqa: E402
    BUILD_DEPS_ARCHIVE_NAME,
    compute_build_deps_closure,
    collect_toolchain_archive_paths,
)


def _make_requisites_runner(
    requisites_map: "dict[tuple, list[str]]",
) -> "callable":
    """Return a subprocess stub that answers ``nix-store --query --requisites``
    based on ``requisites_map`` keyed by a tuple of the seed args."""

    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            seeds = tuple(argv[3:])
            paths = requisites_map.get(seeds, [])
            return ("\n".join(paths) + "\n").encode(), b"", 0
        raise AssertionError(f"unexpected argv: {argv!r}")

    return runner


def test_compute_build_deps_closure_empty_input() -> None:
    """Empty input_outpaths → ([], []) with no subprocess calls."""
    calls: list = []

    def runner(argv):
        calls.append(argv)
        return b"", b"", 0

    result = compute_build_deps_closure([], run_subprocess=runner)
    assert result == ([], [])
    assert calls == []


def test_compute_build_deps_closure_subtracts_toolchain_paths() -> None:
    """Toolchain paths in toolchain_paths_to_subtract are removed from the
    ordered output, preserving topological order for the remaining paths."""
    seed = "/nix/store/out-hello"
    tc_path = "/nix/store/tc-gcc"
    dep_path = "/nix/store/dep-glibc"
    all_req = [dep_path, tc_path, seed]  # topological order

    runner = _make_requisites_runner({(seed,): all_req})
    paths, uncovered = compute_build_deps_closure(
        [seed],
        toolchain_paths_to_subtract=frozenset([tc_path]),
        run_subprocess=runner,
    )
    assert tc_path not in paths
    assert dep_path in paths
    assert seed in paths
    assert uncovered == []
    # Topological order preserved (dep before seed).
    assert paths.index(dep_path) < paths.index(seed)


def test_compute_build_deps_closure_returns_uncovered_for_missing_seed() -> None:
    """A seed outpath not appearing in the requisites output is returned in
    uncovered (completeness gate: it's not locally realised)."""
    good_seed = "/nix/store/good-realised"
    bad_seed = "/nix/store/bad-unrealised"

    runner = _make_requisites_runner({(good_seed, bad_seed): [good_seed]})
    paths, uncovered = compute_build_deps_closure(
        [good_seed, bad_seed],
        run_subprocess=runner,
    )
    assert good_seed in paths
    assert bad_seed not in paths
    assert uncovered == [bad_seed]


def test_compute_build_deps_closure_nonzero_rc_raises() -> None:
    """A non-zero return code from nix-store --query --requisites raises RuntimeError."""

    def runner(argv):
        return b"", b"nix-store: path not found\n", 1

    with pytest.raises(RuntimeError, match="nix-store --query --requisites"):
        compute_build_deps_closure(
            ["/nix/store/any-path"],
            run_subprocess=runner,
        )


def test_compute_build_deps_closure_no_subtract_returns_all() -> None:
    """Without toolchain subtraction, all requisites paths are returned."""
    seed = "/nix/store/out-hello"
    paths_in_store = ["/nix/store/dep-a", "/nix/store/dep-b", seed]

    runner = _make_requisites_runner({(seed,): paths_in_store})
    paths, uncovered = compute_build_deps_closure(
        [seed],
        toolchain_paths_to_subtract=None,
        run_subprocess=runner,
    )
    assert paths == paths_in_store
    assert uncovered == []


# ---------------------------------------------------------------------------
# collect_toolchain_archive_paths
# ---------------------------------------------------------------------------


def test_collect_toolchain_archive_paths_union_of_closures() -> None:
    """Returns the union of requisites for all toolchain out-paths."""
    tc1 = "/nix/store/tc1-gcc"
    tc2 = "/nix/store/tc2-clang"
    closures = {
        tc1: [tc1, "/nix/store/shared-glibc", "/nix/store/only-gcc"],
        tc2: [tc2, "/nix/store/shared-glibc", "/nix/store/only-clang"],
    }

    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            seed = argv[3]
            return ("\n".join(closures.get(seed, [])) + "\n").encode(), b"", 0
        raise AssertionError(f"unexpected argv: {argv!r}")

    result = collect_toolchain_archive_paths([tc1, tc2], run_subprocess=runner)
    assert isinstance(result, frozenset)
    assert tc1 in result
    assert tc2 in result
    assert "/nix/store/shared-glibc" in result
    assert "/nix/store/only-gcc" in result
    assert "/nix/store/only-clang" in result


def test_collect_toolchain_archive_paths_skips_failed_outpath() -> None:
    """A requisites failure for one toolchain is skipped; the rest still contribute."""
    tc_good = "/nix/store/tc-good-gcc"
    tc_bad = "/nix/store/tc-bad-clang"

    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--requisites"]:
            seed = argv[3]
            if seed == tc_good:
                return (tc_good + "\n/nix/store/glibc\n").encode(), b"", 0
            return b"", b"not in store\n", 1
        raise AssertionError(f"unexpected argv: {argv!r}")

    result = collect_toolchain_archive_paths([tc_good, tc_bad], run_subprocess=runner)
    assert tc_good in result
    assert "/nix/store/glibc" in result
    assert tc_bad not in result


def test_collect_toolchain_archive_paths_empty_list() -> None:
    """Empty outpaths list returns an empty frozenset."""
    calls: list = []

    def runner(argv):
        calls.append(argv)
        return b"", b"", 0

    result = collect_toolchain_archive_paths([], run_subprocess=runner)
    assert result == frozenset()
    assert calls == []


def test_collect_toolchain_archive_paths_skips_non_absolute() -> None:
    """Non-absolute paths (e.g. empty string) are skipped silently."""
    calls: list = []

    def runner(argv):
        calls.append(argv)
        return b"", b"", 0

    result = collect_toolchain_archive_paths(["", "relative/path"], run_subprocess=runner)
    assert result == frozenset()
    assert calls == []


# ---------------------------------------------------------------------------
# export_build_deps_archive
# ---------------------------------------------------------------------------


def test_export_build_deps_archive_writes_file(monkeypatch, tmp_path) -> None:
    """export_build_deps_archive delegates to export_closure_exact and
    returns the archive path under BUILD_DEPS_ARCHIVE_NAME."""
    from compiler_suit_runner.preflight import export_build_deps_archive  # noqa: PLC0415

    paths = ["/nix/store/dep-a", "/nix/store/dep-b"]
    exported: list = []

    def _fake_export_closure(archive_path, export_paths, *, run_subprocess=None):
        exported.append((archive_path, list(export_paths)))
        # Simulate writing the archive file.
        archive_path.write_bytes(b"NIX_EXPORT:fake")
        return True, b""

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_compilers_worker.export_closure_exact",
        _fake_export_closure,
    )

    result = export_build_deps_archive(paths, tmp_path)
    assert result == tmp_path / BUILD_DEPS_ARCHIVE_NAME
    assert len(exported) == 1
    assert exported[0][0] == tmp_path / BUILD_DEPS_ARCHIVE_NAME
    assert exported[0][1] == paths


def test_export_build_deps_archive_raises_on_empty_paths(tmp_path) -> None:
    """export_build_deps_archive raises RuntimeError if paths list is empty."""
    from compiler_suit_runner.preflight import export_build_deps_archive  # noqa: PLC0415

    with pytest.raises(RuntimeError, match="no paths to export"):
        export_build_deps_archive([], tmp_path)


def test_export_build_deps_archive_raises_on_export_failure(monkeypatch, tmp_path) -> None:
    """export_build_deps_archive raises RuntimeError when export_closure_exact
    returns ok=False."""
    from compiler_suit_runner.preflight import export_build_deps_archive  # noqa: PLC0415

    monkeypatch.setattr(
        "compiler_suit_runner.workers.build_compilers_worker.export_closure_exact",
        lambda *a, **kw: (False, b"export failed: no space left\n"),
    )
    with pytest.raises(RuntimeError, match="nix-store --export failed"):
        export_build_deps_archive(["/nix/store/dep-a"], tmp_path)



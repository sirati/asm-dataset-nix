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
    build_toolchains_locally,
    check_toolchains_locally,
    enumerate_toolchains,
    enumerate_toolchains_only,
    enumerate_variants,
    filter_existing_variants,
    path_info_batch,
    preflight,
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
        #     — pkg → [arch] index for the scoped-meta path
        if apply_expr and isinstance(payload, dict):
            if "mapAttrs" in apply_expr and "attrNames" in apply_expr:
                projected = {
                    pkg: list(arches.keys()) if isinstance(arches, dict) else []
                    for pkg, arches in payload.items()
                }
                return json.dumps(projected).encode("utf-8"), b"", 0
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
    variants, drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    # 2 (hello/x86_64) + 1 (hello/aarch64) + 1 (busybox/x86_64) = 4
    assert len(variants) == 4
    labels = {v["label"] for v in variants}
    assert "hello-x86_64-gcc15-O0-baseline-unhardened" in labels
    assert "busybox-x86_64-gcc15-O2-baseline-unhardened" in labels

    # Each variant has the expected fields.
    for v in variants:
        assert v["pkg"]
        assert v["arch"]
        assert v["compiler_id"] == "gcc15"
        assert v["drv"].startswith("/nix/store/")
        assert v["variant_dir"] and not v["variant_dir"].endswith(".tar.zst")
        assert isinstance(v["tier"], int)

    # toolchain_drvs covers all distinct drv paths.
    assert drvs == frozenset({
        "/nix/store/aaa-hello-x86-O0.drv",
        "/nix/store/bbb-hello-x86-O2.drv",
        "/nix/store/ccc-hello-aarch.drv",
        "/nix/store/ddd-busybox-x86.drv",
    })


def test_enumerate_variants_filter_by_packages():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["hello"],
        run_subprocess=runner,
    )
    assert variants  # not empty
    assert all(v["pkg"] == "hello" for v in variants)
    assert {v["pkg"] for v in variants} == {"hello"}


def test_enumerate_variants_filter_by_archs():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        archs=["aarch64"],
        run_subprocess=runner,
    )
    assert variants  # at least one (hello/aarch64)
    assert all(v["arch"] == "aarch64" for v in variants)


def test_enumerate_variants_combined_filter():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["busybox"],
        archs=["x86_64"],
        run_subprocess=runner,
    )
    assert len(variants) == 1
    assert variants[0]["pkg"] == "busybox"
    assert variants[0]["arch"] == "x86_64"


def test_enumerate_variants_filter_no_match_returns_empty():
    runner, _ = _make_run_subprocess(_all_responses())
    variants, drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["nonexistent-pkg"],
        run_subprocess=runner,
    )
    assert variants == ()
    assert drvs == frozenset()


def test_enumerate_variants_tier_assignment():
    """``hello`` (tier 1), ``busybox`` (tier 1) — verify tier mapping."""
    runner, _ = _make_run_subprocess(_all_responses())
    variants, _ = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    by_pkg = {v["pkg"]: v["tier"] for v in variants}
    assert by_pkg["hello"] == 1
    assert by_pkg["busybox"] == 1


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


def test_preflight_composes_both_calls():
    runner, calls = _make_run_subprocess(_all_responses())
    result = preflight(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    assert isinstance(result, PreflightResult)
    assert result.sys_name == "x86_64-linux"
    assert len(result.variants) == 4
    assert len(result.toolchain_specs) == 3
    # common_dep_drvs is left empty until phase 1b populates it.
    assert result.common_dep_drvs == ()
    assert isinstance(result.toolchain_drvs, frozenset)
    assert len(result.toolchain_drvs) == 4

    # Three nix-eval flavours — meta, drvPaths, toolchains. Find the
    # ``flake#attr`` token in each argv (with --apply present, the
    # attr is no longer at argv[-1]; the lambda string is).
    eval_attrs = set()
    for argv in calls:
        target = next((tok for tok in argv if "#" in tok), None)
        if target is not None:
            eval_attrs.add(target.split("#", 1)[1])
    assert "_meta.x86_64-linux" in eval_attrs
    assert any(a.startswith("_drvPaths.x86_64-linux") for a in eval_attrs)
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
    assert all(v["pkg"] == "hello" and v["arch"] == "x86_64" for v in result.variants)
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


def test_enumerate_variants_sample_caps_per_group():
    runner, _ = _make_run_subprocess(_wide_responses())
    variants, _drvs = enumerate_variants(
        ".",
        "x86_64-linux",
        sample_size=2,
        sample_seed="alpha",
        run_subprocess=runner,
    )
    # 2 opts × 2 sample = 4 variants total (one (compiler, arch, opt) group per opt).
    assert len(variants) == 4
    # Two per opt group.
    by_opt: dict[str, list] = {}
    for v in variants:
        opt = v["label"].split("-")[3]  # hello-x86_64-gcc15-<opt>-...
        by_opt.setdefault(opt, []).append(v)
    assert {"O0", "O2"} == set(by_opt)
    assert all(len(group) == 2 for group in by_opt.values())


def test_enumerate_variants_sample_zero_returns_full():
    runner, _ = _make_run_subprocess(_wide_responses())
    variants, _ = enumerate_variants(
        ".",
        "x86_64-linux",
        sample_size=0,
        run_subprocess=runner,
    )
    # 6 (flag, hardening) × 2 opts = 12 variants.
    assert len(variants) == 12


def test_enumerate_variants_sample_seed_deterministic():
    """Same seed → identical variant set; different seed → different set."""
    runner1, _ = _make_run_subprocess(_wide_responses())
    runner2, _ = _make_run_subprocess(_wide_responses())
    runner3, _ = _make_run_subprocess(_wide_responses())
    a, _ = enumerate_variants(
        ".", "x86_64-linux",
        sample_size=2, sample_seed="alpha",
        run_subprocess=runner1,
    )
    b, _ = enumerate_variants(
        ".", "x86_64-linux",
        sample_size=2, sample_seed="alpha",
        run_subprocess=runner2,
    )
    c, _ = enumerate_variants(
        ".", "x86_64-linux",
        sample_size=2, sample_seed="beta",
        run_subprocess=runner3,
    )
    labels_a = {v["label"] for v in a}
    labels_b = {v["label"] for v in b}
    labels_c = {v["label"] for v in c}
    assert labels_a == labels_b
    assert labels_a != labels_c


def test_enumerate_variants_sample_larger_than_group_keeps_all():
    """When sample_size exceeds group size, no variants are dropped."""
    runner, _ = _make_run_subprocess(_wide_responses())
    variants, _ = enumerate_variants(
        ".", "x86_64-linux",
        sample_size=999,
        run_subprocess=runner,
    )
    assert len(variants) == 12


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
    """``nix path-info <drv>^*`` returns 0 for realised, non-zero for
    missing. The helper aggregates and returns the failing subset."""
    realised = "/nix/store/aaa.drv"
    missing = "/nix/store/bbb.drv"
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        drv_arg = argv[-1]
        # ``<drv>^*`` expands to every output; the helper feeds the
        # full ``^*`` suffix.
        assert drv_arg.endswith("^*"), drv_arg
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




def test_enumerate_variants_defer_to_phase0_returns_metadata_only():
    """``defer_to_phase0=True`` returns per-binary metadata without
    forcing drv instantiation."""
    runner, calls = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        sample_size=3,
        sample_seed="alpha",
        defer_to_phase0=True,
        run_subprocess=runner,
    )
    # Return shape: dict[pkg, metadata_dict].
    assert isinstance(result, dict)
    assert set(result.keys()) == {"hello", "busybox"}

    hello = result["hello"]
    assert sorted(hello["archs"]) == ["aarch64", "x86_64"]
    assert hello["sample_size"] == 3
    assert hello["sample_seed"] == "alpha"
    assert hello["tier"] == 1
    # Suffixes per arch listed; NOT yet sampled (worker re-samples
    # deterministically with the same seed).
    assert hello["suffixes_by_arch"]["x86_64"] == [
        "gcc15-O0-baseline-unhardened",
        "gcc15-O2-baseline-unhardened",
    ]
    assert hello["suffixes_by_arch"]["aarch64"] == [
        "gcc15-O2-baseline-unhardened",
    ]

    busybox = result["busybox"]
    assert busybox["archs"] == ["x86_64"]
    assert busybox["suffixes_by_arch"]["x86_64"] == [
        "gcc15-O2-baseline-unhardened",
    ]

    # Regression guard: the slow drv-instantiation path is never hit.
    # No call should target ``_drvPaths.<sys>`` (the cached full-matrix
    # eval) and no nix-eval-jobs subprocess should be spawned.
    for argv in calls:
        if argv and pathlib.Path(argv[0]).name == "nix-eval-jobs":
            raise AssertionError(
                f"nix-eval-jobs spawned in deferred mode: {argv}"
            )
        for tok in argv:
            if "#_drvPaths." in tok:
                raise AssertionError(
                    f"_drvPaths eval triggered in deferred mode: {tok}"
                )


def test_enumerate_variants_defer_to_phase0_honours_filters():
    """``packages`` / ``archs`` filters still apply in deferred mode."""
    runner, _ = _make_run_subprocess(_all_responses())
    result = enumerate_variants(
        ".",
        "x86_64-linux",
        packages=["hello"],
        archs=["x86_64"],
        defer_to_phase0=True,
        run_subprocess=runner,
    )
    assert set(result.keys()) == {"hello"}
    assert result["hello"]["archs"] == ["x86_64"]
    assert "aarch64" not in result["hello"]["suffixes_by_arch"]


def test_enumerate_variants_legacy_mode_unchanged():
    """Regression: ``defer_to_phase0=False`` (default) returns the
    legacy ``(variants, toolchain_drvs)`` shape."""
    runner, _ = _make_run_subprocess(_all_responses())
    legacy = enumerate_variants(
        ".",
        "x86_64-linux",
        run_subprocess=runner,
    )
    # Tuple of (variants, drv_set) — not a dict.
    assert isinstance(legacy, tuple)
    assert len(legacy) == 2
    variants, drv_set = legacy
    assert len(variants) == 4
    assert isinstance(drv_set, frozenset)
    assert len(drv_set) == 4


# ---------------------------------------------------------------------------
# enumerate_toolchains_only
# ---------------------------------------------------------------------------


def test_enumerate_toolchains_only_returns_pairs_and_drv_paths():
    """``enumerate_toolchains_only`` is the submitter-side toolchain
    bootstrap surface; it returns ``(pairs, drv_paths)`` and never
    touches variants."""
    responses = _all_responses()
    # Stub drv resolution for every toolchain pair the fixture exposes.
    for arch, comp in (("x86_64", "gcc15"), ("x86_64", "clang20"),
                       ("aarch64", "gcc15")):
        responses[
            f"_crossToolchainMap.x86_64-linux.{arch}.{comp}.drvPath"
        ] = b"/nix/store/" + f"{arch}-{comp}.drv".encode()

    runner, calls = _make_run_subprocess(responses)
    pairs, drv_paths = enumerate_toolchains_only(
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
    # No variant-side eval — we never look at _meta or _drvPaths.
    for argv in calls:
        for tok in argv:
            if "#_meta." in tok or "#_drvPaths." in tok:
                raise AssertionError(
                    f"toolchains-only path touched variant attrs: {tok}"
                )


def test_enumerate_toolchains_only_empty_pairs_skip_drv_eval():
    """If no toolchain pairs survive filtering, drv eval is skipped."""
    runner, calls = _make_run_subprocess(_all_responses())
    pairs, drv_paths = enumerate_toolchains_only(
        ".",
        "x86_64-linux",
        archs=["nonexistent"],
        run_subprocess=runner,
    )
    assert pairs == ()
    assert drv_paths == {}
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



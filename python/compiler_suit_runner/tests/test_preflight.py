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
    PreflightResult,
    enumerate_toolchains,
    enumerate_variants,
    filter_existing_variants,
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



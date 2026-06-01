"""Unit tests for ``compiler_suit_runner.manifest_gen``."""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from compiler_suit_runner.manifest_gen import (
    ManifestHeader,
    ManifestSet,
    emit_all_manifests,
    make_build_common_dep_header,
    make_build_compilers_header,
    make_build_variant_header,
    make_matrix_eval_header,
    make_toolchain_validate_header,
    read_manifest,
    write_manifest,
)
from compiler_suit_runner.partition import VariantSpec


# ---------------------------------------------------------------------------
# Helpers


def _variant(
    pkg: str,
    arch: str,
    suffix: str,
    *,
    compiler_id: str = "gcc15",
    tier: int = 2,
) -> VariantSpec:
    label = f"{pkg}-{arch}-{compiler_id}-{suffix}"
    return {
        "label": label,
        "drv": f"/nix/store/{label}.drv",
        "variant_dir": label,
        "metadata_name": f"{label}.json",
        "compiler_id": compiler_id,
        "compiler_family": "gcc",
        "compiler_version": "15.2.0",
        "optimization": "O2",
        "flag_set": "baseline",
        "hardening": "default",
        "sanitizer": "san-off",
        "march": "march-default",
        "tier": tier,
        "pkg": pkg,
        "arch": arch,
    }


# ---------------------------------------------------------------------------
# Header constructors


def test_build_compilers_header_without_drv():
    h = make_build_compilers_header(
        "x86_64-linux", "aarch64", "gcc14"
    )
    assert h.item_class == "build_compilers"
    assert h.name == "build_compilers__aarch64__gcc14"
    assert h.payload == {
        "sys": "x86_64-linux",
        "arch": "aarch64",
        "compiler_label": "gcc14",
        "attr": "_crossToolchainMap.x86_64-linux.aarch64.gcc14",
    }
    assert "drv" not in h.payload
    assert h.size == 0


def test_build_compilers_header_with_drv():
    h = make_build_compilers_header(
        "x86_64-linux", "armv7l", "gcc11", drv="/nix/store/tc.drv"
    )
    assert h.payload["drv"] == "/nix/store/tc.drv"
    assert h.payload["attr"] == (
        "_crossToolchainMap.x86_64-linux.armv7l.gcc11"
    )


def test_build_common_dep_header():
    h = make_build_common_dep_header("/nix/store/glibc-x.drv", "glibc")
    assert h.item_class == "build_common_dep"
    assert h.name == "common_dep__glibc"
    assert h.payload == {
        "drv": "/nix/store/glibc-x.drv",
        "label": "glibc",
        "attr": "/nix/store/glibc-x.drv",
    }
    assert h.size == 0


def test_build_variant_header_payload():
    v = _variant("hello", "x86_64", "O2", tier=1)
    h = make_build_variant_header(v, "x86_64-linux")
    assert h.item_class == "build_variant"
    assert h.name == v["label"]
    assert h.size == 0
    assert h.payload["sys"] == "x86_64-linux"
    assert h.payload["pkg"] == "hello"
    assert h.payload["arch"] == "x86_64"
    assert h.payload["label"] == v["label"]
    assert h.payload["drv"] == v["drv"]
    assert h.payload["variant_dir"] == v["variant_dir"]
    assert h.payload["compiler_id"] == v["compiler_id"]
    assert h.payload["tier"] == 1
    assert h.payload["attr"] == (
        f"dataset.x86_64-linux.hello.x86_64.{v['label']}"
    )
    # No K=3 affinity hint by default.
    assert "preferred_secondaries" not in h.payload


def test_build_variant_header_preferred_secondaries_emitted_sorted():
    """When ``preferred_secondaries`` is non-empty, it is emitted in
    the payload sorted (deterministic manifest diffs)."""
    v = _variant("hello", "x86_64", "O2")
    h = make_build_variant_header(
        v, "x86_64-linux",
        preferred_secondaries=["sec-b", "sec-a", "sec-c"],
    )
    assert h.payload["preferred_secondaries"] == ["sec-a", "sec-b", "sec-c"]


def test_build_variant_header_preferred_secondaries_omitted_when_empty():
    """Empty / None preferred_secondaries -> no payload key (keeps
    legacy manifests byte-identical)."""
    v = _variant("hello", "x86_64", "O2")
    for empty in (None, []):
        h = make_build_variant_header(
            v, "x86_64-linux", preferred_secondaries=empty,
        )
        assert "preferred_secondaries" not in h.payload


def test_emit_all_manifests_threads_toolchain_placements_to_variants(
    tmp_path: pathlib.Path,
):
    """When ``toolchain_outpath_placements`` is provided to
    ``emit_all_manifests``, the per-variant header's
    ``preferred_secondaries`` is looked up via:
        variant.arch + variant.compiler_id
            -> toolchain_drvs[(arch, compiler_id)]
            -> drv_outpaths[drv]
            -> toolchain_outpath_placements[outpath]
    """
    v = _variant("hello", "x86_64", "O2", compiler_id="gcc15")
    tc_drv = "/nix/store/abc-toolchain-x86_64-gcc15.drv"
    tc_outpath = "/nix/store/abc-toolchain-x86_64-gcc15"
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[v],
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[],
        toolchain_drvs={("x86_64", "gcc15"): tc_drv},
        drv_outpaths={tc_drv: tc_outpath},
        toolchain_outpath_placements={tc_outpath: ["sec-1", "sec-2"]},
    )
    variant_headers = [
        h for h in result.headers if h.item_class == "build_variant"
    ]
    assert len(variant_headers) == 1
    assert variant_headers[0].payload["preferred_secondaries"] == [
        "sec-1", "sec-2",
    ]


def test_emit_all_manifests_omits_preferred_secondaries_when_no_placement(
    tmp_path: pathlib.Path,
):
    """Default (no placement map provided) -> no preferred_secondaries
    field. Backward compat for legacy callers."""
    v = _variant("hello", "x86_64", "O2")
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[v],
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[],
    )
    variant_headers = [
        h for h in result.headers if h.item_class == "build_variant"
    ]
    assert len(variant_headers) == 1
    assert "preferred_secondaries" not in variant_headers[0].payload


# ---------------------------------------------------------------------------
# write_manifest / read_manifest


def test_write_manifest_writes_compact_json_file(
    tmp_path: pathlib.Path,
):
    h = make_build_common_dep_header("/nix/store/glibc.drv", "glibc")
    written = write_manifest(tmp_path, h)
    assert written == tmp_path / "common_dep__glibc.json"
    # The on-disk file is the JSON document only — no longer padded
    # to ``h.size`` (see the dispatch hot-path note in
    # manifest_gen.write_manifest).
    stat = os.stat(written)
    assert 0 < stat.st_size <= 4096

    # Round-trip via read_manifest.
    loaded = read_manifest(written)
    assert loaded == h


def test_write_manifest_creates_target_dir(tmp_path: pathlib.Path):
    nested = tmp_path / "a" / "b" / "c"
    h = make_build_common_dep_header("/nix/store/glibc.drv", "glibc")
    written = write_manifest(nested, h)
    assert written.parent == nested
    assert nested.exists()


def test_write_manifest_no_tmp_leftovers(tmp_path: pathlib.Path):
    h = make_build_common_dep_header("/nix/store/zlib.drv", "zlib")
    write_manifest(tmp_path, h)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_write_manifest_payload_round_trip(tmp_path: pathlib.Path):
    v = _variant("sqlite", "x86_64", "O2", tier=2)
    h = make_build_variant_header(v, "x86_64-linux")
    write_manifest(tmp_path, h)
    loaded = read_manifest(tmp_path / f"{v['label']}.json")
    assert loaded.payload == h.payload
    assert loaded.size == h.size
    assert loaded.item_class == "build_variant"


def test_read_manifest_handles_legacy_sparse_padded_file(
    tmp_path: pathlib.Path,
):
    # Older copies of write_manifest padded the file with ftruncate
    # (sparse zero-fill) to a multi-GiB tail. New write_manifest
    # doesn't, but read_manifest must still load legacy files —
    # synthesise the layout and confirm parse.
    h = make_build_common_dep_header("/nix/store/glibc.drv", "glibc")
    target = write_manifest(tmp_path, h)
    legacy_size = 2 * 1024 * 1024 * 1024  # 2 GiB sparse tail
    fd = os.open(target, os.O_RDWR)
    try:
        os.ftruncate(fd, legacy_size)
    finally:
        os.close(fd)
    assert os.stat(target).st_size == legacy_size
    loaded = read_manifest(target)
    assert loaded == h


def test_read_manifest_rejects_non_object_top_level(
    tmp_path: pathlib.Path,
):
    target = tmp_path / "junk.json"
    target.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        read_manifest(target)


def test_read_manifest_rejects_missing_field(tmp_path: pathlib.Path):
    target = tmp_path / "bad.json"
    target.write_text(
        json.dumps(
            {
                "item_class": "build_common_dep",
                "name": "x",
                "size": 100,
                # payload missing
            }
        )
    )
    # Pad so apparent size matches size field.
    fd = os.open(target, os.O_RDWR)
    try:
        os.ftruncate(fd, 100)
    finally:
        os.close(fd)
    with pytest.raises(ValueError):
        read_manifest(target)


# ---------------------------------------------------------------------------
# emit_all_manifests


def _build_full_input():
    """2 archs × 2 packages × 3 variants = 12 variants in 4 shards."""
    variants: list[VariantSpec] = []
    for pkg in ("hello", "sqlite"):
        for arch in ("x86_64", "aarch64"):
            for suf in ("O0", "O2", "O3"):
                tier = 1 if pkg == "hello" else 2
                variants.append(
                    _variant(pkg, arch, suf, tier=tier)
                )
    toolchain_specs = [
        ("x86_64", "gcc14"),
        ("x86_64", "gcc15"),
        ("aarch64", "gcc14"),
        ("aarch64", "gcc15"),
    ]
    common_deps = [
        ("/nix/store/glibc.drv", "glibc"),
        ("/nix/store/zlib.drv", "zlib"),
    ]
    return variants, toolchain_specs, common_deps


def test_emit_all_manifests_full_shape(tmp_path: pathlib.Path):
    variants, toolchain_specs, common_deps = _build_full_input()

    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
    )
    assert isinstance(result, ManifestSet)
    assert result.target_dir == tmp_path

    grouped = result.by_class
    # matrix_eval / dependency_graph are JSON-free (built in-memory by
    # discover_items), so they never appear in a ManifestSet; toolchain
    # falls back to the build_compilers class because no resolved drvs
    # were supplied.
    assert "matrix_eval" not in grouped
    assert "dependency_graph" not in grouped
    assert len(grouped["build_compilers"]) == 4
    assert len(grouped["build_common_dep"]) == 2
    assert len(grouped["build_variant"]) == 12

    # Every header has a corresponding file; on-disk size is just the
    # JSON document (the framework's pre-flight hashing pass reads
    # every byte of every TaskInfo path, so manifests are no longer
    # padded to ``header.size`` — that's still propagated via the
    # JSON ``size`` field for memory-budget ordering).
    for header in result.headers:
        path = tmp_path / f"{header.name}.json"
        assert path.exists()
        assert 0 < os.stat(path).st_size <= 4096
        # JSON content round-trips.
        loaded = read_manifest(path)
        assert loaded == header


def test_emit_all_manifests_iteration_order(tmp_path: pathlib.Path):
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
    )
    classes = [h.item_class for h in result.headers]
    expected_order = [
        "build_compilers",
        "build_common_dep",
        "build_variant",
    ]
    seen_order: list[str] = []
    for c in classes:
        if not seen_order or seen_order[-1] != c:
            seen_order.append(c)
    assert seen_order == expected_order


def test_emit_all_manifests_empty_inputs(tmp_path: pathlib.Path):
    """A degenerate case: no variants, no toolchains, no deps."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    grouped = result.by_class
    assert "matrix_eval" not in grouped
    assert grouped["build_compilers"] == ()
    assert grouped["toolchain_validate"] == ()
    assert grouped["build_common_dep"] == ()
    assert grouped["build_variant"] == ()


def test_manifest_set_by_class_includes_all_known_classes(
    tmp_path: pathlib.Path,
):
    """``by_class`` returns a tuple for every JSON-backed ItemClass even
    when no items in that class are present, so iteration is
    KeyError-free. matrix_eval / dependency_graph are JSON-free and so
    are deliberately absent from this taxonomy."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    grouped = result.by_class
    expected_keys = {
        "build_compilers",
        "toolchain_validate",
        "build_common_dep",
        "build_variant",
    }
    assert set(grouped.keys()) == expected_keys
    for value in grouped.values():
        assert isinstance(value, tuple)


def test_emit_all_manifests_target_dir_created(tmp_path: pathlib.Path):
    target = tmp_path / "deeply" / "nested" / "manifests"
    assert not target.exists()
    emit_all_manifests(
        target_dir=target,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[],
        common_deps=[],
    )
    assert target.is_dir()


# ---------------------------------------------------------------------------
# Validate-vs-build switch (``--build-compilers`` plumbing)
# ---------------------------------------------------------------------------


def test_toolchain_validate_header_payload():
    h = make_toolchain_validate_header(
        "x86_64-linux", "aarch64", "gcc14",
        drv="/nix/store/tc.drv",
        outpath="/nix/store/tc-out",
    )
    assert h.item_class == "toolchain_validate"
    assert h.name == "toolchain_validate__aarch64__gcc14"
    assert h.payload["drv"] == "/nix/store/tc.drv"
    assert h.payload["outpath"] == "/nix/store/tc-out"
    # The validate_only flag is what discriminates this from the
    # build header — workers branch on item_class but the flag keeps
    # a marker available in the on-wire payload for diagnostics.
    assert h.payload["validate_only"] is True


def test_toolchain_validate_header_omits_outpath_when_unknown():
    h = make_toolchain_validate_header(
        "x86_64-linux", "armv7l", "gcc11", drv="/nix/store/tc.drv",
    )
    assert "outpath" not in h.payload


def test_emit_all_manifests_default_emits_validate_class(
    tmp_path: pathlib.Path,
):
    """With ``allow_toolchain_build`` off (default) and a resolved drv
    + outpath available, the toolchain slot must emit
    ``toolchain_validate`` — that's the no-build-on-secondaries
    contract."""
    toolchain_specs = [("x86_64", "gcc15")]
    tc_drv = "/nix/store/tc15.drv"
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=toolchain_specs,
        common_deps=[],
        toolchain_drvs={("x86_64", "gcc15"): tc_drv},
        drv_outpaths={tc_drv: "/nix/store/tc15-out"},
    )
    grouped = result.by_class
    assert len(grouped["build_compilers"]) == 0
    assert len(grouped["toolchain_validate"]) == 1
    header = grouped["toolchain_validate"][0]
    assert header.payload["drv"] == tc_drv
    assert header.payload["outpath"] == "/nix/store/tc15-out"
    assert (tmp_path / f"{header.name}.json").exists()


def test_emit_all_manifests_opt_in_emits_build_class(
    tmp_path: pathlib.Path,
):
    """With ``allow_toolchain_build=True`` the operator has explicitly
    opted into secondaries building toolchains locally — emit the
    ``build_compilers`` class so the build worker dispatches the
    nix-build path. When the legacy ``stages=None`` default is used
    the ``toolchain_validate`` stage is also active, so a drv-resolved
    spec also emits a validate header alongside (see
    `--build-compilers --debug-testbuild` operator flow)."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[],
        toolchain_drvs={("x86_64", "gcc15"): "/nix/store/tc.drv"},
        allow_toolchain_build=True,
    )
    grouped = result.by_class
    assert len(grouped["build_compilers"]) == 1
    assert len(grouped["toolchain_validate"]) == 1


def test_emit_all_manifests_falls_back_to_build_when_drv_missing(
    tmp_path: pathlib.Path,
):
    """If ``allow_toolchain_build`` is off but the primary couldn't
    resolve a drv (eval failed), the validate-only path is unsafe
    (no outpath → no fetch target) and we fall back to the build
    header. The CLI's pre-dispatch check is the loud signal — this
    is the on-wire safety net."""
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=[],
        toolchain_specs=[("x86_64", "gcc15")],
        common_deps=[],
        # No toolchain_drvs at all → drv lookup returns None.
    )
    grouped = result.by_class
    assert len(grouped["build_compilers"]) == 1
    assert len(grouped["toolchain_validate"]) == 0


def test_build_variant_header_embeds_input_drvs_and_outpaths():
    """``make_build_variant_header`` carries the placement-map plumbing
    for the secondary's pre-fetch loop: input_drvs (sorted) +
    input_outpaths (per-drv mapping)."""
    v = _variant("hello", "x86_64", "O2")
    h = make_build_variant_header(
        v, "x86_64-linux",
        input_drvs=frozenset({
            "/nix/store/d1.drv",
            "/nix/store/d2.drv",
            "/nix/store/d3.drv",
        }),
        drv_outpaths={
            "/nix/store/d1.drv": "/nix/store/d1-out",
            "/nix/store/d2.drv": "/nix/store/d2-out",
            # d3 deliberately absent — must be filtered out.
        },
    )
    # The list is sorted (deterministic dispatch ordering).
    assert h.payload["input_drvs"] == [
        "/nix/store/d1.drv",
        "/nix/store/d2.drv",
    ]
    assert h.payload["input_outpaths"] == {
        "/nix/store/d1.drv": "/nix/store/d1-out",
        "/nix/store/d2.drv": "/nix/store/d2-out",
    }


def test_build_variant_header_without_placement_kwargs_omits_fields():
    """When no input_drvs / drv_outpaths are passed (single-process
    flows, cached-preflight restore), the variant payload stays at
    the legacy shape so older workers still parse it cleanly."""
    v = _variant("hello", "x86_64", "O2")
    h = make_build_variant_header(v, "x86_64-linux")
    assert "input_drvs" not in h.payload
    assert "input_outpaths" not in h.payload


# ---------------------------------------------------------------------------
# matrix_eval (distributed-eval) manifests
# ---------------------------------------------------------------------------


_DEFAULT_TC_AGGREGATE_DRV = "/nix/store/aaaa-toolchains.drv"


def _matrix_eval_metadata() -> dict[str, dict]:
    return {
        "hello": {
            "archs": ["x86_64", "aarch64"],
            "variant_sample": 64,
            "variant_seed": "seed-hello",
            "tier": 1,
            "toolchain_aggregate_drv": _DEFAULT_TC_AGGREGATE_DRV,
        },
        "busybox": {
            "archs": ["x86_64"],
            "variant_sample": 32,
            "variant_seed": "seed-busybox",
            "tier": 1,
            "toolchain_aggregate_drv": _DEFAULT_TC_AGGREGATE_DRV,
        },
    }


def test_matrix_eval_header_payload_shape():
    h = make_matrix_eval_header(
        "hello",
        "x86_64-linux",
        archs=["x86_64", "aarch64"],
        toolchain_aggregate_drv=_DEFAULT_TC_AGGREGATE_DRV,
        variant_sample=64,
        variant_seed="abc123",
    )
    assert h.item_class == "matrix_eval"
    assert h.name == "matrix_eval__hello"
    assert h.size == 0
    assert h.task_id == "matrix_eval__hello"
    # Empty depends_on for now — build_compilers wiring is a TODO.
    assert h.task_depends_on == ()
    assert h.payload["binary"] == "hello"
    assert h.payload["sys"] == "x86_64-linux"
    assert h.payload["archs"] == ["x86_64", "aarch64"]
    assert "suffixes" not in h.payload
    assert h.payload["variant_sample"] == 64
    assert h.payload["variant_seed"] == "abc123"
    assert h.payload["attr"] == "dataset.x86_64-linux.hello"
    assert h.payload["toolchain_aggregate_drv"] == _DEFAULT_TC_AGGREGATE_DRV


def test_matrix_eval_header_omits_optional_fields():
    h = make_matrix_eval_header(
        "zlib",
        "x86_64-linux",
        archs=["x86_64"],
        toolchain_aggregate_drv=_DEFAULT_TC_AGGREGATE_DRV,
    )
    assert "variant_sample" not in h.payload
    assert "variant_seed" not in h.payload
    assert "suffixes" not in h.payload
    # toolchain_aggregate_drv is required — must be present even when
    # the optional sample/seed knobs are absent.
    assert h.payload["toolchain_aggregate_drv"] == _DEFAULT_TC_AGGREGATE_DRV


def test_matrix_eval_header_requires_toolchain_aggregate_drv():
    """Empty/missing ``toolchain_aggregate_drv`` is treated as a
    schema-version mismatch and raises at construction time."""
    for bad in ("", None):
        with pytest.raises(ValueError, match="toolchain_aggregate_drv"):
            make_matrix_eval_header(
                "hello",
                "x86_64-linux",
                archs=["x86_64"],
                toolchain_aggregate_drv=bad,  # type: ignore[arg-type]
            )


def test_matrix_eval_header_round_trips_toolchain_aggregate_drv(
    tmp_path: pathlib.Path,
):
    """The toolchain_aggregate_drv field survives the
    write_manifest -> read_manifest JSON round-trip intact."""
    drv = "/nix/store/bbbb-toolchains.drv"
    h = make_matrix_eval_header(
        "hello",
        "x86_64-linux",
        archs=["x86_64"],
        toolchain_aggregate_drv=drv,
        variant_sample=16,
        variant_seed="seed",
    )
    written = write_manifest(tmp_path, h)
    # Inspect the raw JSON too so we know the field made it onto disk
    # and is not just being re-synthesised by ManifestHeader's defaults.
    raw = json.loads(written.read_text())
    assert raw["payload"]["toolchain_aggregate_drv"] == drv
    loaded = read_manifest(written)
    assert loaded == h
    assert loaded.payload["toolchain_aggregate_drv"] == drv


# ---------------------------------------------------------------------------
# Stage-filtered emission
#
# matrix_eval (phase 2) + dependency_graph (phase 3) are JSON-free: they
# are NOT a valid emit_all_manifests stage and are never written here.
# The build-side stages remain JSON-backed.


def test_emit_all_manifests_stages_build_compilers_only(
    tmp_path: pathlib.Path,
):
    """Submit-time slice: emit only build_compilers (toolchains).
    matrix_eval / dependency_graph are JSON-free and never written;
    ``build`` stage manifests stay out — those land later via
    ``primary.spawn_tasks`` from the dependency_graph planner.
    """
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
        stages=["build_compilers"],
    )
    grouped = result.by_class
    assert "matrix_eval" not in grouped
    assert len(grouped["build_compilers"]) == 4
    # build stage classes must be empty.
    assert grouped["build_common_dep"] == ()
    assert grouped["build_variant"] == ()


def test_emit_all_manifests_stages_build_only(tmp_path: pathlib.Path):
    """build slice: emit only common_dep + variant build records.
    toolchain bootstrap stays out.
    """
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
        stages=["build"],
    )
    grouped = result.by_class
    assert "matrix_eval" not in grouped
    assert grouped["build_compilers"] == ()
    assert grouped["toolchain_validate"] == ()
    assert len(grouped["build_common_dep"]) == 2
    assert len(grouped["build_variant"]) == 12


def test_emit_all_manifests_stages_empty_emits_nothing(
    tmp_path: pathlib.Path,
):
    """An empty stages list (the submit path's non-build-compilers
    default) emits zero manifests — matrix_eval / dependency_graph come
    from discover_items, and build tasks come from primary spawn."""
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
        stages=[],
    )
    assert result.headers == ()


def test_emit_all_manifests_matrix_eval_stage_rejected(
    tmp_path: pathlib.Path,
):
    """matrix_eval / dependency_graph are no longer valid stages —
    requesting them is an unknown-stage error (the phases are
    JSON-free)."""
    for bad in ("matrix_eval", "dependency_graph"):
        with pytest.raises(ValueError, match="unknown stage"):
            emit_all_manifests(
                target_dir=tmp_path,
                sys_name="x86_64-linux",
                variants=[],
                toolchain_specs=[],
                common_deps=[],
                stages=[bad],
            )


def test_emit_all_manifests_stages_none_matches_legacy(
    tmp_path: pathlib.Path,
):
    """Regression guard: stages=None emits every JSON-backed class as
    long as inputs are present. matrix_eval / dependency_graph never
    appear (JSON-free)."""
    variants, toolchain_specs, common_deps = _build_full_input()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
    )
    grouped = result.by_class
    assert len(grouped["build_compilers"]) == 4
    assert len(grouped["build_common_dep"]) == 2
    assert len(grouped["build_variant"]) == 12
    assert "matrix_eval" not in grouped


def test_emit_all_manifests_per_binary_metadata_emits_no_matrix_eval(
    tmp_path: pathlib.Path,
):
    """Passing ``per_binary_metadata`` no longer emits matrix_eval JSON
    — the phase is JSON-free; the kwarg is accepted but inert."""
    variants, toolchain_specs, common_deps = _build_full_input()
    metadata = _matrix_eval_metadata()
    result = emit_all_manifests(
        target_dir=tmp_path,
        sys_name="x86_64-linux",
        variants=variants,
        toolchain_specs=toolchain_specs,
        common_deps=common_deps,
        per_binary_metadata=metadata,
    )
    grouped = result.by_class
    assert "matrix_eval" not in grouped
    assert len(grouped["build_compilers"]) == 4
    assert len(grouped["build_common_dep"]) == 2
    assert len(grouped["build_variant"]) == 12
    # No matrix_eval__* files on disk either.
    assert not list(tmp_path.glob("matrix_eval__*.json"))
    assert not list(tmp_path.glob("dependency_graph*.json"))


def test_emit_all_manifests_stages_unknown_raises(tmp_path: pathlib.Path):
    with pytest.raises(ValueError):
        emit_all_manifests(
            target_dir=tmp_path,
            sys_name="x86_64-linux",
            variants=[],
            toolchain_specs=[],
            common_deps=[],
            stages=["bogus"],
        )

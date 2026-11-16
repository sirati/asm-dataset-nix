from __future__ import annotations

import json
from pathlib import Path

import pytest

from compiler_suit_runner.incremental_cache import VariantAxes
from compiler_suit_runner.manifest_gen import make_build_variant_header
from compiler_suit_runner.package_selection import (
    catalog_token, nix_attr_path, select_package_ids,
)
from compiler_suit_runner.publication import stage_release, variant_complete
from compiler_suit_runner.dependency_graph_planner.descriptors import _variant_descriptor
from compiler_suit_runner.preflight import filter_existing_variants
from compiler_suit_runner.workers.eval_worker import sample_suffix_attrs
from template_graph.tree_walker import parse_variant_path

from .release_fixtures import catalog_entry, release

OLD = "hello--v2_12_1--rtest"


def catalog():
    return {"hello": catalog_entry(), OLD: catalog_entry(OLD, "2.12.1", recipe="old", default=False),
            "zlib": catalog_entry("zlib", "1.3", family="zlib")}


def spec(package_id="hello", version="2.12.3", **kwargs):
    return {**release(package_id, version, **kwargs), "pkg": package_id, "arch": "x86_64",
            "label": "gcc15-O2-baseline-none-san-off-march-default", "compiler_id": "gcc15",
            "compiler_family": "gcc", "compiler_version": "15.2.0", "optimization": "O2",
            "flag_set": "baseline", "hardening": "none", "sanitizer": "san-off", "march": "march-default",
            "drv": "/nix/store/fixture-elf-folder.drv", "variant_dir": "gcc15-x86_64-O2-test",
            "metadata_name": "gcc15-x86_64-O2-test.json", "tier": 1}


def test_selection_preserves_defaults_and_replaces_only_explicit_families():
    c = catalog()
    assert select_package_ids(c) == ["hello", "zlib"]
    assert select_package_ids(c, families=["hello"]) == ["hello"]
    assert select_package_ids(c, versions=["hello=2.12.1"]) == [OLD]
    assert select_package_ids(c, families=["hello", "zlib"], versions=["hello=2.12.1"]) == [OLD, "zlib"]
    versions = ["hello=2.12.1", "hello=2.12.3", "hello=2.12.1"]
    assert select_package_ids(c, versions=versions) == select_package_ids(c, versions=versions[::-1]) == ["hello", OLD]
    assert select_package_ids(c, families=["hello"], all_versions=True) == ["hello", OLD]


@pytest.mark.parametrize("options,match", [
    ({"families": ["missing"]}, "unknown package families"),
    ({"versions": ["hello=404"]}, "unknown package version"),
    ({"package_ids": ["hello--missing"]}, "unknown package ID"),
    ({"families": ["zlib"], "versions": ["hello=2.12.1"]}, "conflict"),
    ({"versions": ["hello"]}, "FAMILY=EXACT_VERSION"),
    ({"all_versions": True, "versions": ["hello=2.12.1"]}, "cannot be combined"),
])
def test_invalid_selectors_fail(options, match):
    with pytest.raises(ValueError, match=match):
        select_package_ids(catalog(), **options)


def test_exact_versions_and_recipe_ambiguity():
    c = catalog()
    c["hello--another"] = catalog_entry("hello--another", recipe="other", default=False)
    with pytest.raises(ValueError, match="ambiguous.*hello--another"):
        select_package_ids(c, versions=["hello=2.12.3"])
    assert select_package_ids(c, package_ids=["hello--another"]) == ["hello--another"]
    assert c["hello"]["packageIdentity"] != c["hello--another"]["packageIdentity"]
    for i, version in enumerate(["1.2", "1+2", "1_2", "1-2"]):
        key = f"hello--r{i}"
        c[key] = catalog_entry(key, version, recipe=f"recipe{i}", default=False)
        assert select_package_ids(c, versions=[f"hello={version}"]) == [key]
    assert nix_attr_path("dataset", "x86_64-linux", "hello+extra") == 'dataset.x86_64-linux."hello+extra"'


def test_cache_identity_tracks_source_and_recipe_under_a_stable_alias():
    a = {"hello": catalog_entry()}
    b = {"hello": catalog_entry(recipe="changed")}
    assert catalog_token(a) != catalog_token(b)
    def axes(c):
        return VariantAxes.from_values(packages=["hello"], catalog_identity=catalog_token(c))
    assert axes(a).canonical_bytes() != axes(b).canonical_bytes()
    assert VariantAxes.from_values(packages=[OLD, "hello", OLD]) == VariantAxes.from_values(packages=["hello", OLD])


def test_sampling_matches_across_releases_and_order():
    values = {f"gcc15-O2-flag{i}": {"compiler": "gcc15", "optimization": "O2", "packageId": "hello"} for i in range(20)}
    other = {key: {**value, "packageId": OLD} for key, value in reversed(list(values.items()))}
    def sample(v):
        return set(sample_suffix_attrs(v, arch="x86_64", sample_size=3, seed="42"))
    assert sample(values) == sample(other)


@pytest.mark.parametrize("package_id", [OLD, "family--v1-aarch64-gcc15-O2", "family--v1-O2-baseline-default-san-off-march-default"])
def test_parser_roundtrip_with_axis_like_package_ids(package_id):
    from template_graph.graph import VariantArray
    from template_graph.parser.role import drv_role
    from compiler_suit_runner.workers.dependency_graph_worker.slice import _group_templates_by_binary
    suffix = "gcc15-O2-baseline-none-san-off-march-default"
    role = drv_role(f"{package_id}-aarch64-{suffix}-elf-folder.drv")
    assert parse_variant_path(role) == (package_id, "aarch64", "gcc15", "O2")
    # Per-binary slicing reads the matrix each array was built under, so
    # the package ID is never re-parsed from the drv name.
    arrays = {(0, "aarch64"): VariantArray(template_id=0, arch="aarch64", variants=[], hashes=[], binary=package_id)}
    assert _group_templates_by_binary(arrays) == {package_id: {0}}


def test_manifest_and_streamed_task_ids_include_architecture():
    a = spec()
    header = make_build_variant_header(a, "x86_64-linux")
    descriptor = _variant_descriptor(binary="hello", arch="x86_64", sys_name="x86_64-linux",
                                     label=a["label"], variant_spec=a, depends_on=[])
    assert header.task_id == descriptor.task_id
    b = {**a, "arch": "aarch64"}
    assert make_build_variant_header(b, "x86_64-linux").task_id != header.task_id
    assert make_build_variant_header({**a, "packageIdentity": "0" * 64}, "x86_64-linux").task_id != header.task_id
    assert header.payload["packageIdentity"] == descriptor.payload["packageIdentity"] == a["packageIdentity"]


def make_output(tmp_path: Path, payload: dict) -> Path:
    out = tmp_path / payload["packageId"]
    (out / "elf").mkdir(parents=True)
    (out / "elf" / "hello").write_bytes(b"\x7fELF" + payload["packageVersion"].encode())
    meta = {**release_metadata_for_output(payload), "packageDrv": "/nix/store/raw-package.drv",
            "packageOutputs": {"out": "/nix/store/raw-package"}}
    (out / "meta.json").write_text(json.dumps(meta))
    return out


def release_metadata_for_output(payload):
    from compiler_suit_runner.package_selection import release_metadata
    return {**release_metadata(payload), "arch": payload["arch"], "compiler": payload["compiler_id"],
            "compilerFamily": "gcc", "compilerVersion": "15.2.0", "optimization": "O2",
            "flags": "baseline", "hardening": "none", "sanitizer": "san-off", "march": "march-default"}


def test_publication_resume_rejects_stale_legacy_and_damaged_results(tmp_path):
    a = spec()
    package_dir = tmp_path / "dataset" / "hello"
    out = make_output(tmp_path / "store", a)
    stage_release(out, package_dir, a)
    assert variant_complete(package_dir, a)
    assert filter_existing_variants((a,), dataset_dir=package_dir.parent) == ((), 1)
    for changed in ({"drv": "different.drv"}, {"packageIdentity": "a" * 64}, {"sourceIdentity": {"outputHash": "changed"}},
                    {"recipeId": {"provider": "changed"}}, {"packageVersion": "new"}):
        assert not variant_complete(package_dir, {**a, **changed})
    (package_dir / a["variant_dir"] / "hello").write_bytes(b"damaged")
    assert not variant_complete(package_dir, a)
    stage_release(out, package_dir, a)
    (package_dir / a["metadata_name"]).write_text(json.dumps({"label": a["label"]}))
    assert not variant_complete(package_dir, a)
    assert filter_existing_variants((a,), dataset_dir=package_dir.parent) == ((a,), 0)


def test_retry_of_one_release_preserves_the_other_and_wrong_source_fails(tmp_path):
    a, b = spec(), spec(OLD, "2.12.1", recipe="old", default=False)
    root = tmp_path / "dataset"
    for payload in (a, b):
        stage_release(make_output(tmp_path / "store", payload), root / payload["packageId"], payload)
    assert variant_complete(root / "hello", a)
    stage_release(tmp_path / "store" / OLD, root / OLD, b)
    assert variant_complete(root / "hello", a) and variant_complete(root / OLD, b)
    with pytest.raises(ValueError, match="provenance mismatch"):
        stage_release(tmp_path / "store" / OLD, root / OLD, {**b, "packageVersion": "incorrect"})


def test_prestaged_matrix_checks_release_identity_and_scopes_selected_ids(tmp_path):
    from compiler_suit_runner.workers.dependency_graph_worker.archive import load_drv_map_from_sidecars, write_drv_map_sidecar
    a, b = release(), release(OLD, "2.12.1", recipe="old", default=False)
    write_drv_map_sidecar(tmp_path, "hello", "hello.drv", package_metadata=a)
    write_drv_map_sidecar(tmp_path, OLD, "old.drv", package_metadata=b)
    assert load_drv_map_from_sidecars(tmp_path, {"hello"}, package_metadata={"hello": a}) == {"hello": "hello.drv"}
    with pytest.raises(FileNotFoundError, match="stale"):
        load_drv_map_from_sidecars(tmp_path, {"hello"}, package_metadata={"hello": release(recipe="changed")})
    write_drv_map_sidecar(tmp_path, "hello", "legacy.drv")
    with pytest.raises(FileNotFoundError, match="stale"):
        load_drv_map_from_sidecars(tmp_path, {"hello"}, package_metadata={"hello": a})


def test_publication_directory_changes_with_release_or_derivation():
    from compiler_suit_runner.preflight import _short_dataset_name
    options = dict(compiler_id="gcc15", arch="x86_64", optimization="O2", full_label="same-suffix")
    a = _short_dataset_name(**options, package_identity="a" * 64, variant_drv="a.drv")
    assert a != _short_dataset_name(**options, package_identity="b" * 64, variant_drv="a.drv")
    assert a != _short_dataset_name(**options, package_identity="a" * 64, variant_drv="b.drv")


def test_unknown_release_fails_submit_before_toolchain_staging(monkeypatch, tmp_path):
    from compiler_suit_runner import cli
    def invalid(*args, **kwargs):
        raise ValueError("unknown package version: hello=missing")
    monkeypatch.setattr(cli, "resolve_package_selection", invalid)
    monkeypatch.setattr(cli, "enumerate_toolchains_only", lambda *a, **kw: pytest.fail("staged toolchains"))
    assert cli.main(["submit", "--shared-fs", str(tmp_path / "unused"), "--package-versions", "hello=missing"]) == 2
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("identity", [None, [], {}, 12, "short"])
def test_incomplete_or_malformed_cached_release_is_a_miss(tmp_path, identity):
    from compiler_suit_runner.incremental_cache import IncrementalCache
    cache = IncrementalCache(tmp_path)
    meta = {**release(), "archs": ["x86_64"], "sample_size": 1, "sample_seed": "42", "tier": 1,
            "packageIdentity": identity}
    cache.store_variants("malformed", {"hello": meta})
    assert cache.lookup_variants("malformed") is None


@pytest.mark.parametrize("wrapped", [False, True])
def test_archived_derivation_provenance_reaches_both_descriptor_paths(wrapped):
    from compiler_suit_runner.workers.dependency_graph_worker.archive import derive_variant_lookup_from_aggregate
    payload = spec(OLD, "2.12.1", recipe="old", default=False)
    path = f"/nix/store/{'a'*32}-{OLD}-x86_64-{payload['label']}-elf-folder.drv"
    meta = {**release_metadata_for_output(payload), "packageDrv": "/nix/store/raw-package.drv",
            "packageOutputs": {"out": "/nix/store/raw-package"}}
    node = {"env": {"datasetMetadata": json.dumps(meta)}, "outputs": {"out": {"path": "folder-output" if wrapped else "/nix/store/folder-output"}}}
    shown = {"version": 4, "derivations": {path.removeprefix("/nix/store/"): node}} if wrapped else {path: node}
    def runner(argv):
        if argv[:3] == ["nix-store", "--query", "--references"]:
            return path.encode() + b"\n", b"", 0
        assert argv[:3] == ["nix", "derivation", "show"]
        return json.dumps(shown).encode(), b"", 0
    result = next(iter(derive_variant_lookup_from_aggregate("aggregate.drv", run_subprocess=runner).values()))
    assert result["packageIdentity"] == payload["packageIdentity"]
    assert result["packageFamily"] == "hello" and result["tier"] == 1
    assert result["compiler_version"] == "15.2.0"
    assert result["outputPaths"] == {"out": "/nix/store/folder-output"}
    header = make_build_variant_header(result, "x86_64-linux")
    assert header.payload["attr"] == f"dataset.x86_64-linux.{OLD}.x86_64.{payload['label']}"
    descriptor = _variant_descriptor(binary=OLD, arch="x86_64", sys_name="x86_64-linux", label=result["label"],
                                     variant_spec=result, depends_on=[])
    assert header.task_id == descriptor.task_id
    for key in ("packageIdentity", "recipeId", "sourceIdentity", "variant_dir", "metadata_name", "packageOutputs", "outputPaths"):
        assert header.payload[key] == descriptor.payload[key]

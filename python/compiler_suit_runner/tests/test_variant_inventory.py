import itertools
import json

import pytest

from compiler_suit_runner.variant_inventory import load_inventory, sample_inventory, validate_inventory
from compiler_suit_runner.workers.eval_worker import sample_suffix_attrs, _validate_evaluated_release
from compiler_suit_runner.preflight import is_known_bad_combo, resolve_package_selection
from .release_fixtures import catalog_entry, release

IDENTITY = "a" * 64
ARCH = "x86_64"


def inventory():
    return {"schema": 1, "identity": IDENTITY, "arch": ARCH, "groups": [
        {"compiler": "gcc15", "compilerFamily": "gcc", "compilerVersion": "15.2.0", "optimization": opt,
         "axes": {"flags": ["baseline", "fastmath", "nopic"], "hardening": ["ssp", "ssp-all", "none"],
                  "sanitizer": ["san-off", "san-undefined"], "march": ["march-default", "march-v2"]}}
        for opt in ("O0", "O2")
    ]}


@pytest.mark.parametrize("sample,seed", [(0, "42"), (1, "42"), (3, "different"), (1000, "42"), (1, "")])
def test_compact_sampling_matches_legacy_sorted_metadata(sample, seed):
    inv = inventory()
    rows = {}
    for group in inv["groups"]:
        for values in itertools.product(*group["axes"].values()):
            suffix = "-".join([group["compiler"], group["optimization"], *values])
            meta = {**release(), **{k: v for k, v in group.items() if k != "axes"}, "arch": ARCH,
                    **dict(zip(group["axes"], values))}
            if is_known_bad_combo(meta) is None:
                rows[suffix] = meta
    expected = set(sample_suffix_attrs(rows, arch=ARCH, sample_size=sample, seed=seed)) if sample and seed else set(rows)
    actual = sample_inventory(inv, arch=ARCH, package_metadata=release(), sample_size=sample, seed=seed)
    assert set(actual) == expected
    other = release("hello--other", "2.12.1", recipe="old", default=False)
    assert sample_inventory(inv, arch=ARCH, package_metadata=other, sample_size=sample, seed=seed) == actual
    ceiling = actual[:2]
    if ceiling:
        assert set(sample_inventory(inv, arch=ARCH, package_metadata=release(), sample_size=0, seed=seed, ceiling=ceiling)) == set(ceiling)


def test_empty_axis_has_no_candidates_and_duplicate_groups_fail():
    inv = inventory()
    for group in inv["groups"]:
        group["axes"]["flags"] = []
    assert sample_inventory(inv, arch=ARCH, package_metadata=release(), sample_size=1, seed="42") == []
    inv["groups"].append(inv["groups"][0])
    with pytest.raises(ValueError, match="duplicate"):
        validate_inventory(inv, IDENTITY, ARCH)


@pytest.mark.parametrize("field,value", [("schema", True), ("schema", 999), ("identity", "b" * 64), ("arch", "aarch64"), ("groups", None)])
def test_bad_inventory_is_rejected(field, value):
    inv = inventory()
    inv[field] = value
    with pytest.raises(ValueError):
        validate_inventory(inv, IDENTITY, ARCH)


def test_cache_shares_axes_across_releases_and_detects_corruption_and_changed_source(tmp_path):
    calls = []
    token = IDENTITY
    def runner(argv):
        calls.append(argv)
        assert "--no-eval-cache" in argv
        target = next(v for v in argv if "#" in v)
        if "_evaluationIdentity" in target:
            return (token + "\n").encode(), b"", 0
        assert "_variantAxes" in target
        return json.dumps(inventory()).encode(), b"", 0
    options = dict(flake_ref=".", system="x86_64-linux", arch=ARCH,
                   expected_identity=IDENTITY, cache_dir=tmp_path, run_subprocess=runner)
    assert load_inventory(**options) == inventory()
    assert load_inventory(**options) == inventory()
    assert sum(any("#_variantAxes" in arg for arg in argv) for argv in calls) == 1
    path = next((tmp_path / "_variant_inventory").glob("*.json"))
    broken = json.loads(path.read_text())
    broken["data"]["groups"][0]["compilerVersion"] = "wrong"
    path.write_text(json.dumps(broken))
    assert load_inventory(**options) == inventory()
    token = "b" * 64
    with pytest.raises(ValueError, match="changed since preflight"):
        load_inventory(**options)
    assert not list((tmp_path / "_variant_inventory").glob("tmp*"))


def test_early_selection_does_not_force_other_releases_or_targets():
    index = {"hello": {"packageFamily": "hello", "packageId": "hello", "isDefault": True},
             "hello--old": {"packageFamily": "hello", "packageId": "hello--old", "isDefault": False}}
    queries = []
    def runner(argv):
        expression = argv[argv.index("--apply") + 1]
        queries.append(expression)
        if "inherit (p) packageFamily packageId isDefault;" in expression:
            return json.dumps(index).encode(), b"", 0
        assert "hello--old" not in expression
        assert 'x86_64' in expression
        entry = catalog_entry()
        entry["availability"] = {ARCH: entry["availability"][ARCH]}
        return json.dumps({"hello": entry}).encode(), b"", 0
    result = resolve_package_selection(".", "x86_64-linux", packages=["hello"], archs=[ARCH], run_subprocess=runner)
    assert list(result) == ["hello"] and set(result["hello"]["availability"]) == {ARCH}
    assert len(queries) == 2


def test_direct_instantiation_rejects_changed_release_in_modern_nix_json():
    path = "/nix/store/fixture.drv"
    expected = release()
    meta = {**expected, "arch": ARCH}
    def runner(argv):
        return json.dumps({"version": 4, "derivations": {"fixture.drv": {"env": {"datasetMetadata": json.dumps(meta)}}}}).encode(), b"", 0
    _validate_evaluated_release({(ARCH, "suffix"): path}, expected, run_subprocess=runner)
    meta["packageVersion"] = "changed"
    with pytest.raises(ValueError, match="changed since preflight"):
        _validate_evaluated_release({(ARCH, "suffix"): path}, expected, run_subprocess=runner)


@pytest.mark.parametrize("options", [{"packages": []}, {"archs": []},
                                      {"all_package_versions": True, "package_versions": ["hello=2.12.3"]}])
def test_empty_or_conflicting_requests_fail_without_evaluating(options):
    def runner(argv):
        pytest.fail("invalid request evaluated Nix")
    with pytest.raises(ValueError):
        resolve_package_selection(".", "x86_64-linux", run_subprocess=runner, **options)


def test_excessive_candidate_product_is_rejected_before_sampling():
    inv = inventory()
    inv["groups"][0]["axes"] = {axis: [f"v{i}" for i in range(20)]
                                for axis in ("flags", "hardening", "sanitizer", "march")}
    with pytest.raises(ValueError, match="candidate limit"):
        validate_inventory(inv, IDENTITY, ARCH)


def test_compact_mode_cannot_silently_select_uncaptured_namespace():
    from compiler_suit_runner.workers.eval_worker import parse_payload
    payload = {"binary": "hello", "sys": "x86_64-linux", "archs": [ARCH], "attr": "datasetCaptured.x86_64-linux.hello",
               "toolchain_aggregate_drv": "/nix/store/toolchains.drv", "variant_inventory_schema": 1,
               "variant_inventory_identity": IDENTITY}
    with pytest.raises(ValueError, match="namespace"):
        parse_payload(payload)


def test_prestaged_matrix_rejects_changed_flags_seed_targets_or_toolchains(tmp_path):
    from compiler_suit_runner.workers.dependency_graph_worker.archive import write_drv_map_sidecar, read_drv_map_sidecar
    meta = {**release(), "variant_inventory_schema": 1, "variant_inventory_identity": IDENTITY,
            "sys": "x86_64-linux", "archs": [ARCH], "variant_sample": 2, "variant_seed": "42",
            "toolchain_aggregate_drv": "toolchains.drv"}
    write_drv_map_sidecar(tmp_path, "hello", "matrix.drv", package_metadata=meta)
    assert read_drv_map_sidecar(tmp_path, "hello", expected_package=meta) == "matrix.drv"
    for change in ({"variant_inventory_identity": "b" * 64}, {"variant_seed": "new"}, {"variant_sample": 3},
                   {"archs": ["aarch64"]}, {"toolchain_aggregate_drv": "changed.drv"}):
        assert read_drv_map_sidecar(tmp_path, "hello", expected_package={**meta, **change}) is None

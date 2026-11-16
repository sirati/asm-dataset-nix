import json
import importlib.util
import os
from pathlib import Path
import sys
import unittest

import pytest

from compiler_suit_runner.capture import captured_spec, required_contract
from compiler_suit_runner.incremental_cache import ToolchainAxes, VariantAxes
from compiler_suit_runner import preflight
from compiler_suit_runner.manifest_gen import make_matrix_eval_header,variant_task_id
from compiler_suit_runner.manifest_gen import make_build_compilers_header,make_toolchain_validate_header
from compiler_suit_runner.dependency_graph_planner.descriptors import _variant_descriptor
from compiler_suit_runner.workers.dependency_graph_worker import run


@pytest.fixture
def real_capture_folder():
    if not all(os.environ.get(name) for name in ["CAPTURE_BINARY", "CAPTURE_GCC", "CAPTURE_CLANG"]):
        pytest.skip("real compiler capture fixture tools are required")
    path = Path(__file__).resolve().parents[3]/"tests/compiler-capture/test_debug_provenance.py"
    module_spec = importlib.util.spec_from_file_location("runner_real_capture_fixture", path)
    fixture_module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = fixture_module
    module_spec.loader.exec_module(fixture_module)
    class Fixture(fixture_module.DebugFixture, unittest.TestCase):
        pass
    fixture = Fixture()
    fixture.setUp()
    try:
        folder, _, contract = fixture.make_folder()
        yield fixture, folder, contract
    finally:
        fixture.tearDown()


def test_worker_binds_requested_identity_and_preserves_requested_version(real_capture_folder, tmp_path, monkeypatch):
    from compiler_suit_runner.workers import build_worker as worker
    fixture, folder, contract = real_capture_folder
    monkeypatch.setattr(worker, "build_attr", lambda *args: (True, (str(folder) + "\n").encode(), b""))
    payload = dict(pkg="fixture", label="fixture", compiler_id="gcc", compiler_family="gcc", compiler_version="15",
        arch="x86_64", drv="/nix/store/fixture-folder.drv", variant_dir="variant_capture-v2",
        metadata_name="variant_capture-v2.json", compiler_capture=contract)
    env = worker.BuildWorkerEnv(flake_ref=".", dataset_output_dir=tmp_path/"staging")
    header = dict(item_class="build_variant", name="fixture", payload=payload)
    result = worker.build_worker(Path("inline"), env, manifest_data=header)
    assert result.success, result.error
    sidecar = json.loads((env.dataset_output_dir/"fixture"/payload["metadata_name"]).read_bytes())
    assert sidecar["compiler_version"] == "15"
    assert sidecar["compiler_capture"]["provenance"]["toolchain"]["version"] == "15.2.0"
    published = env.dataset_output_dir/"fixture"/payload["variant_dir"]
    from compiler_suit_runner.capture import captured_variant_complete
    assert captured_variant_complete(published, label="fixture", drv=payload["drv"], contract=contract, expected=payload)
    payload["compiler_version"] = "14"
    result = worker.build_worker(Path("inline"), env, manifest_data=header)
    assert not result.success
    assert "requested compiler version" in result.error
    assert captured_variant_complete(published, label="fixture", drv=payload["drv"], contract=contract)


def spec():
    return dict(label="hello__x86_64__gcc15-O2",pkg="hello",arch="x86_64",drv="/nix/store/fixture.drv",
        compiler_id="gcc15",compiler_family="gcc",compiler_version="15.2.0",optimization="O2",
        variant_dir="gcc15_x86_64_O2_abcdef12",metadata_name="gcc15_x86_64_O2_abcdef12.json",tier=1)


def test_capture_changes_cache_and_publication_identities():
    assert ToolchainAxes.from_values().canonical_bytes()!=ToolchainAxes.from_values(compiler_capture=True).canonical_bytes()
    assert VariantAxes.from_values().canonical_bytes()!=VariantAxes.from_values(compiler_capture=True).canonical_bytes()
    ordinary=spec()
    captured=captured_spec(ordinary)
    assert captured_spec(captured)==captured
    assert captured["variant_dir"]!=ordinary["variant_dir"]
    assert captured["compiler_capture"]==required_contract("gcc")
    assert variant_task_id(captured,"x86_64-linux")!=variant_task_id(ordinary,"x86_64-linux")


def test_capture_selects_matching_dataset_and_toolchain_outputs(monkeypatch):
    # The package catalog is shared by the ordinary and captured matrices;
    # captured enumeration marks every package for capture.
    catalog={"hello":{"packageFamily":"hello","packageId":"hello",
        "availability":{"x86_64":{"available":True,"reason":None}}}}
    result=preflight.enumerate_variants(".","x86_64-linux",archs=["x86_64"],compiler_capture=True,selected_catalog=catalog)
    assert result["hello"]["compiler_capture"] is True
    assert result["hello"]["archs"]==["x86_64"]
    monkeypatch.setattr(preflight.shutil,"which",lambda _:None)
    calls=[]
    def toolchain(arguments):
        calls.append(arguments)
        return b"/nix/store/fixture.drv",b"",0
    assert preflight.eval_toolchain_drvs(".","x86_64-linux",(("x86_64","gcc15"),),compiler_capture=True,run_subprocess=toolchain)
    assert any("#_capturedCrossToolchainMap.x86_64-linux.x86_64.gcc15" in argument for argument in calls[0])
    header=make_matrix_eval_header("hello","x86_64-linux",["x86_64"],toolchain_aggregate_drv="/nix/store/toolchains.drv",compiler_capture=True)
    assert header.payload["attr"]=="datasetCaptured.x86_64-linux.hello"
    assert header.payload["compiler_capture"] is True


def test_streamed_variant_spec_preserves_required_contract(monkeypatch):
    ordinary=spec()
    monkeypatch.setattr(run._archive,"derive_variant_lookup_from_aggregate",lambda *args,**kwargs:{("x86_64",ordinary["label"]):ordinary})
    lookups,binaries=run._derive_variant_lookups(drv_by_binary={"hello":"/nix/store/matrix.drv"},toolchain_outpaths_map={},compiler_capture=True)
    captured=lookups["hello"][("x86_64",ordinary["label"])]
    descriptor=_variant_descriptor(binary="hello",arch="x86_64",sys_name="x86_64-linux",label=ordinary["label"],variant_spec=captured,depends_on=[])
    assert binaries==["hello"]
    assert descriptor.payload["compiler_capture"]==required_contract("gcc")
    assert descriptor.payload["variant_dir"].endswith("_capture-v2")
    assert descriptor.task_id==variant_task_id(captured,"x86_64-linux")


def test_legacy_elf_sidecar_cannot_complete_captured_variant(tmp_path):
    ordinary=spec()
    directory=tmp_path/"hello"
    directory.mkdir()
    (directory/ordinary["metadata_name"]).write_text(json.dumps(dict(label=ordinary["label"])))
    # Release publication: a legacy label-only sidecar no longer completes
    # an ordinary variant either; it rebuilds with release provenance.
    assert preflight.filter_existing_variants((ordinary,),dataset_dir=tmp_path)==((ordinary,),0)
    captured=captured_spec(ordinary)
    assert preflight.filter_existing_variants((captured,),dataset_dir=tmp_path)==((captured,),0)


def test_secondary_capture_mode_and_archive_namespace(monkeypatch):
    from compiler_suit_runner import cli
    monkeypatch.setenv("CSR_COMPILER_CAPTURE","1")
    secondary=cli._parse_framework_spawned_secondary(["--secondary","tcp://127.0.0.1:9","--secondary-id","test"])
    config=cli._config_from_args(secondary,run_id="capture-test",secondary_id="test")
    assert config.compiler_capture is True
    assert config.matrix_eval_out_dir.name=="_matrix_eval_capture_v2"
    args=cli.build_parser().parse_args(["submit","--compiler-capture","--shared-fs","/tmp/capture-test"])
    local=cli._config_from_args(args,run_id="capture-test",secondary_id="local")
    assert local.compiler_capture is True
    assert local.matrix_eval_out_dir.name==config.matrix_eval_out_dir.name


def test_capture_toolchain_fallback_attributes():
    for header in [make_build_compilers_header("x86_64-linux","aarch64","gcc15",compiler_capture=True),
            make_toolchain_validate_header("x86_64-linux","aarch64","gcc15","/nix/store/toolchain.drv",compiler_capture=True)]:
        assert header.payload["attr"]=="_capturedCrossToolchainMap.x86_64-linux.aarch64.gcc15"

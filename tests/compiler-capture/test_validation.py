import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location("tested_capture",os.environ.get("CAPTURE_VALIDATOR",Path(__file__).parents[2]/"python/compiler_suit_runner/capture.py"))
capture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = capture
_spec.loader.exec_module(capture)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/"objects").mkdir()
        config = dict(schema=1,wrapper_revision=1,executable="/nix/store/fixture/bin/gcc",family="gcc",
                      version="15.2.0",target="x86_64-linux",driver="c",backend="gcc-trace",supervisor="/nix/store/fixture/bin/compiler-capture",debug_policy="required",nix_response_default=False)
        artifacts = []
        objects = {}
        for role in ["object","assembly","preprocessing"]:
            data = role.encode()
            digest = hashlib.sha256(data).hexdigest()
            (self.root/"objects"/digest).write_bytes(data)
            objects[digest] = len(data)
            artifacts.append(dict(sha256=digest,size=len(data),role=role,path_base="cwd",original_path_hex=b"unit".hex()))
        self.record = dict(schema=1,compiler=config,config_sha256=hashlib.sha256(json.dumps(config,separators=(",",":")).encode()).hexdigest(),
            compiler_status=dict(exit_code=0,signal=None),capture_complete=True,capture_errors=[],artifacts=artifacts,
            analysis=dict(mode="compile",inputs=[list(b"unit.c")],output=list(b"unit.o")))
        self.manifest = dict(schema=1,wrapper_revision=1,complete=True,policy="required",errors=[],artifacts=objects,invocations=[])
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        identity = hashlib.sha256(capture._canonical(self.record)).hexdigest()
        self.manifest["invocations"] = [dict(id=identity,count=2,record=self.record)]
        (self.root/"manifest.json").write_text(json.dumps(self.manifest))

    def test_valid_hashes_roles_and_multiplicity(self):
        value = capture.validate_capture(self.root)
        self.assertEqual(value.invocation_count,2)
        self.assertEqual(value.artifact_count,3)

    def test_legacy_or_incomplete_results_do_not_count_as_capture(self):
        self.manifest["complete"] = False
        self.write()
        with self.assertRaises(capture.CaptureValidationError):
            capture.validate_capture(self.root)
        self.assertFalse(capture.validate_capture(self.root,require_complete=False).complete)

    def test_checksum_corruption(self):
        next((self.root/"objects").iterdir()).write_bytes(b"corrupt")
        with self.assertRaises(capture.CaptureValidationError):
            capture.validate_capture(self.root)

    def test_unindexed_file(self):
        (self.root/"objects"/"extra").write_bytes(b"unvalidated")
        with self.assertRaises(capture.CaptureValidationError):
            capture.validate_capture(self.root)

    def test_missing_required_role(self):
        self.record["artifacts"][0]["role"] = "ir"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"roles"):
            capture.validate_capture(self.root)

    def clang_config(self, version):
        config=self.record["compiler"]
        config.update(family="clang",version=version,backend="clang-trace")
        self.record["config_sha256"]=hashlib.sha256(json.dumps(config,separators=(",",":")).encode()).hexdigest()

    def test_legacy_native_backend_requires_bitcode(self):
        self.clang_config("3.5.2")
        self.record["native_from_ir"]=True
        self.record["artifacts"][1]["role"]="ir"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"bitcode"):
            capture.validate_capture(self.root)

    def test_existing_preprocessed_input_does_not_require_an_ir_stage(self):
        self.clang_config("10.0.1")
        self.record["analysis"]["inputs"]=[list(b"unit.i")]
        self.write()
        self.assertTrue(capture.validate_capture(self.root).complete)

    def test_explicit_preprocessed_language_keeps_its_capture_contract(self):
        self.clang_config("3.5.2")
        self.record["analysis"]["input_languages"]=[list(b"cpp-output")]
        self.write()
        self.assertTrue(capture.validate_capture(self.root).complete)

    def test_split_dwarf_requires_a_retained_companion(self):
        self.record["analysis"]["split_dwarf"] = True
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"roles"):
            capture.validate_capture(self.root)

    def test_legacy_split_dwarf_metadata_requires_a_companion(self):
        self.record["analysis"]["debug"]=[list(b"-gsplit-dwarf")]
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"roles"):
            capture.validate_capture(self.root)

    def test_clang_bitcode_frontend_defers_split_companion_but_native_output_does_not(self):
        self.clang_config("21.1.8")
        self.record["analysis"].update(lto=True, split_dwarf=True)
        for artifact in self.record["artifacts"]:
            if artifact["role"] == "assembly":
                artifact["role"] = "ir"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError, "roles"):
            capture.validate_capture(self.root)
        for artifact in self.record["artifacts"]:
            if artifact["role"] not in ["object", "ir"]:
                continue
            old = artifact["sha256"]
            data = b"BC\xc0\xde" + artifact["role"].encode()
            digest = hashlib.sha256(data).hexdigest()
            (self.root/"objects"/old).unlink()
            self.manifest["artifacts"].pop(old)
            (self.root/"objects"/digest).write_bytes(data)
            self.manifest["artifacts"][digest] = len(data)
            artifact.update(sha256=digest, size=len(data))
        self.write()
        self.assertTrue(capture.validate_capture(self.root).complete)

    def test_split_dwarf_companion_must_contain_elf_debug_information(self):
        self.record["artifacts"][0]["role"] = "debug-companion"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"ELF"):
            capture.validate_capture(self.root)

    def add_deferred_dwo(self):
        data=bytearray(64)
        data[:7]=b"\x7fELF\x02\x01\x01"
        struct.pack_into("<HHIQQQIHHHHHH",data,16,1,62,1,0,0,0,0,64,0,0,64,0,0)
        digest=hashlib.sha256(data).hexdigest()
        (self.root/"objects"/digest).write_bytes(data)
        self.manifest["artifacts"][digest]=len(data)
        self.record["artifacts"].append(dict(sha256=digest,size=len(data),role="debug-companion",path_base="cwd",original_path_hex=b"unit.dwo".hex()))
        self.record["analysis"].update(lto=True,split_dwarf=True)

    def test_lto_frontend_can_defer_split_dwarf_until_link(self):
        self.add_deferred_dwo()
        self.write()
        self.assertTrue(capture.validate_capture(self.root).complete)

    def test_lto_link_requires_real_split_dwarf_debug_information(self):
        self.add_deferred_dwo()
        self.record["analysis"]["mode"]="link"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"compilation-unit"):
            capture.validate_capture(self.root)

    def test_artifact_must_fit_its_declared_capture_budget(self):
        self.record["limits"]=dict(artifact_bytes=1,invocation_bytes=1024,total_bytes=4096)
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"declared limit"):
            capture.validate_capture(self.root)

    def test_large_valid_command_record(self):
        self.record["arguments_hex"]=[("-DNAME="+"x"*50000).encode().hex()]
        self.write()
        self.assertEqual(capture.validate_capture(self.root).invocation_count,2)

    def test_cfi_transformation_rejects_changed_machine_assembly(self):
        config=self.record["compiler"]
        config.update(family="clang",version="3.8.1",target="aarch64-unknown-linux-gnu",backend="clang-trace")
        self.record["config_sha256"]=hashlib.sha256(json.dumps(config,separators=(",",":")).encode()).hexdigest()
        pair=[]
        for data in [b".cfi_startproc\n.cfi_endproc\n.cfi_sections .debug_frame\n",b"different machine instructions\n"]:
            digest=hashlib.sha256(data).hexdigest();pair.append(digest)
            (self.root/"objects"/digest).write_bytes(data);self.manifest["artifacts"][digest]=len(data)
            self.record["artifacts"].append(dict(sha256=digest,size=len(data),role="assembly",path_base="invocation-stage",original_path_hex=b"unit.s".hex()))
        self.record["transformations"]=[dict(kind="clang-3.8-aarch64-cfi-directive-order",emitted_assembly_sha256=pair[0],assembler_input_sha256=pair[1])]
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"declared CFI correction"):
            capture.validate_capture(self.root)

    def test_record_identity_mismatch(self):
        self.record["compiler"]["target"] = "different"
        # Deliberately retain the old record hash on disk.
        (self.root/"manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaises(capture.CaptureValidationError):
            capture.validate_capture(self.root)

    def test_retained_pch_source_requires_a_matching_producer(self):
        self.record["retained_pch_sources"]=[self.record["artifacts"][-1]]
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"matching producer"):
            capture.validate_capture(self.root)

    def test_duplicate_json_keys(self):
        (self.root/"manifest.json").write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(capture.CaptureValidationError,"duplicate"):
            capture.validate_capture(self.root)

    def test_malformed_analysis_is_a_validation_failure(self):
        malformed = [None, [], "compile", dict(mode="compile", inputs=[32]),
            dict(mode="compile", inputs=[[True]]), dict(mode="compile", language=32),
            dict(mode="compile", output=32), dict(mode="compile", inputs=[[]], input_languages=[32])]
        for analysis in malformed:
            with self.subTest(analysis=analysis):
                self.record["analysis"] = analysis
                self.write()
                with self.assertRaises(capture.CaptureValidationError):
                    capture.validate_capture(self.root)

    def publication_fixture(self):
        store = self.root/"folder"
        (store/"elf").mkdir(parents=True)
        os.symlink(sys.executable,store/"elf"/"program")
        (store/"auxiliary").mkdir()
        os.symlink(self.root,store/"auxiliary"/"datasetCapture")
        contract = dict(schema=1,wrapper_revision=1,backend="gcc-trace",debug_policy="required",policy="required")
        return store,self.root/"staging",contract

    def test_publication_validates_every_byte_and_contract(self):
        store,dest,contract = self.publication_fixture()
        paths,summary = capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        self.assertTrue(summary["complete"])
        self.assertTrue(all(p.is_file() and not p.is_symlink() for p in paths))
        self.assertTrue(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))
        self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="other.drv",contract=contract))
        (dest/"variant"/"program").chmod(0o600)
        (dest/"variant"/"program").write_bytes(b"damaged")
        self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_publication_retry_replaces_stale_generation(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        (dest/"variant"/"stale").write_bytes(b"old generation")
        stale = dest/".variant.capture-crashed"
        stale.mkdir()
        (stale/"partial").touch()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        self.assertFalse(stale.exists())
        self.assertFalse((dest/"variant"/"stale").exists())
        self.assertTrue(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_completion_rechecks_the_recorded_compiler_contract(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        path = dest/"variant"/"capture-complete.json"
        completion = json.loads(path.read_bytes())
        for change in [dict(backend="clang-trace"), dict(debug_policy="preserve"),
                dict(policy="observe"), dict(required_wrapper_identity=True)]:
            with self.subTest(change=change):
                requested = dict(contract, **change)
                completion["contract"] = requested
                path.write_bytes(capture._canonical(completion))
                self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=requested))

    def test_publication_checks_the_manifest_policy(self):
        store,dest,contract = self.publication_fixture()
        self.manifest["policy"] = "observe"
        self.write()
        with self.assertRaisesRegex(capture.CaptureValidationError,"contract"):
            capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)

    def test_completion_checks_executable_permissions(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        program = dest/"variant"/"program"
        program.chmod(program.stat().st_mode & ~0o111)
        self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_completion_requires_file_roles_and_executable_metadata(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        path = dest/"variant"/"capture-complete.json"
        original = path.read_bytes()
        for change in [dict(role="debug"), dict(executable=None)]:
            with self.subTest(change=change):
                completion = json.loads(original)
                completion["files"]["program"].update(change)
                path.write_bytes(capture._canonical(completion))
                self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_completion_rejects_unindexed_directory_symlinks(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        os.symlink(self.root,dest/"variant"/"unexpected-directory")
        self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_malformed_completion_is_incomplete(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        path = dest/"variant"/"capture-complete.json"
        for completion in [None, [], dict(files={"program": []},schema=1,complete=True,label="unit",drv="unit.drv",contract=contract)]:
            with self.subTest(completion=completion):
                path.write_bytes(capture._canonical(completion))
                self.assertFalse(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))

    def test_interrupted_publication_retains_previous_valid_generation(self):
        store,dest,contract = self.publication_fixture()
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        with mock.patch.object(capture.shutil,"copyfile",side_effect=OSError("injected full disk")):
            with self.assertRaises(OSError):
                capture.stage_captured_variant(store,dest,"variant",label="unit",drv="new.drv",contract=contract)
        self.assertTrue(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))
        self.assertFalse(list(dest.glob(".variant.capture-*")))

    def test_legacy_files_and_wrong_configuration_cannot_complete_capture(self):
        store,dest,contract = self.publication_fixture()
        self.assertFalse(capture.captured_variant_complete(store,label="unit",drv="unit.drv",contract=contract))
        contract["backend"] = "clang-trace"
        with self.assertRaisesRegex(capture.CaptureValidationError,"configuration"):
            capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)

    def test_publication_rejects_directory_traversal(self):
        store,dest,contract = self.publication_fixture()
        with self.assertRaises(capture.CaptureValidationError):
            capture.stage_captured_variant(store,dest,"../escape",label="unit",drv="unit.drv",contract=contract)

    def test_published_non_unicode_elf_name_is_reversible(self):
        store,dest,contract=self.publication_fixture()
        (store/"elf"/"program").rename(store/"elf"/os.fsdecode(b"program\xff"))
        capture.stage_captured_variant(store,dest,"variant",label="unit",drv="unit.drv",contract=contract)
        self.assertTrue(capture.captured_variant_complete(dest/"variant",label="unit",drv="unit.drv",contract=contract))
        self.assertIn(b"program\xff",[os.fsencode(p.name) for p in (dest/"variant").iterdir()])


if __name__ == "__main__":
    unittest.main()

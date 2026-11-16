"""Real ELF and correctly rehashed negative cases for capture publication v2."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
VALIDATOR = Path(os.environ.get("CAPTURE_VALIDATOR", REPO/"python/compiler_suit_runner/capture.py"))
spec = importlib.util.spec_from_file_location("debug_provenance_capture", VALIDATOR)
capture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = capture
spec.loader.exec_module(capture)
BINARY = os.environ.get("CAPTURE_BINARY")
GCC = os.environ.get("CAPTURE_GCC")
CLANG = os.environ.get("CAPTURE_CLANG")


def rehash_manifest(root, mutate):
    path = root/"manifest.json"
    document = json.loads(path.read_bytes())
    for entry in document["invocations"]:
        record = entry["record"]
        mutate(record)
        config = record["compiler"]
        fields = capture.CONFIG_FIELDS + (["wrapper"] if "wrapper" in config else [])
        record["config_sha256"] = hashlib.sha256(json.dumps({name: config[name] for name in fields},
            separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        entry["id"] = hashlib.sha256(capture._canonical(record)).hexdigest()
    path.write_bytes(capture._canonical(document) + b"\n")


class DebugReaderTests(unittest.TestCase):
    def test_invalid_or_truncated_elf_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"invalid.debug"
            for raw in [b"", b"\x7fELF", b"\x7fELF\x02\x01" + b"\0"*58]:
                path.write_bytes(raw)
                with self.subTest(length=len(raw)), self.assertRaises(capture.CaptureValidationError):
                    capture._debug_inspect(path)

    def test_source_links_are_not_followed_and_debug_aliases_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/"debug"
            root.mkdir()
            outside = Path(temporary)/"outside.debug"
            outside.write_bytes(b"not a debug ELF")
            (root/"ignored.source").symlink_to(outside)
            self.assertEqual(capture._debug.debug_files(root), [])
            (root/"alias").symlink_to(outside)
            with self.assertRaisesRegex(capture._debug.DebugError, "escaping"):
                capture._debug.debug_files(root)


@unittest.skipUnless(BINARY and GCC and CLANG, "real capture supervisor and GCC/Clang are required")
class DebugFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()

    def make_folder(self, *, family="gcc", split=False, lto=False, compression=None, value=0, debuglink=False, assembler_options=()):
        self.counter += 1
        root = self.root/("build-" + str(self.counter))
        root.mkdir()
        driver = GCC if family == "gcc" else CLANG
        version = "15.2.0" if family == "gcc" else "21.1.8"
        identity = dict(family=family, version=version, target="x86_64-unknown-linux-gnu",
            wrapper=str(Path(driver).parent.parent), bintools_wrapper="/nix/store/test-binutils", bintools_version="2.46")
        config = dict(schema=1, wrapper_revision=1, executable=driver, family=family, version=version,
            target=identity["target"], driver=family, backend=family + "-trace", supervisor=BINARY,
            debug_policy="required", nix_response_default=False, wrapper=identity["wrapper"])
        (root/"config.json").write_text(json.dumps(config))
        (root/"unit.c").write_text(f"volatile int result={value};int main(void){{return result;}}\n")
        stage, retained = root/"stage", root/"capture"
        subprocess.run([BINARY, "init", stage], check=True, capture_output=True)
        flags = (["-gsplit-dwarf"] if split else []) + (["-flto"] if lto else []) + list(assembler_options)
        call = subprocess.run([BINARY, "--config", str(root/"config.json"), "--", "-O2", "-g", *flags,
            "-Wl,--build-id=sha1", "unit.c", "-o", "program"], cwd=root, capture_output=True,
            env=dict(os.environ, DATASET_CAPTURE_STAGE=str(stage), DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")), timeout=45)
        self.assertEqual(call.returncode, 0, call.stderr)
        call = subprocess.run([BINARY, "finalize", stage, retained, "required"], capture_output=True, timeout=30)
        self.assertEqual(call.returncode, 0, call.stderr)
        info = capture.elf_information(root/"program")
        self.assertTrue(info["build_ids"])
        identity_hash = info["build_ids"][0]
        debug = root/"debug"/"lib/debug/.build-id"/identity_hash[:2]/(identity_hash[2:] + ".debug")
        debug.parent.mkdir(parents=True)
        subprocess.run(["objcopy", "--only-keep-debug", root/"program", debug], check=True, capture_output=True)
        if compression:
            subprocess.run(["objcopy", "--compress-debug-sections=" + compression, debug], check=True, capture_output=True)
        (debug.parent/(identity_hash[2:] + ".executable")).symlink_to(root/"program")
        (debug.parent/(identity_hash[2:] + ".source")).symlink_to(root/"unit.c")
        (root/"debug/lib/debug/program").symlink_to(debug)
        installed = root/"installed"
        installed.mkdir()
        shutil.copy2(root/"program", installed/"program")
        subprocess.run(["strip", installed/"program"], check=True, capture_output=True)
        if debuglink:
            subprocess.run(["objcopy", "--add-gnu-debuglink=" + str(debug), installed/"program"], check=True, capture_output=True)
        folder = root/"folder"
        (folder/"elf").mkdir(parents=True)
        (folder/"elf/program").symlink_to(installed/"program")
        (folder/"auxiliary").mkdir()
        (folder/"auxiliary/datasetCapture").symlink_to(retained)
        (folder/"auxiliary/debug").symlink_to(root/"debug")
        contract = capture.required_contract(family)
        metadata = dict(package="fixture", arch="x86_64", compiler=family, compilerFamily=family,
            compilerVersion=version, compilerCapture=contract, expectedToolchain=identity)
        provenance = dict(schema=1, label="fixture", package=dict(label="fixture", pname="fixture", version="1.0"),
            toolchain=identity, build=dict(package_drv="/nix/store/fixture-package.drv", flake_lock_sha256="a"*64,
                debug_policy=dict(separate_debug_info=True, hook_compatibility=None),
                outputs={"out":dict(path="/nix/store/fixture-out", role="package"),
                    "debug":dict(path="/nix/store/fixture-debug", role="debug"),
                    "datasetCapture":dict(path="/nix/store/fixture-capture", role="compiler-capture")},
                source=dict(path="/nix/store/fixture-source", revision=None)))
        metadata["captureProvenance"] = provenance
        (folder/"meta.json").write_text(json.dumps(metadata))
        (folder/"provenance.json").write_text(json.dumps(provenance))
        return folder, retained, contract

    def stage(self, folder, contract, destination=None, metadata=None):
        destination = destination or self.root/"staging"
        return capture.stage_captured_variant(folder, destination, "variant", label="fixture",
            drv="/nix/store/fixture-folder.drv", contract=contract, metadata=metadata)

    def complete(self, root, contract):
        return capture.captured_variant_complete(root, label="fixture", drv="/nix/store/fixture-folder.drv", contract=contract)

class RealDebugTests(DebugFixture, unittest.TestCase):
    def test_nix_assembler_compression_retains_native_clang_emission(self):
        for options in [["-Wa,--compress-debug-sections"],
                ["-Wa,--fatal-warnings,--compress-debug-sections=zlib"],
                ["-Xassembler", "--compress-debug-sections"]]:
            with self.subTest(options=options):
                folder, retained, contract = self.make_folder(family="clang", assembler_options=options)
                verified = capture.validate_capture(retained)
                self.assertTrue(any(entry["record"].get("native_from_ir") for entry in verified.manifest["invocations"]))
                self.stage(folder, contract)
                self.assertTrue(self.complete(self.root/"staging/variant", contract))

    def test_gcc_debug_is_associated_and_copy_is_store_independent(self):
        folder, retained, contract = self.make_folder()
        paths, summary = self.stage(folder, contract)
        self.assertEqual(summary["provenance"]["package"]["version"], "1.0")
        self.assertIsNone(summary["provenance"]["build"]["source"]["revision"])
        self.assertEqual(len(summary["debug_associations"]["program"]), 1)
        self.assertTrue(all(not path.is_symlink() for path in paths))
        original = folder.parent
        shutil.rmtree(original)
        self.assertTrue(self.complete(self.root/"staging/variant", contract))
        self.assertEqual(subprocess.run([self.root/"staging/variant/program"]).returncode, 0)

    def test_compressed_clang_split_dwarf_has_explicit_companion_lookup(self):
        folder, _, contract = self.make_folder(family="clang", split=True, compression="zlib")
        _, summary = self.stage(folder, contract)
        lookup = summary["debug_associations"]["program"][0]["companions"]
        self.assertTrue(lookup)
        for identity, relative in lookup.items():
            units = capture._debug_inspect(self.root/"staging/variant"/relative)["split_units"]
            self.assertIn(identity, [unit["dwo_id"] for unit in units])
        shutil.rmtree(folder.parent)
        self.assertTrue(self.complete(self.root/"staging/variant", contract))

    def test_gcc_lto_split_dwarf_companions_are_associated(self):
        folder, _, contract = self.make_folder(split=True, lto=True)
        _, summary = self.stage(folder, contract)
        self.assertTrue(summary["debug_associations"]["program"][0]["companions"])
        self.assertTrue(self.complete(self.root/"staging/variant", contract))

    def test_correctly_rehashed_wrong_compiler_target_and_wrapper_are_rejected(self):
        folder, retained, contract = self.make_folder()
        original = (retained/"manifest.json").read_bytes()
        for key, value in [("version", "14.0.0"), ("target", "aarch64-unknown-linux-gnu"), ("wrapper", "/nix/store/foreign-wrapper")]:
            with self.subTest(field=key):
                (retained/"manifest.json").write_bytes(original)
                rehash_manifest(retained, lambda record: record["compiler"].update({key: value}))
                capture.validate_capture(retained)
                with self.assertRaisesRegex(capture.CaptureValidationError, "selected compiler/target/wrapper"):
                    self.stage(folder, contract)

    def test_rehashed_substituted_companion_is_not_accepted(self):
        folder, retained, contract = self.make_folder(split=True)
        _, other, _ = self.make_folder(split=True, value=1)
        foreign = capture.validate_capture(other)
        replacement = next(a for entry in foreign.manifest["invocations"] for a in entry["record"]["artifacts"] if a["role"] == "debug-companion")
        data = (other/"objects"/replacement["sha256"]).read_bytes()
        document = json.loads((retained/"manifest.json").read_bytes())
        old = next(a["sha256"] for entry in document["invocations"] for a in entry["record"]["artifacts"] if a["role"] == "debug-companion")
        document["artifacts"].pop(old)
        document["artifacts"][replacement["sha256"]] = len(data)
        (retained/"objects"/old).unlink()
        (retained/"objects"/replacement["sha256"]).write_bytes(data)
        (retained/"manifest.json").write_bytes(capture._canonical(document))
        def substitute(record):
            for artifact in record["artifacts"]:
                if artifact["sha256"] == old:
                    artifact.update(sha256=replacement["sha256"], size=len(data))
        rehash_manifest(retained, substitute)
        capture.validate_capture(retained)
        with self.assertRaisesRegex(capture.CaptureValidationError, "matching split-DWARF companion"):
            self.stage(folder, contract)

    def test_wrong_debug_file_missing_debug_and_wrong_request_are_rejected(self):
        folder, _, contract = self.make_folder()
        for field, value in [("compiler_version", "14"), ("arch", "aarch64"), ("pkg", "other")]:
            with self.subTest(field=field), self.assertRaisesRegex(capture.CaptureValidationError, "requested"):
                capture.stage_captured_variant(folder, self.root/"stage-request", "variant", label="fixture",
                    drv="/nix/store/fixture-folder.drv", contract=contract, expected={field:value})
        debug = folder.parent/"debug"
        for path in debug.rglob("*.debug"):
            path.write_bytes(b"not DWARF")
        with self.assertRaises(capture.CaptureValidationError):
            self.stage(folder, contract)
        shutil.rmtree(debug)
        with self.assertRaisesRegex(capture.CaptureValidationError, "no validated separate debug"):
            self.stage(folder, contract)

    def test_debuglink_crc_and_package_provenance_are_checked(self):
        folder, _, contract = self.make_folder(debuglink=True)
        self.stage(folder, contract)
        self.assertTrue(self.complete(self.root/"staging/variant", contract))
        provenance_path = folder/"provenance.json"
        original = provenance_path.read_bytes()
        provenance = json.loads(original)
        provenance["package"]["version"] = "different"
        provenance_path.write_text(json.dumps(provenance))
        with self.assertRaisesRegex(capture.CaptureValidationError, "contradicts"):
            self.stage(folder, contract)
        provenance_path.write_bytes(original)
        program = folder.parent/"installed/program"
        sections = capture._debug.Sections(program)
        row = sections.other[".gnu_debuglink"]
        sections.close()
        data = bytearray(program.read_bytes())
        data[row[4] + row[5] - 1] ^= 1
        program.write_bytes(data)
        with self.assertRaisesRegex(capture.CaptureValidationError, "no validated separate debug"):
            self.stage(folder, contract)


if __name__ == "__main__":
    unittest.main()

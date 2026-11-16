import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_validation

CLI = Path(os.environ.get("CAPTURE_INSPECTOR",Path(__file__).parents[2]/"python/compiler_suit_runner/capture_cli.py"))


class CaptureCliTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_validation.ValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def run_cli(self,*args):
        return subprocess.run([sys.executable,"-B",str(CLI),*map(str,args)],
            capture_output=True,text=True,timeout=10)

    def test_raw_capture_summary(self):
        result = self.run_cli(self.root,"--json")
        self.assertEqual(result.returncode,0,result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["kind"],"capture-output")
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["invocation_count"],2)
        self.assertEqual(summary["artifact_count"],3)
        self.assertEqual(summary["artifact_references_by_role"],dict(assembly=2,object=2,preprocessing=2))
        self.assertEqual(summary["compilers"][0]["version"],"15.2.0")
        self.assertIsNone(summary["installed_elf_count"])

    def test_text_summary(self):
        result = self.run_cli(self.root)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("capture-output: complete",result.stdout)
        self.assertIn("2 invocations, 3 artifacts",result.stdout)

    def test_incomplete_capture_requires_explicit_diagnostic_mode(self):
        self.fixture.manifest["complete"] = False
        self.fixture.manifest["errors"] = ["capture quota exceeded"]
        self.fixture.write()
        result = self.run_cli(self.root)
        self.assertEqual(result.returncode,1)
        self.assertIn("incomplete",result.stderr)
        result = self.run_cli(self.root,"--allow-incomplete","--json")
        self.assertEqual(result.returncode,0,result.stderr)
        summary = json.loads(result.stdout)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["errors"],["capture quota exceeded"])

    def test_corrupt_artifact_never_passes_diagnostic_mode(self):
        artifact = next((self.root/"objects").iterdir())
        artifact.write_bytes(b"x"*artifact.stat().st_size)
        for flags in [[],["--allow-incomplete"]]:
            result = self.run_cli(self.root,*flags)
            self.assertEqual(result.returncode,1)
            self.assertIn("checksum",result.stderr)

    def test_folder_contract_is_validated(self):
        store,_,contract = self.fixture.publication_fixture()
        (store/"meta.json").write_text(json.dumps(dict(compilerCapture=contract)))
        result = self.run_cli(store,"--json")
        self.assertEqual(result.returncode,0,result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["kind"],"dataset-folder")
        self.assertEqual(summary["installed_elf_count"],1)
        contract["backend"] = "clang-trace"
        (store/"meta.json").write_text(json.dumps(dict(compilerCapture=contract)))
        result = self.run_cli(store)
        self.assertEqual(result.returncode,1)
        self.assertIn("contract",result.stderr)

    def test_required_folder_associations_cannot_be_bypassed(self):
        store,_,contract = self.fixture.publication_fixture()
        contract["required_elf_associations"] = True
        (store/"meta.json").write_text(json.dumps(dict(compilerCapture=contract)))
        result = self.run_cli(store,"--allow-incomplete")
        self.assertEqual(result.returncode,1)
        self.assertIn("pre-install association",result.stderr)

    def test_published_variant_checks_completion_and_permissions(self):
        store,destination,contract = self.fixture.publication_fixture()
        test_validation.capture.stage_captured_variant(store,destination,"variant",label="unit",drv="unit.drv",contract=contract)
        result = self.run_cli(destination/"variant","--json")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"],"published")
        (destination/"variant"/"program").chmod(0o444)
        result = self.run_cli(destination/"variant","--allow-incomplete")
        self.assertEqual(result.returncode,1)
        self.assertIn("completion validation",result.stderr)

    def test_missing_or_malformed_input_is_a_concise_error(self):
        result = self.run_cli(self.root/"missing")
        self.assertEqual(result.returncode,1)
        self.assertNotIn("Traceback",result.stderr)
        (self.root/"manifest.json").write_text("null")
        result = self.run_cli(self.root)
        self.assertEqual(result.returncode,1)
        self.assertNotIn("Traceback",result.stderr)

    def test_running_does_not_import_the_runner_package(self):
        # A module probe fails if importing the command needs template_graph
        # or dynamic-runner. Neither is on this subprocess's script path.
        result = self.run_cli("--help")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("dataset folder",result.stdout)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import json

spec = importlib.util.spec_from_file_location("batch", Path(__file__).with_name("batch.py"))
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)


class BatchTests(unittest.TestCase):
    def test_private_store_rechecks_review_before_starting_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            reviewed=root/"reviewed.json"
            approved="/nix/store/"+"a"*32+"-fixture.drv"
            unexpected="/nix/store/"+"b"*32+"-unexpected-compiler.drv"
            reviewed.write_text(json.dumps([approved]))
            response=dict(status=0,stdout="",stderr="this derivation will be built:\n  "+unexpected+"\n",
                stream_bytes=dict(stdout=0,stderr=100))
            with mock.patch.dict(os.environ,CAPTURE_REVIEWED_DRVS_JSON=str(reviewed)), mock.patch.object(batch,"bounded_run",return_value=response) as run:
                self.assertEqual(batch.inside_build(approved,"out",root/"report.json"),1)
                self.assertEqual(run.call_count,1)
                self.assertIn("--dry-run",run.call_args[0][0])
            report=json.loads((root/"report.json").read_text())
            self.assertEqual(report["unexpected"],[unexpected])
    def test_matrix_group_validates_later_modes_after_a_failure(self):
        previous=Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            settings=root/"cases.json"
            settings.write_text(json.dumps(dict(derivations={"smoke":"smoke.drv","lto":"lto.drv","thinlto":"thin.drv"},
                defaults="out",reports={name:str(root/(name+".json")) for name in ["smoke","lto","thinlto"]})))
            calls=[]
            def validate(drv, defaults, report):
                self.assertFalse(Path("validation-elf").exists())
                Path("validation-elf").mkdir()
                calls.append((drv,defaults,report))
                return drv=="lto.drv"
            try:
                os.chdir(root)
                with mock.patch.object(batch,"inside_build",side_effect=validate):
                    self.assertEqual(batch.inside_matrix(settings),1)
            finally:os.chdir(previous)
            self.assertEqual([call[0] for call in calls],["smoke.drv","lto.drv","thin.drv"])

    def test_logs_are_drained_and_bounded(self):
        result = batch.bounded_run([sys.executable, "-c",
            "import os; os.write(1,b'x'*200000); os.write(2,b'y'*200000)"])
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["stream_bytes"], dict(stdout=200000, stderr=200000))
        self.assertEqual(len(result["stdout"]), batch.LIMITS["log_bytes"])
        self.assertEqual(len(result["stderr"]), batch.LIMITS["log_bytes"])

    def test_timeout_kills_processes_holding_the_output_pipe(self):
        result = batch.bounded_run([sys.executable, "-c",
            "import os,time; os.fork(); time.sleep(30)"], timeout=0.05)
        self.assertTrue(result["timed_out"])
        self.assertLess(result["status"], 0)

    def test_allocated_storage_and_hardlinked_inodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").write_bytes(b"x"*4096)
            os.link(root / "a", root / "b")
            (root / "dir").mkdir()
            os.symlink(root / "dir", root / "symlink")
            usage = batch.usage(root)
            self.assertEqual(usage["entries"], 5)
            self.assertEqual(usage["inodes"], 4)
            self.assertEqual(usage["allocated_bytes"], sum(
                p.lstat().st_blocks*512 for p in [root, root/"a", root/"dir", root/"symlink"]))

    def test_large_manifests_keep_bounded_failure_evidence(self):
        result=dict(manifest=dict(schema=1,wrapper_revision=1,complete=False,policy="required",errors=["incomplete"],
            artifacts={},invocations=[dict(count=1,record=dict(capture_complete=False,capture_errors=["fixture failure"],
                arguments_hex=["ab"*4096])) for _ in range(200)]))
        batch.compact_report(result)
        self.assertTrue(result["manifest_excerpt_only"])
        self.assertEqual(result["manifest_summary"]["invocation_count"],200)
        self.assertEqual(result["capture_error_counts"]["fixture failure"],200)
        import json
        self.assertLess(len(json.dumps(result)),batch.LIMITS["metadata_file_bytes"])


if __name__ == "__main__":
    unittest.main()

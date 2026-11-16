import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

BINARY = os.environ.get("CAPTURE_BINARY")


@unittest.skipUnless(BINARY, "CAPTURE_BINARY must name the built Rust supervisor")
class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stage = self.root/"stage"
        self.output = self.root/"capture"
        self.compiler = self.root/"compiler"
        self.compiler.write_text(f"#!{sys.executable}\n" + '''
import os, pathlib, signal, subprocess, sys
with open("calls", "ab") as file: file.write(b"1")
pathlib.Path("unit.o").write_bytes(b"original-object"*int(os.environ.get("REPEATS", "1")))
if os.environ.get("TREE"):
    child = subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=bool(os.environ.get("ESCAPE")))
    pathlib.Path("child.pid").write_text(str(child.pid))
    signal.pause()
if os.environ.get("SIGNAL"):
    os.kill(os.getpid(),int(os.environ["SIGNAL"]))
sys.exit(int(os.environ.get("STATUS", "0")))
''')
        self.compiler.chmod(0o755)
        (self.root/"unit.c").write_text("fixture source")
        self.config = self.root/"config.json"
        self.config.write_text(json.dumps(dict(schema=1, wrapper_revision=1,
             executable=str(self.compiler),family="gcc",version="fixture",target="fixture",
             driver="c",backend="observe",supervisor=BINARY)))
        self.command = [BINARY,"--config",str(self.config),"--","-c","unit.c","-o","unit.o"]
        subprocess.run([BINARY,"init",self.stage],check=True)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, **extra):
        return subprocess.run(self.command,cwd=self.root,
            env=dict(os.environ,DATASET_CAPTURE_STAGE=str(self.stage),**extra),capture_output=True,timeout=10)

    def finalize(self, policy="observe"):
        result = subprocess.run([BINARY,"finalize",self.stage,self.output,policy],capture_output=True,timeout=10)
        return result,json.loads((self.output/"manifest.json").read_text())

    def test_snapshot_survives_mutation_and_deletion(self):
        result = self.invoke()
        self.assertEqual(result.returncode,0)
        (self.root/"unit.o").write_bytes(b"mutated")
        (self.root/"unit.o").unlink()
        status, manifest = self.finalize()
        self.assertEqual(status.returncode,0)
        digest = hashlib.sha256(b"original-object").hexdigest()
        self.assertEqual((self.output/"objects"/digest).read_bytes(),b"original-object")
        self.assertIn(digest,manifest["artifacts"])
        self.assertFalse(manifest["complete"])

    def test_duplicate_snapshots_refund_only_removed_copies(self):
        for _ in range(3):
            result=self.invoke(REPEATS="20000",DATASET_CAPTURE_LIMIT="1179648")
            self.assertEqual(result.returncode,0,result.stderr)
        _,manifest=self.finalize()
        self.assertEqual(sum(entry["count"] for entry in manifest["invocations"]),3)
        self.assertEqual(len(manifest["artifacts"]),1)
        for entry in manifest["invocations"]:
            self.assertTrue(entry["record"]["artifacts"])
            self.assertFalse(any("reservation limit" in error for error in entry["record"]["capture_errors"]))

    def test_capture_reservation_failure_keeps_compiler_success(self):
        result = self.invoke(DATASET_CAPTURE_LIMIT="1")
        self.assertEqual(result.returncode,0)
        self.assertEqual((self.root/"calls").read_bytes(),b"1")
        status, manifest = self.finalize("required")
        self.assertNotEqual(status.returncode,0)
        self.assertFalse(manifest["complete"])
        self.assertIn("invocations were lost or interrupted",manifest["errors"])

    def test_compiler_failure_is_recorded_without_retry(self):
        self.assertEqual(self.invoke(STATUS="37").returncode,37)
        _,manifest = self.finalize()
        record = manifest["invocations"][0]["record"]
        self.assertEqual(record["compiler_status"]["exit_code"],37)

    def test_large_valid_argument_retains_a_durable_record(self):
        argument="-DNAME="+"x"*50000
        self.command.append(argument)
        result=self.invoke(DATASET_CAPTURE_LIMIT=str(1024**2))
        self.assertEqual(result.returncode,0,result.stderr)
        status,manifest=self.finalize()
        self.assertEqual(status.returncode,0,status.stderr)
        self.assertEqual(len(manifest["invocations"]),1)
        self.assertEqual(manifest["invocations"][0]["record"]["arguments_hex"][-1],argument.encode().hex())
        self.assertNotIn("invocations were lost or interrupted",manifest["errors"])

    def test_explicit_measured_artifact_budget(self):
        result=self.invoke(REPEATS="1200000",DATASET_CAPTURE_LIMIT=str(64*1024**2),
            DATASET_CAPTURE_ARTIFACT_LIMIT=str(32*1024**2),DATASET_CAPTURE_INVOCATION_LIMIT=str(64*1024**2))
        self.assertEqual(result.returncode,0,result.stderr)
        status,manifest=self.finalize()
        self.assertEqual(status.returncode,0,status.stderr)
        record=manifest["invocations"][0]["record"]
        self.assertEqual(record["limits"]["artifact_bytes"],32*1024**2)
        self.assertEqual(len(record["artifacts"]),1)
        self.assertGreater(record["artifacts"][0]["size"],16*1024**2)
        self.assertFalse(any("byte limit" in error or "reservation limit" in error for error in record["capture_errors"]))
        self.assertEqual((self.root/"calls").read_bytes(),b"1")

    def test_repeated_invocations_retain_multiplicity(self):
        self.assertEqual(self.invoke().returncode,0)
        self.assertEqual(self.invoke().returncode,0)
        _,manifest = self.finalize()
        self.assertEqual(len(manifest["invocations"]),1)
        self.assertEqual(manifest["invocations"][0]["count"],2)
        self.assertEqual(len(manifest["artifacts"]),1)

    def test_artifact_corruption_cannot_finalize_as_complete(self):
        self.invoke()
        artifact = next((self.stage/"objects").iterdir())
        artifact.write_bytes(b"corrupt")
        result = subprocess.run([BINARY,"finalize",self.stage,self.output,"required"],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.output/"manifest.json").exists())

    def test_supervised_signal_status(self):
        self.assertEqual(self.invoke(SIGNAL=str(signal.SIGTERM)).returncode,-signal.SIGTERM)

    def test_parallel_reservations_keep_successful_compiler_status(self):
        env = dict(os.environ,DATASET_CAPTURE_STAGE=str(self.stage),DATASET_CAPTURE_LIMIT=str(1024**2),REPEATS="30000")
        processes = [subprocess.Popen(self.command,cwd=self.root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                     for _ in range(4)]
        for process in processes:
            _,stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode,0,stderr)
        _,manifest = self.finalize()
        self.assertFalse(manifest["complete"])
        for digest,size in manifest["artifacts"].items():
            data = (self.output/"objects"/digest).read_bytes()
            self.assertEqual(len(data),size)
            self.assertEqual(hashlib.sha256(data).hexdigest(),digest)
        allocated = sum(path.stat().st_blocks*512 for path in self.output.rglob("*") if path.is_file())
        self.assertLessEqual(allocated,1024**2)

    def cancellation_case(self,escape=False):
        env = dict(os.environ,DATASET_CAPTURE_STAGE=str(self.stage),TREE="1",ESCAPE="1" if escape else "")
        process = subprocess.Popen(self.command,cwd=self.root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+5
            while not (self.root/"child.pid").exists() and time.monotonic()<deadline:
                time.sleep(0.01)
            self.assertTrue((self.root/"child.pid").exists())
            child = int((self.root/"child.pid").read_text())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5),-signal.SIGTERM)
            status = Path(f"/proc/{child}/stat")
            if status.exists():
                self.assertIn(status.read_text().split()[2],["Z","X"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_cancellation_reaches_compiler_subprocess(self):
        self.cancellation_case()

    def test_cancellation_reaps_an_escaped_compiler_subprocess(self):
        self.cancellation_case(escape=True)


if __name__ == "__main__":
    unittest.main()

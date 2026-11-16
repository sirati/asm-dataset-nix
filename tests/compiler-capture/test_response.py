import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

BINARY=os.environ.get("CAPTURE_BINARY")


@unittest.skipUnless(BINARY,"CAPTURE_BINARY must name the supervisor")
class PipeResponseTests(unittest.TestCase):
    def run_response(self,content,driver="c",family="gcc",version="fixture"):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            stage=root/"stage"
            subprocess.run([BINARY,"init",stage],check=True)
            compiler=root/"compiler"
            compiler.write_text(f"#!{sys.executable}\n"+'''
import pathlib,sys
data=pathlib.Path(sys.argv[1][1:]).read_bytes()
sys.stderr.buffer.write(data)
sys.stdout.buffer.write(sys.stdin.buffer.read())
sys.exit(37)
''')
            compiler.chmod(0o755)
            config=root/"config"
            config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=str(compiler),family=family,
                version=version,target="fixture",driver=driver,backend="observe",supervisor=BINARY,nix_response_default=True)))
            reader,writer=os.pipe()
            def produce():
                with os.fdopen(writer,"wb") as pipe:
                    pipe.write(content)
            producer=threading.Thread(target=produce)
            producer.start()
            try:
                result=subprocess.run([BINARY,"--config",config,"--",f"@/dev/fd/{reader}"],
                    cwd=root,env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage)),pass_fds=(reader,),
                    input=b"original stdin\x00\xff",capture_output=True,timeout=15)
            finally:
                os.close(reader)
                producer.join(timeout=5)
            self.assertFalse(producer.is_alive())
            self.assertEqual(result.returncode,37)
            self.assertEqual(result.stdout,b"original stdin\x00\xff")
            self.assertEqual(result.stderr,content)
            subprocess.run([BINARY,"finalize",stage,root/"capture","observe"],check=True)
            manifest=json.loads((root/"capture/manifest.json").read_text())
            return manifest

    def test_pipe_is_snapshotted_without_consuming_compiler_arguments(self):
        content=b"-E\n'unit with spaces.c'\n-DVALUE=\\\"value\\\"\n"
        manifest=self.run_response(content)
        self.assertTrue(any(a["role"]=="response" and a["sha256"]==hashlib.sha256(content).hexdigest()
            for entry in manifest["invocations"] for a in entry["record"]["artifacts"]))

    def test_analysis_limit_streams_the_entire_original_response(self):
        manifest=self.run_response(b"argument\n"*150000)
        self.assertFalse(manifest["complete"])
        self.assertTrue(any("response exceeds" in error for entry in manifest["invocations"]
            for error in entry["record"]["capture_errors"]))

    def test_configured_cpp_entry_has_implicit_preprocessing_mode(self):
        manifest=self.run_response(b"unit.c\n",driver="target-cpp")
        self.assertEqual(manifest["invocations"][0]["record"]["analysis"]["mode"],"preprocess")

    def test_legacy_wrapper_quotes_preserve_argument_bytes(self):
        helper=os.environ.get("CAPTURE_RESPONSE_QUOTE",str(Path(__file__).parents[2]/"lib/legacy-response-quote.sh"))
        arguments=[b"-c",b"path with spaces.c",b"back\\slash.c",b'quote"source.c',b"nl\nsource.c",b"\xff.c",b"-Wl,-Map,unit.map",b""]
        encoded=subprocess.run([b"bash",b"-c",b'source "$1"; shift; _datasetLegacyClangResponse "$@"',
            b"fixture",os.fsencode(helper),*arguments],check=True,capture_output=True).stdout
        manifest=self.run_response(encoded,family="clang",version="3.4.2")
        record=manifest["invocations"][0]["record"]
        self.assertEqual(record["analysis"]["response_dialect"],"clang-before-3.9")
        self.assertEqual(record["analysis"]["inputs"],[list(value) for value in arguments[1:6]+[b""]])


if __name__=="__main__":
    unittest.main()

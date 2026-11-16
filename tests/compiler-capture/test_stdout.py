import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BINARY = os.environ.get("CAPTURE_BINARY")


@unittest.skipUnless(BINARY,"CAPTURE_BINARY is required")
class StdoutTests(unittest.TestCase):
    def run_stream(self,data,*,large=False):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage,output = root/"stage",root/"output"
            program = root/"program"
            program.write_text(f"#!{sys.executable}\n" +
                ("import os\nfor _ in range(320): os.write(1,b'x'*65536)\n" if large else
                 "import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\nsys.stderr.write('original diagnostic\\n')\n"))
            program.chmod(0o755)
            config = root/"config.json"
            config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=str(program),family="gcc",
                version="fixture",target="fixture",driver="c",backend="observe",supervisor=BINARY)))
            subprocess.run([BINARY,"init",stage],check=True)
            result = subprocess.run([BINARY,"--config",config,"--","-E","-x","c","-"],input=data,
                capture_output=True,timeout=15,env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage)))
            self.assertEqual(result.returncode,0,result.stderr)
            subprocess.run([BINARY,"finalize",stage,output,"observe"],check=True)
            manifest = json.loads((output/"manifest.json").read_text())
            if large:
                self.assertEqual(result.stdout,b"x"*(20*1024**2))
                self.assertFalse(manifest["complete"])
                self.assertEqual(manifest["artifacts"],{})
            else:
                self.assertEqual(result.stdout,data)
                self.assertEqual(result.stderr,b"original diagnostic\n")
                self.assertTrue(manifest["complete"])
                digest = hashlib.sha256(data).hexdigest()
                self.assertEqual((output/"objects"/digest).read_bytes(),data)

    def test_stream_bytes_and_stderr_are_preserved(self):
        self.run_stream(b"stdin\0bytes\xff\n"*10000)

    def test_capture_limit_does_not_truncate_forwarded_stdout(self):
        self.run_stream(b"",large=True)


if __name__ == "__main__":
    unittest.main()

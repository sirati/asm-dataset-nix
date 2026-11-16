"""Actual supervisor transparency tests using a recording executable fixture."""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest

BINARY = os.environ.get("CAPTURE_BINARY")


@unittest.skipUnless(BINARY, "CAPTURE_BINARY must name the built Rust supervisor")
class ForwardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.recorder = self.root/"recorder"
        self.recorder.write_text(f"#!{sys.executable}\n" + '''
import json, os, signal, sys
record = dict(args=[os.fsencode(a).hex() for a in sys.argv[1:]],
              cwd=os.fsencode(os.getcwd()).hex(),
              environment=os.environ["FIXTURE_VALUE"], pgid=os.getpgrp())
with open("record.json", "w") as file: json.dump(record,file)
if "FIXTURE_FD" in os.environ:
    os.write(int(os.environ["FIXTURE_FD"]), b"descriptor-preserved")
if "FIXTURE_SIGNAL" in os.environ:
    os.kill(os.getpid(), int(os.environ["FIXTURE_SIGNAL"]))
sys.stdout.buffer.write(sys.stdin.buffer.read())
sys.stderr.buffer.write(b"original-stderr")
sys.exit(int(os.environ.get("FIXTURE_STATUS", "0")))
''')
        self.recorder.chmod(0o755)
        self.config = self.root/"entry.json"
        self.config.write_text(json.dumps(dict(schema=1, wrapper_revision=1,
            executable=str(self.recorder), family="gcc", version="fixture-1",
            target="fixture", driver="c++", backend="forward",supervisor=BINARY)))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, args, **overrides):
        env = dict(os.environ, FIXTURE_VALUE="value\nwith spaces", **overrides)
        command = [os.fsencode(BINARY), b"--config", os.fsencode(self.config), b"--", *args]
        return subprocess.run(command, cwd=self.root, env=env, input=b"stdin\0bytes",
                              capture_output=True, timeout=10)

    def test_bytes_environment_cwd_and_streams(self):
        args = [b"", b"space arg", b"\xff", b"$()", b"quote'\"\\", b"line\nend"]
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"stdin\0bytes")
        self.assertEqual(result.stderr, b"original-stderr")
        record = json.loads((self.root/"record.json").read_text())
        self.assertEqual(record["args"], [a.hex() for a in args])
        self.assertEqual(record["cwd"], os.fsencode(self.root).hex())
        self.assertEqual(record["environment"], "value\nwith spaces")
        self.assertEqual(record["pgid"], os.getpgrp())

    def test_original_failure_status(self):
        self.assertEqual(self.invoke([], FIXTURE_STATUS="37").returncode, 37)

    def test_signal_wait_status(self):
        result = self.invoke([], FIXTURE_SIGNAL=str(signal.SIGTERM))
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_jobserver_descriptor(self):
        read_fd, write_fd = os.pipe()
        try:
            env = dict(os.environ, FIXTURE_VALUE="ok", FIXTURE_FD=str(write_fd))
            subprocess.run([BINARY,"--config",str(self.config),"--"], cwd=self.root,
                           env=env, input=b"", capture_output=True, pass_fds=(write_fd,), check=True)
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(os.read(read_fd,128), b"descriptor-preserved")
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def test_sigpipe_dispositions(self):
        if not shutil.which("cc"):
            self.skipTest("a C compiler is required for the native signal probe")
        source = self.root/"signal.c"
        source.write_text('#include <signal.h>\nint main(void) { return signal(SIGPIPE,SIG_DFL)==SIG_IGN ? 43:42; }\n')
        executable = self.root/"signal-probe"
        subprocess.run(["cc",source,"-o",executable],check=True,capture_output=True)
        config = json.loads(self.config.read_text())
        config["executable"] = str(executable)
        self.config.write_text(json.dumps(config))
        for restore,expected in [(True,42),(False,43)]:
            result = subprocess.run([BINARY,"--config",str(self.config),"--"],restore_signals=restore)
            self.assertEqual(result.returncode,expected)


if __name__ == "__main__":
    unittest.main()

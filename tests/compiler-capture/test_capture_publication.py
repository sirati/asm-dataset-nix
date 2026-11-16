"""Real publisher tests for shrinking generations and interrupted replacement."""
import importlib.util
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import unittest
from unittest import mock

from test_debug_provenance import DebugFixture, VALIDATOR, capture

spec = importlib.util.spec_from_file_location("generation_publisher", VALIDATOR.with_name("capture_publish.py"))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
try:
    real_publisher = importlib.import_module("dynamic_runner.worker.publish")
except ImportError:
    real_publisher = None


@unittest.skipUnless(real_publisher is not None, "the pinned native publication module is required")
class PublicationTests(DebugFixture, unittest.TestCase):
    def prepare(self, *, split=False, value=0):
        folder, _, contract = self.make_folder(split=split, value=value)
        destination = self.root/"src"/"fixture"
        metadata = dict(label="fixture", pkg="fixture", arch="x86_64", compiler="gcc", compiler_family="gcc",
            compiler_version="15", variant_dir="variant", drv="/nix/store/fixture-folder.drv")
        outputs, summary = self.stage(folder, contract, destination=destination, metadata=metadata)
        sidecar = destination/"variant.json"
        sidecar.write_bytes((destination/"variant/auxiliary/provenance/variant.json").read_bytes())
        return outputs + [sidecar], contract, summary

    def publish(self, outputs, *, before_commit=None):
        (self.root/"dst").mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, DYNRUNNER_PUBLISH_SRC_ROOT=str(self.root/"src"),
                DYNRUNNER_PUBLISH_DST_ROOT=str(self.root/"dst")):
            return publisher.publish_outputs(outputs, self.root/"src", self.root/"dst",
                real_publisher.publish_all, capture.captured_variant_complete, before_commit=before_commit)

    def test_shrinking_file_set_replaces_visible_generation_and_sidecar(self):
        outputs, contract, _ = self.prepare(split=True)
        self.assertTrue(self.publish(outputs))
        visible = self.root/"dst/fixture/variant"
        previous = visible.resolve()
        old_files = set(json.loads((visible/"capture-complete.json").read_bytes())["files"])
        self.assertTrue(self.complete(visible, contract))
        outputs, contract, _ = self.prepare(value=1)
        self.assertTrue(self.publish(outputs))
        new_files = set(json.loads((visible/"capture-complete.json").read_bytes())["files"])
        self.assertTrue(old_files - new_files)
        self.assertNotEqual(visible.resolve(), previous)
        self.assertTrue(self.complete(visible, contract))
        self.assertTrue(previous.exists())
        self.assertEqual({p.relative_to(visible).as_posix() for p in visible.rglob("*") if p.is_file()},
            new_files | {"capture-complete.json"})
        sidecar = self.root/"dst/fixture/variant.json"
        self.assertTrue(sidecar.is_symlink())
        self.assertEqual(json.loads(sidecar.read_bytes())["compiler_capture"]["manifest_sha256"],
            json.loads((visible/"capture-complete.json").read_bytes())["manifest_sha256"])

    def test_sigkill_before_commit_keeps_previous_generation_valid(self):
        outputs, contract, _ = self.prepare()
        self.publish(outputs)
        visible = self.root/"dst/fixture/variant"
        previous = visible.resolve()
        old_bytes = (visible/"capture-complete.json").read_bytes()
        outputs, _, _ = self.prepare(split=True, value=1)
        pid = os.fork()
        if pid == 0:
            try:
                self.publish(outputs, before_commit=lambda: os.kill(os.getpid(), signal.SIGKILL))
            finally:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertEqual(visible.resolve(), previous)
        self.assertEqual((visible/"capture-complete.json").read_bytes(), old_bytes)
        self.assertTrue(self.complete(visible, contract))
        outputs, contract, _ = self.prepare(value=1)
        self.publish(outputs)
        self.assertTrue(self.complete(visible, contract))
        generations = visible.parent/("." + visible.name + ".capture-generations")
        self.assertEqual(len(list(generations.glob("generation-*"))), 2)

    def test_cross_mount_publication_uses_the_real_transport(self):
        destination = self.root/"dst"
        destination.mkdir()
        call = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=32M,nr_inodes=8192", "capture-publish-test", str(destination)], capture_output=True)
        if call.returncode:
            self.skipTest("a bounded private mount namespace is required")
        try:
            self.assertNotEqual(os.stat(destination).st_dev, os.stat(self.root).st_dev)
            outputs, contract, _ = self.prepare(split=True)
            self.publish(outputs)
            visible = destination/"fixture/variant"
            self.assertTrue(self.complete(visible, contract))
            copied = self.root/"copied"
            shutil.copytree(visible, copied)
            shutil.rmtree(self.root/"src")
            self.assertTrue(self.complete(copied, contract))
            self.assertEqual(subprocess.run([copied/"program"]).returncode, 0)
        finally:
            subprocess.run(["umount", str(destination)], check=True, capture_output=True)

    def test_sigkill_at_pointer_and_history_renames_recovers_on_retry(self):
        for phase in ["pointer", "history"]:
            with self.subTest(phase=phase):
                outputs, contract, _ = self.prepare()
                self.publish(outputs)
                visible = self.root/"dst/fixture/variant"
                previous = visible.resolve()
                generations = visible.parent/".variant.capture-generations"
                outputs, _, _ = self.prepare(value=1)
                trigger = visible if phase == "pointer" else generations/"history.json"
                original_replace = publisher.os.replace

                def interrupted_replace(source, destination):
                    if Path(destination) == trigger:
                        os.kill(os.getpid(), signal.SIGKILL)
                    return original_replace(source, destination)

                pid = os.fork()
                if pid == 0:
                    try:
                        with mock.patch.object(publisher.os, "replace", side_effect=interrupted_replace):
                            self.publish(outputs)
                    finally:
                        os._exit(2)
                _, status = os.waitpid(pid, 0)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                self.assertEqual(visible.resolve() == previous, phase == "pointer")
                self.assertTrue(self.complete(visible, contract))
                self.assertTrue(list(visible.parent.glob(".variant.pointer-*")) if phase == "pointer"
                    else list(generations.glob(".history-*")))
                outputs, contract, _ = self.prepare(value=2)
                self.assertTrue(self.publish(outputs))
                self.assertTrue(self.complete(visible, contract))
                self.assertEqual(len(list(generations.glob("generation-*"))), 2)
                self.assertEqual(list(generations.glob(".history-*")), [])
                self.assertEqual(list(generations.glob(".incoming-*")), [])
                self.assertEqual(list(visible.parent.glob(".variant.pointer-*")), [])

    def test_temporary_cleanup_rejects_links_to_unrelated_paths(self):
        outputs, contract, _ = self.prepare()
        self.publish(outputs)
        visible = self.root/"dst/fixture/variant"
        previous = visible.resolve()
        unrelated = self.root/"unrelated"
        unrelated.write_text("keep these bytes")
        generations = visible.parent/".variant.capture-generations"
        for path in [visible.parent/(".variant.pointer-" + "a"*32), generations/(".history-" + "b"*32)]:
            with self.subTest(name=path.name):
                outputs, _, _ = self.prepare(value=1)
                path.symlink_to(unrelated)
                try:
                    with self.assertRaisesRegex(publisher.PublicationError, "abandoned capture"):
                        self.publish(outputs)
                    self.assertEqual(unrelated.read_text(), "keep these bytes")
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(visible.resolve(), previous)
                    self.assertTrue(self.complete(visible, contract))
                finally:
                    path.unlink()


if __name__ == "__main__":
    unittest.main()

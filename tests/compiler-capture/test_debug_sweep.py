"""A debug-hook claim needs actual current evidence for every public pair."""
import copy
from pathlib import Path
import unittest

import debug_sweep


class DebugCoverageTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {"x86_64": [dict(compiler="gcc4_4", family="gcc", version="4.4.7")],
            "ppc64": [dict(compiler="clang21", family="clang", version="21.1.8")]}
        self.sources = dict(files={"lib/capture-overlay.nix":"current", "python/compiler_suit_runner/capture_debug.py":"current"})
        self.report = dict(candidate="/candidate", candidate_binary_sha256="binary", source_identity=self.sources,
            cases=[dict(compiler=entry["compiler"], target=target, mode="normal", status="passed", complete=True,
                manifest_sha256="a"*64, debug_files=1, runtime_ok=True,
                runtime_kind="native" if target=="x86_64" else "emulated", target_abi=True,
                behavior_equal=True, debug_source_mapping=True) for target, entries in self.metadata.items() for entry in entries])

    def summary(self, reports):
        return debug_sweep.summarize(self.metadata, reports, candidate=Path("/candidate"), binary_hash="binary", sources=self.sources)

    def test_missing_pairs_and_empty_reports_cannot_complete_debug_support(self):
        self.assertTrue(self.summary([self.report])["complete"])
        self.assertFalse(self.summary([])["complete"])
        self.report["cases"].pop()
        self.assertEqual(len(self.summary([self.report])["missing"]),1)

    def test_stale_hooks_other_binary_and_missing_runtime_or_debug_are_rejected(self):
        for update in [dict(candidate_binary_sha256="other"), dict(source_identity=dict(files={}))]:
            report=copy.deepcopy(self.report);report.update(update)
            self.assertFalse(self.summary([report])["complete"])
        for update in [dict(debug_files=0),dict(runtime_ok=False),dict(runtime_kind="unavailable"),
                dict(debug_source_mapping=False),dict(behavior_equal=False),dict(status="pending")]:
            report=copy.deepcopy(self.report);report["cases"][0].update(update)
            self.assertFalse(self.summary([report])["complete"])

    def test_retry_replaces_a_refusal_but_cannot_hide_a_later_failure(self):
        failed=copy.deepcopy(self.report);failed["cases"][0]["status"]="failed"
        self.assertTrue(self.summary([failed,self.report])["complete"])
        self.assertFalse(self.summary([self.report,failed])["complete"])

    def test_unsupported_additional_modes_are_not_counted_as_passes(self):
        self.report["cases"].append(dict(self.report["cases"][0],mode="lto"))
        self.assertFalse(self.summary([self.report])["complete"])


if __name__ == "__main__":
    unittest.main()

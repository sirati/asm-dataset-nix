"""A matrix claim must account for missing pairs and current production code."""
import copy
import unittest
import json
from pathlib import Path
import tempfile

import matrix_coverage as coverage


class CoverageTests(unittest.TestCase):
    def test_grouped_report_index_retains_independent_mode_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            files=[]
            for shape in ["smoke","corners"]:
                path=root/(shape+".json");path.write_text(json.dumps(self.report([shape])));files.append(str(path))
            index=root/"index.json";index.write_text(json.dumps(dict(schema=2,reports=files)))
            self.assertEqual([case["shape"] for report in coverage.read_reports(index) for case in report["cases"]],
                ["smoke"]*4+["corners"]*4)
            index.write_text(json.dumps(dict(schema=2,reports=[str(index)])))
            with self.assertRaisesRegex(ValueError,"nested"):coverage.read_reports(index)

    def setUp(self):
        self.metadata = {"x86_64": [dict(compiler="gcc4_4", family="gcc", version="4.4.7"),
            dict(compiler="gcc15", family="gcc", version="15.2.0"),
            dict(compiler="clang3_9", family="clang", version="3.9.1")],
            "aarch64": [dict(compiler="gcc15", family="gcc", version="15.2.0")]}
        self.identity = dict(files={"rust/compiler-capture/src/trace.rs": "current",
            "tests/compiler-capture/matrix_corners.py": "current-fixture"})

    def report(self, shapes):
        return dict(candidate="/candidate", candidate_binary_sha256="binary", source_identity=self.identity,
            cases=[dict(compiler=c, target=t, shape=s, status="passed", complete=True,
                runtime_ok=True,runtime_kind="native" if t=="x86_64" else "emulated",
                manifest_sha256="manifest", corner_cases={"O0": "passed"} if s == "corners" else {})
                for c, t, s in sorted(coverage.expected_cases(self.metadata, shapes))])

    def summarize(self, reports, shapes=coverage.SHAPES):
        return coverage.summarize(self.metadata, shapes, reports, candidate="/candidate",
            binary_sha256="binary", source_identity=self.identity)

    def test_feature_constraints_preserve_every_normal_pair(self):
        result=self.summarize([self.report(coverage.SHAPES)])
        self.assertTrue(result["complete"])
        self.assertEqual(result["shapes"], dict(smoke=4, corners=4, lto=3, thinlto=1))

    def test_smoke_does_not_imply_advanced_coverage(self):
        self.assertFalse(self.summarize([self.report(["smoke"])])["complete"])

    def test_missing_pair_and_empty_reports_are_rejected(self):
        report=self.report(coverage.SHAPES)
        report["cases"].pop()
        self.assertEqual(len(self.summarize([report])["missing"]), 1)
        self.assertFalse(self.summarize([])["complete"])

    def test_pending_failed_and_incomplete_are_rejected(self):
        for update in [dict(status="pending"), dict(status="failed"), dict(complete=False), dict(manifest_sha256=None),
                dict(runtime_ok=False),dict(runtime_kind="unavailable")]:
            report=self.report(coverage.SHAPES)
            report["cases"][0].update(update)
            self.assertFalse(self.summarize([report])["complete"])

    def test_stale_code_and_other_binaries_are_rejected(self):
        for update in [dict(candidate="/older"), dict(candidate_binary_sha256="other"),
                dict(source_identity=dict(files={"rust/compiler-capture/src/trace.rs": "old"}))]:
            report=self.report(coverage.SHAPES)
            report.update(update)
            self.assertFalse(self.summarize([report])["complete"])

    def test_retry_replaces_failure_in_both_directions(self):
        passed=self.report(coverage.SHAPES)
        failed=copy.deepcopy(passed)
        failed["cases"][0]["status"]="failed"
        self.assertTrue(self.summarize([failed, passed])["complete"])
        self.assertFalse(self.summarize([passed, failed])["complete"])

    def test_empty_or_failed_corners_are_rejected(self):
        for corners in [{}, {"O0": "failed"}]:
            report=self.report(["corners"])
            report["cases"][0]["corner_cases"]=corners
            self.assertFalse(self.summarize([report], ["corners"])["complete"])


if __name__ == "__main__":
    unittest.main()

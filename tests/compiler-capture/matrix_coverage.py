"""Require current evidence for every public compiler/target and requested mode.

Smoke success alone cannot satisfy corner-case or LTO coverage. Reports can be
merged after retries; a missing, stale, pending or failed case never counts.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import batch


SHAPES = ("smoke", "corners", "lto", "thinlto")


def read_reports(path):
    report=batch.read_report(path)
    if "cases" in report:return [report]
    children=report.get("reports")
    if report.get("schema")!=2 or not isinstance(children,list) or not 1<=len(children)<=len(SHAPES):
        raise ValueError("invalid matrix report index")
    reports=[batch.read_report(child) for child in children]
    if any("cases" not in child for child in reports):
        raise ValueError("matrix report indexes cannot be nested")
    return reports


def applicable(entry, shape):
    version = tuple(map(int, entry["version"].split(".")))
    if shape == "thinlto":
        return entry["family"] == "clang" and version >= (3, 9)
    if shape == "lto":
        return version >= ((4, 6) if entry["family"] == "gcc" else (3, 7))
    return shape in ("smoke", "corners")


def expected_cases(metadata, shapes):
    return {(entry["compiler"], target, shape) for target, entries in metadata.items()
        for entry in entries for shape in shapes if applicable(entry, shape)}


def coverage_hashes(identity):
    return {name: digest for name, digest in identity["files"].items()
        if not name.startswith("tests/") or Path(name).name in
        ("matrix-fixtures.nix", "matrix_report.py", "matrix_corners.py", "capabilities.py", "ppc_debug.py",
         "batch.py", "sweep.py", "matrix_coverage.py", "isolate.sh")}


def summarize(metadata, shapes, reports, *, candidate, binary_sha256, source_identity):
    expected = expected_cases(metadata, shapes)
    measured, errors = {}, []
    for report in reports:
        if report.get("candidate") != str(candidate) or report.get("candidate_binary_sha256") != binary_sha256:
            errors.append("report belongs to another supervisor binary")
            continue
        if coverage_hashes(report.get("source_identity", {"files": {}})) != coverage_hashes(source_identity):
            errors.append("report uses stale production sources or fixtures")
            continue
        for case in report["cases"]:
            key = (case["compiler"], case["target"], case.get("shape", report.get("shape", "smoke")))
            if key not in expected:
                errors.append(f"unexpected case: {key}")
                continue
            # Later retry reports replace earlier results, including failures.
            measured[key] = case
    missing = sorted(expected - measured.keys())
    failed = sorted(key for key, case in measured.items() if case.get("status") != "passed"
        or case.get("complete") is not True or not case.get("manifest_sha256")
        or case.get("runtime_ok") is not True
        or case.get("runtime_kind") != ("native" if key[1]=="x86_64" else "emulated"))
    corner_statuses = Counter(status for key, case in measured.items() if key[2] == "corners"
        for status in case.get("corner_cases", {}).values())
    for key, case in measured.items():
        if key[2] == "corners" and (not case.get("corner_cases") or any(
                status not in ("passed", "baseline-unsupported") for status in case["corner_cases"].values())):
            failed.append(key)
    return dict(schema=1, complete=bool(expected) and not (missing or failed or errors),
        candidate=str(candidate), candidate_binary_sha256=binary_sha256,
        expected=len(expected), passed=len(expected) - len(missing) - len(set(failed)),
        shapes={shape: sum(key[2] == shape for key in expected) for shape in shapes},
        targets={target: len(entries) for target, entries in metadata.items()},
        missing=missing, failed=sorted(set(failed)), errors=errors,
        corner_case_statuses=dict(corner_statuses))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-tool", type=Path, required=True)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--shapes", choices=SHAPES, nargs="+", default=list(SHAPES))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(batch.checked(["nix", "eval", "--no-write-lock-file", "--json",
        str(batch.REPO) + "#_crossToolchainsMeta.x86_64-linux"]))
    candidate = args.capture_tool.resolve()
    summary = summarize(metadata, args.shapes, [report for path in args.reports for report in read_reports(path)],
        candidate=candidate, binary_sha256=hashlib.sha256((candidate / "bin/compiler-capture").read_bytes()).hexdigest(),
        source_identity=batch.source_identity())
    summary["source_identity"] = batch.source_identity()
    summary["reports"] = list(map(str, args.reports))
    batch.atomic_json(args.report, summary)
    print(json.dumps({key: summary[key] for key in ("complete", "expected", "passed", "shapes", "errors")}))
    return int(not summary["complete"])


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify actual Nix debug splitting for every public compiler/target pair."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import batch
from matrix_coverage import applicable as compiler_mode

MODES = ("normal", "lto", "thinlto", "split", "split-lto")


def applicable(entry, mode):
    if mode == "normal":
        return True
    if mode in ("lto", "thinlto"):
        return compiler_mode(entry, mode)
    version = tuple(map(int, entry["version"].split(".")))
    split = version >= ((4, 8) if entry["family"] == "gcc" else (3, 4))
    return split and (mode != "split-lto" or compiler_mode(entry, "lto"))


def relevant_sources(identity):
    # Runner transport and package catalog adoption have separate tests. This
    # evidence specifically covers compiler hooks, ELF collection and validation.
    return {name: digest for name, digest in identity["files"].items() if name.startswith(("rust/compiler-capture/", "lib/"))
        or name in ["flake.nix", "flake.lock", "python/compiler_suit_runner/capture.py",
            "python/compiler_suit_runner/capture_debug.py", "tests/compiler-capture/debug-fixture.nix",
            "tests/compiler-capture/debug_report.py", "tests/compiler-capture/debug_sweep.py",
            "tests/compiler-capture/capabilities.py", "tests/compiler-capture/ppc_debug.py",
            "tests/compiler-capture/batch.py", "tests/compiler-capture/isolate.sh"]}


def summarize(metadata, reports, *, candidate, binary_hash, sources):
    entries = {(entry["compiler"], target): entry for target, compilers in metadata.items() for entry in compilers}
    expected = {(compiler, target, "normal") for compiler, target in entries}
    measured, errors = {}, []
    for report in reports:
        if report.get("candidate") != str(candidate) or report.get("candidate_binary_sha256") != binary_hash:
            errors.append("debug evidence belongs to another supervisor")
            continue
        if relevant_sources(report.get("source_identity", {"files": {}})) != relevant_sources(sources):
            errors.append("debug evidence uses stale hooks or validators")
            continue
        for case in report["cases"]:
            key = case["compiler"], case["target"], case["mode"]
            entry = entries.get(key[:2])
            if entry is None or key[2] not in MODES or not applicable(entry, key[2]):
                errors.append("unexpected debug compiler/target mode: " + str(key))
                continue
            measured[key] = case
    missing = sorted(expected - measured.keys())
    failed = sorted(key for key, case in measured.items() if case.get("status") != "passed"
        or case.get("complete") is not True or case.get("runtime_ok") is not True
        or case.get("runtime_kind") != ("native" if key[1] == "x86_64" else "emulated")
        or not case.get("manifest_sha256") or case.get("debug_source_mapping") is not True
        or case.get("behavior_equal") is not True or case.get("target_abi") is not True
        or not case.get("debug_files"))
    return dict(schema=1, complete=bool(expected) and not (missing or failed or errors), expected=len(expected),
        passed=len(expected) - len(missing) - sum(key in expected for key in failed),
        additional_passed=sum(key not in expected and key not in failed for key in measured),
        missing=missing, failed=failed, errors=errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs", type=Path)
    parser.add_argument("--capture-tool", type=Path, required=True)
    parser.add_argument("--targets", nargs="+")
    parser.add_argument("--compilers", nargs="+")
    parser.add_argument("--modes", choices=MODES, nargs="+", default=["normal"])
    parser.add_argument("--resume-report", type=Path)
    parser.add_argument("--verify-reports", type=Path, nargs="+")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(batch.checked(["nix", "eval", "--offline", "--no-write-lock-file", "--json",
        str(batch.REPO) + "#_crossToolchainsMeta.x86_64-linux"]))
    candidate = args.capture_tool.resolve()
    binary_hash = hashlib.sha256((candidate/"bin/compiler-capture").read_bytes()).hexdigest()
    identity = batch.source_identity()
    if args.verify_reports:
        reports = [batch.read_report(path) for path in args.verify_reports]
        result = summarize(metadata, reports, candidate=candidate, binary_hash=binary_hash, sources=identity)
        result.update(source_identity=identity, candidate=str(candidate), candidate_binary_sha256=binary_hash,
            reports=list(map(str, args.verify_reports)))
        batch.atomic_json(args.report, result)
        print(json.dumps({key: result[key] for key in ["complete", "expected", "passed", "additional_passed", "errors"]}))
        return int(not result["complete"])
    if not args.nixpkgs:
        parser.error("--nixpkgs is required for builds")
    targets = args.targets or sorted(metadata)
    if set(targets) - metadata.keys():
        parser.error("unknown debug target")
    compilers = {entry["compiler"] for target in targets for entry in metadata[target]}
    if set(args.compilers or []) - compilers:
        parser.error("unknown debug compiler selection")
    report = dict(schema=1, candidate=str(candidate), candidate_binary_sha256=binary_hash,
        source_identity=identity, modes=args.modes, cases=[])
    if args.resume_report:
        previous = batch.read_report(args.resume_report)
        if (previous.get("candidate") != str(candidate) or previous.get("candidate_binary_sha256") != binary_hash
                or relevant_sources(previous["source_identity"]) != relevant_sources(identity)):
            parser.error("cannot resume stale debug evidence")
        report["cases"] = previous["cases"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    batch.atomic_json(args.report, report)
    completed = {(case["compiler"], case["target"], case["mode"]) for case in report["cases"] if case["status"] == "passed"}
    for target in targets:
        for entry in sorted(metadata[target], key=lambda entry: (tuple(map(int, entry["version"].split("."))), entry["family"])):
            compiler = entry["compiler"]
            if args.compilers and compiler not in args.compilers:
                continue
            for mode in args.modes:
                key = compiler, target, mode
                if key in completed or not applicable(entry, mode):
                    continue
                if len(list((batch.REPO/".capture-experiments").glob("batch-*"))) >= batch.LIMITS["maximum_batches"] - 4:
                    import archive_reports
                    with batch.experiment_lock("nix-batch"):
                        archive_reports.main()
                options = ["--debug-lto", "lto" if mode in ["lto", "split-lto"] else "thinlto" if mode == "thinlto" else "none"]
                if mode.startswith("split"):
                    options.append("--debug-split-dwarf")
                command = [sys.executable, str(batch.HERE/"batch.py"), "--nixpkgs", str(args.nixpkgs),
                    "--capture-tool", str(candidate), "--fixture", "separateDebugInfo", "--compiler", compiler,
                    "--target", target, "--timeout", "420", *options]
                result = batch.bounded_run(command, timeout=450)
                lines = result["stdout"].splitlines()
                ledger = batch.read_report(lines[0]) if lines else {}
                measured = batch.read_report(ledger["result_report"]) if ledger.get("result_report") else {}
                experiment, publication = measured.get("experiment", {}), measured.get("publication", {})
                debug_files = len({item["sha256"] for items in publication.get("debug_associations", {}).values() for item in items})
                complete = (measured.get("status") == 0 and publication.get("complete") is True
                    and experiment.get("runtime_ok") is True and experiment.get("behavior_equal") is True
                    and experiment.get("debug_source_mapping") is True and experiment.get("target_abi") is True and debug_files > 0)
                pending = any(name in ledger for name in ["unapproved_prerequisites", "missing_prerequisites"])
                case = dict(compiler=compiler, target=target, mode=mode, status="passed" if complete else "pending" if pending else "failed",
                    complete=complete, ledger=lines[0] if lines else None,
                    manifest_sha256=publication.get("manifest_sha256"), debug_files=debug_files,
                    **{name: experiment.get(name) for name in ["runtime_ok", "runtime_kind", "behavior_equal", "target_abi", "debug_source_mapping"]},
                    diagnostic=(measured.get("validation_error") or ledger.get("error") or result["stderr"])[-1500:] if not complete else "")
                report["cases"] = [row for row in report["cases"] if (row["compiler"], row["target"], row["mode"]) != key] + [case]
                batch.atomic_json(args.report, report)
                print(f"{target}/{compiler}/{mode}: {case['status']}", flush=True)
                if not complete:
                    return 1
    return 0 if report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

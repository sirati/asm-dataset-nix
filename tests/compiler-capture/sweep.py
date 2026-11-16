"""Run the public compiler/ISA matrix through the bounded, offline test driver.

Each compiler/target group shares its small wrapper prerequisites across modes.
No prerequisite acquisition is implicit. Refused downloads and compiler builds
are pending coverage, never successful tests.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import batch
from matrix_coverage import SHAPES, applicable, read_reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs",type=Path,required=True)
    parser.add_argument("--capture-tool",type=Path,required=True)
    parser.add_argument("--targets",nargs="+",help="Default: every public target")
    parser.add_argument("--compilers",nargs="+")
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--shape",choices=SHAPES)
    group.add_argument("--shapes",choices=SHAPES,nargs="+")
    parser.add_argument("--retry-report",type=Path,help="Retry only pending or failed modes from an earlier sweep")
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    shapes=args.shapes or [args.shape or "smoke"]
    if len(set(shapes))!=len(shapes):parser.error("matrix modes must be distinct")
    candidate=args.capture_tool.resolve()
    retry=None
    if args.retry_report:
        retry={(case["compiler"],case["target"],case.get("shape",report.get("shape","smoke")))
            for report in read_reports(args.retry_report) for case in report["cases"] if case["status"]!="passed"}
    metadata=json.loads(batch.checked(["nix","eval","--no-write-lock-file","--json",
        str(batch.REPO)+"#_crossToolchainsMeta.x86_64-linux"]))
    targets=args.targets or sorted(metadata)
    unknown_targets=set(targets)-metadata.keys()
    unknown_compilers=set(args.compilers or [])-{entry["compiler"] for target in targets if target in metadata for entry in metadata[target]}
    if unknown_targets or unknown_compilers:
        parser.error(f"unknown targets {sorted(unknown_targets)} or compilers {sorted(unknown_compilers)}")
    header=dict(schema=1,limits=batch.LIMITS,candidate=str(candidate),
        candidate_binary_sha256=hashlib.sha256((candidate/"bin/compiler-capture").read_bytes()).hexdigest(),
        source_identity=batch.source_identity())
    reports={shape:dict(header,shape=shape,cases=[]) for shape in shapes}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    paths={shape:args.report if len(shapes)==1 else args.report.with_name(args.report.stem+"."+shape+".json") for shape in shapes}
    if len(shapes)>1:
        batch.atomic_json(args.report,dict(header,schema=2,shapes=shapes,reports=[str(path.resolve()) for path in paths.values()]))
    for shape,path in paths.items():batch.atomic_json(path,reports[shape])
    for target in targets:
        for compiler in metadata[target]:
            label=compiler["compiler"]
            if args.compilers and label not in args.compilers:continue
            selected=[shape for shape in shapes if applicable(compiler,shape)
                and (retry is None or (label,target,shape) in retry)]
            if not selected:continue
            if len(list((batch.REPO/".capture-experiments").glob("batch-*")))>=batch.LIMITS["maximum_batches"]-4:
                import archive_reports
                archive_reports.main()
            # Queueing for another group is outside this group's timeout.
            with batch.experiment_lock("nix-batch"):pass
            command=[sys.executable,str(batch.HERE/"batch.py"),"--nixpkgs",str(args.nixpkgs),
                "--capture-tool",str(candidate),"--compiler",label,"--target",target]
            command += ["--matrix-shapes",*selected,"--timeout","600"] if len(selected)>1 else ["--shape",selected[0]]
            result=batch.bounded_run(command,timeout=660)
            lines=result["stdout"].splitlines()
            ledger_path=Path(lines[0]) if lines else None
            ledger=batch.read_report(ledger_path) if ledger_path and ledger_path.is_file() else {}
            pending=any(key in ledger for key in ["unapproved_prerequisites","missing_prerequisites"])
            for shape in selected:
                result_path=ledger.get("result_reports",{}).get(shape,ledger.get("result_report"))
                measured=batch.read_report(result_path) if result_path and Path(result_path).is_file() else {}
                complete=(measured.get("status")==0
                    and measured.get("manifest",measured.get("manifest_summary",{})).get("complete") is True
                    and measured.get("experiment",{}).get("debug_source_mapping") is True
                    and measured.get("experiment",{}).get("runtime_ok") is True)
                corners=measured.get("corners",{}).get("cases",[])
                if shape=="corners":
                    complete=complete and bool(corners) and all(item.get("status") in ["passed","baseline-unsupported"] for item in corners)
                case=dict(compiler=label,target=target,shape=shape,
                    status="passed" if complete else "pending" if pending else "failed",ledger=str(ledger_path),
                    exit_status=measured.get("status",result["status"]),complete=complete,
                    manifest_sha256=measured.get("validated_capture",{}).get("sha256"),
                    artifact_bytes=measured.get("validated_capture",{}).get("artifact_bytes"),
                    runtime_ok=measured.get("experiment",{}).get("runtime_ok") is True,
                    runtime_kind=measured.get("experiment",{}).get("runtime_kind"),
                    corner_cases={item["name"]:item["status"] for item in corners},
                    debug_policy_reference_cases=[item["name"] for item in corners if item.get("debug_policy_references")],
                    errors=ledger.get("error") if not complete else None,
                    diagnostic=((measured.get("validation_error") or measured.get("stderr") or result["stderr"])[-1024:] if not complete else ""))
                reports[shape]["cases"].append(case)
                batch.atomic_json(paths[shape],reports[shape])
                print(f"{target}/{label}/{shape}: {case['status']}",flush=True)
    cases=[case for report in reports.values() for case in report["cases"]]
    if not cases:parser.error("no applicable compiler/target modes selected")
    return int(any(case["status"]!="passed" for case in cases))


if __name__=="__main__":
    raise SystemExit(main())

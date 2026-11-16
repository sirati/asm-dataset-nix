"""Build the pinned test framework in bounded scratch, with reviewed prerequisites.

Only fixed-output source fetches receive network access from Nix's sandbox.
Normal fixture batches keep their isolated network namespace.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import batch

EXPORT_LIMIT=256*1024**2


def inside(drv,seed_path,report,export):
    result=batch.bounded_run(["nix","build","--offline","--no-write-lock-file","--max-jobs","1","--cores","1","--no-link","--json",drv+"^out"],timeout=1800,checkpoint=Path(report).with_name("checkpoint.json"))
    try:
        if result["status"]==0:
            output=json.loads(result["stdout"])[0]["outputs"]["out"]
            info=json.loads(batch.checked(["nix","path-info","--recursive","--json",output]))
            info={row["path"]:row for row in info} if isinstance(info,list) else info
            existing=set(json.loads(Path(seed_path).read_text()))
            ordered=[]
            seen=set()
            def visit(path):
                if path in seen:return
                seen.add(path)
                for reference in info[path].get("references",[]):visit(reference)
                if path not in existing:ordered.append(path)
            visit(output)
            allocated=sum(batch.usage(path)["allocated_bytes"] for path in ordered)
            if allocated>EXPORT_LIMIT:raise RuntimeError("framework export exceeds 256 MiB")
            archive=Path.cwd()/"framework.nar"
            with archive.open("wb") as file:
                subprocess.run(["nix-store","--export",*ordered],stdout=file,check=True)
            if archive.stat().st_size>EXPORT_LIMIT:raise RuntimeError("framework NAR exceeds 256 MiB")
            shutil.copyfile(archive,export)
            result.update(output=output,exports=ordered,allocated_bytes=allocated,archive_bytes=archive.stat().st_size)
        else:
            result["builder_log"]=batch.bounded_run(["nix","log",drv],timeout=30)
    except Exception as error:
        result.update(status=1,error=str(error))
    cgroup=Path("/proc/self/cgroup").read_text().strip().split("::",1)[1]
    result["cgroup"]={name:(Path("/sys/fs/cgroup")/cgroup.lstrip("/")/name).read_text().strip()
        for name in ["memory.max","memory.swap.max","memory.peak","memory.events","cpu.max"]}
    # lower is a bind mount of the entire shared store. Count writable
    # allocation with statvfs, and traverse only the owned upper layer.
    result["bounded_usage"]=batch.usage(Path.cwd()/"upper")
    stats=os.statvfs(Path.cwd())
    result["bounded_filesystem"]=dict(allocated_bytes=(stats.f_blocks-stats.f_bfree)*stats.f_frsize,
        total_bytes=stats.f_blocks*stats.f_frsize,used_inodes=stats.f_files-stats.f_ffree,total_inodes=stats.f_files)
    batch.atomic_json(Path(report),result)
    return int(result["status"]!=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs",type=Path,required=True)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--memory-mib",type=int,choices=[1024,2048,4096],default=1024)
    args=parser.parse_args()
    parent=batch.REPO/".capture-experiments"
    directory=Path(tempfile.mkdtemp(prefix="framework-",dir=parent))
    mountpoint=directory/"bounded";mountpoint.mkdir()
    root=directory/"inputs"
    registration=directory/"registration"
    seed=directory/"seed.json"
    report=directory/"ledger.json"
    ledger=dict(schema=1,before=batch.headroom(parent),limits=dict(batch.LIMITS,memory_bytes=args.memory_mib*1024**2),source=str(args.source),
        cargo_lock_sha256=hashlib.sha256((args.source/"Cargo.lock").read_bytes()).hexdigest(),network="fixed-output prerequisite fetches only",cleanup="pending")
    batch.atomic_json(report,ledger)
    scope=None
    try:
        expression=f"let p=import {args.nixpkgs} {{}}; in p.python313Packages.callPackage {args.source}/nix/wheel.nix {{}}"
        drv=batch.checked(["nix-instantiate","--expr",expression]);ledger["drv"]=drv
        dry=batch.bounded_run(["nix","build","--dry-run","--offline","--no-write-lock-file",drv+"^out"])
        ledger["dry_run"]=dry
        if dry["status"] or "will be fetched" in dry["stderr"]:raise RuntimeError("cached prerequisite acquisition must finish before framework build")
        planned=set(re.findall(r"/nix/store/[a-z0-9]{32}-[^\s]+\.drv",dry["stderr"]))
        reviewed=json.loads((parent/"framework-dry.json").read_text())
        allowed=set(re.findall(r"/nix/store/[a-z0-9]{32}-[^\s]+\.drv",reviewed["stderr"]))
        if planned-allowed:raise RuntimeError("unreviewed framework derivations: "+str(sorted(planned-allowed)))
        required=set(batch.checked(["nix-store","-qR","--include-outputs",drv]).splitlines())
        for tool in [shutil.which("nix"),sys.executable]:
            required.update(batch.checked(["nix-store","-qR",str(Path(tool).resolve().parent.parent)]).splitlines())
        batch.atomic_json(seed,sorted(required))
        root_expr='builtins.toFile "capture-framework-build-inputs" (builtins.concatStringsSep "\\n" (map builtins.storePath (builtins.fromJSON (builtins.readFile '+str(seed)+'))))'
        root_file=batch.checked(["nix","eval","--impure","--raw","--expr",root_expr])
        subprocess.run(["nix-store","--realise",root_file,"--add-root",str(root),"--indirect"],check=True)
        with registration.open("wb") as file:subprocess.run(["nix-store","--dump-db",*sorted(required)],stdout=file,check=True)
        scope="compiler-capture-"+directory.name+".scope";ledger["scope"]=scope
        batch.atomic_json(report,ledger)
        command=[str(batch.HERE/"isolate.sh"),str(mountpoint),str(registration),sys.executable,str(Path(__file__).resolve()),"--inside",drv,str(seed),str(directory/"result.json"),str(directory/"framework.nar")]
        ledger["build"]=batch.bounded_run(command,timeout=1900,env=dict(os.environ,CAPTURE_SCOPE=scope,CAPTURE_PREREQUISITE_NETWORK="1",CAPTURE_PREREQUISITE_MEMORY_BYTES=str(args.memory_mib*1024**2)))
        batch.bounded_run(["systemctl","--user","kill","--kill-whom=all",scope],timeout=10)
        if ledger["build"]["status"]:raise RuntimeError("bounded framework build failed")
    except Exception as error:
        ledger["error"]=str(error)
    finally:
        if scope is not None:
            batch.bounded_run(["systemctl","--user","kill","--kill-whom=all",scope],timeout=10)
        root.unlink(missing_ok=True);registration.unlink(missing_ok=True);mountpoint.rmdir()
        ledger.update(cleanup="namespace discarded; temporary roots released",after=batch.headroom(parent),retained=batch.usage(directory))
        batch.atomic_json(report,ledger)
    print(report)
    return int("error" in ledger)


if __name__=="__main__":
    if sys.argv[1:2]==["--inside"]:raise SystemExit(inside(*sys.argv[2:]))
    with batch.experiment_lock("nix-batch"):raise SystemExit(main())

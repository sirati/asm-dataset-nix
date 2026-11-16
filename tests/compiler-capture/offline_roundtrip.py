"""Export/import one captured cross fixture with no substituters or network.

Both stores share a registered, read-only base. The transferred delta includes
the supervisor, descriptor anchor, immutable configs, selected wrappers and drv
files that reference them. The destination starts without that delta registered.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import batch

EXPORT_LIMIT=16*1024**2


def export_fixture(settings,drv):
    seed=set(json.loads(Path(settings["seed_file"]).read_text()))
    rows=json.loads(batch.checked(["nix","path-info","--recursive","--json",settings["root"],*sorted(seed)]))
    info={row["path"]:row for row in rows} if isinstance(rows,list) else rows
    selected=set(info)-seed
    selected.add(settings["supervisor"])
    selected.update(path for path in info if path.endswith("-capture-view-anchor"))
    # Keep the destination base closed. A drv or source file referring to the
    # removed runtime delta must travel in the archive too.
    while True:
        parents={path for path,row in info.items() if set(row.get("references",())) & selected}
        enlarged=selected|parents
        if enlarged==selected:break
        selected=enlarged
    if drv not in selected:raise RuntimeError("fixture drv did not join the transferred capture delta")
    if sum(info[path]["narSize"] for path in selected)>EXPORT_LIMIT:
        raise RuntimeError("offline fixture delta exceeds 16 MiB")
    ordered=[];seen=set()
    def visit(path):
        if path in seen:return
        seen.add(path)
        for reference in sorted(info[path].get("references",())):
            if reference in selected:visit(reference)
        ordered.append(path)
    for path in sorted(selected):visit(path)
    sys.path[:0]=[str(batch.REPO),str(batch.REPO/"python")]
    from compiler_suit_runner.workers.build_compilers_worker import export_closure_exact
    archive=Path.cwd()/"toolchains.drv.archive"
    success,error=export_closure_exact(archive,ordered)
    if not success:raise RuntimeError(error.decode("utf-8","replace")[-2048:])
    if archive.stat().st_size>EXPORT_LIMIT:raise RuntimeError("offline fixture NAR exceeds 16 MiB")
    configurations=[]
    for path in ordered:
        if path.endswith(".json") and Path(path).is_file() and Path(path).stat().st_size<=65536:
            config=json.loads(Path(path).read_text())
            if isinstance(config,dict) and config.get("supervisor")==settings["supervisor"]+"/bin/compiler-capture":
                configurations.append(dict(path=path,family=config["family"],target=config["target"]))
    if not any(row["family"] in ["gcc","clang"] for row in configurations):
        raise RuntimeError("offline archive has no immutable compiler configuration")
    shutil.copyfile(archive,settings["destination"])
    return dict(root=settings["root"],paths=ordered,configurations=configurations,
        supervisor=settings["supervisor"],archive_bytes=archive.stat().st_size)


def inside(settings_path):
    settings=json.loads(Path(settings_path).read_text())
    absent=batch.bounded_run(["nix-store","--check-validity",settings["drv"],settings["supervisor"]])
    if absent["status"]==0:raise RuntimeError("destination already contains the capture delta")
    archive=Path.cwd()/"toolchains.drv.archive"
    shutil.copyfile(settings["archive"],archive)
    sys.path[:0]=[str(batch.REPO),str(batch.REPO/"python")]
    from compiler_suit_runner.workers.build_worker import ensure_toolchain_archive_imported
    ensure_toolchain_archive_imported(Path.cwd())
    subprocess.run(["nix-store","--check-validity",*settings["paths"]],check=True,capture_output=True)
    status=batch.inside_build(settings["drv"],settings["defaults"],settings["result"])
    result=json.loads(Path(settings["result"]).read_text())
    actual=result.get("validated_capture",{}).get("sha256")
    result["offline_roundtrip"]=dict(delta_was_unregistered=True,imported_paths=len(settings["paths"]),
        substituters=os.environ.get("NIX_CONFIG",""),same_manifest=actual==settings["manifest_sha256"])
    if actual!=settings["manifest_sha256"]:
        result.update(status=1,validation_error="offline rebuild changed the canonical capture manifest")
    batch.atomic_json(Path(settings["result"]),result)
    return int(status!=0 or result["status"]!=0)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nixpkgs",type=Path,required=True)
    parser.add_argument("--capture-tool",type=Path,required=True)
    parser.add_argument("--compiler",default="gcc15")
    parser.add_argument("--target",default="aarch64")
    args=parser.parse_args()
    first=batch.bounded_run([sys.executable,str(batch.HERE/"batch.py"),"--nixpkgs",str(args.nixpkgs),
        "--capture-tool",str(args.capture_tool.resolve()),"--compiler",args.compiler,"--target",args.target,"--export-offline"],timeout=600)
    if first["status"]:raise RuntimeError(json.dumps(first))
    source_ledger=Path(first["stdout"].splitlines()[0]);source=json.loads(source_ledger.read_text())
    measured=json.loads(Path(source["result_report"]).read_text());exported=measured["offline_export"]
    parent=batch.REPO/".capture-experiments"
    directory=Path(tempfile.mkdtemp(prefix="offline-",dir=parent));mountpoint=directory/"bounded";mountpoint.mkdir()
    registration=directory/"registration";root=directory/"inputs";scope="compiler-capture-"+directory.name+".scope"
    ledger=dict(schema=1,source_ledger=str(source_ledger),limits=batch.LIMITS,before=batch.headroom(parent),cleanup="pending")
    try:
        seed=sorted(set(source["input_paths"])-set(exported["paths"]))
        with registration.open("wb") as file:subprocess.run(["nix-store","--dump-db",*seed],stdout=file,check=True)
        seed_path=directory/"seed.json";batch.atomic_json(seed_path,seed)
        expression='builtins.toFile "capture-offline-base" (builtins.concatStringsSep "\\n" (map builtins.storePath (builtins.fromJSON (builtins.readFile '+str(seed_path)+'))))'
        rooted=batch.checked(["nix","eval","--impure","--raw","--expr",expression])
        subprocess.run(["nix-store","--realise",rooted,"--add-root",str(root),"--indirect"],check=True,capture_output=True)
        settings=directory/"settings.json"
        masked=directory/"masked-paths.json";batch.atomic_json(masked,exported["paths"])
        batch.atomic_json(settings,dict(drv=source["derivation"],supervisor=exported["supervisor"],paths=exported["paths"],
            archive=str(source_ledger.parent/"offline.nar"),defaults=",".join(source["default_output_names"]),
            manifest_sha256=measured["validated_capture"]["sha256"],result=str(directory/"result.json")))
        with batch.experiment_lock("nix-batch"):
            command=[str(batch.HERE/"isolate.sh"),str(mountpoint),str(registration),sys.executable,str(Path(__file__).resolve()),"--inside",str(settings)]
            # Tiny capture uses a separate 256 MiB mount inside this bound.
            environment=dict(os.environ,CAPTURE_SCOPE=scope,CAPTURE_SCRATCH_BYTES=str(3*1024**3-256*1024**2),CAPTURE_INODES=str(131072-16384),
                CAPTURE_MASK_PATHS_JSON=str(masked),CAPTURE_MASK_PYTHON=sys.executable)
            ledger["build"]=batch.bounded_run(command,timeout=360,env=environment)
            if ledger["build"]["status"]:raise RuntimeError("offline import/rebuild failed")
    except Exception as error:ledger["error"]=str(error)
    finally:
        batch.bounded_run(["systemctl","--user","kill","--kill-whom=all",scope],timeout=10)
        root.unlink(missing_ok=True);registration.unlink(missing_ok=True)
        if mountpoint.exists():mountpoint.rmdir()
        (source_ledger.parent/"offline.nar").unlink(missing_ok=True)
        ledger.update(cleanup="namespace discarded; roots and owned NAR released",retained=batch.usage(directory),after=batch.headroom(parent))
        batch.atomic_json(directory/"ledger.json",ledger)
    print(directory/"ledger.json")
    return int("error" in ledger)


if __name__=="__main__":
    if sys.argv[1:2]==["--inside"]:raise SystemExit(inside(sys.argv[2]))
    raise SystemExit(main())

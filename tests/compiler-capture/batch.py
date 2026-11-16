"""Owned, bounded compiler-capture experiments. Python standard library only.

Preparation uses the shared daemon only for explicit cached prerequisites.
Builds run in isolate.sh's disposable, offline store. Reports survive it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import fcntl
from contextlib import contextmanager

GIB = 1024**3
LIMITS = dict(jobs=1, cores=1, concurrent_tests=1, capture_bytes=64*1024**2,
              package_capture_bytes=512*1024**2, scratch_bytes=4*GIB,
              writable_fs_bytes=3*GIB, metadata_file_bytes=1024**2,
              maximum_batches=128, maximum_rust_sessions=16,
              prerequisite_bytes=20*GIB, free_reserve_bytes=100*GIB,
              memory_bytes=GIB, swap_bytes=3*GIB, inodes=131072,
              log_bytes=64*1024)
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


@contextmanager
def experiment_lock(name):
    parent=REPO/".capture-experiments"
    parent.mkdir(exist_ok=True)
    with (parent/("."+name+".lock")).open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def source_identity():
    paths = list((REPO/"rust/compiler-capture/src").glob("*.rs")) + list(HERE.glob("*"))
    paths += [REPO/name for name in ["rust/compiler-capture/Cargo.toml","rust/compiler-capture/Cargo.lock",
        "lib/compiler-capture.nix","lib/capture-overlay.nix","nix/compiler-capture.nix",
        "lib/mkBinaryFolder.nix","lib/collect-elf.py","python/compiler_suit_runner/capture.py"]]
    paths += list((REPO/"lib").glob("*.sh"))
    paths += list((REPO/"lib").glob("*.h"))
    paths += list((REPO/"lib").glob("*.nix")) + [REPO/"flake.nix",REPO/"flake.lock",REPO/"develop.nix"]
    paths += list((REPO/"nix").glob("capture-*.nix"))
    paths += list((REPO/"python/compiler_suit_runner").glob("*.py"))
    paths += list((REPO/"python/compiler_suit_runner/workers").glob("*.py"))
    entries = {str(path.relative_to(REPO)):hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted(paths) if path.is_file()}
    return dict(sha256=hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest(),files=entries)


def bounded_run(argv, *, timeout=300, env=None, output_limit=LIMITS["log_bytes"],checkpoint=None):
    """Drain both pipes while retaining at most log_bytes per stream."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    counts = dict(stdout=0, stderr=0)
    selector = selectors.DefaultSelector()
    for name in buffers:
        selector.register(getattr(proc, name), selectors.EVENT_READ, name)
    started=time.monotonic()
    deadline = started + timeout
    next_checkpoint=started
    timed_out = False
    try:
        while selector.get_map():
            if checkpoint is not None and time.monotonic()>=next_checkpoint:
                snapshot=dict(elapsed_seconds=time.monotonic()-started,
                    **{k:bytes(v).decode("utf-8","backslashreplace") for k,v in buffers.items()})
                cg=Path("/proc/self/cgroup").read_text().strip().split("::",1)[1]
                snapshot["memory"]={name:(Path("/sys/fs/cgroup")/cg.lstrip("/")/name).read_text().strip()
                    for name in ["memory.current","memory.peak","memory.events"]}
                stats=os.statvfs(Path.cwd())
                snapshot["filesystem_allocated_bytes"]=(stats.f_blocks-stats.f_bfree)*stats.f_frsize
                atomic_json(Path(checkpoint),snapshot)
                next_checkpoint=time.monotonic()+1
            if time.monotonic() >= deadline and not timed_out:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
            for key, _ in selector.select(0.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                else:
                    counts[key.data] += len(data)
                    buffers[key.data].extend(data)
                    del buffers[key.data][:-output_limit]
        code = proc.wait()
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return dict(argv=list(map(str, argv)), status=code, timed_out=timed_out,
                elapsed_seconds=time.monotonic()-started,
                **{k: bytes(v).decode("utf-8", "backslashreplace")
                   for k, v in buffers.items()}, stream_bytes=counts)


def checked(argv, **kw):
    result = bounded_run(list(map(str, argv)), output_limit=4*1024**2, **kw)
    if result["status"]:
        raise RuntimeError(json.dumps(result, indent=2))
    if result["stream_bytes"]["stdout"] > 4*1024**2:
        raise RuntimeError("metadata exceeds the 4 MiB command-output limit")
    return result["stdout"].strip()


def usage(path):
    size = files = 0
    inodes = set()
    for base, dirs, names in os.walk(path):
        for name in ["."] + names + [d for d in dirs if Path(base, d).is_symlink()]:
            info = os.lstat(os.path.join(base, name))
            inode = (info.st_dev, info.st_ino)
            if inode not in inodes:
                size += info.st_blocks * 512
                inodes.add(inode)
            files += 1
    return dict(allocated_bytes=size, entries=files, inodes=len(inodes))


def compact_report(result):
    """Retain bounded failure evidence when real packages have many records."""
    if len(json.dumps(result).encode())<=800*1024 or "manifest" not in result:
        return result
    manifest=result.pop("manifest")
    records=manifest.get("invocations",[])
    result["manifest_summary"]={key:manifest.get(key) for key in ["schema","wrapper_revision","complete","policy","errors"]}
    result["manifest_summary"].update(artifact_count=len(manifest.get("artifacts",{})),
        invocation_count=sum(entry["count"] for entry in records),unique_records=len(records))
    failed=[entry for entry in records if not entry["record"].get("capture_complete")]
    result["incomplete_invocation_examples"]=failed[:4]
    error_counts={}
    for entry in failed:
        for error in entry["record"].get("capture_errors",[]):
            error_counts[error]=error_counts.get(error,0)+entry["count"]
    result["capture_error_counts"]=error_counts
    result["manifest_excerpt_only"]=True
    return result


def inside_build(drv, default_names, report_path, export_path=None):
    reviewed_path = os.environ.get("CAPTURE_REVIEWED_DRVS_JSON")
    if reviewed_path:
        review = bounded_run(["nix", "build", "--offline", "--dry-run", "--no-write-lock-file", "--json", drv+"^*"], timeout=120)
        allowed = set(json.loads(Path(reviewed_path).read_bytes()))
        planned = set(re.findall(r"/nix/store/[a-z0-9]{32}-[^\s]+\.drv", review["stderr"]))
        if (review["status"] or planned - allowed or "will be fetched" in review["stderr"]
                or review["stream_bytes"]["stderr"] > LIMITS["log_bytes"]):
            atomic_json(Path(report_path), dict(status=1, validation_error="private store requires unreviewed build inputs",
                private_dry_run=review, unexpected=sorted(planned-allowed)))
            return 1
    result = bounded_run(["nix", "build", "--offline", "--no-write-lock-file", "--json",
                          "--max-jobs", "1", "--cores", "1", "--out-link", "result", drv+"^*"],
                         timeout=min(1800,int(os.environ.get("CAPTURE_BUILD_TIMEOUT","240"))))
    if result["status"] != 0:
        result["builder_log"] = bounded_run(["nix", "log", drv],timeout=30)
    try:
        if result["status"] == 0:
            outputs = json.loads(result["stdout"])[0]["outputs"]
            result["outputs"] = outputs
            result["output_usage"] = {key: usage(path) for key, path in outputs.items()}
            folder = Path(outputs["out"]) if "datasetCapture" not in outputs else None
            capture = Path(outputs["datasetCapture"]) if folder is None else folder/"auxiliary"/"datasetCapture"
            result["manifest"] = json.loads((capture / "manifest.json").read_text())
            for name in ["experiment", "corners", "baseline"]:
                if (capture / (name+".json")).is_file():
                    result[name] = json.loads((capture / (name+".json")).read_text())
            remaining=128*1024
            diagnostic_artifacts={}
            for entry in result["manifest"].get("invocations",[]):
                for artifact in entry["record"]["artifacts"]:
                    digest=artifact["sha256"]
                    if artifact["role"] not in ["command","response"] or digest in diagnostic_artifacts:
                        continue
                    if artifact["size"]>min(65536,remaining):
                        continue
                    data=(capture/"objects"/digest).read_bytes()
                    diagnostic_artifacts[digest]=dict(role=artifact["role"],bytes_hex=data.hex())
                    remaining-=len(data)
            result["diagnostic_artifacts"]=diagnostic_artifacts
            if result["manifest"].get("wrapper_revision") == 1:
                spec = importlib.util.spec_from_file_location("capture_validator",REPO/"python/compiler_suit_runner/capture.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                verified = module.validate_capture(capture,require_complete=False)
                result["validated_capture"] = dict(sha256=verified.manifest_sha256,
                    artifact_bytes=verified.artifact_bytes,artifact_count=verified.artifact_count,
                    invocation_count=verified.invocation_count)
                elf_spec=importlib.util.spec_from_file_location("capture_elf_collector",REPO/"lib/collect-elf.py")
                elf_module=importlib.util.module_from_spec(elf_spec)
                elf_spec.loader.exec_module(elf_module)
                elf_directory=Path.cwd()/"validation-elf"
                ordinary=[path for role,path in outputs.items() if role not in ["datasetCapture","debug"]]
                if folder is None:
                    elf_module.collect(elf_directory,ordinary)
                else:
                    elf_directory = folder/"elf"
                try:
                    result["elf_associations"]=module.elf_associations(elf_directory,verified,required=True,required_debug=True)
                except module.CaptureValidationError:
                    result["association_diagnostics"]={
                        "installed":{path.name:module.elf_information(path) for path in list(elf_directory.iterdir())[:8]},
                        "captured":{artifact["sha256"]:module.elf_information(capture/"objects"/artifact["sha256"])
                            for entry in verified.manifest["invocations"][:128] for artifact in entry["record"]["artifacts"] if artifact["role"]=="linked-output"}}
                    remaining=128*1024
                    examples={}
                    candidates=list(elf_directory.iterdir())[:2]+[capture/"objects"/digest for digest in list(result["association_diagnostics"]["captured"])[:2]]
                    for path in candidates:
                        if path.stat().st_size>min(32768,remaining):continue
                        data=path.read_bytes();remaining-=len(data)
                        examples[path.name]=data.hex()
                    result["association_diagnostics"]["elf_bytes_hex"]=examples
                    raise
                for filename,role in [("expected-object-sha256","object"),("expected-program-sha256","linked-output")]:
                    if (capture/filename).is_file():
                        expected = (capture/filename).read_text().split()[0]
                        found = any(artifact["sha256"]==expected and artifact["role"]==role
                            for item in verified.manifest["invocations"] for artifact in item["record"]["artifacts"])
                        if not found:
                            raise RuntimeError(f"{role} snapshot did not survive mutation and fixup")
                if folder is not None:
                    metadata = json.loads((folder/"meta.json").read_text())
                    contract = metadata["compilerCapture"]
                    if contract != module.required_contract(metadata["compilerFamily"]):
                        raise RuntimeError("Nix and Python capture publication contracts disagree")
                    _, summary = module.stage_captured_variant(folder, Path.cwd()/"publication", "variant",
                        label=metadata.get("variantLabel", "debug-fixture"), drv=drv, contract=contract)
                    if not module.captured_variant_complete(Path.cwd()/"publication/variant",
                            label=metadata.get("variantLabel", "debug-fixture"), drv=drv, contract=contract):
                        raise RuntimeError("separate-debug publication failed completion validation")
                    result["publication"] = summary
            if "corners" in result:
                if any(case["status"] not in ["passed", "baseline-unsupported"] for case in result["corners"]["cases"]):
                    raise RuntimeError("compiler corner-case comparisons failed")
            if "baseline" in result and (not result["baseline"]["cases"] or any(
                    case["status"]!="passed" for case in result["baseline"]["cases"])):
                raise RuntimeError("baseline compiler/library/runtime checks failed")
            if (capture/"experiment-filesystem.txt").is_file():
                block_size,blocks,free_blocks,available_blocks,inodes,free_inodes=map(int,(capture/"experiment-filesystem.txt").read_text().split())
                result["capture_filesystem_before_finalize"]=dict(total_bytes=block_size*blocks,
                    allocated_bytes=block_size*(blocks-free_blocks),available_bytes=block_size*available_blocks,
                    inodes=inodes,used_inodes=inodes-free_inodes)
            default = json.loads(checked(["nix", "build", "--offline", "--no-link", "--json", drv+"^"+default_names]))[0]["outputs"]
            selected = json.loads(checked(["nix", "build", "--offline", "--no-link", "--json", drv+"^datasetCapture"]))[0]["outputs"] if folder is None else None
            if "datasetCapture" in default or folder is None and selected != {"datasetCapture": outputs["datasetCapture"]}:
                raise RuntimeError("capture changed default output selection")
            result["default_outputs"] = default
            result["capture_selection"] = selected
            for key, path in outputs.items():
                if not Path(path).is_dir():
                    raise RuntimeError(f"missing output {key}")
            result["namespace_cgroup"] = Path("/proc/self/cgroup").read_text().strip()
            result["bounded_usage"] = usage(Path.cwd() / "upper")
            stats = os.statvfs(Path.cwd())
            result["bounded_filesystem"] = dict(allocated_bytes=(stats.f_blocks-stats.f_bfree)*stats.f_frsize,
                total_bytes=stats.f_blocks*stats.f_frsize,used_inodes=stats.f_files-stats.f_ffree,total_inodes=stats.f_files)
            cg = Path("/proc/self/cgroup").read_text().strip().split("::",1)[1]
            result["cgroup_limits"] = {key:(Path("/sys/fs/cgroup")/cg.lstrip("/")/key).read_text().strip()
                for key in ["memory.max","memory.swap.max","cpu.max"]}
            if export_path:
                # Export only a size-checked supervisor, not its compiler closure.
                # The bounded store also bounds the temporary NAR before the small
                # retained candidate is copied to the owned report directory.
                archive = Path.cwd()/"candidate.nar"
                if result["output_usage"]["out"]["allocated_bytes"] > 16*1024**2:
                    raise RuntimeError("supervisor output exceeds the 16 MiB export budget")
                anchor_file=Path(outputs["out"])/"nix-support/capture-view-anchor"
                exports=[]
                if anchor_file.is_file():
                    anchor=Path(anchor_file.read_text().strip())
                    if not str(anchor).startswith("/nix/store/") or not anchor.name.endswith("-capture-view-anchor") or usage(anchor)["allocated_bytes"]>65536:
                        raise RuntimeError("invalid capture view anchor export")
                    exports.append(str(anchor))
                exports.append(outputs["out"])
                with archive.open("wb") as dest:
                    subprocess.run(["nix-store", "--export", *exports],stdout=dest,check=True)
                if archive.stat().st_size > 16*1024**2:
                    raise RuntimeError("candidate archive exceeds the 16 MiB export budget")
                shutil.copyfile(archive,export_path)
                result["exported_candidate"] = str(export_path)
                result["exported_paths"]=exports
            if os.environ.get("CAPTURE_OFFLINE_EXPORT"):
                from offline_roundtrip import export_fixture
                result["offline_export"]=export_fixture(json.loads(Path(os.environ["CAPTURE_OFFLINE_EXPORT"]).read_text()),drv)
    except Exception as error:
        result["nix_build_status"]=result["status"]
        result["status"]=1
        result["validation_error"]=str(error)
    stats=os.statvfs(Path.cwd())
    result["bounded_filesystem"]=dict(allocated_bytes=(stats.f_blocks-stats.f_bfree)*stats.f_frsize,
        total_bytes=stats.f_blocks*stats.f_frsize,used_inodes=stats.f_files-stats.f_ffree,total_inodes=stats.f_files)
    cg=Path("/proc/self/cgroup").read_text().strip().split("::",1)[1]
    result["cgroup_usage"]={key:(Path("/sys/fs/cgroup")/cg.lstrip("/")/key).read_text().strip()
        for key in ["memory.max","memory.peak","memory.events","cpu.max"]}
    compact_report(result)
    atomic_json(Path(report_path),result)
    print(json.dumps(dict(status=result["status"],timed_out=result["timed_out"],
        report=report_path,complete=result.get("manifest",{}).get("complete")),sort_keys=True))
    return bool(result["status"])


def inside_matrix(settings_path):
    settings=json.loads(Path(settings_path).read_text())
    status=0
    for shape, drv in settings["derivations"].items():
        # Each result is independently validated. The private store keeps the
        # small common wrappers between cases and is discarded after the group.
        validation=Path.cwd()/"validation-elf"
        if validation.exists():shutil.rmtree(validation)
        status |= inside_build(drv,settings["defaults"],settings["reports"][shape])
    return status


def rust_session(memory_mib=1024):
    """Prepare a single reusable bounded target/cache area for fast iteration."""
    parent = REPO / ".capture-experiments"
    parent.mkdir(exist_ok=True)
    if sum(1 for _ in parent.glob("rust-*")) >= LIMITS["maximum_rust_sessions"]:
        raise RuntimeError("clean old owned Rust session ledgers before starting another")
    before = headroom(parent)
    directory = Path(tempfile.mkdtemp(prefix="rust-", dir=parent))
    mountpoint = directory / "bounded"
    mountpoint.mkdir()
    required = set()
    tools = ["nix", "cargo", "rustc", "cc"]
    # A linker is required by rustc even though this crate has no C dependencies.
    for tool in tools:
        path = shutil.which(tool)
        if path is None:
            if tool == "cc":
                continue
            raise RuntimeError(f"missing development tool: {tool}")
        root = Path(path).resolve().parent.parent
        required.update(checked(["nix-store", "-qR", root]).splitlines())
    required.update(checked(["nix-store", "-qR", str(Path(sys.executable).resolve().parent.parent)]).splitlines())
    registration = directory / "registration"
    with registration.open("wb") as output:
        subprocess.run(["nix-store", "--dump-db", *sorted(required)], stdout=output, check=True)
    scope = f"compiler-capture-{directory.name}.scope"
    rust_limits = dict(LIMITS,writable_fs_bytes=512*1024**2,inodes=32768,memory_bytes=memory_mib*1024**2)
    ledger = dict(schema=1, limits=rust_limits, before=before, roots=[],
                  input_paths=sorted(required), scope=scope, cleanup="pending")
    atomic_json(directory / "ledger.json", ledger)
    registry = Path(os.environ.get("CARGO_HOME", str(Path.home()/".cargo"))) / "registry"
    # Use the invoking shell's existing compiler/linker tools. Registry source
    # data are immutable inside this namespace; Cargo's writable cache/target
    # and every newly compiled file are on the 4 GiB filesystem.
    command = [str(HERE/"isolate.sh"), str(mountpoint), str(registration),
               "bash", str(HERE/"rust-session.sh"), str(registry)]
    try:
        subprocess.run(command, env=dict(os.environ, CAPTURE_SCOPE=scope,CAPTURE_TEST_MEMORY_BYTES=str(rust_limits["memory_bytes"]),
            CAPTURE_SCRATCH_BYTES=str(rust_limits["writable_fs_bytes"]),CAPTURE_INODES=str(rust_limits["inodes"])), check=True)
    finally:
        bounded_run(["systemctl", "--user", "kill", "--kill-whom=all", scope], timeout=10)
        registration.unlink(missing_ok=True)
        mountpoint.rmdir()
        ledger["cleanup"] = "namespace discarded"
        ledger["after"] = headroom(parent)
        atomic_json(directory/"ledger.json", ledger)


def atomic_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if len(data) > 1024**2:
        raise RuntimeError("report exceeds the 1 MiB metadata limit")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_report(path):
    """Resolve report links even after owned batch directories are archived."""
    path=Path(path)
    if path.is_file():return json.loads(path.read_text())
    import tarfile
    member=path.parent.name+"/"+path.name
    for archive in sorted((REPO/".capture-experiments").glob("released-reports-*.tar.xz"),reverse=True):
        with tarfile.open(archive) as tar:
            try:entry=tar.getmember(member)
            except KeyError:continue
            if entry.size>LIMITS["metadata_file_bytes"]:raise RuntimeError("archived report exceeds its limit")
            return json.loads(tar.extractfile(entry).read())
    raise FileNotFoundError(path)


def headroom(path, estimated=0):
    stats = os.statvfs(path)
    available = stats.f_bavail * stats.f_frsize
    if available < LIMITS["free_reserve_bytes"] + estimated:
        raise RuntimeError("refusing batch below the free-space reserve")
    return dict(free_bytes=available, free_inodes=stats.f_favail)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--nixpkgs", type=Path, required=True)
    parser.add_argument("--fixture", default="outputContract")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--memory-mib",type=int,choices=[1024,2048,4096],default=1024)
    parser.add_argument("--capture-tool", type=Path)
    parser.add_argument("--compiler")
    parser.add_argument("--target",default="x86_64")
    parser.add_argument("--baseline-target",action="store_true",help="check every baseline C++/LTO toolchain for --target before capture sweeps")
    parser.add_argument("--baseline-lto-option",action="append",default=[],help="explicit additional linker/backend option for baseline exploration")
    parser.add_argument("--baseline-option",action="append",default=[],help="explicit compiler option for baseline exploration")
    parser.add_argument("--shape",choices=["smoke","corners","lto","thinlto"],default="smoke")
    parser.add_argument("--matrix-shapes",nargs="+",choices=["smoke","corners","lto","thinlto"])
    parser.add_argument("--lto-abi",choices=["lp64","lp64f","lp64d"],default="")
    parser.add_argument("--lto-features",action="store_true")
    parser.add_argument("--mode",choices=["baseline","forward","capture"],default="capture")
    parser.add_argument("--debug-lto",choices=["none","lto","thinlto"],default="none")
    parser.add_argument("--debug-split-dwarf",action="store_true")
    parser.add_argument("--debug-case",choices=["normal","already-separated","structured","empty","no-fixup","install-strip","caller-compression","references","reference-violation"],default="normal")
    parser.add_argument("--export-candidate", action="store_true",
                        help="export only the small supervisor output from its bounded build")
    parser.add_argument("--export-offline",action="store_true",help="export a tiny matrix fixture's captured toolchain for an offline round trip")
    args = parser.parse_args()
    if (args.debug_lto != "none" or args.debug_split_dwarf or args.debug_case != "normal") and args.fixture != "separateDebugInfo":
        parser.error("debug-output options require --fixture separateDebugInfo")
    # No arbitrary installables or full matrix enumeration.
    if not args.fixture.replace("-", "").isalnum():
        parser.error("fixture must be an attribute name")
    if args.matrix_shapes and (not args.compiler or len(set(args.matrix_shapes))!=len(args.matrix_shapes)):
        parser.error("a matrix group requires one compiler and distinct shapes")
    if (args.baseline_option or args.baseline_lto_option) and not args.baseline_target:
        parser.error("baseline exploration options require --baseline-target")
    parent = REPO / ".capture-experiments"
    parent.mkdir(exist_ok=True)
    if sum(1 for _ in parent.glob("batch-*")) >= LIMITS["maximum_batches"]:
        raise RuntimeError("archive small reports and clean owned batches before starting another")
    before = headroom(parent)
    batch = Path(tempfile.mkdtemp(prefix="batch-", dir=parent))
    mountpoint = batch / "bounded"
    mountpoint.mkdir()
    limits = dict(LIMITS)
    limits["memory_bytes"]=args.memory_mib*1024**2
    if args.fixture == "quotaProbe":
        limits.update(writable_fs_bytes=64*1024**2,inodes=8192)
    ledger = dict(schema=1, limits=limits, before=before, fixture=args.fixture,
                  revision=checked(["git", "rev-parse", "HEAD"], timeout=10),
                  dirty_diff_sha256=hashlib.sha256(subprocess.check_output(
                      ["git", "diff", "HEAD", "--binary"])).hexdigest(),
                  lock_sha256=hashlib.sha256((REPO / "flake.lock").read_bytes()).hexdigest(),
                  capture_sources=source_identity(),
                  nixpkgs=str(args.nixpkgs), roots=[], cleanup="pending")
    report = batch / "ledger.json"
    atomic_json(report, ledger)
    scope=None
    try:
        tool_arg = ""
        if args.capture_tool:
            tool_arg = "captureTool = builtins.storePath " + json.dumps(str(args.capture_tool.resolve())) + ";"
        expr = f'(import {HERE}/fixtures.nix {{ pkgs = import {args.nixpkgs} {{}}; {tool_arg} }}).{args.fixture}'
        expressions={args.shape:expr}
        if args.baseline_target:
            if args.matrix_shapes or not re.fullmatch(r"[a-zA-Z0-9_-]+",args.target) or args.compiler and not re.fullmatch(r"[a-zA-Z0-9_]+",args.compiler):
                raise RuntimeError("baseline preflight requires one target and optional compiler label")
            options_file=batch/"baseline-options.json"
            atomic_json(options_file,dict(compiler=args.compiler,compiler_options=args.baseline_option,lto_options=args.baseline_lto_option))
            expr=f'let options=builtins.fromJSON (builtins.readFile {options_file}); in import {HERE}/baseline-matrix.nix {{repository={REPO};pkgs=import {args.nixpkgs} {{}};targetLabel={json.dumps(args.target)};compilerLabel=options.compiler;compilerOptions=options.compiler_options;ltoOptions=options.lto_options;}}'
            expressions={"baseline":expr}
            ledger["baseline_target"]=args.target
        if args.compiler and not args.baseline_target:
            if not re.fullmatch(r"[a-zA-Z0-9_]+",args.compiler) or not re.fullmatch(r"[a-zA-Z0-9_-]+",args.target) or not args.capture_tool:
                raise RuntimeError("matrix fixture requires compiler/target labels and --capture-tool")
            expressions={shape:f'import {HERE}/matrix-fixtures.nix {{repository={REPO};pkgs=import {args.nixpkgs} {{}};compilerLabel={json.dumps(args.compiler)};targetLabel={json.dumps(args.target)};mode={json.dumps(args.mode)};shape={json.dumps(shape)};ltoAbi={json.dumps(args.lto_abi)};ltoFeatures={str(args.lto_features).lower()};{tool_arg}}}'
                for shape in (args.matrix_shapes or [args.shape])}
            expr=next(iter(expressions.values()))
            if args.fixture == "separateDebugInfo":
                if args.matrix_shapes:
                    raise RuntimeError("debug-output fixture selects one compiler/target at a time")
                lto = {"none":"", "lto":"-flto", "thinlto":"-flto=thin"}[args.debug_lto]
                expr=f'import {HERE}/debug-fixture.nix {{repository={REPO};pkgs=import {args.nixpkgs} {{}};compilerLabel={json.dumps(args.compiler)};targetLabel={json.dumps(args.target)};ltoMode={json.dumps(lto)};splitDwarf={str(args.debug_split_dwarf).lower()};debugCase={json.dumps(args.debug_case)};{tool_arg}}}'
                expressions={args.shape:expr}
            ledger.update(compiler=args.compiler,target=args.target,mode=args.mode,shape=args.shape,lto_abi=args.lto_abi,lto_features=args.lto_features)
        fixtures=" ".join("("+expression.replace(f"pkgs=import {args.nixpkgs} {{}};","pkgs=matrixPkgs;")+")"
            for expression in expressions.values())
        facts=json.loads(checked(["nix","eval","--impure","--json","--expr",
            f"let matrixPkgs=import {args.nixpkgs} {{}}; fixtures=[{fixtures}]; p=builtins.head fixtures; in {{derivations=map (fixture: fixture.drvPath) fixtures; defaults=p.meta.outputsToInstall or [p.outputName]; infrastructure=p.captureFixtureAllowedDrvs or []; capture_limit=p.datasetCaptureLimitBytes or 67108864; capture_scratch=p.datasetCaptureScratchBytes or 0; offline_root=if p ? captureFixtureOfflineRoot then toString p.captureFixtureOfflineRoot else null;}} "]))
        derivations=dict(zip(expressions,facts.pop("derivations"),strict=True))
        drv=next(iter(derivations.values()))
        ledger["derivation"] = drv
        if args.matrix_shapes:ledger.update(derivations=derivations,shapes=list(derivations))
        defaults = facts["defaults"]
        ledger["default_output_names"] = defaults
        ledger["limits"]["capture_bytes"] = facts["capture_limit"]
        extra_probe=64*1024**2 if args.fixture in ["supervisorIsolation","namespaceProbe"] else 0
        limits["capture_scratch_bytes"]=facts["capture_scratch"]
        limits["extra_probe_bytes"]=extra_probe
        limits["writable_fs_bytes"]-=facts["capture_scratch"]+extra_probe
        limits["inodes"]-=(16384 if facts["capture_scratch"] else 0)+(16384 if extra_probe else 0)
        dry = bounded_run(["nix", "build", "--dry-run", "--no-write-lock-file",
                           "--json", "--max-jobs", "1", "--cores", "1", *(path+"^*" for path in derivations.values())],
                          timeout=args.timeout)
        ledger["dry_run"] = dry
        atomic_json(report, ledger)
        if dry["status"]:
            raise RuntimeError("fixture dry run failed")
        planned = set(re.findall(r"/nix/store/[a-z0-9]{32}-[^\s]+\.drv", dry["stderr"]))
        def infrastructure(path):
            name = Path(path).name[33:]
            return (name in ["dataset-capture-builder.drv","dataset-capture-phases.drv","stdenv-linux.drv"] or name.startswith("capture-")
                    or "-wrapper-" in name and name.endswith("-capture.drv"))
        ledger["declared_infrastructure"] = facts["infrastructure"]
        unexpected = sorted(p for p in planned if p not in derivations.values() and p not in facts["infrastructure"] and not infrastructure(p))
        if unexpected or "will be fetched" in dry["stderr"]:
            ledger["unapproved_prerequisites"] = unexpected
            raise RuntimeError("prerequisite acquisition required; no fixture build started")
        reviewed_path = batch/"reviewed-drvs.json"
        atomic_json(reviewed_path, sorted(planned | set(derivations.values())))
        # Building prerequisites is a separate, explicit batch. Never let a
        # compiler bootstrap or an implicit fetch happen inside this fixture.
        required = checked(["nix-store", "--query", "--requisites", "--include-outputs", *derivations.values()]).splitlines()
        missing = [p for p in required if not Path(p).exists()]
        if missing:
            ledger["missing_prerequisites"] = missing
            raise RuntimeError("missing prerequisites; inspect dry_run before acquisition")
        # Dump registration for exact required paths plus the Nix executable.
        nix_bin = str(Path(shutil.which("nix")).resolve().parent.parent)
        required += checked(["nix-store", "-qR", nix_bin]).splitlines()
        required += checked(["nix-store", "-qR", str(Path(sys.executable).resolve().parent.parent)]).splitlines()
        required = sorted(set(required))
        # A context-bearing text output roots sources, derivations and sibling
        # outputs together without realising any of the derivations.
        paths_file = batch/"input-paths.json"
        atomic_json(paths_file,required)
        root_expr = 'builtins.toFile "capture-input-roots" (builtins.concatStringsSep "\\n" (map builtins.storePath (builtins.fromJSON (builtins.readFile ' + str(paths_file) + '))))'
        root_file = checked(["nix", "eval", "--impure", "--raw", "--expr", root_expr])
        root = batch / "inputs"
        checked(["nix-store", "--realise", root_file, "--add-root", str(root), "--indirect"])
        ledger["roots"] = [str(root)]
        ledger["input_paths"] = required
        registration = batch / "registration"
        with registration.open("wb") as dest:
            subprocess.run(["nix-store", "--dump-db", *required], stdout=dest, check=True)
        atomic_json(report, ledger)
        command = [str(HERE / "isolate.sh"), str(mountpoint), str(registration),
                   sys.executable, str(HERE / "batch.py"), "--inside-build", drv, ",".join(defaults), str(batch/"result.json")]
        ledger["result_report"] = str(batch/"result.json")
        if args.matrix_shapes:
            settings=batch/"matrix-cases.json"
            ledger["result_reports"]={shape:str(batch/("result-"+shape+".json")) for shape in derivations}
            atomic_json(settings,dict(derivations=derivations,defaults=",".join(defaults),reports=ledger["result_reports"]))
            command=[str(HERE/"isolate.sh"),str(mountpoint),str(registration),
                sys.executable,str(HERE/"batch.py"),"--inside-matrix",str(settings)]
        if args.export_candidate:
            if args.fixture != "supervisorPackage":
                raise RuntimeError("candidate export is limited to the supervisor package")
            candidates = sorted(parent.glob("batch-*/candidate.nar"),key=lambda p:p.stat().st_mtime)
            for previous in candidates[:-1]:
                previous.unlink()
            command.append(str(batch / "candidate.nar"))
        scope = "compiler-capture-" + batch.name + ".scope"
        env = dict(os.environ, CAPTURE_SCOPE=scope,CAPTURE_SCRATCH_BYTES=str(limits["writable_fs_bytes"]),
                   CAPTURE_INODES=str(limits["inodes"]),CAPTURE_TEST_MEMORY_BYTES=str(limits["memory_bytes"]),
                   CAPTURE_BUILD_TIMEOUT=str(max(1,args.timeout-60)),CAPTURE_REVIEWED_DRVS_JSON=str(reviewed_path))
        if args.export_offline:
            if not args.compiler or args.mode!="capture" or not facts["offline_root"]:
                raise RuntimeError("offline export requires one explicit captured compiler/target fixture")
            settings=batch/"offline-export.json"
            atomic_json(settings,dict(root=facts["offline_root"],supervisor=str(args.capture_tool.resolve()),
                seed_file=str(paths_file),destination=str(batch/"offline.nar")))
            env["CAPTURE_OFFLINE_EXPORT"]=str(settings)
        ledger["build"] = bounded_run(command, timeout=args.timeout, env=env)
        # Nix sandbox builders can create their own process groups. Cgroup
        # cleanup covers them even if the client was killed on timeout.
        ledger["scope_cleanup"] = bounded_run(
            ["systemctl", "--user", "kill", "--kill-whom=all", scope], timeout=10)
        if ledger["build"]["status"]:
            raise RuntimeError("isolated fixture failed")
    except Exception as error:
        ledger["error"] = str(error)
    finally:
        if scope is not None:
            ledger["scope_cleanup"] = bounded_run(
                ["systemctl", "--user", "kill", "--kill-whom=all", scope], timeout=10)
        for root in ledger["roots"]:
            Path(root).unlink(missing_ok=True)
        (batch / "registration").unlink(missing_ok=True)
        (batch / "input-paths.json").unlink(missing_ok=True)
        mountpoint.rmdir()
        ledger["cleanup"] = "namespace discarded; input roots released"
        ledger["retained"] = usage(batch)
        ledger["after"] = headroom(parent)
        atomic_json(report, ledger)
    print(report)
    if "error" in ledger:
        print(ledger["error"])
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--inside-matrix"]:
        raise SystemExit(inside_matrix(sys.argv[2]))
    if sys.argv[1:2] == ["--rust-session"]:
        parser=argparse.ArgumentParser(description="Bounded development/test session")
        parser.add_argument("--rust-session",action="store_true")
        parser.add_argument("--memory-mib",type=int,choices=[1024,2048,4096],default=1024)
        options=parser.parse_args()
        with experiment_lock("rust-session"):
            rust_session(options.memory_mib)
        raise SystemExit(0)
    if len(sys.argv) in [5,6] and sys.argv[1] == "--inside-build":
        raise SystemExit(inside_build(sys.argv[2], sys.argv[3],sys.argv[4],sys.argv[5] if len(sys.argv)==6 else None))
    with experiment_lock("nix-batch"):
        raise SystemExit(main())

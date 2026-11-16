#!/usr/bin/env python3
"""Bounded real-Nix pilot through eval, streaming planner and build worker.

Run with `nix develop -c python tests/multiversion-flow.py`. External task
transport is local; package evaluation, archives, graph walk and builds are real.
Artifacts stay under the ignored task directory. No scheduler or upload is used.
"""
from __future__ import annotations
# The standalone entry point adds both local packages before importing them.
# ruff: noqa: E402

import dataclasses
import faulthandler
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from compiler_suit_runner.manifest_gen import make_matrix_eval_header, build_compilers_task_id
from compiler_suit_runner.preflight import enumerate_variants, resolve_package_selection
from compiler_suit_runner.publication import variant_complete
from compiler_suit_runner.streamed_spawn import decode_spawn_message, SPAWN_TOPIC
from compiler_suit_runner.workers.build_worker import BuildWorkerEnv, build_worker
from compiler_suit_runner.workers.dependency_graph_worker import run_dependency_graph_task
from compiler_suit_runner.workers.eval_worker import run_eval_task
from template_graph.make_sum_drv import make_wrapper_drv_from_paths

WORK = ROOT / ".multiversion-work"
WORK.mkdir(exist_ok=True)
os.environ["DYNRUNNER_PUBLISH_SRC_ROOT"] = str(WORK / "staging")
os.environ["DYNRUNNER_PUBLISH_DST_ROOT"] = str(WORK / "published")
report = {"started": time.time(), "builds": [], "commands": [], "archives": {}}
if os.environ.get("MULTIVERSION_TRACE"):
    faulthandler.dump_traceback_later(60, repeat=True)


def run(argv, *, input=None):
    if argv[:2] == ["nix", "build"]:
        argv = [*argv, "--max-jobs", "1", "--cores", "1"]
        dry = subprocess.run([*argv, "--dry-run"], capture_output=True, timeout=120)
        (WORK / "logs" / f"flow-dry-{len(report['builds'])}.log").write_bytes(dry.stderr[-65536:])
        if dry.returncode:
            return dry.stdout, dry.stderr, dry.returncode
        # Every prerequisite must already be available. A compiler/bootstrap
        # build is outside this test. Only hello and its ELF folder may build.
        for line in dry.stderr.decode(errors="replace").splitlines():
            if line.startswith("  /nix/store/") and line.endswith(".drv"):
                if "hello" not in line:
                    raise RuntimeError(f"unbudgeted prerequisite build: {line}")
    if argv[:2] == ["nix-store", "--export"]:
        paths = argv[2:] if input is None else input.decode().splitlines()
        sizes = subprocess.run(["nix-store", "--query", "--size", *paths], capture_output=True, check=True, timeout=120)
        total = sum(int(line) for line in sizes.stdout.splitlines())
        if total > 256 * 1024**2:
            raise RuntimeError(f"archive exceeds the 256 MiB test allowance: {total}")
    result = subprocess.run(argv, input=input, capture_output=True, timeout=300)
    report["commands"].append({"command": argv[:3], "returncode": result.returncode})
    if argv[:2] == ["nix", "build"]:
        report["builds"].append({"argv": argv, "returncode": result.returncode})
        (WORK / "logs" / f"flow-build-{len(report['builds'])}.log").write_bytes(result.stderr[-65536:])
    return result.stdout, result.stderr, result.returncode


def evaluate(expr):
    output, error, code = run(["nix", "eval", "--impure", "--json", "--expr", expr])
    if code:
        raise RuntimeError(error.decode(errors="replace"))
    return json.loads(output)


class LocalTransport:
    def __init__(self):
        self.outputs = {}
        self.messages = []

    def publish(self, source, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)

    def publish_string(self, key, value):
        self.outputs[key] = value

    def send_message(self, topic, message):
        self.messages.append((topic, message))


def main():
    print("Resolve the two pinned releases", flush=True)
    if shutil.disk_usage(WORK).free < 100 * 1024**3:
        raise RuntimeError("below the 100 GiB free-space reserve")
    catalog = resolve_package_selection(str(ROOT), "x86_64-linux", packages=["hello"], all_package_versions=True, archs=["x86_64", "aarch64"])
    assert len(catalog) == 2 and {v["packageVersion"] for v in catalog.values()} == {"2.12.1", "2.12.3"}
    entries = enumerate_variants(str(ROOT), "x86_64-linux", selected_catalog=catalog, archs=["x86_64", "aarch64"])
    context = evaluate("(import ./tests/multiversion-fixtures.nix).context")
    print("Prepare the shared toolchain archive", flush=True)
    suffix = "gcc15-O2-baseline-none-san-off-march-default"
    toolchains = make_wrapper_drv_from_paths(drvs=context["toolchains"], name="toolchains", system="x86_64-linux")
    matrix_dir = WORK / "matrix"
    matrix_dir.mkdir(exist_ok=True)
    archive, error, code = run(["nix-store", "--query", "--requisites", toolchains])
    if code:
        raise RuntimeError(error.decode())
    exported, error, code = run(["nix-store", "--export", *archive.decode().splitlines()])
    if code:
        raise RuntimeError(error.decode())
    (matrix_dir / "toolchains.drv.archive").write_bytes(exported)
    matrices = {}
    for package_id, entry in entries.items():
        print(f"Evaluate and archive {package_id}", flush=True)
        header = make_matrix_eval_header(package_id, "x86_64-linux", entry["archs"],
                                        toolchain_aggregate_drv=toolchains, package_metadata=entry)
        payload = {**header.payload, "suffixes": [suffix]}
        transport = LocalTransport()
        result = run_eval_task(payload, matrix_dir, transport, run_subprocess=run, flake_ref=str(ROOT))
        assert len(result["variants"]) == 2
        matrices[package_id] = transport.outputs["matrix_aggregate_drv"]
        path = matrix_dir / f"matrix-{package_id}.drv.archive"
        report["archives"][package_id] = {"name": path.name, "size": path.stat().st_size}
    transport = LocalTransport()
    print("Plan the combined dependency graph and stream descriptors", flush=True)
    graph = run_dependency_graph_task(task=transport, matrix_eval_out_dir=matrix_dir,
                                     bash_path=context["bash"], toolchain_aggregate_drv=toolchains,
                                     matrix_drvs=matrices, run_subprocess=run,
                                     toolchain_task_ids={Path(drv).name.removesuffix(".drv"): build_compilers_task_id("x86_64-linux", arch, "gcc15")
                                                         for arch, drv in zip(["x86_64", "aarch64"], context["toolchains"])})
    report["graph"] = dataclasses.asdict(graph)
    decoded = [decode_spawn_message(message) for topic, message in transport.messages if topic == SPAWN_TOPIC]
    descriptors = [descriptor for batch in decoded for descriptor in batch["descriptors"]]
    variants = [d for d in descriptors if d.kind == "build_variant"]
    assert len(variants) == 4 and len({d.task_id for d in variants}) == 4
    common = [d for d in descriptors if d.kind == "build_common_dep"]
    assert len({d.task_id for d in common}) == len(common)
    assert graph.variants == 4
    assert graph.violations == 0
    assert all(len(d.build_compilers_depends_on) == 1 for d in variants)
    env = BuildWorkerEnv(flake_ref=str(ROOT), dataset_output_dir=WORK / "staging" / "dataset", run_subprocess=run)
    from dynamic_runner.worker.publish import publish_all
    published_dataset = WORK / "published" / "dataset"
    results = []
    for descriptor in variants:
        print(f"Build and validate {descriptor.payload['pkg']}/{descriptor.payload['arch']}", flush=True)
        payload = descriptor.payload
        assert payload["packageIdentity"] == catalog[payload["pkg"]]["packageIdentity"]
        header = {"item_class": "build_variant", "name": descriptor.name, "payload": payload}
        built = build_worker(Path("unused"), env, manifest_data=header)
        assert built.success, built.error
        staging_root = WORK / "staging"
        publish_all([(path, WORK / "published" / path.relative_to(staging_root)) for path in built.staged_outputs])
        folder = published_dataset / payload["pkg"]
        assert variant_complete(folder, payload)
        elf = folder / payload["variant_dir"] / "hello"
        machine = struct.unpack("<H", elf.read_bytes()[18:20])[0]
        assert machine == (62 if payload["arch"] == "x86_64" else 183)
        version_line = None
        if payload["arch"] == "x86_64":
            version_line = subprocess.check_output([str(elf), "--version"], timeout=10).decode().splitlines()[0]
            assert payload["packageVersion"] in version_line
        # A second attempt must perform no Nix work and retain the other
        # release. This exercises the actual worker's completion fast path.
        resumed = build_worker(Path("unused"), dataclasses.replace(env, dataset_resume_dir=published_dataset,
                              run_subprocess=lambda _: (_ for _ in ()).throw(AssertionError("resume ran Nix"))), manifest_data=header)
        assert resumed.success and not resumed.staged_outputs
        stale = {**payload, "packageIdentity": "0" * 64}
        assert not variant_complete(folder, stale)
        out = built.outpath
        subprocess.run(["nix-store", "--add-root", str(WORK / "roots" / f"pilot-{payload['pkg']}-{payload['arch']}"),
                        "--indirect", "--realise", out], check=True, stdout=subprocess.DEVNULL)
        results.append({"packageId": payload["pkg"], "version": payload["packageVersion"], "arch": payload["arch"],
                        "packageIdentity": payload["packageIdentity"], "recipeId": payload["recipeId"], "sourceIdentity": payload["sourceIdentity"],
                        "drv": payload["drv"], "output": out, "machine": machine, "version_output": version_line})
    report.update({"results": results, "graph": dataclasses.asdict(graph), "common_dep_tasks": len(common),
                   "toolchain_ids": sorted({dep for d in variants for dep in d.build_compilers_depends_on}),
                   "atomic_publication": True, "passed": True})
    print("Two-release native/cross flow and resume passed", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        report["finished"] = time.time()
        (WORK / "flow-report.json").write_text(json.dumps(report, sort_keys=True, indent=2, default=str) + "\n")

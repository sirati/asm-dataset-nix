#!/usr/bin/env python3
"""Compare a prior commit with the working tree without building packages.

Run inside nix develop. Snapshots and logs are task-owned under the ignored
.multiversion-work/performance directory; no Git checkout or worktree is made.
"""

import argparse
import io
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", default="5e968fc")
parser.add_argument("--repeats", type=int, default=3)
args = parser.parse_args()
if not 1 <= args.repeats <= 10:
    parser.error("--repeats must be between 1 and 10")
GNU_TIME = shutil.which("time")
if GNU_TIME is None:
    parser.error("GNU time is required; run this script inside nix develop")

base = pathlib.Path(".multiversion-work/performance").resolve()
base.mkdir(parents=True, exist_ok=True)
(base / "logs").mkdir(exist_ok=True)
for phase in ("before", "after"):
    destination = base / f"source-{phase}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
revision = (
    subprocess.check_output(
        [
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            args.baseline + "^{commit}",
        ]
    )
    .decode()
    .strip()
)
archive = subprocess.check_output(["git", "archive", "--format=tar", revision])
with tarfile.open(fileobj=io.BytesIO(archive)) as files:
    files.extractall(base / "source-before", filter="data")
for item in subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0"):
    if not item:
        continue
    source = ROOT / item
    if source.is_file():
        destination = base / "source-after" / item
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
report = {}
for repeat in range(args.repeats):
    for name in ["selection", "selected_variant"]:
        for phase in ["before", "after"]:
            source = base / f"source-{phase}"
            ref = "path:" + str(source)
            if name == "selection":
                script = f"import sys;sys.path[:0]=[{str(source / 'python')!r},{str(source)!r}];from compiler_suit_runner.preflight import enumerate_variants;import json;print(json.dumps(enumerate_variants({ref!r},'x86_64-linux',packages=['hello'],archs=['x86_64'])))"
                profile = pathlib.Path(".multiversion-work/roots/devshell")
                command = (
                    [
                        "nix",
                        "develop",
                        str(profile.resolve()),
                        "-c",
                        "python",
                        "-c",
                        script,
                    ]
                    if profile.exists()
                    else [sys.executable, "-c", script]
                )
            elif phase == "before":
                command = [
                    "nix",
                    "eval",
                    "--no-eval-cache",
                    "--raw",
                    f"{ref}#_drvPaths.x86_64-linux.hello.x86_64.gcc15-O2-baseline-none-san-off-march-default",
                ]
            else:
                command = [
                    "nix",
                    "eval",
                    "--no-eval-cache",
                    "--json",
                    f"{ref}#_selectDataset.x86_64-linux.hello",
                    "--apply",
                    'f: builtins.mapAttrs (_: v: builtins.mapAttrs (_: d: d.drvPath) v) (f { x86_64=["gcc15-O2-baseline-none-san-off-march-default"]; })',
                ]
            start = time.perf_counter()
            p = subprocess.run(
                ["timeout", "180", GNU_TIME, "-v", *command], capture_output=True
            )
            elapsed = time.perf_counter() - start
            (base / "logs" / f"benchmark-{name}-{phase}-{repeat}.log").write_bytes(
                p.stderr[-65536:]
            )
            if p.returncode:
                raise RuntimeError(p.stderr.decode()[-4000:])
            rss = int(
                re.search(rb"Maximum resident set size \(kbytes\): (\d+)", p.stderr)[1]
            )
            entry = {
                "seconds": round(elapsed, 4),
                "max_rss_kib": rss,
                "stdout_bytes": len(p.stdout),
            }
            if name == "selected_variant":
                entry["drv"] = (
                    p.stdout.decode().strip()
                    if phase == "before"
                    else next(iter(json.loads(p.stdout)["x86_64"].values()))
                )
            report.setdefault(name, {}).setdefault(phase, []).append(entry)
            (base / "benchmarks.json").write_text(json.dumps(report, indent=2) + "\n")
            print(name, phase, repeat, entry, flush=True)
for name, cases in report.items():
    cases["median"] = {
        phase: {
            "seconds": statistics.median(v["seconds"] for v in values),
            "max_rss_kib": statistics.median(v["max_rss_kib"] for v in values),
        }
        for phase, values in cases.items()
        if phase != "median"
    }
if "selected_variant" in report:
    assert (
        len(
            {
                v["drv"]
                for phase in ("before", "after")
                for v in report["selected_variant"][phase]
            }
        )
        == 1
    )
(base / "benchmarks.json").write_text(json.dumps(report, indent=2) + "\n")

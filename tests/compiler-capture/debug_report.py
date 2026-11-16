"""Behavior, source-line and runtime evidence for the actual debug-hook fixture."""
import importlib.util
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def compare(compiler, target, version, driver, cxx_driver, report_path):
    environment = {key: value for key, value in os.environ.items() if key != "DATASET_CAPTURE_STAGE"}
    for executable in [driver, cxx_driver]:
        actual = subprocess.run([executable, "--version"], capture_output=True, check=True, env=environment).stdout
        if not re.search(rb"(?<![0-9.])" + re.escape(version.encode()) + rb"(?![0-9.])", actual):
            raise RuntimeError("debug fixture used a different C/C++ compiler version")
    capabilities = module("debug_capabilities", os.environ["CAPTURE_CAPABILITIES"])
    before = capabilities.section_hashes(Path("baseline-program"), behavior=True)
    after = capabilities.section_hashes(Path("program"), behavior=True)
    if before != after:
        changed = [key for key in set(before) | set(after) if before.get(key) != after.get(key)]
        raise RuntimeError("required-debug baseline/capture behavior mismatch: " + str(changed))
    raw = Path("program").read_bytes()[:64]
    machine = struct.unpack_from("<H" if raw[5] == 1 else ">H", raw, 18)[0]
    expected = {"x86_64": (62, 2, 1), "i686": (3, 1, 1), "aarch64": (183, 2, 1),
        "armv7l-hf": (40, 1, 1), "armv7l-sf": (40, 1, 1), "mipsel": (8, 1, 1),
        "mips64el": (8, 1, 1), "ppc32": (20, 1, 2), "ppc64": (21, 2, 2), "riscv64": (243, 2, 1)}[target]
    if raw[:4] != b"\x7fELF" or (machine, raw[4], raw[5]) != expected:
        raise RuntimeError("debug fixture emitted an incorrect target ELF")
    order = "<" if raw[5] == 1 else ">"
    flags = struct.unpack_from(order+"I",raw,48 if raw[4]==2 else 36)[0]
    if (target == "mips64el" and not flags & 0x20 or target == "mipsel" and flags & 0x20
            or target == "armv7l-hf" and not flags & 0x400
            or target == "armv7l-sf" and flags & 0x400
            or target == "ppc64" and flags & 3 != 2):
        raise RuntimeError("debug fixture emitted an incorrect target ABI flag")
    Path(report_path).write_text(json.dumps(dict(schema=1, compiler=compiler, target=target,
        version=version, behavior_equal=True, target_abi=True), sort_keys=True) + "\n")


def installed(target, runtime, program, debug_root, readelf, report_path):
    report = json.loads(Path(report_path).read_bytes())
    debug_files = [path for path in Path(debug_root).rglob("*.debug") if path.is_file() and not path.is_symlink()]
    mapping = False
    decoded = []
    for path in debug_files:
        result = subprocess.run([readelf, "--debug-dump=decodedline", path], capture_output=True, check=True)
        decoded.append(result.stdout[-1024:].decode("utf-8", "backslashreplace"))
        mapping |= bool(re.search(rb"(?:unit.c|main.cpp)\s+1\s+(?:0x[0-9a-f]+|0)(?:\s|$)", result.stdout))
    if not mapping:
        notes = subprocess.run([readelf, "-n", program], capture_output=True).stdout[-1600:].decode("utf-8", "backslashreplace")
        commands = []
        stage = Path(os.environ["DATASET_CAPTURE_STAGE"])
        for path in sorted((stage/"records").glob("*.json"))[:8]:
            if path.stat().st_size > 4*1024**2:
                continue
            record = json.loads(path.read_bytes())
            for artifact in record.get("artifacts", []):
                if artifact["role"] != "command" or artifact["size"] > 65536:
                    continue
                command = json.loads((stage/"objects"/artifact["sha256"]).read_bytes())
                for execution in command.get("execs", [command]):
                    executable = os.fsdecode(bytes.fromhex(execution.get("executable_hex", "")))
                    if "collect" in executable or "/ld" in executable or "-ld" in executable:
                        commands.append(dict(program=executable, args=[os.fsdecode(bytes.fromhex(value))[:160]
                            for value in execution.get("arguments_hex", [])[:24]]))
        raise RuntimeError("Nix separate debug output lacks source-line mappings: " + json.dumps(dict(
            files=[str(path.relative_to(debug_root)) for path in list(Path(debug_root).rglob("*"))[:20]],
            readelf=os.environ.get("READELF"), objcopy=os.environ.get("OBJCOPY"), notes=notes, decoded=decoded,
            build_id_env={key:value for key,value in os.environ.items() if key.startswith("NIX_SET_BUILD_ID")}, commands=commands[:10])))
    run = subprocess.run([*json.loads(runtime), program], capture_output=True, timeout=20)
    if run.returncode:
        raise RuntimeError("installed debug fixture runtime failed: " + repr(run.stderr[-1024:]))
    report.update(debug_source_mapping=True, runtime_ok=True,
        runtime_kind="native" if target == "x86_64" else "emulated", separate_debug_files=len(debug_files))
    Path(report_path).write_text(json.dumps(report, sort_keys=True) + "\n")


if sys.argv[1] == "compare":
    compare(*sys.argv[2:])
elif sys.argv[1] == "installed":
    installed(*sys.argv[2:])
else:
    raise SystemExit("unknown debug fixture report mode")

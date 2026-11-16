"""Exploratory subprocess redirection, not a supported compiler backend.

Only compiler-created auxiliary paths are remapped. Package source paths,
the primary output, cwd, streams and the driver's job order stay intact.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

configuration = json.loads(Path(sys.argv[1]).read_text())
root = Path(configuration["root"])
primary = bytes.fromhex(configuration["primary_hex"]) if configuration["primary_hex"] else None
if configuration.get("program"):
    program = configuration["program"]
    args = sys.argv[2:]
else:
    program = sys.argv[2]
    args = sys.argv[3:]
original = [os.fsencode(arg) for arg in args]
rewritten = list(original)
output_index = next((i+1 for i,arg in enumerate(original[:-1]) if arg==b"-o"),None)
name = Path(program).name
linking = name in ["collect2","ld","ld.bfd","ld.gold","ld.lld"] or name.endswith("-ld")
with (root/"lock").open("a+b") as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    state_path = root/"state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else dict(serial=0,paths={})
    state["serial"] += 1
    serial = state["serial"]
    occurrences = {}
    for i,arg in enumerate(original):
        if i==output_index or arg.startswith(b"-") and arg not in [b"-.i",b"-.bc",b"-.s",b"-.o"]:
            continue
        key = os.path.abspath(arg).hex()
        versions = state["paths"].get(key)
        if versions:
            if linking:
                index = occurrences.get(key,0)
                if index>=len(versions):
                    raise RuntimeError("ambiguous repeated linker operand in subprocess experiment")
                rewritten[i] = bytes.fromhex(versions[index])
                occurrences[key] = index+1
            else:
                rewritten[i] = bytes.fromhex(versions[-1])
    output = None
    if output_index is not None:
        wanted = os.path.abspath(original[output_index])
        if wanted!=primary and original[output_index] not in [b"-",b"/dev/null"]:
            extension = os.path.splitext(wanted)[1]
            output = os.fsencode(root)+b"/job-"+str(serial).encode()+extension
            rewritten[output_index] = output
            state["paths"].setdefault(wanted.hex(),[]).append(output.hex())
    state_path.write_text(json.dumps(state,sort_keys=True))
    fcntl.flock(lock,fcntl.LOCK_UN)
result = subprocess.run([os.fsencode(program),*rewritten])
record = dict(program=os.fsencode(program).hex(),arguments=[a.hex() for a in original],
              rewritten=[a.hex() for a in rewritten],status=result.returncode,
              emitted=output.hex() if output and os.path.isfile(output) else None)
(root/f"job-{serial}.json").write_text(json.dumps(record,sort_keys=True))
if result.returncode<0:
    signal.signal(-result.returncode,signal.SIG_DFL)
    os.kill(os.getpid(),-result.returncode)
raise SystemExit(result.returncode)

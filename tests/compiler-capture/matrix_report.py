import hashlib
import json
from pathlib import Path
import struct
import sys
import importlib.util
import os
import re
import subprocess

compiler,target,mode,output=sys.argv[1:]
report=dict(schema=1,compiler=compiler,target=target,mode=mode,files={})
report["shape"]=os.environ.get("CAPTURE_MATRIX_SHAPE","smoke")
report["driver_facts"]={}
for name in ["c-driver-version.txt", "cxx-driver-version.txt", "driver-target.txt"]:
    value=Path(name).read_bytes()
    if name!="driver-target.txt" and not re.search(rb"(?<![0-9.])"+re.escape(os.environ["CAPTURE_EXPECTED_VERSION"].encode())+rb"(?![0-9.])",value):
        raise RuntimeError(f"actual driver version contradicts the public matrix: {compiler}/{target}/{name}: {value[:1024]!r}")
    if mode=="capture" and any(value!=(Path(".smoke-"+comparison)/name).read_bytes() for comparison in ["baseline","forward"]):
        raise RuntimeError(f"driver identity changed: {compiler}/{target}/{name}")
    report["driver_facts"][name]=value.decode("utf-8","backslashreplace")[:2048]
spec=importlib.util.spec_from_file_location("capabilities",os.environ["CAPTURE_CAPABILITIES"])
capabilities=importlib.util.module_from_spec(spec)
spec.loader.exec_module(capabilities)
for name in ["unit.o","unit-cxx.o","program","unit.i"]:
    data=Path(name).read_bytes()
    item=dict(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    if data[:4]==b"\x7fELF":
        item["machine"]=struct.unpack_from("<H" if data[5]==1 else ">H",data,18)[0]
        item["class"]=data[4]
        expected={"x86_64":(62,2,1),"i686":(3,1,1),"aarch64":(183,2,1),
            "armv7l-hf":(40,1,1),"armv7l-sf":(40,1,1),"mipsel":(8,1,1),
            "mips64el":(8,1,1),"ppc32":(20,1,2),"ppc64":(21,2,2),"riscv64":(243,2,1)}[target]
        if (item["machine"],data[4],data[5])!=expected:
            raise RuntimeError(f"incorrect ELF target for {compiler}/{target}/{name}")
        order="<" if data[5]==1 else ">"
        wide=data[4]==2
        phoff=struct.unpack_from(order+("Q" if wide else "I"),data,32 if wide else 28)[0]
        phsize,phcount=struct.unpack_from(order+"HH",data,54 if wide else 42)
        for index in range(phcount):
            fields=struct.unpack_from(order+("IIQQQQQQ" if wide else "IIIIIIII"),data,phoff+index*phsize)
            if fields[0]==3:
                offset,length=(fields[2],fields[5]) if wide else (fields[1],fields[4])
                item["interpreter_hex"]=data[offset:offset+length].rstrip(b"\0").hex()
        sections=capabilities.section_hashes(Path(name))
        item["sections"]={key:sections[key] for key in [".text",".data",".rodata"] if key in sections}
        behavior=capabilities.section_hashes(Path(name),behavior=True)
        item["behavior_sections"]=behavior
        if mode=="capture":
            for comparison in ["baseline","forward"]:
                # The behavior comparison includes every allocated section.
                # For RISC-V objects it also checks relocation-declared NOP
                # padding, which the linker may remove. A second raw-byte
                # comparison would reject that validated normalization.
                reference=capabilities.section_hashes(Path(".smoke-"+comparison)/name,behavior=True)
                changed=sorted(key for key in set(reference)|set(behavior) if reference.get(key)!=behavior.get(key))
                if changed:
                    original=(Path(".smoke-"+comparison)/name).read_bytes()
                    family="clang" if compiler.startswith("clang") else "gcc"
                    version=os.environ["CAPTURE_EXPECTED_VERSION"]
                    ppc_sections=capabilities.ppc_debug_sections(original,data,family=family,version=version)
                    aarch64_sections=capabilities.aarch64_debug_sections(original,data,family=family,version=version)
                    x86_sections=capabilities.x86_debug_sections(original,data,family=family,version=version)
                    prologue_swap=capabilities.aarch64_debug_prologue_swap(
                        bytes.fromhex(capabilities.section_hashes(None,raw=original,contents=True).get(".text","")),
                        bytes.fromhex(capabilities.section_hashes(None,raw=data,contents=True).get(".text","")),
                        original[:64],data[:64],family=family,version=version)
                    debug=capabilities.section_hashes(Path(".smoke-debug")/name,behavior=True)
                    accepted=capabilities.compatible_behavior(reference,behavior,debug,
                        prologue_swap=prologue_swap,ppc_sections=ppc_sections,aarch64_sections=aarch64_sections,x86_sections=x86_sections)
                    if accepted:
                        item.setdefault("debug_policy_references",{})[comparison]=accepted
                        continue
                    before=capabilities.section_hashes(Path(".smoke-"+comparison)/name,contents=True)
                    after=capabilities.section_hashes(Path(name),contents=True)
                    difference={key:dict(before=before.get(key,"")[:1024],after=after.get(key,"")[:1024],
                        before_metadata=reference.get(key),after_metadata=behavior.get(key)) for key in changed[:8]}
                    raise RuntimeError(f"{comparison} allocated/relocation section mismatch for {compiler}/{target}/{name}: {difference}")
    elif data[:4] in [b"BC\xc0\xde",bytes([0xde,0xc0,0x17,0x0b])] and report["shape"] in ["lto","thinlto"] and compiler.startswith("clang"):
        item["format"]="llvm-bitcode"
        # Native code and relocations are compared in the linked executable;
        # debug generation intentionally changes the serialized input bitcode.
    elif mode=="capture":
        for comparison in ["baseline","forward"]:
            if data != (Path(".smoke-"+comparison)/name).read_bytes():
                raise RuntimeError(f"{comparison} preprocessor mismatch for {compiler}/{target}")
    report["files"][name]=item
report["compared_baseline_and_forward"]=mode=="capture"
runtime_prefix=json.loads(os.environ.get("CAPTURE_RUNTIME_PREFIX","[]"))
if target=="x86_64" or runtime_prefix:
    report["runtime"]={}
    for comparison,path in [("baseline",Path(".smoke-baseline/program")),("forward",Path(".smoke-forward/program")),("capture",Path("program"))] if mode=="capture" else [(mode,Path("program"))]:
        try:
            result=subprocess.run([*runtime_prefix,str(path.resolve())],capture_output=True,timeout=10)
            report["runtime"][comparison]=dict(status=result.returncode,stderr=result.stderr[-1024:].hex())
        except OSError as error:
            report["runtime"][comparison]=dict(status=127,error=str(error))
    report["runtime_ok"]=all(result["status"]==0 for result in report["runtime"].values())
else:
    report["runtime_ok"]=False
report["runtime_kind"]="emulated" if runtime_prefix else "native" if target=="x86_64" else "unavailable"
report["debug_source_mapping"] = mode=="capture" and any(re.search(
    r"unit.c\s+1\s+(?:0x[0-9a-f]+|0)(?:\s|$)",line) for line in Path("source-lines.txt").read_text().splitlines())
Path(output).write_text(json.dumps(report,sort_keys=True)+"\n")

"""Compare real baseline, forwarding and captured commands in the same directory.

Only an optional feature rejected by the baseline may be unavailable. A failure
after baseline success is always a failure. Cross programs use a selected emulator.
"""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location("corner_capabilities", os.environ.get(
    "CAPTURE_CAPABILITIES", Path(__file__).with_name("capabilities.py")))
capabilities = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capabilities)


class MatrixCorners:
    def __init__(self, baseline, adapted, family, version, target):
        self.drivers = {"baseline": baseline, "forward": adapted, "capture": adapted}
        self.family, self.version, self.target = family, tuple(map(int, version.split("."))), target
        self.cases = []

    def run(self, name, commands, outputs, *, files=None, optional=False,
            stdin=None, expected_status=0, opaque=()):
        try:
            self._run(name, commands, outputs, files=files, optional=optional,
                stdin=stdin, expected_status=expected_status, opaque=opaque)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            self.cases.append(dict(name=name, status="failed", error=str(error)[-4096:]))
            print(f"corner failure: {error}", file=sys.stderr, flush=True)

    def _run(self, name, commands, outputs, *, files, optional,
            stdin, expected_status, opaque):
        root = Path("corner-" + name)
        root.mkdir()
        for filename, data in (files or {"unit.c": SOURCE}).items():
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        result = dict(name=name, status="passed", modes={})
        reference = {}
        reference_text = {}
        reference_headers = {}
        reference_data = {}
        debug_reference = None
        for mode in ["baseline", "forward", "capture"]:
            for filename in outputs:
                (root / filename).unlink(missing_ok=True)
            env = dict(os.environ)
            if mode != "capture":
                env.pop("DATASET_CAPTURE_STAGE", None)
            observations = []
            for driver, arguments in commands:
                call = subprocess.run([self.drivers[mode][driver], *arguments], cwd=root,
                    env=env, input=stdin, capture_output=True, timeout=30)
                observation = dict(status=call.returncode, stdout_hex=call.stdout.hex(),
                    stderr=call.stderr.decode("utf-8", "backslashreplace")[-2048:])
                observations.append(observation)
                if call.returncode != expected_status:
                    if mode == "baseline" and optional and call.returncode > 0:
                        result.update(status="baseline-unsupported", modes={mode: observations})
                        self.cases.append(result)
                        return
                    raise RuntimeError(f"{name}/{mode}: expected status {expected_status}: {observation}")
            result["modes"][mode] = observations
            if mode != "baseline":
                if [(x["status"], x["stdout_hex"]) for x in observations] != [
                        (x["status"], x["stdout_hex"]) for x in result["modes"]["baseline"]]:
                    raise RuntimeError(f"{name}/{mode}: status/stdout mismatch")
            for filename in outputs:
                path = root / filename
                if not path.is_file():
                    driver, arguments = commands[-1]
                    diagnostic = subprocess.run([self.drivers[mode][driver], "-###", *arguments],
                        cwd=root, env=env, capture_output=True, timeout=10).stderr.decode(errors="replace")[-3000:]
                    raise RuntimeError(f"{name}/{mode}: missing output {filename}: {observations}: {diagnostic}")
                if filename in opaque:
                    if not path.stat().st_size:
                        raise RuntimeError(f"{name}/{mode}: empty output {filename}")
                    continue
                data = path.read_bytes()
                value = capabilities.section_hashes(path, behavior=True) if data[:4] == b"\x7fELF" else data
                if mode == "baseline":
                    reference[filename] = value
                    reference_headers[filename]=data[:64]
                    reference_data[filename]=data
                    if isinstance(value,dict):reference_text[filename]=capabilities.section_hashes(path,contents=True).get(".text","")
                elif value != reference[filename]:
                    prologue_swap=isinstance(value,dict) and capabilities.aarch64_debug_prologue_swap(
                        bytes.fromhex(reference_text.get(filename,"")),bytes.fromhex(capabilities.section_hashes(path,contents=True).get(".text","")),
                        reference_headers[filename],data[:64],family=self.family,version=".".join(map(str,self.version)))
                    ppc_sections=capabilities.ppc_debug_sections(reference_data[filename],data,family=self.family,version=".".join(map(str,self.version))) if isinstance(value,dict) else set()
                    aarch64_sections=capabilities.aarch64_debug_sections(reference_data[filename],data,family=self.family,version=".".join(map(str,self.version))) if isinstance(value,dict) else set()
                    x86_sections=capabilities.x86_debug_sections(reference_data[filename],data,family=self.family,version=".".join(map(str,self.version))) if isinstance(value,dict) else set()
                    if mode=="capture" and isinstance(value,dict) and (prologue_swap or ppc_sections or aarch64_sections or x86_sections or capabilities.debug_unwind_difference(reference[filename],value)):
                        if debug_reference is None:
                            retained={output_name:(root/output_name).read_bytes() for output_name in outputs}
                            debug_flags=["-g", "-gdwarf-2" if self.family=="gcc" else "-gdwarf-4"]
                            debug_env={key:value for key,value in env.items() if key!="DATASET_CAPTURE_STAGE"}
                            for driver, arguments in commands:
                                call=subprocess.run([self.drivers["baseline"][driver],*arguments,*debug_flags],cwd=root,
                                    env=debug_env,input=stdin,capture_output=True,timeout=30)
                                if call.returncode!=expected_status:
                                    raise RuntimeError(f"{name}/debug-reference: {call.stderr[-2048:]!r}")
                            debug_reference={output_name:capabilities.section_hashes(root/output_name,behavior=True) for output_name in outputs
                                if (root/output_name).read_bytes()[:4]==b"\x7fELF"}
                            for output_name,data in retained.items():(root/output_name).write_bytes(data)
                        comparison=capabilities.compatible_behavior(reference[filename],value,debug_reference.get(filename),prologue_swap=prologue_swap,ppc_sections=ppc_sections,aarch64_sections=aarch64_sections,x86_sections=x86_sections)
                        if comparison:
                            result.setdefault("debug_policy_references",[]).append(filename)
                            result.setdefault("behavior_comparisons",{})[filename]=comparison
                            continue
                    changed = sorted(key for key in set(value) | set(reference[filename])
                        if value.get(key) != reference[filename].get(key)) if isinstance(value, dict) else ["bytes"]
                    details = {}
                    if isinstance(value, dict):
                        details = {key: dict(before=reference[filename].get(key), after=value.get(key)) for key in changed[:4]}
                    raise RuntimeError(f"{name}/{mode}/{filename}: behavior mismatch in {changed}: {details}")
            runtime_prefix=json.loads(os.environ.get("CAPTURE_RUNTIME_PREFIX","[]"))
            if (self.target=="x86_64" or runtime_prefix) and "program" in outputs:
                runtime_arguments={"normal":[]}
                if name.startswith("cxx-runtime"):runtime_arguments["throw"]=["exercise-exceptions"]
                for path,arguments in runtime_arguments.items():
                    runtime=subprocess.run([*runtime_prefix,str((root/"program").resolve()),*arguments],capture_output=True,timeout=15)
                    result.setdefault("runtime",{}).setdefault(mode,{})[path]=runtime.returncode
                    if runtime.returncode:
                        raise RuntimeError(f"{name}/{mode}/{path}: native executable failed: {runtime.returncode}: {runtime.stderr[-1024:]!r}")
        self.cases.append(result)


SOURCE = '''
int values[] = {1, 3, 7};
const char *file = __FILE__;
const char *base = __BASE_FILE__;
int line = __LINE__, first = __COUNTER__, second = __COUNTER__;
int square(int n) { return n*n + values[0] - 1; }
int main(void) { return square(3) != 9; }
'''


def main():
    baseline, adapted, c, cxx, family, version, target, output = sys.argv[1:]
    drivers = lambda root: {"c": str(Path(root) / "bin" / c), "cxx": str(Path(root) / "bin" / cxx)}
    suite = MatrixCorners(drivers(baseline), drivers(adapted), family, version, target)
    def compile_case(name, args, **kwargs):
        suite.run(name, [("c", [*args, "-c", "unit.c", "-o", "unit.o"])], ["unit.o"], **kwargs)
    for flag in ["-O0", "-O1", "-O2", "-O3", "-Os"]:
        compile_case(flag[1:], [flag])
    if family == "clang":
        compile_case("Oz", ["-Oz"])
        compile_case("line-tables-only", ["-gline-tables-only", "-O2"])
    if family == "clang" or suite.version >= (4, 6):
        compile_case("Ofast", ["-Ofast"])
    for flag in ["-pipe", "-ffunction-sections", "-fPIC", "-fno-omit-frame-pointer"]:
        compile_case(flag[1:], [flag, "-O2"])
    if target == "mips64el":
        compile_case("n32-integer-types", ["-O2"], files={"unit.c": '''
typedef char size_type[__builtin_types_compatible_p(__typeof__(sizeof(0)), unsigned int) ? 1 : -1];
typedef char difference_type[__builtin_types_compatible_p(__typeof__((char*)0 - (char*)0), int) ? 1 : -1];
#ifdef __INTPTR_TYPE__
typedef __INTPTR_TYPE__ capture_intptr_t;
#else
#include <stdint.h>
typedef intptr_t capture_intptr_t;
#endif
typedef char intptr_width[sizeof(capture_intptr_t) == sizeof(void*) ? 1 : -1];
int n32_integer_types(void) { return sizeof(void*) == 4 && sizeof(long) == 4; }
'''})
    for name, args in [
        ("long-separate", ["--language", "c", "--output", "unit.o"]),
        ("long-joined", ["--language=c", "--output=unit.o"]),
    ]:
        suite.run(name, [("c", [*args, "-c", "unit.c"])], ["unit.o"])
    suite.run("language-reset", [("c", ["-x", "c", "unit", "-x", "none", "other.c", "-o", "program"])],
        ["program"], files={"unit": "int other(void); int main(void){return other()!=7;}\n", "other.c": "int other(void){return 7;}\n"})
    for suffix in ["cp", "c++", "CPP"]:
        suite.run("suffix-" + suffix.replace("+", "plus"), [("cxx", ["-O2", "-c", "unit." + suffix, "-o", "unit.o"])],
            ["unit.o"], files={"unit." + suffix: "template<class T> T square(T n){return n*n;} int answer(){return square(3);}\n"})
    suite.run("response", [("c", ["@arguments.rsp"])], ["unit object.o"],
        files={"unit file.c": SOURCE, "arguments.rsp": '-O2 -c "unit file.c" -o "unit object.o"\n'})
    suite.run("dependencies", [("c", ["-MD", "-MF", "unit.d", "-MT", "chosen", "-c", "unit.c", "-o", "unit.o"])],
        ["unit.o", "unit.d"], files={"unit.c": '#include "unit.h"\n' + SOURCE, "unit.h": "#define VALUE 7\n"})
    suite.run("syntax-only", [("c", ["-fsyntax-only", "unit.c"])], [])
    suite.run("dependency-only", [("c", ["-M", "unit.c"])], [])
    suite.run("preprocess", [("c", ["-E", "unit.c"])], [])
    suite.run("rejected-source", [("c", ["-c", "unit.c", "-o", "unit.o"])], [],
        files={"unit.c": "#error deliberate compiler rejection\n"}, expected_status=1)
    suite.run("same-basename", [("c", ["-O2", "a/unit.c", "b/unit.c", "-o", "program"])], ["program"],
        files={"a/unit.c": "int other(void);int main(void){return other()!=7;}\n", "b/unit.c": "int other(void){return 7;}\n"})
    suite.run("stdin", [("c", ["-x", "c", "-c", "-", "-o", "unit.o"])], ["unit.o"], stdin=SOURCE.encode())
    suite.run("preexisting-preprocessed", [("c", ["-c", "unit.i", "-o", "unit.o"])], ["unit.o"],
        files={"unit.i": "int square(int n){return n*n;}\n"})
    # Create target assembly once without capture; never use host assembly for a cross compiler.
    assembly = subprocess.run([drivers(baseline)["c"], "-S", "-x", "c", "-", "-o", "-"],
        input=b"int square(int n){return n*n;}\n", capture_output=True,
        env={k: v for k, v in os.environ.items() if k != "DATASET_CAPTURE_STAGE"}, timeout=30)
    if assembly.returncode:
        raise RuntimeError(assembly.stderr.decode(errors="replace"))
    suite.run("preexisting-assembly", [("c", ["-c", "unit.s", "-o", "unit.o"])], ["unit.o"],
        files={"unit.s": assembly.stdout.decode()})
    if family=="gcc":
        suite.run("sx-assembly", [("c", ["-c", "unit.sx", "-o", "unit.o"])], ["unit.o"],
            files={"unit.sx": assembly.stdout.decode()})
    # Avoid assembler macros in this control source; capture must not add a
    # warning to a compilation that the original compiler accepts cleanly.
    suite.run("fatal-assembler-warnings", [("c", ["-g", "-Wa,--fatal-warnings", "-c", "unit.c", "-o", "unit.o"])], ["unit.o"],
        files={"unit.c":"int square(int n){return n*n;}\n"})
    for name, flags in [("relocatable-wl", ["-Wl,-r"]), ("relocatable-xlinker", ["-Xlinker", "-r"])]:
        # These old drivers do not default to PIE. Their binutils predates
        # -no-pie and can parse it as -n followed by -o-pie instead.
        no_pie=[] if suite.version<((5,) if family=="clang" else (6,)) else ["-Wl,-no-pie"]
        suite.run(name, [("c", ["-c", "unit.c", "-o", "unit.o"]),
            ("c", ["-nostdlib", *no_pie, *flags, "unit.o", "-o", "combined.o"])], ["unit.o", "combined.o"])
    for name, flags in [("map-wl", ["-Wl,--as-needed,-Map,caller map.txt"]),
                        ("map-xlinker", ["-Xlinker", "-Map", "-Xlinker", "caller,map.txt"])]:
        map_name = "caller map.txt" if name == "map-wl" else "caller,map.txt"
        suite.run(name, [("c", ["unit.c", *flags, "-o", "program"])], ["program", map_name], opaque=[map_name])
    # The baseline decides target-specific availability. Capture must work if it succeeds.
    compile_case("directives-only", ["-fdirectives-only"], optional=True)
    suite.run("split-dwarf", [("c", ["-g", "-gsplit-dwarf", "-c", "unit.c", "-o", "unit.o"])],
        ["unit.o"], optional=True)
    pch_args = ["-include", "unit.h"] if family == "gcc" else ["-include-pch", "unit.h.gch"]
    suite.run("pch", [("c", ["-x", "c-header", "unit.h", "-o", "unit.h.gch"]),
        ("c", [*pch_args, "-c", "unit.c", "-o", "unit.o"])], ["unit.h.gch", "unit.o"],
        files={"unit.h": "#define VALUE 7\n", "unit.c": "int answer(void){return VALUE;}\n"}, opaque=["unit.h.gch"])
    pch_args = ["-include", "unit.hpp"] if family=="gcc" else ["-include-pch", "unit.hpp.gch"]
    suite.run("pch-cxx", [("cxx", ["-x", "c++-header", "unit.hpp", "-o", "unit.hpp.gch"]),
        ("cxx", [*pch_args, "-c", "unit.cpp", "-o", "unit.o"])], ["unit.hpp.gch", "unit.o"],
        files={"unit.hpp": "#define VALUE 7\ntemplate<class T> T square(T n){return n*n;}\n",
            "unit.cpp": "int answer(){return square(3)+VALUE;}\n"}, opaque=["unit.hpp.gch"])
    suite.run("cxx-exceptions", [("cxx", ["-std=c++98", "-c", "unit.cpp", "-o", "unit.o"])], ["unit.o"],
        files={"unit.cpp": "struct A{~A();}; int function(int n){A a;if(n)throw n;return 7;}\n"})
    cxx_source='''#include <string>
#include <stdexcept>
int main(int argc,char **argv){
  const bool requested_throw=argc>1;
  try {
    std::string value=argc?argv[0]:"capture";
    if(requested_throw)throw std::runtime_error(value);
    if(value.empty())throw std::runtime_error("empty");
    return value.size()==0;
  }catch(const std::exception &error){return requested_throw && error.what()[0]?0:1;}
}
'''
    cxx_flags=["-std=c++98","-O2","-g","-gdwarf-2" if family=="gcc" else "-gdwarf-4"]
    lto_modes=[("cxx-runtime",[])]
    if suite.version>=((4,6) if family=="gcc" else (3,7)):
        lto_modes.append(("cxx-runtime-lto",["-flto"]))
    if family=="clang" and suite.version>=(3,9):
        lto_modes.append(("cxx-runtime-thinlto",["-flto=thin"]))
    for name,flags in lto_modes:
        suite.run(name,[("cxx",[*cxx_flags,*flags,"unit.cpp","-o","program"])],["program"],files={"unit.cpp":cxx_source})
    suite.run("cxx-abi-response",[("cxx",["@producer.rsp"]),
        ("cxx",[*cxx_flags,"consumer.cpp","producer.o","-o","program"])],["producer.o","program"],
        files={"producer.cpp":'#include <string>\nstd::string make_value(){return "capture";}\n',
            "consumer.cpp":'#include <string>\nstd::string make_value();int main(){return make_value().size()!=7;}\n',
            "producer.rsp":" ".join([*cxx_flags,"-c","producer.cpp","-o","producer.o"])+"\n"})
    Path(output).write_text(json.dumps(dict(schema=1, compiler_family=family, version=version,
        target=target, cases=suite.cases), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

"""Real compiler tests for the experimental ptrace backend, inside bounded scratch."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
import sys

BINARY = os.environ.get("CAPTURE_BINARY")
_capabilities_path = os.environ.get("CAPTURE_CAPABILITIES",str(Path(__file__).with_name("capabilities.py")))
_spec = importlib.util.spec_from_file_location("native_capabilities",_capabilities_path)
_capabilities = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_capabilities)
_validator_spec=importlib.util.spec_from_file_location("native_capture_validator",os.environ.get("CAPTURE_VALIDATOR",Path(__file__).parents[2]/"python/compiler_suit_runner/capture.py"))
_validator=importlib.util.module_from_spec(_validator_spec)
sys.modules[_validator_spec.name]=_validator
_validator_spec.loader.exec_module(_validator)


def native_version(family):
    return os.environ.get("CAPTURE_"+family.upper()+"_VERSION","15.2.0" if family=="gcc" else "21.1.8")


class NativeTests(unittest.TestCase):
    def test_i686_pic_relocation_relaxation(self):
        compilers=json.loads(os.environ.get("CAPTURE_RELAX_CLANGS","{}"))
        if not compilers:self.skipTest("cached Clang 12-15 drivers are required")
        for version,compiler in compilers.items():
            for optimization in ["-O0","-O2"]:
                with self.subTest(version=version,optimization=optimization):
                    self.run_case("clang",["-m32","-fPIC",optimization,"-c","unit.c","-o","unit.o"],
                        {"unit.c":"int values[]={1,3,7}; int square(int n){return n*n+values[0]-1;}\n"},"unit.o",
                        compiler_override=compiler,compiler_version=version,target_override="i686-unknown-linux-gnu")

    def test_legacy_thin_lto_inputs_and_archive_members(self):
        compiler=os.environ.get("CAPTURE_LEGACY_THIN_CLANG")
        archiver=os.environ.get("CAPTURE_LEGACY_THIN_AR")
        if not BINARY or not compiler or not archiver:self.skipTest("cached Clang/LLVM 3.9 tools are required")
        self.run_case("clang",["-flto=thin","-O2","main.c","helper.c","-o","program"],
            {"main.c":"int helper(void);int main(void){return helper()!=7;}\n",
             "helper.c":"volatile int seed=7;int helper(void){return seed;}\n"},"program",
            compiler_override=compiler,compiler_version="3.9.1",
            expected_roles={"preprocessing","ir","lto-native-object","linked-output","command"})
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);stage=root/"stage";output=root/"capture"
            (root/"main.c").write_text("int helper(void);int main(void){return helper()!=7;}\n")
            (root/"helper.c").write_text("volatile int seed=7;__attribute__((noinline)) int helper(void){return seed;}\n")
            (root/"main.o.thinlto.o").write_bytes(b"caller-owned sentinel")
            config=root/"config.json"
            config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="clang",
                version="3.9.1",target="x86_64-unknown-linux-gnu",driver="clang",backend="clang-trace",supervisor=BINARY,debug_policy="required")))
            reference=canonical=None
            for capture in [False,True,True]:
                env={key:value for key,value in os.environ.items() if key!="DATASET_CAPTURE_STAGE"}
                if capture:
                    if output.exists():shutil.rmtree(output)
                    subprocess.run([BINARY,"init",stage],check=True)
                    env.update(DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view"))
                prefix=[BINARY,"--config",str(config),"--"] if capture else [compiler]
                for name in ["main","helper"]:
                    call=subprocess.run([*prefix,"-flto=thin","-O2","-c",name+".c","-o",name+".o"],cwd=root,env=env,capture_output=True,timeout=30)
                    self.assertEqual(call.returncode,0,call.stderr)
                subprocess.run([archiver,"crs","libhelper.a","helper.o"],cwd=root,env=env,check=True,capture_output=True)
                call=subprocess.run([*prefix,"-flto=thin","main.o","libhelper.a","-o","program"],cwd=root,env=env,capture_output=True,timeout=30)
                self.assertEqual(call.returncode,0,call.stderr)
                self.assertEqual(subprocess.run([root/"program"],timeout=5).returncode,0)
                behavior=_capabilities.section_hashes(root/"program",behavior=True)
                if not capture:
                    reference=behavior
                    baseline_files={p.name for p in root.iterdir()}
                else:
                    self.assertEqual(behavior,reference)
                    final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                    self.assertEqual(final.returncode,0,final.stderr)
                    verified=_validator.validate_capture(output)
                    raw=(output/"manifest.json").read_bytes()
                    if canonical is None:canonical=raw
                    else:self.assertEqual(raw,canonical)
                    artifacts=[a for entry in verified.manifest["invocations"] for a in entry["record"]["artifacts"]]
                    self.assertTrue(any(a["role"]=="lto-native-object" and b".llvm." in bytes.fromhex(a["original_path_hex"]) for a in artifacts))
            self.assertEqual((root/"main.o.thinlto.o").read_bytes(),b"caller-owned sentinel")
            self.assertEqual({p.name for p in root.iterdir()},baseline_files|{"capture"})

    def test_legacy_external_assembler_unwind_policy(self):
        targets=json.loads(os.environ.get("CAPTURE_LEGACY_EXTERNAL_TARGETS","[]"))
        if not targets:self.skipTest("cached legacy Clang and cross assemblers are required")
        for entry in targets:
            for flags in [[],["-g"],["-g","-gsplit-dwarf"],["-funwind-tables"],["-g","-funwind-tables"],
                    ["-gline-tables-only"],["-g0","-gline-tables-only"],["-gline-tables-only","-g0"]]:
                with self.subTest(target=entry["target"],version=entry["version"],flags=flags):
                    args=["-target",entry["target"],"-B"+entry["bintools"]+"/bin/",*entry.get("flags",[]),
                        "-O2",*flags,"-c","unit.c","-o","unit.o"]
                    self.run_case("clang",args,{"unit.c":"int square(int n){return n*n;}\n"},"unit.o",
                        compiler_override=entry["compiler"],compiler_version=entry["version"],target_override=entry["target"],
                        debug_companions=["unit.dwo"] if "-gsplit-dwarf" in flags else ())

    def test_clang_x86_alignment_tuning(self):
        source="int values[]={1,3,7}; int square(int n){return n*n+values[0]-1;} int main(void){return square(3)!=9;}\n"
        for flag in ["-O1", "-O2", "-O3", "-Os"]:
            with self.subTest(flag=flag):
                self.run_case("clang", [flag, "-c", "unit.c", "-o", "unit.o"], {"unit.c": source}, "unit.o")

    def test_clang34_accumulator_comparisons(self):
        compiler=json.loads(os.environ.get("CAPTURE_PCH_CLANGS", "{}")).get("3.4.2")
        if not compiler:self.skipTest("cached Clang 3.4 is required")
        for immediate in [-128, -1, 0, 1, 7, 9, 127, 128]:
            with self.subTest(immediate=immediate):
                self.run_case("clang", ["-O0", "-c", "unit.c", "-o", "unit.o"],
                    {"unit.c": f'int answer(int n){{__asm__ volatile("" : "+a"(n));return n=={immediate};}}\n'},
                    "unit.o", compiler_override=compiler, compiler_version="3.4.2")
        self.run_case("clang", ["-O0", "-c", "unit.c", "-o", "unit.o"],
            {"unit.c": 'int answer(int n){__asm__ volatile("cmpl $7, %%eax" : "+a"(n) : : "cc");return n;}\n'},
            "unit.o", compiler_override=compiler, compiler_version="3.4.2")

    def test_legacy_clang_native_objects_from_ir(self):
        compilers=json.loads(os.environ.get("CAPTURE_PCH_CLANGS", "{}"))
        selected={v: cc for v, cc in compilers.items() if v.startswith(("3.5.","3.6."))}
        if not selected:self.skipTest("cached Clang 3.5/3.6 drivers are required")
        source="int values[]={1,3,7}; int square(int n){return n*n+values[0]-1;} int main(void){return square(3)!=9;}\n"
        for version, compiler in selected.items():
            for optimization in ["-O0", "-O2", "-Os"]:
                with self.subTest(version=version, optimization=optimization):
                    self.run_case("clang", [optimization,"-c","unit.c","-o","unit.o"], {"unit.c":source}, "unit.o",
                        compiler_override=compiler, compiler_version=version, expected_roles={"ir","preprocessing","object","command"})

    def test_aarch64_native_ir_codegen(self):
        compilers=json.loads(os.environ.get("CAPTURE_AARCH64_CLANGS", "{}"))
        if not compilers:self.skipTest("cached AArch64-capable Clang drivers are required")
        cases=[("unit.c", "int values[]={1,3,7}; int square(int n){return n*n+values[0]-1;} int main(void){return square(3)!=9;}\n", ["-O1"]),
            ("unit.cpp", "struct A{~A();};int function(int n){A a;if(n)throw n;return 7;}\n", ["-std=c++98"])]
        for version, compiler in compilers.items():
            for name, source, flags in cases:
                with self.subTest(version=version, name=name):
                    self.run_case("clang", ["-target","aarch64-unknown-linux-gnu",*flags,"-c",name,"-o","unit.o"], {name:source}, "unit.o",
                        compiler_override=compiler, compiler_version=version, target_override="aarch64-unknown-linux-gnu",
                        expected_roles={"ir","preprocessing","object","command"})

    def test_preprocessed_inputs_are_retained(self):
        for family in ["gcc", "clang"]:
            with self.subTest(family=family):
                self.run_case(family, ["-c","unit.i","-o","unit.o"],
                    {"unit.i":"int square(int n){return n*n;}\n"}, "unit.o",
                    expected_roles={"preprocessing","assembly","object","command"})

    def test_riscv_saved_assembly_alignment(self):
        compilers=json.loads(os.environ.get("CAPTURE_RISCV_CLANGS","{}"))
        if not BINARY or not compilers:self.skipTest("cached Clang 14-16 drivers are required")
        for version,compiler in compilers.items():
            for optimization in ["-O0","-O2","-Os"]:
                with self.subTest(version=version,optimization=optimization),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary);stage=root/"stage";output=root/"output"
                    (root/"unit.c").write_text("int square(int n){return n*n;} int main(void){return square(3)!=9;}\n")
                    arguments=["-target","riscv64-unknown-linux-gnu",optimization,"-c","unit.c","-o","unit.o"]
                    result=subprocess.run([compiler,*arguments],cwd=root,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                    expected=_capabilities.section_hashes(root/"unit.o",behavior=True)
                    subprocess.run([BINARY,"init",stage],check=True)
                    config=root/"config.json"
                    config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="clang",version=version,
                        target="riscv64-unknown-linux-gnu",driver="clang",backend="clang-trace",supervisor=BINARY,debug_policy="required")))
                    result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,capture_output=True,timeout=20,
                        env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(_capabilities.section_hashes(root/"unit.o",behavior=True),expected)
                    final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                    self.assertEqual(final.returncode,0,final.stderr)
                    self.assertTrue(_validator.validate_capture(output).complete)

    def test_legacy_aarch64_cfi(self):
        compiler=os.environ.get("CAPTURE_LEGACY_CLANG")
        bintools=os.environ.get("CAPTURE_AARCH64_BINTOOLS")
        if not BINARY or not compiler or not bintools:self.skipTest("cached Clang 3.8 and AArch64 binutils are required")
        for language in ["c","c++"]:
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"capture"
                (root/"unit.c").write_text("int square(int n){return n*n;} int main(void){return square(3)!=9;}\n")
                arguments=["-target","aarch64-unknown-linux-gnu","-B"+bintools+"/bin/","-x",language,"-O2","-c","unit.c","-o","unit.o"]
                baseline=subprocess.run([compiler,*arguments],cwd=root,capture_output=True,timeout=15)
                self.assertEqual(baseline.returncode,0,baseline.stderr)
                expected=_capabilities.section_hashes(root/"unit.o",behavior=True)
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="clang",version="3.8.1",
                    target="aarch64-unknown-linux-gnu",driver="clang",backend="clang-trace",supervisor=BINARY,debug_policy="required")))
                result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,capture_output=True,timeout=20,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage)))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(_capabilities.section_hashes(root/"unit.o",behavior=True),expected)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,(final.stderr,json.loads((output/"manifest.json").read_text())))
                verified=_validator.validate_capture(output)
                if language=="c":
                    self.assertTrue(any(entry["record"].get("transformations") or entry["record"].get("native_from_ir") for entry in verified.manifest["invocations"]))

    def test_direct_assembler_and_linker(self):
        assembler=shutil.which("as");linker=shutil.which("ld")
        if not BINARY or not assembler or not linker:self.skipTest("native binutils are required")
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/"unit.s").write_text(".text\n.globl _start\n_start:\n mov $60, %eax\n xor %edi, %edi\n syscall\n")
            subprocess.run([assembler,"unit.s","-o","unit.o"],cwd=root,check=True,capture_output=True)
            subprocess.run([linker,"unit.o","-o","program"],cwd=root,check=True,capture_output=True)
            baseline=_capabilities.section_hashes(root/"program",behavior=True)
            stage=root/"stage";output=root/"capture"
            subprocess.run([BINARY,"init",stage],check=True)
            environment=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view"))
            for family,program,arguments in [("assembler",assembler,["-v","unit.s","-o","unit.o"]),("linker",linker,["-v","unit.o","-o","program"])]:
                config=root/(family+".json")
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=program,
                    family=family,version="2.46",target="x86_64-unknown-linux-gnu",driver=family,
                    backend="tool-observe",supervisor=BINARY,debug_policy="required")))
                result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,env=environment,capture_output=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(subprocess.run([root/"program"],timeout=5).returncode,0)
            self.assertEqual(_capabilities.section_hashes(root/"program",behavior=True),baseline)
            final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
            manifest=json.loads((output/"manifest.json").read_text())
            self.assertEqual(final.returncode,0,(final.stderr,manifest))
            verified=_validator.validate_capture(output)
            roles={artifact["role"] for invocation in manifest["invocations"] for artifact in invocation["record"]["artifacts"]}
            self.assertTrue({"assembly","object","command","linked-output","link-map"}<=roles)
            installed=root/"installed";installed.mkdir();shutil.copyfile(root/"program",installed/"program")
            subprocess.run(["strip",installed/"program"],check=True)
            self.assertTrue(_validator.elf_associations(installed,verified,required=True,required_debug=True)["program"]["candidates"])

    def test_direct_llvm_linker_plugin(self):
        compiler=os.environ.get("CAPTURE_CLANG");plugin=os.environ.get("CAPTURE_LLVM_PLUGIN")
        linker=shutil.which("ld")
        if not BINARY or not compiler or not plugin or not linker:self.skipTest("cached LLVMgold and native tools are required")
        for flag in ["-flto","-flto=thin"]:
            with self.subTest(flag=flag),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                (root/"unit.c").write_text('void _start(void){__asm__ volatile("mov $60, %eax; xor %edi, %edi; syscall");}\n')
                subprocess.run([compiler,flag,"-g","-ffreestanding","-fno-stack-protector","-c","unit.c","-o","unit.o"],cwd=root,check=True,capture_output=True)
                arguments=["-plugin",plugin,"unit.o","-o","program"]
                baseline_run=subprocess.run([linker,*arguments],cwd=root,capture_output=True)
                self.assertEqual(baseline_run.returncode,0,baseline_run.stderr)
                baseline=_capabilities.section_hashes(root/"program",behavior=True)
                (root/"program").unlink()
                stage=root/"stage";output=root/"output"
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=linker,family="linker",version="2.46",
                    target="x86_64-unknown-linux-gnu",driver="ld",backend="tool-observe",supervisor=BINARY,debug_policy="required")))
                result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,capture_output=True,timeout=20,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(subprocess.run([root/"program"],timeout=5).returncode,0)
                self.assertEqual(_capabilities.section_hashes(root/"program",behavior=True),baseline)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                verified=_validator.validate_capture(output)
                record=verified.manifest["invocations"][0]["record"]
                self.assertTrue({"ir","lto-native-object","command","linked-output","link-map"}<={item["role"] for item in record["artifacts"]})
                self.assertEqual({path.name for path in root.iterdir()},{"unit.c","unit.o","program","config.json","output"})

    def run_case(self,family,arguments,source_files,primary,stdin=b"",allowed_auxiliary=(),expected_roles=None,bitcode=False,pch=False,allowed_directories=(),reference_erasure=False,compiler_override=None,compiler_version=None,preserve_extra_bytes=True,debug_companions=(),private_lto_companions=(),target_override=None):
        compiler = compiler_override or os.environ.get("CAPTURE_"+family.upper())
        if not BINARY or not compiler:
            self.skipTest("CAPTURE_BINARY and CAPTURE_GCC/CAPTURE_CLANG are required")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name,content in source_files.items():
                path = root/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(content)
            baseline = subprocess.run([compiler,*arguments],cwd=root,input=stdin,capture_output=True,timeout=15)
            self.assertEqual(baseline.returncode,0,baseline.stderr)
            expected = (root/primary).read_bytes()
            expected_sections = _capabilities.section_hashes(root/primary)
            expected_text=_capabilities.section_hashes(root/primary,contents=True).get(".text","")
            expected_behavior = _capabilities.section_hashes(root/primary,behavior=True)
            baseline_extra={p.relative_to(root).as_posix():_validator._object_hash(p,p.stat().st_size)
                for p in root.rglob("*") if p.is_file() and p.relative_to(root).as_posix() not in set(source_files)|{primary}
                and p.parts[len(root.parts)] not in allowed_directories}
            if debug_companions is True:
                debug_companions=tuple(name for name in baseline_extra if name.endswith(".dwo") and name not in private_lto_companions)
                self.assertTrue(debug_companions)
            for name in private_lto_companions:
                self.assertIn(name,baseline_extra)
                (root/name).unlink()
                del baseline_extra[name]
            (root/primary).unlink()
            stage = root/"capture-stage"
            output = root/"capture-output"
            subprocess.run([BINARY,"init",stage],check=True)
            config = root/"config.json"
            config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,
                family=family,version=compiler_version or native_version(family),target=target_override or "x86_64-unknown-linux-gnu",
                driver="c",backend=family+"-trace",supervisor=BINARY,debug_policy="required")))
            result = subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,
                input=stdin,capture_output=True,timeout=20,
                env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
            self.assertEqual(result.returncode,baseline.returncode,result.stderr)
            self.assertTrue((root/primary).exists(),result.stderr)
            self.assertFalse((root/".compiler-view").is_symlink())
            finished = subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True,timeout=15)
            manifest = json.loads((output/"manifest.json").read_text())
            self.assertEqual(finished.returncode,0,(finished.stderr,manifest.get("errors"),manifest.get("invocations")))
            self.assertTrue(manifest["complete"])
            verified=_validator.validate_capture(output)
            for name,content in source_files.items():
                self.assertEqual((root/name).read_text(),content)
            magic=expected[:4] if pch else b"BC\xc0\xde" if bitcode else b"\x7fELF"
            self.assertEqual(expected[:4],magic)
            self.assertEqual((root/primary).read_bytes()[:4],magic)
            actual_sections = _capabilities.section_hashes(root/primary)
            ppc_sections=_capabilities.ppc_debug_sections(expected,(root/primary).read_bytes(),family=family,version=compiler_version or native_version(family))
            aarch64_sections=_capabilities.aarch64_debug_sections(expected,(root/primary).read_bytes(),family=family,version=compiler_version or native_version(family))
            x86_sections=_capabilities.x86_debug_sections(expected,(root/primary).read_bytes(),family=family,version=compiler_version or native_version(family))
            prologue_swap=_capabilities.aarch64_debug_prologue_swap(bytes.fromhex(expected_text),
                bytes.fromhex(_capabilities.section_hashes(root/primary,contents=True).get(".text","")),
                expected[:64],(root/primary).read_bytes()[:64],family=family,version=compiler_version or native_version(family))
            for section in [".text",".data",".rodata"]:
                if section in ppc_sections or section in aarch64_sections or section in x86_sections or section==".text" and prologue_swap:continue
                self.assertEqual(actual_sections.get(section),expected_sections.get(section),section)
            if not any(argument.startswith("-flto") for argument in arguments):
                actual_behavior=_capabilities.section_hashes(root/primary,behavior=True)
                debug_reference=None
                if prologue_swap or ppc_sections or aarch64_sections or x86_sections or _capabilities.debug_unwind_difference(expected_behavior,actual_behavior):
                    retained=(root/primary).read_bytes()
                    debug_flags=["-g", "-gdwarf-2" if family=="gcc" else "-gdwarf-4"]
                    reference=subprocess.run([compiler,*arguments,*debug_flags],cwd=root,input=stdin,capture_output=True,timeout=15,
                        env={key:value for key,value in os.environ.items() if key!="DATASET_CAPTURE_STAGE"})
                    self.assertEqual(reference.returncode,0,reference.stderr)
                    debug_reference=_capabilities.section_hashes(root/primary,behavior=True)
                    (root/primary).write_bytes(retained)
                self.assertIsNotNone(_capabilities.compatible_behavior(expected_behavior,actual_behavior,debug_reference,prologue_swap=prologue_swap,ppc_sections=ppc_sections,aarch64_sections=aarch64_sections,x86_sections=x86_sections),
                    (expected_behavior,actual_behavior,debug_reference))
            roles = {a["role"] for item in manifest["invocations"] for a in item["record"]["artifacts"]}
            ir_backend=any(item["record"].get("native_from_ir") for item in manifest["invocations"])
            required=set(expected_roles or {"preprocessing","assembly","command"})
            if ir_backend and "assembly" in required:
                required.remove("assembly");required.add("ir")
            self.assertTrue(required<=roles,roles)
            if primary=="program":
                self.assertEqual(subprocess.run([root/primary],capture_output=True,timeout=5).returncode,0)
                self.assertIn("link-map",roles)
                installed=root/"installed"
                installed.mkdir()
                shutil.copyfile(root/primary,installed/primary)
                subprocess.run(["strip",installed/primary],check=True,capture_output=True)
                if reference_erasure:
                    original=(installed/primary).read_bytes()
                    needle=b"/nix/store/"+b"0"*32+b"-fixture"
                    self.assertIn(needle,original)
                    (installed/primary).write_bytes(original.replace(needle,b"/nix/store/"+b"1"*32+b"-fixture"))
                    with self.assertRaises(_validator.CaptureValidationError):
                        _validator.elf_associations(installed,verified,required=True,required_debug=True)
                    (installed/primary).write_bytes(original.replace(needle,b"/nix/store/"+b"e"*32+b"-fixture"))
                associations=_validator.elf_associations(installed,verified,required=True,required_debug=True)
                self.assertTrue(associations[primary]["candidates"])
                if reference_erasure:
                    self.assertTrue(all(candidate["evidence"]=="validated-store-reference-removal" and candidate["reference_rewrites"] for candidate in associations[primary]["candidates"]))
                self.assertNotEqual(associations[primary]["installed_sha256"],
                    associations[primary]["candidates"][0]["artifact_sha256"])
            self.assertFalse(any((root/name).exists() for name in ["unit.i","unit.s","same.i","same.s","same.o"] if name not in source_files and name not in allowed_auxiliary and name not in baseline_extra))
            unexpected={p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
                and p.parts[len(root.parts)] not in ["capture-stage","capture-output","installed",*allowed_directories]}
            expected_files={name for name in source_files if Path(name).parts[0] not in allowed_directories}
            self.assertEqual(unexpected,expected_files|{primary,"config.json"}|set(allowed_auxiliary)|set(baseline_extra))
            for name,digest in baseline_extra.items():
                self.assertTrue((root/name).is_file(),name)
                if preserve_extra_bytes and name not in allowed_auxiliary and name not in debug_companions:self.assertEqual(_validator._object_hash(root/name,(root/name).stat().st_size),digest)
            for name in debug_companions:
                path=root/name
                digest=_validator._object_hash(path,path.stat().st_size)
                self.assertTrue(any(artifact["role"]=="debug-companion" and artifact["sha256"]==digest
                    for entry in manifest["invocations"] for artifact in entry["record"]["artifacts"]))
                retained=output/"objects"/digest
                if "-flto" not in arguments:
                    self.assertIn(".debug_info.dwo",_capabilities.section_hashes(retained,contents=True))
                    info=subprocess.run(["readelf","--debug-dump=info",retained],capture_output=True,timeout=5)
                    self.assertEqual(info.returncode,0,info.stderr)
                    self.assertIn(b"DW_TAG_compile_unit",info.stdout)
                path.unlink()
                self.assertEqual(_validator._object_hash(retained,retained.stat().st_size),digest)
                self.assertTrue(_validator.validate_capture(output).complete)
            return manifest

    def test_long_driver_options(self):
        for family in ["gcc","clang"]:
            for output in [["--output","different.o"],["--output=different.o"]]:
                with self.subTest(family=family,output=output):
                    self.run_case(family,["-c","unit.c",*output],{"unit.c":"int square(int n){return n*n;}\n"},"different.o")
            for language in [["--language","c++"],["--language=c++"]]:
                with self.subTest(family=family,language=language):
                    self.run_case(family,[*language,"-c","unit","-o","different.o"],
                        {"unit":"template<class T> T square(T n){return n*n;} int answer(){return square(3);}\n"},"different.o")

    def test_additional_cxx_suffixes(self):
        for family in ["gcc","clang"]:
            for suffix in ["cp","c++","CPP"]:
                with self.subTest(family=family,suffix=suffix):
                    name="unit."+suffix
                    self.run_case(family,["-c",name,"-o","unit.o"],
                        {name:"template<class T> T square(T n){return n*n;} int answer(){return square(3);}\n"},"unit.o")

    def test_legacy_driver_corners(self):
        compilers=json.loads(os.environ.get("CAPTURE_LEGACY_CORNERS","[]"))
        if not compilers:self.skipTest("cached legacy compilers are required")
        for compiler in compilers:
            for suffix in ["cp","c++","CPP"]:
                with self.subTest(compiler=compiler["version"],suffix=suffix):
                    name="unit."+suffix
                    self.run_case(compiler["family"],["-c",name,"--output","different.o"],
                        {name:"template<class T> T square(T n){return n*n;} int answer(){return square(3);}\n"},"different.o",
                        compiler_override=compiler["executable"],compiler_version=compiler["version"])
            with self.subTest(compiler=compiler["version"],language="c++"):
                self.run_case(compiler["family"],["--language=c++","-c","unit","--output=different.o"],
                    {"unit":"template<class T> T square(T n){return n*n;} int answer(){return square(3);}\n"},"different.o",
                    compiler_override=compiler["executable"],compiler_version=compiler["version"])

    def test_gcc_sx_assembly(self):
        self.run_case("gcc",["-c","unit.sx","-o","unit.o"],
            {"unit.sx":"#define RETURN_VALUE 7\n.text\n.globl answer\nanswer:\n mov $RETURN_VALUE,%eax\n ret\n"},"unit.o",
            expected_roles={"preprocessing","assembly","command","object"})

    def test_linker_map_forwarding(self):
        for family in ["gcc","clang"]:
            for flags,path in [(["-Xlinker","-Map","-Xlinker","caller map,one.txt"],"caller map,one.txt"),
                    (["-Wl,--as-needed,-Map,caller.map"],"caller.map"),
                    (["-Wl,--Map=caller.map"],"caller.map"),
                    (["-Wl,-Map,old.map","-Xlinker","--Map=caller.map"],"caller.map")]:
                with self.subTest(family=family,flags=flags):
                    manifest=self.run_case(family,["unit.c",*flags,"-o","program"],
                        {"unit.c":"int main(void){return 0;}\n"},"program",allowed_auxiliary=[path])
                    self.assertTrue(any(artifact["role"]=="link-map" and artifact["path_base"]=="cwd"
                        and artifact["original_path_hex"]==path.encode().hex()
                        for entry in manifest["invocations"] for artifact in entry["record"]["artifacts"]))

    def test_forwarded_relocatable_link(self):
        for family in ["gcc","clang"]:
            for flags in [["-Wl,-r"],["-Wl,--relocatable"],["-Wl,--as-needed,-r"],["-Xlinker","-r"]]:
                with self.subTest(family=family,flags=flags):
                    manifest=self.run_case(family,["-nostdlib","-no-pie",*flags,"unit.c","-o","combined.o"],
                        {"unit.c":"int square(int n){return n*n;}\n"},"combined.o",
                        expected_roles={"preprocessing","assembly","object","command","link-map"})
                    self.assertTrue(manifest["invocations"][0]["record"]["analysis"]["relocatable"])
                    self.assertTrue(any(artifact["role"]=="object" and artifact["original_path_hex"]==b"combined.o".hex()
                        for entry in manifest["invocations"] for artifact in entry["record"]["artifacts"]))

    def test_linker_options_do_not_change_assembly_output_role(self):
        for family in ["gcc","clang"]:
            compiler=os.environ.get("CAPTURE_"+family.upper())
            if not BINARY or not compiler:self.skipTest("native compiler is required")
            with self.subTest(family=family),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);(root/"unit.c").write_text("int square(int n){return n*n;}\n")
                args=["-S","-Wl,-r","unit.c","-o","wanted.s"]
                baseline=subprocess.run([compiler,*args],cwd=root,capture_output=True)
                self.assertEqual(baseline.returncode,0,baseline.stderr)
                (root/"wanted.s").rename(root/"baseline.s")
                stage=root/"stage";output=root/"capture";subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json";config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,
                    family=family,version=native_version(family),target="x86_64-unknown-linux-gnu",driver="cc",backend=family+"-trace",supervisor=BINARY,debug_policy="required")))
                captured=subprocess.run([BINARY,"--config",config,"--",*args],cwd=root,capture_output=True,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                self.assertEqual(captured.returncode,0,captured.stderr)
                finalized=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(finalized.returncode,0,finalized.stderr)
                manifest=_validator.validate_capture(output).manifest
                digest=_validator._object_hash(root/"wanted.s",(root/"wanted.s").stat().st_size)
                self.assertTrue(any(a["role"]=="assembly" and a["sha256"]==digest
                    for entry in manifest["invocations"] for a in entry["record"]["artifacts"]))
                sections=[]
                for name in ["baseline","wanted"]:
                    subprocess.run([compiler,"-x","assembler","-c",root/(name+".s"),"-o",root/(name+".o")],check=True,capture_output=True)
                    sections.append(_capabilities.section_hashes(root/(name+".o"),behavior=True))
                self.assertEqual(*sections)

    def test_directives_only_preprocessing(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                # GCC rejects __COUNTER__ inside a directives-only #if.
                condition="#if __COUNTER__ != 0\n#error counter\n#endif\n" if family=="clang" else ""
                self.run_case(family,["-fdirectives-only","-c","unit.c","-o","unit.o"],
                    {"unit.c":"#include \"unit.h\"\n"+condition+"const char *file=__FILE__; const char *base=__BASE_FILE__;\nint line=__LINE__; int first=__COUNTER__; int second=__COUNTER__;\nint answer(){return VALUE;}\n","unit.h":"#define VALUE 7\n"},"unit.o")

    def test_directives_only_unusual_source_names(self):
        for name in ["unit=one.c", "src dir/quote\"one.c", "src dir/unit.c"]:
            with self.subTest(name=name):
                self.run_case("gcc", ["-Werror", "-fdirectives-only", "-c", name, "-o", "unit.o"],
                    {name: "const char *file=__FILE__; const char *base=__BASE_FILE__; int first=__COUNTER__; int second=__COUNTER__;\n"}, "unit.o")

    def test_legacy_directives_only(self):
        compilers=json.loads(os.environ.get("CAPTURE_LEGACY_GCCS", "{}"))
        if not compilers:self.skipTest("cached legacy GCC drivers are required")
        for version, compiler in compilers.items():
            with self.subTest(version=version):
                self.run_case("gcc", ["-Werror", "-fdirectives-only", "-c", "unit=one.c", "-o", "unit.o"],
                    {"unit=one.c": "const char *file=__FILE__; const char *base=__BASE_FILE__; int first=__COUNTER__; int second=__COUNTER__;\n"},
                    "unit.o", compiler_override=compiler, compiler_version=version)

    def test_directives_only_prefix_maps(self):
        for family in ["gcc","clang"]:
            for path,flags in [("src dir/unit.c",["-fmacro-prefix-map=src dir=public"]),
                    ("src/unit.c",["-fmacro-prefix-map=src=discard","-ffile-prefix-map=src=public"]),
                    ("src=dir/unit.c",["-fmacro-prefix-map=src=dir=public"])]:
                with self.subTest(family=family,path=path,flags=flags):
                    self.run_case(family,["-fdirectives-only",*flags,"-c",path,"-o","unit.o"],
                        {path:"const char *file=__FILE__; const char *base=__BASE_FILE__; int answer(){return 7;}\n"},"unit.o")

    def test_directives_only_rejections_preserve_compiler_status(self):
        compiler=os.environ.get("CAPTURE_GCC")
        if not BINARY or not compiler:self.skipTest("native GCC is required")
        cases=[("unit.c","#if __COUNTER__ != 0\n#error counter\n#endif\nint answer(){return 7;}\n",1)]
        for name,source,expected in cases:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);(root/name).write_text(source)
                args=["-fdirectives-only","-c",name,"-o","unit.o"]
                baseline=subprocess.run([compiler,*args],cwd=root,capture_output=True)
                self.assertEqual(baseline.returncode,expected,baseline.stderr)
                stage=root/"stage";output=root/"capture";subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="gcc",
                    version=native_version("gcc"),target="x86_64-unknown-linux-gnu",driver="gcc",backend="gcc-trace",supervisor=BINARY,debug_policy="required")))
                captured=subprocess.run([BINARY,"--config",config,"--",*args],cwd=root,capture_output=True,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                self.assertEqual(captured.returncode,expected,captured.stderr)
                finalized=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                manifest=json.loads((output/"manifest.json").read_bytes())
                self.assertEqual(finalized.returncode,0,finalized.stderr)
                self.assertEqual(manifest["invocations"][0]["record"]["compiler_status"]["exit_code"],expected)

    def test_split_dwarf_companions_survive_cleanup(self):
        for family in ["gcc","clang"]:
            for primary in ["unit.o","build/different.o"]:
                with self.subTest(family=family,primary=primary):
                    files={"unit.c":"int square(int n){return n*n;}\n"}
                    if primary.startswith("build/"):files["build/marker"]="keep\n"
                    companion=str(Path(primary).with_suffix(".dwo"))
                    self.run_case(family,["-g","-gsplit-dwarf","-c","unit.c","-o",primary],files,primary,
                        allowed_auxiliary=[companion],expected_roles={"preprocessing","assembly","object","debug-companion"},
                        debug_companions=[companion])

    def test_legacy_clang_split_dwarf(self):
        compiler=json.loads(os.environ.get("CAPTURE_PCH_CLANGS", "{}")).get("3.4.2")
        if not BINARY or not compiler:self.skipTest("cached Clang 3.4 is required")
        self.run_case("clang",["-g","-gsplit-dwarf","-c","unit.c","-o","unit.o"],
            {"unit.c":"int square(int n){return n*n;}\n"},"unit.o",debug_companions=["unit.dwo"],
            compiler_override=compiler,compiler_version="3.4.2",expected_roles={"preprocessing","ir","object","debug-companion"})

    def test_split_dwarf_can_be_disabled(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                manifest=self.run_case(family,["-gsplit-dwarf","-gno-split-dwarf","-c","unit.c","-o","unit.o"],
                    {"unit.c":"int square(int n){return n*n;}\n","unit.dwo":"caller-owned sentinel\n"},"unit.o")
                self.assertFalse(any(artifact["role"]=="debug-companion"
                    for entry in manifest["invocations"] for artifact in entry["record"]["artifacts"]))

    def test_split_dwarf_compile_and_link(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-g","-gsplit-dwarf","unit.c","-o","program"],
                    {"unit.c":"int main(void){return 0;}\n"},"program",debug_companions=True,
                    expected_roles={"preprocessing","assembly","object","linked-output","link-map","debug-companion"})

    def test_gcc_split_dwarf_lto(self):
        manifest=self.run_case("gcc",["-g","-gsplit-dwarf","-flto","unit.c","-o","program"],
            {"unit.c":"int main(void){return 0;}\n"},"program",debug_companions=True,
            private_lto_companions=["program.ltrans0.ltrans.dwo"],
            expected_roles={"assembly","object","linked-output","debug-companion"})
        self.assertTrue(any(artifact["role"]=="debug-companion" and artifact["path_base"]=="invocation-stage"
            for entry in manifest["invocations"] for artifact in entry["record"]["artifacts"]))

    def test_split_dwarf_caller_auxiliary_directory(self):
        self.run_case("gcc",["-g","-gsplit-dwarf","-dumpdir","aux/","unit.c","-o","program"],
            {"unit.c":"int main(void){return 0;}\n","aux/marker":"caller-owned sentinel\n"},"program",
            allowed_directories=["aux"],debug_companions=["aux/unit.dwo"])

    def test_clang_split_dwarf_single_file(self):
        manifest=self.run_case("clang",["-g","-gsplit-dwarf=single","-c","unit.c","-o","unit.o"],
            {"unit.c":"int square(int n){return n*n;}\n"},"unit.o")
        self.assertFalse(manifest["invocations"][0]["record"]["analysis"]["split_dwarf"])

    def test_gcc_preprocessed_flag(self):
        self.run_case("gcc",["-fpreprocessed","-c","unit.c","-o","unit.o"],
            {"unit.c":"int square(int n){return n*n;}\n"},"unit.o")

    def test_pipe_and_dependency_files(self):
        for family in ["gcc","clang"]:
            for flags in [["-pipe","-MD"],["-MMD","-MF","caller deps.d","-MT","caller-target"]]:
                with self.subTest(family=family,flags=flags):
                    self.run_case(family,[*flags,"-c","unit.c","-o","unit.o"],
                        {"unit.c":"#include \"unit.h\"\nint answer(){return VALUE;}\n","unit.h":"#define VALUE 7\n"},"unit.o")

    def test_gcc_object(self):
        self.run_case("gcc",["-c","unit.c","-o","unit.o"],{"unit.c":"int square(int n){return n*n;}\n"},"unit.o")

    def test_clang_object(self):
        self.run_case("clang",["-c","unit.c","-o","unit.o"],{"unit.c":"int square(int n){return n*n;}\n"},"unit.o")

    def test_pic_and_optimization(self):
        for family in ["gcc","clang"]:
            for optimization in ["-O0","-O3","-Os"]:
                with self.subTest(family=family,optimization=optimization):
                    self.run_case(family,[optimization,"-fPIC","-c","unit.c","-o","unit.o"],
                        {"unit.c":"int value=3; int square(int n){return n*n+value;}\n"},"unit.o")

    def test_gcc_sanitizers(self):
        for sanitizer in ["address","undefined"]:
            with self.subTest(sanitizer=sanitizer):
                self.run_case("gcc",["-fsanitize="+sanitizer,"unit.c","-o","program"],
                    {"unit.c":"int value=3; int main(void){return value!=3;}\n"},"program")

    def test_lto_objects(self):
        for family,flag in [("gcc","-flto"),("clang","-flto"),("clang","-flto=thin")]:
            with self.subTest(family=family,flag=flag):
                self.run_case(family,[flag,"-c","unit.c","-o","unit.o"],
                    {"unit.c":"int square(int n){return n*n;}\n"},"unit.o",
                    bitcode=family=="clang",expected_roles={"preprocessing","ir" if family=="clang" else "assembly","object","command"})

    def test_gcc_lto_link(self):
        self.run_case("gcc",["-flto=1","a/same.c","b/same.c","-o","program"],
            {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n"},"program",
            expected_roles={"preprocessing","assembly","object","command","link-map","link-resolution","lto-metadata"})

    def test_caller_saved_lto_intermediates(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-flto","-save-temps","unit.c","-o","program"],
                    {"unit.c":"int main(void){return 0;}\n"},"program",
                    expected_roles={"preprocessing","object","command","link-map"},preserve_extra_bytes=False)

    def test_legacy_gcc_lto_links(self):
        compilers=json.loads(os.environ.get("CAPTURE_LEGACY_GCCS","{}"))
        if not compilers:self.skipTest("cached legacy GCC wrappers are required")
        for version,compiler in compilers.items():
            with self.subTest(version=version):
                self.run_case("gcc",["-flto","a/same.c","b/same.c","-o","program"],
                    {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n",
                     "gcc-lto.ltrans.out":"caller input must survive\n","gcc-lto.ltrans0.o":"caller input must survive\n"},"program",
                    compiler_override=compiler,compiler_version=version,
                    expected_roles={"preprocessing","assembly","object","command","link-map"})

    def test_implicit_gcc_lto_from_objects_and_archive(self):
        compilers=json.loads(os.environ.get("CAPTURE_LEGACY_GCCS","{}"))
        if os.environ.get("CAPTURE_GCC"):compilers[os.environ.get("CAPTURE_GCC_VERSION","15.2.0")]=os.environ["CAPTURE_GCC"]
        if not BINARY or not compilers:self.skipTest("cached GCC wrappers are required")
        for version,compiler in compilers.items():
            with self.subTest(version=version),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                (root/"main.c").write_text("int f(void); int main(void){return f()!=7;}\n")
                (root/"helper.c").write_text("int f(void){return 7;}\n")
                for name in ["main","helper"]:
                    subprocess.run([compiler,"-flto","-g","-c",name+".c","-o",name+".o"],cwd=root,check=True,capture_output=True)
                coordinator=subprocess.check_output([compiler,"-print-prog-name=lto-wrapper"],cwd=root).decode().strip()
                plugin=str(Path(coordinator).parent/"liblto_plugin.so")
                subprocess.run(["ar","--plugin",plugin,"rcs","libhelper.a","helper.o"],cwd=root,check=True,capture_output=True)
                arguments=["main.o","libhelper.a","-o","program"]
                baseline_run=subprocess.run([compiler,*arguments],cwd=root,capture_output=True)
                self.assertEqual(baseline_run.returncode,0,baseline_run.stderr)
                baseline=_capabilities.section_hashes(root/"program",behavior=True)
                baseline_symbols=subprocess.check_output(["readelf","-sW",root/"program"]).decode().splitlines()
                (root/"program").unlink()
                before={path.name:path.read_bytes() for path in root.iterdir()}
                stage=root/"stage";output=root/"output"
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="gcc",version=version,
                    target="x86_64-unknown-linux-gnu",driver="gcc",backend="gcc-trace",supervisor=BINARY,debug_policy="required")))
                result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,capture_output=True,timeout=20,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(subprocess.run([root/"program"],timeout=5).returncode,0)
                actual=_capabilities.section_hashes(root/"program",behavior=True)
                if actual!=baseline:
                    import difflib
                    symbols=subprocess.check_output(["readelf","-sW",root/"program"]).decode().splitlines()
                    self.fail(str([key for key in actual.keys()|baseline.keys() if actual.get(key)!=baseline.get(key)])+"\n"+"\n".join(difflib.unified_diff(baseline_symbols,symbols)))
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                verified=_validator.validate_capture(output)
                record=verified.manifest["invocations"][0]["record"]
                # GCC 4.6/4.8 link their fat objects' native code without
                # implicit LTO. Later selected drivers invoke the plugin.
                if tuple(map(int,version.split('.')[:2])) >= (4,9):
                    self.assertTrue(record["analysis"]["lto"])
                    self.assertIn("assembly",{item["role"] for item in record["artifacts"]})
                self.assertTrue({"command","link-map","linked-output"}<={item["role"] for item in record["artifacts"]})
                self.assertEqual({path.name for path in root.iterdir()},set(before)|{"program","config.json","output"})
                for name,data in before.items():self.assertEqual((root/name).read_bytes(),data)

    def test_legacy_clang_lto_links(self):
        compilers=json.loads(os.environ.get("CAPTURE_LEGACY_CLANGS","{}"))
        if not compilers:self.skipTest("cached legacy Clang wrappers are required")
        for version,compiler in compilers.items():
            modes=["-flto"]+(["-flto=thin"] if tuple(map(int,version.split('.')[:2]))>=(3,9) else [])
            for mode in modes:
                with self.subTest(version=version,mode=mode):
                    self.run_case("clang",[mode,"a/same.c","b/same.c","-o","program"],
                        {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n",
                         "program.o":"caller input must survive\n","program.bc":"caller input must survive\n"},"program",
                        compiler_override=compiler,compiler_version=version,
                        expected_roles={"preprocessing","ir","object","command","link-map"})

    def test_clang_lto_links(self):
        for flag in ["-flto","-flto=thin"]:
            with self.subTest(flag=flag):
                self.run_case("clang",[flag,"a/same.c","b/same.c","-o","program"],
                    {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n"},"program",
                    expected_roles={"preprocessing","ir","object","command","link-map","link-resolution"})

    def test_extensionless_explicit_language(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-x","c","-c","source","-o","unit.o"],
                    {"source":"int square(int n){return n*n;}\n"},"unit.o")

    def test_explicit_preprocessed_language(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-x","cpp-output","-c","source","-o","unit.o"],
                    {"source":"int square(int n){return n*n;}\n"},"unit.o",
                    expected_roles={"assembly","object","command"})

    def test_per_input_language_reset(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-x","c","source","-x","none","unit.c","-o","program"],
                    {"source":"int f(void){return 7;}\n",
                     "unit.c":"int f(void); int main(void){return f()!=7;}\n"},"program")

    def test_llvm_ir_output(self):
        self.run_case("clang",["-emit-llvm","-c","unit.c","-o","unit.bc"],
            {"unit.c":"int square(int n){return n*n;}\n"},"unit.bc",bitcode=True,
            expected_roles={"preprocessing","ir","command"})

    def test_relocatable_link(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-r","unit.c","-o","combined.o"],
                    {"unit.c":"int square(int n){return n*n;}\n"},"combined.o",
                    expected_roles={"preprocessing","assembly","object","command","link-map"})

    def test_lto_relocatable_links(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-flto","-r","unit.c","-o","combined.o"],
                    {"unit.c":"int square(int n){return n*n;}\n"},"combined.o",
                    expected_roles={"preprocessing","object","command","link-map"})

    def test_bitcode_link_output(self):
        self.run_case("clang",["-flto","-Wl,-plugin-opt=emit-llvm","unit.c","-o","linked.bc"],
            {"unit.c":"int main(void){return 0;}\n"},"linked.bc",bitcode=True,
            expected_roles={"preprocessing","ir","command"})

    def test_response_delimiter(self):
        # Clang accepts -- in response files. GCC treats it as an unknown
        # option, so this case exercises the actual Clang dialect only.
        self.run_case("clang",["@arguments.rsp"],
            {"unit.c":"int square(int n){return n*n;}\n","arguments.rsp":"-c -o unit.o -- unit.c\n"},"unit.o")

    def test_precompiled_headers(self):
        for family in ["gcc","clang"]:
            for language in ["c-header","c++-header"]:
                with self.subTest(family=family,language=language):
                    self.run_case(family,["-x",language,"-c","unit.h","-o","unit.pch"],
                        {"unit.h":"static inline int square(int n){return n*n;}\n"},"unit.pch",
                        pch=True,expected_roles={"preprocessing","pch","command"})

    def test_gcc_pch_consumers(self):
        compiler=os.environ.get("CAPTURE_GCC")
        if not BINARY or not compiler:self.skipTest("cached GCC is required")
        self._gcc_pch_consumers(compiler,native_version("gcc"))

    def test_legacy_gcc_pch_macros(self):
        compilers=json.loads(os.environ.get("CAPTURE_PCH_GCCS", "{}"))
        if not BINARY or not compilers:self.skipTest("cached legacy GCC drivers are required")
        for version, compiler in compilers.items():
            with self.subTest(version=version):self._gcc_pch_consumers(compiler,version)

    def _gcc_pch_consumers(self,compiler,version):
        for language in ["c","c++"]:
            with self.subTest(language=language),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"output"
                (root/"unit.h").write_text("#define HEADER_VALUE 7\n#define REMOVED_VALUE 8\n#undef REMOVED_VALUE\nstatic inline int square(int n){return n*n;}\n")
                (root/"unit.c").write_text("#ifdef REMOVED_VALUE\n#error stale macro\n#endif\nint value(void){return square(3)+HEADER_VALUE;}\n")
                commands=[["-x",language+"-header","-c","unit.h","-o","unit.h.gch"],
                    ["-x",language,"-Werror=invalid-pch","-include","unit.h","-c","unit.c","-o","unit.o"]]
                for command in commands:
                    result=subprocess.run([compiler,*command],cwd=root,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                expected=_capabilities.section_hashes(root/"unit.o",behavior=True)
                (root/"unit.h.gch").unlink();(root/"unit.o").unlink()
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="gcc",version=version,
                    target="x86_64-unknown-linux-gnu",driver="gcc",backend="gcc-trace",supervisor=BINARY,debug_policy="required")))
                for command in commands:
                    result=subprocess.run([BINARY,"--config",config,"--",*command],cwd=root,capture_output=True,timeout=20,
                        env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                    self.assertEqual(result.returncode,0,(command,result.stderr))
                self.assertEqual(_capabilities.section_hashes(root/"unit.o",behavior=True),expected)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                self.assertTrue(_validator.validate_capture(output).complete)

    def test_clang_pch_consumers(self):
        compilers=json.loads(os.environ.get("CAPTURE_PCH_CLANGS","{}"))
        if os.environ.get("CAPTURE_CLANG"):compilers["21.1.8"]=os.environ["CAPTURE_CLANG"]
        if not BINARY or not compilers:self.skipTest("cached Clang drivers are required")
        for version,compiler in compilers.items():
            with self.subTest(version=version),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"output"
                (root/"unit.h").write_text("#define HEADER_VALUE 7\n#define REMOVED_VALUE 8\n#undef REMOVED_VALUE\nstatic inline int square(int n){return n*n;}\n")
                (root/"consumer.c").write_text("#ifdef REMOVED_VALUE\n#error stale macro\n#endif\nint value(void){return square(3)+HEADER_VALUE;}\n")
                commands=[["-x","c-header","-c","unit.h","-o","unit.pch"],
                    ["-include-pch","unit.pch","-c","consumer.c","-o","unit.o"]]
                for command in commands:
                    result=subprocess.run([compiler,*command],cwd=root,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                expected=_capabilities.section_hashes(root/"unit.o",behavior=True)
                (root/"unit.pch").unlink();(root/"unit.o").unlink()
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="clang",version=version,
                    target="x86_64-unknown-linux-gnu",driver="clang",backend="clang-trace",supervisor=BINARY,debug_policy="required")))
                for command in commands:
                    result=subprocess.run([BINARY,"--config",config,"--",*command],cwd=root,capture_output=True,timeout=20,
                        env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                    self.assertEqual(result.returncode,0,(command,result.stderr))
                self.assertEqual(_capabilities.section_hashes(root/"unit.o",behavior=True),expected)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                self.assertTrue(_validator.validate_capture(output).complete)

    def test_pch_rejects_changed_inputs(self):
        compiler=os.environ.get("CAPTURE_CLANG")
        if not BINARY or not compiler:self.skipTest("cached Clang is required")
        for changed,touch in [("unit.h",False),("nested.h",False),("nested.h",True)]:
            with self.subTest(changed=changed,touch=touch),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"output"
                source={"unit.h":'#include "nested.h"\n',"nested.h":"static inline int square(int n){return n*n;}\n"}
                (root/"unit.c").write_text("#ifdef REMOVED_VALUE\n#error stale macro\n#endif\nint value(void){return square(3)+HEADER_VALUE;}\n")
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family="clang",version="21.1.8",
                    target="x86_64-unknown-linux-gnu",driver="clang",backend="clang-trace",supervisor=BINARY,debug_policy="required")))
                statuses=[]
                for captured in [False,True]:
                    for name,text in source.items():(root/name).write_text(text)
                    command=[BINARY,"--config",config,"--"] if captured else [compiler]
                    environment=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view"))
                    result=subprocess.run([*command,"-x","c-header","-c","unit.h","-o","unit.pch"],cwd=root,env=environment,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                    if touch:
                        state=(root/changed).stat();os.utime(root/changed,ns=(state.st_atime_ns,state.st_mtime_ns+2_000_000_000))
                    else:(root/changed).write_text(source[changed]+"/* changed after PCH creation */\n")
                    result=subprocess.run([*command,"-include-pch","unit.pch","-c","unit.c","-o","unit.o"],cwd=root,env=environment,capture_output=True,timeout=15)
                    statuses.append(result.returncode)
                self.assertEqual(statuses,[1,1])
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                self.assertTrue(_validator.validate_capture(output).complete)

    def test_intentionally_discarded_outputs(self):
        for family in ["gcc","clang"]:
            compiler=os.environ.get("CAPTURE_"+family.upper())
            if not BINARY or not compiler:continue
            with self.subTest(family=family),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"output"
                (root/"unit.c").write_text("int square(int n){return n*n;}\n")
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family=family,
                    version=native_version(family),target="x86_64-unknown-linux-gnu",driver="c",
                    backend=family+"-trace",supervisor=BINARY,debug_policy="required")))
                for mode in ["-c","-S","-E"]:
                    arguments=[mode,"unit.c","-o","/dev/null"]
                    baseline=subprocess.run([compiler,*arguments],cwd=root,capture_output=True)
                    result=subprocess.run([BINARY,"--config",config,"--",*arguments],cwd=root,capture_output=True,
                        env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage)))
                    self.assertEqual(baseline.returncode,0,baseline.stderr)
                    self.assertEqual(result.returncode,0,result.stderr)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,(final.stderr,json.loads((output/"manifest.json").read_text())))
                self.assertTrue(_validator.validate_capture(output).complete)

    def test_assembly_stdout(self):
        for family in ["gcc","clang"]:
            compiler=os.environ.get("CAPTURE_"+family.upper())
            if not BINARY or not compiler:continue
            with self.subTest(family=family),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);stage=root/"stage";output=root/"output"
                (root/"unit.c").write_text("int square(int n){return n*n;}\n")
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family=family,
                    version=native_version(family),target="x86_64-unknown-linux-gnu",driver="c",
                    backend=family+"-trace",supervisor=BINARY,debug_policy="required")))
                result=subprocess.run([BINARY,"--config",config,"--","-S","unit.c","-o","-"],cwd=root,capture_output=True,timeout=15,
                    env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage)))
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn(b"square",result.stdout)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,(final.stderr,json.loads((output/"manifest.json").read_text())))
                verified=_validator.validate_capture(output)
                artifacts=[artifact for entry in verified.manifest["invocations"] for artifact in entry["record"]["artifacts"]]
                stdout=next(artifact for artifact in artifacts if artifact["path_base"]=="stdout")
                self.assertEqual(stdout["role"],"assembly")
                self.assertEqual((output/"objects"/stdout["sha256"]).read_bytes(),result.stdout)
                self.assertFalse((root/"unit.i").exists())
                self.assertFalse((root/"unit.s").exists())

    def test_clang_header_modules(self):
        self.run_case("clang",["-fmodules","-fimplicit-module-maps","-fmodules-cache-path=cache","-Iinclude","-c","unit.c","-o","unit.o"],
            {"unit.c":'#include "unit.h"\nint square(int n){return helper(n);}\n',
             "include/unit.h":"static inline int helper(int n){return n*n;}\n",
             "include/module.modulemap":'module Fixture { header "unit.h" export * }\n'},"unit.o",
            expected_roles={"preprocessing","assembly","object","ir","module","module-map","command"},allowed_directories=["cache"])

    def test_cxx20_module_interfaces(self):
        source="export module Fixture;\nexport int square(int n){return n*n;}\n"
        self.run_case("clang",["-std=c++20","--precompile","unit.cppm","-o","unit.pcm"],
            {"unit.cppm":source},"unit.pcm",pch=True,expected_roles={"preprocessing","module","command"})
        self.run_case("clang",["-std=c++20","-c","unit.cppm","-o","unit.o"],
            {"unit.cppm":source},"unit.o",expected_roles={"preprocessing","module","assembly","object","command"})
        self.run_case("gcc",["-std=c++20","-fmodules-ts","-c","unit.cpp","-o","unit.o"],
            {"unit.cpp":source},"unit.o",expected_roles={"preprocessing","module","assembly","object","command"},
            allowed_directories=["gcm.cache"])

    def test_cxx20_module_consumers(self):
        for family in ["gcc","clang"]:
            compiler=os.environ.get("CAPTURE_"+family.upper())
            if not BINARY or not compiler:continue
            with self.subTest(family=family),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                (root/"unit.cppm").write_text("export module Fixture;\nexport int square(int n){return n*n;}\n")
                (root/"consumer.cpp").write_text("import Fixture;\nint main(void){return square(3)!=9;}\n")
                commands=([
                    ["-std=c++20","--precompile","unit.cppm","-o","Fixture.pcm"],
                    ["-std=c++20","-c","Fixture.pcm","-o","interface.o"],
                    ["-std=c++20","-fmodule-file=Fixture=Fixture.pcm","-c","consumer.cpp","-o","consumer.o"]]
                    if family=="clang" else [
                    ["-std=c++20","-fmodules-ts","-x","c++","-c","unit.cppm","-o","interface.o"],
                    ["-std=c++20","-fmodules-ts","-c","consumer.cpp","-o","consumer.o"]])
                commands.append(["interface.o","consumer.o","-o","program"])
                for command in commands:
                    result=subprocess.run([compiler,*command],cwd=root,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)
                baseline=_capabilities.section_hashes(root/"program",behavior=True)
                for path in list(root.iterdir()):
                    if path.suffix in [".cpp",".cppm"]:continue
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink()
                stage=root/"stage";output=root/"output"
                subprocess.run([BINARY,"init",stage],check=True)
                config=root/"config.json"
                config.write_text(json.dumps(dict(schema=1,wrapper_revision=1,executable=compiler,family=family,
                    version=native_version(family),target="x86_64-unknown-linux-gnu",driver="c++",
                    backend=family+"-trace",supervisor=BINARY,debug_policy="required")))
                for command in commands:
                    result=subprocess.run([BINARY,"--config",config,"--",*command],cwd=root,capture_output=True,timeout=20,
                        env=dict(os.environ,DATASET_CAPTURE_STAGE=str(stage),DATASET_CAPTURE_VIEW=str(stage/"descriptor-view")))
                    self.assertEqual(result.returncode,0,(command,result.stderr))
                self.assertEqual(subprocess.run([root/"program"],timeout=5).returncode,0)
                self.assertEqual(_capabilities.section_hashes(root/"program",behavior=True),baseline)
                final=subprocess.run([BINARY,"finalize",stage,output,"required"],capture_output=True)
                self.assertEqual(final.returncode,0,final.stderr)
                self.assertTrue(_validator.validate_capture(output).complete)

    def test_gcc_same_basename_link(self):
        self.run_case("gcc",["a/same.c","b/same.c","-o","program"],
            {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n"},"program")

    def test_versioned_library_and_linker_script_operand(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["unit.c","libfixture.so.1","-o","program"],
                    {"unit.c":"int main(void){return 0;}\n","libfixture.so.1":"INPUT(-lc)\n"},"program")

    def test_caller_selected_auxiliary_directory(self):
        self.run_case("gcc",["-save-temps=obj","-dumpdir","aux/","-c","unit.c","-o","unit.o"],
            {"unit.c":"int square(int n){return n*n;}\n","aux/marker":"preserved\n"},"unit.o",allowed_directories=["aux"])

    def test_executable_load_segment_includes_elf_header(self):
        self.run_case("gcc",["-Wl,-z,noseparate-code","unit.c","-o","program"],
            {"unit.c":"int main(void){return 0;}\n"},"program")

    def test_reference_removal_is_validated(self):
        self.run_case("gcc",["unit.c","-o","program"],
            {"unit.c":'const char stored_path[]="/nix/store/00000000000000000000000000000000-fixture/lib"; int main(void){return stored_path[0]!=\'/\';}\n'},"program",reference_erasure=True)

    def test_clang_same_basename_link(self):
        self.run_case("clang",["a/same.c","b/same.c","-o","program"],
            {"a/same.c":"int f(void); int main(void){return f()!=7;}\n","b/same.c":"int f(void){return 7;}\n"},"program")

    def test_clang_stdin(self):
        self.run_case("clang",["-x","c","-c","-","-o","unit.o"],{},"unit.o",b"int square(int n){return n*n;}\n")

    def test_clang_fatal_assembler(self):
        self.run_case("clang",["-g","-Wa,--fatal-warnings","-c","unit.c","-o","unit.o"],
            {"unit.c":"int square(int n){return n*n;}\n"},"unit.o")

    def test_source_macros(self):
        for family in ["gcc","clang"]:
            if not os.environ.get("CAPTURE_"+family.upper()):
                continue
            with self.subTest(family=family):
                self.run_case(family,["-c","unit.c","-o","unit.o"],
                    {"unit.c":"const char *file = __FILE__; const char *base = __BASE_FILE__; int line = __LINE__;\n"},"unit.o")

    def test_existing_files_are_preserved(self):
        for family in ["gcc","clang"]:
            if not os.environ.get("CAPTURE_"+family.upper()):
                continue
            with self.subTest(family=family):
                self.run_case(family,["-c","unit.c","-o","unit.o"],
                    {"unit.c":'#include "unit.i"\nint square(int n){return helper(n);}\n',
                     "unit.i":"static int helper(int n){return n*n;}\n",
                     "unit.s":"a source file that capture must leave untouched\n"},"unit.o")

    def test_existing_save_temps(self):
        for family in ["gcc","clang"]:
            with self.subTest(family=family):
                self.run_case(family,["-save-temps","-c","unit.c","-o","unit.o"],
                    {"unit.c":"int square(int n){return n*n;}\n"},"unit.o",allowed_auxiliary=["unit.i","unit.s"]+(["unit.bc"] if family=="clang" else []))

    def test_assembly_inputs(self):
        for family in ["gcc","clang"]:
            for extension in ["s","S"]:
                with self.subTest(family=family,extension=extension):
                    self.run_case(family,["-c","unit."+extension,"-o","unit.o"],
                        {"unit."+extension:".text\n.globl square\nsquare:\n mov %edi, %eax\n imul %edi, %eax\n ret\n"},"unit.o",
                        expected_roles={"assembly","command","object"})


if __name__ == "__main__":
    unittest.main()

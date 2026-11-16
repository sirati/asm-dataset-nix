"""Legacy wrapper compatibility, including query/link isolation and target sizes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


REPO=Path(__file__).resolve().parents[2]
HOOK=REPO/"lib/legacy-clang-stdlib.sh"
HEADER=REPO/"lib/legacy-clang-stdlib.h"


@unittest.skipUnless(shutil.which("bash"),"Bash is required")
class LegacyHookTests(unittest.TestCase):
    def flags(self,args,*,cc1=0,arm_ehabi=False):
        script='params=("$@"); extraBefore=(caller-before); extraAfter=(); cc1='+str(cc1)+'; datasetLegacyStdlibHeader=/compat.h\n'
        script+='source "$COMPAT_HOOK"\nprintf "%s\\0" "${extraBefore[@]}"\n'
        if arm_ehabi:script='datasetLegacyArmEhabi=1\n'+script
        result=subprocess.run(["bash","--norc","--noprofile","-eu","-c",script,"test",*args],
            capture_output=True,check=True,env=dict(os.environ,COMPAT_HOOK=str(HOOK)))
        return result.stdout.split(b"\0")[:-1]

    def test_source_options_precede_caller_flags_without_splitting_paths(self):
        self.assertEqual(self.flags(["-c","source space.cpp","-o","unit.o"]),
            [b"-include",b"/compat.h",b"caller-before"])
        self.assertEqual(self.flags(["--","-leading.cpp"]),[b"-include",b"/compat.h",b"caller-before"])
        self.assertEqual(self.flags(["-x","c++","-c","input"]),[b"-include",b"/compat.h",b"caller-before"])

    def test_queries_object_links_and_frontend_calls_keep_their_arguments(self):
        for args in [["--version"],["-v"],["unit.o","-o","program"],
                ["unit.o","-o","output.cpp"],["-x","none","unit.o"]]:
            with self.subTest(args=args):self.assertEqual(self.flags(args),[b"caller-before"])
        self.assertEqual(self.flags(["-x","c++","source.cpp"],cc1=1),[b"caller-before"])

    def test_arm_ehabi_defaults_apply_to_sources_before_caller_flags(self):
        self.assertEqual(self.flags(["-c","unit.cpp"],arm_ehabi=True),
            [b"-include",b"/compat.h",b"-mllvm",b"-arm-enable-ehabi",b"-mllvm",b"-arm-enable-ehabi-descriptors",b"caller-before"])
        self.assertEqual(self.flags(["unit.o","-o","program"],arm_ehabi=True),[b"caller-before"])


class LegacyMacroTests(unittest.TestCase):
    def test_aliases_follow_target_width_and_explicit_abi_choices(self):
        compiler=json.loads(os.environ.get("CAPTURE_PCH_CLANGS","{}")).get("3.4.2")
        if not compiler:self.skipTest("cached Clang 3.4 is required")
        source=b'''#if __PTRDIFF_MAX__ != (__SIZE_MAX__ >> 1)
#error invalid maximum
#endif
#if _GLIBCXX_USE_CXX11_ABI != EXPECTED_ABI
#error ignored explicit ABI choice
#endif
typedef char pointer_size[(sizeof(__UINTPTR_TYPE__)==sizeof(void*))?1:-1];
enum{first=__COUNTER__,second=__COUNTER__};
typedef char counter_unchanged[(second==first+1)?1:-1];
'''
        for target in [[],["-m32"],["-target","aarch64-unknown-linux-gnu"],
                ["-target","mips64el-unknown-linux-gnuabin32","-mabi=n32"]]:
            for abi in [0,1]:
                with self.subTest(target=target,abi=abi):
                    explicit=["-D_GLIBCXX_USE_CXX11_ABI=1"] if abi else []
                    result=subprocess.run([compiler,*target,*explicit,"-DEXPECTED_ABI="+str(abi),
                        "-std=c++98","-include",str(HEADER),"-x","c++","-fsyntax-only","-"],input=source,capture_output=True,timeout=15)
                    self.assertEqual(result.returncode,0,result.stderr)


if __name__=="__main__":unittest.main()

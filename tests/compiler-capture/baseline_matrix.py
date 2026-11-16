"""Check actual baseline C++ libraries, LTO backends and emulated runtimes."""
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


SOURCE='''#include <string>
#include <stdexcept>
int main(int argc,char **argv){
  const bool requested_throw=argc>1;
  try {
    std::string value=argc?argv[0]:"capture";
    if(requested_throw)throw std::runtime_error(value);
    return value.empty();
  }catch(const std::exception &error){return requested_throw && error.what()[0]?0:1;}
}
'''


def main(configuration,output):
    config=json.loads(Path(configuration).read_text())
    target=config['target']
    expected={'x86_64':(62,2,1),'i686':(3,1,1),'aarch64':(183,2,1),
        'armv7l-hf':(40,1,1),'armv7l-sf':(40,1,1),'mipsel':(8,1,1),
        'mips64el':(8,1,1),'ppc32':(20,1,2),'ppc64':(21,2,2),'riscv64':(243,2,1)}[target]
    report=dict(schema=1,target=target,runtime=config['runtime'],compiler_options=config['compilerOptions'],lto_options=config['ltoOptions'],cases=[])
    # The fixture's native GCC sets empty stdlib overrides that suppress old
    # Clang wrappers' own defaults. Each compiler must select its own headers.
    environment={key:value for key,value in os.environ.items()
        if not re.fullmatch(r'NIX(?:_.*)?_CXXSTDLIB_(?:COMPILE|LINK)',key)}
    def run(args):
        call=subprocess.run(args,capture_output=True,timeout=45,env=environment)
        if call.returncode:raise RuntimeError(f'{args!r}: status {call.returncode}: {call.stderr[-3072:]!r}')
        return call
    for compiler in config['compilers']:
        root=Path(compiler['compiler']);root.mkdir();source=root/'unit.cpp';source.write_text(SOURCE)
        for mode in compiler['modes']:
            case=dict(compiler=compiler['compiler'],version=compiler['version'],mode=mode,status='passed')
            try:
                version=run([compiler['driver'],'--version']).stdout
                if not re.search(rb'(?<![0-9.])'+re.escape(compiler['version'].encode())+rb'(?![0-9.])',version):
                    raise RuntimeError('actual compiler version contradicts metadata')
                flags=[] if mode=='native' else ['-flto' if mode=='lto' else '-flto=thin',*config['ltoOptions']]
                program=root/('program-'+mode)
                run([compiler['driver'],'-std=c++98','-O2',*config['compilerOptions'],*flags,str(source),'-o',str(program)])
                raw=program.read_bytes();order='<' if raw[5]==1 else '>'
                machine=struct.unpack_from(order+'H',raw,18)[0]
                if raw[:4]!=b'\x7fELF' or (machine,raw[4],raw[5])!=expected:
                    raise RuntimeError('incorrect output ELF target')
                case['elf_flags']=struct.unpack_from(order+'I',raw,48 if raw[4]==2 else 36)[0]
                for arguments in [[],['exercise-exceptions']]:run([*config['runtime'],str(program.resolve()),*arguments])
                case['runtime_kind']='emulated' if config['runtime'] else 'native'
            except (OSError,RuntimeError,subprocess.TimeoutExpired) as error:
                case.update(status='failed',error=str(error)[-4096:])
            report['cases'].append(case)
    Path(output).write_text(json.dumps(report,sort_keys=True)+'\n')


if __name__=='__main__':main(*sys.argv[1:])

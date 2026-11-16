"""Small actual-driver experiments. Run in a sandboxed fixture, one at a time."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import importlib.util

_ppc_debug=None


def ppc_debug_sections(before,after,*,family,version):
    if len(before)<20 or before[18:20] not in [b"\x00\x14",b"\x00\x15"] or family!="clang":return set()
    global _ppc_debug
    if _ppc_debug is None:
        path=os.environ.get("CAPTURE_PPC_DEBUG",str(Path(__file__).with_name("ppc_debug.py")))
        spec=importlib.util.spec_from_file_location("ppc_debug_verifier",path)
        _ppc_debug=importlib.util.module_from_spec(spec);spec.loader.exec_module(_ppc_debug)
    return _ppc_debug.verify(before,after,section_hashes,family=family,version=version)


def debug_unwind_difference(before, after):
    """Only a debug-matched compiler reference can authorize these differences."""
    changed={key for key in before.keys() | after.keys() if before.get(key)!=after.get(key)}
    def unwind(key):
        if key.startswith((".rela.", ".rel.")):
            key=key[key.index(".",1)+1:]
            key="."+key
        return key in [".eh_frame", ".eh_frame_hdr", ".sframe"] or key.startswith(
            (".ARM.exidx", ".ARM.extab", ".gcc_except_table"))
    return bool(changed) and all(unwind(key) for key in changed)


def aarch64_w0_immediate(word):
    if word&0xffe0001f==0x52800000:return True  # MOVZ W0, #imm16.
    if word&0xffc003ff!=0x320003e0:return False  # ORR W0, WZR, #logical-immediate.
    imms=(word>>10)&63
    length=((~imms)&63).bit_length()-1
    return length>=1 and (imms&((1<<length)-1))!=(1<<length)-1


def aarch64_debug_prologue_swap(before,after,before_header,after_header,*,family,version):
    """Measured -g motion of an independent W0 constant and frame setup."""
    if family!="clang" or not version.startswith(("3.8.","3.9.","4.","5.","6.","7.","8.","9.","10.","11.")):
        return False
    if before_header[:7]!=b"\x7fELF\x02\x01\x01" or after_header[:9]!=before_header[:9]:
        return False
    if len(before_header)<64 or len(after_header)<64 or before_header[18:20]!=b"\xb7\x00":
        return False
    if struct.unpack_from("<H",before_header,16)[0] not in [1,2,3]:return False
    if before_header[16:32]!=after_header[16:32] or before_header[48:52]!=after_header[48:52]:
        return False
    if len(before)!=len(after) or len(before)%4:
        return False
    offset=swaps=0
    while offset<len(before):
        if before[offset:offset+4]==after[offset:offset+4]:
            offset+=4;continue
        if offset+8>len(before):return False
        move,frame=struct.unpack_from("<II",before,offset)
        # MOVZ W0, #imm16 and ADD X29, SP, #0 change disjoint registers and
        # neither sets flags. Every operand and all other bytes stay identical.
        if not aarch64_w0_immediate(move) or frame!=0x910003fd:
            return False
        if after[offset:offset+8]!=before[offset+4:offset+8]+before[offset:offset+4]:
            return False
        offset+=8;swaps+=1
    return swaps>0


def aarch64_frame_map(before,after,functions,left_relocations,right_relocations):
    """Verify only an independent instruction sequence moving past MOV FP, SP."""
    frame=0x910003fd
    positions={};covered=set()
    for start,end in functions:
        if before[start:end]==after[start:end]:continue
        if start%4 or end>len(before) or end-start>1024 or end-start<16:
            raise ValueError("unknown AArch64 function layout")
        if struct.unpack_from('<I',before,start)[0]!=0xa9bf7bfd or before[start:start+4]!=after[start:start+4]:
            raise ValueError("unknown AArch64 frame save")
        first=start+4
        count=next((n for n in range(1,5) if first+4*(n+1)<=end
            and struct.unpack_from('<I',before,first+4*n)[0]==frame
            and after[first:first+4*(n+1)]==before[first+4*n:first+4*(n+1)]+before[first:first+4*n]),None)
        if count is None:raise ValueError("unknown AArch64 frame motion")
        for pc in range(first,first+count*4,4):
            word=struct.unpack_from('<I',before,pc)[0];relocation=left_relocations.get(pc)
            rt=word&31;rn=(word>>5)&31
            if word&0x9f000000==0x90000000 and rt<29:
                if relocation is None or relocation[1] not in [275,276,311]:
                    raise ValueError("ADRP has no matching page relocation")
            elif word&0xffc00000 in [0xb9400000,0xf9400000,0xb9800000] and rt<29 and rn<29:
                kinds=[286,312] if word&0xffc00000==0xf9400000 else [285]
                if relocation is not None and relocation[1] not in kinds:
                    raise ValueError("unknown AArch64 load relocation")
            elif aarch64_w0_immediate(word) and relocation is None:
                pass
            else:raise ValueError("instruction depends on frame state or is unknown")
            positions[pc]=pc+4
        old_frame=first+count*4
        if old_frame in left_relocations:raise ValueError("frame setup has a relocation")
        positions[old_frame]=first;covered.update(range(first,old_frame+4))
    if len(before)!=len(after) or any(a!=b and i not in covered for i,(a,b) in enumerate(zip(before,after))):
        raise ValueError("AArch64 instructions or operands changed")
    expected=[]
    for pc,row in left_relocations.items():
        row=list(row);row[0]=positions.get(pc,pc);expected.append(row)
    if sorted(expected)!=sorted(right_relocations.values()):
        raise ValueError("AArch64 relocation targets, addends or positions changed")


def debug_scheduling_sections(before,after,bits,machine,checker):
    if not 64<=len(before)<=1024**2 or not 64<=len(after)<=1024**2:return set()
    if before[:7]!=b'\x7fELF'+bytes([bits,1,1]) or before[:9]!=after[:9]:return set()
    flags=48 if bits==2 else 36
    if before[16:24]!=after[16:24] or struct.unpack_from('<HH',before,16)!=(1,machine) or before[flags:flags+4]!=after[flags:flags+4]:return set()
    try:
        left=section_hashes(None,raw=before,behavior=True,symbol_records=True,relocation_records=True)
        right=section_hashes(None,raw=after,behavior=True,symbol_records=True,relocation_records=True)
        if {k:v for k,v in left.items() if k.startswith('symbols:')}!={k:v for k,v in right.items() if k.startswith('symbols:')}:return set()
        raw_left=section_hashes(None,raw=before,contents=True);raw_right=section_hashes(None,raw=after,contents=True)
        verified=set()
        for name,metadata in left.items():
            if not name.startswith('.') or not isinstance(metadata,tuple) or not metadata[1]&4 or metadata==right.get(name):continue
            if metadata[:-1]!=right.get(name,())[:-1]:return set()
            tables=[key[len('relocations:'):] for key,value in left.items() if key.startswith('relocations:') and value['target']==name]
            a={};b={}
            for table in tables:
                rows_a=left['relocations:'+table]['entries'];rows_b=right.get('relocations:'+table,{}).get('entries',[])
                if len(rows_a)!=len(rows_b):return set()
                if len({row[0] for row in rows_a})!=len(rows_a) or len({row[0] for row in rows_b})!=len(rows_b):return set()
                if set(a)&{row[0] for row in rows_a} or set(b)&{row[0] for row in rows_b}:return set()
                a.update((row[0],row) for row in rows_a);b.update((row[0],row) for row in rows_b)
            functions=sorted({(row[4],row[4]+row[5]) for key,rows in left.items() if key.startswith('symbols:')
                for row in rows if row[1]&15==2 and row[3]==name.encode().hex() and row[5]})
            checker(bytes.fromhex(raw_left[name]),bytes.fromhex(raw_right[name]),functions,a,b)
            verified.add(name);verified.update(tables)
        return verified
    except (ValueError,KeyError,IndexError,TypeError,struct.error):return set()


def aarch64_debug_sections(before,after,*,family,version):
    if family!='clang' or not version.startswith(('9.','10.','11.')):return set()
    return debug_scheduling_sections(before,after,2,183,aarch64_frame_map)


def x86_frame_map(before,after,functions,left_relocations,right_relocations):
    """Measured i686 load/frame motion, with exact instruction operands."""
    positions={};covered=set();slots=set()
    def load(pc,end):
        if pc+3>end or before[pc]!=0x8b:return None
        modrm=before[pc+1];register=(modrm>>3)&7
        if register in [4,5]:return None
        mode=modrm&0xc7
        length=6 if mode in [0x05,0x85] else 3 if mode==0x45 else 0
        if not length or pc+length>end:return None
        return length,'absolute' if mode==0x05 else 'stack',register
    def move(old,new,length):
        positions.update((old+i,new+i) for i in range(length));covered.update(range(old,old+length))
    for start,end in functions:
        if before[start:end]==after[start:end]:continue
        if not 0<=start<end<=len(before) or end-start>1024 or before[start:start+1]!=b'\x55' or after[start:start+1]!=b'\x55':
            raise ValueError('unknown i686 frame')
        if before[start+1:start+3]==after[start+1:start+3]==b'\x89\xe5':
            first=start+3;a=load(first,end);b=load(first+a[0],end) if a else None
            if not a or not b or a[1]==b[1] or a[2]==b[2]:raise ValueError('dependent or unknown i686 loads')
            middle=first+a[0];last=middle+b[0]
            if after[first:last]!=before[middle:last]+before[first:middle]:raise ValueError('i686 load operands changed')
            move(first,first+b[0],a[0]);move(middle,first,b[0])
            slots.add(first+2 if a[1]=='absolute' else middle+2)
        else:
            first=pc=start+1;count=0
            while pc+2<=end and before[pc:pc+2]!=b'\x89\xe5' and count<4:
                instruction=load(pc,end)
                if instruction and instruction[1]=='absolute':length=instruction[0];slots.add(pc+2)
                elif pc+2<=end and before[pc]==0x31 and before[pc+1]&0xc0==0xc0 and (before[pc+1]>>3)&7==before[pc+1]&7 and before[pc+1]&7 not in [4,5]:length=2
                elif pc+7<=end and before[pc:pc+2]==b'\x83\x3d':length=7;slots.add(pc+2)
                elif pc+5<=end and 0xb8<=before[pc]<=0xbf and before[pc]&7 not in [4,5]:length=5;slots.add(pc+1)
                else:raise ValueError('instruction depends on frame state or is unknown')
                pc+=length;count+=1
            if not count or before[pc:pc+2]!=b'\x89\xe5' or after[first:pc+2]!=b'\x89\xe5'+before[first:pc]:
                raise ValueError('unknown i686 frame motion')
            move(first,first+2,pc-first);move(pc,first,2)
    if len(before)!=len(after) or any(a!=b and i not in covered for i,(a,b) in enumerate(zip(before,after))):
        raise ValueError('i686 instructions or operands changed')
    expected=[]
    for pc,row in left_relocations.items():
        if pc in positions and (pc not in slots or row[1]!=1):raise ValueError('unknown moved i686 relocation')
        row=list(row);row[0]=positions.get(pc,pc);expected.append(row)
    if sorted(expected)!=sorted(right_relocations.values()):raise ValueError('i686 relocations changed')


def x86_debug_sections(before,after,*,family,version):
    if family!='clang' or not version.startswith(('10.','11.')):return set()
    return debug_scheduling_sections(before,after,1,3,x86_frame_map)


def compatible_behavior(before, after, debug_reference=None, *, prologue_swap=False, ppc_sections=(), aarch64_sections=(), x86_sections=()):
    if before==after:
        return "identical"
    if debug_unwind_difference(before,after) and debug_reference==after:
        return "required-debug-unwind"
    if prologue_swap and debug_reference==after and before.get(".text",())[:-1]==after.get(".text",())[:-1]:
        left={key:value for key,value in before.items() if key!=".text"}
        right={key:value for key,value in after.items() if key!=".text"}
        if left==right or debug_unwind_difference(left,right):
            return "required-debug-aarch64-prologue"
    scheduling_sections=ppc_sections or aarch64_sections or x86_sections
    if scheduling_sections and all(before.get(key,())[:-1]==after.get(key,())[:-1] for key in scheduling_sections):
        left={key:value for key,value in before.items() if key not in scheduling_sections}
        right={key:value for key,value in after.items() if key not in scheduling_sections}
        # Correcting legacy GNU-as CFI ordering can preserve the original
        # unwind sections while the independent -g build gains spurious frames.
        # The checked instructions/relocations must still match that reference;
        # all remaining sections must then equal the original compiler output.
        original_unwind=(left==right and debug_reference is not None
            and debug_unwind_difference(debug_reference,after))
        if (debug_reference==after and (left==right or debug_unwind_difference(left,right))) or original_unwind:
            return "required-debug-ppc-scheduling" if ppc_sections else "required-debug-aarch64-scheduling" if aarch64_sections else "required-debug-i686-scheduling"
    return None


def section_hashes(path, *, behavior=False,contents=False,symbol_records=False,relocation_records=False,raw=None):
    data = path.read_bytes() if raw is None else raw
    if data[:4] != b"\x7fELF":
        return {}
    bits, order = data[4], "<" if data[5] == 1 else ">"
    machine=struct.unpack_from(order+"H",data,18)[0]
    if bits == 2:
        shoff = struct.unpack_from(order+"Q", data, 40)[0]
        entsize, count, names_idx = struct.unpack_from(order+"HHH", data, 58)
        fmt = order+"IIQQQQIIQQ"
    else:
        shoff = struct.unpack_from(order+"I", data, 32)[0]
        entsize, count, names_idx = struct.unpack_from(order+"HHH", data, 46)
        fmt = order+"IIIIIIIIII"
    headers = [struct.unpack_from(fmt, data, shoff+i*entsize) for i in range(count)]
    names = data[headers[names_idx][4]:headers[names_idx][4]+headers[names_idx][5]]
    result = {}
    padding={}
    relocatable=struct.unpack_from(order+"H",data,16)[0]==1
    if behavior and machine==243 and relocatable:
        for table in headers:
            if table[1]!=4 or not headers[table[7]][2]&4:continue
            target=headers[table[7]]
            for offset in range(table[4],table[4]+table[5],table[9]):
                position,info,addend=struct.unpack_from(order+("QQq" if bits==2 else "IIi"),data,offset)
                kind=info&0xffffffff if bits==2 else info&0xff
                if kind!=43:continue # R_RISCV_ALIGN, linker-removable NOP bytes.
                if addend<0 or position+addend>target[5]:raise RuntimeError("invalid RISC-V relaxation range")
                content=data[target[4]+position:target[4]+position+addend]
                cursor=0
                while cursor<len(content):
                    if content[cursor:cursor+2]==b"\x01\x00":cursor+=2
                    elif content[cursor:cursor+4]==b"\x13\x00\x00\x00":cursor+=4
                    else:raise RuntimeError("RISC-V alignment relocation covers non-NOP instructions")
                padding.setdefault(table[7],[]).append((position,position+addend))
        for ranges in padding.values():
            ranges.sort()
            if any(a[1]>b[0] for a,b in zip(ranges,ranges[1:])):raise RuntimeError("overlapping RISC-V relaxation ranges")
    def logical_position(section,position):
        return position-sum(max(0,min(position,last)-first) for first,last in padding.get(section,[]))
    def section_name(index):
        header=headers[index]
        return names[header[0]:].split(b"\0",1)[0].hex()
    symbol_cache={}
    symbol_locations={}
    def symbols(index):
        if index in symbol_cache:return symbol_cache[index]
        table=headers[index];strings=headers[table[6]]
        strings=data[strings[4]:strings[4]+strings[5]]
        values=[];locations=[]
        for offset in range(table[4],table[4]+table[5],table[9]):
            if bits==2:
                name,info,other,section,value,size=struct.unpack_from(order+"IBBHQQ",data,offset)
            else:
                name,value,size,info,other,section=struct.unpack_from(order+"IIIBBH",data,offset)
            label=strings[name:].split(b"\0",1)[0].hex()
            identity=section_name(section) if 0<section<len(headers) else section
            locations.append((section,value))
            size=logical_position(section,value+size)-logical_position(section,value)
            value=logical_position(section,value)
            values.append([label,info,other,identity,value,size])
        symbol_cache[index]=values
        symbol_locations[index]=locations
        return values
    if behavior:
        for index,header in enumerate(headers):
            if header[1] in [2,11] and header[5]:
                # Debugging adds section/file symbols. Compare actual data,
                # function, TLS and externally visible untyped symbols by
                # identity, independently of symbol-table numbering.
                entries=symbols(index)
                public=[entry for entry,(section,_) in zip(entries,symbol_locations[index])
                    if (not 0<section<len(headers) or headers[section][2]&2)
                    and (entry[1]&15 in [1,2,6,10]
                        or entry[1]&15==0 and entry[1]>>4 in [1,2] and entry[0])]
                public=sorted(public,key=repr)
                result["symbols:"+section_name(index)]=public if symbol_records else hashlib.sha256(json.dumps(public,separators=(",",":")).encode()).hexdigest()
    for section_index,header in enumerate(headers):
        name = names[header[0]:].split(b"\0", 1)[0].decode("ascii", "backslashreplace")
        if not header[5]:
            continue
        if behavior:
            # Compare every allocated section and every relocation section
            # targeting allocated bytes. Debug relocation tables may differ.
            relevant=bool(header[2]&2) or header[1] in [4,9] and header[7]<len(headers) and bool(headers[header[7]][2]&2)
            if relevant and name!=".note.gnu.build-id":
                content=b"" if header[1]==8 else data[header[4]:header[4]+header[5]]
                logical_size=header[5]
                if section_index in padding:
                    pieces=[];position=0
                    for first,last in padding[section_index]:pieces.append(content[position:first]);position=last
                    pieces.append(content[position:]);content=b"".join(pieces);logical_size=len(content)
                if header[1]==6 and machine==8:
                    # GNU BFD sets DT_MIPS_UNREFEXTNO to the output section
                    # count plus one. Debug-only sections change that value.
                    # Keep every other dynamic entry and compare the boundary
                    # relative to the same nonallocated-section count.
                    content=bytearray(content)
                    nonallocated=sum(not section[2]&2 for section in headers)
                    for offset in range(0,len(content),header[9]):
                        tag,value=struct.unpack_from(order+("QQ" if bits==2 else "II"),content,offset)
                        if tag==0x70000012:
                            struct.pack_into(order+("Q" if bits==2 else "I"),content,offset+(8 if bits==2 else 4),value-nonallocated)
                if header[1] in [4,9]:
                    # Debug sections can move symbol-table indices without
                    # changing any relocation's target or addend.
                    relocations=[];table=symbols(header[6]);alignments=set()
                    for offset in range(header[4],header[4]+header[5],header[9]):
                        position,info=struct.unpack_from(order+("QQ" if bits==2 else "II"),data,offset)
                        symbol,kind=(info>>32,info&0xffffffff) if bits==2 else (info>>8,info&0xff)
                        addend=struct.unpack_from(order+("q" if bits==2 else "i"),data,offset+(16 if bits==2 else 8))[0] if header[1]==4 else None
                        position=logical_position(header[7],position)
                        if machine==243 and relocatable and kind==43 and header[7] in padding:
                            alignment=1<<max(0,addend.bit_length())
                            if position==0 and alignment<=headers[header[7]][8]:continue
                            key=(position,alignment)
                            if key in alignments:continue
                            alignments.add(key);relocations.append([position,kind,"alignment",alignment]);continue
                        target,value=symbol_locations[header[6]][symbol]
                        identity=list(table[symbol])
                        if target in padding:
                            if addend is not None and 0<=value+addend<=headers[target][5]:addend=logical_position(target,value+addend)-logical_position(target,value)
                            if identity[1]>>4==0 and identity[1]&15 in [0,3] and addend is not None:
                                addend+=identity[4];identity=["",3,identity[2],identity[3],0,0]
                        relocations.append([position,kind,identity,addend])
                    content=json.dumps(relocations,separators=(",",":")).encode()
                    if relocation_records:
                        result["relocations:"+name]=dict(target=bytes.fromhex(section_name(header[7])).decode("ascii"),entries=relocations)
                    if machine==243 and relocatable and not relocations:continue
                    if machine==243 and relocatable:logical_size=len(relocations)*header[9]
                # SHF_INFO_LINK declares sh_info's target section. Older LLVM
                # omits the flag on ARM relocation tables. We already resolve
                # and compare that target and every relocation above.
                flags=header[2]&~0x40 if header[1] in [4,9] else header[2]
                result[name]=(header[1],flags,header[3],logical_size,header[8],hashlib.sha256(content).hexdigest())
        elif contents or name in [".text", ".data", ".rodata", ".symtab", ".debug_info", ".debug_line"]:
            content=data[header[4]:header[4]+header[5]]
            result[name] = content.hex() if contents else hashlib.sha256(content).hexdigest()
    return result


def main():
    compiler, family, mode, output = sys.argv[1:]
    root = Path.cwd()
    cases = [
        ("c-object", ["-c", "unit.c", "-o", "unit.o"], "unit.o"),
        ("cxx-object", ["-x", "c++", "-c", "unit.c", "-o", "unit.o"], "unit.o"),
        ("implicit", ["-c", "unit.c"], "unit.o"),
        ("joined-output", ["-c", "unit.c", "-ounit.o"], "unit.o"),
        ("assembly", ["-S", "unit.c", "-o", "unit.s"], "unit.s"),
        ("preprocess", ["-E", "unit.c", "-o", "unit.i"], "unit.i"),
        ("dependencies", ["-M", "unit.c"], None),
        ("syntax", ["-fsyntax-only", "unit.c"], None),
        ("version", ["--version"], None),
        ("stdin", ["-x", "c", "-c", "-", "-o", "unit.o"], "unit.o"),
        ("dev-null", ["-c", "unit.c", "-o", "/dev/null"], None),
        ("space", ["-c", "source space.c", "-o", "object space.o"], "object space.o"),
        ("non-utf8", [b"-c", b"source\xff.c", b"-o", b"object\xff.o"], b"object\xff.o"),
        ("user-save-temps", ["-save-temps=obj", "-c", "unit.c", "-o", "unit.o"], "unit.o"),
        ("debug-fatal-assembler", ["-g", "-Wa,--fatal-warnings", "-c", "unit.c", "-o", "unit.o"], "unit.o"),
        ("two-basename-link", ["a/foo.c", "b/foo.c", "-o", "program"], "program"),
        ("response", ["@args.rsp"], "unit.o"),
        ("nested-response", ["@outer.rsp"], "unit.o"),
        ("empty-response-arg", ["@empty.rsp"], None),
        ("missing-response", ["@missing.rsp"], None),
        ("preprocessed", ["-c", "input.i", "-o", "unit.o"], "unit.o"),
        ("assembler", ["-c", "input.s", "-o", "unit.o"], "unit.o"),
        ("compile-failure", ["-c", "broken.c", "-o", "unit.o"], None),
        ("lto", ["-flto", "-c", "unit.c", "-o", "unit.o"], "unit.o"),
    ]
    result = dict(schema=1, family=family, mode=mode, compiler=compiler,
                  actual_version=subprocess.check_output([compiler,"--version"])[:2048].hex(),
                  actual_target=subprocess.check_output([compiler,"-dumpmachine"])[:2048].hex(), cases=[])
    for name, arguments, primary in cases:
        work = root / name
        work.mkdir()
        (work/"a").mkdir()
        (work/"b").mkdir()
        source = b"int square(int n) { return n*n; }\n"
        for filename in [b"unit.c", b"source space.c", b"source\xff.c", b"input.i"]:
            with open(os.fsencode(work)+b"/"+filename, "wb") as file:
                file.write(source)
        (work/"a/foo.c").write_text("int f(void); int main(void) { return f()!=7; }\n")
        (work/"b/foo.c").write_text("int f(void) { return 7; }\n")
        (work/"broken.c").write_text("#error intentional compiler failure\n")
        (work/"input.s").write_text(".text\n.globl fixture_asm\nfixture_asm:\n.byte 0\n")
        (work/"args.rsp").write_text("-c 'source space.c' -o 'unit.o'\n")
        (work/"outer.rsp").write_text("@args.rsp\n")
        (work/"empty.rsp").write_text("'' -c unit.c\n")
        before = {os.fsencode(p.relative_to(work)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in work.rglob("*") if p.is_file()}
        extra = []
        # These are experimental options, not the chosen production backend.
        if mode == "saved":
            aux = work/".aux"
            aux.mkdir()
            extra = ["-save-temps=obj", "-dumpdir", str(aux)+"/"]
        elif mode == "jobs" and name not in ["preprocess","dependencies","syntax","version","assembler","user-save-temps","empty-response-arg","missing-response","lto","compile-failure"]:
            jobs = work/".jobs"
            jobs.mkdir()
            absolute_primary = os.path.abspath(os.fsencode(work)+b"/"+os.fsencode(primary)) if primary else b"/dev/null"
            configuration = dict(root=str(jobs),primary_hex=absolute_primary.hex())
            config_file = jobs/"config.json"
            config_file.write_text(json.dumps(configuration))
            wrapper = Path(os.environ["JOB_WRAPPER"])
            if family == "gcc":
                aux = work/".aux"
                aux.mkdir()
                extra = ["-save-temps=obj","-dumpdir",str(aux)+"/","-wrapper",f"{sys.executable},{wrapper},{config_file}"]
            else:
                real = subprocess.check_output([compiler,"-print-prog-name=clang"]).decode().strip()
                if not os.path.isabs(real):
                    raise RuntimeError("Clang driver did not identify an absolute executable")
                configuration["program"] = real
                config_file.write_text(json.dumps(configuration))
                proxy = jobs/"proxy"
                proxy.mkdir()
                launcher = proxy/"clang"
                launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {wrapper} {config_file} "$@"\n')
                launcher.chmod(0o755)
                real_ld = subprocess.check_output([compiler,"-print-prog-name=ld"]).decode().strip()
                ld_config = jobs/"ld.json"
                ld_config.write_text(json.dumps(dict(configuration,program=real_ld)))
                ld_launcher = proxy/"ld"
                ld_launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {wrapper} {ld_config} "$@"\n')
                ld_launcher.chmod(0o755)
                extra = ["-save-temps=obj","-fno-integrated-cc1","-ccc-install-dir",str(proxy),f"-fuse-ld={ld_launcher}","-gdwarf-4"]
        command = [compiler, *arguments, *extra]
        run = subprocess.run(command, cwd=work, input=source, capture_output=True, timeout=20)
        files = {os.fsencode(p.relative_to(work)).hex():dict(size=p.stat().st_size,
                     sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                 for p in work.rglob("*") if p.is_file() and os.fsencode(p.relative_to(work)) not in before}
        modified = [path.hex() for path, digest in before.items()
                    if not os.path.exists(os.fsencode(work)+b"/"+path)
                    or hashlib.sha256(Path(os.fsdecode(os.fsencode(work)+b"/"+path)).read_bytes()).hexdigest()!=digest]
        sections = {}
        if primary is not None:
            path = work / os.fsdecode(primary)
            if path.is_file():
                sections = section_hashes(path)
        result["cases"].append(dict(name=name, arguments=[os.fsencode(a).hex() for a in command[1:]],
              status=run.returncode, stdout=run.stdout[:4096].hex(), stderr=run.stderr[:4096].hex(),
              files=files, modified_inputs=modified, sections=sections))
    Path(output).write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")


if __name__ == "__main__":
    main()

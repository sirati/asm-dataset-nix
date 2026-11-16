"""Verify the measured Clang 3.4 accumulator-CMP reencoding on tiny fixtures.

LLVM's object emitter uses the accumulator imm32 form. Reassembling its text
can choose the equivalent imm8 form. Check instruction bytes and operands,
alignment NOPs, symbol sizes and FDE ranges; reject every other difference.
This is a fixture comparison, not a transformation of captured artifacts.
"""
import re
import struct
import subprocess


def _instructions(path):
    output=subprocess.check_output(["objdump","-w","-d","--section=.text",str(path)],timeout=5).decode("ascii","backslashreplace")
    result=[]
    for line in output.splitlines():
        match=re.match(r"\s*([0-9a-f]+):\s+((?:[0-9a-f]{2}\s+)+)\s*(.+)",line)
        if match:
            result.append((int(match[1],16),bytes.fromhex(match[2])," ".join(match[3].split())))
    return result


def _nop_padding(instructions,first,last):
    position=first
    for address,data,text in instructions:
        if not first<=address<last:continue
        assert address==position and address+len(data)<=last,"invalid alignment boundary"
        assert re.match(r"(?:(?:data16|cs|ds|ss|es|fs|gs) )*nop[lw]?\b",text) or text=="xchg %ax,%ax","non-NOP alignment bytes"
        position+=len(data)
    assert position==last,"incomplete alignment padding"


def _cmp_pair(before,after):
    if before[:1]==after[:1]==b"\x48":before,after=before[1:],after[1:]
    return (len(before)==5 and before[0]==0x3d and len(after)==3 and after[:2]==b"\x83\xf8"
        and int.from_bytes(before[1:],"little",signed=True)==int.from_bytes(after[2:],"little",signed=True))


def verify(before,after,capabilities):
    headers=[]
    for path in [before,after]:
        assert path.stat().st_size<=1024*1024,"encoding check requires a tiny ELF"
        with path.open("rb") as file:header=file.read(64)
        assert header[:6]==b"\x7fELF\x02\x01" and struct.unpack_from("<H",header,18)[0]==62,"encoding check requires x86-64"
        headers.append((header[:9],header[16:32],header[48:52]))
    assert headers[0]==headers[1],"ELF ABI or entrypoint changed"
    expected=capabilities.section_hashes(before,behavior=True)
    actual=capabilities.section_hashes(after,behavior=True)
    if expected==actual:return dict(kind="identical")
    changed={key for key in expected.keys()|actual.keys() if expected.get(key)!=actual.get(key)}
    assert changed<={".text",".eh_frame","symbols:"+b".symtab".hex()},changed
    assert expected[".text"][:-1]==actual[".text"][:-1] and expected[".text"][3]<=65536,"text layout changed"
    assert expected[".eh_frame"][:-1]==actual[".eh_frame"][:-1],"unwind layout changed"
    left=capabilities.section_hashes(before,behavior=True,symbol_records=True)
    right=capabilities.section_hashes(after,behavior=True,symbol_records=True)
    symbol_key="symbols:"+b".symtab".hex()
    left_symbols={tuple(entry[:-1]):entry[-1] for entry in left[symbol_key]}
    right_symbols={tuple(entry[:-1]):entry[-1] for entry in right[symbol_key]}
    assert left_symbols.keys()==right_symbols.keys(),"symbol identities or addresses changed"
    functions={}
    for identity,size in left_symbols.items():
        other_size=right_symbols[identity]
        if size==other_size:continue
        name,info,visibility,section,address=identity
        assert info&15==2 and section==b".text".hex() and size>other_size>0,"non-function symbol changed"
        assert address not in functions or functions[address][:2]==(size,other_size),"conflicting function aliases"
        functions[address]=(size,other_size,name)
    assert functions,"no supported encoding difference"
    first=capabilities.section_hashes(before,contents=True)
    second=capabilities.section_hashes(after,contents=True)
    before_text=bytes.fromhex(first[".text"]);canonical_text=bytearray.fromhex(second[".text"])
    left_code=_instructions(before);right_code=_instructions(after)
    base=expected[".text"][2]
    boundaries=sorted({entry[4] for entry in left[symbol_key] if entry[1]&15==2 and entry[3]==b".text".hex()}
        | {base+len(before_text)})
    evidence=[]
    for address,(size,other_size,name) in sorted(functions.items()):
        a=[entry for entry in left_code if address<=entry[0]<address+size]
        b=[entry for entry in right_code if address<=entry[0]<address+other_size]
        assert len(a)==len(b),"instruction count changed"
        for instructions,length in [(a,size),(b,other_size)]:
            position=address
            for start,data,_ in instructions:
                assert start==position,"incomplete function decoding"
                position+=len(data)
            assert position==address+length,"invalid function boundary"
        count=0
        for original,emitted in zip(a,b):
            assert original[2]==emitted[2],"instruction operands changed"
            if original[1]!=emitted[1]:
                assert _cmp_pair(original[1],emitted[1]),"unsupported instruction reencoding"
                count+=1
        assert count and size-other_size==count*2,"unexpected function size change"
        end=next(boundary for boundary in boundaries if boundary>=address+size)
        _nop_padding(left_code,address+size,end);_nop_padding(right_code,address+other_size,end)
        canonical_text[address-base:end-base]=before_text[address-base:end-base]
        evidence.append(dict(name_hex=name,address=address,original_size=size,captured_size=other_size,comparisons=count))
    assert canonical_text==before_text,"other text bytes changed"
    before_frame=bytes.fromhex(first[".eh_frame"]);frame=bytearray.fromhex(second[".eh_frame"])
    cursor=0;seen=set();frame_base=expected[".eh_frame"][2]
    while cursor+4<=len(frame):
        length=struct.unpack_from("<I",frame,cursor)[0]
        if length==0:break
        assert 8<=length<0xffffffff and cursor+length+4<=len(frame),"unsupported unwind record"
        pointer=struct.unpack_from("<I",frame,cursor+4)[0]
        if pointer:
            cie=cursor+4-pointer
            assert 0<=cie<cursor and frame[cie+8:cie+17]==b"\x01zR\0\x01\x78\x10\x01\x1b","unsupported unwind encoding"
            address=frame_base+cursor+8+struct.unpack_from("<i",frame,cursor+8)[0]
            if address in functions:
                size,other_size,_=functions[address]
                assert struct.unpack_from("<I",frame,cursor+12)[0]==other_size,"unwind range disagrees with symbol"
                struct.pack_into("<I",frame,cursor+12,size);seen.add(address)
        cursor+=length+4
    assert seen==set(functions) and frame==before_frame,"other unwind bytes changed"
    return dict(kind="x86-cmp-immediate-reencoding",functions=evidence)

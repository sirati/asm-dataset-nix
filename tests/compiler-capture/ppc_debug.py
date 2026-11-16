"""Check PowerPC scheduling changes caused by the compiler's debug policy.

Instruction operands and relocation targets stay fixed. Reordered instructions
must have no register, condition, memory or special-register dependency. The
legacy PPC32 PIC sequence also adjusts its literal and load displacement when
its local PC-materialization branch moves. No captured bytes are rewritten.
"""
from collections import Counter, defaultdict, deque
import struct


FP=0x7c3f0b78  # or r31,r1,r1, without condition-register side effects
PC_BRANCHES={0x48000005,0x429f0005}  # branch-and-link to the next instruction


def freeze(value):
    return tuple(map(freeze,value)) if isinstance(value,list) else value


def effects(word):
    op=word>>26;rt=(word>>21)&31;ra=(word>>16)&31;rb=(word>>11)&31
    r=lambda n:("r",n)
    reads=set();writes=set()
    if word in PC_BRANCHES:
        return set(),{"LR"}
    if word&0xfc1fffff==0x7c0802a6:  # mflr
        return {"LR"},{r(rt)}
    if word&0xfc1fffff==0x7c0803a6:  # mtlr
        return {r(rt)},{"LR"}
    if op in [14,15]:
        reads={r(ra)} if ra else set();writes={r(rt)}
    elif op in [8,12,13]:
        reads={r(ra)};writes={r(rt),"XER"}
        if op==13:writes.add("CR")
    elif op in [24,25,26,27,28,29]:
        reads={r(rt)};writes={r(ra)}
        if op in [28,29]:writes.add("CR")
    elif op in [10,11]:
        reads={r(ra)};writes={"CR"}
    elif op in [20,21,23,30]:
        reads={r(rt)};writes={r(ra)}
        if op in [20,30]:reads.add(r(ra))
        if op in [23,30]:reads.add(r(rb))  # Conservative for immediate MD forms.
        if word&1:writes.add("CR")
    elif op in [32,33,34,35,40,41,42,43,58]:
        reads={"memory"}|({r(ra)} if ra else set());writes={r(rt)}
        if op&1 or op==58 and word&3==1:writes.add(r(ra))
    elif op in [36,37,38,39,44,45,62]:
        reads={r(rt)}|({r(ra)} if ra else set());writes={"memory"}
        if op&1 or op==62 and word&3==1:writes.add(r(ra))
    elif op==31:
        xo=(word>>1)&1023
        if xo in [40,233,235,266]:
            reads={r(ra),r(rb)};writes={r(rt)}
        elif xo in [28,124,284,316,444,476,24,536,792]:
            reads={r(rt),r(rb)};writes={r(ra)}
        elif xo in [26,58,824,922,954,986]:
            reads={r(rt)};writes={r(ra)}
        elif xo in [136,138]:
            reads={r(ra),r(rb),"XER"};writes={r(rt),"XER"}
        elif xo in [0,32]:
            return {r(ra),r(rb)},{"CR"}
        else:return {"unknown"},{"unknown"}
        if word&1:writes.add("CR")
    else:return {"unknown"},{"unknown"}
    return reads,writes


def conflict(left,right):
    lr,lw=effects(left);rr,rw=effects(right)
    return "unknown" in lr|lw|rr|rw or bool(lw&(rr|rw) or rw&lr)


def check_lr_paths(code,first,start,end):
    """Every reachable path must overwrite LR before observing the local PC."""
    pending=[first];seen=set()
    while pending:
        pc=pending.pop()
        if pc in seen:continue
        if not start<=pc<end or pc%4:raise ValueError("PIC PC leaves the checked function")
        seen.add(pc);word=struct.unpack_from(">I",code,pc)[0];op=word>>26
        if op in [16,18]:
            if word&1:continue  # A call overwrites LR at this unchanged PC.
            if word&2:raise ValueError("unknown absolute PIC control flow")
            bits=16 if op==16 else 26
            displacement=word&((1<<bits)-4)
            if displacement&(1<<(bits-1)):displacement-=1<<bits
            pending.append(pc+displacement)
            if op==16:pending.append(pc+4)
            continue
        reads,writes=effects(word)
        if "LR" in writes:continue
        if "LR" in reads or "unknown" in reads|writes:
            raise ValueError("PIC PC escapes before LR restoration")
        pending.append(pc+4)


def pic_literals(code,start,end,relocations,*,pc_moves):
    """Normalize only a verified PPC32 local-PC/GOT materialization sequence."""
    loads={};literals={}
    for pc in range(start,end,4):
        word=struct.unpack_from(">I",code,pc)[0]
        if word not in PC_BRANCHES:continue
        if pc in relocations:raise ValueError("PIC branch has an external relocation")
        move=None
        for position in range(pc+4,min(pc+36,end-8),4):
            instruction=struct.unpack_from(">I",code,position)[0]
            if instruction==0x7fc802a6:
                move=position;break
            reads,writes=effects(instruction)
            if "LR" in reads|writes or "unknown" in reads|writes or instruction>>26 in [16,18,19]:break
        if move is None:raise ValueError("unknown PIC materialization")
        load_pc=move+4;add_pc=move+8
        load=struct.unpack_from(">I",code,load_pc)[0];register=(load>>21)&31
        if load>>26!=32 or (load>>16)&31!=30 or register in [1,30,31]:
            raise ValueError("unknown PIC literal load")
        displacement=struct.unpack(">h",struct.pack(">H",load&65535))[0]
        slot=pc+4+displacement
        relocation=relocations.get(slot)
        if slot<0 or slot>=start or slot%4 or relocation is None or relocation[1]!=26:
            raise ValueError("PIC literal has no matching relative relocation")
        if relocation[2][3]!=b".got2".hex():raise ValueError("PIC literal targets a different section")
        add=31<<26|30<<21|register<<16|30<<11|266<<1
        if struct.unpack_from(">I",code,add_pc)[0]!=add:raise ValueError("unknown PIC address addition")
        # The scratch register must be overwritten before it can escape.
        if pc_moves:
            if add_pc+8>end:raise ValueError("live PIC scratch register")
            reads,writes=effects(struct.unpack_from(">I",code,add_pc+4)[0])
            if ("r",register) not in writes or ("r",register) in reads or "unknown" in reads:
                raise ValueError("live PIC scratch register")
        # Moving the local branch changes LR. No later read may observe that
        # internal PC before the saved caller LR is restored.
        if pc_moves:check_lr_paths(code,add_pc+4,start,end)
        loads[load_pc]=(load&0xffff0000,slot)
        normalized=list(relocation);normalized[3]+=pc+4-slot
        literals[slot]=normalized
    return loads,literals


def check_function(before,after,start,end,left_relocations,right_relocations):
    pc_moves=([pc for pc in range(start,end,4) if struct.unpack_from(">I",before,pc)[0] in PC_BRANCHES]
        !=[pc for pc in range(start,end,4) if struct.unpack_from(">I",after,pc)[0] in PC_BRANCHES])
    left_loads,left_literals=pic_literals(before,start,end,left_relocations,pc_moves=pc_moves)
    right_loads,right_literals=pic_literals(after,start,end,right_relocations,pc_moves=pc_moves)
    if left_literals!=right_literals:raise ValueError("PIC relocation value changed")
    def blocks(code,relocations,loads):
        result=[];block=[]
        for pc in range(start,end,4):
            word=struct.unpack_from(">I",code,pc)[0]
            tags=[]
            for position in [pc,pc+2]:
                if position in relocations:
                    entry=relocations[position]
                    # Absolute and TOC-relative fixups can move with their
                    # instruction. Unknown/PC-relative fixups retain their place.
                    non_pc=entry[1] in [1,2,3,4,5,6,7,8,9,14,15,16,17,47,48,49,50,63,64]
                    tags.append((position-pc,freeze(entry[1:]),None if non_pc else position))
            token=(loads.get(pc,word),tuple(tags))
            if word>>26 in [16,18,19] and word not in PC_BRANCHES:
                result.append((block,(pc,token)));block=[]
            else:block.append((pc,word,token))
        if block:result.append((block,None))
        return result
    left=blocks(before,left_relocations,left_loads);right=blocks(after,right_relocations,right_loads)
    if len(left)!=len(right):raise ValueError("control flow changed")
    for (a,barrier_a),(b,barrier_b) in zip(left,right):
        if barrier_a!=barrier_b or Counter(row[2] for row in a)!=Counter(row[2] for row in b):
            raise ValueError("instructions, operands or control flow changed")
        positions=defaultdict(deque)
        for index,row in enumerate(b):positions[row[2]].append(index)
        order=[positions[row[2]].popleft() for row in a]
        for first in range(len(a)):
            for second in range(first+1,len(a)):
                if order[first]>order[second] and conflict(a[first][1],a[second][1]):
                    raise ValueError("dependent instructions changed order")
    return left_literals,right_literals


def verify(before,after,section_hashes,*,family,version):
    if family!="clang" or len(before)>1024**2 or len(after)>1024**2:return set()
    if len(before)<64 or len(after)<64 or before[:4]!=b"\x7fELF":return set()
    if before[5:7]!=b"\x02\x01" or before[:9]!=after[:9] or before[16:24]!=after[16:24]:return set()
    wide=before[4]==2
    machine=struct.unpack_from(">H",before,18)[0]
    if (machine,before[4]) not in [(20,1),(21,2)] or struct.unpack_from(">H",before,16)[0]!=1:return set()
    flag_offset=48 if wide else 36
    if before[flag_offset:flag_offset+4]!=after[flag_offset:flag_offset+4]:return set()
    try:
        left=section_hashes(None,raw=before,behavior=True,symbol_records=True,relocation_records=True)
        right=section_hashes(None,raw=after,behavior=True,symbol_records=True,relocation_records=True)
        raw_left=section_hashes(None,raw=before,contents=True);raw_right=section_hashes(None,raw=after,contents=True)
        if any(left[key]!=right.get(key) for key in left if key.startswith("symbols:")):return set()
        verified=set()
        for name,metadata in left.items():
            if not name.startswith(".") or not isinstance(metadata,tuple) or not metadata[1]&4 or metadata==right.get(name):continue
            if metadata[:-1]!=right.get(name,())[:-1]:return set()
            a=bytes.fromhex(raw_left[name]);b=bytes.fromhex(raw_right[name])
            if len(a)%4 or len(a)!=len(b):return set()
            relocation_names=[key[len("relocations:"):] for key,value in left.items()
                if key.startswith("relocations:") and value["target"]==name]
            left_relocations={};right_relocations={}
            for key in relocation_names:
                la=left["relocations:"+key]["entries"];rb=right.get("relocations:"+key,{}).get("entries",[])
                if len(la)!=len(rb):return set()
                left_relocations.update((row[0],row) for row in la)
                right_relocations.update((row[0],row) for row in rb)
            functions=sorted({(row[4],row[4]+row[5]) for key,rows in left.items() if key.startswith("symbols:")
                for row in rows if row[1]&15==2 and row[3]==name.encode().hex() and row[5]})
            covered=set();normalized_left={};normalized_right={}
            for start,end in functions:
                if start%4 or end>len(a) or end-start>1024:return set()
                if a[start:end]==b[start:end] and all(row==right_relocations.get(pos)
                        for pos,row in left_relocations.items() if start<=pos<end):continue
                if FP.to_bytes(4,"big") not in a[start:end]:return set()
                la,rb=check_function(a,b,start,end,left_relocations,right_relocations)
                normalized_left.update(la);normalized_right.update(rb);covered.update(range(start,end))
            if any(x!=y and index not in covered for index,(x,y) in enumerate(zip(a,b))):return set()
            for position,row in left_relocations.items():
                if position not in covered and normalized_left.get(position,row)!=normalized_right.get(position,right_relocations.get(position)):
                    return set()
            verified.add(name);verified.update(relocation_names)
        return verified
    except (ValueError,KeyError,IndexError,TypeError,struct.error):
        return set()

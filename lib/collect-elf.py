"""Collect installed ELF programs. Relocatable objects and debug-only ELFs
cannot enter the executable corpus. All path operations retain raw Unix bytes.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import struct
import sys


def is_elf_program(path):
    try:
        with open(path,"rb") as file:
            header = file.read(64)
            if len(header)<64 or header[:4]!=b"\x7fELF" or header[4] not in [1,2] or header[5] not in [1,2]:
                return False
            order = "<" if header[5]==1 else ">"
            if struct.unpack_from(order+"H",header,16)[0] not in [2,3]:
                return False
            if header[4]==2:
                offset = struct.unpack_from(order+"Q",header,32)[0]
                entry_size,count = struct.unpack_from(order+"HH",header,54)
                fmt = order+"IIQQQQQQ"
            else:
                offset = struct.unpack_from(order+"I",header,28)[0]
                entry_size,count = struct.unpack_from(order+"HH",header,42)
                fmt = order+"IIIIIIII"
            size = os.fstat(file.fileno()).st_size
            needed = struct.calcsize(fmt)
            if entry_size<needed or not 0<count<=4096 or offset+count*entry_size>size:
                return False
            for index in range(count):
                file.seek(offset+index*entry_size)
                fields = struct.unpack(fmt,file.read(needed))
                if header[4]==2:
                    kind,flags,start,_,_,length,_,_ = fields
                else:
                    kind,start,_,_,length,_,flags,_ = fields
                # Separate-debug files commonly retain ET_EXEC/ET_DYN and
                # program headers but have no file-backed executable segment.
                if kind==1 and flags&1 and length>0 and start+length<=size:
                    return True
    except (OSError,ValueError,struct.error):
        pass
    return False


def version_key(name):
    return tuple((1,int(part)) if part.isdigit() else (0,part)
                 for part in re.split(rb"(\d+)",name))


def collect(destination,sources):
    destination = os.fsencode(destination)
    os.makedirs(destination,exist_ok=True)
    entries = {}
    for source in sources:
        for directory,dirs,files in os.walk(os.fsencode(source)):
            for name in files + [d for d in dirs if os.path.islink(os.path.join(directory,d))]:
                path = os.path.join(directory,name)
                if not is_elf_program(path):
                    continue
                real = os.path.realpath(path)
                candidate = (not os.path.islink(path),version_key(name),name)
                if real not in entries or candidate>entries[real]:
                    entries[real] = candidate
    for real,(_,_,name) in sorted(entries.items()):
        target = os.path.join(destination,name)
        if os.path.lexists(target):
            prefix = hashlib.md5(real).hexdigest()[:8].encode()
            target = os.path.join(destination,prefix+b"_"+name)
            serial = 0
            while os.path.lexists(target):
                serial += 1
                target = os.path.join(destination,prefix+b"_"+str(serial).encode()+b"_"+name)
        os.symlink(real,target)
    return len(entries)


if __name__ == "__main__":
    print(f"Linked {collect(sys.argv[1],sys.argv[2:])} unique ELF program(s)")

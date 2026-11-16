import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("collect_elf",Path(__file__).parents[2]/"lib/collect-elf.py")
elf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(elf)


def fixture_elf(*,bits=64,order="<",kind=2,executable_bytes=4):
    header = bytearray(64)
    header[:7] = b"\x7fELF"+bytes([2 if bits==64 else 1,1 if order=="<" else 2,1])
    struct.pack_into(order+"H",header,16,kind)
    if bits==64:
        struct.pack_into(order+"Q",header,32,64)
        struct.pack_into(order+"HH",header,54,56,1)
        program = struct.pack(order+"IIQQQQQQ",1,5,120,0,0,executable_bytes,executable_bytes,1)
    else:
        struct.pack_into(order+"I",header,28,64)
        struct.pack_into(order+"HH",header,42,32,1)
        program = struct.pack(order+"IIIIIIII",1,96,0,0,executable_bytes,executable_bytes,5,1)
    return bytes(header)+program+b"\0"*executable_bytes


class ElfCollectorTests(unittest.TestCase):
    def test_types_and_endianness(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"fixture"
            for bits in [32,64]:
                for order in ["<",">"]:
                    for kind,expected in [(1,False),(2,True),(3,True),(4,False)]:
                        path.write_bytes(fixture_elf(bits=bits,order=order,kind=kind))
                        self.assertEqual(elf.is_elf_program(path),expected)
            path.write_bytes(fixture_elf(kind=3,executable_bytes=0))
            self.assertFalse(elf.is_elf_program(path))
            path.write_bytes(fixture_elf()[:-1])
            self.assertFalse(elf.is_elf_program(path))

    def test_aliases_collisions_and_raw_byte_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a,b,destination = root/"a",root/"b",root/"elf"
            a.mkdir(); b.mkdir()
            (a/"program").write_bytes(fixture_elf())
            (b/"program").write_bytes(fixture_elf())
            os.symlink(a/"program",a/"alias-9")
            os.symlink(a/"program",a/"alias-10")
            raw = os.fsencode(a)+b"/raw\xff\t\nname"
            with open(raw,"wb") as file:
                file.write(fixture_elf(kind=3))
            (a/"object.o").write_bytes(fixture_elf(kind=1))
            (a/"looks-executable.debug").write_bytes(fixture_elf(kind=2,executable_bytes=0))
            self.assertEqual(elf.collect(destination,[a,b]),3)
            names = os.listdir(os.fsencode(destination))
            self.assertIn(b"program",names)
            self.assertIn(b"raw\xff\t\nname",names)
            self.assertEqual(len([name for name in names if name.endswith(b"_program")]),1)
            self.assertNotIn(b"object.o",names)
            self.assertNotIn(b"looks-executable.debug",names)


if __name__ == "__main__":
    unittest.main()

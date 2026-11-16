"""Regression checks for the behavior comparator, independent of compiler tools."""
from pathlib import Path
import struct
import tempfile
import unittest

import capabilities


def riscv_object(padding=1, *, instruction=b"\x3b\x05\xa5\x02", executable=False, bad_padding=False):
    names=b"\0.text\0.rela.text\0.symtab\0.strtab\0.shstrtab\0.debug_info\0"
    text=(b"\xff\xff" if bad_padding else b"\x01\x00")*padding+instruction
    relocs=b"".join(struct.pack("<QQq",2*i,43,2) for i in range(padding))
    strings=b"\0square\0debug_label\0"
    symbols=b"\0"*24+struct.pack("<IBBHQQ",1,0x12,0,1,padding*2,len(instruction))
    # A weak symbol into a nonallocated debug section can move under -g.
    symbols+=struct.pack("<IBBHQQ",8,0x20,2,6,padding,0)
    values=[(b"",0,0,0,0,0,0),(text,1,6,0,0,4,0),
        (relocs,4,0x40,3,1,8,24),(symbols,2,0,4,1,8,24),
        (strings,3,0,0,0,1,0),(names,3,0,0,0,1,0),
        (b"debug"*padding,1,0,0,0,1,0)]
    data=bytearray(64);headers=[]
    for name,(value,kind,flags,link,info,align,entry) in zip(
            [b"",b".text",b".rela.text",b".symtab",b".strtab",b".shstrtab",b".debug_info"],values):
        offset=len(data);data+=value
        headers.append((names.find(name+b"\0") if name else 0,kind,flags,0,offset,len(value),link,info,align,entry))
    section_offset=len(data)
    for header in headers:data+=struct.pack("<IIQQQQIIQQ",*header)
    data[:16]=b"\x7fELF\x02\x01\x01"+b"\0"*9
    struct.pack_into("<HHIQQQIHHHHHH",data,16,2 if executable else 1,243,1,0,0,section_offset,0,64,0,0,64,len(headers),5)
    return bytes(data)


class BehaviorTests(unittest.TestCase):
    def test_i686_load_and_frame_motion_preserve_relocations(self):
        symbol=['values',0x11,0,'data',0,12]
        samples=[
            ('5589e58b0d000000008b45080fafc08d4408ff5dc3',
             '5589e58b45088b0d000000000fafc08d4408ff5dc3',5,8),
            ('5531c0833d000000000189e50f95c05dc3',
             '5589e531c0833d00000000010f95c05dc3',5,7)]
        for a,b,old,new in samples:
            with self.subTest(before=a):
                left=bytes.fromhex(a);right=bytes.fromhex(b)
                capabilities.x86_frame_map(left,right,[(0,len(left))],{old:[old,1,symbol,0]},{new:[new,1,symbol,0]})

    def test_i686_load_dependencies_and_relocation_changes_are_rejected(self):
        before=bytes.fromhex('5589e58b0d000000008b45080fafc08d4408ff5dc3')
        after=bytes.fromhex('5589e58b45088b0d000000000fafc08d4408ff5dc3')
        symbol=['values',0x11,0,'data',0,12]
        for field,value in [(0,9),(1,2),(2,['another-target']),(3,4)]:
            wrong=[8,1,symbol,0];wrong[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                capabilities.x86_frame_map(before,after,[(0,len(before))],{5:[5,1,symbol,0]},{8:wrong})
        # Both loads would now overwrite ECX; their order is significant.
        with self.assertRaises(ValueError):
            capabilities.x86_frame_map(before[:10]+b'\x4d'+before[11:],after[:4]+b'\x4d'+after[5:],
                [(0,len(before))],{5:[5,1,symbol,0]},{8:[8,1,symbol,0]})
        with self.assertRaises(ValueError):
            capabilities.x86_frame_map(before,after,[(0,len(before))],{5:[5,2,symbol,0]},{8:[8,2,symbol,0]})

    def aarch64_frame_example(self):
        sequence=[0xa9bf7bfd,0x90000008,0xb9400108,0x910003fd,
            0x1b002008,0x51000500,0xa8c17bfd,0xd65f03c0]
        changed=[sequence[0],sequence[3],sequence[1],sequence[2],*sequence[4:]]
        symbol=['values',0x11,0,'data',0,12]
        left={4:[4,275,symbol,0],8:[8,285,symbol,0]}
        right={8:[8,275,symbol,0],12:[12,285,symbol,0]}
        return sequence,changed,left,right

    def test_aarch64_frame_motion_preserves_address_load_relocations(self):
        before,after,left,right=self.aarch64_frame_example()
        capabilities.aarch64_frame_map(struct.pack('<8I',*before),struct.pack('<8I',*after),[(0,32)],left,right)
        for key,index in [(8,1),(8,2),(8,3),(12,0)]:
            wrong={position:list(row) for position,row in right.items()}
            wrong[key][index]=999 if index!=2 else ['another-target']
            with self.subTest(key=key,index=index),self.assertRaises(ValueError):
                capabilities.aarch64_frame_map(struct.pack('<8I',*before),struct.pack('<8I',*after),[(0,32)],left,wrong)

    def test_aarch64_frame_motion_rejects_frame_dependencies_and_changed_code(self):
        before,after,left,right=self.aarch64_frame_example()
        for index,word in [(1,0x9000001d),(2,0xb94003a8),(4,0xd503201f)]:
            a=list(before);b=list(after);a[index]=word
            if index in [1,2]:b[index+1]=word
            with self.subTest(index=index),self.assertRaises(ValueError):
                capabilities.aarch64_frame_map(struct.pack('<8I',*a),struct.pack('<8I',*b),[(0,32)],left,right)

    def test_aarch64_debug_swap_checks_each_operand_and_compiler_identity(self):
        header=bytearray(64);header[:7]=b"\x7fELF\x02\x01\x01";header[16:18]=b"\x01\x00";header[18:20]=b"\xb7\x00"
        move,frame=0x52800060,0x910003fd
        before=struct.pack("<III",move,frame,0xd65f03c0)
        after=struct.pack("<III",frame,move,0xd65f03c0)
        check=lambda a,b,**kw:capabilities.aarch64_debug_prologue_swap(a,b,header,header,
            family=kw.get("family","clang"),version=kw.get("version","10.0.1"))
        self.assertTrue(check(before,after))
        self.assertFalse(check(before,struct.pack("<III",frame,move+32,0xd65f03c0)))
        self.assertFalse(check(before,struct.pack("<III",frame,move,0xd503201f)))
        self.assertFalse(check(before,after,version="12.0.1"))
        self.assertFalse(check(before,after,family="gcc"))
        self.assertFalse(check(struct.pack("<II",move+1,frame),struct.pack("<II",frame,move+1)))
        logical_move=0x320007e0  # ORR W0, WZR, #3.
        for version in ['3.8.1','3.9.1','4.0.1','5.0.2','6.0.1','7.1.0','8.0.1']:
            self.assertTrue(check(struct.pack('<II',logical_move,frame),struct.pack('<II',frame,logical_move),version=version))
        self.assertFalse(check(struct.pack('<II',0x32000420,frame),struct.pack('<II',frame,0x32000420),version='3.8.1'))

    def test_verified_swap_still_requires_the_complete_debug_reference(self):
        before={".text":(1,6,0,8,4,"before"),".eh_frame":"original"}
        after={".text":(1,6,0,8,4,"after"),".eh_frame":"debug"}
        self.assertIsNone(capabilities.compatible_behavior(before,after,prologue_swap=True))
        self.assertEqual(capabilities.compatible_behavior(before,after,after,prologue_swap=True),"required-debug-aarch64-prologue")
        bad=dict(after, **{".rela.text":"changed-target"})
        self.assertIsNone(capabilities.compatible_behavior(before,bad,bad,prologue_swap=True))

    def test_unwind_changes_need_an_exact_debug_reference(self):
        before={".text":"code",".eh_frame":"original",".rela.eh_frame":"original-relocations"}
        after={".text":"code",".eh_frame":"debug",".rela.eh_frame":"debug-relocations"}
        self.assertIsNone(capabilities.compatible_behavior(before,after))
        self.assertEqual(capabilities.compatible_behavior(before,after,after),"required-debug-unwind")
        corrupt=dict(after, **{".eh_frame":"corrupt"})
        self.assertIsNone(capabilities.compatible_behavior(before,corrupt,after))

    def test_verified_ppc_schedule_can_keep_the_original_unwind_policy(self):
        before={".text":(1,6,0,8,4,"before"),".eh_frame":"original",".data":"data"}
        after=dict(before,**{".text":(1,6,0,8,4,"debug")})
        debug=dict(after,**{".eh_frame":"debug-frame"})
        self.assertEqual(capabilities.compatible_behavior(before,after,debug,ppc_sections={".text"}),
            "required-debug-ppc-scheduling")
        self.assertIsNone(capabilities.compatible_behavior(before,after,debug))
        for key in [".eh_frame",".data",".text"]:
            corrupt=dict(after,**{key:"corrupt"})
            self.assertIsNone(capabilities.compatible_behavior(before,corrupt,debug,ppc_sections={".text"}))

    def test_debug_reference_cannot_authorize_code_or_code_relocation_changes(self):
        before={".text":"code",".rela.text":"target",".eh_frame":"original"}
        for changed in [".text", ".rela.text"]:
            after=dict(before, **{changed:"changed",".eh_frame":"debug"})
            self.assertIsNone(capabilities.compatible_behavior(before,after,after))

    def compare(self,left,right):
        with tempfile.TemporaryDirectory() as temporary:
            paths=[Path(temporary)/name for name in ["left","right"]]
            for path,value in zip(paths,[left,right]):path.write_bytes(value)
            return [capabilities.section_hashes(path,behavior=True) for path in paths]

    def test_only_relocation_declared_nops_are_normalized(self):
        left,right=self.compare(riscv_object(1),riscv_object(2))
        self.assertEqual(left,right)

    def test_section_alignment_satisfies_a_leading_constraint(self):
        left,right=self.compare(riscv_object(0),riscv_object(1))
        self.assertEqual(left,right)

    def test_changed_instructions_are_rejected(self):
        left,right=self.compare(riscv_object(1),riscv_object(2,instruction=b"\x3b\x05\xb5\x02"))
        self.assertNotEqual(left,right)

    def test_padding_must_be_nops(self):
        with self.assertRaisesRegex(RuntimeError,"non-NOP"):
            self.compare(riscv_object(1),riscv_object(2,bad_padding=True))

    def test_final_executable_bytes_remain_strict(self):
        left,right=self.compare(riscv_object(1,executable=True),riscv_object(2,executable=True))
        self.assertNotEqual(left,right)


if __name__=="__main__":
    unittest.main()

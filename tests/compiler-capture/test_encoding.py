import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest

import capabilities
import encoding


@unittest.skipUnless(shutil.which("as") and shutil.which("ld"),"native binutils are required")
class EncodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=pathlib.Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def program(self,name,immediate=9,wide=False):
        instruction=f".byte 0x3d\n.long {immediate}" if wide else f"cmp ${immediate},%eax"
        source=f".text\n.globl main\n.type main,@function\nmain:\n.cfi_startproc\n{instruction}\nret\n.cfi_endproc\n.size main,.-main\n.p2align 4\n.globl other\n.type other,@function\nother:\nmov %eax,%eax\nret\n.size other,.-other\n"
        (self.root/(name+".s")).write_text(source)
        subprocess.run(["as","-mx86-used-note=no",name+".s","-o",name+".o"],cwd=self.root,check=True,capture_output=True)
        subprocess.run(["ld","-e","main",name+".o","-o",name],cwd=self.root,check=True,capture_output=True)
        return self.root/name

    def test_only_equivalent_cmp_encoding_and_padding_are_accepted(self):
        result=encoding.verify(self.program("before",wide=True),self.program("after"),capabilities)
        self.assertEqual(result["kind"],"x86-cmp-immediate-reencoding")
        self.assertEqual(result["functions"][0]["comparisons"],1)

    def test_changed_operand_is_rejected(self):
        with self.assertRaises(AssertionError):
            encoding.verify(self.program("before",wide=True),self.program("after",immediate=8),capabilities)

    def test_changed_entrypoint_is_rejected(self):
        before=self.program("before",wide=True);after=self.program("after")
        data=bytearray(after.read_bytes());entry=struct.unpack_from("<Q",data,24)[0]
        struct.pack_into("<Q",data,24,entry+16);after.write_bytes(data)
        with self.assertRaisesRegex(AssertionError,"entrypoint"):
            encoding.verify(before,after,capabilities)

    def test_corrupted_unwind_range_is_rejected(self):
        before=self.program("before",wide=True);after=self.program("after")
        frame=bytes.fromhex(capabilities.section_hashes(after,contents=True)[".eh_frame"])
        data=bytearray(after.read_bytes());offset=data.find(frame)
        record=struct.unpack_from("<I",frame,0)[0]+4
        length=struct.unpack_from("<I",frame,record+12)[0]
        struct.pack_into("<I",data,offset+record+12,length+1);after.write_bytes(data)
        with self.assertRaisesRegex(AssertionError,"unwind"):
            encoding.verify(before,after,capabilities)


if __name__=="__main__":unittest.main()

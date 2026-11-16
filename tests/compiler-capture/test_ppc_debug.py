import struct
import unittest

import ppc_debug


def words(*values):return b"".join(struct.pack(">I",value) for value in values)


class SchedulingTests(unittest.TestCase):
    def check(self,before,after,left=None,right=None):
        return ppc_debug.check_function(before,after,0,len(before),left or {},right or {})

    def test_independent_frame_setup_can_move(self):
        self.check(words(0x38600003,ppc_debug.FP,0x4e800020),words(ppc_debug.FP,0x38600003,0x4e800020))

    def test_register_dependencies_and_changed_operands_are_rejected(self):
        for before,after in [
                (words(0x3be00003,ppc_debug.FP),words(ppc_debug.FP,0x3be00003)),
                (words(0x3821fff0,ppc_debug.FP),words(ppc_debug.FP,0x3821fff0)),
                (words(0x38600003,ppc_debug.FP),words(ppc_debug.FP,0x38800003))]:
            with self.subTest(before=before.hex()),self.assertRaises(ValueError):self.check(before,after)

    def test_memory_order_and_control_flow_are_rejected(self):
        with self.assertRaises(ValueError):self.check(words(0x80640000,0x90a60000),words(0x90a60000,0x80640000))
        with self.assertRaises(ValueError):self.check(words(ppc_debug.FP,0x41820008),words(ppc_debug.FP,0x4182000c))

    def test_relocations_move_with_the_instruction_and_keep_the_target(self):
        symbol=[b"values".hex(),0x11,0,b".data".hex(),0,4]
        self.check(words(0x3c800000,ppc_debug.FP),words(ppc_debug.FP,0x3c800000),{2:[2,6,symbol,0]},{6:[6,6,symbol,0]})
        other=list(symbol);other[0]=b"other".hex()
        with self.assertRaises(ValueError):
            self.check(words(0x3c800000,ppc_debug.FP),words(ppc_debug.FP,0x3c800000),{2:[2,6,symbol,0]},{6:[6,6,other,0]})
        with self.assertRaises(ValueError):
            self.check(words(0x3c800000,ppc_debug.FP),words(ppc_debug.FP,0x3c800000),{2:[2,252,symbol,0]},{6:[6,252,symbol,0]})

    def test_unknown_instructions_cannot_be_reordered(self):
        with self.assertRaises(ValueError):self.check(words(0,ppc_debug.FP),words(ppc_debug.FP,0))

    def test_pic_pc_and_literal_adjustments_are_checked_together(self):
        sequence=[0,0x7c0802a6,0x93e1fffc,0x93c1fff8,0x90010004,0x9421fff0,
            ppc_debug.FP,0x7c6319d6,0x93df0008,0x48000005,0x7fc802a6,0x809effd8,
            0x7fc4f214,0x809e8000,0x83df0008,0x80840000,0x7c632214,0x3863ffff,
            0x38210010,0x80010004,0x83e1fffc,0x83c1fff8,0x7c0803a6,0x4e800020]
        changed=list(sequence);changed[7:10]=[sequence[8],sequence[9],sequence[7]];changed[11]=0x809effdc
        symbol=["",3,0,b".got2".hex(),0,0]
        left={0:[0,26,symbol,0x7fd8]};right={0:[0,26,symbol,0x7fdc]}
        ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)
        with self.assertRaises(ValueError):
            ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,{0:[0,26,symbol,0x7fdd]})
        changed[11]=0x809effd8
        with self.assertRaises(ValueError):
            ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)

    def test_pic_materialization_can_follow_multiple_independent_instructions(self):
        sequence=[0,0x7c0802a6,0x90010004,0x9421fff0,0x93e1000c,0x93c10008,
            0x48000005,0x7c6319d6,ppc_debug.FP,0x7fc802a6,0x809effe4,
            0x7fc4f214,0x809e8000,0x80840000,0x80010014,0x83e1000c,
            0x83c10008,0x7c632214,0x3863ffff,0x38210010,0x7c0803a6,0x4e800020]
        changed=list(sequence);changed[6:9]=[sequence[8],sequence[6],sequence[7]];changed[10]=0x809effe0
        symbol=["",3,0,b".got2".hex(),0,0]
        left={0:[0,26,symbol,0x7fe4]};right={0:[0,26,symbol,0x7fe0]}
        ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)
        # A different register reading the internal PC before mflr r30 would
        # expose the changed LR value even when the GOT arithmetic matches.
        sequence[7]=changed[8]=0x7ca802a6
        with self.assertRaises(ValueError):
            ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)

    def test_pic_lr_restoration_is_checked_on_both_conditional_paths(self):
        sequence=[0,0x7c0802a6,0x90010004,0x9421fff0,0x93e1000c,0x93c10008,
            0x48000005,0x7c6319d6,ppc_debug.FP,0x7fc802a6,0x809effe4,
            0x7fc4f214,0x809e8000,0x80840000,0x80010014,0x83e1000c,
            0x83c10008,0x7c632214,0x3863ffff,0x38210010,0x2c030001,
            0x38600000,0x41820008,0x38600001,0x7c0803a6,0x4e800020]
        changed=list(sequence);changed[6:9]=[sequence[8],sequence[6],sequence[7]];changed[10]=0x809effe0
        symbol=["",3,0,b".got2".hex(),0,0]
        left={0:[0,26,symbol,0x7fe4]};right={0:[0,26,symbol,0x7fe0]}
        ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)
        # One branch now skips mtlr and observes the internal PC in blr.
        sequence[22]=changed[22]=0x4182000c
        with self.assertRaises(ValueError):
            ppc_debug.check_function(words(*sequence),words(*changed),4,len(sequence)*4,left,right)


if __name__=="__main__":unittest.main()

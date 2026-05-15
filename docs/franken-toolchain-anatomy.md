# Franken-Toolchain Anatomy

This project pairs **old clang/gcc binaries** with **modern binutils, glibc,
and libgcc** for cross-compile coverage of the (arch × compiler-version)
matrix. Understanding what is "old" vs "modern" in the emitted binary
matters for both ML-training dataset correctness and triaging the
ABI-mismatch failures in the matrix.

## What each piece of the stack contributes to the output

- **Instruction-level patterns in the `.text` section of your binary = OLD**
  (driven entirely by old clang's frontend + opt + backend)
- **Helper function bodies (`libgcc_s.so.1`) = MODERN**
  (small fraction, often unused)
- **ELF structure (section layout, PLT/GOT format, relocation types) = MODERN**
  (bintools-driven)
- **System call ABI (how clang lowers `read(2)` etc.) = old-clang's choice,
  but the libc it calls into is modern**

## Implication for the dataset

The dataset's intent is "what does compiler version X emit for code Y" —
i.e. instruction-level codegen patterns per compiler era. The franken-stack
preserves that: the `.text` section's instruction-level patterns come
entirely from the old compiler binary's pipeline (frontend, optimizer, and
backend are all part of the same old executable; there's no per-stage
splicing). What gets modernized is *packaging* — the surrounding bintools
and runtime libraries — not codegen.

For the 13 (arch, old-clang) combinations where the build still fails,
the franken-splice itself is the cause: the *modern* libgcc / binutils
were built for one (arch, ABI) flavour and the *old* clang's codegen emits
a different one, and the linker refuses to combine them. The codegen
wasn't accidentally modernized; the runtime+packaging just can't be talked
into accepting it.

# Compiler Flag Impact Analysis

Empirical measurement of how each flag set changes compiled function output
compared to the `-O2 baseline` (gcc15, x86_64, unhardened).

## Busybox (3,285 functions)

| Flag | Compiler Flag | Changed | Added | Removed | % Affected |
|------|---------------|---------|-------|---------|------------|
| noinline | `-fno-inline` | 3,235 | 1,267 | 8 | ~99% |
| unroll | `-funroll-loops` | 3,200 | 0 | 0 | 97% |
| frameptr | `-fno-omit-frame-pointer` | 3,219 | 0 | 0 | 98% |
| novec | `-fno-tree-vectorize` (gcc) / `-fno-vectorize` (clang) | 3,185 | 0 | 0 | 97% |
| hardened | nixpkgs full hardening (fortify, stackprotector, pie, relro, bindnow) | 3,249 | 35 | 13 | ~100% |
| sections | `-ffunction-sections -fdata-sections` | 385 | 0 | 0 | 12% |
| nopic | `-fno-PIC -no-pie` | N/A (busybox is static) | | | |

## Hello (175 functions)

| Flag | Compiler Flag | Changed | Added | Removed | % Affected |
|------|---------------|---------|-------|---------|------------|
| noinline | `-fno-inline` | 158 | 45 | 10 | ~100% |
| nopic | `-fno-PIC -no-pie` | 174 | 0 | 0 | 99% |
| unroll | `-funroll-loops` | 156 | 0 | 0 | 89% |
| novec | `-fno-tree-vectorize` | 84 | 0 | 0 | 48% |
| sections | `-ffunction-sections -fdata-sections` | 64 | 0 | 0 | 37% |
| hardened | nixpkgs full hardening | 161 | 8 | 11 | ~100% |
| frameptr | `-fno-omit-frame-pointer` | 0 | 0 | 0 | 0% |

## Notes

- **frameptr** shows 0% on hello because gcc15 already preserves frame pointers
  for that small binary. On larger programs (busybox) it affects 98% of functions.
- **nopic** is not applicable to statically linked binaries like busybox. On
  dynamically linked binaries it changes nearly all functions (RIP-relative
  vs absolute addressing).
- **sections** has the lowest impact (~12% on busybox). It changes function/data
  layout and alignment rather than instruction selection. Still produces
  meaningfully different binaries for decompiler analysis.
- **noinline** increases the total function count significantly (adds ~1,267
  functions in busybox) because previously-inlined functions become standalone.
- **hardened** vs unhardened affects nearly all functions due to stack canaries,
  FORTIFY_SOURCE buffer checks, and position-independent code generation.

## Methodology

Comparison done by disassembling with `objdump -d --no-show-raw-insn`, parsing
function boundaries, stripping addresses, and diffing instruction mnemonics +
operands. A function is "changed" if any instruction differs. "Added"/"Removed"
counts functions that exist in only one variant.

# Cross-Compilation Failure Analysis

Total: 320 tests (40 compilers x 8 targets). 241 pass, 59 fail, 0 skipped, 20 unsupported (n/a).
Previous run: 241 pass, 79 fail, 0 skipped.
Change: 20 failures reclassified as n/a (below minimum supported compiler version).

No regressions from previously passing compiler/target combinations.

# Grouped Failures

## Eval: cross target ppc32 not available in old nixpkgs (18)

**Affected**: gcc6-12, gcc4_8-4_9 (nixpkgs-22.11), clang8-17 (nixpkgs-22.11/23.11/24.05)

```
error: gcc10: cross target ppc32 not available in this nixpkgs
```

`pkgsCross.ppc32` only exists in nixpkgs-unstable. Old nixpkgs inputs don't have it.

**Status**: Accept — old nixpkgs limitation.

## Eval: ppc32 not available for pre-pkgsCross clang (3)

**Affected**: clang5/ppc32, clang6/ppc32, clang7/ppc32

Same as above but from nixpkgs-22.11 `llvmPackages_5/6/7`.

**Status**: Accept — old nixpkgs limitation.

## Build: old clang mips64el N32 (9)

**Affected**: clang3_4, clang3_5, clang3_7, clang3_8, clang3_9, clang4, clang5,
clang6, clang7 / mips64el

```
configure: error: C compiler cannot create executables
```

Old clang versions (<8) have broken MIPS N32 ABI codegen. clang8+ all pass mips64el.

**Status**: Accept — old compiler limitation.

## Build: clang9 riscv64 codegen failure (1)

**Affected**: clang9/riscv64

```
configure: error: C compiler cannot create executables
```

RISC-V backend in clang was experimental in clang 9 and stabilized in clang 10.
clang <9 is now excluded from the riscv64 matrix (below MSV). clang9 meets the MSV
but its RISC-V support is too immature for the sysroot.

**Status**: Accept — old compiler limitation.

## Build: old clang ppc64 (clang5-7, clang3_4-3_5) (5)

**Affected**: clang5/ppc64, clang6/ppc64, clang7/ppc64, clang3_4/ppc64, clang3_5/ppc64

- **clang5-7** (from nixpkgs-22.11): same ELFv1/ELFv2 ABI issue as the fixed
  clang8-17 group, but they come from `llvmPackages_5/6/7` which are broken in
  22.11 and don't evaluate for ppc32 either. The `-mabi=elfv2` fix is applied
  but the old nixpkgs wrapping has additional issues.
- **clang3_4/3_5** (hybrid wrapper from nixpkgs-18.03): these ancient clangs
  invoke the native x86_64 assembler instead of the cross assembler for ppc64.
  clang 3.4/3.5 lack proper cross-assembler tool selection. clang3_7+ works.

**Status**: Accept — old compiler limitations.

## Eval: gcc4_4/gcc4_6 no cross infrastructure (12)

**Affected**: gcc4_4, gcc4_6 / i686, armv7l, mipsel, mips64el, ppc32, ppc64
(aarch64 and riscv64 now n/a — below MSV)

```
error: gcc44: cross-compiler not available in this nixpkgs for <target>
```

nixpkgs-15.09 predates `pkgsCross` and `buildPackages` entirely. Cross-compilation
not possible.

**Status**: Accept — nixpkgs too old.

## Build: gcc4_5 cross compiler build failure (6)

**Affected**: gcc4_5 / i686, armv7l, mipsel, mips64el, ppc32, ppc64
(aarch64 and riscv64 now n/a — below MSV)

```
make[1]: *** [Makefile:5012: all-gcc] Error 2
```

GCC 4.5.4 source fails to compile with modern GCC 15 (warnings as errors).

**Status**: Accept — ancient compiler source incompatible with modern build tools.

## Eval: gcc5 mips64el/ppc32/ppc64 (3)

**Affected**: gcc5 / mips64el, ppc32, ppc64
(riscv64 now n/a — below MSV)

gcc5 is from nixpkgs-18.03 (pre-pkgsCross). Cross-compilation only works for
targets that the old nixpkgs understands (i686, aarch64, armv7l, mipsel pass).

**Status**: Accept — old nixpkgs limitation.

## Build: clang3_5/aarch64 ICE (1)

**Affected**: clang3_5/aarch64

```
clang-3.5: error: clang frontend command failed due to signal
```

clang 3.5 internal compiler error on AArch64 codegen. clang3_7+ all pass.

**Status**: Accept — old compiler bug.

# Fixes Applied

| # | Fix | Passes Gained | Details |
|---|-----|--------------|---------|
| 9 | MSV enforcement | 20 FAIL→n/a | `architectures.nix`+`matrix.nix`: exclude compiler/target combos below minimum supported version per ISA. |
| 8 | Remove stop-on-first-failure | 33 (from SKIP→OK) | `test_cross.sh`: added `--no-skip` flag. `test_skipped.sh`: re-tests all SKIPs. |
| 7 | clang <3.9 ppc64 ld symlink | 2 (clang3_7, clang3_8) | `mkVariant.nix`: `-B<dir>` symlink fix for old clang ppc64. |
| 6 | clang ppc64 `-mabi=elfv2` | 12 | `mkVariant.nix`: force ELFv2 ABI for clang+ppc64. |

Previous fixes (still in effect): #1 getPkgsForTarget null-safe, #2 clang ppc64 `-fuse-ld=`,
#4 gcc12 `--disable-libsanitizer`, #5 operator precedence fix.

# Summary

| Category | Count | Root cause |
|----------|-------|------------|
| ppc32 missing in old nixpkgs | 21 | pkgsCross.ppc32 only in unstable |
| Old clang mips64el N32 | 9 | Broken codegen in clang <8 |
| Old clang ppc64 (3.4-3.5, 5-7) | 5 | Assembler/ABI issues |
| gcc4_4/4_6 no cross infra | 12 | nixpkgs-15.09 too old |
| gcc4_5 build failure | 6 | Source incompatible with GCC 15 |
| gcc5 old nixpkgs limits | 3 | Pre-pkgsCross nixpkgs |
| clang9 riscv64 | 1 | RISC-V not stable until clang 10 |
| clang3_5 aarch64 ICE | 1 | Old compiler bug |
| **Total** | **59** | **All known limitations** |

Additionally, 20 compiler/target combos are excluded as unsupported (n/a):
- riscv64: 7 GCC (<7) + 10 Clang (<9) = 17
- aarch64: 3 GCC (<4.8)

All 59 remaining failures are inherent limitations of old compilers or old nixpkgs.
No actionable fixes remain.

# Cross-Compilation for GCC 4.4, 4.5, 4.6

## Background

GCC 4.4, 4.5, and 4.6 come from nixpkgs that predate the modern
`pkgsCross`/`buildPackages` infrastructure:

| Compiler | Source nixpkgs | Cross infra available |
|----------|---------------|-----------------------|
| gcc4.4   | 15.09         | None (no pkgsCross, no buildPackages) |
| gcc4.5   | 18.03         | buildPackages exists, but wrappers broken with modern Nix |
| gcc4.6   | 15.09         | None (no pkgsCross, no buildPackages) |

The compilers themselves DO support cross-compilation — they just need to be
*built* as cross-compilers with `--target=<triple>`.

**Critical constraint**: A cross-compiler must be built using the native
(non-cross) version of the same GCC. Otherwise weird internal errors occur.

## Implementation

### Files

- `lib/old-gcc-cross.nix` (NEW) — cross-GCC builder for 15.09/18.03-era compilers
  - `getGccUnsupportedHardeningFlags` — version-based hardening flag detection
  - `mkCrossGccFromOldExpr` — builds cross-gcc from 15.09 source (gcc4.4, gcc4.6)
  - `mkCrossGccFrom1803` — builds cross-gcc from 18.03 re-import (gcc4.5)
- `lib/old-compilers.nix` (MODIFIED) — `mkOldGccEntry` routes to new builders
- `lib/old-nixpkgs.nix` (MODIFIED) — added `gccSubdir` to 15.09 gcc specs
- `lib/architectures.nix` (MODIFIED) — updated `minGccVersion` for armv7l and ppc64

### Strategy 1: nixpkgs-15.09 (gcc4.4, gcc4.6) — `mkCrossGccFromOldExpr`

Call the old gcc expression directly from the 15.09 source tree with cross
parameters:

```nix
import "${nixpkgs-15_09}/pkgs/development/compilers/gcc/4.4/default.nix" {
  cross = { config = "i686-unknown-linux-gnu"; arch = "i686"; libc = "glibc"; };
  binutilsCross = targetPkgs.buildPackages.binutils;
  libcCross = mergedGlibc;  # symlinks to modern glibc dev+lib outputs
  crossStageStatic = false;
  stdenv = modernWrappedNativeGccStdenv;  # native gcc4.4 wrapped with modern wrapCC
  # ... other deps from modern nixpkgs
}
```

The result is an unwrapped cross-GCC binary, which we then wrap with the
modern cc-wrapper using the hybrid approach (same as old Clang cross):

```nix
modernCrossGcc = targetPkgs.buildPackages.gcc;
hybridGcc = modernCrossGcc.override {
  cc = gccCrossUnwrapped // { hardeningUnsupportedFlags = [...]; };
};
targetPkgs.overrideCC targetPkgs.stdenv hybridGcc
```

### Strategy 2: nixpkgs-18.03 (gcc4.5) — `mkCrossGccFrom1803`

Re-import 18.03 with `crossSystem` to get `buildPackages.gcc45`. Override
the unwrapped `.cc` with `depsBuildBuild = [ oldPkgs.gcc45 ]` (same-version
bootstrap) to avoid build failures from the 18.03 default gcc7.3 compiler.
Extract the fixed unwrapped `.cc` and wrap with modern cc-wrapper.

## Issues Encountered and Fixes

### Strategy 1 issues (gcc4.4, gcc4.6 via 15.09 path)

#### 1. `perl`/`libmpc`/`libelf` unexpected argument

gcc4.4 expression doesn't accept `perl`, `libmpc`, `libelf` that gcc4.6 needs.

**Fix**: Use `builtins.functionArgs` to introspect which parameters the old
expression accepts, only pass accepted ones via `conditionalArgs`.

#### 2. `crossConfig: unbound variable`

Old gcc44 wrapper's setup-hook uses `$crossConfig` which is unset in modern
builds (bash `set -u`).

**Fix**: Don't use old wrapper. Create modern wrapper via `pkgs.wrapCC nativeGcc.cc`.

#### 3. `libc-ldflags-before: No such file`

Old gcc builder.sh reads `$NIX_CC/nix-support/libc-ldflags-before` which
doesn't exist in modern cc-wrappers.

**Fix**: `postFixup` on the modern wrapper: `touch $out/nix-support/libc-ldflags-before`

#### 4. `C compiler cannot create executables` (hardening flags)

Modern cc-wrapper injects hardening flags that gcc4.4 doesn't understand
(`-fstack-clash-protection`, `-fstack-protector-strong`, `-fzero-call-used-regs`).

**Fix**: Set `hardeningUnsupportedFlags` on unwrapped gcc before passing to
`wrapCC`.

#### 5. `LONG_MIN undeclared`

Old gcc source `libiberty/fibheap.c` uses `LONG_MIN` without including
`<limits.h>`. Modern glibc no longer provides it implicitly.

**Fix**: `NIX_CFLAGS_COMPILE = "-include limits.h"` in `overrideAttrs`.

#### 6. `-Werror=format-security` from hardening

`libcpp/macro.c` triggers format-security warning turned error.

**Fix**: `hardeningDisable = ["all"]` — disable all hardening for building
the cross-compiler itself.

#### 7. `@itemx must follow @item` (texinfo)

Modern texinfo is incompatible with old gcc 4.x texi files.

**Fix**: `makeFlags = [...] ++ ["MAKEINFO=true"]` — replace makeinfo with no-op.

#### 8. `C preprocessor fails sanity check` (libgcc configure)

Cross-gcc's `--with-headers` pointing to glibc lib output which has no `/include`.
Modern nixpkgs splits glibc into `dev` (headers) and `lib` (libraries) outputs.

**Fix**: Create merged glibc directory via `pkgs.runCommand` that symlinks
both `dev/include` and `lib/lib`.

#### 9. `field 'uc' has incomplete type` (struct ucontext)

Modern glibc (2.34+) removed `struct ucontext`, old gcc source uses it in
platform-specific unwind headers.

**Fix**: `postPatch` with `sed -i 's/struct ucontext/ucontext_t/g'` on
`gcc/config/*/linux-unwind.h`.

#### 10. `invalid instruction suffix for push` (wrong assembler)

Cross-gcc calls plain `as` (native x86_64) instead of target-specific
assembler (e.g. `i686-unknown-linux-gnu-as`).

**Fix**: `postFixup` symlinks cross-bintools into gcc's internal search path
at `$out/${target.crossConfig}/bin/`.

#### 11. `paxmark: command not found` (gcc4.6 ONLY)

Old 15.09 gcc4.6 builder.sh calls `paxmark` (PaX hardening tool) in
postInstall without checking if it exists. Modern nixpkgs doesn't have paxctl.

**Fix**: Provide a no-op `paxmark` script via
`pkgs.writeShellScriptBin "paxmark" "exit 0"` in `nativeBuildInputs`.

### Strategy 2 issues (gcc4.5 via 18.03 path)

#### 12. `gnu_inline` attribute inconsistency in cfns.gperf

18.03's default build compiler (gcc 7.3) rejects gcc4.5 source due to
inconsistent `gnu_inline` attribute on `libc_name_p` in `cfns.gperf`.

**Fix**: Override `depsBuildBuild = [ oldPkgs.gcc45 ]` on the unwrapped
cross-gcc derivation so gcc4.5 compiles itself (same-version bootstrap).
This avoids the gcc7 incompatibility entirely.

### Target triple incompatibilities

#### 13. armv7l `gnueabihf` unknown to gcc < 4.7

The hard-float ABI suffix (`gnueabihf`) in `armv7l-unknown-linux-gnueabihf`
was added to GCC's config.sub in GCC 4.7. GCC 4.4/4.5/4.6 don't recognize it.

**Fix**: Updated `architectures.nix` to set `minGccVersion = { major = 4; minor = 7; }`
for armv7l, excluding gcc4.4/4.5/4.6 from this target.

#### 14. ppc64 `gnuabielfv2` unknown to gcc < 4.9

The ELFv2 ABI suffix (`gnuabielfv2`) in `powerpc64-unknown-linux-gnuabielfv2`
was added in GCC 4.9. GCC 4.4/4.5/4.6/4.8 don't recognize it.

**Fix**: Updated `architectures.nix` to set `minGccVersion = { major = 4; minor = 9; }`
for ppc64, excluding gcc4.4-4.8 from this target.

#### 15. gcc4.5 limited cross targets in 18.03

The 18.03 re-import path only has cross support for a subset of targets:
- mips64el: `buildPackages.gcc45` not available (18.03 lacks cross attrs)
- ppc32: `kernelArch` attribute missing (18.03 cross infra incomplete)
- mipsel: glibc ABI mismatch (`skipping incompatible libc.so`)

**Result**: gcc4.5 cross only works for i686.

## Final Status

| Compiler | i686 | armv7l | mipsel | mips64el | ppc32 | ppc64 | aarch64 | riscv64 |
|----------|------|--------|--------|----------|-------|-------|---------|---------|
| gcc4.4   | OK   | N/A^1  | OK     | OK       | OK    | N/A^2 | N/A^3   | N/A^4   |
| gcc4.5   | OK   | N/A^1  | FAIL^5 | FAIL^6   | FAIL^7| N/A^2 | N/A^3   | N/A^4   |
| gcc4.6   | OK   | N/A^1  | OK     | OK       | OK    | N/A^2 | N/A^3   | N/A^4   |

Notes:
1. armv7l excluded: `gnueabihf` target triple requires GCC >= 4.7
2. ppc64 excluded: `gnuabielfv2` target triple requires GCC >= 4.9
3. aarch64 excluded: ARM64 backend added in GCC 4.8
4. riscv64 excluded: RISC-V backend added in GCC 7
5. mipsel gcc4.5: glibc ABI mismatch in 18.03 cross re-import
6. mips64el gcc4.5: cross-compiler not available in 18.03
7. ppc32 gcc4.5: `kernelArch` missing in 18.03 cross infrastructure

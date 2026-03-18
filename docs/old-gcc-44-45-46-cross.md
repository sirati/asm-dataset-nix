# Cross-Compilation for GCC 4.4, 4.5, 4.6

## Background

GCC 4.4, 4.5, and 4.6 come from nixpkgs that predate the modern
`pkgsCross`/`buildPackages` infrastructure:

| Compiler | Source nixpkgs | Cross infra available |
|----------|---------------|-----------------------|
| gcc4.4   | 15.09         | None (no pkgsCross, no buildPackages) |
| gcc4.5   | 15.09         | None (no pkgsCross, no buildPackages) |
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
  - `mkCrossGccFrom1803` — builds cross-gcc from 18.03 re-import (gcc5)
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

### Strategy 2 (gcc4.5): Now also uses Strategy 1

gcc4.5 was originally sourced from nixpkgs-18.03, but its gcc expression
uses the modern `buildPlatform`/`hostPlatform`/`targetPlatform` interface
and 18.03's default gcc7.3 couldn't compile gcc4.5 source (`cfns.gperf`
`gnu_inline` attribute error). Since nixpkgs-15.09 also contains gcc4.5
with the old-style `cross`/`binutilsCross` interface, gcc4.5 was moved
to the 15.09 group and now uses `mkCrossGccFromOldExpr` — identical to
gcc4.4 and gcc4.6. This gives gcc4.5 full cross-target support.

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

### Target triple incompatibilities

#### 13. armv7l `gnueabihf` unknown to gcc < 4.7

The hard-float ABI suffix (`gnueabihf`) in `armv7l-unknown-linux-gnueabihf`
was added to GCC's config.sub in GCC 4.7. GCC 4.4/4.5/4.6 don't recognize it.

**Fix**: Updated `architectures.nix` to set `minGccVersion = { major = 4; minor = 7; }`
for armv7l, excluding gcc4.4/4.5/4.6 from this target.

#### 14. ppc64 `gnuabielfv2` unknown to gcc <= 4.6 (old-expression path)

The ELFv2 ABI suffix (`gnuabielfv2`) in `powerpc64-unknown-linux-gnuabielfv2`
is not recognized by the old GCC expressions from nixpkgs-15.09 (gcc4.4/4.5/4.6).
GCC 4.8+ from nixpkgs-22.11 works fine via `pkgsCross` (the cross infrastructure
handles the triple without the GCC source needing to parse it directly).

**Fix**: Updated `architectures.nix` to set `minGccVersion = { major = 4; minor = 8; }`
for ppc64, excluding gcc4.4-4.6 from this target while keeping gcc4.8+ working.

## Final Status

All three compilers have identical cross-target support:

| Compiler | i686 | mipsel | mips64el | ppc32 |
|----------|------|--------|----------|-------|
| gcc4.4   | OK   | OK     | OK       | OK    |
| gcc4.5   | OK   | OK     | OK       | OK    |
| gcc4.6   | OK   | OK     | OK       | OK    |

Excluded targets (via `minGccVersion` in `architectures.nix`):
- **armv7l**: `gnueabihf` target triple requires GCC >= 4.7
- **ppc64**: `gnuabielfv2` triple unknown to old-expression path (GCC <= 4.6); minGccVersion = 4.8
- **aarch64**: ARM64 backend added in GCC 4.8
- **riscv64**: RISC-V backend added in GCC 7

# Cross-Compilation Failure Analysis

Total: 320 tests (40 compilers x 8 targets). 196 pass, 32 fail, 92 skipped.
Previous run: 181 pass, 37 fail, 102 skipped.
Improvement: +15 passes, -5 failures, -10 skipped.

No regressions from previously passing compiler/target combinations.

# Grouped Failures

## Eval: cross target ppc32 not available in this nixpkgs (18)

**Affected**: gcc6-12, gcc4_8-4_9 (nixpkgs-22.11), clang8-17 (nixpkgs-22.11/23.11/24.05)

```
error: gcc10: cross target ppc32 not available in this nixpkgs
```

`pkgsCross.ppc32` only exists in nixpkgs-unstable. Old nixpkgs inputs don't have it.
`getPkgsForTarget` now returns `null` and `mkStdenv` throws a descriptive error instead
of the raw `attribute 'ppc32' missing`.

**Status**: Known limitation. Accept — old nixpkgs simply don't define ppc32.

## Build failure: old clang mips64el N32 (8)

**Affected**: clang3_4/mips64el clang3_7/mips64el clang3_8/mips64el clang3_9/mips64el
clang4/mips64el clang5/mips64el clang6/mips64el clang7/mips64el

```
configure: C compiler cannot create executables
checking for mips64el-unknown-linux-gnuabin32-gcc... mips64el-unknown-linux-gnuabin32-clang
```

Old clang versions (<8) have broken MIPS N32 ABI support. clang8+ all pass mips64el.

**Status**: Known limitation. Accept — old compiler bug.

## Eval: gcc4_4/gcc4_6 cross not available (2)

**Affected**: gcc4_4/i686 gcc4_6/i686

```
error: gcc44: cross-compiler not available in this nixpkgs for i686
```

nixpkgs 15.09 predates `pkgsCross` and `buildPackages` entirely. Cross-compilation
not possible for these ancient compilers.

**Status**: Known limitation. Accept — nixpkgs too old.

## Build failure: gcc4_5 cross compiler fails to build (1)

**Affected**: gcc4_5/i686

```
make[1]: *** [Makefile:5012: all-gcc] Error 2
```

GCC 4.5.4 source fails to compile with modern GCC 15 (warnings treated as errors).
The eval fix (#5) works — this is a build-time failure of the cross compiler itself.

**Status**: Known limitation. Accept — ancient compiler source incompatible with modern build tools.

## Eval: gcc5 cross not available for mips64el (1)

**Affected**: gcc5/mips64el

```
error: gcc5: cross-compiler not available in this nixpkgs for mips64el
```

gcc5 is from nixpkgs-18.03 (pre-pkgsCross). The re-import with crossSystem for
mips64el (gnuabin32) is not understood by that old nixpkgs.

**Note**: gcc5 now passes i686, aarch64, armv7l, mipsel (4 new passes from fix #5).
Previously it failed at i686 due to the eval bug, hiding all subsequent targets.

**Status**: Known limitation. Accept.

## Compiler crash (ICE): clang3_5/aarch64 (1)

**Affected**: clang3_5/aarch64

```
clang-3.5: error: clang frontend command failed due to signal (use -v to see invocation)
clang version 3.5.2 (tags/RELEASE_352/final)
Target: aarch64-unknown-linux-gnu
```

clang 3.5 was the first release with AArch64 support and has known codegen bugs.

**Status**: Known limitation. Accept — old compiler bug.

## Eval: gcc12/ppc32 not available (1)

**Affected**: gcc12/ppc32

Same as the ppc32 group above. gcc12 is from nixpkgs-22.11 which lacks `pkgsCross.ppc32`.

Note: gcc12/mips64el (previously failing due to libsanitizer) now passes (fix #4).
gcc12 stops at ppc32 instead of mips64el.

# Fixes Applied This Round

| # | Fix | Failures Fixed | Details |
|---|-----|---------------|---------|
| 1 | `getPkgsForTarget` null-safe | 0 (18 improved errors) | `architectures.nix`: check `?` before access, return `null`. Old errors were raw `attribute 'ppc32' missing`, now descriptive. |
| 2 | clang ppc64 `-fuse-ld=` | 5 | `mkVariant.nix`: `overrideAttrs` + `postFixup` to append `-fuse-ld=<full-ld-path>` to `cc-cflags`. Fixes clang's triple normalization dropping `abielfv2`. |
| 4 | gcc12 `--disable-libsanitizer` | 1 | `old-compilers.nix`: add `--disable-libsanitizer` to `configureFlags` for old cross GCC. Fixes struct stat mismatch. |
| 5 | Operator precedence `?` vs `&&` | 1 (+4 unlocked) | `old-compilers.nix`: `(oldCrossPkgs ? buildPackages) && ... != ""` + check `crossAvailable.value`. gcc5 now passes 4 more targets. |

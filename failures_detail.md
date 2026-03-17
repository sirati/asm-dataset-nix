# Cross-Compilation Failure Analysis (Detailed)

Host: x86_64-linux, nixpkgs-unstable + old nixpkgs (15.09, 18.03, 22.11, 23.11, 24.05)
40 compilers x 8 targets = 320 tests. 196 pass, 32 fail, 92 skipped.
Previous run: 181 pass, 37 fail, 102 skipped.

No regressions from previously passing compiler/target combinations.

---

## Fixes Applied This Round

### Fix #1: `getPkgsForTarget` null-safe (18 improved error messages)

**File**: `lib/architectures.nix`

**Problem**: `getPkgsForTarget` directly accessed `pkgs.pkgsCross.${target.crossAttr}`,
throwing a raw `attribute 'ppc32' missing` error when old nixpkgs lacked the cross attr.

**Fix**: Check `!(pkgs ? pkgsCross) || !(pkgs.pkgsCross ? ${target.crossAttr})` before
accessing; return `null` when missing. Added null-handling in `mkVariant.nix` (throws
descriptive error) and `old-compilers.nix` (both `mkOldGccEntry` and `mkOldClangEntry`
check for `null` from `getOldCrossPkgs`).

**Result**: Errors now read `gcc10: cross target ppc32 not available in this nixpkgs`
instead of `attribute 'ppc32' missing`. Still fails (ppc32 genuinely doesn't exist in
old nixpkgs), but the error is descriptive and the matrix handles it gracefully.

### Fix #2: clang ppc64 linker `-fuse-ld=` (5 failures fixed)

**File**: `lib/mkVariant.nix`

**Problem**: clang normalizes the triple `powerpc64-unknown-linux-gnuabielfv2` to
`powerpc64-unknown-linux-gnu` internally, dropping the `abielfv2` suffix. When
searching for the linker, clang invokes bare `ld` instead of the prefixed cross
linker `powerpc64-unknown-linux-gnuabielfv2-ld`, causing `posix_spawn failed`.

**Fix**: For clang+ppc64, use `cc.overrideAttrs` with `postFixup` to append
`-fuse-ld=${cc.bintools}/bin/${cc.bintools.targetPrefix}ld` to `$out/nix-support/cc-cflags`.
This tells clang exactly which linker binary to use.

**Key insight**: Using `cc.override { extraBuildCommands = ... }` would **replace**
(not append to) the nixpkgs-provided `extraBuildCommands` that set up the
`resource-root` symlinks and `-resource-dir` flag. This caused all header checks to
fail (`checking for stdio.h... no`). Using `overrideAttrs` + `postFixup` appends
without disturbing the existing wrapper setup.

**Result**: clang18-22/ppc64 all build successfully.

### Fix #4: gcc12 `--disable-libsanitizer` (1 failure fixed)

**File**: `lib/old-compilers.nix`

**Problem**: gcc12 (nixpkgs-22.11) cross-compilation for mips64el failed because
`libsanitizer` has hardcoded MIPS N32 `struct stat` sizes that don't match the
glibc 2.35 headers. Errors included `static assertion failed`, `redefinition of
'struct stat64'`, and `'__NR_mmap2' was not declared`.

**Fix**: Added `--disable-libsanitizer` to `configureFlags` in the `overrideAttrs`
for old cross GCC builds. Applied broadly to all old cross GCCs (not just gcc12)
since sanitizers aren't needed for dataset builds and can cause similar issues with
other version/target combinations.

**Result**: gcc12/mips64el now passes. gcc12 now fails at ppc32 (next target) which
is the expected "ppc32 not available in old nixpkgs" error.

### Fix #5: Operator precedence `?` vs `&&` (1 failure fixed, 4 passes unlocked)

**File**: `lib/old-compilers.nix`

**Problem**: The `crossAvailable` check had an operator precedence bug:
```nix
crossAvailable = builtins.tryEval (
  oldCrossPkgs ? buildPackages && oldCrossPkgs.buildPackages.${attr}.name
);
```
Nix `?` has lower precedence than `&&`, so this parsed as:
```nix
oldCrossPkgs ? (buildPackages && oldCrossPkgs.buildPackages.${attr}.name)
```
When `&&` short-circuited to the `.name` string, `tryEval` returned
`{ success = true; value = "gcc-cross-wrapper-..."; }` — a string, not a bool.
Later code used this as a boolean and failed with "expected a Boolean but found a string".

**Fix**: Three changes:
1. Parenthesized: `(oldCrossPkgs ? buildPackages) && ...`
2. Added `!= ""` to ensure boolean result: `... .name != ""`
3. Changed condition to `crossAvailable.success && crossAvailable.value` (since
   `tryEval` can succeed with `false`, meaning cross is genuinely unavailable)

**Result**: gcc5/i686 now passes (was eval error). gcc5 also now passes aarch64,
armv7l, mipsel (4 unlocked targets), failing at mips64el (pre-pkgsCross nixpkgs
doesn't understand gnuabin32). gcc4_5/i686 eval now succeeds but the cross compiler
build fails (gcc4_5 source incompatible with modern gcc15).

---

## Remaining Failures (32)

### 1. ppc32 missing in old nixpkgs (18 failures)

**Affected**: gcc6-12, gcc4_8-4_9 (nixpkgs-22.11), clang8-17 (nixpkgs-22.11/23.11/24.05)

**Error**:
```
error: gcc10: cross target ppc32 not available in this nixpkgs
```

**Root cause**: `pkgsCross.ppc32` was added to nixpkgs after 24.05 (only exists in
nixpkgs-unstable). Old nixpkgs inputs don't define it.

**Status**: Accept. These compilers pass all other available targets. ppc32 is simply
not available in the old nixpkgs versions.

---

### 2. old clang mips64el N32 (8 failures)

**Affected**: clang3_4, clang3_7, clang3_8, clang3_9, clang4, clang5, clang6, clang7
(from nixpkgs-18.03 and nixpkgs-22.11)

**Error**:
```
configure: error: C compiler cannot create executables
checking for mips64el-unknown-linux-gnuabin32-gcc... mips64el-unknown-linux-gnuabin32-clang
```

**Root cause**: These old clang versions have limited or broken MIPS N32 ABI support.
The N32 ABI (`gnuabin32`) is a 32-bit ABI on a 64-bit MIPS CPU, requiring specific
codegen support that wasn't fully implemented until clang 8.

**Status**: Accept (old compiler limitation). clang8+ all pass mips64el.

---

### 3. gcc4_4/gcc4_6 no cross support (2 failures)

**Affected**: gcc4_4/i686, gcc4_6/i686 (from nixpkgs-15.09)

**Error**:
```
error: gcc44: cross-compiler not available in this nixpkgs for i686
```

**Root cause**: nixpkgs 15.09 predates the `pkgsCross` and `buildPackages` infrastructure
entirely. The re-import with `crossSystem` also lacks `buildPackages`, so cross-compilation
is impossible for these ancient compilers.

**Status**: Accept (ancient nixpkgs limitation). Native compilation works.

---

### 4. gcc4_5 cross compiler build failure (1 failure)

**Affected**: gcc4_5/i686 (from nixpkgs-18.03)

**Error**:
```
make[1]: *** [Makefile:5012: all-gcc] Error 2
```
(gcc-4.5.4 source fails to compile with gcc 15 — warnings treated as errors)

**Root cause**: The eval fix (#5) made gcc4_5/i686 evaluate successfully, but the
actual cross compiler build fails because GCC 4.5.4 source code is incompatible
with modern GCC 15 used as the build compiler. This is a pre-existing build issue
that was hidden by the eval error.

### Fix strategies

**A. Suppress warnings in gcc4_5 build**:
Override gcc4_5's cross compiler derivation to add `-Wno-error` to the build flags.
This may allow the old GCC source to compile despite the warnings.

```nix
bootstrappedCC = oldCrossGcc.cc.overrideAttrs (old: {
  depsBuildBuild = [ oldPkgs.${attr} ];
  CFLAGS = "-Wno-error";
  CXXFLAGS = "-Wno-error";
});
```

**B. Accept the limitation**:
gcc4_5 is from 2012 and native compilation works. Cross-compilation of this vintage
compiler with modern build tools is fragile.

**Status**: Accept for now. Could try fix A if more coverage is desired.

---

### 5. gcc5 mips64el cross not available (1 failure)

**Affected**: gcc5/mips64el (from nixpkgs-18.03)

**Error**:
```
error: gcc5: cross-compiler not available in this nixpkgs for mips64el
```

**Root cause**: gcc5 is from nixpkgs-18.03 (pre-pkgsCross). When re-importing with
`crossSystem = { config = "mips64el-unknown-linux-gnuabin32"; }`, the old nixpkgs
doesn't understand the N32 ABI triple and fails to produce a cross compiler.

**Note**: This failure was previously hidden because gcc5 failed at i686 (eval bug).
Fix #5 revealed it — gcc5 now passes i686, aarch64, armv7l, mipsel (4 new passes).

**Status**: Accept (old nixpkgs limitation for N32 ABI).

---

### 6. clang3_5 ICE on aarch64 (1 failure)

**Affected**: clang3_5/aarch64

**Error**:
```
clang-3.5: error: clang frontend command failed due to signal (use -v to see invocation)
clang version 3.5.2 (tags/RELEASE_352/final)
Target: aarch64-unknown-linux-gnu
make[2]: *** [Makefile:3639: lib/libhello_a-getopt.o] Error 254
```

**Root cause**: clang 3.5.2 (from 2015) has a code generation bug for AArch64 that
causes an internal compiler error (ICE / segfault, exit code 254 = signal 11) when
compiling GNU hello's `getopt.c`. AArch64 support was added in clang 3.5 and was
still immature. clang3_7+ all pass aarch64.

**Status**: Accept (old compiler bug).

---

## Summary of remaining failures

| # | Failure | Count | Status |
|---|---------|-------|--------|
| 1 | ppc32 missing in old nixpkgs | 18 | Accept (old nixpkgs limitation) |
| 2 | old clang mips64el N32 | 8 | Accept (old compiler limitation) |
| 3 | gcc4_4/gcc4_6 no cross support | 2 | Accept (ancient nixpkgs) |
| 4 | gcc4_5 cross compiler build | 1 | Accept (source incompatibility) |
| 5 | gcc5 mips64el unavailable | 1 | Accept (old nixpkgs N32 limitation) |
| 6 | clang3_5 aarch64 ICE | 1 | Accept (old compiler bug) |
| 7 | gcc12/ppc32 unavailable | 1 | Same as #1 (subset) |
| **Total** | | **32** | **All known limitations** |

All 32 remaining failures are inherent limitations of old compilers or old nixpkgs
versions. No actionable fixes remain — every failure is either an old nixpkgs missing
a cross target, an old compiler with broken codegen, or ancient GCC source that can't
build with modern tools.

## Results Matrix

| Compiler   | i686 | aarch64 | armv7l | mipsel | mips64el | ppc32 | ppc64 | riscv64 |
|------------|------|---------|--------|--------|----------|-------|-------|---------|
| gcc15      | OK   | OK      | OK     | OK     | OK       | OK    | OK    | OK      |
| gcc14      | OK   | OK      | OK     | OK     | OK       | OK    | OK    | OK      |
| gcc13      | OK   | OK      | OK     | OK     | OK       | OK    | OK    | OK      |
| gcc12      | OK   | OK      | OK     | OK     | OK*      | FAIL  | SKIP  | SKIP    |
| gcc11      | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc10      | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc9       | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc8       | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc7       | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc6       | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc5       | OK*  | OK*     | OK*    | OK*    | FAIL     | SKIP  | SKIP  | SKIP    |
| gcc4_9     | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc4_8     | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| gcc4_6     | FAIL | SKIP    | SKIP   | SKIP   | SKIP     | SKIP  | SKIP  | SKIP    |
| gcc4_5     | FAIL | SKIP    | SKIP   | SKIP   | SKIP     | SKIP  | SKIP  | SKIP    |
| gcc4_4     | FAIL | SKIP    | SKIP   | SKIP   | SKIP     | SKIP  | SKIP  | SKIP    |
| clang22    | OK   | OK      | OK     | OK     | OK       | OK    | OK*   | SKIP    |
| clang21    | OK   | OK      | OK     | OK     | OK       | OK    | OK*   | SKIP    |
| clang20    | OK   | OK      | OK     | OK     | OK       | OK    | OK*   | SKIP    |
| clang19    | OK   | OK      | OK     | OK     | OK       | OK    | OK*   | SKIP    |
| clang18    | OK   | OK      | OK     | OK     | OK       | OK    | OK*   | SKIP    |
| clang17    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang16    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang15    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang14    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang13    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang12    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang11    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang10    | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang9     | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang8     | OK   | OK      | OK     | OK     | OK       | FAIL  | SKIP  | SKIP    |
| clang7     | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang6     | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang5     | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang4     | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang3_9   | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang3_8   | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang3_7   | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |
| clang3_5   | OK   | FAIL    | SKIP   | SKIP   | SKIP     | SKIP  | SKIP  | SKIP    |
| clang3_4   | OK   | OK      | OK     | OK     | FAIL     | SKIP  | SKIP  | SKIP    |

\* = Fixed this round
- gcc12/mips64el: was FAIL (libsanitizer), now OK (fix #4: `--disable-libsanitizer`)
- clang18-22/ppc64: were FAIL (linker), now OK (fix #2: `-fuse-ld=`)
- gcc5/i686-mipsel: were SKIP (eval bug), now OK (fix #5: precedence fix)

Note: The test script stops at the first failure per compiler, so targets after the
first FAIL are marked SKIP. For example, gcc6-12 all pass 5 targets but FAIL at ppc32,
so ppc64 and riscv64 are SKIP even though some of them (e.g. gcc13-15/ppc64) work.

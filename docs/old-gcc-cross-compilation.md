# Old GCC Cross-Compilation: Bootstrap Version Mismatch

## Problem

When cross-compiling old GCC versions (8-12) from nixpkgs 22.11 for i686,
the builds fail because the **stage-final cross-compiler** (GCC 11.3.0) is
too new to compile some GCC versions' runtime libraries (libgcc, libstdc++).

In nixpkgs 22.11's cross-compilation bootstrap, building GCC N for a foreign
target proceeds roughly as:

1. The native `buildPackages.stdenv.cc` (GCC 11.3.0) compiles GCC N's source
   code into the cross-compiler binaries (cc1, cc1plus, xgcc).
2. The newly-built xgcc (GCC N itself) compiles the runtime libraries
   (libgcc, libstdc++, libgomp, etc.) for the target.
3. The result is wrapped into the final GCC N cross toolchain.

Step 1 fails when GCC N's source code uses compiler features, built-in macros,
or machine modes that GCC 11.3.0 does not provide — or when GCC 11.3.0
introduces stricter checks that reject GCC N's source.

## Affected Compilers and Specific Failures

### GCC 8 (i686)

Three cascading issues:

1. **`cet.h: No such file or directory`** in libgcc assembly files.
   GCC 8's `libgcc/config/i386/*.S` files `#include <cet.h>`, a glibc system
   header for Control-flow Enforcement Technology.

2. **Undeclared CPU feature bits** in `libgcc/config/i386/cpuinfo.c`.
   GCC 8's cpuinfo.c references `bit_AVX512VBMI2`, `bit_GFNI`, etc. defined
   in GCC 8's own `cpuid.h`, but the include resolves to GCC 11.3.0's version.

3. **`__is_constructible` undeclared** in libstdc++ `type_traits`.
   GCC 8's libstdc++ headers use compiler intrinsics that GCC 11.3.0's
   include path resolution does not satisfy correctly.

### GCC 9, GCC 10 (i686)

**`UINT192` alignment error** in `libgcc/config/libbid/bid_functions.h`.
UINT192 is declared with `ALIGN(16)` but has size 24 bytes (3 x UINT64),
which is not a multiple of 16. GCC 11+ rejects arrays of such types.
Fixed upstream in GCC 11 (PR97164) but never backported.

- GCC PR97164: https://gcc.gnu.org/bugzilla/show_bug.cgi?id=97164

### GCC 12 (i686)

Two distinct issues:

1. **`__LIBGCC_SF_MAX__`, `__LIBGCC_DF_MAX__`, `__LIBGCC_XF_MAX__` undeclared**
   in `libgcc/libgcc2.c`. Built-in macros introduced in GCC 12 (commit 54f0224d),
   emitted by the compiler with `-fbuilding-libgcc` — GCC 11.3.0 does not have
   the generation code.
   - GCC Bugzilla #100341: https://gcc.gnu.org/bugzilla/show_bug.cgi?id=100341

2. **`unknown machine mode 'HF'`** in `libgcc/soft-fp/half.h`. GCC 12 added
   `HFmode` (half-precision float) support; GCC 11.3.0 does not recognize it.

## Solution: `depsBuildBuild` Override (Fixed-Point Bootstrap)

All four affected compilers are now **fixed** using a `depsBuildBuild` override
on the unwrapped cross GCC derivation. This replaces the default native compiler
(GCC 11.3.0) with the native GCC of the same version, so GCC N compiles itself.

In `lib/old-compilers.nix`:

```nix
# Override the unwrapped cross GCC's depsBuildBuild to use native gccN
bootstrappedCC = oldCrossGcc.cc.overrideAttrs (old: {
  depsBuildBuild = [ oldPkgs.${attr} ];
});
# Rewrap with original wrapper (preserving libc, bintools, etc.)
rewrapped = oldCrossGcc.override { cc = bootstrappedCC; };
```

This approach:
- Only changes what compiler builds GCC N's source — does not affect glibc,
  bintools, or any other package
- Avoids the glibc hash mismatch that occurs when overlaying the whole stdenv
  (which would change glibc's derivation hash and break cc-wrapper assertions)
- Is applied uniformly to all old GCC cross builds — for gcc11 (the default
  compiler in 22.11), the override is a no-op (same derivation hash)
- Only triggers a rebuild of the cross GCC itself, not the entire toolchain

### Approaches That Were Tried and Failed

1. **Source patches**: No upstream patches exist for the `__LIBGCC_*` builtins
   or `HFmode` issues — GCC Bugzilla #100341 was closed INVALID ("not a bug").

2. **Nixpkgs stdenv overlay**: Re-importing nixpkgs-22.11 with
   `overlays = [ (final: prev: { stdenv = prev.overrideCC prev.stdenv gccN; }) ]`
   changes what compiler builds glibc, producing a different glibc hash. This
   breaks the cc-wrapper assertion `libc_dev == bintools.libc_dev`.

3. **`gcc.cc.override { stdenv = gcc9Stdenv; }`**: Overriding the `stdenv`
   parameter of the unwrapped GCC derivation changes platform detection,
   triggering a full 3-stage bootstrap build that fails with
   `C++ preprocessor "/lib/cpp" fails sanity check`.

### References

- nixpkgs PR #352821: https://github.com/NixOS/nixpkgs/pull/352821
  (Modern nixpkgs fix: auto-select matching GCC stdenv for cross-compilation)
- nixpkgs Issue #244871: https://github.com/NixOS/nixpkgs/issues/244871
  (Tracks the general cross-build GCC version mismatch problem)
- nixpkgs PR #209870: https://github.com/NixOS/nixpkgs/pull/209870
  (Bootstrap reform ensuring libgcc is compiled by its own GCC version)

## Summary

| Compiler | i686 cross status | Root cause | Fix |
|----------|------------------|------------|-----|
| gcc8     | **fixed**        | GCC 11.3 stage-final incompatible with gcc8 sources | `depsBuildBuild` override |
| gcc9     | **fixed**        | UINT192 alignment + stage-final mismatch | `depsBuildBuild` override |
| gcc10    | **fixed**        | UINT192 alignment + stage-final mismatch | `depsBuildBuild` override |
| gcc11    | works            | N/A (gcc11 IS the default compiler) | N/A |
| gcc12    | **fixed**        | GCC 11.3 lacks `__LIBGCC_*` builtins and `HFmode` | `depsBuildBuild` override |

# Cross-Compilation Test Report: hello (O2-baseline-unhardened)

Host: x86_64-linux
Constraints: systemd cgroup, 60GB RAM + 60GB swap, 12 parallel compilers
Each compiler tries archs in order (i686, aarch64, armv7l, mipsel, mips64el, ppc32, ppc64, riscv64), stops on first failure.

## Summary

- **Passed**: 241
- **Failed**: 59
- **Skipped** (after first failure): 0
- **Unsupported** (below minimum compiler version): 20

## Results Matrix

| Compiler   | i686      | aarch64   | armv7l    | mipsel    | mips64el  | ppc32     | ppc64     | riscv64   |
|------------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|
| gcc15      | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| gcc14      | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| gcc13      | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| gcc12      | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc11      | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc10      | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc9       | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc8       | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc7       | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| gcc6       | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | n/a       |
| gcc5       | OK        | OK        | OK        | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| gcc4_9     | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | n/a       |
| gcc4_8     | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | n/a       |
| gcc4.6     | OK        | OK        | OK        | OK        | N/A       | N/A       | N/A       | N/A       |
| gcc4.5     | OK        | FAIL      | FAIL      | FAIL      | N/A       | N/A       | N/A       | N/A       |
| gcc4.4     | OK        | OK        | OK        | OK        | N/A       | N/A       | N/A       | N/A       |
| clang22    | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| clang21    | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| clang20    | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| clang19    | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| clang18    | OK        | OK        | OK        | OK        | OK        | OK        | OK        | OK        |
| clang17    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang16    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang15    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang14    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang13    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang12    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang11    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang10    | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        |
| clang9     | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | FAIL      |
| clang8     | OK        | OK        | OK        | OK        | OK        | FAIL      | OK        | n/a       |
| clang7     | OK        | OK        | OK        | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang6     | OK        | OK        | OK        | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang5     | OK        | OK        | OK        | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang4     | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_9   | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_8   | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_7   | OK        | OK        | OK        | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_5   | OK        | FAIL      | OK        | OK        | FAIL      | OK        | FAIL      | n/a       |
| clang3_4   | OK        | OK        | OK        | OK        | FAIL      | OK        | FAIL      | n/a       |


## Failure Modes

### 1. `pkgsCross` missing on old nixpkgs (eval error)

**Affected**: gcc4_5, gcc4_8, gcc4_9, gcc5, gcc6, clang3_7, clang3_8, clang3_9, clang4 — all cross targets

Old nixpkgs inputs (18.03, 15.09) don't have the `pkgsCross` attribute. The `mkStdenv`
in `old-compilers.nix` calls `archLib.getPkgsForTarget oldPkgs target` which tries
`oldPkgs.pkgsCross.<crossAttr>`, causing an eval-time error.

```
error: attribute 'pkgsCross' missing
  at lib/architectures.nix:46:62
```

**Fix**: For compilers from pre-`pkgsCross` nixpkgs, cross-compilation needs a different
approach — either use the compiler binary directly without going through old nixpkgs'
cross infrastructure, or skip cross targets for these compilers.

### 2. Old GCC fails to cross-compile itself for i686 — FIXED

**Affected**: gcc8, gcc9, gcc10, gcc12 — i686 target

**Previously**: When cross-compiling for i686, nixpkgs-22.11 uses GCC 11.3.0 as the
stage-final compiler to build GCC N's source. GCC 11.3.0 is incompatible with some
older/newer GCC sources (UINT192 alignment in gcc9/10, missing builtins in gcc12,
libstdc++ intrinsics in gcc8).

**Fix**: Override `depsBuildBuild` on the unwrapped cross GCC derivation to use the
native GCC of the same version. This makes GCC N compile its own source (fixed-point
bootstrap), avoiding all version mismatch issues. The override only rebuilds the cross
GCC itself — glibc, bintools, and all other packages remain unchanged.

See `docs/old-gcc-cross-compilation.md` for detailed analysis.

### 3. Exec format error — cross coreutils in nativeBuildInputs (build error)

**Affected**: gcc7, gcc11, gcc13, gcc14 — aarch64 target

The old `overrideCC` pulls in aarch64-compiled coreutils as a build-time dependency.
The x86_64 builder can't execute the aarch64 binary.

```
/nix/store/.../coreutils-aarch64-unknown-linux-gnu-9.10/bin/date:
  cannot execute binary file: Exec format error
```

**Root cause**: The old compiler's stdenv from nixpkgs-22.11 has cross-compiled packages
leaking into `nativeBuildInputs`. The `overrideCC` swaps the compiler but the rest of
the stdenv still references cross-compiled tools from the old nixpkgs.

### 4. compiler-rt fails to build for mipsel (build error)

**Affected**: ALL clang versions (5-22) — mipsel target; clang15 also fails on mips64el

Building `compiler-rt` (Clang's runtime library) for mipsel fails across every Clang version.
This affects both old (nixpkgs-22.11) and current (nixpkgs-unstable) Clang.

```
error: builder failed with exit code 1/2
compiler-rt-*.src/lib/builtins/atomic.c (clang5-9)
compiler-rt sanitizer_common build errors (clang10+)
```

**Root cause**: compiler-rt has long-standing mipsel cross-compilation issues in nixpkgs.
This is not specific to old compilers — even clang22 from current nixpkgs fails.

## Summary by Compiler

| Compiler Group | Native | i686 | aarch64 | armv7l | mipsel | mips64el |
|----------------|--------|------|---------|--------|--------|----------|
| gcc15 (current)| OK     | OK   | OK      | OK     | OK     | OK       |
| gcc7-14 (22.11)| OK     | OK*  | mixed   | -      | -      | -        |
| gcc4_5-6 (18.03)| OK    | FAIL | FAIL    | -      | -      | -        |
| clang5-22      | OK     | OK   | OK      | OK     | FAIL   | FAIL/SKIP|
| clang3_7-4 (18.03)| OK  | FAIL | FAIL    | -      | -      | -        |

\* gcc8/9/10/12 i686 fixed via `depsBuildBuild` bootstrap (see `docs/old-gcc-cross-compilation.md`)

**Fully working compilers (all 6 targets)**: gcc15 only

**Working for i686+aarch64+armv7l**: clang5-22 (21 compilers), gcc7, gcc11, gcc13, gcc14

**Native + i686 only**: gcc7-14 (all 8 compilers now work for i686)

**Native only**: gcc4_5-6, clang3_7-4

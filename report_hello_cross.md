# Cross-Compilation Test Report: hello (O2-baseline-unhardened)

Host: x86_64-linux
Constraints: systemd cgroup, 60GB RAM + 60GB swap, 12 parallel compilers
Each compiler tries archs in order (i686, aarch64, armv7l, mipsel, mips64el, ppc32, ppc64, riscv64), stops on first failure.

## Summary

- **Passed**: 256
- **Failed**: 41
- **Skipped** (after first failure): 0
- **Unsupported** (below minimum compiler version): 63

## Results Matrix

| Compiler   | i686      | aarch64   | armv7l-hf | armv7l-sf | mipsel    | mips64el  | ppc32     | ppc64     | riscv64   |
|------------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|
| gcc15      | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| gcc14      | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| gcc13      | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| gcc12      | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc11      | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc10      | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc9       | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc8       | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc7       | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| gcc6       | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | n/a       |
| gcc5       | OK        | OK        | OK        | n/a       | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| gcc4_9     | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | n/a       |
| gcc4_8     | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | n/a       |
| gcc4_6     | OK        | n/a       | n/a       | OK        | OK        | OK        | OK        | n/a       | n/a       |
| gcc4_5     | OK        | n/a       | n/a       | OK        | OK        | OK        | OK        | n/a       | n/a       |
| gcc4_4     | OK        | n/a       | n/a       | OK        | OK        | OK        | OK        | n/a       | n/a       |
| clang22    | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| clang21    | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| clang20    | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| clang19    | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| clang18    | OK        | OK        | OK        | n/a       | OK        | OK        | OK        | OK        | OK        |
| clang17    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang16    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang15    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang14    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang13    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang12    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang11    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang10    | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | OK        |
| clang9     | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | FAIL      |
| clang8     | OK        | OK        | OK        | n/a       | OK        | OK        | FAIL      | OK        | n/a       |
| clang7     | OK        | OK        | OK        | n/a       | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang6     | OK        | OK        | OK        | n/a       | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang5     | OK        | OK        | OK        | n/a       | OK        | FAIL      | FAIL      | FAIL      | n/a       |
| clang4     | OK        | OK        | OK        | n/a       | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_9   | OK        | OK        | OK        | n/a       | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_8   | OK        | OK        | OK        | n/a       | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_7   | OK        | OK        | OK        | n/a       | OK        | FAIL      | OK        | OK        | n/a       |
| clang3_5   | OK        | FAIL      | OK        | n/a       | OK        | FAIL      | OK        | FAIL      | n/a       |
| clang3_4   | OK        | OK        | OK        | n/a       | OK        | FAIL      | OK        | FAIL      | n/a       |


## Failure Modes

### 1. Old GCC fails to cross-compile itself for i686 — FIXED

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

### 2. Exec format error — cross coreutils in nativeBuildInputs (build error)

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

### 3. compiler-rt fails to build for mipsel (build error)

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

| Compiler Group      | i686 | aarch64 | armv7l-hf | armv7l-sf | mipsel | mips64el | ppc32 | ppc64 | riscv64 |
|---------------------|------|---------|-----------|-----------|--------|----------|-------|-------|---------|
| gcc13-15 (current)  | OK   | OK      | OK        | n/a       | OK     | OK       | OK    | OK    | OK      |
| gcc7-12 (22.11)     | OK   | OK      | OK        | n/a       | OK     | OK       | FAIL  | OK    | OK/n/a  |
| gcc5 (18.03)        | OK   | OK      | OK        | n/a       | OK     | FAIL     | FAIL  | FAIL  | n/a     |
| gcc4_8-4_9 (22.11)  | OK   | OK      | OK        | n/a       | OK     | OK       | FAIL  | OK    | n/a     |
| gcc4_4-4_6 (15.09)  | OK   | n/a     | n/a       | OK        | OK     | OK       | OK    | n/a   | n/a     |
| clang18-22 (current)| OK   | OK      | OK        | n/a       | OK     | OK       | OK    | OK    | OK      |
| clang5-17 (22.11)   | OK   | OK      | OK        | n/a       | OK     | OK       | mixed | OK    | mixed   |
| clang3_4-4 (18.03/15.09)| OK | OK   | OK        | n/a       | OK     | FAIL     | OK    | mixed | n/a     |

\* gcc8/9/10/12 i686 fixed via `depsBuildBuild` bootstrap (see `docs/old-gcc-cross-compilation.md`)
\* gcc4_4-4_6 cross via `old-gcc-cross.nix` — calls old GCC expression directly with cross params (see `docs/old-gcc-44-45-46-cross.md`)
\* gcc4_4-4_6: aarch64 n/a (ARM64 added in GCC 4.8), armv7l-hf n/a (gnueabihf added in GCC 4.7, use armv7l-sf instead), ppc64 n/a (gnuabielfv2 unknown to GCC ≤ 4.6), riscv64 n/a (added in GCC 7)
\* armv7l-sf: soft-float ARM (gnueabi) for gcc ≤ 4.6 only — identical integer code, different float calling convention

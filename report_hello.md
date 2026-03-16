# Compiler Test Report: hello (x86_64, O2-baseline-unhardened)

Build: `nix build .#dataset.x86_64-linux.hello.x86_64.<compiler>-O2-baseline-unhardened`
Constraints: systemd cgroup, 60GB RAM + 60GB swap limit

## Summary

- **Tested**: 41 compilers
- **Passed**: 37
- **Failed**: 4

## Results

### GCC (14/16 passed)

| Compiler | Version | Source | Status | Notes |
|----------|---------|--------|--------|-------|
| gcc4_4   | 4.4.7   | nixpkgs-15.09 | FAIL | `crossConfig: unbound variable` in old wrapper |
| gcc4_5   | 4.5.4   | nixpkgs-18.03 | OK | |
| gcc4_6   | 4.6.4   | nixpkgs-15.09 | FAIL | `crossConfig: unbound variable` in old wrapper |
| gcc4_8   | 4.8.5   | nixpkgs-22.11 | OK | |
| gcc4_9   | 4.9.4   | nixpkgs-22.11 | OK | |
| gcc5     | 5.5.0   | nixpkgs-18.03 | OK | |
| gcc6     | 6.5.0   | nixpkgs-22.11 | OK | |
| gcc7     | 7.5.0   | nixpkgs-22.11 | OK | |
| gcc8     | 8.5.0   | nixpkgs-22.11 | OK | |
| gcc9     | 9.5.0   | nixpkgs-22.11 | OK | |
| gcc10    | 10.4.0  | nixpkgs-22.11 | OK | |
| gcc11    | 11.3.0  | nixpkgs-22.11 | OK | |
| gcc12    | 12.2.0  | nixpkgs-22.11 | OK | |
| gcc13    | 13.4.0  | nixpkgs-unstable | OK | |
| gcc14    | 14.3.0  | nixpkgs-unstable | OK | |
| gcc15    | 15.2.0  | nixpkgs-unstable | OK | |

### Clang (23/25 passed)

| Compiler | Version | Source | Status | Notes |
|----------|---------|--------|--------|-------|
| clang3_4 | 3.4.2   | nixpkgs-18.03 | FAIL | `C compiler cannot create executables` |
| clang3_5 | 3.5.2   | nixpkgs-18.03 | FAIL | `C compiler cannot create executables` |
| clang3_6 | 3.6.2   | nixpkgs-15.09 | FAIL | `crossConfig: unbound variable` in old wrapper |
| clang3_7 | 3.7.1   | nixpkgs-18.03 | OK | |
| clang3_8 | 3.8.1   | nixpkgs-18.03 | OK | |
| clang3_9 | 3.9.1   | nixpkgs-18.03 | OK | |
| clang4   | 4.0.1   | nixpkgs-18.03 | OK | |
| clang5   | 5.0.2   | nixpkgs-22.11 | OK | |
| clang6   | 6.0.1   | nixpkgs-22.11 | OK | |
| clang7   | 7.1.0   | nixpkgs-22.11 | OK | |
| clang8   | 8.0.1   | nixpkgs-22.11 | OK | |
| clang9   | 9.0.1   | nixpkgs-22.11 | OK | |
| clang10  | 10.0.1  | nixpkgs-22.11 | OK | |
| clang11  | 11.1.0  | nixpkgs-22.11 | OK | |
| clang12  | 12.0.1  | nixpkgs-22.11 | OK | |
| clang13  | 13.0.1  | nixpkgs-22.11 | OK | |
| clang14  | 14.0.6  | nixpkgs-22.11 | OK | |
| clang15  | 15.0.7  | nixpkgs-23.11 | OK | |
| clang16  | 16.0.6  | nixpkgs-23.11 | OK | |
| clang17  | 17.0.6  | nixpkgs-24.05 | OK | |
| clang18  | 18.1.8  | nixpkgs-unstable | OK | |
| clang19  | 19.1.7  | nixpkgs-unstable | OK | |
| clang20  | 20.1.8  | nixpkgs-unstable | OK | |
| clang21  | 21.1.8  | nixpkgs-unstable | OK | |
| clang22  | 22.1.0-rc3 | nixpkgs-unstable | OK | |

## Failures

All 4 failures fall into 2 categories:

### 1. `crossConfig: unbound variable` (gcc4_4, gcc4_6, clang3_6)

The compiler wrapper from nixpkgs-15.09 references `$crossConfig` in its setup-hook,
which doesn't exist in modern stdenv. These wrappers are too old to be mixed with
current nixpkgs infrastructure.

### 2. `C compiler cannot create executables` (clang3_4, clang3_5)

The clang binary from nixpkgs-18.03 builds successfully but cannot produce working
executables when injected into modern stdenv. Likely an ABI or system header
incompatibility between the old clang and current glibc/binutils.

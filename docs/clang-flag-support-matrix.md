# Clang Flag Support Matrix

## Methodology

Each flag was tested against the real Clang binary (not the Nix cc-wrapper) for every version
present in this project's `_crossToolchainMap`. The test compiles `int main(){return 0;}` with
the flag(s) via `-S -o /dev/null` (compile-only, no linking) and checks whether the compiler
emits an "unknown argument", "unsupported option", or "unknown target CPU" error. A clean exit
(even with an "argument unused" warning) is counted as supported.

Source of truth for flag-introduction versions: LLVM release notes fetched from
`releases.llvm.org`, cross-checked against actual binary tests from binaries in `/nix/store`.

All tests are run against an `x86_64-linux-gnu` target unless noted otherwise.
`-mbranch-protection` is AArch64-specific; the target-check column reflects x86_64 driver
behaviour (clang 7–17: silently warns; clang 18+: hard error for wrong target).

Legend: `Y` = accepted without error, `N` = driver error (unknown/unsupported argument),
`W` = accepted with "argument unused / not supported" warning, `D` = preprocessor define
accepted syntactically but effectiveness is runtime-library-dependent (see notes).

## Optimization Levels

All optimization levels (`-O0`, `-O1`, `-O2`, `-O3`, `-Os`, `-Ofast`, `-Oz`) are accepted by
every Clang version in scope (3.4 – 22). No version-specific restrictions apply.

## Flag Sets

| Flag(s) | 3.4 | 3.5 | 3.7 | 3.8 | 3.9 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 |
|---------|-----|-----|-----|-----|-----|---|---|---|---|---|---|----|----|----|----|----|----|----|----|----|----|----|----|-----|
| `-fno-inline` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-funroll-loops` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-ffunction-sections -fdata-sections` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fno-omit-frame-pointer` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fno-PIC` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fno-tree-vectorize` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-flto` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-flto=thin` (ThinLTO) | N | N | W | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-ffast-math` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fPIE` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fPIE -static-pie` (staticpie) | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |

Notes:
- `-flto=thin`: Clang 3.7 recognizes the flag but warns "optimization flag not supported" and
  ignores it. Clang 3.8+ accepts it silently. ThinLTO actually functions starting from 3.9
  (when the full ThinLTO infrastructure landed), which matches the `minClangVersion = {major=3; minor=9}` in flags.nix.
- `-static-pie`: Introduced in Clang 9 (review D58307). Clang 8 silently ignores the flag;
  flags.nix uses `minClangVersion = {major=9; minor=0}`.

## Hardening Modes

| Flag(s) | 3.4 | 3.5 | 3.7 | 3.8 | 3.9 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 |
|---------|-----|-----|-----|-----|-----|---|---|---|---|---|---|----|----|----|----|----|----|----|----|----|----|----|----|-----|
| `-fstack-protector-strong --param=ssp-buffer-size=4` | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fstack-protector-all` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-D_FORTIFY_SOURCE=2` | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D |
| `-D_FORTIFY_SOURCE=3` | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D | D |
| `-fPIE` (pie hardening) | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-Wformat -Wformat-security -Werror=format-security` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fno-strict-overflow` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fcf-protection=full` (CET, x86 only) | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-mbranch-protection=standard` (BTI+PAC, aarch64 only) | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fstack-clash-protection` | N | N | N | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fzero-call-used-regs=used-gpr` | N | N | N | N | N | N | N | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y |

Notes:
- `-fstack-protector-strong`: Introduced in Clang 3.5 (review D2717 merged early 2014).
  Clang 3.4 rejects it with "unknown argument".
- `-D_FORTIFY_SOURCE=2/3`: Both are preprocessor defines that all Clang versions accept
  syntactically (the `-D` flag is universal). **Actual fortification effectiveness** depends on
  glibc version and optimization level: `_FORTIFY_SOURCE=2` needs at least `-O1`; level 3
  requires `__builtin_dynamic_object_size` (present since Clang 8) and glibc 2.33+. flags.nix
  restricts `fortify3` to `minClangVersion = {major=14; minor=0}` to match the ecosystem
  baseline where both compiler and libc support are reliably available.
- `-fcf-protection=full`: Intel CET support added in Clang 7.0 (first mentioned in Clang 7
  release notes context). Clang 6 and earlier reject it.
- `-mbranch-protection=standard`: AArch64 BTI+PAC. Added in Clang 8. On x86_64: Clang 7–17
  silently warns "argument unused", Clang 18+ emits a hard error "unsupported option for
  target". When cross-compiling to aarch64 (`-target aarch64-linux-gnu`), Clang 8+ accepts
  it correctly. flags.nix uses `minClangVersion = {major=7; minor=0}`.
- `-fstack-clash-protection`: Introduced in Clang 11.0 (documented in Clang 11 release notes).
- `-fzero-call-used-regs=used-gpr`: Introduced in Clang 15.0 (documented in Clang 15 release
  notes via review D110869).

## Sanitizers

| Flag(s) | 3.4 | 3.5 | 3.7 | 3.8 | 3.9 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 |
|---------|-----|-----|-----|-----|-----|---|---|---|---|---|---|----|----|----|----|----|----|----|----|----|----|----|----|-----|
| `-fsanitize=address` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fsanitize=memory` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fsanitize=thread` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-fsanitize=undefined` | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |

Notes:
- The unified `-fsanitize=` interface was introduced in Clang 3.2 (replacing the old
  `-faddress-sanitizer`, `-fthread-sanitizer`, `-fcatch-undefined-behavior` flags that were
  removed in 3.5). All four sanitizer modes compile without driver error on every version here.
- **Runtime availability** is a separate concern: MemorySanitizer and ThreadSanitizer require
  x86_64 or aarch64 (per flags.nix `archs` constraints); AddressSanitizer needs a runtime
  library present in the cross stdenv.

## March Levels (x86-64 microarchitecture)

| Flag(s) | 3.4 | 3.5 | 3.7 | 3.8 | 3.9 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 |
|---------|-----|-----|-----|-----|-----|---|---|---|---|---|---|----|----|----|----|----|----|----|----|----|----|----|----|-----|
| `-march=x86-64-v2` | N | N | N | N | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-march=x86-64-v3` | N | N | N | N | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |
| `-march=x86-64-v4` | N | N | N | N | N | N | N | N | N | N | N | N | N | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y | Y |

Notes:
- x86-64 microarchitecture levels (v2/v3/v4) were added in Clang 12 (commit landed October
  2020, merged for the 12.0 release). Clang 11 and earlier report "unknown target CPU".
  flags.nix uses `minClangVersion = {major=12; minor=0}`.

## Summary Table: First Supporting Clang Version

| Flag / Profile | First Clang version |
|---------------|---------------------|
| `-O0` / `-O1` / `-O2` / `-O3` / `-Os` / `-Ofast` / `-Oz` | ≤ 3.4 (all in scope) |
| `-fno-inline` / `-funroll-loops` / `-ffast-math` | ≤ 3.4 |
| `-ffunction-sections` / `-fdata-sections` | ≤ 3.4 |
| `-fno-omit-frame-pointer` / `-fno-PIC` / `-fno-tree-vectorize` | ≤ 3.4 |
| `-fPIE` / `-fstack-protector-all` | ≤ 3.4 |
| `-fsanitize=address/memory/thread/undefined` | ≤ 3.4 |
| `-flto` | ≤ 3.4 |
| `-D_FORTIFY_SOURCE=2/3` (syntactic) | ≤ 3.4 |
| `-Wformat -Wformat-security -Werror=format-security` | ≤ 3.4 |
| `-fno-strict-overflow` | ≤ 3.4 |
| `-fstack-protector-strong` / `--param=ssp-buffer-size=4` | 3.5 |
| `-flto=thin` (functional) | 3.9 (driver 3.8; 3.7 warns) |
| `-fcf-protection=full` (x86 CET) | 7 |
| `-mbranch-protection=standard` (aarch64 BTI+PAC) | 8 |
| `-fPIE -static-pie` | 9 |
| `-fstack-clash-protection` | 11 |
| `-march=x86-64-v2/v3/v4` | 12 |
| `-D_FORTIFY_SOURCE=3` (effective, glibc 2.33+) | 14 (flags.nix constraint) |
| `-fzero-call-used-regs=used-gpr` | 15 |

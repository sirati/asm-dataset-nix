# Cross-Compilation Failure Analysis

Total failures: 37

# Grouped Failures

## Regressions

**gcc ppc32** (gcc6-11, gcc4_8-4_9 from nixpkgs-22.11): `pkgsCross.ppc32` does not
exist in nixpkgs-22.11. These compilers pass all 5 original targets (i686, aarch64,
armv7l, mipsel, mips64el) but fail for the newly added ppc32 target because the old
nixpkgs lacks the cross-compilation definition. Similarly affects clang8-17 from old nixpkgs.

**Note**: This is not a regression from previous results — ppc32, ppc64, and riscv64
are newly added targets. All previously passing compiler/target combinations remain passing.

## Build failure: `hello-variant-mips64el-unknown-linux-gnuabin32-2.12.2.drv` (8)

**Affected**: clang3_4/mips64el clang3_7/mips64el clang3_8/mips64el clang3_9/mips64el clang4/mips64el clang5/mips64el clang6/mips64el clang7/mips64el 

```
configure: C compiler cannot create executables
checking for mips64el-unknown-linux-gnuabin32-gcc... mips64el-unknown-linux-gnuabin32-clang
configure flags: --disable-dependency-tracking --prefix=/nix/store/jd6ax6ghkhlpdxsgy7hwbyx69s84f3wy-hello-variant-mips64el-unknown-linux-gnuabin32-2.12.2 --build=x86_64-unknown-linux-gnu --host=mips64el-unknown-linux-gnuabin32
(config.log not available — exact linker/compiler error hidden inside sandbox)
```

## Build failure: `hello-variant-powerpc64-unknown-linux-gnuabielfv2-2.12.2.drv` (5)

**Affected**: clang18/ppc64 clang19/ppc64 clang20/ppc64 clang21/ppc64 clang22/ppc64 

```
configure: C compiler cannot create executables
checking for powerpc64-unknown-linux-gnuabielfv2-gcc... powerpc64-unknown-linux-gnuabielfv2-clang
configure flags: --disable-dependency-tracking --prefix=/nix/store/gasns4bvx8by8ihirfkh3y9nh79k94fr-hello-variant-powerpc64-unknown-linux-gnuabielfv2-2.12.2 --build=x86_64-unknown-linux-gnu --host=powerpc64-unknown-linux-gnuabielfv2
(config.log not available — exact linker/compiler error hidden inside sandbox)
```

## Eval: attribute 'buildPackages' missing (2)

**Affected**: gcc4_4/i686 gcc4_6/i686 

```
       error: attribute 'buildPackages' missing
```

## Eval error (2)

**Affected**: gcc4_5/i686 gcc5/i686 

```
       error: expected a Boolean but found a string: "i686-unknown-linux-gnu-gcc-cross-wrapper-5.5.0-i686-unknown-linux-gnu-stage-final"
```

## Eval: attribute 'ppc32' missing (18)

**Affected**: clang10/ppc32 clang11/ppc32 clang12/ppc32 clang13/ppc32 clang14/ppc32 clang15/ppc32 clang16/ppc32 clang17/ppc32 clang8/ppc32 clang9/ppc32 gcc10/ppc32 gcc11/ppc32 gcc4_8/ppc32 gcc4_9/ppc32 gcc6/ppc32 gcc7/ppc32 gcc8/ppc32 gcc9/ppc32 

```
       error: attribute 'ppc32' missing
```

## Compiler crash (ICE): `hello-variant-aarch64-unknown-linux-gnu-2.12.2.drv` (1)

**Affected**: clang3_5/aarch64 

```
Compiler crash (ICE):
clang-3.5: error: clang frontend command failed due to signal (use -v to see invocation)
clang version 3.5.2 (tags/RELEASE_352/final)
Target: aarch64-unknown-linux-gnu
Thread model: posix
make[2]: *** [Makefile:3639: lib/libhello_a-getopt.o] Error 254
make[1]: *** [Makefile:4950: all-recursive] Error 1
```

## Sanitizer struct stat mismatch: `mips64el-unknown-linux-gnuabin32-stage-final-gcc-12.2.0.drv` (1)

**Affected**: gcc12/mips64el 

```
[01m[Kcc1:[m[K [01;31m[Kerror: [m[Kno include path in which to search for stdc-predef.h
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_platform_limits_linux.cpp:75:38:[m[K [01;31m[Kerror: [m[Kstatic assertion failed
[01m[K/nix/store/x7wxn8kqz6nrla4sqp3pcl1cg2spfq8m-glibc-mips64el-unknown-linux-gnuabin32-2.35-163-dev/include/bits/struct_stat.h:190:8:[m[K [01;31m[Kerror: [m[Kredefinition of '[01m[Kstruct stat64[m[K'
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_syscall_generic.inc:19:24:[m[K [01;31m[Kerror: [m[K'[01m[K__NR_mmap2[m[K' was not declared in this scope
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_linux.cpp:279:23:[m[K [01;31m[Kerror: [m[K'[01m[Kstruct stat64[m[K' has no member named '[01m[Kst_atim[m[K'; did you mean '[01m[Kst_atime[m[K'?
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_linux.cpp:280:23:[m[K [01;31m[Kerror: [m[K'[01m[Kstruct stat64[m[K' has no member named '[01m[Kst_mtim[m[K'; did you mean '[01m[Kst_mtime[m[K'?
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_linux.cpp:281:23:[m[K [01;31m[Kerror: [m[K'[01m[Kstruct stat64[m[K' has no member named '[01m[Kst_ctim[m[K'; did you mean '[01m[Kst_ctime[m[K'?
[01m[K../../../../gcc-12.2.0/libsanitizer/sanitizer_common/sanitizer_syscall_generic.inc:19:24:[m[K [01;31m[Kerror: [m[K'[01m[K__NR_stat64[m[K' was not declared in this scope
```


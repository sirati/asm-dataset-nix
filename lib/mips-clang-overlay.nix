# Nixpkgs overlay to fix LLVM compiler-rt cross-compilation for MIPS targets.
#
# Three bugs in compiler-rt break Clang cross-compilation for MIPS:
#
# 1. sanitizer_platform_limits_posix.h — wrong struct stat sizes:
#      O32 (mipsel):          hardcodes 160, actual sizeof(struct stat) = 144
#      N32 (mips64el abin32): hardcodes 176, actual sizeof(struct stat) = 160
#
#    Three source patterns across LLVM versions:
#      LLVM 5-14:  FIRST_32_SECOND_64(160, 216)           — no N32 branch
#      LLVM 15-19: FIRST_32_SECOND_64((_MIPS_SIM == _ABIN32) ? 176 : 160, 216)
#      LLVM 20-22: #if _ABIN32 → F_3_S_6(176, 216) #else → F_3_S_6(160, 216)
#
# 2. fp_compare_impl.inc — wrong CMP_RESULT type on MIPS N32:
#      GCC's __libgcc_cmp_return__ is DI mode (8 bytes) on N32, but compiler-rt
#      falls through to `typedef long CMP_RESULT` which is 4 bytes on N32 (ILP32).
#      Fix: add a `#elif defined(__mips__) && (_MIPS_SIM == _ABIN32)` case.
#      Only affects LLVM 13+ (fp_compare_impl.inc didn't exist before LLVM 13).
#
# 3. Sanitizers/extras don't support MIPS:
#      dfsan, msan fail on N32; scudo_standalone needs 64-bit atomics unavailable
#      on O32. Fix: disable sanitizers, xray, libfuzzer, memprof, orc,
#      ctx_profile via cmake flags (only when target is MIPS).
#
# Usage in flake.nix:
#   overlays = [ (import ./lib/mips-clang-overlay.nix) ];
#
# Apply to both current and old nixpkgs imports that contain clang compilers
# used for MIPS cross-compilation.

final: prev:
let
  patchCompilerRt =
    rt:
    rt.overrideAttrs (old: {
      postPatch =
        (old.postPatch or "")
        + ''
          # ── Fix 1: struct_kernel_stat_sz for MIPS O32/N32 in sanitizer headers ──
          for f in lib/sanitizer_common/sanitizer_platform_limits_posix.h \
                   lib/sanitizer_common/sanitizer_platform_limits_posix.cc; do
            [ -f "$f" ] || continue
            # LLVM 15-19: inline ternary — fix both O32 (160→144) and N32 (176→160)
            sed -i 's/(_MIPS_SIM == _ABIN32) ? 176 : 160/(_MIPS_SIM == _ABIN32) ? 160 : 144/g' "$f"
            # LLVM 5-14, 20-22 O32: bare FIRST_32_SECOND_64(160, 216) → (144, 216)
            # (must run before N32 fix so the N32 result (160) doesn't get re-matched)
            sed -i 's/FIRST_32_SECOND_64(160, 216)/FIRST_32_SECOND_64(144, 216)/g' "$f"
            # LLVM 20-22 N32: preprocessor #if _ABIN32 branch: (176, 216) → (160, 216)
            sed -i 's/FIRST_32_SECOND_64(176, 216)/FIRST_32_SECOND_64(160, 216)/g' "$f"
          done

          # ── Fix 2: CMP_RESULT type for MIPS N32 (LLVM 13+) ──
          # GCC's __libgcc_cmp_return__ is 8 bytes (DI mode) on N32,
          # but compiler-rt defaults to `long` (4 bytes on ILP32 N32).
          f=lib/builtins/fp_compare_impl.inc
          if [ -f "$f" ]; then
            sed -i 's|^#else$|#elif defined(__mips__) \&\& (_MIPS_SIM == _ABIN32)\
// MIPS N32: GCC uses DI mode (8 bytes) for __libgcc_cmp_return__.\
typedef long long CMP_RESULT;\
#else|' "$f"
          fi
        '';
      # ── Fix 3: Disable sanitizers/extras that don't support MIPS ──
      # dfsan, msan fail on N32; scudo_standalone needs 64-bit atomics
      # unavailable on O32. Detect MIPS target from cmakeFlags at build time.
      preConfigure =
        (old.preConfigure or "")
        + ''
          if echo "$cmakeFlags" | grep -qi 'mips'; then
            cmakeFlagsArray+=(
              "-DCOMPILER_RT_BUILD_SANITIZERS=OFF"
              "-DCOMPILER_RT_BUILD_XRAY=OFF"
              "-DCOMPILER_RT_BUILD_LIBFUZZER=OFF"
              "-DCOMPILER_RT_BUILD_MEMPROF=OFF"
              "-DCOMPILER_RT_BUILD_ORC=OFF"
              "-DCOMPILER_RT_BUILD_CTX_PROFILE=OFF"
            )
          fi
        '';
    });

  patchLlvmSet =
    llvmPkgs:
    llvmPkgs
    // {
      compiler-rt = patchCompilerRt llvmPkgs.compiler-rt;
      compiler-rt-libc = patchCompilerRt llvmPkgs.compiler-rt-libc;
    }
    // (
      # nixpkgs ≤24.05 has a `libraries` extensible set on llvmPackages_N.
      # The cross clang wrapper gets its compiler-rt from there, so we must
      # patch that too.
      if llvmPkgs ? libraries then
        {
          libraries = llvmPkgs.libraries.extend (
            lfinal: lprev: {
              compiler-rt = patchCompilerRt lprev.compiler-rt;
              compiler-rt-libc = patchCompilerRt lprev.compiler-rt-libc;
            }
          );
        }
      else
        { }
    );

  llvmAttrNames = builtins.filter (n: builtins.match "llvmPackages_[0-9]+" n != null) (
    builtins.attrNames prev
  );

  llvmOverrides = builtins.listToAttrs (
    map (name: {
      inherit name;
      value = patchLlvmSet prev.${name};
    }) llvmAttrNames
  );
in
llvmOverrides
// (if prev ? llvmPackages then { llvmPackages = patchLlvmSet prev.llvmPackages; } else { })

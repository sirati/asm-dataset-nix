# Optimization levels, code-changing flag sets, hardening profiles,
# sanitizers, and march levels.
#
# Flag sets with null cflags use compiler-specific alternatives
# (gccFlag / clangFlag), resolved at build time by mkVariant.nix.
#
# Each entry may carry compiler/arch constraints:
#   minGccVersion / minClangVersion: { major, minor }; { 0, 0 } means no minimum
#   clangOnly: bool; gcc-only flags would set gccOnly (unused so far)
#   archs: list of target.label values this entry applies to (null = all)
#
# matrix.nix filters (compiler, target, entry) tuples by these constraints
# at eval time so unsupported combos never get instantiated.

{ }:

let
  noMin = {
    major = 0;
    minor = 0;
  };

  optimizationLevels = [
    {
      flag = "-O0";
      label = "O0";
      clangOnly = false;
    }
    {
      flag = "-O1";
      label = "O1";
      clangOnly = false;
    }
    {
      flag = "-O2";
      label = "O2";
      clangOnly = false;
    }
    {
      flag = "-O3";
      label = "O3";
      clangOnly = false;
    }
    {
      flag = "-Os";
      label = "Os";
      clangOnly = false;
    }
    {
      flag = "-Oz";
      label = "Oz";
      clangOnly = true;
    }
    {
      flag = "-Ofast";
      label = "Ofast";
      clangOnly = false;
    }
  ];

  flagSets = [
    {
      label = "baseline";
      cflags = "";
      cxxflags = "";
    }
    {
      label = "noinline";
      cflags = "-fno-inline";
      cxxflags = "-fno-inline";
    }
    {
      label = "unroll";
      cflags = "-funroll-loops";
      cxxflags = "-funroll-loops";
    }
    {
      label = "sections";
      cflags = "-ffunction-sections -fdata-sections";
      cxxflags = "-ffunction-sections -fdata-sections";
    }
    {
      label = "frameptr";
      cflags = "-fno-omit-frame-pointer";
      cxxflags = "-fno-omit-frame-pointer";
    }
    {
      label = "nopic";
      cflags = "-fno-PIC";
      cxxflags = "-fno-PIC";
      ldflags = "-no-pie";
      # Must also disable PIE/PIC hardening, otherwise the linker
      # re-adds -pie. Newer nixpkgs renamed the flag pie → pic
      # (the "pie" entry was removed from knownHardeningFlags); we
      # disable both for back-compat across nixpkgs versions.
      extraHardeningDisable = [ "pic" ];
    }
    {
      label = "novec";
      cflags = null;
      cxxflags = null;
      gccFlag = "-fno-tree-vectorize";
      clangFlag = "-fno-vectorize";
    }

    # ── Tier-A/B/C additions ────────────────────────────────────────
    {
      # Link-time optimization (-flto). Restructures function bodies
      # across translation units; produces dramatically different asm.
      label = "lto";
      cflags = "-flto";
      cxxflags = "-flto";
      ldflags = "-flto";
      minGccVersion = {
        major = 4;
        minor = 6;
      };
      minClangVersion = {
        major = 3;
        minor = 7;
      };
    }
    {
      # ThinLTO — clang-specific, faster than full LTO.
      label = "ltothin";
      cflags = "-flto=thin";
      cxxflags = "-flto=thin";
      ldflags = "-flto=thin";
      clangOnly = true;
      minClangVersion = {
        major = 3;
        minor = 9;
      };
    }
    {
      # -ffast-math — allows FP reordering / FMA fusion / drops NaN
      # handling. Visible in numeric code; no-op in non-FP code.
      label = "fastmath";
      cflags = "-ffast-math";
      cxxflags = "-ffast-math";
    }
    {
      # -static-pie — produce a position-independent fully-static
      # binary. Different startup, no PLT, libc inlined.
      label = "staticpie";
      cflags = "-fPIE";
      cxxflags = "-fPIE";
      ldflags = "-static-pie";
      minGccVersion = {
        major = 8;
        minor = 0;
      };
      minClangVersion = {
        major = 9;
        minor = 0;
      };
      # Prevent the linker from re-adding -pie via cc-wrapper:
      # static-pie must not be combined with -pie (only one of the
      # two link modes can win, and -pie loses).
      extraHardeningDisable = [
        "pic"
        "pie"
      ];
    }
  ];

  # Hardening — refactored from {hardened, unhardened} to individual
  # profiles. Each entry enables exactly one hardening flavour atop
  # an otherwise-disabled baseline so the asm impact is cleanly
  # isolated, except "default" (nixpkgs cc-wrapper's full set) and
  # "none" (everything off).
  hardeningModes = [
    {
      label = "none";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "default";
      hardeningEnable = [ ];
      hardeningDisable = [ ];
    }
    {
      # nixpkgs hardeningEnable=stackprotector → -fstack-protector-strong
      label = "ssp";
      hardeningEnable = [ "stackprotector" ];
      hardeningDisable = [ "all" ];
    }
    {
      # All-functions stack-protector — every function gets a canary.
      # Not a nixpkgs flag, requires explicit cflags injection.
      label = "ssp-all";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fstack-protector-all";
    }
    {
      # _FORTIFY_SOURCE=2 (default fortify level in nixpkgs).
      label = "fortify";
      hardeningEnable = [ "fortify" ];
      hardeningDisable = [ "all" ];
    }
    {
      # _FORTIFY_SOURCE=3 — extra runtime checks for object-size
      # detection. GCC 12+, Clang 14+.
      label = "fortify3";
      hardeningEnable = [ "fortify3" ];
      hardeningDisable = [ "all" ];
      minGccVersion = {
        major = 12;
        minor = 0;
      };
      minClangVersion = {
        major = 14;
        minor = 0;
      };
    }
    {
      label = "pie";
      hardeningEnable = [ "pie" ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "relro";
      hardeningEnable = [ "relro" ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "bindnow";
      hardeningEnable = [ "bindnow" ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "relro-bindnow";
      hardeningEnable = [
        "relro"
        "bindnow"
      ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "format";
      hardeningEnable = [ "format" ];
      hardeningDisable = [ "all" ];
    }
    {
      label = "strictoverflow";
      hardeningEnable = [ "strictoverflow" ];
      hardeningDisable = [ "all" ];
    }
    {
      # Intel CET (shadow stack + endbr64 at indirect-call targets).
      # x86 only; GCC 8+, Clang 7+.
      label = "cet";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fcf-protection=full";
      archs = [
        "x86_64"
        "i686"
      ];
      minGccVersion = {
        major = 8;
        minor = 0;
      };
      minClangVersion = {
        major = 7;
        minor = 0;
      };
    }
    {
      # ARM Branch Target Identification + Pointer Authentication.
      # aarch64 only; GCC 9+, Clang 7+.
      label = "btipac";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-mbranch-protection=standard";
      archs = [ "aarch64" ];
      minGccVersion = {
        major = 9;
        minor = 0;
      };
      minClangVersion = {
        major = 7;
        minor = 0;
      };
    }
  ];

  # Sanitizer axis. Heavy IR rewriting; mutually-exclusive (one
  # sanitizer per build). Userspace runtime libs (libasan, libubsan,
  # ...) are linker-injected, so cross-compile support depends on
  # the cross stdenv shipping them — restrict to native targets for
  # now.
  sanitizerModes = [
    {
      label = "san-off";
      cflags = "";
      ldflags = "";
    }
    {
      label = "san-address";
      cflags = "-fsanitize=address";
      ldflags = "-fsanitize=address";
      minGccVersion = {
        major = 4;
        minor = 8;
      };
      minClangVersion = {
        major = 3;
        minor = 1;
      };
      archs = [
        "x86_64"
        "aarch64"
      ];
    }
    {
      label = "san-undefined";
      cflags = "-fsanitize=undefined";
      ldflags = "-fsanitize=undefined";
      minGccVersion = {
        major = 4;
        minor = 9;
      };
      minClangVersion = {
        major = 3;
        minor = 4;
      };
    }
    {
      label = "san-memory";
      cflags = "-fsanitize=memory";
      ldflags = "-fsanitize=memory";
      clangOnly = true;
      minClangVersion = {
        major = 3;
        minor = 4;
      };
      archs = [
        "x86_64"
        "aarch64"
      ];
    }
    {
      label = "san-thread";
      cflags = "-fsanitize=thread";
      ldflags = "-fsanitize=thread";
      minGccVersion = {
        major = 4;
        minor = 8;
      };
      minClangVersion = {
        major = 3;
        minor = 4;
      };
      archs = [
        "x86_64"
        "aarch64"
      ];
    }
  ];

  # x86-64 microarchitecture levels (psABI v3, 2020). Each level is
  # a strict superset of features over the previous; gcc/clang map
  # them to internal feature sets.
  marchLevels = [
    {
      label = "march-default";
      cflags = "";
    }
    {
      label = "march-v2";
      cflags = "-march=x86-64-v2";
      archs = [ "x86_64" ];
      minGccVersion = {
        major = 11;
        minor = 0;
      };
      minClangVersion = {
        major = 12;
        minor = 0;
      };
    }
    {
      label = "march-v3";
      cflags = "-march=x86-64-v3";
      archs = [ "x86_64" ];
      minGccVersion = {
        major = 11;
        minor = 0;
      };
      minClangVersion = {
        major = 12;
        minor = 0;
      };
    }
    {
      label = "march-v4";
      cflags = "-march=x86-64-v4";
      archs = [ "x86_64" ];
      minGccVersion = {
        major = 11;
        minor = 0;
      };
      minClangVersion = {
        major = 12;
        minor = 0;
      };
    }
  ];

in
{
  inherit
    optimizationLevels
    flagSets
    hardeningModes
    sanitizerModes
    marchLevels
    ;
}

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
      # Disable cc-wrapper's PIE/PIC hardening so the linker doesn't
      # re-add -pie (which would conflict with -static-pie). The
      # legacy "pie" name is gone in modern nixpkgs (unified into
      # "pic"); we only list "pic" because nixpkgs 24+ rejects
      # unknown names.
      extraHardeningDisable = [
        "pic"
      ];
    }
  ];

  # Hardening — refactored from {hardened, unhardened} to individual
  # profiles. We inject hardening flags directly via ``extraCflags`` /
  # ``extraLdflags`` rather than relying on cc-wrapper's
  # ``hardeningEnable`` hook, because the set of supported names in
  # ``stdenv.cc.defaultHardeningFlags`` varies across nixpkgs versions
  # (modern: bindnow/format/fortify/.../pic/relro; old-22.11:
  # shadowstack/pacret/.../stackprotector). Direct injection is the
  # only set that works uniformly across the legacy compiler infra.
  #
  # ``hardeningDisable = ["all"]`` silences cc-wrapper's automatic
  # hardening so the only flags visible to the compiler are the ones
  # this profile explicitly injects, giving clean asm-level isolation.
  # The "default" entry leaves cc-wrapper's defaults active (whatever
  # the per-nixpkgs version considers stock-hardened).
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
      label = "ssp";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fstack-protector-strong --param=ssp-buffer-size=4";
    }
    {
      label = "ssp-all";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fstack-protector-all";
    }
    {
      label = "fortify";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      # Needs ≥-O1 to actually inject the runtime checks; below that
      # gcc/clang silently no-op the macro. We inject it regardless;
      # at -O0 this profile collapses to baseline.
      extraCflags = "-D_FORTIFY_SOURCE=2";
    }
    {
      label = "fortify3";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-D_FORTIFY_SOURCE=3";
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
      # PIE — produces a position-independent executable. Affects
      # call/jump instruction encoding and main entry-point shape.
      label = "pie";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fPIE";
      extraLdflags = "-pie";
    }
    {
      label = "relro";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-Wl,-z,relro";
    }
    {
      label = "bindnow";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-Wl,-z,now";
    }
    {
      label = "relro-bindnow";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-Wl,-z,relro -Wl,-z,now";
    }
    {
      label = "format";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-Wformat -Wformat-security -Werror=format-security";
    }
    {
      label = "strictoverflow";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fno-strict-overflow";
    }
    {
      # Intel CET (shadow stack + endbr64 at indirect-call targets).
      # x86 only; GCC 8+, Clang 7+. Visible at asm: every function
      # entry gets endbr64; indirect branches gain validation.
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
      # aarch64 Branch Target Identification + Pointer Authentication.
      # Visible at asm: bti j/c/jc instructions at indirect targets;
      # pacia/autia in prologue/epilogue.
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
    {
      # -fstack-clash-protection — emits explicit stack-probing
      # instructions in function prologues that allocate large stack
      # frames. GCC 8+, Clang 11+.
      label = "stackclash";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fstack-clash-protection";
      minGccVersion = {
        major = 8;
        minor = 0;
      };
      minClangVersion = {
        major = 11;
        minor = 0;
      };
    }
    {
      # -fzero-call-used-regs=used-gpr — zeros caller-saved GPRs at
      # function return to limit ROP gadgets. GCC 11+, Clang 16+.
      label = "zerocallregs";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflags = "-fzero-call-used-regs=used-gpr";
      minGccVersion = {
        major = 11;
        minor = 0;
      };
      minClangVersion = {
        major = 16;
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

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
#
# Link-time flags come in TWO distinct kinds, routed through different
# channels by mkVariant.nix — confusing them breaks every link:
#
#   ldflags / extraLdflags    — RAW ld flags (``-z relro``). Routed to
#       ``NIX_LDFLAGS``, which the ld-wrapper feeds straight to ``ld``.
#       Driver-only flags must NEVER go here: ld parses ``-fsanitize=x``
#       as ``-f sanitize=x`` (``--auxiliary``) and fails every
#       executable link with "-f may not be used without -shared".
#
#   linkFlags / extraLinkFlags — COMPILER-DRIVER link flags
#       (``-fsanitize=x``, ``-flto``, ``-static-pie``). Routed to
#       ``NIX_CFLAGS_LINK``, which the cc-wrapper appends to driver
#       invocations that link; the driver then expands them into the
#       proper ld arguments (and runtime libs, e.g. libasan).

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
      # -Ofast was introduced in GCC 4.6; gcc 4.4/4.5 reject it outright
      # and every configure compile-test dies ("Missing or broken C
      # compiler"). All matrix clangs (>= 3.4) accept it.
      minGccVersion = {
        major = 4;
        minor = 6;
      };
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
      # Don't push ``-no-pie`` into NIX_LDFLAGS: it gets applied
      # to every ld invocation including partial relinks (``ld -r``),
      # where it overrides ``-r`` and forces the linker to emit
      # an executable instead of a relocatable. Packages that do
      # multi-stage linking (busybox: ``applets/built-in.o`` →
      # partial-link then final link) then fail with
      # ``cannot use executable file 'applets/built-in.o' as input``.
      # ``extraHardeningDisable = ["pic"]`` already suppresses the
      # cc-wrapper's automatic ``-pie`` injection, which is what we
      # actually need.
      extraHardeningDisable = [ "pic" ];
    }
    {
      # Use the GCC form for both compilers — clang silently accepts
      # ``-fno-tree-vectorize`` as an alias for ``-fno-vectorize``,
      # but GCC does not accept ``-fno-vectorize``. Packages whose
      # build runs HOSTCC (gcc) on flags meant for the cross CC
      # (e.g. busybox: ``HOSTCC scripts/basic/fixdep`` invokes the
      # native gcc with the matrix's NIX_CFLAGS) reject the
      # clang-only spelling. Keep one spelling that both accept.
      label = "novec";
      cflags = "-fno-tree-vectorize";
      cxxflags = "-fno-tree-vectorize";
    }

    # ── Tier-A/B/C additions ────────────────────────────────────────
    {
      # Link-time optimization (-flto). Restructures function bodies
      # across translation units; produces dramatically different asm.
      label = "lto";
      cflags = "-flto";
      cxxflags = "-flto";
      linkFlags = "-flto";
      # Slim LTO objects are bitcode; plain binutils ``ar``/``ranlib``
      # can't index them ("archive has no index; run ranlib"), so
      # mkVariant points AR/RANLIB/NM at the plugin-aware tools
      # (gcc-ar / llvm-ar). gcc < 4.9 emits fat objects by default and
      # gcc < 4.7 has no gcc-ar at all — mkVariant handles that gate.
      needsLtoTools = true;
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
      linkFlags = "-flto=thin";
      needsLtoTools = true;
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
      # Driver flag (gcc expands it to ``-static -pie --no-dynamic-linker``
      # plus rcrt1.o startfiles) — raw ld doesn't know ``-static-pie``.
      # Appended at link via NIX_CFLAGS_LINK; on a ``-shared`` link the
      # later -static-pie wins (gcc) or conflicts (clang), so configure's
      # shared-library probe fails cleanly and packages fall back to
      # static-only builds — exactly what a static-pie variant wants.
      linkFlags = "-static-pie";
      # Static linking needs the target libc's static archives (libc.a
      # in glibc.static) — without them every configure link-test fails
      # ("cannot find -lc") and feature detection collapses just like
      # the sanitizer case. mkVariant adds them to buildInputs.
      needsStaticLibc = true;
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
      # -fstack-protector-strong: GCC 4.9+, Clang 3.5+. Older compilers
      # reject the flag and configure aborts ("Missing or broken C
      # compiler"). -fstack-protector-all (ssp-all) is ancient and
      # stays ungated.
      minGccVersion = {
        major = 4;
        minor = 9;
      };
      minClangVersion = {
        major = 3;
        minor = 5;
      };
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
      #
      # ``-pie`` must NOT apply to ``-shared`` links: gcc treats
      # pie/shared as a last-wins Negative pair, so an APPENDED -pie
      # overrides -shared and the "shared library" links as a PIE
      # executable (entry _start; downstream "cannot use executable
      # file libfoo.so as input"). Routed through
      # ``extraCflagsBefore`` → NIX_CFLAGS_COMPILE_BEFORE, which the
      # cc-wrapper PREPENDS: executables still get -pie, while a
      # package's own later ``-shared`` wins on gcc and clang ignores
      # -pie whenever -shared/-static is present. (The 18.03-era
      # native wrappers — gcc5, clang 3.7-4 — don't read the BEFORE
      # var; they simply drop -pie, which builds correctly but
      # without the explicit pie link flag.)
      # ``-fPIE`` is prepended too: appended it would override a
      # package's own ``-fPIC`` (also last-wins) on shared-object
      # compiles, producing PIE-grade relocations that break the
      # ``-shared`` link (R_X86_64_PC32 "can not be used when making
      # a shared object", e.g. against asan globals).
      label = "pie";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraCflagsBefore = "-fPIE -pie";
    }
    {
      # ``extraLdflags`` is appended to ``NIX_LDFLAGS`` which the
      # binutils ld-wrapper feeds directly to ``ld``. Use raw ld
      # syntax (``-z relro``) — ``-Wl,...`` is gcc/clang driver
      # syntax and would reach ld as the literal token ``-Wl,...``,
      # producing ``ld: unrecognized option '-Wl'``.
      label = "relro";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-z relro";
    }
    {
      label = "bindnow";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-z now";
    }
    {
      label = "relro-bindnow";
      hardeningEnable = [ ];
      hardeningDisable = [ "all" ];
      extraLdflags = "-z relro -z now";
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
      linkFlags = "";
    }
    {
      label = "san-address";
      cflags = "-fsanitize=address";
      linkFlags = "-fsanitize=address";
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
      linkFlags = "-fsanitize=undefined";
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
      linkFlags = "-fsanitize=memory";
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
      linkFlags = "-fsanitize=thread";
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

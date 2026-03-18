# Cross-compilation support for very old GCC versions (4.4, 4.5, 4.6).
#
# These compilers come from nixpkgs that predate the modern pkgsCross/buildPackages
# infrastructure. To build them as cross-compilers, we call their original nix
# expressions directly with cross-compilation parameters, then wrap the result
# with the modern cc-wrapper.
#
# Two strategies:
#
# 1. nixpkgs-15.09 (gcc4.4, gcc4.6): Call the old gcc expression from the 15.09
#    source tree with `cross` param set. Build with native gccN as build compiler.
#
# 2. nixpkgs-18.03 (gcc4.5): Re-import with crossSystem to get
#    buildPackages.gccN, extract the unwrapped .cc, wrap with modern cc-wrapper.
#
# In both cases, the final result is wrapped with the modern cc-wrapper to get
# correct cross-bintools, cross-libc, and hardening flag handling.

{
  pkgs,
  lib,
}:

let
  # Determine which hardening flags an old GCC doesn't support.
  # The modern cc-wrapper injects these, but old GCC rejects them.
  getGccUnsupportedHardeningFlags =
    version:
    let
      parts = builtins.match "([0-9]+)\\.([0-9]+).*" version;
      major = if parts != null then lib.toInt (builtins.elemAt parts 0) else 0;
      minor = if parts != null then lib.toInt (builtins.elemAt parts 1) else 0;
    in
    # -fstack-protector-strong: added in GCC 4.9
    lib.optional (major < 4 || (major == 4 && minor < 9)) "stackprotector"
    # -fstack-clash-protection: added in GCC 8
    ++ lib.optional (major < 8) "stackclashprotection"
    # -fcf-protection: added in GCC 8
    ++ lib.optional (major < 8) "shadowstack"
    # -fzero-call-used-regs: added in GCC 11
    ++ lib.optional (major < 11) "zerocallusedregs"
    # -ftrivial-auto-var-init: added in GCC 12
    ++ lib.optional (major < 12) "trivialautovarinit"
    # -fstrict-flex-arrays: added in GCC 13
    ++ lib.optional (major < 13) "strictflexarrays1"
    ++ lib.optional (major < 13) "strictflexarrays3";

  # Build an old GCC (from nixpkgs-15.09) as a cross-compiler by calling its
  # original nix expression with cross-compilation parameters.
  #
  # Arguments:
  #   nixpkgsSrc  — the old nixpkgs source (flake input, e.g. nixpkgs-15_09)
  #   gccSubdir   — path within nixpkgs to the gcc expression (e.g. "4.4")
  #   oldPkgs     — the old nixpkgs package set (native, for the build compiler)
  #   attr        — the gcc attribute name in oldPkgs (e.g. "gcc44")
  #   targetPkgs  — the modern cross package set (e.g. pkgsCross.aarch64)
  #   target      — target definition from architectures.nix
  #
  # Returns: a cc-wrapper suitable for overrideCC
  mkCrossGccFromOldExpr =
    {
      nixpkgsSrc,
      gccSubdir,
      oldPkgs,
      attr,
    }:
    targetPkgs: target:
    let
      crossBintools = targetPkgs.buildPackages.binutils;
      # The old gcc expression expects libcCross to have both /include and /lib
      # in a single path, but modern nixpkgs splits glibc into dev and lib outputs.
      # Create a merged directory with symlinks to both.
      glibcLib = targetPkgs.stdenv.cc.libc;
      glibcDev = targetPkgs.stdenv.cc.libc.dev;
      crossLibc = pkgs.runCommand "glibc-merged-${target.crossConfig}" { } ''
        mkdir -p $out
        ln -s ${glibcDev}/include $out/include
        ln -s ${glibcLib}/lib $out/lib
      '';
      nativeGcc = oldPkgs.${attr};

      # The old gcc expressions access various cross.* attributes.
      # gcc4.4: cross.config, cross.arch, cross.gcc.{arch,cpu,abi}
      # gcc4.6: cross.config, cross.arch, cross.libc
      # Provide a minimal cross definition covering both.
      crossTarget = {
        config = target.crossConfig;
        # Extract arch from the triple (first component before '-')
        arch = builtins.head (lib.splitString "-" target.crossConfig);
        # All our targets are Linux/glibc
        libc = "glibc";
      };

      # Create an stdenv that uses the native old GCC as the build compiler.
      # We can't use the old gcc wrapper directly (its setup-hook references
      # $crossConfig which is unset in modern builds). Instead, wrap the old
      # unwrapped gcc binary with the modern wrapCC, giving us a working
      # setup-hook while still using the old compiler for code generation.
      # Add `lib` back since old expressions use `stdenv.lib` which modern
      # stdenv no longer provides.
      # We need to tell the modern wrapper which hardening flags old gcc
      # doesn't support, so it doesn't inject unknown flags.
      nativeVersion = nativeGcc.cc.version or nativeGcc.version or "unknown";
      nativeUnsupportedFlags = getGccUnsupportedHardeningFlags nativeVersion;
      nativeGccWithFlags = nativeGcc.cc // {
        hardeningUnsupportedFlags = nativeUnsupportedFlags;
      };
      modernWrappedNativeGcc = (pkgs.wrapCC nativeGccWithFlags).overrideAttrs (old: {
        # The old gcc builder.sh reads $NIX_CC/nix-support/libc-ldflags-before
        # which doesn't exist in modern cc-wrappers. Create it as empty.
        postFixup = (old.postFixup or "") + ''
          touch $out/nix-support/libc-ldflags-before
        '';
      });
      gcc44Stdenv = (pkgs.overrideCC pkgs.stdenv modernWrappedNativeGcc) // {
        inherit lib;
        # Old expressions access stdenv.isGNU (Hurd detection, removed in modern nixpkgs)
        isGNU = false;
      };

      # Call the old gcc expression directly (NOT via callPackage, which
      # auto-fills arguments and can pass unexpected ones like cloogppl
      # that don't exist in all gcc versions).
      oldGccExpr = import "${nixpkgsSrc}/pkgs/development/compilers/gcc/${gccSubdir}";

      # Introspect what parameters the old expression accepts.
      # gcc4.4 and gcc4.6 have different signatures — gcc4.4 doesn't accept
      # libmpc, libelf, or perl, while gcc4.6 does.
      acceptedArgs = builtins.functionArgs oldGccExpr;

      # Base arguments that all old gcc expressions accept
      baseArgs = {
        stdenv = gcc44Stdenv;
        inherit (pkgs) fetchurl;
        noSysDirs = true;
        cross = crossTarget;
        binutilsCross = crossBintools;
        libcCross = crossLibc;
        crossStageStatic = false;
        texinfo = pkgs.texinfo;
        gmp = pkgs.gmp;
        mpfr = pkgs.mpfr;
        gettext = pkgs.gettext;
        which = pkgs.which;
        zlib = pkgs.zlib;
      };

      # Extra arguments only passed if the expression accepts them
      conditionalArgs =
        lib.optionalAttrs (acceptedArgs ? libmpc) { libmpc = pkgs.libmpc; }
        // lib.optionalAttrs (acceptedArgs ? libelf) { libelf = pkgs.libelf; }
        // lib.optionalAttrs (acceptedArgs ? perl) { perl = pkgs.perl; }
        // lib.optionalAttrs (acceptedArgs ? cloog) { cloog = null; };

      # gcc4.6's builder.sh calls `paxmark` (PaX hardening tool) without
      # checking if it exists. Provide a no-op script so the build succeeds.
      fakePaxmark = pkgs.writeShellScriptBin "paxmark" "exit 0";

      gccCrossUnwrapped = (oldGccExpr (baseArgs // conditionalArgs)).overrideAttrs (old: {
        nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ fakePaxmark ];
        # The old expression produces configureFlags as a string, but modern
        # mkDerivation expects a list. Convert it.
        configureFlags =
          if builtins.isString (old.configureFlags or [ ]) then
            builtins.filter (s: s != "") (builtins.map lib.trim (lib.splitString "\n" old.configureFlags))
          else
            old.configureFlags;
        # Remove crossAttrs — legacy mechanism from old nixpkgs that modern
        # mkDerivation doesn't understand (tries to coerce attrset to string).
        crossAttrs = null;
        # Old gcc source code is missing #include <limits.h> in several files
        # (e.g. libiberty/fibheap.c uses LONG_MIN). Modern glibc no longer
        # provides these implicitly. Force-include limits.h and stdlib.h.
        # Also disable _FORTIFY_SOURCE which causes warnings with old code.
        hardeningDisable = [
          "all"
        ];
        # Old gcc source code is missing #include <limits.h> in several files
        # (e.g. libiberty/fibheap.c uses LONG_MIN without including it).
        # Modern glibc no longer provides these implicitly. Inject via
        # NIX_CFLAGS_COMPILE so all compilation commands pick them up.
        NIX_CFLAGS_COMPILE = "-include limits.h";
        # Modern glibc (2.34+) removed `struct ucontext` in favor of
        # `ucontext_t`. Old gcc source uses `struct ucontext` in
        # platform-specific unwind headers. Fix with sed in postPatch.
        postPatch = (old.postPatch or "") + ''
          find gcc/config -name 'linux-unwind.h' -exec \
            sed -i 's/struct ucontext/ucontext_t/g' {} +
        '';
        # Modern texinfo is incompatible with old gcc 4.x texi files.
        # Disable info doc generation by replacing makeinfo with `true`.
        makeFlags = (old.makeFlags or [ ]) ++ [ "MAKEINFO=true" ];
        # The old cross-gcc invokes `as` from its internal search path
        # (${prefix}/${target}/bin/). Symlink the cross-bintools there
        # so the cross-gcc can find the cross-assembler and cross-linker.
        postFixup = (old.postFixup or "") + ''
          mkdir -p $out/${target.crossConfig}/bin
          for tool in ${crossBintools}/bin/${target.crossConfig}-*; do
            name=$(basename "$tool")
            # Strip the target prefix to get the bare tool name (e.g. "as")
            bare=''${name#${target.crossConfig}-}
            ln -sf "$tool" "$out/${target.crossConfig}/bin/$bare"
          done
        '';
        # Override meta to avoid stdenv.lib.licenses reference failure
        meta = {
          description = "GCC ${old.version or "old"} cross-compiler for ${target.crossConfig}";
          license = lib.licenses.gpl3Plus;
          platforms = lib.platforms.linux;
        };
      });

      # Get the version from the unwrapped cross GCC
      version = nativeGcc.cc.version or nativeGcc.version or "unknown";
      unsupportedFlags = getGccUnsupportedHardeningFlags version;

      # Wrap with the modern cc-wrapper using the hybrid wrapper approach:
      # take the modern cross GCC wrapper as template, swap in our old cross-GCC binary.
      modernCrossGcc = targetPkgs.buildPackages.gcc;
      hybridGcc = modernCrossGcc.override {
        cc = gccCrossUnwrapped // {
          hardeningUnsupportedFlags = unsupportedFlags;
        };
      };
    in
    targetPkgs.overrideCC targetPkgs.stdenv hybridGcc;

  # Build a cross-compiler from nixpkgs-18.03 using re-import with crossSystem.
  # The old nixpkgs has buildPackages but its cc-wrapper is broken with modern
  # stdenv ($crossConfig unbound). So we extract the unwrapped .cc and wrap it
  # with the modern cc-wrapper.
  mkCrossGccFrom1803 =
    {
      nixpkgsSrc,
      system,
      oldPkgs,
      attr,
    }:
    targetPkgs: target:
    let
      # Re-import old nixpkgs with crossSystem
      oldCrossPkgs = import nixpkgsSrc {
        inherit system;
        crossSystem = {
          config = target.crossConfig;
        };
        config.allowUnfree = true;
      };

      # Check if the cross-compiler is available
      crossAvailable = builtins.tryEval (
        oldCrossPkgs ? buildPackages
        && oldCrossPkgs.buildPackages ? ${attr}
        && oldCrossPkgs.buildPackages.${attr} ? cc
        && oldCrossPkgs.buildPackages.${attr}.cc.name != ""
      );

      # Get the unwrapped cross-GCC from the old nixpkgs.
      # Override it to: (1) use native gccN as build compiler (same-version
      # bootstrap), and (2) fix source incompatibilities with modern compilers.
      oldCrossGccWrapped = oldCrossPkgs.buildPackages.${attr};
      oldCrossGccUnwrapped = oldCrossGccWrapped.cc.overrideAttrs (old: {
        # Use native gccN of the same version as build compiler, not
        # 18.03's default gcc7 which rejects old source code.
        depsBuildBuild = [ oldPkgs.${attr} ];
        # Fix cfns.gperf gnu_inline attribute inconsistency (rejected by gcc 7+):
        # The generated cfns.h has __attribute__((gnu_inline)) on the definition
        # but not the declaration. Fix by regenerating or patching.
        postPatch = (old.postPatch or "") + ''
          if [ -f gcc/cp/cfns.gperf ]; then
            # Add gnu_inline to the lookup function declaration
            sed -i 's/^__inline$//' gcc/cp/cfns.gperf 2>/dev/null || true
          fi
          # Also fix the pre-generated cfns.h if it exists
          if [ -f gcc/cp/cfns.h ]; then
            sed -i '/^__attribute__.*gnu_inline/d' gcc/cp/cfns.h 2>/dev/null || true
          fi
        '';
      });

      version = oldCrossGccUnwrapped.version or oldCrossGccWrapped.version or "unknown";
      unsupportedFlags = getGccUnsupportedHardeningFlags version;

      # Use modern cc-wrapper (hybrid approach)
      modernCrossGcc = targetPkgs.buildPackages.gcc;
      hybridGcc = modernCrossGcc.override {
        cc = oldCrossGccUnwrapped // {
          hardeningUnsupportedFlags = unsupportedFlags;
        };
      };
    in
    if !(crossAvailable.success && crossAvailable.value) then
      builtins.throw "${attr}: cross-compiler not available in nixpkgs-18.03 for ${target.label}"
    else
      targetPkgs.overrideCC targetPkgs.stdenv hybridGcc;

in
{
  inherit
    getGccUnsupportedHardeningFlags
    mkCrossGccFromOldExpr
    mkCrossGccFrom1803
    ;
}

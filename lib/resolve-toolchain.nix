# Resolve one toolchain for both package builds and offline closure enumeration.
{ pkgs, lib }:
{ compiler, target, capture ? null }:
let
  archLib = import ./architectures.nix { };

  # Get the pkgs set for this target (native or cross)
  targetPkgs =
    let
      p = archLib.getPkgsForTarget pkgs target;
    in
    if p == null then builtins.throw "cross target ${target.label} not available" else p;

  # Build the custom stdenv using the compiler's mkStdenv
  baseStdenv = compiler.mkStdenv targetPkgs target;

  # GCC 5's AArch64 compiler works with the selected modern sysroot, but its
  # original binutils 2.28 cannot read glibc's SHT_RELR sections. Keep GCC and
  # libc intact and use the target's existing modern bintools wrapper.
  legacyCrossLto=compiler.family=="gcc" && lib.versionAtLeast compiler.version "4.6" && lib.versionOlder compiler.version "4.7" && target.label!="x86_64";
  gcc5Mips=compiler.family=="gcc" && lib.versionAtLeast compiler.version "5" && lib.versionOlder compiler.version "6" && target.label=="mipsel";
  # GCC 5's configured cross linker can bypass Nix's bintools wrapper. Restore
  # that selected wrapper so setup-hook link flags, including NIX_SET_BUILD_ID,
  # reach the real linker. Old collect2 can retain its configured linker even
  # under -B, so use the cached target collect2. GCC 5 still supplies its own
  # frontend, LTO plugin and lto-wrapper through the original driver arguments.
  gcc5Cross=compiler.family=="gcc" && lib.versionAtLeast compiler.version "5" && lib.versionOlder compiler.version "6" && target.label!="x86_64";
  linkerCompatibleStdenv = if legacyCrossLto || gcc5Cross then
    (import ./capture-stdenv.nix {pkgs=targetPkgs;}) {
      stdenv=baseStdenv;
      cc=(import ./gcc-driver-compat.nix {inherit pkgs;}) {
        cc=baseStdenv.cc;
        bintools=if target.label=="aarch64" then targetPkgs.stdenv.cc.bintools else baseStdenv.cc.bintools;
        linkerCompiler=if legacyCrossLto then baseStdenv.cc.cc else targetPkgs.stdenv.cc.cc;
        runtimeCompiler=if gcc5Mips then targetPkgs.stdenv.cc.cc else null;
        arguments=lib.optional (target.label=="mipsel") "-mabi=32";
      };
    }
    else baseStdenv;

  # Fix clang ppc64: two issues with old clang + ppc64 ELFv2 target:
  # 1. Linker: clang normalizes powerpc64-unknown-linux-gnuabielfv2 to
  #    powerpc64-unknown-linux-gnu internally, then can't find the cross linker.
  #    Fix: inject -fuse-ld=<path> (clang >=3.9) or create a ld symlink dir
  #    with the normalized triple name (clang <3.9).
  # 2. ABI: old clang versions (<18) from old nixpkgs default to ELFv1 for ppc64,
  #    but the sysroot/glibc is ELFv2. Fix: inject -mabi=elfv2.
  # Both fixes use overrideAttrs+postFixup (not override { extraBuildCommands })
  # to avoid replacing the resource-root setup.

  # Parse clang major version for version-gated fixes.
  clangMajor =
    let
      parts = builtins.match "([0-9]+)\\..*" (compiler.version or "0");
    in
    if parts != null then lib.toInt (builtins.head parts) else 0;

  powerpcStdenv =
    if compiler.family == "clang" && target.label == "ppc64" then
      let
        cc = linkerCompatibleStdenv.cc;
        ldPath = "${cc.bintools}/bin/${cc.bintools.targetPrefix}ld";
        # clang <3.9 doesn't support -fuse-ld=<absolute-path>. Instead, create
        # a directory with a symlink using the normalized triple name that clang
        # searches for (powerpc64-unknown-linux-gnu-ld).
        ldSymlinkFix = ''
          mkdir -p $out/ppc64-ld-fix
          ln -s ${ldPath} $out/ppc64-ld-fix/powerpc64-unknown-linux-gnu-ld
          echo "-B$out/ppc64-ld-fix" >> $out/nix-support/cc-cflags
        '';
        # clang >=3.9 supports -fuse-ld=<absolute-path>.
        ldFuseFix = ''
          echo "-fuse-ld=${ldPath}" >> $out/nix-support/cc-cflags
        '';
        fixedCC = cc.overrideAttrs (old: {
          postFixup = (old.postFixup or "") + ''
            ${if clangMajor < 4 then ldSymlinkFix else ldFuseFix}
            echo "-mabi=elfv2" >> $out/nix-support/cc-cflags
          '';
        });
      in
      targetPkgs.overrideCC linkerCompatibleStdenv fixedCC
    else
      linkerCompatibleStdenv;

  legacyGold=compiler.family=="clang" && lib.versionAtLeast compiler.version "3.7" && lib.versionOlder compiler.version "5";
  riscvLtoAbi=compiler.family=="clang" && clangMajor>=9 && clangMajor<15 && target.label=="riscv64";
  mipsTarget=builtins.elem target.label ["mipsel" "mips64el"];
  legacyBfd=legacyGold && (mipsTarget || target.label=="ppc64");
  linkerTargetAbi=if target.label=="ppc64" then "elfv2" else if target.label=="mips64el" then "n32" else "";
  ltoCompatibleStdenv = if compiler.family=="clang" && (clangMajor==16 || legacyGold || riscvLtoAbi || linkerTargetAbi!="") then
    (import ./capture-stdenv.nix {pkgs=targetPkgs;}) {
      stdenv=powerpcStdenv;
      cc=(import ./clang-lto-linker.nix {inherit pkgs;}) {
        cc=powerpcStdenv.cc;
        lld=if clangMajor==16 && !mipsTarget then compiler.mkLld targetPkgs target else null;
        gold=if legacyGold && !legacyBfd then compiler.mkGold targetPkgs target else null;
        bfd=if clangMajor==16 && mipsTarget then powerpcStdenv.cc.bintools.bintools
          else if legacyBfd then compiler.mkGold targetPkgs target else null;
        riscvAbi=riscvLtoAbi;
        targetAbi=linkerTargetAbi;
      };
    } else powerpcStdenv;

  compatibleStdenv = if compiler.family=="clang" && lib.versionOlder compiler.version "3.9" then
    (import ./capture-stdenv.nix {pkgs=targetPkgs;}) {
      stdenv=ltoCompatibleStdenv;
      cc=(import ./legacy-clang-response.nix {inherit pkgs;}) ltoCompatibleStdenv.cc;
    }
    else ltoCompatibleStdenv;

  captureTool = if capture != null && capture ? captureTool then capture.captureTool
    else pkgs.buildPackages.callPackage ../nix/compiler-capture.nix {};
  cc = compatibleStdenv.cc;
  prefix = cc.targetPrefix or "";
  c = if compiler.family == "gcc" then "gcc" else "clang";
  cxx = if compiler.family == "gcc" then "g++" else "clang++";
  entry = name: real: { inherit name; driver = name; executable = "${cc.cc}/bin/${real}"; };
  wrappedCC = (import ./compiler-capture.nix { inherit pkgs captureTool; }) {
    inherit cc;
    inherit (compiler) family version;
    target = compatibleStdenv.hostPlatform.config;
    backend = "${compiler.family}-trace";
    debugPolicy = capture.debugPolicy or "required";
    entries = [
      (entry "${prefix}${c}" (if compiler.family == "gcc" then "${prefix}${c}" else c))
      (entry "${prefix}${cxx}" (if compiler.family == "gcc" then "${prefix}${cxx}" else cxx))
      (entry "${prefix}cc" (if compiler.family == "gcc" then "${prefix}${c}" else c))
      (entry "${prefix}c++" (if compiler.family == "gcc" then "${prefix}${cxx}" else cxx))
      ((entry "${prefix}cpp" (if compiler.family == "gcc" then "${prefix}cpp" else "cpp")) // { optional = true; })
    ];
  };
  stdenv = if capture == null then compatibleStdenv else
    (import ./capture-stdenv.nix { pkgs = targetPkgs; }) { stdenv = compatibleStdenv; cc = wrappedCC; };
in { inherit targetPkgs stdenv; }

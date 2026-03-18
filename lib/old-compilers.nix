# Old compiler discovery from legacy nixpkgs inputs.
# Produces compiler entries in the same shape as compilers.nix.
#
# Takes a list of { oldPkgs, gccSpecs, clangSpecs, nixpkgsSrc?, system? }
# records describing which compiler versions to extract from each old nixpkgs.
#
# Cross-compilation strategies (in order of preference):
#
# 1. pkgsCross available (nixpkgs 22.11+): use buildPackages.<compiler>
#    directly. For GCC, override depsBuildBuild for version-matched bootstrap.
#
# 2. No pkgsCross but nixpkgsSrc + buildPackages available (nixpkgs 18.03):
#    re-import with crossSystem, extract unwrapped .cc from the old cross-gcc,
#    wrap with modern cc-wrapper (hybrid approach — avoids broken old wrappers).
#
# 3. No pkgsCross, no buildPackages but nixpkgsSrc + gccSubdir (nixpkgs 15.09):
#    call the old gcc expression directly with cross params, build using native
#    gccN as the build compiler. Wrap result with modern cc-wrapper.
#
# Uses tryEval for safety — gracefully skips compilers that fail evaluation.
# Uses explicit spec lists rather than auto-discovery (old nixpkgs attr names vary).
#
# Returns: { gcc = [ ... ]; clang = [ ... ]; all = [ ... ]; }

{
  pkgs,
  lib,
  oldNixpkgsSets,
}:

let
  archLib = import ./architectures.nix { };
  oldGccCross = import ./old-gcc-cross.nix { inherit pkgs lib; };

  # Extract version string from a Clang package across different nixpkgs eras.
  # Modern: .clang.version exists
  # Old (3.7+): .clang.cc.name is "clang-X.Y.Z", extract version from name
  # Very old (3.4-3.5): .clang.name is "clang-X.Y.Z" (no wrapper)
  extractClangVersion =
    llvmPkg:
    let
      clang = llvmPkg.clang;
      # Modern path: .clang.version (nixpkgs 22.11+)
      modern =
        if clang ? version then
          builtins.tryEval clang.version
        else
          {
            success = false;
            value = null;
          };
      # Old wrapper path: .clang.cc.name = "clang-X.Y.Z"
      oldCC =
        if (clang ? cc) && (clang.cc ? name) then
          builtins.tryEval clang.cc.name
        else
          {
            success = false;
            value = null;
          };
      # Very old path: .clang.name = "clang-X.Y.Z" or "clang-wrapper-X.Y.Z"
      oldName =
        if clang ? name then
          builtins.tryEval clang.name
        else
          {
            success = false;
            value = null;
          };
      # Extract version from "clang-X.Y.Z" or "clang-wrapper-X.Y.Z"
      extractFromName =
        name:
        let
          m1 = builtins.match "clang-wrapper-(.*)" name;
          m2 = builtins.match "clang-(.*)" name;
        in
        if m1 != null then
          builtins.head m1
        else if m2 != null then
          builtins.head m2
        else
          name;
    in
    if modern.success then
      modern.value
    else if oldName.success && oldName.value != "" then
      extractFromName oldName.value
    else if oldCC.success && oldCC.value != "" then
      extractFromName oldCC.value
    else
      null;

  # Extract the CC wrapper from an old LLVM package for use with overrideCC.
  # Modern (5+): .stdenv.cc exists and is a proper cc-wrapper
  # Old (3.6-4): .stdenv.cc exists but may be named differently
  # Very old (3.4-3.5): no .stdenv.cc, use .clang directly
  extractClangCC =
    llvmPkg:
    let
      hasStdenvCC = (llvmPkg ? stdenv) && (llvmPkg.stdenv ? cc) && (llvmPkg.stdenv.cc ? name);
      stdenvCCName =
        if hasStdenvCC then
          builtins.tryEval llvmPkg.stdenv.cc.name
        else
          {
            success = false;
            value = "";
          };
    in
    if stdenvCCName.success && stdenvCCName.value != "" then
      llvmPkg.stdenv.cc
    else if llvmPkg ? clang then
      llvmPkg.clang
    else
      null;

  # Extract the unwrapped clang binary from an old LLVM package.
  # Modern (3.7+): .clang.cc is the unwrapped clang (e.g. "clang-3.7.1")
  # Very old (3.4-3.5): .clang IS the raw binary (no .cc, or .cc is gcc)
  extractUnwrappedClang =
    llvmPkg:
    let
      clang = llvmPkg.clang;
      hasCC = clang ? cc;
      ccName = if hasCC then (builtins.tryEval clang.cc.name).value or "" else "";
      ccIsClang = builtins.match "clang-.*" ccName != null;
    in
    if hasCC && ccIsClang then
      clang.cc # 3.7, 3.8, 3.9, 4.0: .cc is the unwrapped clang
    else
      clang; # 3.4, 3.5: .clang itself is the raw binary

  # Determine which hardening flags an old clang doesn't support.
  # These flags are injected by the modern cc-wrapper but old clangs reject them.
  getClangUnsupportedHardeningFlags =
    version:
    let
      parts = builtins.match "([0-9]+)\\.([0-9]+).*" version;
      major = if parts != null then lib.toInt (builtins.elemAt parts 0) else 0;
      minor = if parts != null then lib.toInt (builtins.elemAt parts 1) else 0;
    in
    # -fstack-protector-strong: added in clang 3.5
    lib.optional (major < 3 || (major == 3 && minor < 5)) "stackprotector"
    # -fstack-clash-protection: added in clang 11
    ++ lib.optional (major < 11) "stackclashprotection"
    # -fzero-call-used-regs: added in clang 16
    ++ lib.optional (major < 16) "zerocallusedregs";

  # Whether an old clang needs -fmacro-prefix-map stripped from the wrapper.
  # -fmacro-prefix-map was added in clang 10.
  clangNeedsMacroPrefixMapStripped =
    version:
    let
      parts = builtins.match "([0-9]+)\\..*" version;
      major = if parts != null then lib.toInt (builtins.head parts) else 0;
    in
    major < 10;

  # Build commands to strip -fmacro-prefix-map flags from nix-support files.
  # Old clangs (<10) don't support this flag.
  stripMacroPrefixMapCommands = ''
    for f in $out/nix-support/cc-cflags $out/nix-support/libc-cflags $out/nix-support/libcxx-cxxflags; do
      if [ -f "$f" ]; then
        sed -i "s/ -fmacro-prefix-map=[^ ]*//g" "$f"
      fi
    done
    sed -i '/-fmacro-prefix-map/d' $out/nix-support/setup-hook
  '';

  # Get cross-compiler from old nixpkgs for a given target.
  # For pre-pkgsCross nixpkgs, re-imports with crossSystem to get
  # buildPackages which contain the cross-compilers.
  #
  # Returns the cross-imported pkgs set with buildPackages, or null
  # if cross-compilation is not possible.
  getOldCrossPkgs =
    {
      oldPkgs,
      nixpkgsSrc ? null,
      system ? null,
    }:
    target:
    if oldPkgs ? pkgsCross then
      # Modern nixpkgs: use pkgsCross directly (returns null if cross attr missing)
      archLib.getPkgsForTarget oldPkgs target
    else if nixpkgsSrc != null && system != null then
      # Pre-pkgsCross nixpkgs: re-import with crossSystem
      import nixpkgsSrc {
        inherit system;
        crossSystem = {
          config = target.crossConfig;
        };
        config.allowUnfree = true;
      }
    else
      null;

  # Build a compiler entry for an old GCC version.
  # Uses the old nixpkgs' GCC but injects it into the current nixpkgs' stdenv,
  # so we get old compiler + current libc/binutils.
  #
  # For cross-compilation with pkgsCross (22.11+): overrides depsBuildBuild
  # on the unwrapped cross GCC to use the native GCC of the same version.
  #
  # For cross-compilation with nixpkgsSrc + buildPackages (18.03): re-import
  # with crossSystem, extract unwrapped .cc, wrap with modern cc-wrapper.
  #
  # For cross-compilation with nixpkgsSrc + gccSubdir (15.09): call old gcc
  # expression directly with cross params, wrap with modern cc-wrapper.
  mkOldGccEntry =
    nixpkgsInfo: # { oldPkgs, nixpkgsSrc?, system? }
    {
      attr,
      label,
      gccSubdir ? null,
    }:
    let
      oldPkgs = nixpkgsInfo.oldPkgs;
      tried = builtins.tryEval (oldPkgs.${attr}.cc.version or oldPkgs.${attr}.version);
    in
    if !tried.success then
      null
    else
      {
        name = attr;
        family = "gcc";
        label = "gcc${label}";
        version = tried.value;
        mkStdenv =
          targetPkgs: target:
          if target.crossAttr == null && !(target ? crossSystem) then
            # Native: just use the old compiler directly
            targetPkgs.overrideCC targetPkgs.stdenv oldPkgs.${attr}
          else if oldPkgs ? pkgsCross then
            # pkgsCross available (22.11+): use buildPackages with depsBuildBuild bootstrap
            let
              oldCrossPkgs = getOldCrossPkgs nixpkgsInfo target;
            in
            if oldCrossPkgs == null then
              builtins.throw "${attr}: cross target ${target.label} not available in this nixpkgs"
            else
              let
                oldCrossGcc = oldCrossPkgs.buildPackages.${attr};
                bootstrappedCC = oldCrossGcc.cc.overrideAttrs (old: {
                  depsBuildBuild = [ oldPkgs.${attr} ];
                  # Disable libsanitizer for old cross GCC — its struct stat
                  # definitions can mismatch the cross glibc headers (e.g. gcc12/mips64el).
                  configureFlags = (old.configureFlags or [ ]) ++ [ "--disable-libsanitizer" ];
                });
                rewrapped = oldCrossGcc.override { cc = bootstrappedCC; };
              in
              targetPkgs.overrideCC targetPkgs.stdenv rewrapped
          else if nixpkgsInfo.nixpkgsSrc != null && nixpkgsInfo.system != null && gccSubdir != null then
            # Pre-buildPackages (15.09): call old gcc expression directly with
            # cross params, build using native gccN, wrap with modern cc-wrapper.
            oldGccCross.mkCrossGccFromOldExpr {
              nixpkgsSrc = nixpkgsInfo.nixpkgsSrc;
              inherit gccSubdir oldPkgs attr;
            } targetPkgs target
          else if nixpkgsInfo.nixpkgsSrc != null && nixpkgsInfo.system != null then
            # Pre-pkgsCross but has buildPackages (18.03): re-import with
            # crossSystem, extract unwrapped .cc, wrap with modern cc-wrapper.
            oldGccCross.mkCrossGccFrom1803 {
              nixpkgsSrc = nixpkgsInfo.nixpkgsSrc;
              system = nixpkgsInfo.system;
              inherit oldPkgs attr;
            } targetPkgs target
          else
            builtins.throw "${attr}: cross-compilation not supported (no pkgsCross and no nixpkgsSrc)";
      };

  # Build a compiler entry for an old Clang/LLVM version.
  # Handles multiple eras of LLVM packaging in nixpkgs:
  # - Modern (5+): .clang.version and .stdenv.cc
  # - Old (3.6-4): .stdenv.cc exists, version from .clang.cc.name
  # - Very old (3.4-3.5): only .clang, version from .clang.name
  #
  # Cross-compilation strategies:
  # 1. pkgsCross available (22.11+): use buildPackages.<llvmPkg>.clang
  # 2. Pre-pkgsCross (18.03): hybrid wrapper — modern cross cc-wrapper with
  #    the old unwrapped clang binary swapped in. The old nixpkgs' own cross
  #    infrastructure has broken C++ stdlib hooks, so we bypass it entirely.
  mkOldClangEntry =
    nixpkgsInfo: # { oldPkgs, nixpkgsSrc?, system? }
    { attr, label }:
    let
      oldPkgs = nixpkgsInfo.oldPkgs;
      llvmPkg = oldPkgs.${attr};
      tried = builtins.tryEval llvmPkg;
      version = if tried.success then extractClangVersion llvmPkg else null;
    in
    if !tried.success || version == null then
      null
    else
      {
        name = attr;
        family = "clang";
        label = "clang${label}";
        inherit version;
        mkStdenv =
          targetPkgs: target:
          if target.crossAttr == null && !(target ? crossSystem) then
            # Native: use extractClangCC on the native LLVM package
            let
              cc = extractClangCC oldPkgs.${attr};
            in
            targetPkgs.overrideCC targetPkgs.stdenv cc
          else if oldPkgs ? pkgsCross then
            # pkgsCross available (22.11+): get cross-clang from buildPackages.
            # Use .clang directly (not extractClangCC) because
            # buildPackages.llvmPkg.stdenv.cc is the native compiler,
            # while .clang is the actual cross-compiler wrapper.
            let
              oldCrossPkgs = getOldCrossPkgs nixpkgsInfo target;
            in
            if oldCrossPkgs == null then
              builtins.throw "${attr}: cross target ${target.label} not available in this nixpkgs"
            else
              targetPkgs.overrideCC targetPkgs.stdenv oldCrossPkgs.buildPackages.${attr}.clang
          else
            # Pre-pkgsCross (18.03, 15.09): hybrid wrapper approach.
            # The old nixpkgs' cross infrastructure has broken C++ stdlib
            # hooks that reference cross-compiled GCC binaries. Instead,
            # take the modern cross clang wrapper and swap in the old
            # unwrapped clang binary. This gives us correct cross bintools
            # and sysroot from modern nixpkgs + old clang codegen.
            let
              unwrappedClang = extractUnwrappedClang oldPkgs.${attr};
              unsupportedFlags = getClangUnsupportedHardeningFlags version;
              needsStripMacroMap = clangNeedsMacroPrefixMapStripped version;

              # Use the modern cross clang wrapper as a template
              modernCrossClang = targetPkgs.buildPackages.llvmPackages.clang;
              hybridClang = modernCrossClang.override {
                cc = unwrappedClang // {
                  hardeningUnsupportedFlags = unsupportedFlags;
                };
                propagateDoc = false;
                extraBuildCommands = if needsStripMacroMap then stripMacroPrefixMapCommands else "";
              };
            in
            targetPkgs.overrideCC targetPkgs.stdenv hybridClang;
      };

  # Process one old nixpkgs set into compiler entries.
  processOldNixpkgs =
    {
      oldPkgs,
      gccSpecs,
      clangSpecs,
      nixpkgsSrc ? null,
      system ? null,
    }:
    let
      nixpkgsInfo = { inherit oldPkgs nixpkgsSrc system; };
      gccEntries = builtins.filter (x: x != null) (map (mkOldGccEntry nixpkgsInfo) gccSpecs);
      clangEntries = builtins.filter (x: x != null) (map (mkOldClangEntry nixpkgsInfo) clangSpecs);
    in
    {
      gcc = gccEntries;
      clang = clangEntries;
    };

  allResults = map processOldNixpkgs oldNixpkgsSets;

  mergedGcc = lib.concatMap (r: r.gcc) allResults;
  mergedClang = lib.concatMap (r: r.clang) allResults;

  # Deduplicate by label — if the same label appears multiple times
  # (e.g. gcc48 from both 18.03 and 22.11), keep the first occurrence.
  dedupByLabel =
    comps:
    builtins.attrValues (
      builtins.listToAttrs (
        lib.reverseList (
          map (c: {
            name = c.label;
            value = c;
          }) comps
        )
      )
    );

  dedupGcc = dedupByLabel mergedGcc;
  dedupClang = dedupByLabel mergedClang;

in
{
  gcc = dedupGcc;
  clang = dedupClang;
  all = dedupGcc ++ dedupClang;
}

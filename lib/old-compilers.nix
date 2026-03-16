# Old compiler discovery from legacy nixpkgs inputs.
# Produces compiler entries in the same shape as compilers.nix.
#
# Takes a list of { oldPkgs, gccSpecs, clangSpecs } records describing which
# compiler versions to extract from each old nixpkgs input.
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

  # Build a compiler entry for an old GCC version.
  # Uses the old nixpkgs' GCC but injects it into the current nixpkgs' stdenv,
  # so we get old compiler + current libc/binutils.
  mkOldGccEntry =
    oldPkgs:
    { attr, label }:
    let
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
          let
            oldTargetPkgs = archLib.getPkgsForTarget oldPkgs target;
          in
          targetPkgs.overrideCC targetPkgs.stdenv oldTargetPkgs.${attr};
      };

  # Build a compiler entry for an old Clang/LLVM version.
  # Handles multiple eras of LLVM packaging in nixpkgs:
  # - Modern (5+): .clang.version and .stdenv.cc
  # - Old (3.6-4): .stdenv.cc exists, version from .clang.cc.name
  # - Very old (3.4-3.5): only .clang, version from .clang.name
  mkOldClangEntry =
    oldPkgs:
    { attr, label }:
    let
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
          let
            oldTargetPkgs = archLib.getPkgsForTarget oldPkgs target;
            oldLlvmPkg = oldTargetPkgs.${attr};
            cc = extractClangCC oldLlvmPkg;
          in
          targetPkgs.overrideCC targetPkgs.stdenv cc;
      };

  # Process one old nixpkgs set into compiler entries.
  processOldNixpkgs =
    {
      oldPkgs,
      gccSpecs,
      clangSpecs,
    }:
    let
      gccEntries = builtins.filter (x: x != null) (map (mkOldGccEntry oldPkgs) gccSpecs);
      clangEntries = builtins.filter (x: x != null) (map (mkOldClangEntry oldPkgs) clangSpecs);
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

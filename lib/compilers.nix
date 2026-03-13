# Dynamic compiler discovery.
# Enumerates all working GCC and Clang versions in the given pkgs set.
#
# Returns: { gcc = [ ... ]; clang = [ ... ]; all = [ ... ]; }
# Each entry: { name, family, label, version, mkStdenv }
#   mkStdenv : pkgs -> stdenv  (works for both native and cross pkgs)
#
# When a compiler version matches the default stdenv compiler, mkStdenv
# returns the default stdenv directly. This ensures cross-compilation
# hits the binary cache instead of rebuilding the cross toolchain.

{ pkgs }:

let
  lib = pkgs.lib;

  # The default GCC version used by stdenv
  defaultGccVersion = pkgs.stdenv.cc.version;

  # ── GCC discovery ────────────────────────────────────────────────────────
  gccCandidates = builtins.filter (n: builtins.match "gcc([0-9]+)" n != null) (
    builtins.attrNames pkgs
  );

  gccAvailable = builtins.filter (
    n:
    let
      tried = builtins.tryEval (pkgs.${n}.cc.version or pkgs.${n}.version);
    in
    tried.success
  ) gccCandidates;

  mkGccEntry =
    name:
    let
      version = pkgs.${name}.cc.version or pkgs.${name}.version;
      vnum = lib.removePrefix "gcc" name;
      isDefault = version == defaultGccVersion;
    in
    {
      inherit name version;
      family = "gcc";
      label = "gcc${vnum}";
      # Use default stdenv when this IS the default GCC (cache-friendly)
      mkStdenv =
        targetPkgs:
        if isDefault then targetPkgs.stdenv else targetPkgs.overrideCC targetPkgs.stdenv targetPkgs.${name};
    };

  # ── Clang/LLVM discovery ─────────────────────────────────────────────────
  llvmCandidates = builtins.filter (n: builtins.match "llvmPackages_([0-9]+)" n != null) (
    builtins.attrNames pkgs
  );

  llvmAvailable = builtins.filter (
    n:
    let
      tried = builtins.tryEval (pkgs.${n}.stdenv);
    in
    tried.success
  ) llvmCandidates;

  mkClangEntry =
    name:
    let
      version = pkgs.${name}.clang.version;
      vnum = lib.removePrefix "llvmPackages_" name;
    in
    {
      inherit name version;
      family = "clang";
      label = "clang${vnum}";
      mkStdenv = targetPkgs: targetPkgs.${name}.stdenv;
    };

in
{
  gcc = map mkGccEntry gccAvailable;
  clang = map mkClangEntry llvmAvailable;
  all = (map mkGccEntry gccAvailable) ++ (map mkClangEntry llvmAvailable);
}

# Build one variant: (package, compiler, architecture, opt-level, flag-set, hardening).
#
# Returns: { variantLabel, variantPkg, meta }

{ pkgs, lib }:

{
  pkg, # { attr, label } from packages.nix
  compiler, # { name, family, label, version, mkStdenv } from compilers.nix
  target, # { label, crossAttr, system } from architectures.nix
  optLevel, # { flag, label, clangOnly } from flags.nix
  flagSet, # { label, cflags, cxxflags, ... } from flags.nix
  hardening, # { label, hardeningDisable } from flags.nix
}:

let
  archLib = import ./architectures.nix { };

  # Get the pkgs set for this target (native or cross)
  targetPkgs = archLib.getPkgsForTarget pkgs target;

  # Build the custom stdenv using the compiler's mkStdenv
  customStdenv = compiler.mkStdenv targetPkgs;

  # Resolve compiler-specific flags (e.g., novec differs between gcc and clang)
  resolvedCflags =
    if flagSet.cflags == null then
      (if compiler.family == "gcc" then flagSet.gccFlag else flagSet.clangFlag)
    else
      flagSet.cflags;

  resolvedCxxflags =
    if flagSet.cxxflags == null then
      (if compiler.family == "gcc" then flagSet.gccFlag else flagSet.clangFlag)
    else
      flagSet.cxxflags;

  # Extra linker flags from the flag set (e.g., -Wl,--gc-sections, -no-pie)
  extraLdflags = flagSet.ldflags or "";

  # Extra hardening flags to disable for this flag set (e.g., pie for nopic)
  extraHardeningDisable = flagSet.extraHardeningDisable or [ ];

  # Combine optimization level + extra flags
  allCflags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      optLevel.flag
      resolvedCflags
    ]
  );

  allCxxflags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      optLevel.flag
      resolvedCxxflags
    ]
  );

  # Variant label encodes the full combination
  variantLabel = lib.concatStringsSep "-" [
    pkg.label
    target.label
    compiler.label
    optLevel.label
    flagSet.label
    hardening.label
  ];

  # Override the package with our custom stdenv and flags
  basePkg = targetPkgs.${pkg.attr}.override { stdenv = customStdenv; };

  # Combine hardening disables from the hardening mode + flag set
  allHardeningDisable =
    if hardening.hardeningDisable == [ "all" ] then
      [ "all" ]
    else
      hardening.hardeningDisable ++ extraHardeningDisable;

  variantPkg = basePkg.overrideAttrs (
    old:
    let
      # Some packages use env.NIX_CFLAGS_COMPILE (newer pattern), others use the
      # top-level attribute (legacy). We must place our flags where the package expects.
      inEnv = (old.env or { }) ? NIX_CFLAGS_COMPILE;

      getOld = attr: if inEnv then (old.env.${attr} or "") else (old.${attr} or "");

      mergedCflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CFLAGS_COMPILE")
          allCflags
        ]
      );
      mergedCxxflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CXXFLAGS_COMPILE")
          allCxxflags
        ]
      );
      mergedLdflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_LDFLAGS")
          extraLdflags
        ]
      );

      # Build the flag attrs — either in env or at top level
      newFlags = {
        NIX_CFLAGS_COMPILE = mergedCflags;
        NIX_CXXFLAGS_COMPILE = mergedCxxflags;
      }
      // lib.optionalAttrs (mergedLdflags != "") {
        NIX_LDFLAGS = mergedLdflags;
      };

      flagAttrs = if inEnv then { env = (old.env or { }) // newFlags; } else newFlags;

    in
    flagAttrs
    // {
      pname = "${old.pname or pkg.attr}-variant";
      hardeningDisable = allHardeningDisable;

      # Skip tests — we only care about the compiled binaries
      doCheck = false;
      doInstallCheck = false;

      # Metadata for manifest generation
      passthru = (old.passthru or { }) // {
        datasetMeta = {
          inherit variantLabel;
          package = pkg.label;
          arch = target.label;
          compiler = compiler.label;
          compilerVersion = compiler.version;
          compilerFamily = compiler.family;
          optimization = optLevel.label;
          flags = flagSet.label;
          hardening = hardening.label;
        };
      };
    }
  );

in
{
  inherit variantLabel variantPkg;
  meta = variantPkg.passthru.datasetMeta;
}

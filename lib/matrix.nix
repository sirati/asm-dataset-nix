# Generate the full combinatorial matrix of all variants.
#
# Uses a nested attrset structure for fast lookup:
#   dataset.<pkg>.<arch>.<variant-suffix>
# This avoids forcing 75K+ keys when looking up a single variant.
#
# Metadata (labels, compiler info) is computed without creating derivations,
# so _meta can be evaluated instantly even for the full 75K matrix.

{
  pkgs,
  lib,
  extraCompilers ? {
    gcc = [ ];
    clang = [ ];
    all = [ ];
  },
}:

let
  currentCompilers = import ./compilers.nix { inherit pkgs; };

  # Merge current and extra (old) compilers, deduplicating by label.
  # Current compilers take priority over extra ones.
  dedupByLabel =
    comps:
    builtins.attrValues (
      builtins.listToAttrs (
        map (c: {
          name = c.label;
          value = c;
        }) comps
      )
    );

  compilers = {
    gcc = dedupByLabel (extraCompilers.gcc ++ currentCompilers.gcc);
    clang = dedupByLabel (extraCompilers.clang ++ currentCompilers.clang);
    all = dedupByLabel (extraCompilers.all ++ currentCompilers.all);
  };
  archDefs = import ./architectures.nix { };
  flagDefs = import ./flags.nix { };
  pkgDefs = import ./packages.nix { };
  mkVariant = import ./mkVariant.nix { inherit pkgs lib; };
  mkBinaryTarball = import ./mkBinaryTarball.nix { inherit pkgs lib; };

  # Skip -Oz for GCC (clang-only flag)
  isValidCombo = compiler: optLevel: !(optLevel.clangOnly && compiler.family == "gcc");

  # Skip compiler/target combos below the minimum supported version (from support_matrix.md).
  parseVersion =
    version:
    let
      parts = builtins.match "([0-9]+)\\.([0-9]+).*" version;
    in
    if parts != null then
      {
        major = lib.toInt (builtins.elemAt parts 0);
        minor = lib.toInt (builtins.elemAt parts 1);
      }
    else
      {
        major = 0;
        minor = 0;
      };

  meetsMinVersion =
    cv: mv: mv.major == 0 || cv.major > mv.major || (cv.major == mv.major && cv.minor >= mv.minor);

  # maxVersion: { major = 0; minor = 0; } means no maximum (all versions pass).
  meetsMaxVersion =
    cv: mv: mv.major == 0 || cv.major < mv.major || (cv.major == mv.major && cv.minor <= mv.minor);

  isValidArchCombo =
    compiler: target:
    let
      cv = parseVersion compiler.version;
      minV = if compiler.family == "gcc" then target.minGccVersion else target.minClangVersion;
      maxV =
        if compiler.family == "gcc" then
          target.maxGccVersion or {
            major = 0;
            minor = 0;
          }
        else
          target.maxClangVersion or {
            major = 0;
            minor = 0;
          };
    in
    meetsMinVersion cv minV && meetsMaxVersion cv maxV;

  # All valid (compiler, optLevel) pairs
  compilerOptPairs = lib.concatMap (
    compiler:
    map (opt: { inherit compiler opt; }) (
      builtins.filter (isValidCombo compiler) flagDefs.optimizationLevels
    )
  ) compilers.all;

  # All (compiler, optLevel, flagSet, hardening) tuples
  allFlagCombos = lib.concatMap (
    { compiler, opt }:
    lib.concatMap (
      flagSet:
      map (hardening: {
        inherit
          compiler
          opt
          flagSet
          hardening
          ;
      }) flagDefs.hardeningModes
    ) flagDefs.flagSets
  ) compilerOptPairs;

  # Compute the suffix key and metadata without creating any derivation.
  mkSuffix =
    {
      compiler,
      opt,
      flagSet,
      hardening,
    }:
    lib.concatStringsSep "-" [
      compiler.label
      opt.label
      flagSet.label
      hardening.label
    ];

  mkMeta =
    pkgDef: target:
    {
      compiler,
      opt,
      flagSet,
      hardening,
    }:
    let
      suffix = mkSuffix {
        inherit
          compiler
          opt
          flagSet
          hardening
          ;
      };
    in
    {
      variantLabel = lib.concatStringsSep "-" [
        pkgDef.label
        target.label
        suffix
      ];
      package = pkgDef.label;
      arch = target.label;
      compiler = compiler.label;
      compilerFamily = compiler.family;
      compilerVersion = compiler.version;
      optimization = opt.label;
      flags = flagSet.label;
      hardening = hardening.label;
    };

  # Build one variant entry (lazy — derivation not forced until .tarball/.rawPkg is accessed)
  mkEntry =
    pkgDef: target: combo:
    let
      suffix = mkSuffix combo;
      meta = mkMeta pkgDef target combo;
    in
    {
      name = suffix;
      value = {
        inherit meta;
        inherit (meta) variantLabel;
        # These are lazy — only evaluated when actually accessed
        tarball = mkBinaryTarball (mkVariant {
          pkg = pkgDef;
          compiler = combo.compiler;
          inherit target;
          optLevel = combo.opt;
          flagSet = combo.flagSet;
          hardening = combo.hardening;
        });
        rawPkg =
          (mkVariant {
            pkg = pkgDef;
            compiler = combo.compiler;
            inherit target;
            optLevel = combo.opt;
            flagSet = combo.flagSet;
            hardening = combo.hardening;
          }).variantPkg;
      };
    };

  # Filter flag combos to only include compilers that meet the target's minimum version.
  combosForTarget =
    target: builtins.filter (combo: isValidArchCombo combo.compiler target) allFlagCombos;

  # ── Nested attrset: dataset.<pkg>.<arch>.<suffix> ────────────────────────
  nestedMatrix = lib.genAttrs (map (p: p.label) pkgDefs.all) (
    pkgLabel:
    let
      pkgDef = lib.findFirst (p: p.label == pkgLabel) (throw "unreachable") pkgDefs.all;
    in
    lib.genAttrs (builtins.attrNames archDefs.targets) (
      archName:
      let
        target = archDefs.targets.${archName};
      in
      builtins.listToAttrs (map (mkEntry pkgDef target) (combosForTarget target))
    )
  );

  # ── Pure metadata (no derivations, instant eval) ─────────────────────────
  metaMatrix = lib.genAttrs (map (p: p.label) pkgDefs.all) (
    pkgLabel:
    let
      pkgDef = lib.findFirst (p: p.label == pkgLabel) (throw "unreachable") pkgDefs.all;
    in
    lib.genAttrs (builtins.attrNames archDefs.targets) (
      archName:
      let
        target = archDefs.targets.${archName};
      in
      builtins.listToAttrs (
        map (combo: {
          name = mkSuffix combo;
          value = mkMeta pkgDef target combo;
        }) (combosForTarget target)
      )
    )
  );

  # Count without forcing derivation evaluation
  combosPerArch = builtins.length allFlagCombos;
  numArchs = builtins.length (builtins.attrNames archDefs.targets);
  numPkgs = builtins.length pkgDefs.all;
  matrixSize = combosPerArch * numArchs * numPkgs;

in
{
  inherit nestedMatrix metaMatrix matrixSize;

  # Targeted access (fast — only evaluates the requested slice)
  getPackage = pkgLabel: nestedMatrix.${pkgLabel};
  getPackageArch = pkgLabel: archLabel: nestedMatrix.${pkgLabel}.${archLabel};
}

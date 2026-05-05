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

  # Generic per-axis filter: an axis entry may declare
  # ``minGccVersion`` / ``minClangVersion`` (default: no minimum),
  # ``clangOnly`` (default false), and ``archs`` (default: null =
  # all targets accepted).
  noMin = {
    major = 0;
    minor = 0;
  };

  entryAcceptsCompiler =
    entry: compiler:
    let
      cv = parseVersion compiler.version;
      minV =
        if compiler.family == "gcc" then
          entry.minGccVersion or noMin
        else
          entry.minClangVersion or noMin;
      familyOk =
        if entry.clangOnly or false then compiler.family == "clang" else true;
    in
    familyOk && meetsMinVersion cv minV;

  entryAcceptsArch =
    entry: target:
    let
      allowed = entry.archs or null;
    in
    allowed == null || builtins.elem target.label allowed;

  # All (compiler, optLevel, flagSet, hardening, sanitizer, march)
  # tuples — compiler-only filtering happens here; arch filtering
  # gets applied later in ``combosForTarget``. Each axis independent;
  # mutually-exclusive options live within a single axis (one
  # sanitizer per build, one march level per build).
  allFlagCombos = lib.concatMap (
    { compiler, opt }:
    lib.concatMap (
      flagSet:
      lib.concatMap (
        hardening:
        lib.concatMap (
          sanitizer:
          map (march: {
            inherit
              compiler
              opt
              flagSet
              hardening
              sanitizer
              march
              ;
          }) (builtins.filter (e: entryAcceptsCompiler e compiler) flagDefs.marchLevels)
        ) (builtins.filter (e: entryAcceptsCompiler e compiler) flagDefs.sanitizerModes)
      ) (builtins.filter (e: entryAcceptsCompiler e compiler) flagDefs.hardeningModes)
    ) (builtins.filter (e: entryAcceptsCompiler e compiler) flagDefs.flagSets)
  ) compilerOptPairs;

  # Compute the suffix key and metadata without creating any derivation.
  mkSuffix =
    {
      compiler,
      opt,
      flagSet,
      hardening,
      sanitizer,
      march,
    }:
    lib.concatStringsSep "-" [
      compiler.label
      opt.label
      flagSet.label
      hardening.label
      sanitizer.label
      march.label
    ];

  mkMeta =
    pkgDef: target:
    {
      compiler,
      opt,
      flagSet,
      hardening,
      sanitizer,
      march,
    }:
    let
      suffix = mkSuffix {
        inherit
          compiler
          opt
          flagSet
          hardening
          sanitizer
          march
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
      sanitizer = sanitizer.label;
      march = march.label;
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
          sanitizer = combo.sanitizer;
          march = combo.march;
        });
        rawPkg =
          (mkVariant {
            pkg = pkgDef;
            compiler = combo.compiler;
            inherit target;
            optLevel = combo.opt;
            flagSet = combo.flagSet;
            hardening = combo.hardening;
            sanitizer = combo.sanitizer;
            march = combo.march;
          }).variantPkg;
      };
    };

  # Filter flag combos for a specific target: drop combos whose
  # compiler doesn't meet the target's minimum version, AND drop
  # combos whose flagSet/hardening/sanitizer/march entry has an
  # ``archs`` allow-list that excludes this target.
  combosForTarget =
    target:
    builtins.filter (
      combo:
      isValidArchCombo combo.compiler target
      && entryAcceptsArch combo.flagSet target
      && entryAcceptsArch combo.hardening target
      && entryAcceptsArch combo.sanitizer target
      && entryAcceptsArch combo.march target
    ) allFlagCombos;

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

  # ── Cross-toolchain pre-build ──────────────────────────────────────────
  # For each valid (target, compiler) pair, expose the cross-compiler (cc)
  # derivation. Building these before the package phase populates the Nix
  # store / binary cache so that subsequent variant builds skip toolchain
  # construction. Combos that fail evaluation (e.g. old-nixpkgs missing an
  # attribute for a target) are silently dropped via tryEval.
  crossToolchainMap = lib.mapAttrs (
    archName: target:
    let
      targetPkgs' = archDefs.getPkgsForTarget pkgs target;
      validCompilers = builtins.filter (comp: isValidArchCombo comp target) compilers.all;
    in
    if targetPkgs' == null then
      { }
    else
      builtins.listToAttrs (
        lib.concatMap (
          comp:
          let
            # ``deepSeq cc.drvPath cc`` forces full evaluation of the
            # cross-compiler derivation INSIDE the tryEval scope.
            # Without the force, ``mkStdenv ... .cc`` returns lazily;
            # ``tryEval`` succeeds at the wrapper level but downstream
            # consumers (``nix-eval-jobs``, ``nix eval --json``) later
            # hit the deferred throw outside any tryEval scope and
            # crash the whole arch's ``listToAttrs`` evaluation. With
            # the force, deferred throws (e.g. nixpkgs-18.03 pkgsCross
            # for ppc32 missing ``platform.kernelArch``) become
            # eval-time throws inside the tryEval and we cleanly drop
            # that one combo while keeping the rest of the arch.
            tried = builtins.tryEval (
              let cc = (comp.mkStdenv targetPkgs' target).cc;
              in builtins.deepSeq cc.drvPath cc
            );
          in
          if tried.success then
            [
              {
                name = comp.label;
                value = tried.value;
              }
            ]
          else
            # Log to stderr so operators see which combos got dropped.
            # Visible in ``nix eval`` / ``nix-eval-jobs`` stderr; can
            # be grep'd by consumers for matrix-coverage diagnostics.
            builtins.trace
              "matrix.nix: dropping ${target.label}+${comp.label} (eval failed inside tryEval)"
              [ ]
        ) validCompilers
      )
  ) archDefs.targets;

  # Per-arch linkFarms for batch building: one derivation that depends on
  # every valid cross-compiler for the architecture.
  crossToolchains = lib.mapAttrs (
    archName: compilerMap:
    pkgs.linkFarm "cross-toolchains-${archName}" (
      lib.mapAttrsToList (label: cc: {
        name = label;
        path = cc;
      }) compilerMap
    )
  ) crossToolchainMap;

  # Pure metadata: which (arch, compiler) pairs the matrix considers valid.
  # Instant to evaluate — does not force any derivations.
  crossToolchainsMeta = lib.mapAttrs (
    archName: target:
    map (comp: {
      compiler = comp.label;
      family = comp.family;
      version = comp.version;
    }) (builtins.filter (comp: isValidArchCombo comp target) compilers.all)
  ) archDefs.targets;

in
{
  inherit
    nestedMatrix
    metaMatrix
    matrixSize
    crossToolchainMap
    crossToolchains
    crossToolchainsMeta
    ;

  # Targeted access (fast — only evaluates the requested slice)
  getPackage = pkgLabel: nestedMatrix.${pkgLabel};
  getPackageArch = pkgLabel: archLabel: nestedMatrix.${pkgLabel}.${archLabel};
}

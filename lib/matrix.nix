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
  mkBinaryFolder = import ./mkBinaryFolder.nix { inherit pkgs lib; };

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

  # Per-target "known broken" version lists: an evaluable+buildable
  # toolchain wrapper still fails to compile real code under that
  # `(arch, compiler-version)` pair, for reasons documented in
  # `lib/architectures.nix` next to each entry (e.g. missing
  # integrated-as for ppc64+clang3.x, autoconf-vs-direct divergence
  # for aarch64+clang3_5). Dropping at matrix-build time means the
  # framework never enumerates the combo and the dispatch never
  # wastes a worker on a known-impossible variant.
  #
  # Entry shape:
  #   { major; minor; }        — exact (major, minor) match
  #   { major; }    (no minor) — wildcard: matches any minor of that major
  # The wildcard form is what we want for "the entire major release
  # is broken on this target" (e.g. all of GCC 11.x on ppc32); the
  # exact form is for surgical singletons (e.g. clang 3.5.2 alone).
  isVersionInBrokenList =
    cv: brokenList:
    builtins.any (
      b: b.major == cv.major && (!(b ? minor) || b.minor == cv.minor)
    ) brokenList;

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
      brokenList =
        if compiler.family == "gcc" then
          target.brokenGccVersions or [ ]
        else
          target.brokenClangVersions or [ ];
    in
    meetsMinVersion cv minV
    && meetsMaxVersion cv maxV
    && !(isVersionInBrokenList cv brokenList);

  # All valid (compiler, optLevel) pairs. Optimization levels carry the
  # same constraint fields as the other axes (clangOnly for -Oz,
  # minGccVersion for -Ofast), so reuse the generic per-axis filter
  # (defined below; Nix lets-bindings are order-independent).
  compilerOptPairs = lib.concatMap (
    compiler:
    map (opt: { inherit compiler opt; }) (
      builtins.filter (e: entryAcceptsCompiler e compiler) flagDefs.optimizationLevels
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

  # Generic matrix-side gate: a package can be variant-instantiated by
  # ``mkVariant`` (which calls ``targetPkgs.${attr}.override { stdenv = …; }``)
  # only if its underlying package function declares a ``stdenv`` parameter.
  # ``stdenvNoCC``-only packages (e.g. ``nanopb``) survive into the matrix
  # otherwise and throw ``unexpected argument 'stdenv'`` at build time.
  #
  # Introspection: nixpkgs' ``lib.makeOverridable`` wraps ``.override`` as a
  # functor whose ``__functionArgs`` mirrors the underlying package lambda's
  # formal parameters. ``pkg.override.__functionArgs ? stdenv`` is therefore
  # the most direct, allocation-free probe — no need to actually invoke
  # ``override`` or call ``overrideAttrs`` (both would force real evaluation).
  # The whole probe is wrapped in ``tryEval`` so a malformed/legacy override
  # mechanism on some obscure package can't take down the entire matrix.
  pkgAcceptsStdenv =
    targetPkgs: pkgDef:
    if targetPkgs == null || !(targetPkgs ? ${pkgDef.attr}) then
      false
    else
      let
        probe = builtins.tryEval (
          let
            p = targetPkgs.${pkgDef.attr};
          in
          p ? override
          && (p.override ? __functionArgs)
          && (p.override.__functionArgs ? stdenv)
        );
      in
      probe.success && probe.value;

  # Per-(pkg, arch) platform-availability gate, complementary to
  # ``pkgAcceptsStdenv``. Many nixpkgs packages declare a non-trivial
  # ``meta.platforms`` allowlist or ``meta.badPlatforms`` denylist (e.g.
  # ``guetzli`` ships SSE2 intrinsics unconditionally → x86-only;
  # ``hyperscan`` is x86-only; ``rav1e`` rules out most cross targets).
  # Others mark themselves ``meta.broken = true`` on hosts where their
  # build is known not to land (``xed`` on aarch64 cross), or carry
  # ``meta.insecure = true`` so nixpkgs refuses to evaluate them by
  # default (``quickjs`` with active CVEs).
  #
  # nixpkgs collapses all three signals into a single boolean,
  # ``pkg.meta.available``, computed in ``<nixpkgs>/lib/meta.nix`` by
  # combining ``availableOn`` (platforms/badPlatforms vs the build's
  # host platform) with ``broken`` and the insecure-allowlist check.
  # ``stdenv.mkDerivation``'s "Refusing to evaluate package … because
  # it is not available on the requested hostPlatform" error fires from
  # exactly the same condition we check here, so using ``meta.available``
  # mirrors the upstream gate one-to-one.
  #
  # The probe runs inside ``tryEval`` because a small number of legacy
  # attrs throw from ``meta`` itself (broken assertions in old nixpkgs
  # revisions or `requireFile`-style fetchers). The fallback ``false``
  # keeps the rest of the matrix evaluable; a probe that can't determine
  # availability is treated as "not available".
  pkgSupportsPlatform =
    targetPkgs: pkgDef:
    if targetPkgs == null || !(targetPkgs ? ${pkgDef.attr}) then
      false
    else
      let
        probe = builtins.tryEval (
          let
            p = targetPkgs.${pkgDef.attr};
          in
          (p.meta.available or true)
        );
      in
      probe.success && probe.value;

  # Combined gate: both axes must hold for a (pkg, arch) cell to
  # enumerate variants. Single source of truth used by both
  # ``nestedMatrix`` (derivation-building) and ``metaMatrix`` (pure
  # metadata) so they stay in lock-step — if any cell shows up empty
  # in ``_meta``, the same cell will be empty in ``dataset``.
  pkgIsBuildableForTarget =
    targetPkgs: pkgDef:
    pkgAcceptsStdenv targetPkgs pkgDef
    && pkgSupportsPlatform targetPkgs pkgDef;

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

  # Build one variant entry (lazy — derivation not forced until .elfFolder/.rawPkg is accessed)
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
        elfFolder = mkBinaryFolder (mkVariant {
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
        targetPkgs' = archDefs.getPkgsForTarget pkgs target;
      in
      if !(pkgIsBuildableForTarget targetPkgs' pkgDef) then
        { }
      else
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
        targetPkgs' = archDefs.getPkgsForTarget pkgs target;
      in
      if !(pkgIsBuildableForTarget targetPkgs' pkgDef) then
        { }
      else
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

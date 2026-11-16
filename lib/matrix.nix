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
  capture ? null,
  packageProviders,
  evaluationIdentity ? null,
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
  catalog = import ./package-versions.nix { inherit lib; providers = packageProviders; };
  pkgDefs = { all = catalog.entries; };
  resolvePackage = import ./resolve-package.nix { inherit pkgs lib; providers = packageProviders; };
  mkVariant = import ./mkVariant.nix {
    inherit pkgs lib;
    resolvePackage = { pkg, target }: resolvedPackages.${pkg.packageId}.${target.label};
  };
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

  # Bind these computations once. Nix shares bound thunks, not arbitrary
  # repeated function applications over every compiler/flag combination.
  resolvedPackages = lib.mapAttrs (_: pkg:
    lib.mapAttrs (_: target: resolvePackage { inherit pkg target; }) archDefs.targets
  ) catalog.byId;
  pkgIsBuildableForTarget = target: pkg: resolvedPackages.${pkg.packageId}.${target.label}.available;

  # Filter each independent axis before forming its Cartesian product.
  # Compiler versions and permitted axes are shared by all optimization levels.
  groupsForTarget = target: lib.concatMap (compiler:
    let
      accepts = entry: entryAcceptsCompiler entry compiler
        && (target == null || entryAcceptsArch entry target);
      axes = {
        flagSets = builtins.filter accepts flagDefs.flagSets;
        hardeningModes = builtins.filter accepts flagDefs.hardeningModes;
        sanitizerModes = builtins.filter accepts flagDefs.sanitizerModes;
        marchLevels = builtins.filter accepts flagDefs.marchLevels;
      };
    in if target != null && !(isValidArchCombo compiler target) then [] else
      map (opt: { inherit compiler opt axes; })
        (builtins.filter (entry: entryAcceptsCompiler entry compiler) flagDefs.optimizationLevels)
  ) compilers.all;
  groupsByTarget = lib.mapAttrs (_: target: groupsForTarget target) archDefs.targets;
  groupSize = group: builtins.foldl' (n: entries: n * builtins.length entries) 1
    (builtins.attrValues group.axes);
  countGroups = groups: builtins.foldl' (n: group: n + groupSize group) 0 groups;
  expandGroup = { compiler, opt, axes }: lib.concatMap (flagSet:
    lib.concatMap (hardening: lib.concatMap (sanitizer:
      map (march: { inherit compiler opt flagSet hardening sanitizer march; }) axes.marchLevels
    ) axes.sanitizerModes) axes.hardeningModes
  ) axes.flagSets;
  combosByTarget = lib.mapAttrs (_: groups: lib.concatMap expandGroup groups) groupsByTarget;
  combosForTarget = target: combosByTarget.${target.label};

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
    resolvedPackages.${pkgDef.packageId}.${target.label}.meta // {
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
    } // lib.optionalAttrs (capture != null) {
      compilerCapture = import ./capture-contract.nix {
        inherit (compiler) family;
        policy = capture.policy or "required";
        debugPolicy = capture.debugPolicy or "required";
      };
    };

  # Build one variant entry (lazy — derivation not forced until .elfFolder/.rawPkg is accessed)
  mkEntry =
    pkgDef: target: combo:
    let
      suffix = mkSuffix combo;
      meta = mkMeta pkgDef target combo;
      variant = mkVariant {
        pkg = pkgDef;
        compiler = combo.compiler;
        inherit target capture;
        optLevel = combo.opt;
        flagSet = combo.flagSet;
        hardening = combo.hardening;
        sanitizer = combo.sanitizer;
        march = combo.march;
      };
    in
    {
      name = suffix;
      value = {
        inherit meta;
        inherit (meta) variantLabel;
        # These are lazy — only evaluated when actually accessed
        elfFolder = mkBinaryFolder variant;
        rawPkg = variant.variantPkg;
      };
    };

  # Selected builds do not need the flat legacy key set. Resolve each
  # requested suffix against the small independent axis lists instead.
  consume = axis: entries: text:
    let matches = builtins.filter (entry: lib.hasPrefix "${entry.label}-" text) entries;
        ordered = builtins.sort (a: b: builtins.stringLength a.label > builtins.stringLength b.label) matches;
        entry = if ordered == [] then throw "unknown variant ${axis}: ${text}" else builtins.head ordered;
    in { value = entry; rest = lib.removePrefix "${entry.label}-" text; };
  comboForSuffix = target: suffix:
    let
      requestedCompiler = builtins.head (lib.splitString "-" suffix);
      current = currentCompilers.getByLabel requestedCompiler;
      c = if current == null then consume "compiler" compilers.all suffix else {
        value = current; rest = lib.removePrefix "${requestedCompiler}-" suffix;
      };
      o = consume "optimization" flagDefs.optimizationLevels c.rest;
      f = consume "flag set" flagDefs.flagSets o.rest;
      h = consume "hardening" flagDefs.hardeningModes f.rest;
      z = consume "sanitizer" flagDefs.sanitizerModes h.rest;
      m = lib.findFirst (entry: entry.label == z.rest) null flagDefs.marchLevels;
      compiler = c.value;
      entries = [ f.value h.value z.value ] ++ lib.optional (m != null) m;
      valid = m != null && isValidArchCombo compiler target
        && entryAcceptsCompiler o.value compiler
        && builtins.all (entry: entryAcceptsCompiler entry compiler && entryAcceptsArch entry target) entries;
    in if !valid then throw "unsupported variant ${target.label}/${suffix}" else {
      inherit compiler;
      opt = o.value; flagSet = f.value; hardening = h.value; sanitizer = z.value; march = m;
    };
  selectDataset = lib.mapAttrs (_: pkg: request:
    let selection = if request ? selection then
      if (request.identity or null) != evaluationIdentity then throw "evaluation inputs changed since preflight"
      else request.selection
      else request;
    in
    lib.mapAttrs (arch: suffixes:
      let target = archDefs.targets.${arch} or (throw "unknown target ${arch}");
      in if !(pkgIsBuildableForTarget target pkg) then
        throw "package ${pkg.packageId} on ${arch}: ${resolvedPackages.${pkg.packageId}.${arch}.reason}"
      else lib.genAttrs suffixes (suffix: (mkEntry pkg target (comboForSuffix target suffix)).value.elfFolder)
    ) selection
  ) catalog.byId;
  variantAxes = lib.mapAttrs (arch: groups: {
    schema = 1;
    inherit arch;
    identity = evaluationIdentity;
    groups = map (group: {
      compiler = group.compiler.label;
      compilerFamily = group.compiler.family;
      compilerVersion = group.compiler.version;
      optimization = group.opt.label;
      axes = {
        flags = map (entry: entry.label) group.axes.flagSets;
        hardening = map (entry: entry.label) group.axes.hardeningModes;
        sanitizer = map (entry: entry.label) group.axes.sanitizerModes;
        march = map (entry: entry.label) group.axes.marchLevels;
      };
    }) groups;
  }) groupsByTarget;

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
      if !(pkgIsBuildableForTarget target pkgDef) then
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
      in
      if !(pkgIsBuildableForTarget target pkgDef) then
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
  combosPerArch = countGroups (groupsForTarget null);
  numArchs = builtins.length (builtins.attrNames archDefs.targets);
  numPkgs = builtins.length pkgDefs.all;
  matrixSize = combosPerArch * numArchs * builtins.length catalog.defaultIds;
  curatedMatrixSize = combosPerArch * numArchs * numPkgs;

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
            # Seed on the FULL cross-stdenv (``mkStdenv``), not just its
            # ``.cc``: a variant builds against ``overrideCC targetPkgs.stdenv
            # cc`` (mkVariant.nix:250), so the build node needs the whole
            # cross-stdenv closure (the ``stdenv-linux`` base + cc + bintools +
            # cross-libc), not only the compiler. Seeding on ``.cc`` shipped the
            # compiler but dropped the ``stdenv-linux`` output → offline cross
            # builds failed with "Cannot build stdenv-linux.drv". The shared
            # native build tools (bash/coreutils/make) are subtracted as the
            # per-node baseline at upload time, so only the cross-specific delta
            # ships.
            tried = builtins.tryEval (
              let toolchainStdenv = ((import ./resolve-toolchain.nix { inherit pkgs lib; }) {
                compiler = comp; inherit target capture;
              }).stdenv;
              in builtins.deepSeq toolchainStdenv.drvPath toolchainStdenv
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
  # every valid cross-stdenv (compiler + cross build environment) for the
  # architecture, so a single build realises the full cross-stdenv closures.
  crossToolchains = lib.mapAttrs (
    archName: compilerMap:
    pkgs.linkFarm "cross-toolchains-${archName}" (
      lib.mapAttrsToList (label: tc: {
        name = label;
        path = tc;
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

  packageCatalog = lib.mapAttrs (_: pkg:
    let native = resolvedPackages.${pkg.packageId}.x86_64;
    in {
      inherit (pkg) packageFamily packageId releaseId recipeId isDefault;
      packageVersion = if native.meta == null then null else native.meta.packageVersion;
      packageIdentity = if native.meta == null then null else native.meta.packageIdentity;
      sourceIdentity = if native.meta == null then null else native.meta.sourceIdentity;
      packageCatalogSchema = catalog.schema;
      variantInventorySchema = 1;
      variantInventoryIdentity = evaluationIdentity;
      availability = lib.mapAttrs (_: target:
        let resolved = resolvedPackages.${pkg.packageId}.${target.label};
        in { inherit (resolved) available reason; }
      ) archDefs.targets;
    }
  ) catalog.byId;

  variantCounts = lib.mapAttrs (_: groups: countGroups groups) groupsByTarget;

  packageVariantCounts = lib.mapAttrs (_: pkg:
    lib.mapAttrs (_: target:
      if (pkgIsBuildableForTarget target pkg)
      then countGroups groupsByTarget.${target.label} else 0
    ) archDefs.targets
  ) catalog.byId;

in
{
  inherit
    nestedMatrix
    metaMatrix
    matrixSize
    curatedMatrixSize
    crossToolchainMap
    crossToolchains
    crossToolchainsMeta
    packageCatalog
    packageVariantCounts
    selectDataset
    variantAxes
    variantCounts
    ;

  # Targeted access (fast — only evaluates the requested slice)
  getPackage = pkgLabel: nestedMatrix.${pkgLabel};
  getPackageArch = pkgLabel: archLabel: nestedMatrix.${pkgLabel}.${archLabel};
}

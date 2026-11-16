# Run from the isolated checkout with --impure. Flake inputs stay locked.
let
  flake = builtins.getFlake (toString ../.);
  system = "x86_64-linux";
  lib = flake.inputs.nixpkgs.lib;
  pkgs = import flake.inputs.nixpkgs { inherit system; };
  providers = {
    current = { path = flake.inputs.nixpkgs; revision = flake.inputs.nixpkgs.rev; narHash = flake.inputs.nixpkgs.narHash; };
    nixpkgs-24_05 = { path = flake.inputs.nixpkgs-24_05; revision = flake.inputs.nixpkgs-24_05.rev; narHash = flake.inputs.nixpkgs-24_05.narHash; };
  };
  catalog = import ../lib/package-versions.nix { inherit lib providers; };
  resolve = import ../lib/resolve-package.nix { inherit pkgs lib providers; };
  archs = (import ../lib/architectures.nix {}).targets;
  oldId = builtins.head (builtins.filter (id: id != "hello" && catalog.byId.${id}.packageFamily == "hello") (builtins.attrNames catalog.byId));
  suffix = "gcc15-O2-baseline-none-san-off-march-default";
  second = "clang21-O2-baseline-none-san-off-march-default";
  raw = id: arch: compilerSuffix: flake._rawPackages.${system}.${id}.${arch}.${compilerSuffix};
  metadata = id: arch: flake._meta.${system}.${id}.${arch}.${suffix};
  lazyMatrix = import ../lib/matrix.nix { inherit pkgs lib; packageProviders = providers; extraCompilers = throw "catalog forced compilers"; };
  provider23 = { path = flake.inputs.nixpkgs-23_11; revision = flake.inputs.nixpkgs-23_11.rev; narHash = flake.inputs.nixpkgs-23_11.narHash; };
  twins = import ../lib/resolve-package.nix { inherit pkgs lib; providers = providers // { nixpkgs-23_11 = provider23; }; };
  recipe23 = catalog.byId.${oldId}.recipeId // {
    provider = "nixpkgs-23_11"; inherit (provider23) revision narHash;
  };
  twin = twins {
    pkg = catalog.byId.${oldId} // { recipeId = recipe23; releaseId = "hello@${builtins.hashString "sha256" (builtins.toJSON recipe23)}"; };
    target = archs.x86_64;
  };
  fakePkgs = pkgs // {
    noSource = { override.__functionArgs.stdenv = true; meta.available = true; };
    nullSource = { override.__functionArgs.stdenv = true; meta.available = true; src = null; };
  };
  fakeResolve = import ../lib/resolve-package.nix { pkgs = fakePkgs; inherit lib providers; };
  mkVariant = import ../lib/mkVariant.nix { inherit pkgs lib; resolvePackage = resolve; };
  flags = import ../lib/flags.nix {};
  shimVariant = compatibility: mkVariant {
    pkg = catalog.byId.dash // { packageId = "dash--fixture"; label = "dash--fixture"; inherit compatibility; };
    compiler = { family = "gcc"; label = "gcc15"; version = pkgs.stdenv.cc.version; mkStdenv = p: _: p.stdenv; };
    target = archs.x86_64; optLevel = builtins.elemAt flags.optimizationLevels 2;
    flagSet = builtins.head flags.flagSets; hardening = builtins.head flags.hardeningModes;
  };
in {
  inherit oldId suffix;
  installables = {
    nativeNew = flake.dataset.${system}.hello.x86_64.${suffix};
    nativeOld = flake.dataset.${system}.${oldId}.x86_64.${suffix};
    crossNew = flake.dataset.${system}.hello.aarch64.${suffix};
    crossOld = flake.dataset.${system}.${oldId}.aarch64.${suffix};
  };
  context = {
    bash = toString pkgs.bash;
    toolchains = map (arch: flake._crossToolchainMap.${system}.${arch}.gcc15.drvPath) [ "x86_64" "aarch64" ];
  };
  checks = {
    pureCatalog = import ./package-versions.nix { inherit lib; };
    lazyCatalog = lazyMatrix.packageCatalog.hello.packageVersion == "2.12.3";
    missingTarget = (resolve { pkg = catalog.byId.${oldId}; target = { label = "unknown-target"; }; }).reason == "unavailable-target";
    missingRelease = (resolve { pkg = catalog.byId.${oldId} // { attr = "noSuchRelease"; }; target = archs.x86_64; }).reason == "unknown-release-attribute";
    missingProvider = !(builtins.tryEval (resolve { pkg = catalog.byId.hello // { recipeId.provider = "missing"; }; target = archs.x86_64; }).available).success;
    missingSource = (fakeResolve { pkg = catalog.byId.hello // { attr = "noSource"; }; target = archs.x86_64; }).reason == "missing-source";
    missingNullSource = (fakeResolve { pkg = catalog.byId.hello // { attr = "nullSource"; }; target = archs.x86_64; }).reason == "missing-source";
    sameVersionDifferentRecipe = twin.meta.packageVersion == lazyMatrix.packageCatalog.${oldId}.packageVersion && twin.meta.packageIdentity != lazyMatrix.packageCatalog.${oldId}.packageIdentity;
    familyShim = (shimVariant "current-v1").variantPkg.buildInputs == [] && (shimVariant "none").variantPkg.buildInputs != [];
    wrongVersion = !(builtins.tryEval (resolve { pkg = catalog.byId.${oldId} // { expectedVersion = "999"; }; target = archs.x86_64; }).meta).success;
    sourcePin = toString (raw "expat" "x86_64" suffix).src == toString pkgs.expat.src;
    historicalSourcePin = toString (raw oldId "x86_64" suffix).src == (resolve {pkg=catalog.byId.${oldId}; target=archs.x86_64;}).meta.sourceIdentity.storePath;
    nativeCompiler = builtins.all (id: (raw id "x86_64" suffix).stdenv.cc.drvPath == pkgs.stdenv.cc.drvPath) [ "hello" oldId ];
    crossCompiler = builtins.all (id: (raw id "aarch64" suffix).stdenv.cc.drvPath == pkgs.pkgsCross.aarch64-multiplatform.stdenv.cc.drvPath) [ "hello" oldId ];
    secondCompiler = map (id: { inherit id; cc = (raw id "x86_64" second).stdenv.cc.drvPath; drv = (raw id "x86_64" second).drvPath; source = toString (raw id "x86_64" second).src; version = (raw id "x86_64" second).version; }) [ "hello" oldId ];
    secondCompilerCross = map (id: { inherit id; cc = (raw id "aarch64" second).stdenv.cc.drvPath; drv = (raw id "aarch64" second).drvPath; source = toString (raw id "aarch64" second).src; version = (raw id "aarch64" second).version; }) [ "hello" oldId ];
    metaAgreement = builtins.all (id: builtins.all (arch:
      metadata id arch == builtins.intersectAttrs (metadata id arch) flake.dataset.${system}.${id}.${arch}.${suffix}.datasetMeta
    ) [ "x86_64" "aarch64" ]) [ "hello" oldId ];
    noDeepMetadataContext = builtins.all (context: !(context.allOutputs or false))
      (builtins.attrValues (builtins.getContext (builtins.toJSON flake.dataset.${system}.hello.aarch64.${suffix}.datasetMeta)));
    unsupportedCompiler = !(builtins.hasAttr "gcc4_4-O2-baseline-none-san-off-march-default" flake._meta.${system}.hello.riscv64);
  };
}

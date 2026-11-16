# Resolve the same pinned recipe for availability, metadata and builds.
# Package dependencies come from the provider; compiler selection stays in
# mkVariant and overrides only stdenv. No URL/version rewriting is supported.
{ pkgs, lib, providers }:
let
  archLib = import ./architectures.nix { };
  providerSets = lib.mapAttrs (name: provider:
    lib.mapAttrs (_: target:
      if name == "current" then archLib.getPkgsForTarget pkgs target else
      import provider.path ({
        localSystem = { system = pkgs.stdenv.buildPlatform.system; };
        # Modern config contains normalized options such as replaceStdenv =
        # null that older nixpkgs interprets differently. Pass only the policy
        # needed by package recipes, never the modern stdenv configuration.
        config = {
          allowUnfree = true;
          allowInsecurePredicate = pkgs.config.allowInsecurePredicate or (_: false);
        };
      } // lib.optionalAttrs (target.crossAttr != null || target ? crossSystem) {
        crossSystem = target.crossSystem or { config = target.crossConfig; };
      })
    ) archLib.targets
  ) providers;
  sourceRecord = src:
    if src == null then null else {
      # A store path also identifies local/generated sources. Fixed-output
      # hashes and upstream revisions are retained when the fetcher has them.
      storePath = toString src;
      outputHash = src.outputHash or null;
      outputHashAlgo = src.outputHashAlgo or null;
      outputHashMode = src.outputHashMode or null;
      revision = src.rev or null;
      urls = src.urls or (lib.optional (src ? url) src.url);
    };
in
{ pkg, target }:
let
  provider = pkg.recipeId.provider;
  selectedPkgs = if !(providers ? ${provider}) then
    throw "package resolver: unknown provider ${provider} for ${pkg.packageId}"
    else providerSets.${provider}.${target.label} or null;
  exists = selectedPkgs != null && lib.hasAttrByPath (lib.splitString "." pkg.attr) selectedPkgs;
  selected = lib.getAttrFromPath (lib.splitString "." pkg.attr) selectedPkgs;
  probe = builtins.tryEval (if exists then selected.meta.available or true else false);
  acceptsStdenv = selected ? override && selected.override ? __functionArgs
    && selected.override.__functionArgs ? stdenv;
  hasSource = selected ? src && selected.src != null;
  available = exists && probe.success && probe.value && acceptsStdenv && hasSource;
  reason = if selectedPkgs == null then "unavailable-target"
    else if !exists then "unknown-release-attribute"
    else if !probe.success then "recipe-evaluation-failed"
    else if !probe.value then "unsupported-platform"
    else if !acceptsStdenv then "incompatible-stdenv-override"
    else if !hasSource then "missing-source"
    else null;
  packageVersion = selected.version or (lib.getVersion selected);
  sourceIdentity = sourceRecord (selected.src or null);
  record = {
    inherit (pkg) packageFamily releaseId recipeId;
    inherit packageVersion sourceIdentity;
  };
  metadata = record // {
    inherit (pkg) packageId isDefault;
    packageCatalogSchema = 1;
    packageIdentity = builtins.hashString "sha256" (builtins.toJSON record);
  };
in {
  inherit available reason;
  package = if !available then throw "package ${pkg.packageId} on ${target.label}: ${reason}"
    else if pkg.expectedVersion != null && packageVersion != pkg.expectedVersion then
      throw "package ${pkg.packageId}: expected ${pkg.expectedVersion}, resolved ${packageVersion}"
    else selected;
  meta = if exists && probe.success then
    if pkg.expectedVersion != null && packageVersion != pkg.expectedVersion then
      throw "package ${pkg.packageId}: expected ${pkg.expectedVersion}, resolved ${packageVersion}"
    else metadata
    else null;
}

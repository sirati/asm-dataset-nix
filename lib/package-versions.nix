# Curated releases. This module validates keys without evaluating packages or
# compiler variants. Providers are pinned inputs passed explicitly by flake.nix.
{ lib, providers, packages ? (import ./packages.nix { }).all, releases ? null }:
let
  safe = s: builtins.isString s && builtins.match "[A-Za-z0-9][A-Za-z0-9_+-]*" s != null;
  families = map (p: p.label) packages;
  unique = xs: builtins.length xs == builtins.length (lib.unique xs);
  recipe = provider: attr: {
    kind = "nixpkgs";
    inherit provider attr;
    revision = providers.${provider}.revision;
    narHash = providers.${provider}.narHash;
    dependencyPolicy = "provider-pinned-with-selected-stdenv";
    adapterRevision = if provider == "current" then 1 else 0;
  };
  descriptor = p: provider: isDefault: expectedVersion:
    let
      recipeId = recipe provider p.attr;
      releaseId = "${p.label}@${builtins.hashString "sha256" (builtins.toJSON recipeId)}";
      packageId = if isDefault then p.label else
        "${p.label}--v${lib.replaceStrings [ "." "+" "-" ] [ "_" "_" "_" ] expectedVersion}--r${builtins.substring 0 12 (builtins.hashString "sha256" (builtins.toJSON recipeId))}";
    in {
      inherit (p) attr;
      inherit packageId releaseId recipeId isDefault expectedVersion;
      packageFamily = p.label;
      label = packageId;
      # Existing compatibility adjustments have only been measured against
      # the current recipe provider. A new release must opt in explicitly.
      compatibility = if provider == "current" then "current-v1" else "none";
    };
  defaults = map (p: descriptor p "current" true null) packages;
  extra = if releases != null then releases else [
    (descriptor { attr = "hello"; label = "hello"; } "nixpkgs-24_05" false "2.12.1")
  ];
  entries = defaults ++ extra;
  ids = map (p: p.packageId) entries;
  valid =
    if !(builtins.all (f: safe f && !(lib.hasInfix "--" f) && !(lib.hasInfix "__" f)) families)
    then throw "package catalog: unsafe family or reserved delimiter (-- or __)"
    else if !(unique families) then throw "package catalog: duplicate family"
    else if !(builtins.all (p: safe p.packageId && !(lib.hasInfix "__" p.packageId)) entries)
    then throw "package catalog: unsafe packageId"
    else if !(unique ids) then throw "package catalog: duplicate packageId"
    else if !(builtins.all (p: builtins.elem p.packageFamily families) entries)
    then throw "package catalog: release refers to an unknown family"
    else if !(builtins.all (f: builtins.length (builtins.filter (p: p.packageFamily == f && p.isDefault) entries) == 1) families)
    then throw "package catalog: each family needs exactly one default"
    else true;
in assert valid; {
  schema = 1;
  inherit entries;
  defaultIds = map (p: p.packageId) defaults;
  byId = builtins.listToAttrs (map (p: { name = p.packageId; value = p; }) entries);
}

# Pure evaluation regressions. Poison values prove that unselected providers,
# target sets and compiler discovery stay lazy, even for actual drv paths.
let
  f = builtins.getFlake (toString ../.);
  pkgs = import f.inputs.nixpkgs { system = "x86_64-linux"; };
  providers = {
    current = { path = f.inputs.nixpkgs; revision = f.inputs.nixpkgs.rev; narHash = f.inputs.nixpkgs.narHash; };
    nixpkgs-24_05 = { path = throw "unused historical provider forced";
      revision = f.inputs.nixpkgs-24_05.rev; narHash = f.inputs.nixpkgs-24_05.narHash; };
  };
  matrix = import ../lib/matrix.nix {
    pkgs = pkgs // { pkgsCross = throw "unused foreign target forced"; };
    inherit (pkgs) lib;
    packageProviders = providers;
    extraCompilers = throw "unused compiler discovery forced";
    evaluationIdentity = builtins.hashString "sha256" "fixture";
  };
  baseline = "gcc15-O2-baseline-none-san-off-march-default";
  selected = (matrix.selectDataset.hello { x86_64 = [ baseline ]; }).x86_64.${baseline};
  default = matrix.packageCatalog.hello;
  narrowed = default // { availability = { inherit (default.availability) x86_64; }; };
  rejects = value: !(builtins.tryEval value).success;
  bakedSource = builtins.path {
    name = "evaluation-baked-source-fixture";
    path = ../.;
    filter = path: _:
      !(builtins.elem (baseNameOf path) [ ".git" "docs" ".multiversion-work" ".pytest_cache" "__pycache__" ]);
  };
  resolvedInputs = builtins.mapAttrs (_: input: {
    revision = input.rev or null; narHash = input.narHash or null;
    source = builtins.unsafeDiscardStringContext (toString input);
  }) f.inputs;
  identityFor = inputs: import ../lib/evaluation-identity.nix {
    inherit (pkgs) lib; system = "x86_64-linux"; root = bakedSource; inherit inputs;
  };
in {
  bakedSourceIdentity = identityFor resolvedInputs == f._evaluationIdentity.x86_64-linux;
  inputOverrideChangesIdentity = identityFor (resolvedInputs // { nixpkgs = "changed-input"; }) != identityFor resolvedInputs;
  nativeSelectionDoesNotForceUnusedProviders = builtins.stringLength (builtins.toJSON narrowed) > 0;
  directLookupSkipsCompilerDiscovery = selected.drvPath == f._drvPaths.x86_64-linux.hello.x86_64.${baseline};
  longestHardeningPrefix = (matrix.selectDataset.hello {
    x86_64 = [ "gcc15-O2-baseline-ssp-all-san-off-march-default" ];
  }).x86_64.gcc15-O2-baseline-ssp-all-san-off-march-default.drvPath
    == f._drvPaths.x86_64-linux.hello.x86_64.gcc15-O2-baseline-ssp-all-san-off-march-default;
  unknownTarget = rejects (matrix.selectDataset.hello { unknown = [baseline]; }).unknown;
  unknownFlag = rejects (matrix.selectDataset.hello {
    x86_64 = [ "gcc15-O2-baseline-unknown-san-off-march-default" ];
  }).x86_64.gcc15-O2-baseline-unknown-san-off-march-default.drvPath;
  staleSelection = rejects (matrix.selectDataset.hello {
    identity = "stale"; selection.x86_64 = [baseline];
  }).x86_64;
  checkedSelection = (matrix.selectDataset.hello {
    identity = builtins.hashString "sha256" "fixture"; selection.x86_64 = [baseline];
  }).x86_64.${baseline}.drvPath == selected.drvPath;
  candidateCountPreserved = f._variantCounts.x86_64-linux.x86_64 + f._variantCounts.x86_64-linux.aarch64 == 541733;
  schema = f._variantAxes.x86_64-linux.x86_64.schema;
}

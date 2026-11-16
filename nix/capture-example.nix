# One unchanged native package. This does not enumerate the dataset matrix.
{ pkgs, captureTool ? pkgs.buildPackages.callPackage ./compiler-capture.nix {} }:
let
  lib = pkgs.lib;
  compiler = {
    family = "gcc";
    version = pkgs.stdenv.cc.version;
    mkStdenv = targetPkgs: _: targetPkgs.stdenv;
  };
  target = (import ../lib/architectures.nix {}).targets.x86_64;
  selected = (import ../lib/resolve-toolchain.nix { inherit pkgs lib; }) {
    inherit compiler target;
    capture = { inherit captureTool; debugPolicy = "required"; };
  };
  package = pkgs.hello.override { stdenv = selected.stdenv; };
  captured = (import ../lib/capture-overlay.nix { inherit pkgs captureTool; }) {
    inherit package;
    policy = "required";
    limitBytes = 536870912;
    separateDebugInfo = true;
    debugCompiler = selected.stdenv.cc.datasetCapture.identity;
  };
in (import ../lib/mkBinaryFolder.nix { inherit pkgs lib; }) {
  variantLabel = "hello-capture-example";
  variantPkg = captured;
  meta = {
    package = "hello";
    arch = target.label;
    compiler = "gcc${lib.versions.major compiler.version}";
    compilerVersion = compiler.version;
    compilerFamily = compiler.family;
    compilerCapture = import ../lib/capture-contract.nix { family = "gcc"; };
    expectedToolchain = selected.stdenv.cc.datasetCapture.identity;
  };
}

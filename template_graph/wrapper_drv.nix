# Parametric wrapper derivation: bundles a list of pre-built drv
# paths into a single .drv via string-context references, mirroring
# ``mkWrapper`` from ``sum_drv.nix``. No build runs — callers use
# ``nix-instantiate`` only.

{
  pkgs ? import <nixpkgs> {},
  drvs,
  name,
  system,
}:

derivation {
  inherit name system;
  builder = "${pkgs.bash}/bin/bash";
  args = [ "-c" "true" ];
  refs = builtins.toString drvs;
}

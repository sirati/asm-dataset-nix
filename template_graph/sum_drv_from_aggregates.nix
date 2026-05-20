# Build a "sum-root" .drv directly from pre-built aggregate paths.
#
# Layered tree:
#
#   sum-root .drv
#     ├── toolchains .drv         (already an aggregate, inputDrvs = each cc)
#     ├── matrix-<binary_a> .drv  (already an aggregate, inputDrvs = variants
#     │                            + toolchains backref for refcount sort)
#     ├── matrix-<binary_b> .drv
#     └── ...
#
# This file is the post-aggregate counterpart to ``sum_drv.nix``:
# the toolchains + matrix wrappers are now produced by the flake itself
# (``_toolchainsWrapper`` / ``_matrixWrapper.<binary>`` in ``flake.nix``),
# so the python tool only needs to splice the existing aggregate paths
# into one sum-root. No ``mkWrapper`` is needed here — that would just
# nest a redundant layer between the sum-root and the aggregates.
#
# Inputs are passed in as already-instantiated raw store paths (via
# ``builtins.storePath`` + ``builtins.appendContext`` on the python
# side); each path's basename must contain the literal substring
# ``"toolchains"`` (for the single toolchains aggregate) or ``"matrix"``
# (for each per-binary matrix aggregate) — the python validator in
# ``make_sum_drv_from_paths`` enforces that contract before we splice.
#
# No build is ever invoked — only ``nix-instantiate`` to materialise
# the .drv file.

{
  bash,
  toolchainsDrv,         # single drv path (string-context carrier)
  matrixDrvs,            # list of drv paths (string-context carriers)
  rootName ? "sum-root",
  system ? "x86_64-linux",
}:

derivation {
  inherit system;
  name = rootName;
  builder = "${bash}/bin/bash";
  args = [ "-c" "true" ];
  # ``builtins.toString`` on a list of derivation-like values
  # concatenates their outPaths under string context — each element
  # ends up in this wrapper's inputDrvs. Toolchains first so the
  # streaming planner sees it as the first depth-1 child (highest
  # refcount due to every matrix wrapper also referencing it).
  inputs = builtins.toString ([ toolchainsDrv ] ++ matrixDrvs);
}

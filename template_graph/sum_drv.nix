# DEPRECATED — kept only for legacy callers that have not yet migrated
# to ``sum_drv_from_aggregates.nix``. New code (and
# ``make_sum_drv_from_paths`` in particular) now expects the toolchains
# and per-binary matrix WRAPPERS to be built upstream (phase 1 / phase
# 2 of the pipeline, via ``make_wrapper_drv_from_paths`` →
# ``wrapper_drv.nix``); the post-aggregate assembler
# ``sum_drv_from_aggregates.nix`` then just splices those aggregates
# into the sum-root. This file's internal ``mkWrapper`` is therefore a
# redundant extra layer.
#
# Do not remove — older tests / external callers may still import it.
# New work should import ``sum_drv_from_aggregates.nix`` instead.
#
# Build a "sum" derivation that bundles toolchains + per-binary
# matrices into one top-level .drv:
#
#   sum-root .drv
#     ├── toolchains .drv              (inputDrvs = each toolchain)
#     ├── matrix-<binary_a> .drv       (inputDrvs = each variant)
#     ├── matrix-<binary_b> .drv
#     └── ...
#
# Toolchains live in their own wrapper because they are shared across
# every matrix — a graph algorithm allocates toolchain template nodes
# once, and the matrix subtrees then reuse them as already-known
# terminals. Matrices are per-binary so the algorithm can route each
# variant set independently.
#
# Caller-driven naming: the root + toolchains wrapper names are bare
# parameters; matrix wrappers get their names from each matrix entry
# (typically ``matrix-<binary>``). No build is ever invoked — only
# ``nix-instantiate`` to materialise the .drv files.

{
  bash,
  toolchains,
  matrices,  # list of { name = "..."; drvs = [...]; }
  rootName ? "sum-root",
  toolchainsName ? "toolchains",
  system ? "x86_64-linux",
}:

let
  mkWrapper = name: drvs:
    derivation {
      inherit name system;
      builder = "${bash}/bin/bash";
      args = [ "-c" "true" ];
      # ``builtins.toString`` on a list of derivations concatenates
      # their outPaths under string context — each drv ends up in
      # this wrapper's inputDrvs.
      refs = builtins.toString drvs;
    };

  toolchainsDrv = mkWrapper toolchainsName toolchains;
  # Each matrix wrapper also references the toolchains wrapper, so
  # toolchains ends up referenced N+1 times (root + every matrix).
  # ``nix-store --query --tree`` sorts children by reference count;
  # this floats toolchains to the top of the printed tree.
  matrixDrvs = map (m: mkWrapper m.name ([ toolchainsDrv ] ++ m.drvs)) matrices;
in

derivation {
  inherit system;
  name = rootName;
  builder = "${bash}/bin/bash";
  args = [ "-c" "true" ];
  # Toolchains first, then every matrix in declared order. nix's
  # inputDrvs is an unordered set, so the calling tool / test
  # identifies each child by name.
  inputs = builtins.toString ([ toolchainsDrv ] ++ matrixDrvs);
}

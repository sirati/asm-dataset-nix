{ pkgs, dynamicRunnerSrc }:

let
  # Build `dynamic-runner` against `pkgs.python313Packages` directly,
  # without applying the upstream overlay. The overlay would extend
  # `pythonPackagesExtensions`, which changes the python package set's
  # fixed point and invalidates every cached python derivation
  # downstream — including unrelated dev tooling like fastmcp / mcp.
  # `shutdownManagerBin` is the musl-static helper the wheel's
  # postInstall drops into `dynamic_runner/_shutdown_manager/`; the
  # framework's own flake builds it via the same shutdown-manager-bin
  # derivation we callPackage here.
  shutdownManagerBin = pkgs.callPackage
    "${dynamicRunnerSrc}/nix/shutdown-manager-bin.nix" { };
  # The wheel now also requires the musl-static slurm-wrapper binary
  # (added in the primary-coordinator-unification rewrite). Build it
  # via the framework's wrapper-bin derivation, same `pkgs` (not
  # `python313Packages`) scope as shutdownManagerBin since it needs
  # `pkgsCross`. Mirrors the framework overlay's wiring.
  wrapperManagerBin = pkgs.callPackage
    "${dynamicRunnerSrc}/nix/wrapper-bin.nix" { };
  dynamicRunner = pkgs.python313Packages.callPackage
    "${dynamicRunnerSrc}/nix/wheel.nix"
    { inherit shutdownManagerBin wrapperManagerBin; };

  devPythonPackages = python-pkgs: with python-pkgs; [
    pip
    ruff
    pytest
  ] ++ [ dynamicRunner ];

  devPackages = with pkgs; [
    basedpyright
    nil
    nixd
    # Parallel multi-attr nix evaluator. Used by preflight to
    # batch-evaluate the sampled drv paths in a single nix process
    # with N workers, instead of forking ``nix eval`` once per
    # variant (which re-walks each variant's cross-toolchain
    # closure from scratch).
    nix-eval-jobs
    #vscode-json-languageserver
    #bash-language-server
    #mcp-language-server
    #mcp-nixos
  ];

  devShell = pkgs.mkShell {
    packages = [
      (pkgs.python313.withPackages devPythonPackages)
    ]
    ++ devPackages;

    # Add the in-repo task packages to PYTHONPATH so `python -m
    # compiler_suit_runner` / `python -m ssh_debug_runner` resolve
    # without needing `pip install -e .` in the dev shell. We rely
    # on the dev shell being entered from the repo root (so $PWD /
    # `pwd` is the repo root); the alternative is to capture the
    # repo path at flake-eval time, but that pins the shell to a
    # fixed store path and breaks rename / move operations.
    shellHook = ''
      _repo_root="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
      if [ -d "$_repo_root/python" ]; then
        export PYTHONPATH="$_repo_root/python''${PYTHONPATH:+:$PYTHONPATH}"
      fi
    '';
  };

in
{
  inherit devShell;
}

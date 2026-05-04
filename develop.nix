{ pkgs, dynamicRunnerSrc }:

let
  # Build `dynamic-runner` against `pkgs.python313Packages` directly,
  # without applying the upstream overlay. The overlay would extend
  # `pythonPackagesExtensions`, which changes the python package set's
  # fixed point and invalidates every cached python derivation
  # downstream — including unrelated dev tooling like fastmcp / mcp.
  dynamicRunner = pkgs.python313Packages.callPackage
    "${dynamicRunnerSrc}/nix/wheel.nix"
    { };

  devPythonPackages = python-pkgs: with python-pkgs; [
    pip
    ruff
    pytest
  ] ++ [ dynamicRunner ];

  devPackages = with pkgs; [
    basedpyright
    nil
    nixd
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

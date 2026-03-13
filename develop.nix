{ pkgs }:

let
  devPythonPackages = python-pkgs: with python-pkgs; [
    pip
    ruff
  ];

  devPackages = with pkgs; [
    basedpyright
    nil
    nixd
    vscode-json-languageserver
    bash-language-server
    mcp-language-server
    mcp-nixos
  ];

  # makeShell = { pascal, deploymentPythonPackages, deploymentPackages }: pkgs.mkShell {
  #   packages = [
  #     (pkgs.python313.withPackages (
  #       python-pkgs: (deploymentPythonPackages pascal python-pkgs) ++ (devPythonPackages python-pkgs)
  #     ))
  #   ]
  #   ++ deploymentPackages
  #   ++ devPackages;
  # };

  devShell = pkgs.mkShell {
    packages = [
      (pkgs.python313.withPackages devPythonPackages)
    ]
    ++ devPackages;
  };

in
{
  # inherit makeShell;
  inherit devShell;
}

# Lint checks for the Python sources (python/, template_graph/), using
# the repo-root ruff.toml.
{
  lib,
  runCommand,
  ruff,
  root,
}:
let
  src = lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.unions [
      (root + "/ruff.toml")
      (root + "/python")
      (root + "/template_graph")
    ];
  };
in
{
  ruff = runCommand "ruff-check" { nativeBuildInputs = [ ruff ]; } ''
    cd ${src}
    ruff check --no-cache python template_graph
    touch $out
  '';
}

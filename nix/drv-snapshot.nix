# Derivation-path snapshot of a few dataset cells.
#
# A refactor that should not change any derivation (a "hash-neutral"
# step) must leave these paths unchanged. `cells` lists the cells as
# "<package>/<arch>/<variant suffix>"; the expected paths live in
# drv-snapshot.<system>.json next to this file.
#
#   nix eval .#checks.<system>.drvPathSnapshot.drvPath   # compare
#   nix eval --json .#_drvPathSnapshot.<system> \
#     > nix/drv-snapshot.<system>.json                  # regenerate
#
# Regenerate only for a change that is meant to alter derivations, and
# review the diff.
{
  lib,
  runCommand,
  system,
  dataset,
}:
let
  cells = [
    # hello on every target arch (armv7l-sf has no gcc15 toolchain)
    "hello/aarch64/gcc15-O2-baseline-default-san-off-march-default"
    "hello/armv7l-hf/gcc15-O2-baseline-default-san-off-march-default"
    "hello/armv7l-sf/gcc4_4-O2-baseline-bindnow-san-off-march-default"
    "hello/i686/gcc15-O2-baseline-default-san-off-march-default"
    "hello/mips64el/gcc15-O2-baseline-default-san-off-march-default"
    "hello/mipsel/gcc15-O2-baseline-default-san-off-march-default"
    "hello/ppc32/gcc15-O2-baseline-default-san-off-march-default"
    "hello/ppc64/gcc15-O2-baseline-default-san-off-march-default"
    "hello/riscv64/gcc15-O2-baseline-default-san-off-march-default"
    "hello/x86_64/gcc15-O2-baseline-default-san-off-march-default"
    # old gcc (native, cross, ppc32) and clang (current and old)
    "hello/x86_64/gcc4_4-O2-baseline-bindnow-san-off-march-default"
    "hello/aarch64/gcc4_8-O2-baseline-bindnow-san-off-march-default"
    "hello/ppc32/gcc4_4-O2-baseline-bindnow-san-off-march-default"
    "hello/x86_64/clang19-O2-baseline-default-san-off-march-default"
    "hello/x86_64/clang6-O2-baseline-default-san-off-march-default"
  ];

  drvPathOf =
    cell:
    builtins.unsafeDiscardStringContext
      (lib.getAttrFromPath (lib.splitString "/" cell) dataset).drvPath;

  actual = lib.genAttrs cells drvPathOf;

  expectedFile = ./. + "/drv-snapshot.${system}.json";
  hasExpected = builtins.pathExists expectedFile;
  expected = lib.importJSON expectedFile;

  mismatches = lib.filter (cell: (expected.${cell} or null) != actual.${cell}) cells;
  extra = lib.filter (cell: !(actual ? ${cell})) (builtins.attrNames expected);

  check =
    if mismatches == [ ] && extra == [ ] then
      runCommand "drv-path-snapshot-ok" { } "touch $out"
    else
      throw (
        "derivation-path snapshot changed for: "
        + lib.concatStringsSep ", " (mismatches ++ extra)
        + ". If the change is intended, regenerate nix/drv-snapshot.${system}.json."
      );
in
{
  inherit actual;
  checks = lib.optionalAttrs hasExpected { drvPathSnapshot = check; };
}

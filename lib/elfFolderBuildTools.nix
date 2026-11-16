# Build-host tools the elf-folder wrapper (lib/mkBinaryFolder.nix) needs as
# nativeBuildInputs to identify + collect ELF binaries (`file -bL`, find, awk).
# These run on the BUILD host (native, not the cross target), so for a
# substituter-free cross build their closure must be resident on every build
# node — but they are in NO toolchain / stdenv closure (they are the
# consumer's own elf-folder tooling). Shared from one place so the
# substituter-free submitter force-ships exactly this set into import_common
# (see cli.py / preflight.eval_build_host_tools) and mkBinaryFolder consumes
# the same list — they cannot drift.
{ pkgs }:

with pkgs; [
  file
  findutils
  gawk
  buildPackages.python3
]

# Small public entry points independent of the runner framework and legacy tools.
{ pkgs }:
let
  lib = pkgs.lib;
  source = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../python/compiler_suit_runner/capture.py
      ../python/compiler_suit_runner/capture_debug.py
      ../python/compiler_suit_runner/capture_publish.py
      ../python/compiler_suit_runner/capture_cli.py
      ../tests/compiler-capture/test_validation.py
      ../tests/compiler-capture/test_capture_cli.py
      ../tests/compiler-capture/test_capabilities.py
      ../tests/compiler-capture/capabilities.py
      ../tests/compiler-capture/ppc_debug.py
      ../tests/compiler-capture/test_ppc_debug.py
      ../tests/compiler-capture/batch.py
      ../tests/compiler-capture/test_batch.py
      ../tests/compiler-capture/matrix_coverage.py
      ../tests/compiler-capture/test_matrix_coverage.py
      ../tests/compiler-capture/test_legacy_stdlib.py
      ../tests/compiler-capture/test_debug_provenance.py
      ../tests/compiler-capture/test_capture_publication.py
      ../tests/compiler-capture/debug_sweep.py
      ../tests/compiler-capture/test_debug_sweep.py
      ../lib/legacy-clang-stdlib.h
      ../lib/legacy-clang-stdlib.sh
      ../tests/compiler-capture/test_elf_collector.py
      ../lib/collect-elf.py
    ];
  };
in {
  inspector = pkgs.writeShellScriptBin "capture-inspect" ''
    exec ${pkgs.python3}/bin/python3 -B ${source}/python/compiler_suit_runner/capture_cli.py "$@"
  '';
  validation = pkgs.runCommand "compiler-capture-validation" {
    nativeBuildInputs = [ pkgs.python3 ];
  } ''
    export PYTHONDONTWRITEBYTECODE=1
    export CAPTURE_VALIDATOR=${source}/python/compiler_suit_runner/capture.py
    export CAPTURE_INSPECTOR=${source}/python/compiler_suit_runner/capture_cli.py
    python3 -m unittest discover -s ${source}/tests/compiler-capture -p 'test_*.py'
    touch "$out"
  '';
}

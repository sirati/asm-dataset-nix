# Always call with buildPackages. This program runs on the build host, including
# when the compiler being observed targets a different machine.
{ lib, rustPlatform, python3, stdenv, runCommand }:
let
  # A store path independent of package outputs avoids reference cycles when
  # a compiler retains the intermediate's filename in symbols or debug data.
  viewAnchor = runCommand "capture-view-anchor" { dontFixup = true; } ''
    mkdir -p "$out"
    ln -s /proc/self/fd/198 "$out/view"
  '';
  source = lib.fileset.toSource {
    root = ../rust/compiler-capture;
    fileset = lib.fileset.unions [
      ../rust/compiler-capture/Cargo.toml
      ../rust/compiler-capture/Cargo.lock
      ../rust/compiler-capture/src
    ];
  };
  checksSource = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../tests/compiler-capture/test_forwarding.py
      ../tests/compiler-capture/test_storage.py
      ../tests/compiler-capture/test_stdout.py
      ../tests/compiler-capture/test_response.py
      ../tests/compiler-capture/test_validation.py
      ../tests/compiler-capture/test_native.py
      ../tests/compiler-capture/capabilities.py
      ../tests/compiler-capture/ppc_debug.py
      ../python/compiler_suit_runner/capture.py
      ../python/compiler_suit_runner/capture_debug.py
      ../lib/legacy-response-quote.sh
    ];
  };
in
rustPlatform.buildRustPackage {
  pname = "compiler-capture";
  version = "0.1.0";
  src = source;
  cargoLock.lockFile = builtins.toFile "compiler-capture-Cargo.lock" (builtins.readFile ../rust/compiler-capture/Cargo.lock);
  dontUseCargoParallelTests = true;
  nativeCheckInputs = [ python3 ];
  postInstall = ''
    mkdir -p "$out/nix-support"
    printf '%s\n' ${viewAnchor} > "$out/nix-support/capture-view-anchor"
  '';
  postCheck = ''
    export CAPTURE_BINARY="$(find "$PWD/target" -name compiler-capture -type f -print -quit)"
    test -n "$CAPTURE_BINARY"
    env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_forwarding.py
    env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_storage.py
    env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_stdout.py
    CAPTURE_RESPONSE_QUOTE=${checksSource}/lib/legacy-response-quote.sh env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_response.py
    export CAPTURE_VALIDATOR=${checksSource}/python/compiler_suit_runner/capture.py
    env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_validation.py
    export CAPTURE_GCC=${stdenv.cc}/bin/gcc
    export CAPTURE_GCC_VERSION=${stdenv.cc.version}
    export CAPTURE_CAPABILITIES=${checksSource}/tests/compiler-capture/capabilities.py
    env -u DATASET_CAPTURE_STAGE python3 ${checksSource}/tests/compiler-capture/test_native.py
  '';
  meta = {
    description = "Version-configured compiler artifact supervisor";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "compiler-capture";
  };
}

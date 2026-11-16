# One actual-target stdenv and real Nix separateDebugInfo hook per public pair.
{ repository, pkgs, compilerLabel, targetLabel, captureTool, ltoMode ? "", splitDwarf ? false,
  debugCase ? "normal" }:
let
  flake = builtins.getFlake ("git+file://" + toString repository);
  lib = pkgs.lib;
  identity = builtins.head (builtins.filter (entry: entry.compiler == compilerLabel)
    flake._crossToolchainsMeta.x86_64-linux.${targetLabel});
  original = flake._crossToolchainMap.x86_64-linux.${targetLabel}.${compilerLabel};
  cc = original.cc;
  prefix = cc.targetPrefix or "";
  c = if identity.family == "gcc" then "gcc" else "clang";
  cxx = if identity.family == "gcc" then "g++" else "clang++";
  entry = name: real: { inherit name; driver = name; executable = "${cc.cc}/bin/${real}"; };
  adapted = (import ../../lib/compiler-capture.nix { inherit pkgs captureTool; }) {
    inherit cc;
    inherit (identity) family version;
    target = original.hostPlatform.config;
    backend = "${identity.family}-trace";
    entries = [
      (entry "${prefix}${c}" (if identity.family == "gcc" then "${prefix}${c}" else c))
      (entry "${prefix}${cxx}" (if identity.family == "gcc" then "${prefix}${cxx}" else cxx))
    ];
  };
  stdenv = (import ../../lib/capture-stdenv.nix { inherit pkgs; }) { stdenv = original; cc = adapted; };
  flags = lib.concatStringsSep " " (lib.optional (ltoMode != "") ltoMode ++ lib.optional splitDwarf "-gsplit-dwarf");
  emulator = { i686 = "i386"; aarch64 = "aarch64"; armv7l-hf = "arm"; armv7l-sf = "arm";
    mipsel = "mipsel"; mips64el = "mipsn32el"; ppc32 = "ppc"; ppc64 = "ppc64"; riscv64 = "riscv64"; }.${targetLabel} or null;
  runtime = lib.optionals (emulator != null) [ "${pkgs.buildPackages.qemu-user}/bin/qemu-${emulator}" ]
    ++ lib.optionals (targetLabel == "ppc64") [ "-cpu" "power8" ];
  fixture = stdenv.mkDerivation ({
    name = "capture-debug-${compilerLabel}-${targetLabel}";
    dontUnpack = true;
    dontConfigure = true;
    hardeningDisable = [ "all" ];
    separateDebugInfo = debugCase == "already-separated";
    __structuredAttrs = debugCase == "structured";
    dontFixup = debugCase == "no-fixup";
    nativeBuildInputs = [ pkgs.buildPackages.python3 ];
    buildPhase = if debugCase == "empty" then ''
      printf '%s\n' '{"schema":1,"empty":true}' > "$datasetCapture/experiment.json"
    '' else ''
      printf 'int square(int n) { return n*n; }\n' > unit.c
      printf 'extern "C" int square(int); int main() { return square(3)!=9; }\n' > main.cpp
      (
        unset DATASET_CAPTURE_STAGE
        ${cc}/bin/${prefix}${c} ${flags} -O2 -g ${if identity.family == "gcc" then "-gdwarf-2" else "-gdwarf-4"} -c unit.c -o unit.o
        ${cc}/bin/${prefix}${cxx} ${flags} -O2 -g ${if identity.family == "gcc" then "-gdwarf-2" else "-gdwarf-4"} main.cpp unit.o -o program
        mv program baseline-program
      )
      ${adapted}/bin/${prefix}${c} ${flags} -O2 -c unit.c -o unit.o
      ${adapted}/bin/${prefix}${cxx} ${flags} -O2 main.cpp unit.o -o program
      CAPTURE_CAPABILITIES=${./capabilities.py} python3 ${./debug_report.py} compare \
        ${compilerLabel} ${targetLabel} ${identity.version} ${cc}/bin/${prefix}${c} ${cc}/bin/${prefix}${cxx} "$datasetCapture/experiment.json"
    '';
    installPhase = ''
      mkdir -p "$out/bin"
      ${lib.optionalString (debugCase == "reference-violation") ''printf '%s\n' ${pkgs.hello} > "$out/forbidden-reference"''}
      ${lib.optionalString (debugCase != "empty") ''cp program "$out/bin/program"''}
      ${lib.optionalString (debugCase == "install-strip") ''${cc.bintools}/bin/${cc.bintools.targetPrefix or ""}strip "$out/bin/program"''}
    '';
    postFixup = lib.optionalString (debugCase != "empty") ''
      python3 ${./debug_report.py} installed ${targetLabel} ${lib.escapeShellArg (builtins.toJSON runtime)} \
        "$out/bin/program" "$debug" ${pkgs.buildPackages.binutils}/bin/readelf "$datasetCapture/experiment.json"
    '';
  } // lib.optionalAttrs (debugCase == "caller-compression") { NIX_CFLAGS_COMPILE = "-Wa,--compress-debug-sections"; }
    // lib.optionalAttrs (builtins.elem debugCase [ "references" "reference-violation" ]) {
      disallowedReferences = [ pkgs.hello ];
    });
  captured = (import ../../lib/capture-overlay.nix { inherit pkgs captureTool; }) {
    package = fixture;
    policy = "required";
    separateDebugInfo = true;
    debugCompiler = adapted.datasetCapture.identity;
    reportUsage = true;
  };
  folder = (import ../../lib/mkBinaryFolder.nix { inherit pkgs lib; }) {
    variantLabel = "debug-${compilerLabel}-${targetLabel}";
    variantPkg = captured;
    meta = {
      package = "capture-debug-fixture";
      arch = targetLabel;
      compiler = compilerLabel;
      compilerFamily = identity.family;
      compilerVersion = identity.version;
      expectedToolchain = adapted.datasetCapture.identity;
      compilerCapture = import ../../lib/capture-contract.nix { inherit (identity) family; };
    };
  };
in assert !(builtins.pathExists (flake.outPath + "/.capture-experiments"));
folder // {
  captureFixtureAllowedDrvs = [ captured.drvPath ] ++ builtins.filter
    (path: builtins.match ".*-wrapper-.*\\.drv" path != null)
    ([ cc.drvPath cc.bintools.drvPath ] ++ (cc.wrapperPrerequisiteDrvs or []));
  captureFixtureOfflineRoot = stdenv;
  datasetCaptureLimitBytes = captured.datasetCaptureLimitBytes;
  datasetCaptureScratchBytes = captured.datasetCaptureScratchBytes;
}

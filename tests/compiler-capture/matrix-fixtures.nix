# Select one public compiler/target entry. Never instantiate the dataset matrix.
{ repository, pkgs, compilerLabel, targetLabel, captureTool, mode ? "capture", shape ? "smoke", ltoAbi ? "", ltoFeatures ? false }:
let
  flake = builtins.getFlake ("git+file://" + toString repository);
  lib = pkgs.lib;
  matches = builtins.filter (entry: entry.compiler==compilerLabel) flake._crossToolchainsMeta.x86_64-linux.${targetLabel};
  identity = builtins.head matches;
  family = identity.family;
  stdenv = flake._crossToolchainMap.x86_64-linux.${targetLabel}.${compilerLabel};
  cc = stdenv.cc;
  prefix = cc.targetPrefix or "";
  c = if family=="gcc" then "gcc" else "clang";
  cxx = if family=="gcc" then "g++" else "clang++";
  ltoFlag=if shape=="lto" then "-flto" else if shape=="thinlto" then "-flto=thin" else "";
  emulator = {
    i686="i386";aarch64="aarch64";armv7l-hf="arm";armv7l-sf="arm";
    mipsel="mipsel";mips64el="mipsn32el";ppc32="ppc";ppc64="ppc64";riscv64="riscv64";
  }.${targetLabel} or null;
  runtime = lib.optionals (emulator!=null) ["${pkgs.buildPackages.qemu-user}/bin/qemu-${emulator}"]
    ++ lib.optionals (targetLabel=="ppc64") ["-cpu" "power8"];
  entry = name: real: {inherit name; driver=name;executable="${cc.cc}/bin/${real}";};
  adapted = (import ../../lib/compiler-capture.nix {inherit pkgs captureTool;}) {
    inherit cc family;
    version=identity.version;
    target=stdenv.hostPlatform.config;
    backend=if mode=="forward" then "forward" else "${family}-trace";
    entries=[(entry "${prefix}${c}" (if family=="gcc" then "${prefix}${c}" else c))
      (entry "${prefix}${cxx}" (if family=="gcc" then "${prefix}${cxx}" else cxx))
      (entry "${prefix}cc" (if family=="gcc" then "${prefix}${c}" else c))
      (entry "${prefix}c++" (if family=="gcc" then "${prefix}${cxx}" else cxx))
      ((entry "${prefix}cpp" (if family=="gcc" then "${prefix}cpp" else "cpp")) // {optional=true;})];
  };
  selected = if mode=="baseline" then cc else adapted;
  capture = import ../../lib/capture-overlay.nix {inherit pkgs;captureTool=if mode=="capture" then captureTool else null;};
  fixtureStdenv = (import ../../lib/capture-stdenv.nix {inherit pkgs;}) {stdenv=pkgs.stdenv;cc=selected;};
  fixture = fixtureStdenv.mkDerivation {
    name="capture-matrix-${compilerLabel}-${targetLabel}-${mode}-${shape}";
    dontUnpack=true;
    hardeningDisable=["all"];
    nativeBuildInputs=[pkgs.python3 selected cc.bintools pkgs.binutils];
    passthru.captureFixtureAllowedDrvs = builtins.filter (path: builtins.match ".*-wrapper-.*\\.drv" path != null) ([cc.drvPath cc.bintools.drvPath] ++ (cc.wrapperPrerequisiteDrvs or []));
    passthru.captureFixtureOfflineRoot=fixtureStdenv;
    buildPhase=''
      ${lib.optionalString (mode=="forward") "unset DATASET_CAPTURE_STAGE"}
      printf 'int square(int n){return n*n;} int main(void){return square(3)!=9;}\n' > unit.c
      printf 'int square_cpp(int n){return n*n;}\n' > unit.cpp
      smoke() {
        local driver="$1"
        shift
        "$driver/bin/${prefix}${c}" --version > c-driver-version.txt
        "$driver/bin/${prefix}${cxx}" --version > cxx-driver-version.txt
        "$driver/bin/${prefix}${c}" -dumpmachine > driver-target.txt
        "$driver/bin/${prefix}${c}" "$@" ${ltoFlag} -O2 -c unit.c -o unit.o
        "$driver/bin/${prefix}${c}" "$@" ${ltoFlag} ${lib.optionalString (ltoAbi!="") "-Wl,-plugin-opt=-target-abi=${ltoAbi}"} ${lib.optionalString ltoFeatures "-Xlinker -plugin-opt=-mattr=+m,+a,+f,+d,+c"} unit.o -o program
        "$driver/bin/${prefix}${cxx}" "$@" ${ltoFlag} -O2 -c unit.cpp -o unit-cxx.o
        "$driver/bin/${prefix}${c}" -E unit.c > unit.i
      }
      ${lib.optionalString (mode=="capture") ''
        (unset DATASET_CAPTURE_STAGE; smoke ${cc})
        mkdir .smoke-baseline
        cp unit.o unit-cxx.o unit.i program c-driver-version.txt cxx-driver-version.txt driver-target.txt .smoke-baseline/
        (unset DATASET_CAPTURE_STAGE; smoke ${cc} -g ${if family=="gcc" then "-gdwarf-2" else "-gdwarf-4"})
        mkdir .smoke-debug
        cp unit.o unit-cxx.o program .smoke-debug/
        (unset DATASET_CAPTURE_STAGE; smoke ${selected})
        mkdir .smoke-forward
        cp unit.o unit-cxx.o unit.i program c-driver-version.txt cxx-driver-version.txt driver-target.txt .smoke-forward/
      ''}
      smoke ${selected}
      ${lib.optionalString (mode=="capture") ''
        readelf --debug-dump=decodedline ${if shape=="smoke" then "unit.o" else "program"} > source-lines.txt
      ''}
      export CAPTURE_CAPABILITIES=${./capabilities.py}
      export CAPTURE_PPC_DEBUG=${./ppc_debug.py}
      export CAPTURE_MATRIX_SHAPE=${shape}
      export CAPTURE_EXPECTED_VERSION=${identity.version}
      export CAPTURE_RUNTIME_PREFIX=${lib.escapeShellArg (builtins.toJSON runtime)}
      python3 ${./matrix_report.py} ${compilerLabel} ${targetLabel} ${mode} "$datasetCapture/experiment.json"
      ${lib.optionalString (shape=="corners") ''
        mkdir corners
        (cd corners; python3 ${./matrix_corners.py} ${cc} ${selected} \
          ${prefix}${c} ${prefix}${cxx} ${family} ${identity.version} ${targetLabel} "$datasetCapture/corners.json")
      ''}
    '';
    installPhase=''mkdir -p "$out"; cp program "$out/program"'';
  };
in assert !(builtins.pathExists (flake.outPath + "/.capture-experiments"));
assert matches!=[];
assert builtins.elem shape ["smoke" "corners" "lto" "thinlto"];
assert builtins.elem ltoAbi ["" "lp64" "lp64f" "lp64d"];
assert shape!="thinlto" || family=="clang" && lib.versionAtLeast identity.version "3.9";
assert builtins.elem shape ["smoke" "corners"] || lib.versionAtLeast identity.version (if family=="gcc" then "4.6" else "3.7");
capture {package=fixture;reportUsage=true;}

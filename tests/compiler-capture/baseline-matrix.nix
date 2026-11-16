# A cheap C++/LTO/runtime pass before the instrumented compiler matrix.
{ repository, pkgs, targetLabel, compilerLabel ? null, compilerOptions ? [], ltoOptions ? [] }:
let
  flake=builtins.getFlake ("git+file://" + toString repository);
  lib=pkgs.lib;
  entries=builtins.filter (entry: compilerLabel==null || entry.compiler==compilerLabel)
    flake._crossToolchainsMeta.x86_64-linux.${targetLabel};
  toolchains=map (entry:
    let
      cc=flake._crossToolchainMap.x86_64-linux.${targetLabel}.${entry.compiler}.cc;
      prefix=cc.targetPrefix or "";
    in entry // {
      inherit cc;
      driver="${cc}/bin/${prefix}${if entry.family=="gcc" then "g++" else "clang++"}";
      modes=["native"] ++ lib.optional (lib.versionAtLeast entry.version (if entry.family=="gcc" then "4.6" else "3.7")) "lto"
        ++ lib.optional (entry.family=="clang" && lib.versionAtLeast entry.version "3.9") "thinlto";
    }) entries;
  emulator={i686="i386";aarch64="aarch64";armv7l-hf="arm";armv7l-sf="arm";
    mipsel="mipsel";mips64el="mipsn32el";ppc32="ppc";ppc64="ppc64";riscv64="riscv64";}.${targetLabel} or null;
  runtime=lib.optionals (emulator!=null) ["${pkgs.buildPackages.qemu-user}/bin/qemu-${emulator}"]
    ++ lib.optionals (targetLabel=="ppc64") ["-cpu" "power8"];
  configuration=pkgs.writeText "capture-baseline-toolchains-${targetLabel}.json" (builtins.toJSON {
    target=targetLabel;inherit runtime compilerOptions ltoOptions;
    compilers=map (entry: builtins.removeAttrs entry ["cc"]) toolchains;
  });
  fixture=pkgs.stdenv.mkDerivation {
    name="capture-baseline-toolchains-${targetLabel}";
    dontUnpack=true;
    hardeningDisable=["all"];
    nativeBuildInputs=[pkgs.python3];
    passthru.captureFixtureAllowedDrvs=builtins.filter (path: builtins.match ".*-wrapper-.*\\.drv" path!=null)
      (lib.concatMap (entry: [entry.cc.drvPath entry.cc.bintools.drvPath]
        ++ (entry.cc.wrapperPrerequisiteDrvs or [])) toolchains);
    buildPhase=''
      unset DATASET_CAPTURE_STAGE
      python3 ${./baseline_matrix.py} ${configuration} "$datasetCapture/baseline.json"
    '';
    installPhase=''mkdir -p "$out"; cp "$datasetCapture/baseline.json" "$out/"'';
  };
in assert !(builtins.pathExists (flake.outPath + "/.capture-experiments"));
assert entries!=[];
(import ../../lib/capture-overlay.nix {inherit pkgs;}) {package=fixture;}

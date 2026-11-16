{ pkgs, captureTool ? pkgs.buildPackages.callPackage ../../nix/compiler-capture.nix {} }:
let
  repositoryFlake =
    let flake = builtins.getFlake ("git+file://" + toString ../..);
    in assert !(builtins.pathExists (flake.outPath + "/.capture-experiments")); flake;
  capture = import ../../lib/capture-overlay.nix { inherit pkgs; };
  collect = args: (import ../../lib/capture-overlay.nix { inherit pkgs captureTool; }) (args // {reportUsage=true;});
  wrapCompiler = cc: family: backend:
    let
      c = if family == "gcc" then "gcc" else "clang";
      cxx = if family == "gcc" then "g++" else "clang++";
      entry = name: real: { inherit name; driver = name; executable = "${cc.cc}/bin/${real}"; };
    in (import ../../lib/compiler-capture.nix { inherit pkgs captureTool; }) {
      inherit cc family backend;
      version = cc.version;
      target = pkgs.stdenv.hostPlatform.config;
      entries = [ (entry c c) (entry cxx cxx) (entry "cc" c) (entry "c++" cxx) ((entry "cpp" "cpp") // {optional=true;}) ];
    };
  wrap = family: backend: wrapCompiler (if family=="gcc" then pkgs.stdenv.cc else pkgs.llvmPackages_21.clang) family backend;
  base = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-output-contract";
    outputs = [ "out" "dev" ];
    env.FIXTURE_ENV = "preserved";
    meta.outputsToInstall = [ "out" ];
    dontUnpack = true;
    installPhase = ''
      test "$FIXTURE_ENV" = preserved
      test -d "$datasetCapture"
      test "''${datasetCapture:0:1}" = /
      mkdir -p "$out" "$dev"
      echo original > "$out/marker"
      echo headers > "$dev/marker"
      echo recognizable > "$datasetCapture/fixture-marker"
    '';
    postFixup = ''echo hook-preserved > "$out/hook"'';
  };
  captured = capture { package = base; };
  contract = drv:
    assert drv.outputs == [ "out" "dev" "datasetCapture" ];
    assert drv.outputName == base.outputName;
    assert drv.meta.outputsToInstall == base.meta.outputsToInstall;
    assert drv.FIXTURE_ENV == "preserved";
    assert (capture { package = drv; }).drvPath == drv.drvPath;
    drv;
  capability = family: mode:
    let cc = if family == "gcc" then pkgs.stdenv.cc else pkgs.llvmPackages_21.clang;
    in capture { package = pkgs.stdenvNoCC.mkDerivation {
      name = "capture-capability-${family}-${mode}";
      dontUnpack = true;
      nativeBuildInputs = [ pkgs.python3 cc ];
      JOB_WRAPPER = ./job_wrapper.py;
      buildPhase = ''
        python3 ${./capabilities.py} ${cc}/bin/${if family == "gcc" then "gcc" else "clang"} ${family} ${mode} "$datasetCapture/experiment.json"
      '';
      installPhase = ''mkdir -p "$out"'';
    }; };
  intercepted = family: backend:
    let cc = wrap family backend;
    in (if backend == "forward" then capture else collect) { package = pkgs.stdenvNoCC.mkDerivation {
      name = "capture-interception-${family}-${backend}";
      dontUnpack = true;
      nativeBuildInputs = [ pkgs.python3 cc ];
      buildPhase = ''
        ${pkgs.lib.optionalString (backend == "forward") "unset DATASET_CAPTURE_STAGE"}
        python3 ${./capabilities.py} ${cc}/bin/${if family == "gcc" then "gcc" else "clang"} ${family} baseline "$datasetCapture/experiment.json"
        test "$(${cc}/bin/cc -dumpmachine)" = "${pkgs.stdenv.hostPlatform.config}"
        ${cc}/bin/c++ --version >/dev/null
      '';
      installPhase = ''mkdir -p "$out"'';
    }; };
  realPackagePolicy = name: policy:
    let
      cc=wrap "gcc" "gcc-trace";
      stdenv=(import ../../lib/capture-stdenv.nix {inherit pkgs;}) {stdenv=pkgs.stdenv;inherit cc;};
      original=pkgs.${name}.override {inherit stdenv;};
      reviewed=if name=="jq" then original.overrideAttrs (old: {
        passthru=(old.passthru or {}) // {captureFixtureAllowedDrvs=[pkgs.removeReferencesTo.drvPath];};
      }) else original;
    in collect ({package=reviewed;inherit policy;limitBytes=536870912;}
      # fmt's unchanged test build measured 1.5 GiB RAM and filled the old
      # 1 GiB outer filesystem at finalization. Rebalance the same 3 GiB cap.
      // pkgs.lib.optionalAttrs (name=="fmt") {
        limitBytes=1073741824;artifactLimitBytes=134217728;invocationLimitBytes=536870912;
        scratchBytes=1342177280;
      });
  realPackage = name: realPackagePolicy name "required";
  ltoCompilerCase = original: family: flag: let cc=wrapCompiler original family "${family}-trace";c=if family=="gcc" then "gcc" else "clang";
  in collect {policy="required";package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-${family}-${if flag=="-flto=thin" then "thinlto" else "lto"}";
    dontUnpack=true;nativeBuildInputs=[cc pkgs.python3 pkgs.binutils];
    buildPhase=''
      mkdir a b
      printf 'int f(void); int main(void){return f()!=7;}\n' > a/same.c
      printf 'int f(void){return 7;}\n' > b/same.c
      (unset DATASET_CAPTURE_STAGE;${original}/bin/${c} ${flag} a/same.c b/same.c -o baseline)
      printf 'caller-owned sentinel\n' > program1.lto.o
      printf 'caller-owned sentinel\n' > program2.lto.o
      ${cc}/bin/${c} ${flag} a/same.c b/same.c -o program
      ${pkgs.lib.optionalString (original.version=="16.0.6" && flag=="-flto=thin") ''
        for attempt in {1..10}; do ${cc}/bin/${c} ${flag} a/same.c b/same.c -o program; done
      ''}
      ./baseline
      ./program
      readelf --debug-dump=decodedline program > lines.txt
      test -s lines.txt
      python3 - ${./capabilities.py} <<'PY'
      import importlib.util,pathlib,sys
      spec=importlib.util.spec_from_file_location("capabilities",sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      assert module.section_hashes(pathlib.Path("baseline"),behavior=True)==module.section_hashes(pathlib.Path("program"),behavior=True)
      assert "same.c" in pathlib.Path("lines.txt").read_text()
      PY
      test ! -e same.o
      test ! -e same.i
      test ! -e same.s
      test ! -e program.lto.o
      test ! -e program.res
      test "$(cat program1.lto.o)" = caller-owned\ sentinel
      test "$(cat program2.lto.o)" = caller-owned\ sentinel
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  ltoCase = family: flag: ltoCompilerCase (if family=="gcc" then pkgs.stdenv.cc else pkgs.llvmPackages_21.clang) family flag;
  pchCompilerCase = original: let cc=wrapCompiler original "clang" "clang-trace";
  in collect {policy="required";package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-clang-${original.version}-pch-consumer";dontUnpack=true;
    passthru.captureFixtureAllowedDrvs=[original.drvPath] ++ (original.wrapperPrerequisiteDrvs or []);
    hardeningDisable=["all"];nativeBuildInputs=[cc pkgs.python3 pkgs.binutils];
    buildPhase=''
      printf 'static inline int square(int n){return n*n;}\n' > unit.h
      printf 'int main(void){return square(3)!=9;}\n' > unit.c
      smoke() {
        "$1/bin/clang" -x c-header -c unit.h -o unit.pch
        "$1/bin/clang" -include-pch unit.pch unit.c -o program
      }
      (unset DATASET_CAPTURE_STAGE;smoke ${original})
      cp program baseline
      rm unit.pch
      smoke ${cc}
      ./baseline
      ./program
      python3 - ${./capabilities.py} ${./encoding.py} ${pkgs.lib.escapeShellArg original.version} "$datasetCapture/experiment.json" <<'PY'
      import importlib.util,pathlib,sys
      spec=importlib.util.spec_from_file_location("capabilities",sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      expected=module.section_hashes(pathlib.Path("baseline"),behavior=True)
      actual=module.section_hashes(pathlib.Path("program"),behavior=True)
      before=module.section_hashes(pathlib.Path("baseline"),contents=True)
      after=module.section_hashes(pathlib.Path("program"),contents=True)
      if expected!=actual and sys.argv[3]=="3.4.2":
          spec=importlib.util.spec_from_file_location("encoding",sys.argv[2]);encoding=importlib.util.module_from_spec(spec);spec.loader.exec_module(encoding)
          evidence=encoding.verify(pathlib.Path("baseline"),pathlib.Path("program"),module)
          import json
          pathlib.Path(sys.argv[4]).write_text(json.dumps(dict(pch_encoding=evidence)))
      else:
          assert expected==actual,{key:dict(before=expected.get(key),after=actual.get(key),before_bytes=before.get(key,"")[:512],after_bytes=after.get(key,"")[:512]) for key in expected.keys()|actual.keys() if expected.get(key)!=actual.get(key)}
      PY
      test ! -e unit.i
      test ! -e unit.s
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  moduleCase = family: let
    original=if family=="gcc" then pkgs.stdenv.cc else pkgs.llvmPackages_21.clang;
    cc=wrapCompiler original family "${family}-trace";
    cxx=if family=="gcc" then "g++" else "clang++";
  in collect {policy="required";package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-${family}-module-consumer";dontUnpack=true;
    nativeBuildInputs=[cc pkgs.python3 pkgs.binutils];
    buildPhase=''
      printf 'export module Fixture;\nexport int square(int n){return n*n;}\n' > unit.cppm
      printf 'import Fixture;\nint main(void){return square(3)!=9;}\n' > consumer.cpp
      smoke() {
        ${if family=="clang" then ''
          "$1/bin/${cxx}" -std=c++20 --precompile unit.cppm -o Fixture.pcm
          "$1/bin/${cxx}" -std=c++20 -c Fixture.pcm -o interface.o
          "$1/bin/${cxx}" -std=c++20 -fmodule-file=Fixture=Fixture.pcm -c consumer.cpp -o consumer.o
        '' else ''
          "$1/bin/${cxx}" -std=c++20 -fmodules-ts -x c++ -c unit.cppm -o interface.o
          "$1/bin/${cxx}" -std=c++20 -fmodules-ts -c consumer.cpp -o consumer.o
        ''}
        "$1/bin/${cxx}" interface.o consumer.o -o program
      }
      (unset DATASET_CAPTURE_STAGE;smoke ${original})
      cp program baseline
      rm -rf gcm.cache Fixture.pcm interface.o consumer.o program
      smoke ${cc}
      ./baseline
      ./program
      python3 - ${./capabilities.py} <<'PY'
      import importlib.util,pathlib,sys
      spec=importlib.util.spec_from_file_location("capabilities",sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      assert module.section_hashes(pathlib.Path("baseline"),behavior=True)==module.section_hashes(pathlib.Path("program"),behavior=True)
      PY
      test ! -e unit.iim
      test ! -e unit.s
      test ! -e consumer.ii
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  rawDriverCase = family: let
    original=if family=="gcc" then pkgs.stdenv.cc else pkgs.llvmPackages_21.clang;
    cc=wrap family "${family}-trace";c=if family=="gcc" then "gcc" else "clang";
  in collect {policy="required";package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-raw-${family}-drivers";dontUnpack=true;
    nativeBuildInputs=[cc pkgs.python3 pkgs.binutils];
    buildPhase=''
      printf 'int square(int n){return n*n;} int main(void){return square(3)!=9;}\n' > unit.c
      # Raw drivers receive no Nix linker search flags. Exercise their native
      # compile interface, then link all three objects with the selected wrapper.
      (unset DATASET_CAPTURE_STAGE; ${original.cc}/bin/${c} -c unit.c -o baseline.o)
      (unset DATASET_CAPTURE_STAGE; ${cc.cc}/bin/${c} -c unit.c -o forward.o)
      ${cc.cc}/bin/${c} -c unit.c -o captured.o
      (unset DATASET_CAPTURE_STAGE; ${original}/bin/${c} baseline.o -o baseline)
      (unset DATASET_CAPTURE_STAGE; ${original}/bin/${c} forward.o -o forward)
      ${cc}/bin/${c} captured.o -o program
      ./baseline
      ./forward
      ./program
      python3 - ${./capabilities.py} <<'PY'
      import importlib.util,pathlib,sys
      spec=importlib.util.spec_from_file_location("capabilities",sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      expected=module.section_hashes(pathlib.Path("baseline"),behavior=True)
      assert expected==module.section_hashes(pathlib.Path("forward"),behavior=True)
      assert expected==module.section_hashes(pathlib.Path("program"),behavior=True)
      PY
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
in {
  pchClang = pchCompilerCase pkgs.llvmPackages_21.clang;
  moduleGcc = moduleCase "gcc";
  moduleClang = moduleCase "clang";
  pchClang34 = pchCompilerCase repositoryFlake._crossToolchainMap.x86_64-linux.x86_64.clang3_4.cc;
  clang16Lto = ltoCompilerCase repositoryFlake._crossToolchainMap.x86_64-linux.x86_64.clang16.cc "clang" "-flto";
  clang16ThinLto = ltoCompilerCase repositoryFlake._crossToolchainMap.x86_64-linux.x86_64.clang16.cc "clang" "-flto=thin";
  compilerCache = let cc=wrap "gcc" "gcc-trace"; in collect {policy="required";package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-compiler-cache";dontUnpack=true;
    nativeBuildInputs=[cc pkgs.ccache pkgs.python3];
    buildPhase=''
      export CCACHE_DIR="$TMPDIR/compiler-cache" CCACHE_COMPILERCHECK=content
      printf 'int main(void){return 0;}\n' > unit.c
      (unset DATASET_CAPTURE_STAGE CCACHE_DISABLE
       ccache ${cc}/bin/gcc -c unit.c -o unit.o
       rm unit.o
       ccache ${cc}/bin/gcc -c unit.c -o unit.o
       ccache --print-stats > cache-stats.txt
       ${cc}/bin/gcc unit.o -o baseline)
      python3 - <<'PY'
      from pathlib import Path
      stats=dict(line.split() for line in Path("cache-stats.txt").read_text().splitlines())
      assert int(stats.get("direct_cache_hit",0))+int(stats.get("preprocessed_cache_hit",0))>=1,stats
      PY
      test "$CCACHE_DISABLE" = 1
      ccache ${cc}/bin/gcc -c unit.c -o unit.o
      ${cc}/bin/gcc unit.o -o program
      ./program
      python3 - ${./capabilities.py} <<'PY'
      import importlib.util,pathlib,sys
      spec=importlib.util.spec_from_file_location("capabilities",sys.argv[1]);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
      assert module.section_hashes(pathlib.Path("baseline"),behavior=True)==module.section_hashes(pathlib.Path("program"),behavior=True)
      PY
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  directLto = let cc=wrap "clang" "clang-trace";plugin="${pkgs.lib.getLib pkgs.llvmPackages_21.llvm}/lib/LLVMgold.so";
    stdenv=(import ../../lib/capture-stdenv.nix {inherit pkgs;}) {stdenv=pkgs.stdenv;inherit cc;};
  in collect {policy="required";package=stdenv.mkDerivation {
    name="capture-direct-llvm-linker-plugin";dontUnpack=true;
    buildPhase=''
      printf 'void _start(void){__asm__ volatile("mov $60, %%eax; xor %%edi, %%edi; syscall");}\n' > unit.c
      cc -flto=thin -g -ffreestanding -fno-stack-protector -c unit.c -o unit.o
      ld -plugin ${plugin} unit.o -o program
      ./program
      test ! -e unit.o.0.preopt.bc
      test ! -e unit.o.5.precodegen.bc
      test ! -e program.lto.o
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  rawGcc=rawDriverCase "gcc";
  rawClang=rawDriverCase "clang";
  ltoGcc=ltoCase "gcc" "-flto=1";
  ltoClang=ltoCase "clang" "-flto";
  thinLtoClang=ltoCase "clang" "-flto=thin";
  realHello = realPackage "hello";
  realHelloObserve = realPackagePolicy "hello" "observe";
  realLz4 = realPackage "lz4";
  realFmt = realPackage "fmt";
  realFmtObserve = realPackagePolicy "fmt" "observe";
  realZlib = realPackage "zlib";
  realZlibObserve = realPackagePolicy "zlib" "observe";
  realJq = realPackage "jq";
  realInih = realPackage "inih";
  realInihObserve = realPackagePolicy "inih" "observe";
  directTools = let
    cc=wrap "gcc" "gcc-trace";
    stdenv=(import ../../lib/capture-stdenv.nix {inherit pkgs;}) {stdenv=pkgs.stdenv;inherit cc;};
  in collect {policy="required";package=stdenv.mkDerivation {
    name="capture-direct-binutils";dontUnpack=true;
    buildPhase=''
      test "$(readlink -f "$(type -p as)")" = "$(readlink -f ${cc.bintools}/bin/as)"
      test "$(readlink -f "$(type -p ld)")" = "$(readlink -f ${cc.bintools}/bin/ld)"
      printf '.text\n.globl _start\n_start:\n mov $60, %%eax\n xor %%edi, %%edi\n syscall\n' > unit.s
      printf 'int square(int n){return n*n;}\n' > helper.c
      cc -c helper.c -o helper.o
      as unit.s -o unit.o
      ld unit.o helper.o -o program
      ld -Map caller.map unit.o helper.o -o program
      test -s caller.map
      ./program
    '';
    installPhase=''mkdir -p "$out/bin";cp program "$out/bin/program"'';
  };};
  outputContract = contract captured;
  unstructuredOutput = contract (capture { package = base.overrideAttrs { __structuredAttrs = false; }; });
  structuredOutput = contract (capture { package = base.overrideAttrs { __structuredAttrs = true; }; });
  dontFixupOutput = contract (capture { package = base.overrideAttrs { dontFixup = true; }; });
  customPhasesOutput = contract (capture { package = base.overrideAttrs {
    phases = [ "customPhase" ];
    customPhase = ''
      mkdir -p "$out" "$dev"
      test -d "$datasetCapture"
      echo custom > "$out/marker"
    '';
  }; });
  emptyOutput = capture { package = pkgs.runCommand "capture-empty-output" {} ''mkdir -p "$out"''; };
  restrictedOutput = contract (capture { package = base.overrideAttrs {
    allowedReferences = [ "out" ];
  }; });
  capabilityGccBaseline = capability "gcc" "baseline";
  capabilityGccSaved = capability "gcc" "saved";
  capabilityClangBaseline = capability "clang" "baseline";
  capabilityClangSaved = capability "clang" "saved";
  capabilityGccJobs = capability "gcc" "jobs";
  capabilityClangJobs = capability "clang" "jobs";
  # importCargoLock's vendor directory only links the already cached, locked
  # crate sources. Its exact derivation is an explicit fixture prerequisite.
  supervisorPackage = (capture { package = captureTool; }) // {
    captureFixtureAllowedDrvs=[captureTool.cargoDeps.drvPath];
  };
  quotaProbe = capture { package = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-quota-probe";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.python3 ];
    buildPhase = ''python3 ${./quota_probe.py} "$datasetCapture/experiment.json"'';
    installPhase = ''mkdir -p "$out"'';
  }; };
  ptraceProbe = capture { package = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-ptrace-probe";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.stdenv.cc ];
    buildPhase = ''
      cc ${./ptrace_probe.c} -o probe
      ./probe > "$datasetCapture/experiment.json"
    '';
    installPhase = ''mkdir -p "$out"'';
  }; };
  namespaceProbe = capture { package = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-namespace-probe";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.util-linuxMinimal pkgs.util-linuxMinimal.mount ];
    buildPhase = ''
      export ORIGINAL_UID=$(id -u)
      unshare --user --map-current-user --mount sh -c '
        test "$(id -u)" = "$ORIGINAL_UID"
        mount -t tmpfs -o size=67108864,nr_inodes=8192 tmpfs "$DATASET_CAPTURE_STAGE"
        test "$(stat -f -c %T "$DATASET_CAPTURE_STAGE")" = tmpfs
      '
    '';
    installPhase = ''mkdir -p "$out"'';
  }; };
  supervisorIsolation = collect { package = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-supervisor-isolation";
    dontUnpack = true;
    buildPhase = ''
      mkdir -p "$out/scratch"
      export ORIGINAL_UID=$(id -u)
      # Store outputs are on the overlay filesystem, so the supervisor must
      # create a new bounded mount even in the experiment's outer tmpfs.
      ${captureTool}/bin/compiler-capture isolate "$out/scratch" 67108864 ${pkgs.stdenv.shell} -c '
        test "$(id -u)" = "$ORIGINAL_UID"
        test "$(stat -f -c %T "$out/scratch")" = tmpfs
        ${captureTool}/bin/compiler-capture init "$out/scratch"
        ${captureTool}/bin/compiler-capture finalize "$out/scratch" "$out/isolated-capture" observe
        touch "$out/scratch/private-file"
        echo "$ORIGINAL_UID" > "$out/builder-uid"
      '
      test ! -e "$out/scratch/private-file"
    '';
    installPhase = ''test -f "$out/builder-uid"'';
  }; };
  parallelView = collect {package=pkgs.stdenvNoCC.mkDerivation {
    name="capture-parallel-view";
    dontUnpack=true;
    nativeBuildInputs=[(wrap "gcc" "gcc-trace")];
    buildPhase=''
      for number in 1 2 3 4; do
        printf 'int square%s(int n){return n*n;}\n' "$number" > "unit$number.c"
        cc -c "unit$number.c" -o "unit$number.o" &
      done
      wait
      for number in 1 2 3 4; do test -f "unit$number.o"; done
      test "$(find "$DATASET_CAPTURE_STAGE/records" -type f | wc -l)" -ge 4
      test "$(find . -name '*.i' -o -name '*.s' | wc -l)" = 0
    '';
    installPhase=''mkdir -p "$out"'';
  };policy="required";};
  forwardingGcc = intercepted "gcc" "forward";
  forwardingClang = intercepted "clang" "forward";
  captureGcc = intercepted "gcc" "gcc-dumpdir";
  captureClang = intercepted "clang" "observe";
  captureGccTrace = intercepted "gcc" "gcc-trace";
  captureClangTrace = intercepted "clang" "clang-trace";
  corpusRoles =
    let
      package = pkgs.stdenv.mkDerivation {
        name = "capture-role-payload";
        outputs = [ "out" "dev" "debug" "datasetCapture" ];
        dontUnpack = true;
        dontStrip = true;
        buildPhase = ''
          printf 'int main(void) { return 0; }\n' > unit.c
          cc -g -c unit.c -o unit.o
          cc unit.o -o program
        '';
        installPhase = ''
          mkdir -p "$out/bin" "$dev" "$debug" "$datasetCapture"
          cp program "$out/bin/program"
          cp unit.o "$out/relocatable-object"
          cp program "$debug/debug-output-program"
          cp program "$datasetCapture/capture-output-program"
        '';
        passthru.datasetOutputRoles = { debug = "debug"; datasetCapture = "compiler-capture"; };
      };
      folder = (import ../../lib/mkBinaryFolder.nix {inherit pkgs;inherit (pkgs) lib;}) {
        variantPkg = package;
        variantLabel = "capture-role-fixture";
        meta = { fixture = true; };
      };
    in capture {package=folder.overrideAttrs (old: {
      buildCommand = old.buildCommand + ''
        test "$(ls -A "$out/elf")" = program
        test -L "$out/auxiliary/debug"
        test -L "$out/auxiliary/datasetCapture"
      '';
    });};
  fixupGcc = let cc = wrap "gcc" "gcc-dumpdir"; in collect { package = pkgs.stdenvNoCC.mkDerivation {
    name = "capture-fixup-mutation";
    dontUnpack = true;
    nativeBuildInputs = [ cc pkgs.binutils ];
    buildPhase = ''
      printf 'int main(void) { return 0; }\n' > original.c
      ${cc}/bin/gcc -g -c original.c -o original.o
      sha256sum original.o > "$datasetCapture/expected-object-sha256"
      ${cc}/bin/gcc original.o -o program
      sha256sum program > "$datasetCapture/expected-program-sha256"
      printf mutated > original.o
      rm original.o
      strip program
    '';
    installPhase = ''mkdir -p "$out/bin"; cp program "$out/bin/program"'';
    postFixup = ''test -x "$out/bin/program"'';
  }; };
}

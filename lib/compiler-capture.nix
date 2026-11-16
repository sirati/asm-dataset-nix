# Adapt the existing wrapper without re-running its flag assembly or rebuilding
# the compiler. Each entry point gets immutable facts and its own real driver.
# This module deliberately requires a measured list of entry points.
{ pkgs, captureTool ? pkgs.buildPackages.callPackage ../nix/compiler-capture.nix {} }:
{ cc, family, version, target, entries, backend ? "observe", debugPolicy ? "required", nixResponseDefault ? null }:
let
  lib = pkgs.lib;
  needsCfiTools=family=="clang" && lib.versionAtLeast version "3.8" && lib.versionOlder version "3.9";
  assemblerSearch=pkgs.buildPackages.runCommand "capture-clang38-assembler-search" {} ''
    mkdir -p "$out"
    ln -s ${cc.bintools}/bin/${cc.bintools.targetPrefix or ""}as "$out/as"
    ln -sf ${cc.bintools}/bin/${cc.bintools.targetPrefix or ""}as "$out/${cc.bintools.targetPrefix or ""}as"
  '';
  selectedWrapper=if needsCfiTools then pkgs.buildPackages.symlinkJoin {
    name="capture-${cc.name}-cfi-tools";paths=[cc];postBuild=''
      printf '%s\n' ${assemblerSearch} > "$out/nix-support/capture-assembler-search"
    '';
  } else cc;
  rawAdapted=(import ./capture-raw-compiler.nix {inherit pkgs captureTool;}) {
    cc=cc.cc;inherit family version target entries debugPolicy;wrapper=selectedWrapper;
  };
  rawCompiler=cc.cc // rawAdapted // {inherit (cc.cc) meta;};
  capturedBintools=(import ./capture-bintools.nix {inherit pkgs captureTool;}) {
    bintools=cc.bintools;inherit target debugPolicy;
  };
  effectiveEntries=map (entry: entry // {executable=(cc.datasetDriverExecutables or {}).${entry.name} or entry.executable;}) entries;
  entryCommands = lib.concatMapStringsSep "\n" (entry:
    let
      template = pkgs.writeText "capture-${entry.name}-template.json" (builtins.toJSON {
        schema = 1;
        wrapper_revision = 1;
        executable = entry.executable;
        inherit family version target backend;
        driver = entry.driver;
        supervisor = "${captureTool}/bin/compiler-capture";
        debug_policy = debugPolicy;
        nix_response_default = nixResponseDefault;
        wrapper = toString selectedWrapper;
      });
      config = pkgs.buildPackages.runCommand "capture-${entry.name}.json" {
        nativeBuildInputs = [ pkgs.buildPackages.python3 ];
      } ''
        python3 - ${lib.escapeShellArg "${selectedWrapper}/bin/${entry.name}"} ${template} "$out" ${if entry.optional or false then "optional" else "required"} <<'PY'
        import json, os, re, sys
        wrapper, template, output, availability = sys.argv[1:]
        if availability == "optional" and not os.path.exists(wrapper):
            with open(output, "w") as file:
                file.write("null\n")
            sys.exit(0)
        with open(template) as file:
            config = json.load(file)
        with open(wrapper) as file:
            match = re.search(r'NIX_CC_USE_RESPONSE_FILE:-([01])', file.read())
        measured = bool(match and match[1] == "1")
        declared = config["nix_response_default"]
        if declared is not None and declared != measured:
            raise ValueError("declared response default contradicts the selected Nix wrapper")
        config["nix_response_default"] = measured
        with open(output, "w") as file:
            json.dump(config, file, separators=(",", ":"))
        PY
      '';
      launcher = pkgs.writeShellScript "capture-${entry.name}" ''
        exec ${captureTool}/bin/compiler-capture --config ${config} -- "$@"
      '';
    in ''
      ${lib.optionalString (entry.optional or false) ''if test -e ${cc}/bin/${entry.name}; then''}
      # Dereference only this wrapper script; preserve all other support files,
      # symlinks, resource directories and compatibility modifications.
      test "$(head -c 2 ${cc}/bin/${entry.name})" = '#!'
      cp --remove-destination ${selectedWrapper}/bin/${entry.name} "$out/bin/${entry.name}"
      substituteInPlace "$out/bin/${entry.name}" \
        --replace-fail ${lib.escapeShellArg entry.executable} ${lib.escapeShellArg (toString launcher)}
      chmod +x "$out/bin/${entry.name}"
      ln -s ${config} "$out/nix-support/capture-${entry.name}.json"
      ${lib.optionalString (entry.optional or false) "fi"}
    '') effectiveEntries;
  adapted = pkgs.buildPackages.symlinkJoin {
    name = "${cc.name}-capture";
    paths = [ selectedWrapper ];
    postBuild = ''
      # symlinkJoin makes directory trees containing symlinks to files. Never
      # write through a support-file symlink into the immutable original.
      ${entryCommands}
      # The original cc setup hook adds its unwrapped compiler bin directory
      # before propagated binutils. Some GCC outputs contain an as symlink.
      # Put the selected tool adapters alongside cc so direct calls reach them.
      for executable in ${capturedBintools}/bin/*; do
        name="''${executable##*/}"
        case "$name" in
          ${cc.targetPrefix or ""}as|${cc.targetPrefix or ""}ld|${cc.targetPrefix or ""}ld.bfd|${cc.targetPrefix or ""}ld.gold|${cc.targetPrefix or ""}ld.lld)
            ln -sf "$executable" "$out/bin/$name";;
        esac
      done
      printf '%s\n' ${rawCompiler} > "$out/nix-support/.orig-cc.capture"
      mv -T "$out/nix-support/.orig-cc.capture" "$out/nix-support/orig-cc"
      for support in propagated-build-inputs propagated-native-build-inputs; do
        if test -f "$out/nix-support/$support"; then
          cp --remove-destination "$out/nix-support/$support" "$out/nix-support/.$support.capture"
          mv "$out/nix-support/.$support.capture" "$out/nix-support/$support"
          substituteInPlace "$out/nix-support/$support" --replace ${cc.bintools} ${capturedBintools}
        fi
      done
    '';
  };
in
assert entries != [];
assert builtins.elem debugPolicy [ "required" "preserve" ];
assert lib.all (entry: builtins.match "[a-zA-Z0-9_+.-]+" entry.name != null) entries;
assert builtins.elem family [ "gcc" "clang" ];
assert builtins.elem backend [ "forward" "observe" "gcc-dumpdir" "gcc-trace" "clang-trace" ];
# Attribute compatibility is intentional. Keep the original wrapper metadata,
# override interface, libc and bintools alongside the adapter's derivation.
cc // adapted // {
  cc=rawCompiler;
  bintools=capturedBintools;
  inherit (cc) meta;
  passthru = (cc.passthru or {}) // { datasetCapture = {
    inherit captureTool entries backend;
    schema = 1;
    identity = { inherit family version target; wrapper = toString selectedWrapper;
      bintools_wrapper = toString cc.bintools; bintools_version = cc.bintools.version; };
  }; };
  datasetCapture = {
    inherit captureTool entries backend;
    schema = 1;
    identity = { inherit family version target; wrapper = toString selectedWrapper;
      bintools_wrapper = toString cc.bintools; bintools_version = cc.bintools.version; };
  };
}

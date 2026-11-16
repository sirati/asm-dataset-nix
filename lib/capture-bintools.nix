# Direct assembler/linker calls retain the selected Nix wrappers. Compiler
# subprocesses already owned by the tracer exec through these adapters once.
{ pkgs, captureTool }:
{ bintools, target, debugPolicy ? "required" }:
let
  lib=pkgs.lib;
  prefix=bintools.targetPrefix or "";
  entries=[ {tool="as";family="assembler";} {tool="ld";family="linker";}
    {tool="ld.bfd";family="linker";} {tool="ld.gold";family="linker";} {tool="ld.lld";family="linker";} ];
  adapted=pkgs.buildPackages.symlinkJoin {
    name="${bintools.name}-capture";
    paths=[bintools];
    postBuild=lib.concatMapStringsSep "\n" (entry:
      let
        name="${prefix}${entry.tool}";
        config=pkgs.writeText "capture-${name}.json" (builtins.toJSON {
          schema=1;wrapper_revision=1;
          executable="${bintools}/bin/${name}";
          family=entry.family;version=bintools.version;inherit target;
          driver=name;backend="tool-observe";
          supervisor="${captureTool}/bin/compiler-capture";
          debug_policy=debugPolicy;nix_response_default=false;wrapper=toString bintools;
        });
        launcher=pkgs.writeShellScript "capture-${name}" ''
          exec ${captureTool}/bin/compiler-capture --config ${config} -- "$@"
        '';
      in ''
        if test -e ${bintools}/bin/${name}; then
          ln -sf ${launcher} "$out/bin/${name}"
          ln -s ${config} "$out/nix-support/capture-${name}.json"
        fi
      '') entries;
  };
in bintools // adapted // { inherit (bintools) meta; }

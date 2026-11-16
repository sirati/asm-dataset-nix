# Select a measured LTO linker while preserving the Nix linker's flag assembly.
{pkgs}:
{cc,lld ? null,gold ? null,bfd ? null,riscvAbi ? false,targetAbi ? ""}:
let
  lib=pkgs.lib;
  prefix=cc.targetPrefix or "";
  hasLinker=lld!=null || gold!=null || bfd!=null;
  tool=if lld!=null then "ld.lld" else if bfd!=null then "ld.bfd" else "ld.gold";
  program=if lld!=null then "${lld}/bin/ld.lld" else if bfd!=null then "${bfd}/bin/${bfd.targetPrefix or ""}ld.bfd" else "${gold}/bin/ld.gold";
  oldSearch=hasLinker && lld==null && lib.versionOlder cc.version "3.9";
  linker=pkgs.buildPackages.runCommand "capture-clang-${cc.version}-lto-linker" {
    nativeBuildInputs=[pkgs.buildPackages.python3];
  } ''
    mkdir -p "$out/bin"
    python3 - ${cc.bintools} ${lib.escapeShellArg prefix} ${program} "$out/bin/${prefix}${tool}" <<'PY'
    import pathlib,sys
    bintools,prefix,lld,output=sys.argv[1:]
    root=pathlib.Path(bintools)
    raw=(root/'nix-support/orig-bintools').read_text().strip()
    script=(root/'bin'/(prefix+'ld')).read_text()
    expected=raw+'/bin/'+prefix+'ld'
    if not script.startswith('#!') or expected not in script:
        raise ValueError('selected linker wrapper has no measured execution point')
    path=pathlib.Path(output);path.write_text(script.replace(expected,lld));path.chmod(0o755)
    PY
    ${lib.optionalString oldSearch ''
      for name in ld ${prefix}ld ${builtins.replaceStrings ["-gnuabielfv2-"] ["-gnu-"] prefix}ld; do
        ln -sf ${prefix}${tool} "$out/bin/$name"
      done
    ''}
  '';
  choice=if !hasLinker then "" else if oldSearch then "-B${linker}/bin/" else "-fuse-ld=${linker}/bin/${prefix}${tool}";
  fragment=pkgs.writeText "capture-clang-lto-choice.sh" ''
    if [[ "$dontLink" != 1 ]]; then
      datasetLtoEnabled=0
      datasetExplicitLinker=0
      for option in "''${params[@]}"; do
        case "$option" in --) break;; -fuse-ld=*|--ld-path=*) datasetExplicitLinker=1;; esac
        ${lib.optionalString riscvAbi ''
        case "$option" in *-plugin-opt=-target-abi=*|*-plugin-opt=-mattr=*) datasetExplicitLinker=1;; esac
        ''}
      done
      for option in "''${extraBefore[@]}" "''${params[@]}" "''${extraAfter[@]}"; do
        case "$option" in --) break;; -fno-lto|-flto=none) datasetLtoEnabled=0;; -flto|-flto=*) datasetLtoEnabled=1;; esac
      done
      if [[ "$datasetLtoEnabled" = 1 && "$datasetExplicitLinker" = 0 ]]; then
        datasetLtoArguments=()
        ${if riscvAbi then ''
        datasetTargetAbi=lp64d
        datasetTargetIsa=rv64gc
        for option in "''${extraBefore[@]}" "''${params[@]}" "''${extraAfter[@]}"; do
          case "$option" in --) break;; -mabi=*) datasetTargetAbi="''${option#-mabi=}";; -march=*) datasetTargetIsa="''${option#-march=}";; esac
        done
        datasetTargetFeatures=
        case "$datasetTargetIsa" in
          rv64gc|rv64imafdc) datasetTargetFeatures=+m,+a,+f,+d,+c;;
          rv64g|rv64imafd) datasetTargetFeatures=+m,+a,+f,+d;;
          rv64imac) datasetTargetFeatures=+m,+a,+c;;
          rv64ima) datasetTargetFeatures=+m,+a;;
          rv64i) datasetTargetFeatures=+64bit;;
        esac
        if [[ -n "$datasetTargetFeatures" && "$datasetTargetAbi" =~ ^lp64[fd]?$ ]]; then
          datasetLtoArguments=(-Xlinker "-plugin-opt=-mattr=$datasetTargetFeatures" -Xlinker "-plugin-opt=-target-abi=$datasetTargetAbi")
        else
          datasetLtoArguments=()
        fi
        '' else if oldSearch then ''
        extraBefore=("${choice}" "''${extraBefore[@]}")
        '' else lib.optionalString hasLinker ''
        datasetLtoArguments=("${choice}")
        ''}
        ${lib.optionalString (targetAbi!="") ''
        datasetExplicitAbi=0
        datasetTargetAbi=${targetAbi}
        for option in "''${extraBefore[@]}" "''${params[@]}" "''${extraAfter[@]}"; do
          case "$option" in
            --) break;;
            *-plugin-opt=-target-abi=*) datasetExplicitAbi=1;;
            -mabi=elfv1|-mabi=elfv2|-mabi=n32|-mabi=o32|-mabi=n64) datasetTargetAbi="''${option#-mabi=}";;
            -mabi=32) datasetTargetAbi=o32;;
            -mabi=64) datasetTargetAbi=n64;;
          esac
        done
        if [[ "$datasetExplicitAbi" = 0 ]]; then
          datasetLtoArguments+=(-Xlinker "-plugin-opt=-target-abi=$datasetTargetAbi")
        fi
        ${lib.optionalString (targetAbi=="n32" && lib.versionOlder cc.version "8") ''
        # This LLVM release selects the object class from the architecture in
        # the triple, even with target-abi=n32. Keep its MIPS64 CPU and N32 ABI,
        # selecting the 32-bit ELF container required by that ABI.
        datasetExplicitTriple=0
        for option in "''${extraBefore[@]}" "''${params[@]}" "''${extraAfter[@]}"; do
          case "$option" in --) break;; *-plugin-opt=mtriple=*) datasetExplicitTriple=1;; esac
        done
        if [[ "$datasetTargetAbi" = n32 && "$datasetExplicitTriple" = 0 ]]; then
          datasetLtoArguments+=(-Xlinker -plugin-opt=mtriple=mipsel-unknown-linux-gnuabin32)
        fi
        ''}
        ''}
        for datasetLtoArgument in "''${datasetLtoArguments[@]}"; do
        datasetLinkerInserted=0
        for ((datasetLinkerIndex=0;datasetLinkerIndex<''${#extraAfter[@]};datasetLinkerIndex++)); do
          if [[ "''${extraAfter[datasetLinkerIndex]}" = -- ]]; then
            extraAfter=("''${extraAfter[@]:0:datasetLinkerIndex}" "$datasetLtoArgument" "''${extraAfter[@]:datasetLinkerIndex}")
            datasetLinkerInserted=1
            break
          fi
        done
        if [[ "$datasetLinkerInserted" = 0 ]]; then
          extraAfter+=("$datasetLtoArgument")
        fi
        done
      fi
    fi
  '';
  adapted=pkgs.buildPackages.symlinkJoin {
    name="capture-${cc.name}-lto-linker";paths=[cc];
    nativeBuildInputs=[pkgs.buildPackages.python3];
    postBuild=''
      python3 - ${cc} "$out" ${fragment} ${lib.escapeShellArg prefix} <<'PY'
      import pathlib,sys
      original,output,fragment,prefix=sys.argv[1:]
      marker='if (( "''${NIX_CC_USE_RESPONSE_FILE:-1}" >= 1 )); then'
      for name in ['clang','clang++','cc','c++','cpp']:
          source=pathlib.Path(original)/'bin'/(prefix+name)
          if not source.exists():continue
          data=source.read_text()
          # This point follows flag assembly and precedes response-file creation.
          selected=marker if data.count(marker)==1 else None
          if selected is None:raise ValueError('selected compiler wrapper has no measured response dispatch')
          destination=pathlib.Path(output)/'bin'/(prefix+name)
          destination.unlink();destination.write_text(data.replace(selected,pathlib.Path(fragment).read_text()+'\n'+selected,1));destination.chmod(0o755)
      PY
    '';
  };
in assert builtins.elem targetAbi ["" "elfv2" "n32" "o32"];
assert (hasLinker && builtins.length (builtins.filter (value: value!=null) [lld gold bfd])==1) || (riscvAbi || targetAbi!="") && !hasLinker;
cc // adapted // {
  inherit (cc) meta;
  datasetLtoLinker=if hasLinker then linker else null;
  wrapperPrerequisiteDrvs=[cc.drvPath] ++ (cc.wrapperPrerequisiteDrvs or []);
}

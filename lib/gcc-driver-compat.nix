# Adapt cached GCC drivers without rebuilding their compiler derivations.
{pkgs}:
{cc,bintools ? cc.bintools,linkerCompiler ? null,runtimeCompiler ? null,arguments ? []}:
let
  lib=pkgs.lib;
  prefix=cc.targetPrefix or "";
  triple=lib.removeSuffix "-" prefix;
  runtime=if runtimeCompiler==null then null else pkgs.buildPackages.runCommand "capture-gcc-runtime-search" {} ''
    mkdir -p "$out"
    for file in ${runtimeCompiler}/lib/gcc/${triple}/${runtimeCompiler.version}/crt*.o \
      ${runtimeCompiler}/lib/gcc/${triple}/${runtimeCompiler.version}/libgcc*.a \
      ${lib.getLib runtimeCompiler}/${triple}/lib/libgcc*.so* \
      ${lib.getLib runtimeCompiler}/${triple}/lib/libstdc++*.so* \
      ${lib.getLib runtimeCompiler}/${triple}/lib/libsupc++*.a; do
      if test -e "$file"; then ln -sf "$file" "$out/''${file##*/}"; fi
    done
  '';
  search=if linkerCompiler==null then null else pkgs.buildPackages.runCommand "capture-gcc-linker-search" {} ''
    mkdir -p "$out/bin"
    ln -s ${bintools}/bin/${prefix}ld "$out/bin/ld"
    ln -s ${bintools}/bin/${prefix}ld "$out/bin/collect-ld"
    ln -s ${bintools}/bin/${prefix}as "$out/bin/as"
    ln -s ${bintools}/bin/${prefix}ld "$out/bin/${prefix}ld"
    ln -s ${bintools}/bin/${prefix}as "$out/bin/${prefix}as"
    ln -s ${linkerCompiler}/libexec/gcc/${lib.removeSuffix "-" prefix}/${linkerCompiler.version}/collect2 "$out/bin/collect2"
  '';
  entries=[ {name="${prefix}gcc";real="${prefix}gcc";} {name="${prefix}g++";real="${prefix}g++";}
    {name="${prefix}cc";real="${prefix}gcc";} {name="${prefix}c++";real="${prefix}g++";} {name="${prefix}cpp";real="${prefix}cpp";} ];
  launchers=builtins.listToAttrs (map (entry: {name=entry.name;value=pkgs.writeShellScript "capture-gcc-compat-${entry.name}" ''
    exec ${cc.cc}/bin/${entry.real} ${lib.optionalString (search!=null) "-B${search}/bin/"} ${lib.optionalString (runtime!=null) "-B${runtime}/ -L${runtime}/"} ${lib.escapeShellArgs arguments} "$@"
  '';}) entries);
  adapted=pkgs.buildPackages.symlinkJoin {
    name="${cc.name}-driver-compat";paths=[cc];
    postBuild=lib.concatMapStringsSep "\n" (entry: ''
      if test -e ${cc}/bin/${entry.name}; then
        cp --remove-destination ${cc}/bin/${entry.name} "$out/bin/${entry.name}"
        substituteInPlace "$out/bin/${entry.name}" --replace-fail ${cc.cc}/bin/${entry.real} ${launchers.${entry.name}}
      fi
    '') entries + ''
      for executable in ${bintools}/bin/*; do ln -sf "$executable" "$out/bin/''${executable##*/}"; done
      if test -f "$out/nix-support/propagated-build-inputs"; then
        cp "$out/nix-support/propagated-build-inputs" "$out/nix-support/.propagated.capture"
        mv "$out/nix-support/.propagated.capture" "$out/nix-support/propagated-build-inputs"
        substituteInPlace "$out/nix-support/propagated-build-inputs" --replace ${cc.bintools} ${bintools}
      fi
    '';
  };
in cc // adapted // {
  inherit bintools;datasetDriverExecutables=launchers;inherit (cc) meta;
  wrapperPrerequisiteDrvs=[cc.drvPath] ++ (cc.wrapperPrerequisiteDrvs or []);
}

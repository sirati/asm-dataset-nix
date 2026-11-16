# Correct response encoding and standard-library compatibility for old Clang. This
# compatibility adapter is shared by baseline and capture toolchain resolution.
{ pkgs }:
cc:
let
  lib=pkgs.lib;
  prefix=cc.targetPrefix or "";
  quoteSource=builtins.toFile "capture-legacy-response-quote.sh" (builtins.readFile ./legacy-response-quote.sh);
  stdlibHeader=builtins.toFile "capture-legacy-clang-stdlib.h" (builtins.readFile ./legacy-clang-stdlib.h);
  stdlibHook=builtins.toFile "capture-legacy-clang-stdlib.sh" (builtins.readFile ./legacy-clang-stdlib.sh);
  preferPic=lib.versionOlder cc.version "3.5" && lib.hasPrefix "mips" prefix;
  armEhabi=lib.versionOlder cc.version "3.5" && lib.hasPrefix "arm" prefix;
  names=map (name: "${prefix}${name}") ["clang" "clang++" "cc" "c++" "cpp"];
  fixed=pkgs.buildPackages.symlinkJoin {
    name="${cc.name}-response-fixed";
    paths=[cc];
    nativeBuildInputs=[pkgs.buildPackages.python3];
    postBuild=lib.concatMapStringsSep "\n" (name: ''
      if test -e ${cc}/bin/${name} && grep -F 'printf "%q\n"' ${cc}/bin/${name} >/dev/null; then
        test "$(head -c 2 ${cc}/bin/${name})" = '#!'
        {
          IFS= read -r shebang
          printf '%s\n' "$shebang"
          cat ${quoteSource}
          cat
        } < ${cc}/bin/${name} > "$out/bin/${name}.new"
        substituteInPlace "$out/bin/${name}.new" --replace-fail 'printf "%q\n"' _datasetLegacyClangResponse
        python3 - "$out/bin/${name}.new" ${stdlibHeader} ${stdlibHook} ${if preferPic then "1" else "0"} ${if armEhabi then "1" else "0"} <<'PY'
      import pathlib,re,sys
      path=pathlib.Path(sys.argv[1])
      data=path.read_text()
      markers=re.findall(r'^if [^\n]*NIX_CC_USE_RESPONSE_FILE[^\n]*then$',data,re.MULTILINE)
      if len(markers)!=1:
          raise ValueError('old Clang wrapper has no measured response dispatch')
      fragment='datasetLegacyStdlibHeader='+sys.argv[2]+'\ndatasetLegacyPreferPic='+sys.argv[4]+'\ndatasetLegacyArmEhabi='+sys.argv[5]+'\nsource '+sys.argv[3]+'\n'
      path.write_text(data.replace(markers[0],fragment+markers[0],1))
      PY
        mv -T "$out/bin/${name}.new" "$out/bin/${name}"
        chmod +x "$out/bin/${name}"
      fi
    '') names;
  };
in
if cc.legacyClangResponseFixed or false then cc else
cc // fixed // {
  inherit (cc) meta;
  legacyClangResponseFixed=true;
  wrapperPrerequisiteDrvs=[cc.drvPath] ++ (cc.wrapperPrerequisiteDrvs or []);
  passthru=(cc.passthru or {}) // {legacyClangResponseFixed=true;};
}

# Opt in one already selected package. Dependencies and package phases retain
# their original derivations. Finalization runs after the entire package builder,
# including custom phases, dontFixup, and post-fixup hooks.
{ pkgs, captureTool ? null }:
{ package, policy ? "observe", limitBytes ? 67108864, artifactLimitBytes ? 16777216, invocationLimitBytes ? 50331648, scratchBytes ? pkgs.lib.min 2147483648 (pkgs.lib.max 67108864 (limitBytes * 4)), reportUsage ? false, separateDebugInfo ? false, debugCompiler ? null }:
let
  lib = pkgs.lib;
  legacyCompression = separateDebugInfo && debugCompiler != null
    && debugCompiler.family == "clang" && lib.versionOlder debugCompiler.version "3.5";
  compressionCompatibility = ''
    # Remove one occurrence introduced by Nix's debug hook. Preserve duplicate
    # caller requests and execute the package's own postHook afterward.
    datasetDebugCompressionMarker=' -Wa,--compress-debug-sections '
    datasetDebugCompressionFlags="''${NIX_CFLAGS_COMPILE:-} "
    datasetDebugCompressionSuffix="''${datasetDebugCompressionFlags##*"$datasetDebugCompressionMarker"}"
    if [[ "$datasetDebugCompressionSuffix" != "$datasetDebugCompressionFlags" ]]; then
      datasetDebugCompressionPrefix="''${datasetDebugCompressionFlags%"$datasetDebugCompressionMarker$datasetDebugCompressionSuffix"}"
      export NIX_CFLAGS_COMPILE="$datasetDebugCompressionPrefix ''${datasetDebugCompressionSuffix% }"
    fi
    unset datasetDebugCompressionMarker datasetDebugCompressionFlags datasetDebugCompressionSuffix datasetDebugCompressionPrefix
  '';
  selectedPackage = if !separateDebugInfo then package else package.overrideAttrs (old: {
    separateDebugInfo = true;
    __structuredAttrs = (old.__structuredAttrs or false) || lib.any (name: builtins.hasAttr name old)
      [ "allowedReferences" "allowedRequisites" "disallowedReferences" "disallowedRequisites" ];
    # mkDerivation appends the debug output itself. Preserve the package's
    # ordinary outputs and avoid declaring debug twice on an existing split.
    outputs = builtins.filter (output: output != "debug") (old.outputs or [ "out" ]);
    passthru = (old.passthru or {}) // {
      datasetOutputRoles = (old.passthru.datasetOutputRoles or {}) // { debug = "debug"; };
      datasetSeparateDebugCompatibility = if legacyCompression then "clang-3.4-without-hook-assembler-compression" else null;
    };
  } // lib.optionalAttrs legacyCompression (
    if (old.env or {}) ? postHook then {
      env = old.env // { postHook = compressionCompatibility + "\n" + toString old.env.postHook; };
    } else { postHook = compressionCompatibility + "\n" + toString (old.postHook or ""); }
  ));
  finish = pkgs.writeShellScript "dataset-capture-phases" ''
    set -euo pipefail
    ${lib.optionalString (captureTool != null) ''${captureTool}/bin/compiler-capture init "$DATASET_CAPTURE_STAGE"''}
    ${selectedPackage.drvAttrs.realBuilder or (selectedPackage.drvAttrs.builder or pkgs.stdenv.shell)} "$@"
    ${lib.optionalString (reportUsage && captureTool != null) ''
      ${pkgs.coreutils}/bin/stat -f -c '%S %b %f %a %c %d' "$DATASET_CAPTURE_STAGE" > "$datasetCapture/experiment-filesystem.txt"
    ''}
    ${if captureTool == null then ''
      printf '%s\n' '{"schema":1,"complete":false,"invocations":[],"artifacts":[],"coverage":"output-only-fixture"}' > "$datasetCapture/manifest.json"
    '' else ''
      ${captureTool}/bin/compiler-capture finalize "$DATASET_CAPTURE_STAGE" "$datasetCapture" ${policy}
    ''}
  '';
in
assert builtins.elem policy [ "observe" "required" ];
assert limitBytes > 0 && limitBytes <= 1073741824;
assert artifactLimitBytes > 0 && artifactLimitBytes <= 134217728;
assert invocationLimitBytes >= artifactLimitBytes && invocationLimitBytes <= 536870912;
assert scratchBytes >= 67108864 && scratchBytes <= 2147483648;
if package.datasetCaptureEnabled or false then
  assert !separateDebugInfo || (package.datasetSeparateDebugInfo or false);
  package
else
selectedPackage.overrideAttrs (old: {
  outputs = (old.outputs or [ "out" ]) ++ lib.optional (!(builtins.elem "datasetCapture" (old.outputs or [ "out" ]))) "datasetCapture";
  realBuilder = "${pkgs.writeShellScript "dataset-capture-builder" ''
    set -euo pipefail
    if [[ -n ''${NIX_ATTRS_SH_FILE:-} ]]; then
      source "$NIX_ATTRS_SH_FILE"
      export datasetCapture="''${outputs[datasetCapture]}"
      ${lib.optionalString separateDebugInfo ''export debug="''${outputs[debug]}"''}
    fi
    : "''${datasetCapture:?missing capture output}"
    case "$datasetCapture" in /*) ;; *) exit 125;; esac
    ${pkgs.coreutils}/bin/mkdir -p "$datasetCapture"
    ${lib.optionalString separateDebugInfo ''
      # Empty packages and dontFixup still have to produce declared outputs.
      # Publication separately requires real debug data for each installed ELF.
      ${pkgs.coreutils}/bin/mkdir -p "$debug"
    ''}
    export DATASET_CAPTURE_LIMIT=${toString limitBytes}
    export DATASET_CAPTURE_ARTIFACT_LIMIT=${toString artifactLimitBytes}
    export DATASET_CAPTURE_INVOCATION_LIMIT=${toString invocationLimitBytes}
    ${lib.optionalString (captureTool!=null) ''
      export CCACHE_DISABLE=1
      export SCCACHE_RECACHE=1
    ''}
    ${if captureTool == null then ''
      export DATASET_CAPTURE_STAGE="$NIX_BUILD_TOP/.dataset-capture-stage"
      ${pkgs.coreutils}/bin/mkdir -m 700 "$DATASET_CAPTURE_STAGE"
      exec ${finish} "$@"
    '' else ''
      export DATASET_CAPTURE_STAGE="$(${pkgs.coreutils}/bin/mktemp -d /tmp/.dataset-capture.XXXXXXXXXXXX)"
      captureViewAnchor="$(${pkgs.coreutils}/bin/cat ${captureTool}/nix-support/capture-view-anchor)"
      test -L "$captureViewAnchor/view"
      export DATASET_CAPTURE_VIEW="$captureViewAnchor/view"
      captureStatus=0
      ${captureTool}/bin/compiler-capture isolate "$DATASET_CAPTURE_STAGE" ${toString scratchBytes} ${finish} "$@" || captureStatus=$?
      ${captureTool}/bin/compiler-capture discard "$DATASET_CAPTURE_STAGE" || true
      exit "$captureStatus"
    ''}
  ''}";
  passthru = (old.passthru or {}) // {
    datasetCaptureEnabled = true;
    datasetCaptureSchema = 1;
    datasetCapturePolicy = policy;
    datasetCaptureLimitBytes = limitBytes;
    datasetCaptureArtifactLimitBytes=artifactLimitBytes;
    datasetCaptureInvocationLimitBytes=invocationLimitBytes;
    datasetCaptureScratchBytes = if captureTool == null then 0 else scratchBytes;
    datasetSeparateDebugInfo = separateDebugInfo;
    datasetOutputRoles = (old.passthru.datasetOutputRoles or {}) // { datasetCapture = "compiler-capture"; };
  };
})

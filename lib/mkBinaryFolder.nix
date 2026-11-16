# Filter ELF executables and shared libraries from a built variant into
# a flat folder of symlinks. No copies, no compression — the dataset
# publisher (compiler_suit_runner) deref-copies into out-network at
# runtime via the dynamic_runner publish API.
#
# Output layout per variant:
#   $out/elf/<basename>   symlink into ``variantPkg``'s store path
#                         (target's mode bits — including +x — are what
#                         readers see after following the link)
#   $out/meta.json        build metadata sidecar
#
# Symlink targets are absolute /nix/store paths, so nix's reference scanner
# captures variantPkg as a runtime dep and keeps it GC-rooted.
#
# Input: { variantLabel, variantPkg, meta } (output of mkVariant.nix)

{ pkgs, lib }:

{
  variantLabel,
  variantPkg,
  meta,
}:

let
  # Collect programs from every ordinary output, not just the default
  # output. A package is ONE build (one .drv) split into multiple outputs;
  # multi-output packages put binaries in non-default outputs. e.g. lz4 has
  # outputs = [ dev lib man out ] with the DEFAULT output `dev` (headers,
  # no ELF) — the lz4 executable lives in `out`, liblz4.so in `lib`.
  # Searching only the default output silently drops them, and we cannot
  # special-case per package. Capture and debug outputs retain separate roles.
  roles = variantPkg.datasetOutputRoles or {};
  collector = builtins.toFile "capture-collect-elf.py" (builtins.readFile ./collect-elf.py);
  outputRole = o: roles.${o} or (if o == "datasetCapture" then "compiler-capture" else if o == "debug" then "debug" else "package");
  ordinaryOutputs = builtins.filter (o: !(builtins.elem (outputRole o) [ "compiler-capture" "debug" ])) variantPkg.outputs;
  outPaths = map (o: "${variantPkg.${o}}") ordinaryOutputs;
  auxiliaryOutputs = builtins.filter (o: builtins.elem (outputRole o) [ "compiler-capture" "debug" ]) variantPkg.outputs;
  provenanceRequired = (meta.compilerCapture.publication_schema or 1) >= 2;
  source = variantPkg.src or null;
  provenance = {
    schema = 1;
    label = variantLabel;
    package = {
      label = meta.package or variantLabel;
      pname = variantPkg.pname or null;
      version = variantPkg.version or null;
    };
    build = {
      # This is an identity, not a request for every build-time output reachable
      # from the derivation. Keep the bytes without a deep drv string context.
      package_drv = builtins.unsafeDiscardStringContext variantPkg.drvPath;
      outputs = builtins.listToAttrs (map (name: {
        inherit name;
        value = { path = toString variantPkg.${name}; role = outputRole name; };
      }) variantPkg.outputs);
      source = {
        path = if source == null then null else toString source;
        revision = if builtins.isAttrs source then source.rev or null else null;
      };
      flake_lock_sha256 = builtins.hashFile "sha256" ../flake.lock;
      debug_policy = {
        separate_debug_info = true;
        hook_compatibility = variantPkg.datasetSeparateDebugCompatibility or null;
      };
    };
    toolchain = meta.expectedToolchain or null;
  };
  folderMeta = meta // {
    # Provenance text. A drvPath string carries a deep Nix context that
    # would otherwise realize every output in its bootstrap graph; the
    # package outputs referenced below already carry the build dependencies.
    packageDrv = builtins.unsafeDiscardStringContext variantPkg.drvPath;
    packageOutputs = builtins.listToAttrs (map (o: { name = o; value = toString variantPkg.${o}; }) variantPkg.outputs);
  } // lib.optionalAttrs provenanceRequired { captureProvenance = provenance; };
in

pkgs.runCommand "${variantLabel}-elf-folder"
  {
    nativeBuildInputs = import ./elfFolderBuildTools.nix { inherit pkgs; };
    datasetMetadata = builtins.toJSON folderMeta;
    passthru = {
      datasetMeta = folderMeta;
    };
  }
  (''
    mkdir -p $out/elf

    python3 ${collector} "$out/elf" ${lib.escapeShellArgs outPaths}
    ${lib.optionalString (auxiliaryOutputs != []) ''mkdir -p "$out/auxiliary"''}
    ${lib.concatMapStringsSep "\n" (o: ''ln -s ${lib.escapeShellArg (toString variantPkg.${o})} "$out/auxiliary/${o}"'') auxiliaryOutputs}

    cat > $out/meta.json <<'METAEOF'
    ${builtins.toJSON folderMeta}
    METAEOF
  '' + lib.optionalString provenanceRequired ''
      cat > "$out/provenance.json" <<'PROVENANCEEOF'
      ${builtins.toJSON provenance}
      PROVENANCEEOF
  '')

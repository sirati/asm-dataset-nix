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
  # Collect ELFs from EVERY output of the variant, not just the default
  # output. A package is ONE build (one .drv) split into multiple outputs;
  # multi-output packages put binaries in non-default outputs. e.g. lz4 has
  # outputs = [ dev lib man out ] with the DEFAULT output `dev` (headers,
  # no ELF) — the lz4 executable lives in `out`, liblz4.so in `lib`.
  # Searching only the default output silently drops them, and we cannot
  # special-case per package. Referencing every output also makes them all
  # runtime deps (the single build already produces them, so no extra cost).
  outPaths = map (o: "${variantPkg.${o}}") variantPkg.outputs;
in

pkgs.runCommand "${variantLabel}-elf-folder"
  {
    nativeBuildInputs = with pkgs; [
      file
      findutils
    ];
    passthru = {
      datasetMeta = meta;
    };
  }
  ''
    mkdir -p $out/elf

    for src in ${lib.concatStringsSep " " outPaths}; do
      find "$src" -type f -print0 | while IFS= read -r -d "" f; do
        if file -b "$f" | grep -q "^ELF"; then
          basename=$(basename "$f")
          if [ -e "$out/elf/$basename" ]; then
            hash=$(echo "$f" | md5sum | cut -c1-8)
            ln -s "$f" "$out/elf/''${hash}_''${basename}"
          else
            ln -s "$f" "$out/elf/$basename"
          fi
        fi
      done
    done

    count=$(find $out/elf -mindepth 1 -type l | wc -l)
    echo "Linked $count ELF files for ${variantLabel}"

    cat > $out/meta.json <<'METAEOF'
    ${builtins.toJSON meta}
    METAEOF
  ''

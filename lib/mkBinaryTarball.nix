# Extract only ELF executables and shared libraries from a built variant
# into a flat .tar.zst tarball.
#
# Input: { variantLabel, variantPkg, meta } (output of mkVariant.nix)
# Output: derivation producing $out/<variantLabel>.tar.zst + $out/meta.json

{ pkgs, lib }:

{
  variantLabel,
  variantPkg,
  meta,
}:

pkgs.runCommand "${variantLabel}-elf-tarball"
  {
    nativeBuildInputs = with pkgs; [
      file
      findutils
      gnutar
      zstd
    ];
    passthru = {
      datasetMeta = meta;
    };
  }
  ''
    mkdir -p $out tmp_elf

    # Find all ELF files in the built package
    find ${variantPkg} -type f -print0 | while IFS= read -r -d "" f; do
      if file -b "$f" | grep -q "^ELF"; then
        basename=$(basename "$f")
        if [ -e "tmp_elf/$basename" ]; then
          # Handle filename collision with hash prefix
          hash=$(echo "$f" | md5sum | cut -c1-8)
          cp "$f" "tmp_elf/''${hash}_''${basename}"
        else
          cp "$f" "tmp_elf/$basename"
        fi
      fi
    done

    count=$(find tmp_elf -type f | wc -l)
    echo "Found $count ELF files for ${variantLabel}"

    # Create flat tarball (empty is fine — some lib-only packages may have none)
    tar -cf $out/${variantLabel}.tar.zst \
      --zstd \
      -C tmp_elf \
      .

    # Metadata sidecar
    cat > $out/meta.json <<'METAEOF'
    ${builtins.toJSON meta}
    METAEOF
  ''

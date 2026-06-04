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
      gawk
    ];
    passthru = {
      datasetMeta = meta;
    };
  }
  ''
    mkdir -p $out/elf

    # Enumerate every ELF entry across ALL outputs — regular files AND
    # symlinks that resolve to an ELF (packages expose several named
    # binaries as symlinks to one executable, e.g. lz4cat, unlz4 -> lz4).
    # One row per entry: <real-target> <TAB> <is-symlink 0|1> <TAB> <name>.
    # `file -bL` types through the link; `readlink -f` canonicalises to the
    # real ELF.
    : > entries.tsv
    for src in ${lib.concatStringsSep " " outPaths}; do
      find "$src" \( -type f -o -type l \) -print0 | while IFS= read -r -d "" f; do
        if file -bL "$f" 2>/dev/null | grep -q "^ELF"; then
          real=$(readlink -f "$f")
          if [ -L "$f" ]; then sym=1; else sym=0; fi
          bn=$(basename "$f")
          printf '%s\t%s\t%s\n' "$real" "$sym" "$bn" >> entries.tsv
        fi
      done
    done

    # Keep only UNIQUE ELFs (symlinks to the same target are the SAME
    # binary). Per unique target choose ONE name: prefer a non-symlink
    # entry; otherwise the natural (version) order last — the highest
    # version (v10.0 after v9.0.1.2). Natural order already yields the
    # "longest" for a plain version chain without mis-ranking a deeper but
    # lower version. The sort puts the winner first per target group (k1);
    # awk keeps the first row per group.
    TAB="$(printf '\t')"
    if [ -s entries.tsv ]; then
      sort -t"$TAB" -k1,1 -k2,2n -k3,3Vr entries.tsv \
        | awk -F"$TAB" '!seen[$1]++ { print $1 "\t" $3 }' \
        | while IFS="$TAB" read -r real name; do
            if [ -e "$out/elf/$name" ] || [ -L "$out/elf/$name" ]; then
              h=$(printf '%s' "$real" | md5sum | cut -c1-8)
              ln -s "$real" "$out/elf/''${h}_''${name}"
            else
              ln -s "$real" "$out/elf/$name"
            fi
          done
    fi

    count=$(find $out/elf -mindepth 1 -type l | wc -l)
    echo "Linked $count unique ELF(s) for ${variantLabel}"

    cat > $out/meta.json <<'METAEOF'
    ${builtins.toJSON meta}
    METAEOF
  ''

{
  description = "Assembly/binary dataset: cross-compiled ELF corpus for decompiler analysis";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      developModule = import ./develop.nix;

      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f {
            inherit system;
            pkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
            };
          }
        );

      perSystem = forAllSystems (
        { system, pkgs }:
        let
          lib = pkgs.lib;
          matrix = import ./lib/matrix.nix { inherit pkgs lib; };
          develop = developModule { inherit pkgs; };

          # ── Nested dataset output ──────────────────────────────────────────
          # Access: .#dataset.<system>.<pkg>.<arch>.<compiler-opt-flags-hardening>
          # e.g.:   .#dataset.x86_64-linux.hello.aarch64.gcc15-O2-baseline-unhardened
          # Only evaluates the requested slice, not the full 75K matrix.
          datasetNested = lib.mapAttrs (
            pkgLabel: archAttrs:
            lib.mapAttrs (archLabel: variantAttrs: lib.mapAttrs (_: v: v.tarball) variantAttrs) archAttrs
          ) matrix.nestedMatrix;

          # ── Manifest generation app ────────────────────────────────────────
          # Usage:
          #   nix run .#generate-manifest                          # full meta (fast)
          #   nix run .#generate-manifest -- hello                 # one pkg meta
          #   nix run .#generate-manifest -- hello x86_64          # one pkg+arch meta
          #   nix run .#generate-manifest -- hello x86_64 drv      # with drvPaths (slow)
          generateManifestScript = pkgs.writeShellScript "generate-manifest" ''
            set -euo pipefail
            PKG="''${1:-}"
            ARCH="''${2:-}"
            MODE="''${3:-meta}"

            if [ "$MODE" = "drv" ]; then
              BASE=".#_drvPaths.${system}"
            else
              BASE=".#_meta.${system}"
            fi

            if [ -n "$PKG" ] && [ -n "$ARCH" ]; then
              OUT="asm-dataset-''${MODE}-''${PKG}-''${ARCH}.json"
              echo "Generating $MODE for $PKG/$ARCH..."
              EXPR="$BASE.$PKG.$ARCH"
            elif [ -n "$PKG" ]; then
              OUT="asm-dataset-''${MODE}-''${PKG}.json"
              echo "Generating $MODE for $PKG (all archs)..."
              EXPR="$BASE.$PKG"
            else
              OUT="asm-dataset-''${MODE}.json"
              echo "Generating full $MODE manifest..."
              EXPR="$BASE"
            fi

            ${pkgs.nix}/bin/nix eval --json "$EXPR" \
              --extra-experimental-features "nix-command flakes" \
              > "$OUT"

            echo "Written to $OUT"
          '';

        in
        {
          packages = { };

          apps = {
            generate-manifest = {
              type = "app";
              program = toString generateManifestScript;
            };
          };

          devShells = {
            default = develop.devShell;
          };

          # Nested dataset (the main output — derivations, lazy)
          dataset = datasetNested;

          # Pure metadata — no derivations, instant eval
          _meta = matrix.metaMatrix;

          # drvPaths — expensive to eval (forces derivation instantiation).
          # Use: nix eval .#_drvPaths.<sys>.<pkg>.<arch> --json
          _drvPaths = lib.mapAttrs (
            pkgLabel: archAttrs:
            lib.mapAttrs (
              archLabel: variantAttrs: lib.mapAttrs (suffix: v: v.tarball.drvPath) variantAttrs
            ) archAttrs
          ) matrix.nestedMatrix;

          # Debug outputs
          _debug = {
            compilers =
              let
                c = import ./lib/compilers.nix { inherit pkgs; };
              in
              {
                gcc = map (e: { inherit (e) label version; }) c.gcc;
                clang = map (e: { inherit (e) label version; }) c.clang;
              };
            targets = builtins.attrNames (import ./lib/architectures.nix { }).targets;
            matrixSize = matrix.matrixSize;
          };
        }
      );

    in
    {
      dataset = nixpkgs.lib.mapAttrs (_: s: s.dataset) perSystem;
      apps = nixpkgs.lib.mapAttrs (_: s: s.apps) perSystem;
      devShells = nixpkgs.lib.mapAttrs (_: s: s.devShells) perSystem;
      packages = nixpkgs.lib.mapAttrs (_: s: s.packages) perSystem;

      _meta = nixpkgs.lib.mapAttrs (_: s: s._meta) perSystem;
      _drvPaths = nixpkgs.lib.mapAttrs (_: s: s._drvPaths) perSystem;
      _debug = nixpkgs.lib.mapAttrs (_: s: s._debug) perSystem;
    };
}

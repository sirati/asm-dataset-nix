{
  description = "Assembly/binary dataset: cross-compiled ELF corpus for decompiler analysis";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    # Old nixpkgs releases for legacy compiler versions.
    # 15.09: GCC 4.4, 4.6
    # 18.03: GCC 4.5, 5; Clang 3.4-3.9, 4
    # 22.11: GCC 4.8-12, Clang 5-14
    # 23.11: Clang 15-16
    # 24.05: Clang 17
    nixpkgs-15_09.url = "github:NixOS/nixpkgs/9c31c72cafe536e0c21238b2d47a23bfe7d1b033";
    nixpkgs-15_09.flake = false;
    nixpkgs-18_03.url = "github:NixOS/nixpkgs/120b013e0c082d58a5712cde0a7371ae8b25a601";
    nixpkgs-18_03.flake = false;
    nixpkgs-22_11.url = "github:NixOS/nixpkgs/4d2b37a84fad1091b9de401eb450aae66f1a741e";
    nixpkgs-23_11.url = "github:NixOS/nixpkgs/057f9aecfb71c4437d2b27d3323df7f93c010b7e";
    nixpkgs-24_05.url = "github:NixOS/nixpkgs/63dacb46bf939521bdc93981b4cbb7ecb58427a0";
  };

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-15_09,
      nixpkgs-18_03,
      nixpkgs-22_11,
      nixpkgs-23_11,
      nixpkgs-24_05,
    }:
    let
      systems = [ "x86_64-linux" ];
      developModule = import ./develop.nix;

      # MIPS compiler-rt cross-compilation fixes (see lib/mips-clang-overlay.nix)
      compilerRtMipsOverlay = import ./lib/mips-clang-overlay.nix;

      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f {
            inherit system;
            # Clean pkgs — no overlays, hits binary cache. Use for devShells.
            cleanPkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
            };
            # Overlayed pkgs — dataset-specific patches. Use for builds.
            pkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [ compilerRtMipsOverlay ];
            };
          }
        );

      perSystem = forAllSystems (
        { system, pkgs, cleanPkgs }:
        let
          lib = pkgs.lib;

          # ── Old nixpkgs sets for legacy compilers ────────────────────────
          oldNixpkgsSets = import ./lib/old-nixpkgs.nix {
            inherit system;
            nixpkgsInputs = {
              inherit
                nixpkgs-15_09
                nixpkgs-18_03
                nixpkgs-22_11
                nixpkgs-23_11
                nixpkgs-24_05
                ;
            };
            mipsClangOverlay = compilerRtMipsOverlay;
          };

          oldCompilers = import ./lib/old-compilers.nix {
            inherit pkgs lib;
            inherit oldNixpkgsSets;
          };

          matrix = import ./lib/matrix.nix {
            inherit pkgs lib;
            extraCompilers = oldCompilers;
          };
          develop = developModule { pkgs = cleanPkgs; };

          # ── Nested dataset output ──────────────────────────────────────────
          # Access: .#dataset.<system>.<pkg>.<arch>.<compiler-opt-flags-hardening>
          # e.g.:   .#dataset.x86_64-linux.hello.aarch64.gcc15-O2-baseline-unhardened
          # Only evaluates the requested slice, not the full matrix.
          datasetNested = lib.mapAttrs (
            pkgLabel: archAttrs:
            lib.mapAttrs (archLabel: variantAttrs: lib.mapAttrs (_: v: v.tarball) variantAttrs) archAttrs
          ) matrix.nestedMatrix;

          # ── Manifest generation app ────────────────────────────────────────
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
                currentC = import ./lib/compilers.nix { inherit pkgs; };
                allGcc = map (e: { inherit (e) label version; }) (oldCompilers.gcc ++ currentC.gcc);
                allClang = map (e: { inherit (e) label version; }) (oldCompilers.clang ++ currentC.clang);
              in
              {
                gcc = allGcc;
                clang = allClang;
                total = builtins.length allGcc + builtins.length allClang;
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

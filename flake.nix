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

      # Overlay to fix compiler-rt on 32-bit MIPS (O32 ABI).
      # The O32 ABI struct stat is 144 bytes, but compiler-rt (through
      # LLVM 22) hardcodes 160 (the N32 value). This was reported as
      # LLVM D129749 and partially fixed in LLVM 15, then regressed.
      # We fix it universally via postPatch sed on the .h file.
      compilerRtMipsOverlay =
        final: prev:
        let
          patchCompilerRt =
            rt:
            rt.overrideAttrs (old: {
              postPatch = (old.postPatch or "") + ''
                for f in lib/sanitizer_common/sanitizer_platform_limits_posix.h \
                         lib/sanitizer_common/sanitizer_platform_limits_posix.cc; do
                  [ -f "$f" ] || continue
                  # LLVM 5-14, 20-22: FIRST_32_SECOND_64(160, 216) in __mips__ #else branch
                  sed -i 's/FIRST_32_SECOND_64(160, 216)/FIRST_32_SECOND_64(144, 216)/g' "$f"
                  # LLVM 16-19: inline ternary (_MIPS_SIM == _ABIN32) ? 176 : 160
                  sed -i 's/(_MIPS_SIM == _ABIN32) ? 176 : 160/(_MIPS_SIM == _ABIN32) ? 176 : 144/g' "$f"
                done
              '';
            });
          patchLlvmSet =
            llvmPkgs:
            llvmPkgs
            // {
              compiler-rt = patchCompilerRt llvmPkgs.compiler-rt;
            };
          llvmAttrNames = builtins.filter (n: builtins.match "llvmPackages_[0-9]+" n != null) (
            builtins.attrNames prev
          );
          llvmOverrides = builtins.listToAttrs (
            map (name: {
              inherit name;
              value = patchLlvmSet prev.${name};
            }) llvmAttrNames
          );
        in
        llvmOverrides // { llvmPackages = patchLlvmSet prev.llvmPackages; };

      forAllSystems =
        f:
        nixpkgs.lib.genAttrs systems (
          system:
          f {
            inherit system;
            pkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [ compilerRtMipsOverlay ];
            };
          }
        );

      perSystem = forAllSystems (
        { system, pkgs }:
        let
          lib = pkgs.lib;

          # ── Old nixpkgs sets for legacy compilers ────────────────────────
          oldPkgs_15_09 = import nixpkgs-15_09 {
            inherit system;
            config.allowUnfree = true;
          };
          oldPkgs_18_03 = import nixpkgs-18_03 {
            inherit system;
            config.allowUnfree = true;
          };
          oldPkgs_22_11 = import nixpkgs-22_11 {
            inherit system;
            config.allowUnfree = true;
            overlays = [ compilerRtMipsOverlay ];
          };
          oldPkgs_23_11 = import nixpkgs-23_11 {
            inherit system;
            config.allowUnfree = true;
            overlays = [ compilerRtMipsOverlay ];
          };
          oldPkgs_24_05 = import nixpkgs-24_05 {
            inherit system;
            config.allowUnfree = true;
            overlays = [ compilerRtMipsOverlay ];
          };

          oldCompilers = import ./lib/old-compilers.nix {
            inherit pkgs lib;
            oldNixpkgsSets = [
              {
                oldPkgs = oldPkgs_15_09;
                nixpkgsSrc = nixpkgs-15_09;
                inherit system;
                gccSpecs = [
                  {
                    attr = "gcc44";
                    label = "4_4";
                  }
                  {
                    attr = "gcc46";
                    label = "4_6";
                  }
                ];
                clangSpecs = [
                  {
                    attr = "llvmPackages_36";
                    label = "3_6";
                  }
                ];
              }
              {
                oldPkgs = oldPkgs_18_03;
                nixpkgsSrc = nixpkgs-18_03;
                inherit system;
                gccSpecs = [
                  {
                    attr = "gcc45";
                    label = "4_5";
                  }
                  {
                    attr = "gcc5";
                    label = "5";
                  }
                ];
                clangSpecs = [
                  {
                    attr = "llvmPackages_34";
                    label = "3_4";
                  }
                  {
                    attr = "llvmPackages_35";
                    label = "3_5";
                  }
                  {
                    attr = "llvmPackages_37";
                    label = "3_7";
                  }
                  {
                    attr = "llvmPackages_38";
                    label = "3_8";
                  }
                  {
                    attr = "llvmPackages_39";
                    label = "3_9";
                  }
                  {
                    attr = "llvmPackages_4";
                    label = "4";
                  }
                ];
              }
              {
                oldPkgs = oldPkgs_22_11;
                gccSpecs = [
                  {
                    attr = "gcc48";
                    label = "4_8";
                  }
                  {
                    attr = "gcc49";
                    label = "4_9";
                  }
                  {
                    attr = "gcc6";
                    label = "6";
                  }
                  {
                    attr = "gcc7";
                    label = "7";
                  }
                  {
                    attr = "gcc8";
                    label = "8";
                  }
                  {
                    attr = "gcc9";
                    label = "9";
                  }
                  {
                    attr = "gcc10";
                    label = "10";
                  }
                  {
                    attr = "gcc11";
                    label = "11";
                  }
                  {
                    attr = "gcc12";
                    label = "12";
                  }
                ];
                clangSpecs = [
                  {
                    attr = "llvmPackages_5";
                    label = "5";
                  }
                  {
                    attr = "llvmPackages_6";
                    label = "6";
                  }
                  {
                    attr = "llvmPackages_7";
                    label = "7";
                  }
                  {
                    attr = "llvmPackages_8";
                    label = "8";
                  }
                  {
                    attr = "llvmPackages_9";
                    label = "9";
                  }
                  {
                    attr = "llvmPackages_10";
                    label = "10";
                  }
                  {
                    attr = "llvmPackages_11";
                    label = "11";
                  }
                  {
                    attr = "llvmPackages_12";
                    label = "12";
                  }
                  {
                    attr = "llvmPackages_13";
                    label = "13";
                  }
                  {
                    attr = "llvmPackages_14";
                    label = "14";
                  }
                ];
              }
              {
                oldPkgs = oldPkgs_23_11;
                clangSpecs = [
                  {
                    attr = "llvmPackages_15";
                    label = "15";
                  }
                  {
                    attr = "llvmPackages_16";
                    label = "16";
                  }
                ];
                gccSpecs = [ ];
              }
              {
                oldPkgs = oldPkgs_24_05;
                clangSpecs = [
                  {
                    attr = "llvmPackages_17";
                    label = "17";
                  }
                ];
                gccSpecs = [ ];
              }
            ];
          };

          matrix = import ./lib/matrix.nix {
            inherit pkgs lib;
            extraCompilers = oldCompilers;
          };
          develop = developModule { inherit pkgs; };

          # ── Nested dataset output ──────────────────────────────────────────
          # Access: .#dataset.<system>.<pkg>.<arch>.<compiler-opt-flags-hardening>
          # e.g.:   .#dataset.x86_64-linux.hello.aarch64.gcc15-O2-baseline-unhardened
          # Only evaluates the requested slice, not the full matrix.
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

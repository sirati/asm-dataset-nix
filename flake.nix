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

    # External runner: provides `python3Packages.dynamic-runner` via its
    # overlay (replaces the previous in-tree `dynamic-batch-rs` path-flake).
    # Tracks default branch; bump via `nix flake update dynamic-runner`.
    dynamic-runner.url = "github:sirati/dynamic-runner";
    # Generic semantic-layering helpers + extract-layer-assignment tool
    # (replaces the in-tree `nix/semantic-layering.nix` import).
    nix-docker-layered-image.url = "github:sirati/nix-docker-layered-image/v0.1.0";
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
      dynamic-runner,
      nix-docker-layered-image,
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
            # Image-build pkgs — only the overlays the runner / ssh-debug
            # images actually need (`dynamic-runner` python pkg, semantic-
            # layering helpers). Crucially does NOT include the mips-clang
            # compiler-rt overlay: that overlay perturbs every llvmPackages_N
            # set, which cascade-shifts the python313Packages fixed point
            # through clang-using build deps, knocking scikit-image / bokeh
            # / xarray etc. off the cache.nixos.org binary cache and forcing
            # ~hours of source rebuilds. The mips fix only matters for
            # MIPS cross-compile derivations, which the runner images don't
            # produce — so we keep it out of this set.
            runnerPkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [
                dynamic-runner.overlays.default
                nix-docker-layered-image.overlays.default
              ];
            };
            # Cross-compile pkgs — full overlay set including mips-clang
            # compiler-rt patches. Used for the matrix / crossToolchains.
            pkgs = import nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [
                compilerRtMipsOverlay
                dynamic-runner.overlays.default
                nix-docker-layered-image.overlays.default
              ];
            };
          }
        );

      perSystem = forAllSystems (
        { system, pkgs, cleanPkgs, runnerPkgs }:
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
          # Dev shell stays fully on `cleanPkgs` so the binary cache
          # serves mcp-nixos / language-server / transitive python
          # packages unchanged. `dynamic-runner` is added to the dev
          # Python env by `callPackage`-ing its `nix/wheel.nix`
          # directly against `cleanPkgs.python313Packages`, bypassing
          # the overlay's `pythonPackagesExtensions` hook (which would
          # cascade-invalidate the entire python set's fixed point).
          # We drive SuitTask off the framework directly during local
          # development rather than `pip install -e .` against
          # pyproject.toml.
          develop = developModule {
            pkgs = cleanPkgs;
            dynamicRunnerSrc = dynamic-runner;
          };

          # ── Nested dataset output ──────────────────────────────────────────
          # Access: .#dataset.<system>.<pkg>.<arch>.<compiler-opt-flags-hardening>
          # e.g.:   .#dataset.x86_64-linux.hello.aarch64.gcc15-O2-baseline-unhardened
          # Only evaluates the requested slice, not the full matrix.
          datasetNested = lib.mapAttrs (
            pkgLabel: archAttrs:
            lib.mapAttrs (archLabel: variantAttrs: lib.mapAttrs (_: v: v.elfFolder) variantAttrs) archAttrs
          ) matrix.nestedMatrix;

          # ── Manifest generation app ────────────────────────────────────────
          # ── Docker image (compiler_suit_runner) ───────────────────────────
          # Layered podman image used by the SLURM runner (see
          # plans/splendid-snacking-cascade.md §B2.5). Lazily evaluated;
          # depends on harmonia which is available in the pinned nixpkgs
          # but the call is wrapped to fail at attribute access time only,
          # not at flake-eval time, so other outputs are unaffected by any
          # future packaging gap.
          # Both runner images are built against `runnerPkgs` (no
          # mips-clang-overlay) so their python313 closure stays on the
          # binary cache; see the comment in `forAllSystems` above.
          dockerImage = import ./nix/docker-image.nix {
            pkgs = runnerPkgs;
            lib = runnerPkgs.lib;
            runnerSrc = ./python;
            harmonia = runnerPkgs.harmonia or null;
            # Bake the same dev-debug pubkey we use for the
            # ssh-debug task so `--enable-ssh-debug` on the runner
            # accepts the same key. The corresponding private key
            # lives at .ssh-debug/id_ed25519 (gitignored).
            pubkeyFile = ./.ssh-debug/id_ed25519.pub;
          };

          # ── ssh-debug image (interactive debugging) ───────────────────────
          # Parallel image used by the `ssh_debug_runner` task: spawns
          # podman containers (via SLURM) running sshd on a high port so a
          # developer can ssh in for live debugging through the gateway.
          # See python/ssh_debug_runner/ for the dispatcher.
          sshDebugImage = import ./nix/ssh-debug-image.nix {
            pkgs = runnerPkgs;
            lib = runnerPkgs.lib;
            runnerSrc = ./python;
            pubkeyFile = ./.ssh-debug/id_ed25519.pub;
          };

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
          packages = {
            inherit dockerImage sshDebugImage;
          };

          # Expose dockerImage at top-level too so `.#dockerImage` works
          # without `.packages.x86_64-linux.dockerImage`. Mirrors the
          # asm-tokenizer convention.
          inherit dockerImage sshDebugImage;

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
              archLabel: variantAttrs: lib.mapAttrs (suffix: v: v.elfFolder.drvPath) variantAttrs
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

          # ── Cross-toolchains (pre-build for cache population) ─────────────
          # Build before the package phase so SLURM workers can pull cross
          # compilers from a shared cache instead of rebuilding them.
          #   nix build .#crossToolchains.<sys>.<arch>            — all compilers for one arch
          #   nix build .#_crossToolchainMap.<sys>.<arch>.<comp>  — single toolchain
          # Some (arch, compiler) combos fail evaluation due to gaps in old
          # nixpkgs cross infrastructure (e.g. gcc5 + ppc32: nixpkgs-18.03
          # lacks platform.kernelArch for the triple). Those errors raise
          # from derivationStrict and cannot be caught by tryEval, so an
          # aggregate "all" output would propagate them and is intentionally
          # omitted. The job-feeder is expected to iterate
          # _crossToolchainsMeta and tolerate per-combo failures.
          crossToolchains = matrix.crossToolchains;
          _crossToolchainMap = matrix.crossToolchainMap;
          _crossToolchainsMeta = matrix.crossToolchainsMeta;
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

      crossToolchains = nixpkgs.lib.mapAttrs (_: s: s.crossToolchains) perSystem;
      _crossToolchainMap = nixpkgs.lib.mapAttrs (_: s: s._crossToolchainMap) perSystem;
      _crossToolchainsMeta = nixpkgs.lib.mapAttrs (_: s: s._crossToolchainsMeta) perSystem;

      # Layered podman image for the compiler_suit_runner. Lives both
      # under packages.<sys>.dockerImage (so `nix build .#dockerImage`
      # works on the default system) and as a per-system attribute.
      dockerImage = nixpkgs.lib.mapAttrs (_: s: s.dockerImage) perSystem;

      # Layered podman image for the ssh_debug_runner — interactive
      # SSH-able debug containers. Same exposure pattern as dockerImage.
      sshDebugImage = nixpkgs.lib.mapAttrs (_: s: s.sshDebugImage) perSystem;
    };
}

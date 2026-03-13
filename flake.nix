{
  description = "asm-dataset development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];

      developModule = import ./develop.nix;

      # ── High-level derivations (system-independent pure values) ─────────────
      # Loaded via pkgs/default.nix which auto-discovers every subdirectory
      # containing a high-level.nix and wires inter-package dependencies using a
      # fixed-point (lib.fix-style) so definition order does not matter.
      # pytorchScope = import ./pkgs;

      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f {
        inherit system;
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
      });

      # ── Per-system outputs ───────────────────────────────────────────────────
      perSystem = forAllSystems ({ system, pkgs }:
        let
          # ── Test environments (packages, devShells, apps) ───────────────────
          # tests = import ./test.nix { inherit pkgs pytorchScope; };

          # ── Dev shell for this project (Nix tooling) ────────────────────────
          develop = developModule { inherit pkgs; };

          # ── gen-hashes apps — one per HLD that declares generateHashesScript ─
          # Exposed under apps.<system>.default.<pkgName>.gen-hashes so that:
          #   nix run .#default.flash-attn.gen-hashes -- --tag v2.8.1
          # apps.default is a plain namespace attrset; gen-hashes carries type/program.
          # genHashesLib = import ./generate-hashes/lib.nix { inherit pkgs; };
          # genHashesSubApps =
          #   builtins.listToAttrs (
          #     builtins.concatLists (
          #       map (name:
          #         let hld = pytorchScope.${name};
          #         in
          #         if hld.generateHashesScript != null
          #         then [{ inherit name; value = { gen-hashes = genHashesLib.makeGenHashesApp hld; }; }]
          #         else []
          #       ) (builtins.attrNames pytorchScope)
          #     )
          #   );

        in
        {
          # packages  = tests.packages;

          devShells = {
            # Override the default devShell with the project development shell
            # (linters, LSPs, etc.) rather than a test environment.
            default = develop.devShell;
          };

          # apps = tests.apps // {
          #   default = genHashesSubApps;
          # };
        }
      );

    in
    {
      # ── High-level derivations + concretise function ─────────────────────────
      #
      # System-independent output.  Consumers import these into their own flakes:
      #
      #   inputs.torch-flake.url = "github:…/nix-torch-bin-pascal";
      #
      #   result = torch-flake.pytorch-packages.concretise {
      #     inherit pkgs;
      #     mlPackages              = with torch-flake.pytorch-packages; [ torch flash-attn ];
      #     python                  = "3.13";
      #     cuda                    = "12.8";
      #     torch                   = "2.10";
      #     pascal                  = false;
      #     allowBuildingFromSource = true;
      #   };
      #
      # See also example.nix for a complete downstream flake template.
      # pytorch-packages = pytorchScope // {
      #   concretise = import ./concretise;
      # };

      # ── Concrete test packages ───────────────────────────────────────────────
      # packages  = nixpkgs.lib.mapAttrs (_: s: s.packages)  perSystem;

      # ── Dev shells ───────────────────────────────────────────────────────────
      devShells = nixpkgs.lib.mapAttrs (_: s: s.devShells) perSystem;

      # ── Test apps ────────────────────────────────────────────────────────────
      # Every test environment is wired up as a runnable app so that
      #   nix run .#<name>
      # drops straight into the functional test suite.
      # apps = nixpkgs.lib.mapAttrs (_: s: s.apps) perSystem;
    };
}

/*
  asm-dataset-nix runner: layered podman image.

  Implements B2.5 of the splendid-snacking-cascade plan.

  This image is built once per submission and shipped to SLURM nodes
  via dynamic_batch's layered_transfer pipeline (per-blob upload
  dedup, see asm-tokenizer/dynamic_batch/packaging/layered_transfer.py).
  Inside each container the runner orchestrates harmonia (peer
  binary cache), an optional cachix uploader, and per-variant
  `nix build` invocations.

  Layers are assigned via `nix/semantic-layering.nix` so a small
  change to a single unit (e.g. project source) only invalidates
  that unit's layer instead of reshuffling popularity-contest
  buckets across the whole image.

  ─── harmonia ────────────────────────────────────────────────────
  harmonia is the peer binary-cache server. The current pinned
  nixpkgs ships it (`pkgs.harmonia`). Should a future bump remove
  the attribute, callers can pass `harmonia = null` and supply
  their own derivation via `contents`. The image still builds in
  that case; production deployments without harmonia will lack
  the peer cache mechanic and effectively fall back to whatever
  global substituters the daemon is configured with.
*/
{
  pkgs,
  lib,
  semantic-layering,
  name ? "asm-dataset-nix-runner",
  tag ? "latest",
  runnerSrc,
  contents ? [ ],
  pythonPackages ? (_: [ ]),
  harmonia ? pkgs.harmonia or null,
  includeCachix ? true,
}:

let
  semanticLayering = semantic-layering { inherit lib; };

  # Python with pytest + caller-supplied extras. The runner itself
  # is pure-stdlib at present (B2.x), but cluster smoke-tests run
  # `pytest` inside the container so we always include it.
  pythonEnv = pkgs.python313.withPackages (
    py: [ py.pytest ] ++ (pythonPackages py)
  );

  # Project source materialised under /app/python/compiler_suit_runner
  # so that `PYTHONPATH=/app/python` + `python -m compiler_suit_runner`
  # finds the package without needing it to be installed into the
  # python wrapper. This mirrors the asm-tokenizer projectFiles
  # pattern but scoped to the runner package only.
  projectFiles = pkgs.runCommand "asm-dataset-nix-runner-source" { } ''
    mkdir -p $out/app/python/compiler_suit_runner
    cp -r ${runnerSrc}/compiler_suit_runner/. $out/app/python/compiler_suit_runner/
    chmod -R +w $out/app
  '';

  # Optional cachix uploader. Wrapped in `or null` so a stripped
  # nixpkgs (or `--option allowed-substituters` pinning) still
  # builds the image; the runner detects absence at runtime and
  # logs a warning instead of crashing.
  cachixPkg =
    if includeCachix then (pkgs.cachix or null) else null;

  # Always-in-image foundation packages. Order does NOT determine
  # layer assignment (semantic-layering does); this is the
  # `contents` arg for buildLayeredImage.
  basePkgs = [
    pkgs.nix
    pkgs.gitMinimal
    pkgs.cacert
    pkgs.coreutils
    pkgs.bash
  ];

  imageContents =
    basePkgs
    ++ [
      pythonEnv
      projectFiles
    ]
    ++ lib.optional (harmonia != null) harmonia
    ++ lib.optional (cachixPkg != null) cachixPkg
    ++ contents;

  # Previous-build layer assignment for partial-build cache
  # stability. Reads NIX_DOCKER_LAYER_CACHE on `--impure` builds;
  # null on regular builds (full popularity-contest fallback for
  # the basics tier).
  previousAssignment =
    semanticLayering.readAssignmentFromEnv "NIX_DOCKER_LAYER_CACHE";

  # ── Semantic layer plan ──────────────────────────────────────
  # Order matters: foundational units FIRST. Each unit's
  # subcomponent_out claims its closure from the remaining graph,
  # so later units don't redundantly include shared paths.
  #
  # Layout (bottom-up in the docker manifest):
  #   base-python (1 layer)   - python313 + interpreter closure
  #   nix-tooling (1 layer)   - pkgs.nix + cacert + git
  #   harmonia    (1 layer)   - peer cache server + its closure
  #                             (only present if harmonia != null)
  #   cachix      (1 layer)   - federation uploader + closure
  #                             (only present if includeCachix)
  #   project-code (2 layers) - runner source alone, then deps
  #                             (deps are typically empty since
  #                              python313 is already peeled)
  #   basics      (1 layer)   - bash, coreutils, libc remnants
  units =
    [
      {
        name = "base-python";
        roots = [ pythonEnv ];
      }
      {
        name = "nix-tooling";
        roots = [
          pkgs.nix
          pkgs.cacert
          pkgs.gitMinimal
        ];
      }
    ]
    ++ lib.optional (harmonia != null) {
      name = "harmonia";
      roots = [ harmonia ];
    }
    ++ lib.optional (cachixPkg != null) {
      name = "cachix";
      roots = [ cachixPkg ];
    }
    ++ [
      {
        name = "project-code";
        roots = [ projectFiles ];
        isolate = true;
      }
    ];

  layeringPipeline = semanticLayering.buildPipeline {
    inherit units previousAssignment;
    maxLayers = 120;
  };

in
pkgs.dockerTools.buildLayeredImage {
  inherit name tag;

  contents = imageContents;

  layeringPipeline = pkgs.writeText "${name}-pipeline.json" (
    builtins.toJSON layeringPipeline
  );

  config = {
    Env = [
      "LANG=C.UTF-8"
      "PYTHONPATH=/app/python"
      "PATH=/usr/local/bin:/usr/bin:/bin:/run/current-system/sw/bin"
    ];

    # The runner __main__ entry point is added in B4.1. Until then
    # `--help` is a smoke-default that doesn't require argparse to
    # know about the full CLI; this lets the image build and be
    # transferred independently of the CLI work.
    Cmd = [
      "python"
      "-m"
      "compiler_suit_runner"
      "--help"
    ];

    WorkingDir = "/app";
  };
}

/*
  asm-dataset-nix runner: layered podman image.

  Implements B2.5 of the splendid-snacking-cascade plan.

  This image is built once per submission and shipped to SLURM nodes
  via dynamic_runner's layered_transfer pipeline (per-blob upload
  dedup, see dynamic-runner/python/dynamic_runner/packaging/layered_transfer.py).
  Inside each container the runner orchestrates harmonia (peer
  binary cache), an optional cachix uploader, and per-variant
  `nix build` invocations.

  Layers are assigned via the external `nix-docker-layered-image`
  flake (overlay-injected as `pkgs.lib.semanticLayering`) so a small
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
  name ? "asm-dataset-nix-runner",
  tag ? "latest",
  runnerSrc,
  contents ? [ ],
  pythonPackages ? (_: [ ]),
  harmonia ? pkgs.harmonia or null,
  includeCachix ? true,
  # Optional pubkey baked into /root/.ssh/authorized_keys so the
  # opt-in `--enable-ssh-debug` flow can exec sshd with a usable
  # auth file. If null, the SSH bits still get installed (openssh
  # binary, host-key staging, sshd_config generation at boot) but
  # no key is authorized — sshd would refuse all logins until the
  # operator drops a key in via `podman exec`.
  pubkeyFile ? null,
}:

let
  # Provided by `nix-docker-layered-image`'s overlay applied in flake.nix.
  semanticLayering = pkgs.lib.semanticLayering;

  # Python env: pytest for in-container smoke tests, dynamic-runner for the
  # SLURM bridge (provided by the dynamic-runner flake's overlay), plus any
  # caller-supplied extras.
  #
  # No psutil dep — dynamic-runner aa8ab87+ dissolved that structurally
  # (the Python side was reading two ints just to hand them to Rust;
  # now Rust does std::thread::available_parallelism + /proc/meminfo
  # directly, which is also cgroup-aware vs psutil's host-physical view).
  pythonEnv = pkgs.python313.withPackages (
    py: [ py.pytest py.dynamic-runner ] ++ (pythonPackages py)
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

  # Bake sshd host keys at image-build time. Fingerprint reuse across
  # ephemeral containers is acceptable for debugging (clients pass
  # `-o StrictHostKeyChecking=no`). The runtime stages these into
  # /tmp/ssh-debug/ with chmod 600, since nix-store mode is 0444 and
  # sshd refuses to read 0444 host keys.
  hostKeys = pkgs.runCommand "asm-dataset-nix-runner-host-keys" { } ''
    mkdir -p $out/etc/ssh
    ${pkgs.openssh}/bin/ssh-keygen -A -f $out
    chmod 600 $out/etc/ssh/ssh_host_*_key
    chmod 644 $out/etc/ssh/ssh_host_*_key.pub
  '';

  # Optional /root/.ssh/authorized_keys with the dev's pubkey. Wired
  # in only when the caller passes `pubkeyFile`. The compiler_suit_runner's
  # ssh_debug module fails open if no key is authorized — sshd binds
  # the port but refuses every connection.
  rootAuthorizedKeys =
    if pubkeyFile != null then
      pkgs.runCommand "asm-dataset-nix-runner-authorized-keys" { } ''
        mkdir -p $out/root/.ssh
        cp ${pubkeyFile} $out/root/.ssh/authorized_keys
        chmod 700 $out/root/.ssh
        chmod 600 $out/root/.ssh/authorized_keys
      ''
    else null;

  # /etc/passwd, /etc/group, /etc/shadow, /etc/nsswitch.conf —
  # required by harmonia-cache 3.x (which talks to nix-daemon as
  # root) and by anything that does name lookups (sshd, sudo, ...).
  # `dockerTools.buildLayeredImage` does NOT bake an NSS DB by
  # default; without these podman/runtime fail with "no matching
  # entries in passwd file" when User="root" or when sshd's
  # locked-account check fires on `!` in shadow.
  nssFiles = pkgs.runCommand "asm-dataset-nix-runner-nss" { } ''
    mkdir -p $out/etc
    cat > $out/etc/passwd <<EOF
    root:x:0:0:root:/root:${pkgs.bash}/bin/bash
    sshd:x:74:74:Privilege-separated SSH:/var/empty:/run/current-system/sw/bin/nologin
    nobody:x:65534:65534:Nobody:/var/empty:/run/current-system/sw/bin/nologin
    EOF
    sed -i 's/^    //' $out/etc/passwd
    cat > $out/etc/group <<EOF
    root:x:0:
    sshd:x:74:
    nogroup:x:65534:
    EOF
    sed -i 's/^    //' $out/etc/group
    # Empty password field for root: sshd's locked-account check
    # treats `!` / `*` prefixes as locked and refuses login. Empty
    # = "no password required"; password auth is blocked elsewhere.
    cat > $out/etc/shadow <<EOF
    root::1::::::
    sshd:!:1::::::
    nobody:!:1::::::
    EOF
    sed -i 's/^    //' $out/etc/shadow
    chmod 644 $out/etc/passwd $out/etc/group
    chmod 600 $out/etc/shadow
    cat > $out/etc/nsswitch.conf <<EOF
    passwd: files
    group: files
    shadow: files
    hosts: files dns
    networks: files dns
    services: files
    protocols: files
    rpc: files
    EOF
    sed -i 's/^    //' $out/etc/nsswitch.conf
    chmod 644 $out/etc/nsswitch.conf
  '';

  # Default /etc/nix/nix.conf. Single-user mode (build-users-group
  # empty) because podman containers don't have nixbld* users;
  # sandbox=false because the user namespace can't nest mount/cgroup
  # ops sandboxed builds need. The `!include /etc/nix/peer.conf`
  # is a SOFT include — silently skipped if the file's missing —
  # populated at runtime by ``compiler_suit_runner.peer_cache``'s
  # PeerNixConfWatcher with the live federation peer set.
  nixConfFile = pkgs.writeText "nix.conf" ''
    experimental-features = nix-command flakes
    sandbox = false
    build-users-group =
    # max-jobs: how many derivations the daemon will build in parallel.
    # Defaults to 1 — without this, every concurrent ``nix build`` from
    # the worker pool queues serially at the daemon, wasting the worker
    # concurrency the framework set up. ``auto`` = match the container's
    # CPU count (cgroup-aware in modern nix). cores = 0 keeps each
    # individual build using all visible cores for its own ``make -jN``,
    # which interacts fine with max-jobs > 1 because builds with heavy
    # parallelism alternate I/O and compute phases.
    max-jobs = auto
    cores = 0
    substituters = https://cache.nixos.org
    trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=
    extra-trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=

    !include /etc/nix/peer.conf
  '';

  nixConfDir = pkgs.runCommand "asm-dataset-nix-runner-nix-conf" { } ''
    mkdir -p $out/etc/nix
    cp ${nixConfFile} $out/etc/nix/nix.conf
    chmod 644 $out/etc/nix/nix.conf
  '';

  # Always-in-image foundation packages. Order does NOT determine
  # layer assignment (semantic-layering does); this is the
  # `contents` arg for buildLayeredImage.
  basePkgs = [
    pkgs.nix
    pkgs.gitMinimal
    pkgs.cacert
    pkgs.coreutils
    pkgs.bash
    pkgs.gnused
    pkgs.gawk
    pkgs.gnugrep
    pkgs.findutils
    # openssh: required by the opt-in `--enable-ssh-debug` flow.
    # Always shipped (cost is small, ~5MB) so the operator can flip
    # the feature on per-dispatch without rebuilding the image.
    # See compiler_suit_runner.ssh_debug for the runtime side.
    pkgs.openssh
  ];

  imageContents =
    basePkgs
    ++ [
      pythonEnv
      projectFiles
      nssFiles
      nixConfDir
      hostKeys
    ]
    ++ lib.optional (rootAuthorizedKeys != null) rootAuthorizedKeys
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
      {
        name = "nss-and-nix-conf";
        roots = [ nssFiles nixConfDir ];
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
      # Force every ``nix`` client to talk to nix-daemon over the unix
      # socket instead of opening /nix/var/nix/db/db.sqlite directly.
      # In single-user mode (build-users-group =) running as root, nix
      # otherwise defaults to direct-mode access; concurrent ``nix
      # build`` invocations from the worker pool then race on the
      # SQLite write lock and surface as
      # ``error: SQLite database '/nix/var/nix/db/db.sqlite' is busy``.
      # Daemon mode serializes all DB writes through the daemon's
      # single connection, eliminating the contention.
      "NIX_REMOTE=daemon"
      # cacert is in basePkgs but its bundle path needs to be exposed
      # explicitly for nix / curl / openssl to find it. Without these,
      # `nix build` over HTTPS fails to verify substituter TLS certs.
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "CURL_CA_BUNDLE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];

    # Entrypoint is fixed `python -m`; Cmd is the default secondary
    # module to invoke. The dynamic_runner SLURM wrapper invokes
    # secondaries as `podman run image:tag {secondary_module} --secondary ...`,
    # which APPENDS to Entrypoint and REPLACES Cmd, yielding
    # `python -m compiler_suit_runner --secondary ...`.
    Entrypoint = [
      "python"
      "-m"
    ];
    Cmd = [
      "compiler_suit_runner"
      "--help"
    ];

    WorkingDir = "/app";
  };
}

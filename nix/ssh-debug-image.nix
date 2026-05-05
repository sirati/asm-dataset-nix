/*
  asm-dataset-nix ssh-debug image: SUPERSET of nix/docker-image.nix
  with OpenSSH layered on top, so a developer can ssh in to a running
  SLURM-spawned container and reproduce / inspect / debug the actual
  workload — including running `nix build`, hitting harmonia for
  store sharing, exec'ing the compiler_suit_runner code paths, etc.

  Compared to docker-image.nix this image adds:
   - openssh (sshd + ssh-keygen)
   - bakedHostKeys: ssh-keygen -A run at build time
   - authorizedKeys: <repo>/.ssh-debug/id_ed25519.pub baked at /root/.ssh
   - sshd_config: high port (default 22222), root login by key only
   - The `ssh_debug_runner` python package (in addition to
     compiler_suit_runner) so the framework can dispatch sshd workers

  Cmd defaults to `ssh_debug_runner --help` so a non-secondary
  invocation is a smoke test; the SLURM secondary spawn appends
  `<secondary_module> --secondary ...` to the Entrypoint=["python","-m"]
  yielding `python -m ssh_debug_runner --secondary ...` (which fires up
  sshd, see python/ssh_debug_runner/worker.py).
*/
{
  pkgs,
  lib,
  name ? "asm-dataset-nix-ssh-debug",
  tag ? "latest",
  runnerSrc,
  pubkeyFile,
  sshdPort ? 22222,
  contents ? [ ],
  pythonPackages ? (_: [ ]),
  harmonia ? pkgs.harmonia or null,
  includeCachix ? true,
}:

let
  semanticLayering = pkgs.lib.semanticLayering;

  # Same python env as docker-image.nix: pytest for in-container smoke
  # tests, dynamic-runner for the SLURM bridge, plus extras. (psutil
  # was needed pre-aa8ab87; dissolved structurally upstream — Rust now
  # does available_parallelism + /proc/meminfo directly.)
  pythonEnv = pkgs.python313.withPackages (
    py: [ py.pytest py.dynamic-runner ] ++ (pythonPackages py)
  );

  # Project source: BOTH compiler_suit_runner (so ssh sessions can
  # exercise the real workload) AND ssh_debug_runner (so the SLURM
  # secondary entry can spawn sshd via the dynamic_runner protocol).
  projectFiles = pkgs.runCommand "asm-dataset-nix-ssh-debug-source" { } ''
    mkdir -p $out/app/python/compiler_suit_runner
    cp -r ${runnerSrc}/compiler_suit_runner/. $out/app/python/compiler_suit_runner/
    mkdir -p $out/app/python/ssh_debug_runner
    cp -r ${runnerSrc}/ssh_debug_runner/. $out/app/python/ssh_debug_runner/
    chmod -R +w $out/app
  '';

  # Bake sshd host keys at image-build time. Fingerprint reuse across
  # ephemeral containers is acceptable for debugging.
  hostKeys = pkgs.runCommand "ssh-debug-host-keys" { } ''
    mkdir -p $out/etc/ssh
    ${pkgs.openssh}/bin/ssh-keygen -A -f $out
    chmod 600 $out/etc/ssh/ssh_host_*_key
    chmod 644 $out/etc/ssh/ssh_host_*_key.pub
  '';

  # /root/.ssh/authorized_keys with the dev's pubkey.
  rootAuthorizedKeys = pkgs.runCommand "ssh-debug-authorized-keys" { } ''
    mkdir -p $out/root/.ssh
    cp ${pubkeyFile} $out/root/.ssh/authorized_keys
    chmod 700 $out/root/.ssh
    chmod 600 $out/root/.ssh/authorized_keys
  '';

  # /etc/passwd, /etc/group, /etc/shadow — sshd needs a real `root`
  # entry to authenticate the login. dockerTools.buildLayeredImage
  # does NOT bake any nss DB by default; without these podman /
  # `User = "root"` configurations fail with "no matching entries
  # in passwd file" before sshd even starts.
  nssFiles = pkgs.runCommand "ssh-debug-nss" { } ''
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
    # treats `!` / `*` prefixes as locked and refuses login even
    # with PermitRootLogin=yes. Empty means "no password required",
    # which is fine because PasswordAuthentication=no in sshd_config
    # blocks password auth entirely — only pubkey via the baked
    # authorized_keys works.
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

  sshdConfig = pkgs.writeText "sshd_config" ''
    Port ${toString sshdPort}
    ListenAddress 0.0.0.0
    AddressFamily any

    # Host keys (baked into the image).
    HostKey /etc/ssh/ssh_host_rsa_key
    HostKey /etc/ssh/ssh_host_ecdsa_key
    HostKey /etc/ssh/ssh_host_ed25519_key

    # `yes` (vs prohibit-password) is required because /etc/shadow
    # lists root as locked (`!`) — sshd's locked-account check fires
    # before auth methods are tried under prohibit-password.
    # PasswordAuthentication=no still blocks password auth entirely.
    PermitRootLogin yes
    PasswordAuthentication no
    PubkeyAuthentication yes
    KbdInteractiveAuthentication no

    # No /var/empty in this image; skip privsep.
    UsePAM no

    AuthorizedKeysFile /root/.ssh/authorized_keys

    PidFile /tmp/sshd.pid
    LogLevel INFO

    Subsystem sftp ${pkgs.openssh}/libexec/sftp-server

    # Keep the connection alive (debug sessions tend to idle).
    ClientAliveInterval 60
    ClientAliveCountMax 1440

    # Forward the env vars set on the image (e.g. PATH, PYTHONPATH,
    # SSH_DEBUG_*) into the interactive session so users don't need
    # to re-source anything after login.
    AcceptEnv LANG LC_*
    PermitUserEnvironment yes
  '';

  sshdConfigDir = pkgs.runCommand "ssh-debug-sshd-config" { } ''
    mkdir -p $out/etc/ssh
    cp ${sshdConfig} $out/etc/ssh/sshd_config
  '';

  # /root/.ssh/environment so an ssh login session sees the same
  # PATH/PYTHONPATH/etc. that the framework's secondary worker has.
  # sshd reads this verbatim (KEY=VALUE per line, no shell expansion,
  # no leading whitespace), so we use writeText for full control.
  rootEnvironmentFile = pkgs.writeText "ssh-debug-environment" ''
    PATH=/usr/local/bin:/usr/bin:/bin:/run/current-system/sw/bin
    PYTHONPATH=/app/python
    LANG=C.UTF-8
    SSH_DEBUG_SSHD_PORT=${toString sshdPort}
    SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
    CURL_CA_BUNDLE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
  '';

  rootEnvironment = pkgs.runCommand "ssh-debug-root-environment" { } ''
    mkdir -p $out/root/.ssh
    cp ${rootEnvironmentFile} $out/root/.ssh/environment
    chmod 600 $out/root/.ssh/environment
  '';

  # Default /etc/nix/nix.conf. Single-user mode (build-users-group
  # empty) is required because podman containers don't have nixbld*
  # users. Sandbox is disabled because podman's user namespace doesn't
  # allow the nested mount/cgroup ops sandboxed builds need.
  #
  # The `!include /etc/nix/peer.conf` directive does a SOFT include
  # of the peer-cache federation snippet that
  # ssh_debug_runner.bootstrap (`_PeerNixConfWatcher`) rewrites at
  # runtime as containers join/leave the SLURM run. Soft = nix
  # silently skips the directive if the file doesn't exist (e.g.
  # `podman run image:tag serve` standalone with no peers yet).
  nixConfFile = pkgs.writeText "nix.conf" ''
    experimental-features = nix-command flakes
    sandbox = false
    build-users-group =
    # See docker-image.nix nixConfFile for the rationale: without
    # max-jobs > 1 the daemon serializes every concurrent ``nix build``
    # from the worker pool. ``auto`` matches the container's CPU count.
    max-jobs = auto
    cores = 0
    substituters = https://cache.nixos.org
    trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=
    extra-trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY=

    !include /etc/nix/peer.conf
  '';

  nixConfDir = pkgs.runCommand "ssh-debug-nix-conf" { } ''
    mkdir -p $out/etc/nix
    cp ${nixConfFile} $out/etc/nix/nix.conf
    chmod 644 $out/etc/nix/nix.conf
  '';

  cachixPkg =
    if includeCachix then (pkgs.cachix or null) else null;

  # SUPERSET of docker-image.nix's basePkgs: same nix-build-capable
  # foundation, plus openssh and the small text-processing tools a
  # debug session typically wants on PATH.
  basePkgs = [
    pkgs.nix
    pkgs.gitMinimal
    pkgs.cacert
    pkgs.coreutils
    pkgs.bash
    pkgs.openssh
    pkgs.gnused
    pkgs.gawk
    pkgs.gnugrep
    pkgs.findutils
    pkgs.less
    pkgs.which
    pkgs.procps
  ];

  imageContents =
    basePkgs
    ++ [
      pythonEnv
      projectFiles
      hostKeys
      rootAuthorizedKeys
      sshdConfigDir
      rootEnvironment
      nssFiles
      nixConfDir
    ]
    ++ lib.optional (harmonia != null) harmonia
    ++ lib.optional (cachixPkg != null) cachixPkg
    ++ contents;

  previousAssignment =
    semanticLayering.readAssignmentFromEnv "NIX_DOCKER_LAYER_CACHE";

  # Layer plan mirrors docker-image.nix where the units overlap; the
  # ssh-specific units come last so they don't reshuffle the basics
  # tier vs. the runner image's blob cache.
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
        name = "openssh";
        roots = [ pkgs.openssh ];
      }
      {
        name = "project-code";
        roots = [ projectFiles ];
        isolate = true;
      }
      {
        name = "ssh-keys-and-config";
        roots = [
          hostKeys
          rootAuthorizedKeys
          sshdConfigDir
          rootEnvironment
          nssFiles
          nixConfDir
        ];
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
      "SSH_DEBUG_SSHD_PORT=${toString sshdPort}"
      # See docker-image.nix Env for the rationale: without this, every
      # parallel ``nix build`` opens db.sqlite directly and they
      # contend on the SQLite write lock instead of serializing through
      # the running nix-daemon socket.
      "NIX_REMOTE=daemon"
      # TEMPORARY — gathering trace-level transport logs for the
      # primary-timeout diagnosis the dynamic_runner peer asked for.
      # Drop once the LMU CIP reverse-connection mode bug is fixed.
      "RUST_LOG=trace,quinn=warn,rustls=warn"
      # cacert is in basePkgs but its bundle path needs to be exposed
      # explicitly for nix / curl / openssl to find it. Without these,
      # `nix shell nixpkgs#…` inside the container fails to download
      # the flake registry over HTTPS.
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "CURL_CA_BUNDLE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
    ];

    # Match docker-image.nix: SLURM wrapper appends to Entrypoint and
    # replaces Cmd, so secondary spawn becomes
    # `python -m ssh_debug_runner --secondary <primary> ...`.
    Entrypoint = [
      "python"
      "-m"
    ];
    # `serve` is ssh_debug_runner's standalone entrypoint: it runs
    # the bootstrap (nix-daemon + harmonia + peer-watcher) and exec's
    # sshd in foreground so a fresh `podman run image:tag` is
    # immediately ssh-able with the baked authorized_keys. Under the
    # SLURM dispatch path the framework appends
    # `<secondary_module> --secondary <url> ...` to the Entrypoint,
    # which REPLACES Cmd — so `serve` only fires for direct
    # `podman run` invocations.
    Cmd = [
      "ssh_debug_runner"
      "serve"
    ];

    WorkingDir = "/app";
    User = "root";
  };
}

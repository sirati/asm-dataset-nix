# Build one variant: (package, compiler, architecture, opt-level,
# flag-set, hardening, sanitizer, march).
#
# Returns: { variantLabel, variantPkg, meta }

{ pkgs, lib }:

{
  pkg, # { attr, label } from packages.nix
  compiler, # { name, family, label, version, mkStdenv } from compilers.nix
  target, # { label, crossAttr, system } from architectures.nix
  optLevel, # { flag, label, clangOnly } from flags.nix
  flagSet, # { label, cflags, cxxflags, ... } from flags.nix
  hardening, # { label, hardeningEnable, hardeningDisable, extraCflags? } from flags.nix
  sanitizer ? {
    label = "san-off";
    cflags = "";
    linkFlags = "";
  }, # { label, cflags, linkFlags } from flags.nix
  march ? {
    label = "march-default";
    cflags = "";
  }, # { label, cflags } from flags.nix
}:

let
  archLib = import ./architectures.nix { };

  # Get the pkgs set for this target (native or cross)
  targetPkgs =
    let
      p = archLib.getPkgsForTarget pkgs target;
    in
    if p == null then builtins.throw "cross target ${target.label} not available" else p;

  # Build the custom stdenv using the compiler's mkStdenv
  baseStdenv = compiler.mkStdenv targetPkgs target;

  # Fix clang ppc64: two issues with old clang + ppc64 ELFv2 target:
  # 1. Linker: clang normalizes powerpc64-unknown-linux-gnuabielfv2 to
  #    powerpc64-unknown-linux-gnu internally, then can't find the cross linker.
  #    Fix: inject -fuse-ld=<path> (clang >=3.9) or create a ld symlink dir
  #    with the normalized triple name (clang <3.9).
  # 2. ABI: old clang versions (<18) from old nixpkgs default to ELFv1 for ppc64,
  #    but the sysroot/glibc is ELFv2. Fix: inject -mabi=elfv2.
  # Both fixes use overrideAttrs+postFixup (not override { extraBuildCommands })
  # to avoid replacing the resource-root setup.

  # Parse clang major version for version-gated fixes.
  clangMajor =
    let
      parts = builtins.match "([0-9]+)\\..*" (compiler.version or "0");
    in
    if parts != null then lib.toInt (builtins.head parts) else 0;

  customStdenv =
    if compiler.family == "clang" && target.label == "ppc64" then
      let
        cc = baseStdenv.cc;
        ldPath = "${cc.bintools}/bin/${cc.bintools.targetPrefix}ld";
        # clang <3.9 doesn't support -fuse-ld=<absolute-path>. Instead, create
        # a directory with a symlink using the normalized triple name that clang
        # searches for (powerpc64-unknown-linux-gnu-ld).
        ldSymlinkFix = ''
          mkdir -p $out/ppc64-ld-fix
          ln -s ${ldPath} $out/ppc64-ld-fix/powerpc64-unknown-linux-gnu-ld
          echo "-B$out/ppc64-ld-fix" >> $out/nix-support/cc-cflags
        '';
        # clang >=3.9 supports -fuse-ld=<absolute-path>.
        ldFuseFix = ''
          echo "-fuse-ld=${ldPath}" >> $out/nix-support/cc-cflags
        '';
        fixedCC = cc.overrideAttrs (old: {
          postFixup = (old.postFixup or "") + ''
            ${if clangMajor < 4 then ldSymlinkFix else ldFuseFix}
            echo "-mabi=elfv2" >> $out/nix-support/cc-cflags
          '';
        });
      in
      targetPkgs.overrideCC baseStdenv fixedCC
    else
      baseStdenv;

  # Resolve compiler-specific flags (e.g., novec differs between gcc and clang)
  resolvedCflags =
    if flagSet.cflags == null then
      (if compiler.family == "gcc" then flagSet.gccFlag else flagSet.clangFlag)
    else
      flagSet.cflags;

  resolvedCxxflags =
    if flagSet.cxxflags == null then
      (if compiler.family == "gcc" then flagSet.gccFlag else flagSet.clangFlag)
    else
      flagSet.cxxflags;

  # Link-time flags, split by channel (see the header of flags.nix):
  #
  #  - ldflags/extraLdflags        → NIX_LDFLAGS        (raw ld flags,
  #    e.g. ``-z relro``; the ld-wrapper feeds them straight to ld)
  #  - linkFlags/extraLinkFlags    → NIX_CFLAGS_LINK    (compiler-driver
  #    flags applied only on linking invocations, e.g. ``-fsanitize=x``,
  #    ``-flto``, ``-static-pie``; the driver expands them into proper
  #    ld args + runtime libs)
  #  - extraCflagsBefore           → NIX_CFLAGS_COMPILE_BEFORE (driver
  #    flags PREPENDED before the package's own args — needed for
  #    ``-pie``, where a later ``-shared`` must win on gcc's last-wins
  #    pie/shared Negative pair so shared objects don't get linked as
  #    PIE executables)
  #
  # Sanitizers need -fsanitize=X on both compile and link; the compile
  # side rides NIX_CFLAGS_COMPILE, the link side NIX_CFLAGS_LINK.
  sanitizerCflags = sanitizer.cflags or "";
  sanitizerLinkFlags = sanitizer.linkFlags or "";
  marchCflags = march.cflags or "";

  extraLdflags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      (flagSet.ldflags or "")
      (sanitizer.ldflags or "")
      (hardening.extraLdflags or "")
    ]
  );

  extraLinkFlags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      (flagSet.linkFlags or "")
      sanitizerLinkFlags
      (hardening.extraLinkFlags or "")
    ]
  );

  extraCflagsBefore = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      (flagSet.extraCflagsBefore or "")
      (hardening.extraCflagsBefore or "")
    ]
  );

  # Extra hardening flags to disable for this flag set (e.g., pie for nopic)
  extraHardeningDisable = flagSet.extraHardeningDisable or [ ];

  # Hardening profile injects flags directly (cflags/ldflags) rather
  # than relying on cc-wrapper's hardeningEnable hook, since the set
  # of supported names varies across nixpkgs versions used by old vs
  # current compilers.
  hardeningExtraCflags = hardening.extraCflags or "";

  # Combine optimization level + extra flags + sanitizer + march +
  # hardening-extra. Empty strings are filtered out so we don't emit
  # leading/trailing whitespace.
  allCflags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      optLevel.flag
      resolvedCflags
      sanitizerCflags
      marchCflags
      hardeningExtraCflags
    ]
  );

  allCxxflags = lib.concatStringsSep " " (
    builtins.filter (s: s != "") [
      optLevel.flag
      resolvedCxxflags
      sanitizerCflags
      marchCflags
      hardeningExtraCflags
    ]
  );

  # Static-linking variants (-static-pie) need the target libc's static
  # archives on the link path; the default stdenv only carries the
  # shared glibc. ``glibc.static`` is the lib output with libc.a/libm.a.
  staticLibc =
    if flagSet.needsStaticLibc or false then targetPkgs.glibc.static or null else null;

  # Variant label encodes the full combination
  variantLabel = lib.concatStringsSep "-" [
    pkg.label
    target.label
    compiler.label
    optLevel.label
    flagSet.label
    hardening.label
    sanitizer.label
    march.label
  ];

  # Override the package with our custom stdenv and flags
  basePkg = targetPkgs.${pkg.attr}.override { stdenv = customStdenv; };

  # Combine hardening disables from the hardening mode + flag set
  allHardeningDisable =
    if hardening.hardeningDisable == [ "all" ] then
      [ "all" ]
    else
      hardening.hardeningDisable ++ extraHardeningDisable;
  allHardeningEnable = hardening.hardeningEnable or [ ];

  variantPkg = basePkg.overrideAttrs (
    old:
    let
      # Some packages use env.NIX_CFLAGS_COMPILE (newer pattern), others use the
      # top-level attribute (legacy). We must place our flags where the package expects.
      inEnv = (old.env or { }) ? NIX_CFLAGS_COMPILE;

      getOld = attr: if inEnv then (old.env.${attr} or "") else (old.${attr} or "");

      mergedCflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CFLAGS_COMPILE")
          allCflags
        ]
      );
      mergedCxxflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CXXFLAGS_COMPILE")
          allCxxflags
        ]
      );
      mergedLdflags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_LDFLAGS")
          extraLdflags
        ]
      );
      mergedLinkFlags = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CFLAGS_LINK")
          extraLinkFlags
        ]
      );
      mergedCflagsBefore = lib.concatStringsSep " " (
        builtins.filter (s: s != "") [
          (getOld "NIX_CFLAGS_COMPILE_BEFORE")
          extraCflagsBefore
        ]
      );

      # Build the flag attrs — either in env or at top level
      newFlags = {
        NIX_CFLAGS_COMPILE = mergedCflags;
        NIX_CXXFLAGS_COMPILE = mergedCxxflags;
      }
      // lib.optionalAttrs (mergedLdflags != "") {
        NIX_LDFLAGS = mergedLdflags;
      }
      // lib.optionalAttrs (mergedLinkFlags != "") {
        NIX_CFLAGS_LINK = mergedLinkFlags;
      }
      // lib.optionalAttrs (mergedCflagsBefore != "") {
        NIX_CFLAGS_COMPILE_BEFORE = mergedCflagsBefore;
      };

      flagAttrs = if inEnv then { env = (old.env or { }) // newFlags; } else newFlags;

    in
    flagAttrs
    // lib.optionalAttrs (staticLibc != null) {
      buildInputs = (old.buildInputs or [ ]) ++ [ staticLibc ];
    }
    // {
      pname = "${old.pname or pkg.attr}-variant";
      hardeningDisable = allHardeningDisable;
      hardeningEnable = allHardeningEnable;

      # Skip tests — we only care about the compiled binaries
      doCheck = false;
      doInstallCheck = false;

      # Metadata for manifest generation
      passthru = (old.passthru or { }) // {
        datasetMeta = {
          inherit variantLabel;
          package = pkg.label;
          arch = target.label;
          compiler = compiler.label;
          compilerVersion = compiler.version;
          compilerFamily = compiler.family;
          optimization = optLevel.label;
          flags = flagSet.label;
          hardening = hardening.label;
          sanitizer = sanitizer.label;
          march = march.label;
        };
      };
    }
  );

in
{
  inherit variantLabel variantPkg;
  meta = variantPkg.passthru.datasetMeta;
}

# Old compiler discovery from legacy nixpkgs inputs.
# Produces compiler entries in the same shape as compilers.nix.
#
# Takes a list of { oldPkgs, gccSpecs, clangSpecs, nixpkgsSrc?, system? }
# records describing which compiler versions to extract from each old nixpkgs.
#
# Cross-compilation strategies (in order of preference):
#
# 1. pkgsCross available (nixpkgs 22.11+): use buildPackages.<compiler>
#    directly. For GCC, override depsBuildBuild for version-matched bootstrap.
#
# 2. No pkgsCross but nixpkgsSrc + buildPackages available (nixpkgs 18.03):
#    re-import with crossSystem, extract unwrapped .cc from the old cross-gcc,
#    wrap with modern cc-wrapper (hybrid approach — avoids broken old wrappers).
#
# 3. No pkgsCross, no buildPackages but nixpkgsSrc + gccSubdir (nixpkgs 15.09):
#    call the old gcc expression directly with cross params, build using native
#    gccN as the build compiler. Wrap result with modern cc-wrapper.
#
# Uses tryEval for safety — gracefully skips compilers that fail evaluation.
# Uses explicit spec lists rather than auto-discovery (old nixpkgs attr names vary).
#
# Returns: { gcc = [ ... ]; clang = [ ... ]; all = [ ... ]; }

{
  pkgs,
  lib,
  oldNixpkgsSets,
}:

let
  archLib = import ./architectures.nix { };
  oldGccCross = import ./old-gcc-cross.nix { inherit pkgs lib; };

  # Normalize ``meta.priority`` to an integer. Old nixpkgs (15.09, 18.03)
  # set the gcc4_x wrapper's ``meta.priority = "10";`` as a STRING.
  # That makes ``nix build .#flake-attr^*`` reject the attr with
  # ``error: 'meta.priority' is not an integer`` — even though every
  # other code path treats the wrapper fine. Coerce to int when we see
  # a string; pass through unchanged otherwise.
  #
  # Wired into ``oldPkgs.${attr}`` lookups below so every downstream
  # consumer (variant stdenvs, crossToolchainMap, manifest emission)
  # sees the clean drv.
  fixMetaPriority =
    drv:
    if (drv ? meta) && (drv.meta ? priority)
       && (builtins.typeOf drv.meta.priority == "string") then
      drv // {
        meta = drv.meta // { priority = lib.toInt drv.meta.priority; };
      }
    else
      drv;

  # Ensure the cc-wrapper exposes ``targetPrefix``. Very old cc-wrappers
  # (nixpkgs 15.09, 18.03 — gcc4.4/4.5/4.6) predate the attribute, so
  # upstream nixpkgs derivations that read ``stdenv.cc.targetPrefix``
  # unconditionally (busybox, zlib, ...) throw ``attribute 'targetPrefix'
  # missing`` at eval time. Modern cc-wrappers compute it from the
  # platform: empty string for native, ``"<target-triple>-"`` for cross.
  #
  # Callers pass the resolved prefix because we don't have a platform
  # attrset on the wrapper itself at this point — native is just ``""``,
  # cross paths build their wrapper via ``modernCrossGcc.override`` and
  # therefore already have the modern wrapper's computed value.
  ensureTargetPrefix =
    prefix: drv:
    if drv ? targetPrefix then drv else drv // { targetPrefix = prefix; };

  # Generalised cc-wrapper attribute backfill. Modern cc-wrappers always
  # expose a set of attrs (``targetPrefix``, ``isGNU``, ``isClang``,
  # ``libc``, ...) that upstream nixpkgs derivations read unconditionally:
  #
  #   - openssl/default.nix:        stdenv.cc.isGNU
  #   - tinycc/package.nix:         stdenv.cc.libc
  #   - busybox/zlib/gawk:          stdenv.cc.targetPrefix
  #
  # Very old wrappers (15.09 / 18.03) and the raw-binary clang3.4/3.5
  # path (where ``extractClangCC`` returns ``llvmPkg.clang`` itself, not
  # a wrapper) miss one or more of these. ``ensureCcAttrs`` overlays
  # only the missing ones with the caller-supplied defaults; present
  # attrs pass through unchanged (idempotent).
  #
  # ``defaults`` is an attrset of ``{ attrName = defaultValue; ... }``;
  # the same shape as ``//`` but applied per-attr with a presence check.
  ensureCcAttrs =
    defaults: drv:
    let
      missing = lib.filterAttrs (n: _: !(drv ? ${n})) defaults;
    in
    drv // missing;

  # Per-family defaults for the cc-wrapper backfills. ``targetPrefix``
  # varies between native ("") and cross ("<triple>-"), so it's
  # supplied separately by callers.
  #
  # ``libc`` defaults to the target pkgs' stdenv.cc.libc — that's the
  # glibc (or musl) the wrapper effectively links against once it's
  # plugged into the target stdenv via ``overrideCC``. ``isGNU``/
  # ``isClang`` are constants per family.
  gccCcDefaults = targetPkgs: {
    isGNU = true;
    isClang = false;
    libc = targetPkgs.stdenv.cc.libc;
  };
  clangCcDefaults = targetPkgs: {
    isGNU = false;
    isClang = true;
    libc = targetPkgs.stdenv.cc.libc;
  };

  # Extract version string from a Clang package across different nixpkgs eras.
  # Modern: .clang.version exists
  # Old (3.7+): .clang.cc.name is "clang-X.Y.Z", extract version from name
  # Very old (3.4-3.5): .clang.name is "clang-X.Y.Z" (no wrapper)
  extractClangVersion =
    llvmPkg:
    let
      clang = llvmPkg.clang;
      # Modern path: .clang.version (nixpkgs 22.11+)
      modern =
        if clang ? version then
          builtins.tryEval clang.version
        else
          {
            success = false;
            value = null;
          };
      # Old wrapper path: .clang.cc.name = "clang-X.Y.Z"
      oldCC =
        if (clang ? cc) && (clang.cc ? name) then
          builtins.tryEval clang.cc.name
        else
          {
            success = false;
            value = null;
          };
      # Very old path: .clang.name = "clang-X.Y.Z" or "clang-wrapper-X.Y.Z"
      oldName =
        if clang ? name then
          builtins.tryEval clang.name
        else
          {
            success = false;
            value = null;
          };
      # Extract version from "clang-X.Y.Z" or "clang-wrapper-X.Y.Z"
      extractFromName =
        name:
        let
          m1 = builtins.match "clang-wrapper-(.*)" name;
          m2 = builtins.match "clang-(.*)" name;
        in
        if m1 != null then
          builtins.head m1
        else if m2 != null then
          builtins.head m2
        else
          name;
    in
    if modern.success then
      modern.value
    else if oldName.success && oldName.value != "" then
      extractFromName oldName.value
    else if oldCC.success && oldCC.value != "" then
      extractFromName oldCC.value
    else
      null;

  # Extract the CC wrapper from an old LLVM package for use with overrideCC.
  # Modern (5+): .stdenv.cc exists and is a proper cc-wrapper
  # Old (3.6-4): .stdenv.cc exists but may be named differently
  # Very old (3.4-3.5): no .stdenv.cc, use .clang directly
  extractClangCC =
    llvmPkg:
    let
      hasStdenvCC = (llvmPkg ? stdenv) && (llvmPkg.stdenv ? cc) && (llvmPkg.stdenv.cc ? name);
      stdenvCCName =
        if hasStdenvCC then
          builtins.tryEval llvmPkg.stdenv.cc.name
        else
          {
            success = false;
            value = "";
          };
    in
    if stdenvCCName.success && stdenvCCName.value != "" then
      llvmPkg.stdenv.cc
    else if llvmPkg ? clang then
      llvmPkg.clang
    else
      null;

  # Extract the unwrapped clang binary from an old LLVM package.
  # Modern (3.7+): .clang.cc is the unwrapped clang (e.g. "clang-3.7.1")
  # Very old (3.4-3.5): .clang IS the raw binary (no .cc, or .cc is gcc)
  extractUnwrappedClang =
    llvmPkg:
    let
      clang = llvmPkg.clang;
      hasCC = clang ? cc;
      ccName = if hasCC then (builtins.tryEval clang.cc.name).value or "" else "";
      ccIsClang = builtins.match "clang-.*" ccName != null;
    in
    if hasCC && ccIsClang then
      clang.cc # 3.7, 3.8, 3.9, 4.0: .cc is the unwrapped clang
    else
      clang; # 3.4, 3.5: .clang itself is the raw binary

  # Determine which hardening flags an old clang doesn't support.
  # These flags are injected by the modern cc-wrapper but old clangs reject them.
  #
  # IMPORTANT: this list must be exhaustive. Modern cc-wrapper's
  # default hardening set grows over time — every new hardening
  # flag that clang grew support for in version N means clang < N
  # rejects it and autoconf's compile test fails (silent
  # "C compiler cannot create executables" error). Cross-reference
  # against ``docs/clang-flag-support-matrix.md`` when bumping
  # nixpkgs to catch any new flags.
  getClangUnsupportedHardeningFlags =
    version:
    let
      parts = builtins.match "([0-9]+)\\.([0-9]+).*" version;
      major = if parts != null then lib.toInt (builtins.elemAt parts 0) else 0;
      minor = if parts != null then lib.toInt (builtins.elemAt parts 1) else 0;
    in
    # -fstack-protector-strong: added in clang 3.5
    lib.optional (major < 3 || (major == 3 && minor < 5)) "stackprotector"
    # -fstack-clash-protection: added in clang 11
    ++ lib.optional (major < 11) "stackclashprotection"
    # -fzero-call-used-regs: added in clang 16
    ++ lib.optional (major < 16) "zerocallusedregs"
    # -fstrict-flex-arrays={1,3}: added in clang 16. Without
    # stripping, nix's default hardening set passes
    # -fstrict-flex-arrays=1 to the compiler; clang < 16 errors
    # with ``unknown argument`` before autoconf's first compile-test
    # can even reach the linker. The wrapper's add-hardening.sh
    # supports stripping these by name when listed here.
    ++ lib.optional (major < 16) "strictflexarrays1"
    ++ lib.optional (major < 16) "strictflexarrays3";

  # Whether an old clang needs -fmacro-prefix-map stripped from the wrapper.
  # -fmacro-prefix-map was added in clang 10.
  clangNeedsMacroPrefixMapStripped =
    version:
    let
      parts = builtins.match "([0-9]+)\\..*" version;
      major = if parts != null then lib.toInt (builtins.head parts) else 0;
    in
    major < 10;

  # Build commands to strip -fmacro-prefix-map flags from nix-support files.
  # Old clangs (<10) don't support this flag.
  stripMacroPrefixMapCommands = ''
    for f in $out/nix-support/cc-cflags $out/nix-support/libc-cflags $out/nix-support/libcxx-cxxflags; do
      if [ -f "$f" ]; then
        sed -i "s/ -fmacro-prefix-map=[^ ]*//g" "$f"
      fi
    done
    sed -i '/-fmacro-prefix-map/d' $out/nix-support/setup-hook
  '';

  # Parse the clang major version once for reuse.
  clangMajor =
    version:
    let parts = builtins.match "([0-9]+)\\..*" version;
    in if parts != null then lib.toInt (builtins.head parts) else 0;

  # Per-(arch, old-clang-version) ABI overrides. The hybrid wrapper splices
  # modern binutils + libgcc against an old clang binary; for archs whose
  # ABI defaults shifted between the old-clang era and modern nixpkgs, the
  # linker rejects the combo with "file in wrong format" or "soft-float vs
  # double-float". The flags below pin codegen to the ABI the modern libgcc
  # is built for, recovering link compatibility. Per-combo because some
  # old-clang backends don't honour the flag and fall through to wrong
  # ABI silently (e.g. clang3.4's ppc64 backend still wants ELFv1 even
  # under -mabi=elfv2, which we can't paper over here).
  abiFlagsFor =
    arch: version:
    let major = clangMajor version; in
    # mips64el-gnuabin32: modern libgcc is built for N32 ABI. clang's
    # default for the gnuabin32 triple is sometimes N64; pin to n32.
    # Confirmed-working from clang3.4 through clang7 in repro tests.
    if arch == "mips64el" && major <= 7 then [ "-mabi=n32" ]
    # riscv64-gnu: modern glibc/libgcc is double-float (lp64d); clang9
    # may default to soft-float (lp64). Force lp64d and rv64gc march.
    else if arch == "riscv64" && major == 9 then [ "-mabi=lp64d" "-march=rv64gc" ]
    else [ ];

  # Format an abi-flag list as a postFixup-shell snippet appended to
  # cc-cflags. The leading space matters — cc-wrapper concatenates
  # the file's contents into the final argv without re-adding separators.
  abiPostFixup =
    flags:
    lib.optionalString (flags != [ ]) ''
      echo " ${lib.concatStringsSep " " flags}" >> $out/nix-support/cc-cflags
    '';

  # Get cross-compiler from old nixpkgs for a given target.
  # For pre-pkgsCross nixpkgs, re-imports with crossSystem to get
  # buildPackages which contain the cross-compilers.
  #
  # Returns the cross-imported pkgs set with buildPackages, or null
  # if cross-compilation is not possible.
  getOldCrossPkgs =
    {
      oldPkgs,
      nixpkgsSrc ? null,
      system ? null,
    }:
    target:
    if oldPkgs ? pkgsCross then
      # Modern nixpkgs: use pkgsCross directly (returns null if cross attr missing)
      archLib.getPkgsForTarget oldPkgs target
    else if nixpkgsSrc != null && system != null then
      # Pre-pkgsCross nixpkgs: re-import with crossSystem.
      # Prefer ``target.crossSystem`` when defined — that's the
      # complete crossSystem attrset (including ``platform.kernelArch``
      # for archs whose legacy nixpkgs ``lib.systems`` lookup is
      # missing it; e.g. ppc32 in nixpkgs-18.03 throws
      # ``attribute 'kernelArch' missing`` from kernel-headers
      # without an explicit value). Fall back to the bare config
      # triple for archs that don't need any platform overrides.
      import nixpkgsSrc {
        inherit system;
        crossSystem = target.crossSystem or {
          config = target.crossConfig;
        };
        config.allowUnfree = true;
      }
    else
      null;

  # Build a compiler entry for an old GCC version.
  # Uses the old nixpkgs' GCC but injects it into the current nixpkgs' stdenv,
  # so we get old compiler + current libc/binutils.
  #
  # For cross-compilation with pkgsCross (22.11+): overrides depsBuildBuild
  # on the unwrapped cross GCC to use the native GCC of the same version.
  #
  # For cross-compilation with nixpkgsSrc + buildPackages (18.03): re-import
  # with crossSystem, extract unwrapped .cc, wrap with modern cc-wrapper.
  #
  # For cross-compilation with nixpkgsSrc + gccSubdir (15.09): call old gcc
  # expression directly with cross params, wrap with modern cc-wrapper.
  mkOldGccEntry =
    nixpkgsInfo: # { oldPkgs, nixpkgsSrc?, system? }
    {
      attr,
      label,
      gccSubdir ? null,
    }:
    let
      oldPkgs = nixpkgsInfo.oldPkgs;
      tried = builtins.tryEval (oldPkgs.${attr}.cc.version or oldPkgs.${attr}.version);
      # Normalised reference to the old gcc wrapper — meta.priority
      # coerced to int so flake-attr ``^*`` builds work.
      cleanGcc = fixMetaPriority oldPkgs.${attr};
    in
    if !tried.success then
      null
    else
      {
        name = attr;
        family = "gcc";
        label = "gcc${label}";
        version = tried.value;
        mkStdenv =
          targetPkgs: target:
          if target.crossAttr == null && !(target ? crossSystem) then
            # Native: just use the old compiler directly. Backfill
            # ``targetPrefix = ""`` for very-old wrappers (15.09, 18.03)
            # that predate the attribute — upstream nixpkgs derivations
            # (busybox, zlib, ...) read it unconditionally. ``isGNU``,
            # ``isClang`` and ``libc`` are already present on gcc4_x
            # wrappers but we pass them through ``ensureCcAttrs`` for
            # symmetry (idempotent — no-op when attr already present).
            targetPkgs.overrideCC targetPkgs.stdenv (
              ensureCcAttrs (gccCcDefaults targetPkgs // { targetPrefix = ""; }) cleanGcc
            )
          else if oldPkgs ? pkgsCross then
            # pkgsCross available (22.11+): use buildPackages with depsBuildBuild bootstrap
            let
              oldCrossPkgs = getOldCrossPkgs nixpkgsInfo target;
            in
            if oldCrossPkgs == null then
              builtins.throw "${attr}: cross target ${target.label} not available in this nixpkgs"
            else
              let
                oldCrossGcc = oldCrossPkgs.buildPackages.${attr};
                bootstrappedCC = oldCrossGcc.cc.overrideAttrs (old: {
                  depsBuildBuild = [ oldPkgs.${attr} ];
                  # Disable libsanitizer for old cross GCC — its struct stat
                  # definitions can mismatch the cross glibc headers (e.g. gcc12/mips64el).
                  configureFlags = (old.configureFlags or [ ]) ++ [ "--disable-libsanitizer" ];
                });
                rewrapped = oldCrossGcc.override { cc = bootstrappedCC; };
              in
              targetPkgs.overrideCC targetPkgs.stdenv rewrapped
          else if nixpkgsInfo.nixpkgsSrc != null && nixpkgsInfo.system != null && gccSubdir != null then
            # Pre-buildPackages (15.09): call old gcc expression directly with
            # cross params, build using native gccN, wrap with modern cc-wrapper.
            oldGccCross.mkCrossGccFromOldExpr {
              nixpkgsSrc = nixpkgsInfo.nixpkgsSrc;
              inherit gccSubdir oldPkgs attr;
            } targetPkgs target
          else if nixpkgsInfo.nixpkgsSrc != null && nixpkgsInfo.system != null then
            # Pre-pkgsCross but has buildPackages (18.03): re-import with
            # crossSystem, extract unwrapped .cc, wrap with modern cc-wrapper.
            oldGccCross.mkCrossGccFrom1803 {
              nixpkgsSrc = nixpkgsInfo.nixpkgsSrc;
              system = nixpkgsInfo.system;
              inherit oldPkgs attr;
            } targetPkgs target
          else
            builtins.throw "${attr}: cross-compilation not supported (no pkgsCross and no nixpkgsSrc)";
      };

  # Build a compiler entry for an old Clang/LLVM version.
  # Handles multiple eras of LLVM packaging in nixpkgs:
  # - Modern (5+): .clang.version and .stdenv.cc
  # - Old (3.6-4): .stdenv.cc exists, version from .clang.cc.name
  # - Very old (3.4-3.5): only .clang, version from .clang.name
  #
  # Cross-compilation strategies:
  # 1. pkgsCross available (22.11+): use buildPackages.<llvmPkg>.clang
  # 2. Pre-pkgsCross (18.03): hybrid wrapper — modern cross cc-wrapper with
  #    the old unwrapped clang binary swapped in. The old nixpkgs' own cross
  #    infrastructure has broken C++ stdlib hooks, so we bypass it entirely.
  mkOldClangEntry =
    nixpkgsInfo: # { oldPkgs, nixpkgsSrc?, system? }
    { attr, label }:
    let
      oldPkgs = nixpkgsInfo.oldPkgs;
      llvmPkg = oldPkgs.${attr};
      tried = builtins.tryEval llvmPkg;
      version = if tried.success then extractClangVersion llvmPkg else null;
    in
    if !tried.success || version == null then
      null
    else
      {
        name = attr;
        family = "clang";
        label = "clang${label}";
        inherit version;
        mkStdenv =
          targetPkgs: target:
          if target.crossAttr == null && !(target ? crossSystem) then
            # Native: use extractClangCC on the native LLVM package.
            # Backfill missing cc-wrapper attrs for very-old clang
            # wrappers (nixpkgs 15.09 / 18.03 — clang3.4 through clang4).
            # Their wrappers predate or omit attrs that upstream nixpkgs
            # derivations read unconditionally:
            #   - targetPrefix  (gawk, busybox, zlib)
            #   - isGNU         (openssl)
            #   - libc          (tinycc)
            # The very-old clang3.4/3.5 path returns ``llvmPkg.clang``
            # itself (no real wrapper) so all three are missing.
            # Wrappers that already expose the attr keep their value
            # unchanged — ``ensureCcAttrs`` only fills holes. (The
            # 15.09 clang3.6 wrapper inherits ``isGNU=true`` from the
            # gcc-based stdenv; that's mildly inaccurate but only
            # gates ``separateDebugInfo`` downstream and doesn't fail
            # eval, so we leave it.)
            let
              cc = extractClangCC oldPkgs.${attr};
            in
            targetPkgs.overrideCC targetPkgs.stdenv (
              ensureCcAttrs (clangCcDefaults targetPkgs // { targetPrefix = ""; }) cc
            )
          else if oldPkgs ? pkgsCross then
            # pkgsCross available (22.11+): get cross-clang from buildPackages.
            # Use .clang directly (not extractClangCC) because
            # buildPackages.llvmPkg.stdenv.cc is the native compiler,
            # while .clang is the actual cross-compiler wrapper.
            let
              oldCrossPkgs = getOldCrossPkgs nixpkgsInfo target;
            in
            if oldCrossPkgs == null then
              builtins.throw "${attr}: cross target ${target.label} not available in this nixpkgs"
            else
              let
                rawCrossClang = oldCrossPkgs.buildPackages.${attr}.clang;
                abiFlags = abiFlagsFor target.label version;
                # Override the wrapper's postFixup to append ABI flags to
                # cc-cflags. The wrapper is already built; appending via
                # overrideAttrs reruns its installation phase with the
                # extra commands tacked on.
                crossClang =
                  if abiFlags == [ ] then
                    rawCrossClang
                  else
                    rawCrossClang.overrideAttrs (old: {
                      postFixup = (old.postFixup or "") + abiPostFixup abiFlags;
                    });
              in
              # Defensive backfill: 22.11+ cross wrappers generally
              # expose all cc-attrs, but old-LLVM cross attrs vary
              # across nixpkgs eras. Idempotent — no-op when present.
              targetPkgs.overrideCC targetPkgs.stdenv (
                ensureCcAttrs
                  (clangCcDefaults targetPkgs
                   // { targetPrefix = "${target.crossConfig}-"; })
                  crossClang
              )
          else
            # Pre-pkgsCross (18.03, 15.09): hybrid wrapper approach.
            # The old nixpkgs' cross infrastructure has broken C++ stdlib
            # hooks that reference cross-compiled GCC binaries. Instead,
            # take the modern cross clang wrapper and swap in the old
            # unwrapped clang binary. This gives us correct cross bintools
            # and sysroot from modern nixpkgs + old clang codegen.
            let
              unwrappedClang = extractUnwrappedClang oldPkgs.${attr};
              unsupportedFlags = getClangUnsupportedHardeningFlags version;
              needsStripMacroMap = clangNeedsMacroPrefixMapStripped version;
              abiFlags = abiFlagsFor target.label version;

              # Use the modern cross clang wrapper as a template
              modernCrossClang = targetPkgs.buildPackages.llvmPackages.clang;
              hybridClang = modernCrossClang.override {
                cc = unwrappedClang // {
                  hardeningUnsupportedFlags = unsupportedFlags;
                };
                propagateDoc = false;
                extraBuildCommands =
                  (if needsStripMacroMap then stripMacroPrefixMapCommands else "")
                  + abiPostFixup abiFlags;
              };
            in
            targetPkgs.overrideCC targetPkgs.stdenv hybridClang;
      };

  # Process one old nixpkgs set into compiler entries.
  processOldNixpkgs =
    {
      oldPkgs,
      gccSpecs,
      clangSpecs,
      nixpkgsSrc ? null,
      system ? null,
    }:
    let
      nixpkgsInfo = { inherit oldPkgs nixpkgsSrc system; };
      gccEntries = builtins.filter (x: x != null) (map (mkOldGccEntry nixpkgsInfo) gccSpecs);
      clangEntries = builtins.filter (x: x != null) (map (mkOldClangEntry nixpkgsInfo) clangSpecs);
    in
    {
      gcc = gccEntries;
      clang = clangEntries;
    };

  allResults = map processOldNixpkgs oldNixpkgsSets;

  mergedGcc = lib.concatMap (r: r.gcc) allResults;
  mergedClang = lib.concatMap (r: r.clang) allResults;

  # Deduplicate by label — if the same label appears multiple times
  # (e.g. gcc48 from both 18.03 and 22.11), keep the first occurrence.
  dedupByLabel =
    comps:
    builtins.attrValues (
      builtins.listToAttrs (
        lib.reverseList (
          map (c: {
            name = c.label;
            value = c;
          }) comps
        )
      )
    );

  dedupGcc = dedupByLabel mergedGcc;
  dedupClang = dedupByLabel mergedClang;

in
{
  gcc = dedupGcc;
  clang = dedupClang;
  all = dedupGcc ++ dedupClang;
}

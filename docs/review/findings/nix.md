# Nix layer code-quality review (main @ 72dd7be)

Scope: `flake.nix`, `develop.nix`, `lib/*.nix`, `nix/*.nix`, `template_graph/*.nix`, and the repo root (scripts, loose markdown, `docs/`, `.gitignore`). All paths are relative to the repo root.

I checked a few claims with eval-only commands. Nothing was built.

- `builtins.listToAttrs` keeps the first duplicate.
- `builtins.tryEval ({}).a` aborts. tryEval does not catch a missing-attribute error.
- `(linkFarm ...).stdenv` is `stdenv-linux-no-cc`.
- In the pinned nixpkgs (68a8af9), `gcc9..12` and `llvmPackages_12..17` are throwing aliases. Discovery finds gcc13-15 and clang18-22.
- With `lib/mips-clang-overlay.nix` applied, the native `llvmPackages_20` compiler-rt drv changes from `as1n...-compiler-rt-libc-20.1.8.drv` to `3iq5...-compiler-rt-libc-20.1.8.drv`.
- I replicated the axis filters from `lib/flags.nix` and `lib/architectures.nix` for 41 compilers. `allFlagCombos` has 415,734 entries. Per target there are x86_64 391,659, aarch64 150,074, i686 66,252, mipsel 62,856, mips64el 61,668, armv7l-hf 61,516, ppc64 57,976, riscv64 41,816, ppc32 26,596 and armv7l-sf 1,340 variants. That is about 922k variants per package, and `lib/packages.nix` lists 244 packages.

## What is good

- The `dataset.<pkg>.<arch>.<suffix>` nesting is lazy, so one variant or one cell can be evaluated without the rest.
- The axes are data. Constraint fields (`minGccVersion`, `clangOnly`, `archs`) go through one generic filter (`entryAcceptsCompiler` and `entryAcceptsArch` in `lib/matrix.nix:138-157`).
- The comments record real failure modes with the actual error text. Examples are the ld-flag vs driver-flag split (`lib/flags.nix:15-28`), the `NIX_CFLAGS_COMPILE_BEFORE` handling for `-pie` and `-fno-PIC`, and the src pin across the pname rename.
- `lib/elfFolderBuildTools.nix` is one shared list used by both `mkBinaryFolder` and `_buildHostTools`, so the two cannot drift.
- `mkBinaryFolder` collects ELFs from every output and dedupes symlink aliases deterministically.
- Old nixpkgs sets use explicit spec lists (`lib/old-nixpkgs.nix`) rather than discovery.
- The `runnerPkgs` split keeps the images on cache.nixos.org, and the reason is written down.
- The `storeDbRegistration` design is well argued.

## Top 5 issues (ranked)

1. N1/N2: target package sets, old cross package sets and toolchain stdenvs are rebuilt per cell or per variant. Some of these are full nixpkgs re-imports.
2. N7: the corpus shrinks silently. Compilers, packages and toolchains drop out through tryEval or false-returning probes, and nothing reports it.
3. N5: the MIPS compiler-rt overlay applies to every platform. It pushes native and non-MIPS clang off the binary cache and forced the three-pkgs-set workaround.
4. N3/N4: each cell re-filters the 416k-entry combo list. `_meta` is far from "instant". `matrixSize` and the header comments are wrong by three to four orders of magnitude.
5. N15/N16: the toolchains seeded by `_crossToolchainMap` are not the toolchains variants use. Python also reads an incidental `.stdenv` attribute off a linkFarm.

---

## A. Evaluation performance

### N1 (high, perf) Unmemoized nixpkgs re-imports per cell and per variant
- `lib/architectures.nix:245-252`: for `crossSystem` targets (ppc32, armv7l-sf), `getPkgsForTarget` runs `import pkgs.path { crossSystem = ...; }` on every call. Nix does not memoize function applications, so each call builds a new nixpkgs fixpoint.
- It is called once per (pkg, arch) cell (`lib/matrix.nix:387`, `:406`). For the 244 packages that is 488 instantiations per full pass. `mkVariant` also calls it once per variant (`lib/mkVariant.nix:27-34`). A ppc32 cell of one package has about 26.6k variants, so it pays about 26.6k nixpkgs instantiations.
- The same pattern appears for old nixpkgs. Pre-pkgsCross sets re-import with `crossSystem` on every `mkStdenv` call (`lib/old-compilers.nix:412-418` and `lib/old-gcc-cross.nix:247-253`, the gcc5/18.03 path). That runs once per variant for every cross arch.
- Fix: in `matrix.nix`, compute `targetPkgsByArch = lib.mapAttrs (_: archDefs.getPkgsForTarget pkgs) archDefs.targets` once. Pass `targetPkgs` into `mkVariant` instead of letting it re-derive the set. In `processOldNixpkgs`, build `oldCrossPkgsByArch` once per old set and close over it.

### N2 (high, perf/structure) Toolchain stdenv rebuilt per variant, and mkVariant runs twice per entry
- `lib/mkVariant.nix:37` calls `compiler.mkStdenv targetPkgs target` for every variant. On the old-gcc paths this re-runs `overrideAttrs` and `override` on the cross gcc (`lib/old-compilers.nix:519-525`, `lib/old-gcc-cross.nix:159-226`), so every variant re-hashes a fresh toolchain derivation.
- `lib/matrix.nix:338-358` (`mkEntry`) calls `mkVariant` twice with identical arguments, once for `elfFolder` and once for `rawPkg`. Nothing consumes `rawPkg`.
  - Correction from the synthesis pass: Nix evaluates lazily and nothing forces `rawPkg`, so the second call costs nothing at evaluation time. It is dead code (see N30), not a performance cost. The per-variant stdenv rebuild in the first bullet is real.
- `crossToolchainMap` (`lib/matrix.nix:467`) builds a third, independent copy.
- Fix: build one table `toolchains.<arch>.<compilerLabel> = { stdenv; ltoTools; ... }` in `matrix.nix`. Have `mkVariant` take a toolchain record, and define `crossToolchainMap` as `mapAttrs (_: t: t.stdenv)` over the same table. Drop `rawPkg`, or derive it from the same `let`.

### N3 (high, perf) Combo enumeration cost, and size numbers off by orders of magnitude
- `allFlagCombos` (`lib/matrix.nix:243-264`) has about 416k entries. `combosForTarget target` (`:366-375`) re-filters the whole list for every (pkg, arch) cell (`:392`, `:415`), and each element runs `isValidArchCombo`, which does a regex parse and `toInt` (`:92-116`). A full pass is about 2,440 cells times 416k predicates.
- `matrixSize = combosPerArch * numArchs * numPkgs` (`:420-424`) ignores the per-target filters and reports about 1.0e9. The real upper bound is about 922k per package, or about 225M overall. The header (`:1-8`) still says "75K".
- `_meta.<sys>.<pkg>.x86_64` serializes about 392k objects with 11 fields each. Python reads this per arch (`python/compiler_suit_runner/workers/eval_worker.py:517-560`).
- `lib.findFirst (p: p.label == pkgLabel)` inside `genAttrs` over the same labels (`:381`, `:400`) is quadratic. It is cheap at 244 packages, but `listToAttrs` by label is the obvious form.
- Fix: memoize `combosByArch = lib.mapAttrs (_: combosForTarget) archDefs.targets`. Store a parsed `{ major; minor; }` on each compiler entry once. Compute `matrixSize` as the sum over targets. Consider a factorized `_axes.<sys>.<arch>` output (axis lists plus validity per compiler) so the Python sampler does not need the materialized 392k-entry cell.

### N4 (high, perf) `_meta` is not "instant" or derivation-free
- Claimed at `flake.nix:296`, `README.md:43` and `lib/matrix.nix:7-8`, `:396`.
- `compilers.all` needs every old compiler's version, which comes from `tryEval` (`lib/old-compilers.nix:443`, `:570-572`). Any `_meta` or `dataset` access therefore imports all five old nixpkgs plus current nixpkgs.
- `pkgIsBuildableForTarget` (`lib/matrix.nix:233-236`) forces the arch's `pkgsCross` set (or a re-import, see N1) and `meta.available`. `meta.available` forces the package's `mkDerivation` call.
- Fix: put static `version` strings in the `lib/old-nixpkgs.nix` specs, and assert inside `mkStdenv` that they match the real compiler. Keep static validity (axes and versions) separate from the buildability probe, so pure metadata can be computed without any nixpkgs import.

### N5 (high, perf) MIPS compiler-rt overlay applies to every platform
- `lib/mips-clang-overlay.nix:33-80` and `:106-118` patch every `llvmPackages_N` compiler-rt with `postPatch` and `preConfigure`, whatever the host platform. The MIPS check is a build-time `grep -qi mips "$cmakeFlags"` (`:69`), even though the platform is known at eval time.
- I confirmed that the native x86_64 `llvmPackages_20` compiler-rt drv changes under the overlay. Every native and non-MIPS clang toolchain in the matrix is therefore off cache.nixos.org and rebuilds from source.
- This is the cause of the three-pkgs-set split in `flake.nix:81-124`; the comment at `flake.nix:86-95` describes the cascade.
- Fix: `patchCompilerRt = rt: if rt.stdenv.hostPlatform.isMips then rt.overrideAttrs (...) else rt;`. Then re-check whether `pkgs` and `runnerPkgs` can merge. This changes drv hashes, so schedule it with a corpus rebuild.

### N6 (medium, perf) Reading one `_crossToolchainMap` entry forces every toolchain of the arch
- `lib/matrix.nix:441-486` builds each arch's map with `listToAttrs` over `tryEval (deepSeq drvPath)` results. Reading one key needs the whole key set, so every compiler's stdenv drvPath for that arch is forced.
- `flake.nix:344` and `README.md:46` describe the attribute as "single toolchain".
- Fix: `lib.genAttrs (labels that pass isValidArchCombo)` with the value computed per label. A failing combo should throw from its own value instead of reshaping the key set.

## B. Hidden failure and silent fallbacks

### N7 (high, hidden-failure) Compilers, packages and toolchains drop out with no report
- `lib/compilers.nix:21-31` and `:59-69` discover compilers by regex over `attrNames pkgs` plus `tryEval`. Today gcc9-12 and llvm12-17 are throwing aliases that get skipped silently. If a nixpkgs bump removes gcc13, it leaves the corpus with no message.
- `lib/matrix.nix:172-236`: a misspelled or renamed `attr` in `packages.nix` makes `targetPkgs ? attr` false, and a throwing alias makes the probe's `tryEval` fail. Either way every arch gets `{}`. Python only sees empty cells.
- `crossToolchainMap` drops a combo with `builtins.trace` only (`:479-484`). `crossToolchainsMeta` (`:504-511`) and `nestedMatrix` still list it, so validity has three different answers.
- Fix: keep an explicit expected-current-compiler list and assert discovery matches it. Add an `_audit.<sys>` output listing every dropped (pkg, arch, compiler) with a reason. Fail evaluation when a package is unbuildable on native x86_64, which should never be a platform issue.

### N8 (medium, hidden-failure) Constraint fields are never validated
- `or` defaults are everywhere: `lib/matrix.nix:96-112` (`maxGccVersion or`, `brokenGccVersions or []`), `:144-149` (`minGccVersion or`, `clangOnly or false`) and `:155` (`archs or null`).
- A typo such as `minClangversion`, `arch` or `brokenGCCVersions` silently turns the constraint off. An `archs` label that is not a target (`"x86-64"`) silently excludes the entry everywhere.
- `lib/flags.nix:9` documents `gccOnly`, but the matrix never reads it. Setting it does nothing.
- Fix: write a `validateEntry` that checks keys against an allowed set and checks that `archs` is a subset of `attrNames targets`. Run it on all axes and targets, and expose it as a flake `check`.

### N9 (medium, hidden-failure/docs) Wrong model of what tryEval catches
- `lib/matrix.nix:445-455` claims `deepSeq` turns "nixpkgs-18.03 pkgsCross for ppc32 missing `platform.kernelArch`" into a throw that tryEval catches. tryEval does not catch missing-attribute errors; I verified this.
- `flake.nix:345-351` says the opposite, which is the correct version.
- The real protection is the static `brokenGccVersions`/`brokenClangVersions` lists in `lib/architectures.nix:151-177` and the ppc32 `crossSystem` override.
- Fix: correct the comment and keep one description of the policy: static lists decide validity, and anything else fails loudly.

### N10 (medium, hidden-failure) `or null` in mkVariant produces mislabeled variants
- `lib/mkVariant.nix:234-235`: `targetPkgs.glibc.static or null`. If the attribute is missing, `staticpie` variants build without `libc.a`. `lib/flags.nix:209-212` says configure then collapses.
- `lib/compilers.nix:97-99` and `lib/old-compilers.nix:587`: `.llvm or null` makes `ltoTools` null (`lib/mkVariant.nix:207-217`). LTO variants then fall back to plain `ar` with no warning.
- `compiler.version or "0"` (`lib/mkVariant.nix:52`, `:185`) hides a missing field that every entry is required to have.
- Fix: throw when a flag set's requirement (`needsStaticLibc`, `needsLtoTools`) cannot be met, or exclude the combo at matrix time with an audit entry.

### N11 (medium, hidden-failure) Image silently drops harmonia and cachix
- `flake.nix:218` (`runnerPkgs.harmonia or null`), `nix/docker-image.nix:43` and `:107-108`.
- The flake comment at `flake.nix:183-188` describes a wrapper that "fails at attribute access time only". No such wrapper exists.
- A nixpkgs bump that renames harmonia would ship a production image with no peer cache, and nothing would say so.
- Fix: make harmonia required, with an explicit `withHarmonia ? true` opt-out.

### N12 (low, hidden-failure) Patch failures swallowed in the 18.03 gcc path
- `lib/old-gcc-cross.nix:274-283` runs the `sed` edits with `2>/dev/null || true`.
- The comment says "Add gnu_inline to the lookup function declaration". The code deletes `__inline` lines and `gnu_inline` attribute lines.
- Fix: use `substituteInPlace --replace-fail` or real patch files so a no-op edit fails the build. Make the comment describe what the edit does.

## C. Correctness-adjacent design

### N13 (medium, correctness) env vs top-level placement decided from one variable
- `lib/mkVariant.nix:296-346` sets `inEnv` from `old.env ? NIX_CFLAGS_COMPILE` alone and then places all five variables (`NIX_LDFLAGS`, `NIX_CFLAGS_LINK`, `NIX_CFLAGS_COMPILE_BEFORE`, ...) at that level.
- Take a package with `NIX_CFLAGS_COMPILE` in `env` and `NIX_LDFLAGS` at top level, or the reverse. The merge reads the empty location and drops the package's own value, or mkDerivation's env/derivation-argument overlap assertion fails evaluation.
- Fix: decide placement per variable, with `if old.env ? X then env else top-level`.

### N14 (medium, design) The `-variant` pname rename forces two workarounds
- `lib/mkVariant.nix:366` renames pname. That forces the src pin (`:356-364`) and the suffix strip in `allowInsecurePredicate` (`flake.nix:55-73`).
- Other `finalAttrs.pname`-derived attributes (doc dirs, `meta.mainProgram`, pname-based URLs in patches) still shift.
- The rename adds no information. Every variant of a package gets the same `<pname>-variant`, and the elf-folder derivation already carries the full `variantLabel`.
- Fix: drop the rename. The metadata lives in `passthru.datasetMeta` and `meta.json` anyway. This changes hashes, so do it with a rebuild.

### N15 (medium, correctness/perf) Seeded toolchains are not the toolchains variants use
- `lib/mkVariant.nix:39-82` patches the ppc64 clang wrapper. LTO tools (`:190-217`) and `glibc.static` (`:234`) are also added per variant. `_crossToolchainMap` seeds only `comp.mkStdenv` (`lib/matrix.nix:466-469`). The substituter-free toolchain archive therefore ships the unpatched ppc64 wrapper, and every node has to build the real one.
- The ppc64 fix also applies `-mabi=elfv2` and `-fuse-ld` to every clang, 18-22 included, although the comment says "<18" (`:44-45`). The code gates on `clangMajor < 4` while the comment says "clang <3.9" (`:61-62` vs `:75`).
- Fix: move per-target wrapper fixes into the compiler entries' `mkStdenv`, or into a target hook applied in the toolchain table from N2, so seeding and variants share one value.

### N16 (medium, interface) Python reads an incidental `.stdenv`; the real export is unused
- `python/compiler_suit_runner/preflight.py:1605` evaluates `crossToolchains.<sys>.<arch>.stdenv`. That is the linkFarm's own runCommand stdenv, `stdenv-linux-no-cc` (verified). It is not the cc-bearing native stdenv the docstring describes (`:1580-1590`).
- `_baselineStdenv = pkgs.stdenv` is exported at `flake.nix:355-359` and `:394`, and no Python code uses it.
- Fix: export one explicit `_nativeBuildStdenv.<sys>` and point preflight at it. Delete whichever export stays unused.

### N17 (medium, correctness/docs) `dedupByLabel` keeps the opposite entry from what the comments say
- `lib/matrix.nix:23-40` says "Current compilers take priority". It concatenates `extra ++ current` and `listToAttrs` keeps the first entry, so old compilers win.
- `lib/old-compilers.nix:743-756` reverses the list and then keeps the first, so the last entry wins. The comment says the first wins, and its example ("gcc48 from both 18.03 and 22.11") is false because 18.03 only lists gcc5.
- No labels collide with the pinned nixpkgs today. A bump where unstable still ships gcc12 or llvm17 would silently pick the 22.11 or 24.05 build.
- `_debug.compilers` (`flake.nix:328-329`) does not dedup, so it can disagree with the matrix.
- Fix: one helper with explicit priority that throws on unexpected collisions.

## D. Separation of concerns and duplication

### N18 (medium, duplication) The variant metadata schema is defined three times
- `lib/matrix.nix:266-323` (`mkSuffix`, `mkMeta`, `variantLabel`), `lib/mkVariant.nix:237-247` (`variantLabel`) and `:374-389` (`datasetMeta`), and the passthru in `lib/mkBinaryFolder.nix:40-42`.
- Adding an axis means editing all three. If one is missed, `_meta` and `meta.json` disagree.
- Fix: the matrix computes `meta` and `variantLabel` once and passes them to `mkVariant`, which only attaches them.

### N19 (medium, separation) mkVariant mixes generic flag plumbing with per-package and per-target fixes
- The `dash` and `brotli` shims are keyed on `pkg.attr` (`lib/mkVariant.nix:252-281`). The ppc64 clang wrapper fix is keyed on `target.label` (`:39-82`).
- Each new package fix grows the generic builder.
- Fix: add an optional `fixup = { compiler, target }: old: { ... }` field in `lib/packages.nix`, or a `lib/package-fixups.nix` keyed by attr. Move target-specific wrapper fixes into the compiler layer (see N15).

### N20 (medium, duplication) Version parsing exists seven times with different fallbacks
- `lib/matrix.nix:48-62`, `lib/mkVariant.nix:50-54` and `:185-188`, `lib/old-compilers.nix:307-312`, `:331-337` and `:351-354`, and `lib/old-gcc-cross.nix:27-33`.
- The fallbacks differ (`0.0`, `"0"`, `"unknown"`).
- Fix: one `lib/version.nix` built on `lib.versions.major`/`minor`, or a parsed `{ major; minor; }` stored on each compiler entry (see N3).

### N21 (medium, duplication) Compiler capability facts are encoded twice
- The flags old compilers do not support live in `lib/old-gcc-cross.nix:27-46` and `lib/old-compilers.nix:307-327`.
- The same facts appear as axis minimums in `lib/flags.nix:262-275` (ssp gcc4.9/clang3.5), `:405-421` (stackclash gcc8/clang11) and `:422-437` (zerocallregs gcc11/clang16). `docs/*-flag-support-matrix.md` is a third copy.
- Fix: one capability table (`flag -> { gcc; clang; }` minimums) feeding both the axis constraints and `hardeningUnsupportedFlags`.

### N22 (medium, duplication) The cross predicate and `noMin` are copy-pasted
- `target.crossAttr != null || target ? crossSystem` appears at `lib/compilers.nix:52`, `:86` and `:96`, `lib/old-compilers.nix:458` and `:590`, and `lib/architectures.nix:232`.
- `noMin` is defined three times: `lib/architectures.nix:14`, `lib/flags.nix:33` (unused) and `lib/matrix.nix:133`.
- `armv7l-sf` excludes clang through `minClangVersion = 999.0` (`lib/architectures.nix:77-80`).
- Fix: put an `isCross` field on each target, and a `families = [ "gcc" ]` (or `clang = false`) field instead of the 999 trick. Keep shared constants in one util file.

### N23 (medium, complexity) old-compilers.nix picks its strategy by probing attributes
- `mkOldGccEntry` (`lib/old-compilers.nix:457-551`) branches on `oldPkgs ? pkgsCross`, `nixpkgsSrc != null` and `gccSubdir != null`. `mkOldClangEntry` (`:588-715`) has another three-way split. The file is 766 lines, mostly comments explaining which era each branch is for.
- `extractClangCC` and `clangHasRealWrapper` (`:238-279`) duplicate the same probe.
- Fix: give each spec in `lib/old-nixpkgs.nix` an explicit `strategy` field (`"pkgsCross"`, `"reimport-1803"`, `"old-expr-1509"`). Dispatch on it, with one file per strategy, and have a single probe return `{ cc; hasRealWrapper; }`.

### N24 (medium, duplication) The two image files duplicate about 200 lines and have drifted
- `nix/docker-image.nix:115-273` and `:345-352` against `nix/ssh-debug-image.nix:58-255` and `:300-307` cover the same things: nixbld users, NSS files, host keys, authorized keys, nix.conf, registration and layer units.
- The ssh-debug header calls the image a "SUPERSET" (`:2`). It lacks the flake source, `nix-eval-jobs`, `strace`, `NIX_PATH`, `CSR_FLAKE_DIR` and `narinfo-cache-negative-ttl = 0` (`nix/docker-image.nix:257-264`).
- Fix: add `nix/image-common.nix` that returns these parts. Each image composes it with its own extras.

### N25 (low, coupling) develop.nix reaches into dynamic-runner's private file layout
- `develop.nix:13-24` calls `${dynamicRunnerSrc}/nix/shutdown-manager-bin.nix`, `wrapper-bin.nix` and `wheel.nix` and "mirrors the framework overlay's wiring". An upstream rename breaks the dev shell.
- Fix: ask upstream to export a builder function (for example `lib.mkDynamicRunner { pkgs, python3Packages }`) and call that.

## E. Flake interface and image inputs

### N26 (medium, perf/hygiene) Image flake source uses a basename denylist
- `flake.nix:204-217` excludes `docs`, `dataset`, `result`, etc. at any depth.
- A git flake's `./.` already contains only tracked files, so most of those names are absent anyway. Tracked junk still goes in: `convo.md` (460 KB), `plan_dist_eval.md`, `test_*.sh`, `README.md`. Editing any of them rebuilds the image and changes `CSR_FLAKE_DIR`.
- `python/` ships twice, in `projectFiles` and in `flakeFiles`.
- Fix: switch to an allowlist with `lib.fileset.toSource`, covering `flake.nix`, `flake.lock`, `lib`, `nix`, `template_graph` and `python` only if the eval path needs it.

### N27 (medium, dead code) The legacy manifest and tarball path is still exported
- `apps.generate-manifest` (`flake.nix:238-269`) uses a relative `.#`. Its no-argument mode would serialize about 225M entries.
- `lib/manifest.nix` is not imported anywhere. Its script calls `entry.get('drvPath')` on `_drvPaths` leaves, which are plain strings, and it stores exceptions in `depsError` instead of failing.
- `lib/mkBinaryTarball.nix` is not imported either. It still has the default-output-only bug that `mkBinaryFolder` fixed (`lib/mkBinaryFolder.nix:26-34`) and keeps its own tool list.
- Fix: delete all three. `python/compiler_suit_runner/manifest_gen.py` is the live path.

### N28 (low, naming/structure) Output plumbing and namespaces
- 14 hand-written `mapAttrs (_: s: s.X) perSystem` lines (`flake.nix:382-415`).
- The underscore outputs `_meta`, `_drvPaths`, `_crossToolchainMap`, `_buildHostTools` and `_imageStoreRegistration` are the runner's public API, not private.
- `__test_broken__` is a test fixture inside `_drvPaths` (`flake.nix:308-321`). It is a derivation in a namespace of drvPath strings.
- `crossToolchains` includes the native x86_64 entry.
- Fix: generate the per-system transposition. Group the runner API under one attribute (for example `legacyPackages.<sys>.runner.{meta,drvPaths,toolchains,...}`). Move the fixture to `_testFixtures` or `checks`.

### N29 (low, docs) Stale and contradictory comments
- `flake.nix:173-175`: the example suffix `gcc15-O2-baseline-unhardened` no longer exists.
- `flake.nix:181`: an orphan "Manifest generation app" header.
- `flake.nix:184`: cites `plans/splendid-snacking-cascade.md`, which is not in the repo.
- `lib/architectures.nix:2` says every target is verified with GCC and Clang, which is false for armv7l-sf.
- `lib/architectures.nix:46-49` blames clang3.5/aarch64 on an "autoconf divergence". `docs/known-issues.md` describes a register-allocator segfault.
- `lib/old-nixpkgs.nix:45` and `:81`, `flake.nix:8-9` and `lib/old-gcc-cross.nix:10-14` disagree on which nixpkgs provides gcc4.5. The spec lists it in 15.09.
- `lib/flags.nix:440-444` says sanitizers are native-only. The `archs` lists include aarch64, and `san-undefined` has no restriction.
- `lib/old-gcc-cross.nix:171-182` repeats the same limits.h comment twice and mentions a stdlib.h include that is not done.
- `template_graph/sum_drv_from_aggregates.nix:13-15` cites `_toolchainsWrapper`/`_matrixWrapper` flake outputs that do not exist.

### N30 (low, dead code) Smaller dead items
- `lib/flags.nix:33-36` (`noMin`).
- `lib/flags.nix:4-5` and `lib/mkVariant.nix:84-95`: the null-`cflags` `gccFlag`/`clangFlag` path. No entry uses it.
- `lib/mkVariant.nix:15-23`: argument defaults that duplicate `flags.nix` entries.
- `lib/matrix.nix:348-358` (`rawPkg`) and `:524-526` (`getPackage`, `getPackageArch`).
- `lib/packages.nix:432-442` (`tier1-3`, `highOnly`, `medOnly`, `lowOnly`).
- `lib/old-compilers.nix:397`: an unused argument.
- `lib/old-gcc-cross.nix:103` and `:214`: the same version computed twice.
- `flake.nix:327`: a second import of `compilers.nix` for `_debug`.
- `nix/ssh-debug-image.nix:402-405`: `RUST_LOG=trace`, marked "TEMPORARY" and baked into the image.

### N31 (low, purity/duplication) template_graph wrapper derivations
- The same `derivation { builder = bash; args = ["-c" "true"]; refs = toString ...; }` is written three times: `template_graph/sum_drv.nix:44-53` (deprecated, still used at `template_graph/make_sum_drv.py:340`), `template_graph/wrapper_drv.nix` and `template_graph/sum_drv_from_aggregates.nix`.
- `wrapper_drv.nix:7` defaults to `import <nixpkgs> {}`. That is impure and evaluates a full nixpkgs per call just to get bash. The wrapper drv hash also depends on `NIX_PATH`, so it differs between a dev machine and the container.
- Fix: pass `bash` in, as `sum_drv_from_aggregates.nix` does. Remove `sum_drv.nix` together with its legacy caller.

### N32 (low, security hygiene) Image credentials
- `nix/docker-image.nix:115-120` and `nix/ssh-debug-image.nix:60-65` generate sshd host private keys into `/nix/store`. They are world-readable on the builder, present in every image copy and servable by the harmonia peer cache. The output is also non-reproducible.
- Root has an empty password (`nix/docker-image.nix:207`, `nix/ssh-debug-image.nix:133`).
- `nix/ssh-debug-image.nix:165-168` justifies `PermitRootLogin yes` by saying root is locked (`!`). The shadow file sets `root::`.
- Fix: generate host keys at container start. The runtime already copies them to `/tmp/ssh-debug`. Fix the comment.

## F. Repo root

### N33 (medium, stale tooling) Root scripts build an attribute that no longer exists
- `test_compilers.sh:26`, `test_cross.sh:98` and `test_skipped.sh:81` build `...<compiler>-O2-baseline-unhardened`. The current suffix is `-O2-baseline-none-san-off-march-default`. Every run records each compiler as "fail" because of a missing attribute.
- The compiler and arch lists are hard-coded four times (`test_compilers.sh:10-19`, `test_cross.sh:49-58`, `gen_table.sh:8-17`, `test_skipped.sh`).
- `gen_table.sh:18-31` re-implements the min/max tables from `lib/architectures.nix` but not the broken lists.
- `lib/architectures.nix:104`, `:144-146` and `:191` justify the broken lists by citing `table.md`, which these scripts generate.
- Fix: delete the scripts or move them to `scripts/legacy/`. If they are kept, derive compilers, archs and suffixes from `_crossToolchainsMeta` or `_meta`.

### N34 (medium, repo organization) Root markdown, docs/ and .gitignore
- Agent transcripts and prompts are committed at the root: `convo.md` (460 KB, 212 absolute `/home/...` paths), `convo_req.md` and `bootstrapping_human.md`.
- `goal.md` is the original prompt and still describes tarball output, which `mkBinaryFolder` replaced. `plan_dist_eval.md` is a 69 KB plan.
- Correction from the sol-loop: `table.md` is a runtime input, not only a snapshot. The eval worker's support filter reads it (`python/compiler_suit_runner/support_table.py`), and the multi-version branch hashes it into the evaluation identity. It must stay until that data moves into Nix.
- `table.md`, `failures.md`, `failures_detail.md` and `report_hello*.md` are generated snapshots. Their inputs (`.cross-results/`, `test_results.jsonl`) are gitignored, so the snapshots cannot be regenerated from the repo, and they disagree with each other (`failures.md` says "40 compilers x 8 targets", `table.md` has 9 cross targets).
- `support_matrix.md` is cited as the source of the minimum versions (`lib/architectures.nix:4`, `lib/matrix.nix:47`), but its numbers do not match the code (it lists "x86/x64 GCC 1.x" and "ARM64 Clang 3.4").
- `slurm.md` hard-codes a personal cluster account and an absolute home path.
- `docs/` mixes durable design notes (`franken-toolchain-anatomy.md`, the flag matrices, `known-issues.md`) with one-off plans and findings (`dynrunner_refactor_findings_2f30920.md`, `slurm_test_plan.md`). The runbooks sit in a separate root `dynrunner/` directory, and there is no index.
- `git ls-files -ci --exclude-standard` returns nothing, so no tracked file is gitignored.
- `.gitignore` does not list `dataset/` or `.docker-layer-cache.json`, which the image filter expects to exist (`flake.nix:210-211`).
- `.mcp.json` is tracked and needs `mcp-language-server` and the bash and JSON language servers. `develop.nix:42-45` has those commented out.
- Fix: delete the transcripts and prompts. Reorganize `docs/` as `design/`, `compat/`, `ops/` and `archive/` (move `dynrunner/*.md` into `ops/`). Move the snapshots to dated `docs/archive/` files or drop them. Fix the README links (`README.md:117`). Restore the dev tools or untrack `.mcp.json`.

---

## Suggested refactor order

Before steps 2-5, snapshot `_drvPaths` for a handful of cells (for example hello across all archs, plus one old-gcc and one ppc32 cell). Those steps should leave drv hashes unchanged, and diffing against the snapshot proves it.

1. Cleanup with no behavior change, in any order: delete dead code (N27, N30), handle the root clutter and stale scripts (N33, N34), and fix comments (N9, N29). This step has no dependencies and makes the rest easier to review.
2. Shared helpers: add `lib/util.nix` with version parsing, `isCross`, the shared constants and a collision-checking `dedupByLabel` (N20, N22, N17). Hash-neutral.
3. Validation and audit: add the axis and target schema check, the expected-compiler assert and the `_audit` output (N8, N7). Depends on step 2. It turns today's silent drops into visible ones before the larger changes.
4. Memoization and the toolchain table: `targetPkgsByArch`, old cross package sets per arch, and a `toolchains.<arch>.<compiler>` table used by both `mkVariant` and `crossToolchainMap`. Build `_crossToolchainMap` lazily per key (N1, N2, N6). Depends on step 2. Hash-neutral if done carefully.
5. Eval cost: per-arch combo memoization, pre-parsed versions, static old-compiler versions and a correct `matrixSize` (N3, N4). Depends on steps 2 and 4. The factorized `_axes` output needs a matching Python change, so do that part second.
6. mkVariant cleanup: take `meta` and `variantLabel` from the matrix (N18), move package fixups into data (N19), decide env placement per variable (N13), and add hard errors for unmet requirements (N10). Depends on step 4, which changes the `mkVariant` signature. N13 can change hashes for affected packages.
7. Hash-changing work, batched into one corpus rebuild: gate the MIPS overlay (N5), drop the pname rename (N14), and move the ppc64 and LTO fixes into the toolchain layer (N15). Depends on step 4. After N5, re-test whether `pkgs` and `runnerPkgs` can merge.
8. Images, independent of everything else: `nix/image-common.nix` (N24), the fileset allowlist (N26), required harmonia (N11), runtime host keys (N32) and the template_graph wrappers (N31).
9. Interface, coordinated with the Python side: explicit `_nativeBuildStdenv` (N16), grouped runner outputs and the moved test fixture (N28), and the upstream dynamic-runner builder (N25).

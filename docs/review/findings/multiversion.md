# Code-quality review: feat/multiversion-package-support

Scope: `git diff 72dd7be` on branch head `56219bc` (4 commits, 52 files). Line numbers refer to the branch head. Paths are relative to the repo root.

Checks run (read-only): the focused pytest suites (`test_package_versions`, `test_variant_inventory`, `test_preflight`, `test_cli`, `test_incremental_cache`, `test_eval_worker`) all pass. A few `nix eval` probes against the pinned nixpkgs confirmed compiler label sets and `src` presence. One Python probe reproduced M11.

## What is good

- `lib/package-versions.nix:37-48` validates the catalog strictly (duplicate IDs and families, reserved `--`/`__`, exactly one default per family) without evaluating any package. `tests/package-versions.nix:25` proves the catalog stays lazy with a poisoned `attr`.
- One resolver (`lib/resolve-package.nix`) now feeds availability, metadata and builds. It replaces the two older gates in `lib/matrix.nix` (`pkgAcceptsStdenv`, `pkgSupportsPlatform`) and returns a named reason for each skipped cell.
- `resolvedPackages` (`lib/matrix.nix:157-160`) binds each (package, target) resolution once. `groupsForTarget` (`lib/matrix.nix:164-181`) filters each axis before taking the product. Counts are computed arithmetically. These are real structural improvements.
- Task IDs come from one helper (`build_variant_task_id`, `python/compiler_suit_runner/package_selection.py:138-143`) used by both the manifest and streamed descriptor paths, and the ID now includes the architecture.
- Embedding `datasetMetadata` in the elf-folder derivation env (`lib/mkBinaryFolder.nix:47`) makes provenance readable from a `.drv` without re-evaluating the flake. The `unsafeDiscardStringContext` on `packageDrv` has a comment explaining why.
- Completion treats legacy, partial and stale records as misses instead of inventing provenance.
- `_discard_subtree` (`template_graph/streaming/state.py:285-303`) fixes exponential traversal of shared DAGs, and `test_discard.py` guards it.
- The selector unit tests and parser round-trip tests with axis-like package IDs (`python/compiler_suit_runner/tests/test_package_versions.py:94-103`) are short and test behavior.

## Top 5 issues

1. M14. The published variant directory name includes the derivation hash, so every drv change adds a new directory and nothing removes the old one. Duplicate variants pile up in the dataset.
2. M5 and M8. There are two complete matrix-eval paths. The new one sends suffix strings that Nix parses back by label prefix, and the compiler lookup is a regex special case.
3. M10 and M11. Every submit now resolves the full catalog without caching, while the variant-enumeration cache caches a free Python step. Its validation also rejects valid records.
4. M1. Identity names disagree across layers: `recipeId` is a record, `releaseId` is a recipe hash, `label` and `package` change meaning by layer, and one token has four names.
5. M19 and M20. Selector rules are written in four places, and part of the selection logic is Nix source assembled in Python f-strings.

## A. Identity model and naming

### M1 (high, naming) Identity terms are inconsistent between Nix and Python
- Where:
  - `lib/package-versions.nix:8-24`. `recipeId` is an attrset (provider, attr, revision, narHash, policy, adapterRevision), not an ID. `releaseId` (line 19) is `family@sha256(recipeId)`, a recipe digest that involves neither version nor source.
  - The real release identity is `packageIdentity` (`lib/resolve-package.nix:112-120`). It hashes `recipeId` twice, once directly and once through `releaseId`.
  - `label` means packageId in the Nix descriptor (`lib/package-versions.nix:26`) but the full variant label in Python specs (`python/compiler_suit_runner/manifest_gen.py:541`, `python/compiler_suit_runner/partition.py`).
  - `package` is the packageId in `lib/matrix.nix:237` and `lib/mkVariant.nix:380`, the family in `python/compiler_suit_runner/variant_inventory.py:133`, and is rewritten to the family in `python/compiler_suit_runner/preflight.py:798`.
  - The package key is called `binary` (eval worker, `archive.py` `wanted_binaries`), `pkg` (payloads, `publication.py:35`), `packageId` and `package_id`. `--packages` and `VariantAxes.packages` mean families.
  - One evaluation token is called `evaluationIdentity` (`flake.nix:153`), `identity` (`lib/matrix.nix:320`), `variantInventoryIdentity` (`lib/matrix.nix:480`) and `variant_inventory_identity` (`python/compiler_suit_runner/preflight.py:999`). The value returned by `catalog_token()` is stored as `_package_catalog_token` and passed in as `catalog_identity` (`python/compiler_suit_runner/cli.py:856`, `:1443`).
  - camelCase release fields sit next to snake_case compiler fields in the same dicts: `packageFamily` and `compiler_family` in `VariantSpec` (`python/compiler_suit_runner/partition.py:48-59`), and `matrixEvaluationIdentity` next to `matrix_aggregate_drv` in one sidecar (`python/compiler_suit_runner/workers/dependency_graph_worker/archive.py:128-134`).
- Why: A reader has to know which layer produced a dict before `label` or `package` means anything, and grepping a key finds unrelated uses. These names are about to be written into persistent sidecars and completion records, which makes later renames migrations.
- Fix: Write a short glossary and rename before merge: `recipe` (the record), `recipeDigest` (was `releaseId`), `releaseIdentity` (was `packageIdentity`), `packageId` for the key everywhere, and `packageFamily`, never `package`. Choose one wire casing (camelCase, since Nix emits it) and convert once at the Python boundary (see M3).

### M2 (medium, design) Non-default package IDs change whenever the provider pin moves
- Where: `lib/package-versions.nix:20-21` builds `hello--v2_12_1--r<12 hex of sha256(recipeId)>`, and `recipeId` contains `revision` and `narHash` (lines 11-12). The same hash is computed twice (lines 19 and 21). The version slug maps `.`, `+` and `-` all to `_`.
- Why: Bumping `nixpkgs-24_05` renames the matrix key, the publication directory and every task ID, and breaks any `--package-ids` value a user saved. The content digest already lives in `packageIdentity`, so the key does not need it.
- Fix: Make the non-default key an explicit curated field (for example `id = "hello-2_12_1"`), checked for uniqueness by the existing validation. Bind the recipe digest once in a `let`.

### M3 (medium, maintainability) Release records travel as untyped dicts, sliced and revalidated at 22 call sites
- Where: `release_metadata(...)`/`validate_release(...)` in `python/compiler_suit_runner/incremental_cache.py:444`, `package_selection.py:131,132,148`, `suit_task.py:1540`, `cli.py:1542,1614`, `publication.py:31,32,72,74`, `manifest_gen.py:358,557`, `workers/eval_worker.py:331,1109`, `preflight.py:933,990`, `workers/dependency_graph_worker/archive.py:131,165,166,715`, `dependency_graph_planner/descriptors.py:308` (all under `python/compiler_suit_runner/`). Field tuples are written by hand more than once:
  - `("packageCatalogSchema", "packageId", "packageFamily", "packageVersion", "recipeId", "releaseId")` at `workers/eval_worker.py:979` and `:1110`.
  - `("packageDrv", "packageOutputs", "outputPaths", "compilerCapture")` at `manifest_gen.py:559` and `descriptors.py:310`. `package_selection.py:159` has a third copy that leaves out `outputPaths`.
  - `("variant_inventory_schema", "variant_inventory_identity")` at `cli.py:1615`, `suit_task.py:1542`, `manifest_gen.py:359` and `preflight.py:997`.
- Why: A new field means finding every tuple, and the copies already differ.
- Fix: Add a frozen `Release` dataclass with `from_nix(mapping)`, which validates once, and `to_payload()`. Add a small `InventoryBinding` for the schema/identity pair. Pass these objects around instead of dict slices.

### M4 (medium, duplication) Schema constants and safe-ID rules are defined separately in Nix and Python
- Where:
  - Catalog schema `1` appears at `lib/package-versions.nix:50`, at `lib/resolve-package.nix:64` (hard-coded instead of reading `catalog.schema`) and at `python/compiler_suit_runner/package_selection.py:9`.
  - Inventory schema `1` appears at `lib/matrix.nix:319` and `:479` and at `python/compiler_suit_runner/variant_inventory.py:16`. Literal `== 1` checks appear at `python/compiler_suit_runner/package_selection.py:95`, `workers/eval_worker.py:950,1162`, `manifest_gen.py:359` and `preflight.py:997`.
  - The ID alphabet is defined at `lib/package-versions.nix:5`, `python/compiler_suit_runner/package_selection.py:14` and `python/compiler_suit_runner/variant_inventory.py:19`.
- Fix: Use one named constant per language. Let the Nix catalog own ID validation, and let Python check the alphabet in one helper, only where an ID reaches a path or shell boundary.

## B. Formatted names parsed back into identities

### M5 (high, design/perf) The fast path sends suffix strings that Nix parses back by label prefix
- Where: `python/compiler_suit_runner/variant_inventory.py:135-138` formats `compiler-opt-flags-hardening-sanitizer-march`, a second copy of `mkSuffix` (`lib/matrix.nix:191-207`). `lib/matrix.nix:279-304` (`consume`/`comboForSuffix`) parses the string back by longest label prefix. Line 286 splits off the compiler at the first `-`, which assumes compiler labels never contain a hyphen. Lines 296-299 repeat the validity filters that `groupsForTarget` (`lib/matrix.nix:164-181`) already applies.
- Why: Correctness depends on accidents of the label set. `ssp`/`ssp-all` and `relro`/`relro-bindnow` happen to parse today, but a new hyphenated label can split wrongly or throw "unknown variant". Three implementations must stay in agreement (Nix format, Python format, Nix parse), plus the `tree_walker` regex.
- Fix: Pass structured selections, for example `_selectDataset.<sys>.<id> { x86_64 = [ { compiler = "gcc15"; optimization = "O2"; flags = "baseline"; ... } ]; }`, and look each axis up in a label-indexed attrset (`flagSetsByLabel.${flags}`). Nix builds the suffix exactly once, with `mkSuffix`. Python samples tuples from the inventory groups and never formats suffixes.

### M6 (medium, fragile parsing) The planner still recovers the package ID from derivation names
- Where: `template_graph/tree_walker.py:146-193`. `_VARIANT_RE` changed from lazy to greedy at line 149, and the arch is matched by suffix. It is now called from `python/compiler_suit_runner/workers/dependency_graph_worker/slice.py:52-56`, `template_graph/cowalk/template_histogram.py:138-142` and `template_graph/cowalk/_role_merge.py:90-96`, where the import sits inside the per-template loop. `python/compiler_suit_runner/workers/dependency_graph_worker/archive.py:757-761` then checks the parsed ID against `datasetMetadata`. `template_graph/parser/role.py:213` special-cases `-elf-folder.drv` to keep `--` intact.
- Why: The plan says not to reparse names. Every new elf-folder `.drv` now carries authoritative metadata, and each `matrix-<id>` aggregate already belongs to one package. Each parse site catches the error and returns `"<unknown>"`, which hides any change in name shape.
- Fix: Build a `drv -> packageId` map once in `archive.py` (from the aggregate or from `datasetMetadata`) and pass it to the planner. Keep `parse_variant_path` behind one function for legacy archives only.

### M7 (low, parsing) Other string-shape checks
- Where: `python/compiler_suit_runner/workers/eval_worker.py:323` detects the namespace with `attr.startswith("dataset.")`. `lib/compilers.nix:106-118` maps labels to attrs with the regexes `gcc([0-9]+)` and `clang([0-9]+)`. `archive.py` `_variant_spec_from_parsed` still derives the compiler family and version by stripping digits whenever `datasetMetadata` is missing.
- Fix: Carry the namespace as an explicit payload field. See M9 for compiler lookup and M16 for legacy archives.

## C. Evaluation performance

### M8 (high, duplication) Two complete matrix-eval paths are maintained
- Where: `python/compiler_suit_runner/workers/eval_worker.py:950-958` is the inventory plus `_selectDataset` path, and `:961-1000` is the legacy `_meta` plus flat `dataset` path. `:1162-1169` chooses between them on `variant_inventory_schema == 1`.
  - Sampling exists twice: `sample_suffix_attrs` (`eval_worker.py:186-230`) and `sample_inventory` (`python/compiler_suit_runner/variant_inventory.py:122-150`). Both hard-code the seed format `f"{seed}:{compiler}:{arch}:{opt}"` (`:226` and `:147`). The `sample_suffix_attrs` docstring still says every peer imports that one function.
  - Release validation differs between the paths: legacy compares `_meta` per suffix (`:979-982`), the direct path runs `nix derivation show` (`:1095-1115`).
  - `python/README.md` says the legacy path serves "older flakes", but the runner image is built from this same flake, and preflight always emits schema 1 (`preflight.py:997`).
- Why: Every change to filters, validation or capture namespaces has to be made twice, and only parity tests keep the two paths in line.
- Fix: After M5, delete the legacy branch from the pipeline and keep `_meta` as a debug output. Until then, share one `_sample_group(candidates, seed, compiler, arch, opt, size)`.

### M9 (medium, perf special case) The compiler fast path is a regex special case, and historical compilers still force full discovery
- Where: `lib/compilers.nix:104-118` (`getByLabel`) repeats the `gccAvailable`/`llvmAvailable` probes (`:25-30`, `:63-70`) for modern labels only. `lib/matrix.nix:286-290` falls back to `consume "compiler" compilers.all`, which forces every old nixpkgs import in `lib/old-compilers.nix`. `dedupByLabel` (`lib/matrix.nix:27-42`) keeps the first duplicate, so extra (old) compilers win, while its comment says current ones do. The fast path always picks the current compiler. Today the label sets do not overlap (current gcc13-15 and clang18-22, old up to gcc12 and clang17, checked with `nix eval`), so both paths agree only because of the current pins.
- Why: Old-compiler variants, a large part of the matrix, pay full discovery on each selection. A future pin with overlapping labels would make `_selectDataset` and `dataset` disagree without any error.
- Fix: Build `compilersByLabel` as an attrset whose names come from cheap specs (filtered `attrNames pkgs` plus the old-nixpkgs spec labels) and whose values are lazy entries that check availability on access. Use it both in `groupsForTarget` and in selection. Fix the precedence comment or the order.

### M10 (high, perf/resume) Every submit resolves the full catalog, and the enumeration cache now saves nothing
- Where: `python/compiler_suit_runner/cli.py:1432-1455` always calls `resolve_package_selection`, which runs up to three `nix eval`s. Without `--packages`, the details projection resolves all 244 families on all 10 targets, which forces every cross package set and every `src`. After that, `enumerate_variants` with `selected_catalog` (`python/compiler_suit_runner/preflight.py:970-1001`) only reshapes data in Python. Its output is still cached under `VariantAxes` (`cli.py:1537-1578`), with new key fields (`incremental_cache.py:146-151`), `ENTRY_VERSION = 3`, payload validation (`incremental_cache.py:440-450`) and a second consistency check against the catalog (`cli.py:1540-1547`).
- Why: Before this branch, a cache hit skipped Nix entirely. Now the expensive step is never cached and the cached step costs nothing. The cache code still has to be maintained and has a bug (M11).
- Fix: Before merge, either cache the resolved catalog keyed by `_evaluationIdentity` (cheap to evaluate) plus the selector, or remove the VARIANTS cache namespace.

### M11 (medium, cache correctness) Cache validation rejects valid records, causing a permanent miss
- Where: `python/compiler_suit_runner/incremental_cache.py:444-446` requires `packageIdentity is None` when a release has no available requested target. But `lib/matrix.nix:471-475` fills `packageIdentity` from the x86_64 resolution, and `lib/resolve-package.nix:73` returns metadata whenever the attribute exists, whether or not the package is available. I reproduced it: a record `{archs: [], skipped_targets: {aarch64: "unsupported-platform"}, packageIdentity: <hex>}` raises "invalid unavailable-release cache record", and `lookup_variants` turns that into a warning and a miss.
- Why: Any run that combines `--archs aarch64` with an x86-only package (guetzli, hyperscan) never gets a cache hit.
- Fix: The check disappears if M10 removes the cache. Otherwise define "unavailable" in one place, for example by having Nix return a null identity when no target is available.

### M12 (medium, perf) Other paths still force full or extra evaluation
- Where:
  - The flat outputs `dataset`, `_meta` and `_drvPaths` still build the full key list for each (package, arch) (`lib/matrix.nix:337-375`). `_eval_meta_for_arch` with `selected_suffixes` (`python/compiler_suit_runner/workers/eval_worker.py:561-564`) still forces that key list, because `intersectAttrs` needs the names.
  - `packageCatalog` reads `.x86_64` for version, identity and source (`lib/matrix.nix:472`) even when only other targets are requested.
  - `load_inventory` evaluates `_evaluationIdentity` in a fresh `nix eval --no-eval-cache` once per arch (`python/compiler_suit_runner/variant_inventory.py:76`, called per arch from `eval_worker.py:951-957`), and Nix recomputes the same value again in `selectDataset`.
  - `packageVariantCounts` (`lib/matrix.nix:490-495`) forces availability for every package on every target, and nothing consumes it.
- Fix: Evaluate the identity once per task. Take the native target from `pkgs.stdenv.hostPlatform`. Remove unused outputs. Once M8 is done, document the flat outputs as interactive-only.

### M13 (medium, perf) The completion check re-hashes every published byte for every task
- Where: `python/compiler_suit_runner/publication.py:58-63` computes sha256 over every file of the published variant. It runs before every build task, against the publish destination (`python/compiler_suit_runner/workers/build_worker.py:1959-1974`). `filter_existing_variants` (`python/compiler_suit_runner/preflight.py:2018-2034`) would do the same for the whole dataset if anything called it.
- Why: When most variants are already done, a resume reads the whole dataset back over the shared filesystem. The same code wrote the completion record atomically.
- Fix: Check the completion record, the file list and the file sizes. Keep full hashing as an explicit verify command.

## D. Publication and resume

### M14 (high, data layout) Variant directories change with every derivation change, and old ones are never removed
- Where: `_short_dataset_name` (`python/compiler_suit_runner/preflight.py:34-60`) hashes `[label, packageIdentity, variant_drv]` into 16 hex digits for new records, while legacy records hash only the label into 8. `stage_release` (`python/compiler_suit_runner/publication.py:116-121`) writes `<packageId>/<variant_dir>`, and no code in the runner prunes a previous directory for the same (packageId, label) (checked with grep for rmtree and prune). Any change to the elf-folder drv creates a new directory: a nixpkgs bump, an `elfFolderBuildTools` change, or the new `datasetMetadata` env at `lib/mkBinaryFolder.nix:47`.
- Why: After the first run following the merge, every existing package directory holds both the 8-hex legacy copy and the new copy, and each later pin bump adds one more. Anyone walking `<packageId>/*` sees duplicate variants with no way to tell which one is current. Before this branch, the label hash replaced the old directory in place.
- Fix: Key the directory on (packageId, variant label) only, and keep drv and identity in the completion record, which already checks both (`publication.py:53`). If history must be kept, write a per-package index of current directories and prune superseded ones in the same atomic publish.

### M15 (medium, separation) Output naming is computed in three places through private imports
- Where: `variant_output_names` (`python/compiler_suit_runner/package_selection.py:165-173`) lazily imports `preflight._short_dataset_name`. It is called from `python/compiler_suit_runner/workers/dependency_graph_worker/archive.py:763`, `python/compiler_suit_runner/dependency_graph_planner/descriptors.py:309` and `python/compiler_suit_runner/manifest_gen.py:558`. Each call overwrites a `variant_dir` the spec already has (`manifest_gen.py:543` then `:558`, `descriptors.py:288` then `:309`). `archive.py:40` imports `_short_dataset_name` at module level.
- Fix: Move naming into `publication.py` (for example `variant_location(spec) -> (dir, metadata_name)`). Compute it once when `archive.py` builds the spec, and treat it as data from then on.

### M16 (medium, dual path) The build worker has two publication paths, and runs can mix them
- Where: `python/compiler_suit_runner/workers/build_worker.py:2154-2170` calls `stage_release` when the payload has `packageIdentity`. Otherwise the older `copy_elf_folder` plus sidecar path runs. `python/compiler_suit_runner/workers/dependency_graph_worker/archive.py:709-712` silently skips (`continue`) archives without `datasetMetadata`, so one run can publish some variants through the legacy best-effort path, without provenance.
- Fix: Fail planning when an archive lacks `datasetMetadata`, since `matrix_eval` can recreate it cheaply. Then delete the legacy publication branch.

### M17 (low, error handling) Silent fallbacks around resume and staging
- Where:
  - `python/compiler_suit_runner/workers/build_worker.py:2413-2417` turns any path mismatch into `resume_dir = None`, which turns the resume check off without a log line.
  - `build_worker.py:2156-2163` uses `except Exception`, so programming errors are reported as "release staging failed".
  - `python/compiler_suit_runner/publication.py:66` treats every error, including a `KeyError` from a renamed field, as "not complete", which quietly triggers a rebuild.
  - `publication.py:81-83` skips the compiler-field check whenever the payload value is empty.
- Fix: Log a warning when resume is disabled. Catch only `(OSError, ValueError)` around staging. Log the reason for a completion miss at debug level.

### M18 (low, layering) The publication module imports from the build worker
- Where: `python/compiler_suit_runner/publication.py:122` lazily imports `write_sidecar_metadata` from `workers/build_worker.py`, which in turn imports `publication`.
- Fix: Move `write_sidecar_metadata` into `publication.py`.

## E. Selection and CLI

### M19 (high, duplication) Selector rules are written in four places
- Where:
  - `select_package_ids` (`python/compiler_suit_runner/package_selection.py:35-85`) validates `FAMILY=VERSION`, unknown families, `--packages` conflicts and the all-versions combination.
  - `resolve_package_selection` (`python/compiler_suit_runner/preflight.py:875-905`) repeats those checks before calling it.
  - `python/compiler_suit_runner/cli.py:1444-1452` re-checks unknown archs and "no available cells".
  - `python/compiler_suit_runner/preflight.py:983-1001` checks both again.
- Why: The messages and the order of checks already differ. For example, `resolve_package_selection` checks a package ID against the index (`:896-898`) before `validate_package_id` ever sees its alphabet.
- Fix: Put one `resolve_selection(load_index, load_details, request)` in the selection module. It owns every rule and returns a typed result. Preflight only supplies the Nix loaders, and the CLI calls nothing else.

### M20 (medium, Nix inside Python) Catalog projections are Nix source assembled in f-strings, and tests regex-parse them
- Where: `python/compiler_suit_runner/preflight.py:882-927` (three `--apply` expressions with embedded `builtins.fromJSON "<json>"`), `python/compiler_suit_runner/cli.py:2285-2287` and `python/compiler_suit_runner/workers/eval_worker.py:561-564`. The test fake at `python/compiler_suit_runner/tests/test_preflight.py:109-116` pulls the JSON back out with `re.findall(r'builtins.fromJSON ("...")')`.
- Why: The selection logic is split between Python and ad hoc Nix text, and the tests depend on the exact shape of that text.
- Fix: Add flake functions (`_packageIndex.<sys>`, `_packageDetails.<sys> { ids; targets; }`) and call them with one JSON argument, the way `_selectDataset` already works. A test can then fake a function call instead of a string.

### M21 (medium, hidden coupling) The resolved selection is passed through the argparse Namespace
- Where: `python/compiler_suit_runner/cli.py:1442-1443` sets `args._resolved_package_ids` and `args._package_catalog_token`. `cli.py:855-856` reads them back with `getattr` defaults, so any caller that skips that block in `cmd_submit` silently puts an empty catalog identity into the cache key.
- Fix: Pass the resolved selection explicitly, as in `_variant_axes_from_args(args, selection)`.

### M22 (medium, behavior change) The `preflight` subcommand changed meaning and left dead code behind
- Where:
  - `python/compiler_suit_runner/cli.py:2266-2305` now prints release estimates and no longer reports toolchains, common deps or distinct drvs.
  - `preflight()` (`python/compiler_suit_runner/preflight.py:1974-2015`) has no caller left.
  - `filter_existing_variants` (`preflight.py:2018`) had no production caller before this branch either, yet it was rewritten.
  - The module docstring (`preflight.py:1-15`) and the `enumerate_variants` docstring (`:948-970`) still describe `attrNames` enumeration and the old return shape.
- Fix: Keep the toolchain output or give the new command a new name, such as `plan`. Delete the dead functions or wire them in, and update the docstrings.

### M23 (low, CLI consistency) The `catalog` subcommand redeclares options, and repeat behavior differs
- Where: `python/compiler_suit_runner/cli.py:579-583` declares `--flake`, `--system` and `--packages` again but not `--archs`, so `catalog` always resolves every target. `--packages` uses `nargs="+"` without `extend` (`cli.py:155`), while `--package-versions` and `--package-ids` extend. `cmd_submit` reports user mistakes with `log.exception` (`cli.py:1453-1455`), so "unknown family" prints a traceback.
- Fix: Reuse a subset of `_add_common_args`. Make all repeatable options extend. Use `log.error` for `ValueError`.

## F. Nix module structure

### M24 (medium, data mixed with logic) `package-versions.nix` mixes curated data, descriptor construction and validation
- Where: `lib/package-versions.nix:32-34` hard-codes the hello 2.12.1 release inside the module, as the default of `releases`, with a hand-copied `{ attr = "hello"; label = "hello"; }` instead of a reference to `packages.nix`. The `releases` argument expects fully built descriptors, so `tests/package-versions.nix:10-13` builds them by hand with a fake `releaseId`, and `descriptor` is never tested. `adapterRevision` (line 14) and `compatibility` (line 29) both encode `provider == "current"` under different names. Only `compatibility` gates the shims (`lib/mkVariant.nix:268`, `:273`), and only `adapterRevision` enters the identity hash.
- Fix: Move curated releases to a data file (`lib/package-releases.nix`, a list of `{ family; provider; version; id; }`) and map each entry through `descriptor`. Derive a single `adapter = { name = "current-v1"; revision = 1; }` and use it for both the shims and the identity.

### M25 (medium, resolver) `resolve-package.nix` repeats checks and returns metadata for unavailable packages
- Where:
  - `lib/resolve-package.nix:69-77` writes the expected-version throw twice, once for `package` and once for `meta`.
  - `meta` is non-null whenever the attribute exists (line 73), even when the package is unavailable. This causes M11.
  - `packageCatalogSchema = 1` (line 64) is hard-coded instead of reading `catalog.schema`.
  - `hasSource` (line 47) assumes a single `src`, so a recipe that only has `srcs` would be reported as "missing-source". No default package is affected today (checked by eval).
  - For the `current` provider, the package comes from the resolver's package set but the stdenv comes from mkVariant's own `getPkgsForTarget` call (`lib/mkVariant.nix:29-33`). For the two `crossSystem` targets, those are two separate nixpkgs imports.
- Fix: Compute the version check once. Return `meta = null` unless the package is available, or put `available` into `meta`. Have the resolver return its `targetPkgs`, and use those in mkVariant for the current provider.

### M26 (medium, duplication) Dataset metadata is still defined twice, plus a third layer
- Where:
  - `lib/matrix.nix:209-247` (`mkMeta`) and `lib/mkVariant.nix:378-392` (`passthru.datasetMeta`) build the same record. `variantLabel` is also built twice (`lib/mkVariant.nix:238-246` and `lib/matrix.nix:232-236`).
  - `lib/mkBinaryFolder.nix:35-41` adds a third layer and writes it out twice, as the env var at line 47 and in the heredoc at line 98.
  - Step 2 of the plan asked for one shared definition. Because there isn't one, `tests/multiversion-fixtures.nix:101-103` has to assert that the copies agree.
- Fix: Write one `variantMetadata { pkg; target; combo; resolved; }` function in its own file and use it in both places. In `mkBinaryFolder`, write `$datasetMetadata` (or a `passAsFile` file) to `meta.json` instead of interpolating the JSON a second time.

### M27 (medium, evaluation identity) The evaluation identity mirrors the image filter by hand and also hashes Python code
- Where: `lib/evaluation-identity.nix:5-15` lists the directories and files to hash. The runner image filter at `flake.nix:229-243` is a separate list, and `tests/evaluation-performance.nix:131-136` is a third copy. `flake.nix:162` lists the inputs by hand. The token covers every `.py` file under `python/compiler_suit_runner`, so any runner change, logging included, invalidates the inventory caches and makes in-flight `_selectDataset` calls fail with "evaluation inputs changed since preflight".
- Why: Submitter and image agree only while three lists agree. A Nix evaluation identity that also versions Python sampling rules mixes two concerns.
- Fix: Hash the same filtered source the image uses (the `cleanSourceWith` result). Use `inputs` minus `self`. Version the Python sampling rules with an explicit constant in `variant_inventory.py`.

### M28 (low, compatibility shim) `_selectDataset` accepts a bare map that skips the identity check
- Where: `lib/matrix.nix:305-310` (`if request ? selection ... else request`). `python/compiler_suit_runner/workers/eval_worker.py:438` sends the bare map whenever the identity is missing, and only a test uses the bare form (`tests/evaluation-performance.nix:127`). Because `evaluationIdentity ? null` is optional (`lib/matrix.nix:14`), forgetting to wire it produces the misleading "evaluation inputs changed".
- Fix: Always require `{ identity; selection; }`, and make `evaluationIdentity` a required argument.

### M29 (low, flake outputs) Seven new top-level `_` outputs with per-system boilerplate, some unused
- Where: `flake.nix:321-329` and `:419-425`. `_packageVariantCounts` (`:326`) and `curatedMatrixSize` (`:373`) have no consumer, `_rawPackages` is used only by tests, and `_selectDataset` is a function exposed as a flake output.
- Fix: Group them under one `_runner.<system>` attrset (catalog, axes, counts, select, identity) and drop the unused ones.

## G. Python module layout

### M30 (medium, separation) `package_selection.py` is a grab bag, and lazy imports hide import cycles
- Where:
  - `python/compiler_suit_runner/package_selection.py` holds selector resolution (35-85), Nix quoting helpers (23-32), release validation (107-125), a matrix sidecar identity (93-104), a cache token (128-135), task-ID formatting (138-143), metadata translation (146-162) and publication naming (165-173, which imports a private preflight function).
  - `python/compiler_suit_runner/variant_inventory.py:72,124` lazily imports `run_nix_eval` and `is_known_bad_combo` from preflight.
  - `python/compiler_suit_runner/workers/dependency_graph_worker/archive.py:702` and `python/compiler_suit_runner/workers/eval_worker.py:1097` import the private `nix_drv_show._normalize_show_output`. `archive.py:764` imports `preflight._tier_from_pkg`.
  - Modules that already import `package_selection` at the top import it again inside functions (`eval_worker.py:106` versus `:256`, `:329`, `:562`, `:1098`, and `manifest_gen.py:59` versus `:259`, `:357`, `:556`).
  - New top-level imports were inserted in the middle of the stdlib block (`suit_task.py:82`, `eval_worker.py:106`).
- Why: The cycles exist because shared helpers live in `preflight.py`: the nix-eval runner, the rules, naming and tiers. Lazy imports hide the dependency graph and weaken static analysis.
- Fix: Split into `nix_expr.py` (`nix_string`, `nix_attr_path`, `run_nix_eval`), `releases.py` (the `Release` type and selection), `publication.py` (naming and completion) and `variant_rules.py` (`is_known_bad_combo`, tiers). Then make every import top-level.

### M31 (low, over-validation) Defensive checks on the runner's own Nix output
- Where: `python/compiler_suit_runner/variant_inventory.py:24-67` (group, label and candidate limits of 8192, 128, 100000 and 5000000), `:83-96` (a checksum over a cache file the same function wrote), `:17` (a 4 MiB limit), plus `python/compiler_suit_runner/workers/eval_worker.py:318-331` and `python/compiler_suit_runner/preflight.py:928-933`.
- Why: About 80 lines guard against a trusted, schema-versioned producer, and every limit is one more constant to keep in step with matrix growth. Today `_variantCounts` is about 540k candidates for x86_64 and aarch64 together.
- Fix: Keep the schema, identity and arch checks. Drop the size limits and the checksum, because `os.replace` already prevents torn writes.

### M32 (low, speculative fields) The code references capture fields that nothing on this branch produces
- Where: `compilerCapture` is copied at `python/compiler_suit_runner/package_selection.py:159`, `python/compiler_suit_runner/manifest_gen.py:559` and `python/compiler_suit_runner/dependency_graph_planner/descriptors.py:310`.
- Fix: Remove it until the capture work lands, then add it through the `Release` and variant types.

## H. Bundled unrelated behavior changes

### M33 (low/medium, scope) Planner and installable semantics changed for every package
- Where:
  - `template_graph/cowalk/build_template.py:238-268` now dedupes singleton nodes by derivation ident instead of role name, and `name_to_id.setdefault` (line 267) leaves `name_to_id` partial. Nothing reads it except the dataclass field (`template_graph/graph/template.py:53`).
  - Children are now walked in ident order (`build_template.py:296`).
  - `template_graph/parser/role.py:213` makes every elf-folder root its own role.
  - `python/compiler_suit_runner/workers/eval_worker.py:910-915` switches local flakes with `.git` from `path:` to `git+file://`, which hides untracked files from workers.
- Why: These change template shape and worker inputs for all packages, not only versioned ones, yet they arrive bundled inside a feature commit. `name_to_id` is now misleading.
- Fix: Move them to separate commits, with before/after template counts on a real matrix archive. Delete `name_to_id` or document that it maps only the first node.

## I. Tests, docs and repo hygiene

### M34 (medium, tests) The root-level tests are evidence scripts, not regression tests
- Where:
  - `tests/*.nix` and `tests/*.py` are neither in the pytest testpaths (`python/pyproject.toml:63-66`) nor in flake checks.
  - `tests/evaluation-performance.nix:144-165` and `tests/multiversion-fixtures.nix:84-107` return attrsets of booleans and lists, and nothing asserts on them.
  - Several hard-coded values will break on the next pin bump: `541733` candidates (`tests/evaluation-performance.nix:163`), `"2.12.3"` (`tests/multiversion-fixtures.nix:86`), the `gcc15`/`clang21` suffixes, and `{"2.12.1", "2.12.3"}` (`tests/multiversion-flow.py:97`).
  - `multiversion-flow.py` also enforces a 100 GiB free-space reserve and writes into `.multiversion-work`.
- Fix: Turn the pure checks (catalog validation, poison-value laziness, parse round trips) into `checks.<system>` entries using `assert`/`lib.assertMsg`, or into `pytest -m nix` tests, and read expected values from the catalog instead of literals. Keep the pilot flow and the benchmark out of the test tree, for example under `scripts/experiments/`.

### M35 (medium, docs placement) A work order and run ledgers are committed as docs
- Where:
  - `planning-multiversion.md` (403 lines, at the repo root) is an agent work order. It says to "run implementation in a T3-created worktree", forbids commit, reset and stash, and records "approximately 587 GiB free".
  - `docs/multiversion-verification.json` (1013 lines, 74 store paths, free-space byte counts, deleted paths) and `docs/evaluation-performance.json` (324 lines of per-run timings) are ledgers from a single session.
  - `docs/multiversion-compatibility.md` is a lab notebook, with initial free bytes and "owned by another thread".
  - `.multiversion-work` is baked into `.gitignore`, `flake.nix:239` and `tests/evaluation-performance.nix:135`. The README hard-codes the current version, 2.12.3.
  - No maintained design doc defines the identity terms. The planning doc is the closest thing.
- Fix: Move the plan, the JSON ledgers and the lab notes into the PR description or an external artifact. Replace them with one short `docs/package-releases.md`: an identity glossary, the catalog format, how to add a release, resume semantics and the known costs. Keep `evaluation-performance.md` only if it is cut down to the design and the reproduction steps. Remove `.multiversion-work` from `flake.nix`.

## Suggested refactor order

Before merge. These decide persistent formats or remove costs this branch adds:
1. Decide the publication layout (M14) and package-ID stability (M2, which needs the data file from M24). Both shape on-disk data and user-facing IDs, so changing them later means a migration.
2. Add the `Release` dataclass (M3), then do the identity renames (M1) through it, so each rename touches one place. Do this before any sidecar or completion record is published with the current names.
3. Choose a cache strategy (M10), which removes or fixes M11.
4. Restore or rename the `preflight` subcommand and delete the dead functions (M22).
5. Remove the work order, ledgers and `.multiversion-work` references, and add the short design doc (M35).

After merge, in dependency order:
6. Switch `_selectDataset` to structured selections and add `compilersByLabel` (M5, M9). Then delete the legacy eval path and the duplicate sampler (M8), along with the bare-map shim (M28). M5 has to come first so the new path covers everything before the legacy one goes.
7. Consolidate selection into one resolver backed by Nix functions (M19, M20, M21).
8. Split the modules (M30), then make imports top-level and move naming and the sidecar writer into `publication.py` (M15, M18). Drop the legacy publication branch (M16) and tighten error handling (M17).
9. Clean up the Nix side: the releases data file (M24), the resolver (M25), a single `variantMetadata` (M26), an identity derived from the filtered source (M27), and grouped flake outputs (M29). These are independent of each other.
10. Make resume cheaper (M13) and remove the remaining extra evaluations (M12).
11. Turn the tests into checks (M34) and split the bundled planner changes into their own commits (M33).

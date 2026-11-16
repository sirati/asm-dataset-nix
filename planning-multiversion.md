# Multiple versions of one dataset package

Status: implementation plan, 2026-11-14.

## Outcome and scope

Allow two or more upstream releases of one package to coexist in a single dataset
run under the same compiler, target, flags, and capture policy. Each result must
identify the exact release, source, and recipe used. Selection, evaluation,
dependency planning, publication, and resume must distinguish them.

Implement the general mechanism and prove it with one small real package first.
Use GNU hello as the initial candidate because the project already includes it
and existing pinned nixpkgs inputs may provide distinct releases. Verify the
actual releases before choosing them. The implementation may select two pinned
hello releases without requesting further approval. If hello cannot demonstrate
the necessary behavior without recipe changes, choose an existing small C package
with documented evidence and report the substitution.

This task does not mean ELF multi-output support, compiler function multiversioning,
or multiple compilers for a package. Those are separate concepts. The package
version becomes an additional selection dimension while existing compiler axes
remain independent.

Do not automatically multiply the entire package catalog by historical versions.
Default invocations keep their current version selection and matrix size. Additional
releases are explicit, curated catalog entries. Broader release curation and
changes to default coverage are a later user decision.

## Starting state and concurrent work

The project root is on local `main`, 65 commits ahead of origin at planning time.
It contains extensive uncommitted compiler-capture work owned by another active
thread, plus the user's existing `flake.lock` modification. Do not commit, reset,
stash, clean, or move any of those changes as part of this task.

Run implementation in a T3-created worktree from local `main`, with
`startFromOrigin: false`. The unpublished local runner commits are required.
A worktree created from origin/main would omit those changes. Uncommitted capture
work is not inherited, and must not be copied wholesale into the new branch.

This plan is stored in the project root and is readable by absolute path from the
new worktree. Copy this plan into the implementation checkout if needed, without
copying unrelated working-tree files. Keep the original compiler-capture
`planning.md` unchanged.

Implement core multiple-version support against the new checkout. Inspect current
capture interfaces read-only to avoid designing conflicting identities. If capture
work becomes committed, integrate its actual commit only after inspecting it.
If it remains uncommitted, deliver the independent work and list the exact small
integration changes and tests still pending. Do not claim capture-enabled tests
passed when they were unavailable. No changes to the other thread's working files.

## Relevant current behavior

- `lib/packages.nix` describes packages with an attribute name and label.
- `lib/matrix.nix` uses that attribute both for availability checks and matrix
  construction. Package labels are matrix keys; suffixes contain compiler axes.
- `lib/mkVariant.nix` resolves `targetPkgs.${pkg.attr}`, overrides its stdenv,
  applies compatibility shims, and pins its pre-rename source to avoid a
  `finalAttrs.pname` source-URL regression.
- Preflight enumerates package keys. A matrix-eval task handles each package key,
  generating package-named archives and mappings for the streaming planner.
- `template_graph/tree_walker.py` recovers the package prefix from variant
  derivation names. Hyphenated identities must round-trip through that parser.
- Task IDs and published paths incorporate package keys, but an uncaptured resume
  check currently accepts an existing sidecar by variant label alone.
- Current root-only capture additions include `resolve-toolchain.nix`, output
  roles, a captured dataset namespace, and capture-aware cache/completion checks.
  These are integration constraints, not committed baseline assumptions.

Adding only a version field or changing `version` on a derivation does not select
a different release. Resolution must select a matching source and compatible
recipe, and every downstream stage must retain that identity.

## Design decisions

### 1. Separate family, release identity, and display version

Use a descriptor with these logical fields, with final spelling chosen consistently:

- `packageFamily`: existing logical package label, such as `hello` or `lua`.
- `packageId`: safe unique key for one selected release/recipe entry.
- `packageVersion`: exact resolved upstream version, without lossy normalization.
- `releaseId`: immutable catalog ID, distinct from a moving default alias.
- `recipeId`: recipe provider, pinned revision, attribute path, and optional
  centrally defined adapter revision.
- `sourceIdentity`: immutable fetch identity, including available content hashes
  and revisions, rather than only a download URL.
- `packageIdentity`: canonical digest of the release/recipe/source record.
- `isDefault`: whether the legacy family selector selects this entry.

Retain the existing key `hello` for its current default entry. Give additional
entries safe explicit keys, for example `hello--v2_10--r<digest>`. This is an
illustration, not a claim that that release has been resolved yet. Restrict keys
to a filesystem/Nix-store/parser-safe alphabet. Reject duplicate IDs and ambiguous
version selectors; do not silently overwrite entries using `listToAttrs`.

Keep exact versions in metadata. A slug is only a path component, never an
upstream-version parser. Two recipes with the same upstream version can differ
in patches and dependencies; they must not share the same full identity.

The default alias can resolve differently after an intentional nixpkgs update.
Therefore stable alias spelling is not sufficient for cache or completion reuse.
Use the resolved identity and derivation, even for the legacy default entry.

### 2. Preserve the matrix shape

Continue indexing `dataset.<system>.<packageId>.<arch>.<suffix>`. Additional
release entries use additional package keys. Keep the compiler/flag suffix shape
so the streaming planner does not require a new nested version axis everywhere.

Apply the same expansion to `_meta`, `_drvPaths`, raw-package access, manifest
exports, and captured outputs when available. Existing default-package paths
must remain valid. Expose a small `_packageCatalog.<system>` metadata output for
selection and inventory, including family/default relationships and provenance.
Do not make callers derive the family by splitting a package ID.

Define supported quoting for Nix attribute paths. Validate reserved delimiters
against all registered family labels. Test dots, plus signs, underscores, and
hyphens in upstream versions without constructing unsafe shell commands.

### 3. Resolve one recipe consistently

Introduce one resolver used by availability checks, metadata, and actual builds.
It receives the package descriptor and target/build context. It returns the
selected package derivation, release identity, provenance, and availability reason.
The build path applies the existing selected compiler stdenv and dataset flags.

For the first pilot, prefer unmodified recipes from existing pinned nixpkgs
inputs. Import the correct native or cross package set using that revision's
supported mechanism. Keep package recipe source independent from compiler source:
selecting a historical package must not silently replace the requested compiler.

Investigate two possible providers before committing to an adapter:

1. A full package from an existing pinned nixpkgs set, with the requested stdenv
   override and its pinned dependencies. This gives a real historical recipe.
2. An existing recipe evaluated with explicit dependencies in the current package
   context, only where a measured adapter is necessary and compatible.

Do not assume a historical package object is safe against a modern cross context,
or that calling a historical lambda through current `callPackage` is universal.
Choose the least invasive proven provider for the pilot, retain its dependency
policy in provenance, and implement a small provider interface for later recipes.

Never download an arbitrary release by synthesizing a URL and accepting a fake
hash. Never represent a metadata-only version override as release support. No
edits to upstream source trees, package build commands, or per-package capture
hooks. A source/version override is a separate explicitly reviewed provider and
is not the initial mechanism. Add a new pinned nixpkgs input only if the existing
ones cannot supply two usable releases, and only to the isolated checkout.

### 4. Make selection explicit and backward compatible

Implement these semantics and document them in CLI help:

- With no version options, `--packages hello` selects the default hello release;
  no package filter selects the existing catalog's default entries only.
- `--package-versions hello=<exact-version>` selects an explicit release. Accept
  repeated selections or multiple values using one documented syntax.
- Two selected hello versions replace the implicit default selection for hello;
  do not add a third release accidentally. Other selected families retain defaults.
- A version selection can imply its family when `--packages` is absent. If an
  explicit package-family filter conflicts, reject the request with a clear error.
- `--all-package-versions`, if implemented, expands only the selected families to
  their curated releases, never discovers arbitrary releases from the network.
- An advanced exact `packageId` selector may disambiguate same-version recipes.
  Ambiguous family/version matches fail with candidate IDs and recipe details.

A single selector resolver produces a sorted set of package IDs, threaded into
all phases. Canonicalize ordering and duplicates. Unknown families, versions,
or package IDs fail before toolchain staging; they must not yield an empty
successful submission. Expose selected versions and estimated variant counts in
preflight output. New extra keys must not broaden legacy `attrNames` enumeration
in the runner accidentally; use catalog defaults explicitly.

### 5. Keep compiler/dependency sharing correct

Toolchains remain keyed by target/compiler/capture policy, not by package release.
Common dependencies remain deduplicated by actual derivation identity, not by
family or version labels. Different recipes may share some dependencies and
require different versions of others. Do not disable existing deduplication or
force unrelated release-specific dependencies to share one task.

## Exploratory work, before changing the runner

1. Read instructions in the implementation worktree and verify its base SHA.
   Record pins and checkout state. Confirm root work remains untouched.
2. Inventory two distinct hello releases available from already pinned providers.
   Record actual `.version`, source fetch identity, recipe revision/attr path,
   outputs, stdenv override interface, platforms, and dependency policy.
3. Build small synthetic release descriptors to test duplicate detection, default
   aliases, key escaping, missing/ambiguous versions, and lazy catalog evaluation.
   Do not build actual compilers for these pure checks.
4. Build each real release natively with the same modern compiler and one baseline
   configuration. Use `--max-jobs 1 --cores 1`. Prove distinct upstream content
   using source identity and package-level version output where feasible.
5. Evaluate both with a second supported compiler and a cross architecture.
   Build one bounded cross pair using cached toolchains. Check ELF architecture
   rather than executing foreign binaries. Verify requested compiler selection
   from derivation inputs and capture evidence when available.
6. Check a changed `pname`/`finalAttrs` source case. Ensure existing source pinning
   is applied to the correctly resolved release, not the family default recipe.
7. Record a deterministic unsupported target/compiler case with a reason.
   Missing release data must not fall back to the default package silently.

Checkpoint: select the concrete two-release pilot and provider only after these
checks. Save the evidence in a small compatibility report; then implement broadly.

## Step-by-step implementation

### Step 1. Catalog schema and resolver

Add `lib/package-versions.nix` for curated entries and
`lib/resolve-package.nix` for provider resolution, or equivalent clearly separated
modules. Adapt existing `{ attr; label; }` entries into default descriptors so the
rest of the package list need not be rewritten by hand.

Register the pilot's default and additional release. Pass pinned provider inputs
explicitly from `flake.nix`. Do not import the mutable host `NIX_PATH` or resolve
remote branch names at runtime. Avoid import-from-derivation for basic metadata.

Keep metadata enumeration lazy. It may inspect a selected derivation's metadata
where required, but must not instantiate every compiler/flag variant. Measure
catalog and single-family evaluation time and source/store footprint.

### Step 2. Nix matrix construction

Expand package entries before matrix generation. Replace direct
`targetPkgs.${pkg.attr}` lookups in both availability checks and `mkVariant` with
the shared resolver. Keep `_meta` and the realizable dataset in agreement.

Apply package-specific compatibility shims by family plus explicit recipe/release
constraints. Tests must show that shims still apply to versioned entries without
blanket rewriting historical packages. Preserve `env` flags, compiler link flags,
LTO tools, outputs, and default output behavior.

Put the distinct package ID into the variant derivation label used by the ELF
folder, matrix archive, and planner. Keep the actual package pname/version intact
where possible; do not rename source URLs inadvertently. Add full release metadata
to both lazy metadata and built sidecars from one shared definition.

Do not weaken global evaluation errors to hide a broken release entry. Distinguish
unknown release, unavailable target, incompatible override, and build failure.
Report skipped cells with reasons while allowing unrelated valid cells to run.

### Step 3. CLI, catalog display, and preflight

Add catalog listing with a family filter and JSON output, or extend an existing
preflight inventory command. Show version, exact ID, provider pin, default status,
and target availability. Expose two-release selection examples in both READMEs.

Add version selectors and call one canonical selection function before costly
enumeration/toolchain work. Carry descriptor metadata in `per_binary_metadata`.
Use family identity for priority/tier lookup; a versioned ID must not lose the
family's curated priority. Preserve deterministic sampling and define whether
samples match across releases. Prefer identical sampled compiler/flag combinations
for a family so release comparisons are meaningful; test independence from
selector ordering and from adding another release.

### Step 4. Distributed evaluation and planner identities

Treat package ID as the current per-binary worker identity. Audit all archive,
sidecar, gate, and matrix-aggregate names. Two releases must get separate
matrix-eval tasks and archives; dependency_graph still combines their graphs.

Update both manifest/task construction and streamed descriptor construction.
Maintain one shared identity helper rather than separately concatenating IDs.
Verify architecture remains part of variant identity even when the worker's
`label` contains only a suffix. Include package release and capture mode wherever
current task semantics require them. Preserve cross-phase dependency wiring.

Add parser round trips in `template_graph` for a versioned package ID, especially
labels ending in tokens resembling architectures, compiler names, or optimization
levels. Do not recover family/version by reparsing derivation names; look those
up through the explicit catalog/metadata associated with the package ID.

### Step 5. Cache, resume, and publication

Extend enumeration cache axes with canonical release selection and a catalog
schema/identity token. Ensure recipe/source changes invalidate results even when
CLI strings are identical. Version cache payloads as needed; incomplete legacy
entries should be misses, not exceptions or fabricated version metadata.

Keep publishing `<dataset-root>/<packageId>/<variant-dir>/...` to minimize churn.
The default alias retains its familiar directory, while additional releases get
distinct directories. Sidecars include family, ID, exact version, recipe/source
identity, package identity, actual variant drv, output paths, and compiler axes.

Replace label-only completion for new records with a checked identity including
release provenance and actual derivation. A default alias changing upstream
release must not reuse yesterday's result. Legacy sidecars lacking required
identity cannot prove version correctness and should be rebuilt when relevant.
Capture-enabled completion must satisfy both package identity and its existing
capture contract. Never treat capture validation alone as package-version proof.

Publish metadata and completion atomically. Retrying release B must not remove or
skip release A. Same version with a new recipe/source must not be mistaken for
an existing complete result. Keep package identity separate from timestamps,
host names, and run IDs.

### Step 6. Capture integration without overlapping ownership

The current root capture implementation is not committed as of this plan. Core
version support must remain independent of its Rust internals. Expected integration
points are `resolve-toolchain.nix`, `mkVariant.nix`, matrix metadata, output role
collection, captured dataset selection, and Python capture-aware cache/completion.

Use one package resolver before applying the requested capture toolchain and
output adapter. Historical package selection must not drop `datasetCapture` or
lose compiler/version association. Run both pilot releases with capture enabled
when that implementation is available in the checkout, and check all required
ELF/capture associations and release provenance.

If integration cannot yet run, list the exact pending test and anticipated merge
points in the handoff. Do not edit root capture files to get a green check and do
not mark that integration complete without running it.

## Tests and acceptance criteria

Use focused unit tests for selectors/identity and real Nix fixtures for recipe and
source correctness. Do not mirror every helper with a tautological unit test.

Required cases:

- Existing default package paths and CLI behavior remain valid and do not expand
  automatically when another release is registered.
- Two actual upstream versions of one family build together with identical
  compiler/target/flag selections and publish separate validated results.
- Selecting release B changes source and reported version, not only labels.
- Canonical selection is stable under reordering and deduplication.
- Unknown and ambiguous selectors fail before any large build or upload.
- Same upstream version from different recipes has distinct identity.
- Missing source/provider/target does not silently fall back to another release.
- `_meta`, catalog, dataset, task manifests, descriptor batches, output sidecars,
  and resume checks agree on identity.
- Versioned IDs round-trip through tree parsing and archive/gate naming.
- Common dependencies and toolchains deduplicate when their drv identities match;
  genuinely different dependency drvs remain separate.
- Changing the default release or recipe invalidates stale enumeration and output
  completion even if its legacy alias and compiler suffix remain unchanged.
- Exact source pinning survives existing variant pname overrides.
- Pure metadata and single-family lookup do not force the full variant matrix.
- Actual native and bounded cross builds verify the requested compiler and target.
- Existing runner regression tests pass. Capture integration is either tested
  explicitly or recorded as pending because its implementation is unavailable.

A distributed-flow test must carry two releases through eval, graph planning,
streamed spawn, worker staging, and completion. Mock external scheduler transport
where appropriate, but use real Nix builds for the two source/version assertions.
No full SLURM deployment is required to validate basic release support.

## Disk-conscious exploratory and test procedure

The root filesystem had approximately 587 GiB free at plan time, shared with other
active work. Recheck before testing; this figure is not a storage allocation.

Use a task-owned ledger for input roots, result roots, outputs, scratch, and logs.
Inspect the earlier capture plan's storage procedure read-only, but keep cleanup
scoped to this worktree/task. Never run broad `git clean`, delete another worktree,
or garbage-collect another thread's unrooted test artifacts intentionally.

- Build one small package/version/compiler combination at a time initially.
  One build job, one core; at most ten tests overall.
- Dry-run exact installables before building. Prefer cached prerequisites and
  existing pins; avoid unexpected historical compiler/bootstrap rebuilds.
- Start with a 10 GiB allowance for newly acquired prerequisites/results and
  2 GiB for owned scratch/logs, plus a 100 GiB free-space reserve. If a needed
  toolchain exceeds it, report and schedule a measured separate batch; do not
  silently start an unbounded bootstrap.
- Keep only both current pilot results and a minimal failure reproducer rooted.
  Use bounded logs and timeouts, with no automatic `--keep-failed`.
- Filter Nix sources to exclude logs, caches, captured artifacts, and results.
  A changing full checkout should not be recopied for each fixture.
- Use scoped result links and remove only this task's links when finished.
  `--no-link` does not avoid store allocation, and removing links alone does not
  reclaim outputs. Delete owned unreferenced results through Nix tools.
- Root reusable prerequisites before any bounded GC. Prefer targeted deletion
  of owned results, and investigate references instead of `--ignore-liveness`.
- A systemd user scope around `nix build` does not cap daemon compiler processes.
  Use the established bounded Nix test environment if hard limits are required;
  otherwise state that fixture size and prechecks are soft growth controls.
- Keep a small compatibility/test report, not copies of all sources and outputs.
  Record actual disk growth and final cleanup state.

No lockfile updates outside the isolated checkout. No full matrix or entire
historical nixpkgs package-set build is part of this task.

## Deliverables and completion report

1. General release catalog/resolver, preserving default selectors.
2. Two real, pinned releases of one pilot family with source/recipe provenance.
3. Nix matrix, CLI, eval/planner, cache, publication, and resume support.
4. Focused regression tests plus native/cross Nix evidence and cleanup report.
5. Updated README examples and a concise compatibility/inventory report.
6. A reviewable implementation branch with commits containing only this task's
   work. Do not push or merge unless instructed. Explain any capture integration
   that remains dependent on unpublished work.

First milestone is the two-release resolver and Nix proof, before broad runner
changes. Final core completion requires both releases in one end-to-end run,
collision-free identities, correct source/version evidence, and safe resume.

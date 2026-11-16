# Multiple-version compatibility record

Implementation base: `72dd7bec5f27e41e25f5eab31a12c81c36bc2034`, local
`main`, in the T3-bound `feat/multiversion-package-support` worktree. The plan
is preserved verbatim in `planning-multiversion.md`. The project-root checkout
was inspected read-only. Its compiler-capture changes and modified lockfile
are owned by another thread and are not implementation inputs here.

## Storage and test ownership

Initial available space: 623,883,640,832 bytes. Initial checkout size:
4,445,550 bytes. The task budget is 10 GiB of acquired prerequisites/results,
2 GiB of scratch/logs, and a 100 GiB free-space reserve. Builds use one job,
one core, timeouts, and bounded logs. These are soft growth controls on the
shared Nix daemon, not a hard filesystem quota. No shared-store GC is allowed
by this task; cleanup uses only owned paths and targeted Nix deletion.

The ignored `.multiversion-work/` directory holds the task ledger, bounded
logs, input roots and result roots. The committed evidence below records the
final state without retaining copies of upstream source trees.

## Inventory and provider choice

No lockfile or input was changed. Existing pins supplied these releases:

| Input | Revision | hello | stdenv override introspection |
| --- | --- | --- | --- |
| current nixpkgs | `68a8af93ff4297686cb68880845e61e5e2e41d92` | 2.12.3 | supported |
| nixpkgs 15.09 | `9c31c72cafe536e0c21238b2d47a23bfe7d1b033` | 2.10 | legacy interface |
| nixpkgs 18.03 | `120b013e0c082d58a5712cde0a7371ae8b25a601` | 2.10 | legacy interface |
| nixpkgs 22.11 | `4d2b37a84fad1091b9de401eb450aae66f1a741e` | 2.12.1 | supported |
| nixpkgs 23.11 | `057f9aecfb71c4437d2b27d3323df7f93c010b7e` | 2.12.1 | supported |
| nixpkgs 24.05 | `63dacb46bf939521bdc93981b4cbb7ecb58427a0` | 2.12.1 | supported |

The pilot selects the full pinned 24.05 package for 2.12.1. The current
default remains `hello`, version 2.12.3. Both recipes have one `out` output
and declare all platforms. Their exact source fetch hashes differ:

| Version | Source | SHA-256 SRI |
| --- | --- | --- |
| 2.12.3 | `mirror://gnu/hello/hello-2.12.3.tar.gz` | `sha256-DV9gFUOC/uELEUocNOeF2LH0kgc64tOm97FHaHs2aqA=` |
| 2.12.1 | `mirror://gnu/hello/hello-2.12.1.tar.gz` | `sha256-jZkUKv2SV28wsM18tCqNxoCZmLxdYH2Idh9RLibH2yA=` |

The current recipe lives at `pkgs/by-name/he/hello/package.nix:52` and the
24.05 recipe at `pkgs/by-name/he/hello/package.nix:41`. Source paths and
fetcher fields are recorded in the machine-readable verification artifact.
The historical provider imports its own native or cross package set with
`localSystem` and `crossSystem`, retains its pinned dependencies, and accepts
the dataset-selected modern stdenv. A historical `callPackage` recipe in the
current dependency context was also evaluated. It selected the same source
and version with a different derivation, but was unnecessary for the pilot.
No recipe adapter, source override, synthesized URL, fake hash, upstream source
edit, or package-specific capture hook was introduced.

Forwarding the entire modern normalized `pkgs.config` is incompatible with
24.05 because `replaceStdenv = null` is interpreted as a function there. The
resolver passes only the required unfree/insecure package policies. Current
compatibility shims remain gated by explicit family and adapter revision;
historical recipes do not receive them automatically.

## Nix proof and regression coverage

The initial full-provider experiments built both native releases with GCC
15.2.0 and reported `hello (GNU Hello) 2.12.3` and `hello (GNU Hello) 2.12.1`.
The AArch64 pair has ELF machine type 183. Both native compiler derivations
are `mcmj9cdln6f4y3dp35bwbf9ghmp2cd9j-gcc-wrapper-15.2.0.drv`; both cross
compiler derivations are
`d41dhqql3ms2m6l74c06aiyzr9s37kax-aarch64-unknown-linux-gnu-gcc-wrapper-15.2.0.drv`.
Exact matrix variants also evaluate with Clang 21.1.8, using
`g75wdmbgprvnj7xp3cdwfsdaqp6y4629-clang-wrapper-21.1.8.drv` for both releases.
Both Clang package derivations also instantiate for AArch64 with
`23ix882lq815xp58v7ahwxrwy67fwq44-aarch64-unknown-linux-gnu-clang-wrapper-21.1.8.drv`.
Clang coverage is evaluation only; the bounded real builds use GCC 15.

`tests/package-versions.nix` and `tests/multiversion-fixtures.nix` check
duplicate/reserved IDs, exact version spelling, lazy catalog evaluation,
missing providers/releases/sources/targets, wrong expected versions, compiler
selection, family shim constraints, and equality between lazy and built
metadata. Two real 2.12.1 recipes from 23.11 and 24.05 have different package
identities even though their upstream tarball is identical. The expat
`finalAttrs.pname` case and historical hello retain their pre-rename source.
RISC-V with GCC 4.4 is deterministically excluded because RISC-V requires
GCC 7 or newer, as declared in `lib/architectures.nix`.

The runner suite covers selectors, canonical ordering, ambiguity, stable
sampling across releases, source/recipe cache invalidation, shared task IDs,
architecture, parser round trips, legacy/stale/damaged completion, retry
isolation, and prestaged archive provenance. Existing planner tests check
common-dependency deduplication by actual derivation identity and preserve
distinct dependency derivations. No compiler or dependency dedup key was
changed to include the package version.

The built sidecar records the raw package drv path as text with its Nix
string context discarded. Retaining a drvPath's deep context would request
all outputs of the compiler bootstrap graph. The dry-run guard caught that
before any build started. After the fix, the cross-cell dry run contains
only the hello variant and its ELF folder, with no prerequisite fetches or
bootstrap builds. `noDeepMetadataContext` prevents that regression.

The bounded distributed proof is `tests/multiversion-flow.py`. It exercises
two package IDs and four GCC 15 baseline cells through real Nix evaluation,
matrix archives, the combined graph, streamed descriptor batches, worker
staging and checked resume. Only scheduler transport is local. Final build results and checked completion are recorded below.

The real flow also found first-hyphen package parsing and prefix-based meta
selection that merged `hello--…` into `hello`. Dataset root roles now preserve
exact IDs, and slicing/meta grouping use the same variant parser. Singleton
templates keep distinct derivation identities even when dependency names share
a role; their slot ordering matches cowalk's ident ordering. This prevents
historical/current dependencies from being conflated. Archived metadata uses
the existing normalizer for both legacy and Nix v4 derivation-show JSON.

The real graph exposed an existing exponential walk when discarding shared
toolchain DAGs. `_discard_subtree` now visits each raw node object once.
A 40-level shared DAG regression would previously traverse over a trillion
paths; it now preserves unrelated release dependencies and finishes directly.

## Evaluation and storage measurements

The catalog index contains 245 curated entries and took 0.444 seconds with
18,649 JSON bytes. Resolving both hello releases took 9.346 seconds with
2,626 JSON bytes. These measurements disable Nix's eval cache. The pure
catalog test deliberately makes compiler enumeration throw, proving that
catalog provenance does not force the compiler/flag matrix.

A real unfiltered inventory selected exactly the original 244 defaults in
7.00 seconds. Of those, 238 are available natively; the six skips have
explicit reasons: capnproto, cln, mbedtls and pngquant lack the supported
stdenv override, chibi-scheme is absent, and wolfssl fails recipe evaluation.
Registering 2.12.1 does not broaden default runner enumeration. The legacy
matrix-size estimate counts defaults; `curatedMatrixSize` counts all entries.

Both pilot releases expose 541,733 candidate cells across native x86_64 and
AArch64 before Python support filtering or sampling. The bounded proof
projects its single suffix inside Nix before JSON serialization. It does
not serialize that whole matrix for a four-build test.

The development-shell dry run estimated 547.5 MiB downloaded and 2.6 GiB
unpacked. All six inventoried provider source trees total about 683 MiB;
some were already cached. Native/cross hello prerequisites were cached except
for approximately 2 MiB of tarballs and a small historical fetch environment.
The task uses the normal daemon with one job and one core, not a hard quota.
Concurrent system-wide disk growth cannot be attributed to this task. Final
owned-output sizes, retained roots and targeted cleanup are recorded below.

## Final verification and cleanup

The complete regression run passed 1,581 tests, with 7 skipped and 17 live
Nix/SLURM tests deselected. The separate bounded Nix proof ran successfully.
All pure Nix checks passed, including both native/cross Clang package drv
evaluations and the metadata-context check. Ruff passed for the new modules,
the changed identity/planner code and the standalone flow script.

Both hello versions passed one combined native/AArch64 run through real
evaluation, distinct archives, graph planning, streamed descriptors, package
builds and the framework's real atomic publication API. The final graph has
four variants, nine deduplicated dependency tasks and zero violations. All
four variants depend on the correct target/compiler task; only two compiler
task IDs are needed. Native ELFs report their exact GNU Hello releases.
ELF machine values are 62 for x86_64 and 183 for AArch64. Checked resume for
each published variant invokes no Nix command, and stale release identity
does not satisfy completion. The final publication pass reused the real
package builds from the preceding build pass and took 51.82 seconds.

The four raw package outputs total 959,446 bytes; the four ELF folders total
8,336 NAR bytes. Pre-cleanup owned scratch was 9,569,417 bytes. After cleanup
the task directory used under 0.3 MiB of allocated blocks, including the
two native publications, ledger and short verification logs. The two native
pilot results remain rooted under `.multiversion-work/roots/`. The dev shell
and the two selected provider inputs remain rooted as reusable prerequisites.
Cross results, initial provider-probe results and a temporary diagnostic tool
were unrooted and deleted through `nix store delete`: eight owned paths,
with no blocked deletions. Matrix archives, copied cross ELFs, temporary
scripts, bytecode, pytest caches and the task's `/tmp` dry-run file were
removed. No shared-store GC or cleanup of another worktree was run.

Registration-time accounting found 3,389,480,760 NAR bytes across 313 new
paths in the development environment's build graph. The retained task
prerequisite/result closures contain 882,709,296 NAR bytes registered during
the task. These sets overlap and can include acquisitions shared with other
active work. Both measurements remain within the 10 GiB allowance. No C/C++
compiler or bootstrap was built. Global free space fell from 623,883,640,832
to 562,139,631,616 bytes during concurrent work; that system-wide delta is
not exclusive task growth. The free-space reserve stayed above 100 GiB.

The machine-readable [verification record](multiversion-verification.json)
contains the inventory, exact pins/hashes, catalog, compiler and package
derivations, native version output, ELF headers, graph counters and cleanup
ledger. The copied plan's SHA-256 is
`ae063b4fbc0c079c0b0ae52ec92870f757fea19b9be2d7cd729f5e1bd6a501d5`.

## Capture integration

The baseline has no compiler-capture implementation. The root-only interfaces
were inspected read-only. Integration must resolve the package before applying
`resolve-toolchain.nix` and the capture output adapter in `mkVariant.nix`;
preserve `datasetCapture` and output roles in `mkBinaryFolder.nix`; expand
`datasetCaptured` and capture metadata by package ID; and require both release
identity and the existing capture contract during Python completion checks.
Capture-enabled two-release builds remain pending until those changes are
committed and integrated. No capture-enabled test is claimed here.

The pending merge points are:

- In `lib/mkVariant.nix`, resolve the package before the root thread's
  `resolve-toolchain.nix` and capture adapter. Merge `lib/matrix.nix` and
  `flake.nix` so captured namespaces use the same catalog IDs/provenance.
  In `lib/mkBinaryFolder.nix`, retain output roles, debug and `datasetCapture`
  while preserving the informational drv path's discarded Nix context.
- In Python preflight/cache/manifest and streamed descriptor construction,
  retain both the catalog token and the capture contract. Extend the shared
  task identity with the existing capture namespace/policy where required.
  In build-worker and capture publication/completion, require both package
  provenance/actual drv and the existing capture validation contract. Capture
  validation alone must not satisfy package-version completion.
- Run both releases with native GCC 15 and Clang 21 capture, plus the bounded
  AArch64 captured pair. Check every required ELF/debug/capture association,
  exact source/recipe metadata, and resume when either the release or capture
  policy changes. These tests require the other thread's actual committed
  implementation; none of its uncommitted files were copied or edited here.

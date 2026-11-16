# Evaluation performance

This follow-up optimizes the multiversion implementation at `5e968fc` without
changing Nix, nixpkgs pins, compiler selection, legacy dataset paths or package
derivations. The installed evaluator is upstream Nix 2.34.9.

## Upstream work examined

[Nix structural attribute-set sharing, PR 13987](https://github.com/NixOS/nix/pull/13987)
reduces copying when merging attribute sets. That evaluator change is already
part of the installed Nix generation. The application-level lesson is to bind
shared computations once, rather than repeatedly call a function for each
compiler/flag cell. Package resolution and provenance now have shared bindings
per package and target, and compiler-axis lists are shared across optimization
levels.

[Eelco Dolstra's parallel-evaluation article](https://determinate.systems/blog/parallel-nix-eval/)
explains how evaluated thunks are reused. Its parallel evaluator requires
different evaluator support, so this change does not depend on it. Instead,
the runner stops requesting and serializing values that its sample will discard.
More evaluator threads would not remove that unnecessary work.

[Determinate lazy trees](https://docs.determinate.systems/determinate-nix/lazy-trees/)
defer source copying and access. This project cannot reproduce that engine
feature in Nix expressions, but it can avoid touching irrelevant providers and
target package sets. Poison-value tests prove that native default selection
and direct modern-compiler lookup do not force those values.

[nix-eval-jobs](https://github.com/NixOS/nix-eval-jobs) already supplies parallel
evaluation and streamed results. The new selector feeds it only the requested
derivations, instead of making it discover a large flattened matrix first.
The existing evaluator and daemon configuration remain unchanged.

## Implementation

Release selection first reads the cheap family/default index. Exact-version
requests fetch only version/recipe fields for matching families. Full provenance
and availability are then evaluated for selected IDs and requested targets.
Default native hello selection therefore leaves the historical provider and
foreign target package sets untouched.

The Nix matrix filters each independent axis before expanding products and
counts products arithmetically. `_variantAxes.<system>.<arch>` returns compact
compiler/optimization groups with their allowed flags, hardening, sanitizer
and march labels. Package-specific support rules still run in Python. Full
suffix strings are sorted before seeded sampling, preserving prefix cases
such as `ssp` and `ssp-all` and the exact previous choices.

`_selectDataset.<system>.<packageId>` resolves requested suffixes directly.
Modern GCC/Clang lookup bypasses unrelated compiler discovery. Legacy flat
outputs remain compatible, but still need to construct their flat key set;
the optimized runner uses the new function instead. Requested and legacy
paths produce identical derivation paths.

Compact inventories are cached under the matrix evaluation directory and
shared by all package releases. The token hashes evaluation-relevant files,
the lockfile, actual resolved input identities and system. Actual inputs cover
overrides made without writing the lockfile. It excludes documentation and tests. Hashing the
whole checkout would incorrectly distinguish the submitter from the filtered
container source. A real Nix test proves their tokens match. Cache entries
check schema, source identity, architecture, duplicate groups/labels, candidate
bounds, byte limits and checksums. Atomic writes tolerate concurrent producers.
Workers disable the shared SQLite evaluation cache for these small queries.

The direct selector checks the preflight token before instantiation. Workers
then read actual local derivation metadata and check release/target provenance.
Prestaged matrix maps also bind the token, system, architecture set, suffix
limits, sample size/seed and toolchain aggregate. Package provenance alone
cannot prove that a prior sample still matches the request.

The fast path explicitly rejects other dataset namespaces rather than silently
switching captured requests to uncaptured outputs. Capture integration remains
dependent on the separate implementation described in the multiversion report.

## Measurements and verification

Benchmark results and final verification are recorded in
`evaluation-performance.json`. Benchmarks interleave three runs of the prior
and optimized source snapshots on the same shared host, with the same inputs
and evaluator. Selected-variant measurements disable Nix's persistent eval
cache. File-system caches are warm; these are observed timings, not guarantees.

| Operation | Before | Optimized | Speedup | Peak RSS before / after |
| --- | ---: | ---: | ---: | ---: |
| Default native hello inventory | 4.44 s | 1.25 s | 3.6x | 618 / 113 MiB |
| One selected GCC 15 derivation | 4.60 s | 0.43 s | 10.6x | 585 / 114 MiB |

The selected-derivation comparison uses the optimized runner API after the
change. The traditional flat path is preserved and still constructs its key
set. All six measured derivation paths are identical across before/after runs.

Run the reproducible benchmark with:

```sh
nix develop -c python tests/evaluation-benchmark.py --baseline 5e968fc --repeats 3
```

The full regression suite passed 1,601 tests, with 7 skipped and 17 live
Nix/SLURM tests deselected. The separate bounded two-release native/AArch64
flow passed real evaluation, atomic publication and checked resume. It produced
four variants, nine deduplicated dependency tasks and no planner violations.
All four compiler prerequisites are wired to the two actual compiler tasks.
The two releases shared exactly two current inventory cache entries, one per
target. Both real upstream sources and versions remain distinct.

The real sampling parity check compares 2,400 GCC 15/O2 candidate names and
2,340 supported candidates against the legacy metadata path, across eight
seed/sample-size combinations. The results are identical. Synthetic tests also
cover empty axes, oversized products, invalid schemas, corrupt cache entries,
source changes, stale prestaged samples, namespace isolation and unsafe temporary
path interpolation. A real Nix selector test passes spaces, Unicode and literal
`${...}` in its temporary directory name.

Cleanup removed both benchmark source snapshots, temporary selector files and
four owned cross-result store paths through targeted Nix deletion. The two
native pilot results remain rooted. Owned scratch fell from about 21 MiB to
under 0.6 MiB, including the two current shared inventory entries and short
logs. No compiler bootstrap, shared-store GC, root-checkout edit, push or merge
was performed. The machine-readable record includes cleanup details.

Full catalog listings across all targets still import the requested provider
contexts. Historical compiler fallback can still discover historical compiler
sets. Full unsampled runs still enumerate and instantiate all requested cells.
These costs remain explicit; the change does not claim constant-time evaluation
for every public flake output.

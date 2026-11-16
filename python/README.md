# compiler_suit_runner

The Python runner builds the [asm-dataset-nix](../README.md) ELF corpus
locally or on SLURM through
[dynamic-runner](https://github.com/sirati/dynamic-runner). It evaluates
package matrices, shares prerequisite Nix closures, and publishes
per-variant ELF files and JSON sidecars.

## Development environment

From the repository root:

```sh
nix develop
export PYTHONPATH="$PWD:$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
compiler-suit-runner submit --help
```

The shell provides Python 3.13, pytest, and the dynamic-runner revision
selected by `flake.lock`. The export adds the repository root for
`template_graph` alongside the Python packages.
Use this environment to keep the runner and framework versions aligned.

For an editable installation outside Nix:

```sh
pip install -e 'python/[test,slurm]'
```

The `slurm` extra installs dynamic-runner from its Git repository rather
than the flake pin. The base package has no declared runtime dependencies,
but submitting runs requires dynamic-runner. Nix, `nix-eval-jobs`, and the
transport/container tools are separate system dependencies. Optional
Cachix uploads require the `cachix` executable; the `cachix` extra does
not install it.

## Execution phases

One `SuitTask` declares four framework phases:

| Phase | Work |
|-------|------|
| `build_compilers` | Optional toolchain-build tasks, enabled by `--build-compilers` |
| `matrix_eval` | One task per package evaluates selected architectures and publishes derivation archives and mappings |
| `dependency_graph` | One primary-pinned task combines the package graphs and streams build descriptors |
| `build` | Shared-dependency builds, variant builds, and per-node affine import gates |

The build phase has no full-phase barrier. Tasks can run as the planner
streams them, once their individual dependencies and import gates are
satisfied. Matrix-evaluation and dependency-graph tasks are created in
memory; build tasks are spawned at runtime. The old partition/merge
workers remain in the source tree but do not describe this pipeline.

Preflight runs on the submitter. It resolves and realizes toolchain
prerequisites, retains build-time closures, and prepares common and
per-toolchain archives. This can build missing toolchains locally even
without `--build-compilers`. That flag enables additional in-cluster
compiler-build tasks.

## Local run

From the repository root, inside `nix develop`:

```sh
compiler-suit-runner submit \
  --flake . \
  --multi-computer single-process \
  --packages hello --archs x86_64 \
  --variant-sample 1 \
  --jobs 1 --cores 1 --build-max-concurrent 1 \
  --shared-fs /tmp/csr-smoke
```

This selects one sampled combination per compiler, architecture, and
optimization group. It is not a one-build smoke test. The default
`--variant-sample` is 2; `--variant-sample 0` disables sampling.
`--variant-seed` controls deterministic sample selection. Use a direct
`nix build` as shown in the root README to test only one variant.
`--max-variants` is currently a deprecated no-op.

## Opt-in compiler capture

Add `--compiler-capture` to `submit` to select captured variants and their
matching toolchain archives. Capture has separate cache and publication
identities. Existing ELF-only results do not satisfy a captured request.
Secondary workers inherit the selection from the submitter.

First validate a single native build with the root README's
[`capture-example` workflow](../README.md#compiler-artifact-capture).
Adding `--compiler-capture` to a sampled runner command still considers every
selected compiler and optimization group. Expand package, target and worker
counts only after inspecting a small set of explicit captured variants.

Published captured variants contain their ELFs, `auxiliary/datasetCapture/`,
required separate debug output under `auxiliary/debug/`, selected build facts
under `auxiliary/provenance/`, and `capture-complete.json`.
Run `nix run .#capture-inspect -- /path/to/published/variant` from the repository
to verify the completion record, file hashes, execute permissions, compiler
contract, selected compiler/target identity, installed ELF associations, and
debug information. A required capture failure blocks publication. Revision-2
captured directories use `_capture-v2`, task IDs use `__capture_v2`, and matrix
archives default to `_matrix_eval_capture_v2`. Revision-1 captures remain
inspectable but cannot satisfy a new captured request. See the
[compatibility contract](../docs/compiler-capture-compatibility.md) for supported
modes and resource limits.
List curated releases, then select both hello releases explicitly:

```sh
python -m compiler_suit_runner catalog --packages hello --json
python -m compiler_suit_runner preflight \
  --package-versions hello=2.12.1 hello=2.12.3 --archs x86_64 aarch64
python -m compiler_suit_runner submit \
  --flake . --multi-computer single-process \
  --package-versions hello=2.12.1 hello=2.12.3 --archs x86_64 \
  --variant-sample 1 --jobs 1 --cores 1 --build-max-concurrent 1 \
  --shared-fs /tmp/csr-two-releases
```

The two-release command still samples every supported compiler/optimization
group. For the bounded GCC 15 native/AArch64 proof, use
`nix develop -c python tests/multiversion-flow.py` from the repository root.
It builds four small variants with one job and one core, mocks scheduler
transport, and exercises real Nix evaluation, archives, graph planning,
streamed descriptors, worker staging and resume.

`--packages` names families and selects defaults unless version options are
present. `--package-versions FAMILY=EXACT_VERSION` accepts multiple values
and repeated options. Explicit versions replace that family's default and
imply their family when no family filter is present. Other selected families
keep their defaults. Unknown/conflicting selectors fail before toolchain
work. Ambiguous versions list candidate recipes; use `--package-ids ID` to
choose one. `--all-package-versions` selects the curated entries of the
selected families. It never fetches a release inventory from the network.

Both releases use identical sampled compiler/flag suffixes where their
supported cells agree. Selection ordering, duplicates, and adding a second
release do not change the first release's sample. Priority follows the
explicit family metadata, including for versioned IDs.

New matrix workers sample a compact, package-independent `_variantAxes`
inventory, then call `_selectDataset` to instantiate the requested suffixes.
They check the resulting derivations' authoritative release metadata before
exporting archives. This preserves the legacy sampling choices and output
derivations without serializing full per-variant provenance during sampling.
Older flakes without the inventory capability retain the legacy worker path.

Inventory files under `_matrix_eval/_variant_inventory/` are shared across
release workers. Their identities cover the system, pins and evaluation code;
the identity is equal in the submitter's checkout and the filtered worker
image. Cached files have bounded size, schema/target checks and a checksum.
Incomplete or corrupt files are regenerated. Source changes fail before
instantiation and require fresh preflight. Worker queries disable Nix's
shared SQLite eval cache to avoid concurrent write contention.

## SLURM run

```sh
compiler-suit-runner submit \
  --flake . \
  --multi-computer slurm \
  --packaging podman \
  --gateway ssh://user@head \
  --slurm-root-folder /shared/slurm \
  --jobs 4 \
  --build-max-concurrent 2 \
  --packages hello --archs x86_64 aarch64
```

The framework owns the gateway, packaging, SLURM resource, and retry
arguments. Inspect `submit --help` for the options supported by the
pinned framework. See the [SLURM runbook](../dynrunner/SLURM_RUNBOOK.md)
and [operations notes](../dynrunner/LMU_OPERATIONS.md) for deployment.

## Closure transport

Toolchain archives and per-package matrix derivation archives use affine
import gates. Each gate runs in a worker subprocess once per secondary
node before dependent builds run there. Toolchain archives split common
paths from compiler-specific paths to avoid repeatedly shipping the same
closure. They include build-time prerequisites as well as runtime paths.

* `--build-deps-local` also exports and imports the realized output closure
  of variant build inputs. `CSR_BUILD_DEPS_LOCAL=1` enables it too.
* Common-dependency output imports are affine by default.
  `--no-common-deps-affine` restores peer-substituter transport for them.
* `--no-substituters` forces build-dependency preloading and disables
  harmonia and substitution for the variant/common-dependency build path.
  Fully affine mode, with build-dependency preloading and common-dependency
  affine imports enabled, implies this behavior too.
* `--no-toolchain-dedup` restores full per-package archives instead of
  subtracting the separately shipped toolchain closure.

Substituter-free worker builds do not make preflight offline. The
submitter can use public caches and build missing prerequisites while
preparing archives. Variant/common-dependency workers check before
building that imported prerequisites will not need rebuilding or fetching.

## Output layout

A dataset derivation in the Nix store has this shape:

```text
<store-output>/
├── elf/<name>            symlink to a unique package ELF
└── meta.json             Nix-side variant metadata
```

The worker dereferences each ELF into staging and returns explicit files
for framework publication. The dataset output root contains:

```text
<dataset-root>/
├── <packageId>/
│   ├── <variant-dir>/
│   │   ├── <elf-name>    copied ELF bytes
│   │   ├── metadata.json
│   │   └── package-complete.json
│   └── <variant-dir>.json
└── _matrix_eval/         evaluation archives and intermediate metadata
```

Local runs default to `<shared-fs>/dataset/`. Container workers publish
through the framework's output mount, `/app/out-network`; the host output
location follows the framework run configuration. New sidecars carry the
Nix build's checked family, exact version, immutable release ID, recipe pin,
source identity and canonical package identity, plus actual package/variant
derivations, output paths and compiler axes. A metadata mismatch fails the
worker. The framework publishes the ELF files, metadata and completion record
as one atomic set. Completion verifies release/derivation identity and file
hashes; partial, legacy, stale or damaged results rebuild.
New variant directory hashes include the package identity and actual
derivation. Updating a default alias therefore cannot mix old and new ELF
file sets in one directory.

Captured builds validate their provenance, compiler artifacts, preprocessing
artifacts, and separate debug associations before publication. The captured
variant path is a relative symlink to a complete generation. Its adjacent JSON
sidecar links into that same generation. A retry transfers and validates a
hidden generation before replacing the visible link, so removed files cannot
survive from an earlier result. The publisher retains the current and previous
generations. Resolve the variant link once before reading its indexed files.
See the [publication format](../docs/compiler-capture-format.md) for the
generation layout and split-DWARF companion lookup.

Variant outputs are folders and sidecars. Old partition files and phase-sentinel
paths are not the completion contract for this pipeline.

## Incremental cache and evaluation reuse

The submitter caches enumeration results under:

```text
~/.cache/compiler_suit_runner/
├── toolchains/<key>/entry.json
└── variants/<key>/entry.json
```

Keys account for repository/input state, canonical selected package IDs and
resolved catalog provenance. Changing a default release, source or recipe
invalidates the entry even when its alias and CLI options stay unchanged.
A hit reuses enumeration results; manifests, stage selection,
and dispatch configuration are rebuilt for the current invocation.
Unknown cache versions or malformed entries are treated as misses.
Use `--no-cache` to bypass this cache or `--cache-root` to select another
location.

`--prestaged-matrix-eval` skips matrix evaluation and reuses archives and
per-package derivation-map sidecars from a prior run. Its intended input
is the prior evaluation output directory. Release provenance must match the
resolved catalog; missing, legacy or stale sidecars fail and require fresh
matrix evaluation. Only selected package IDs are reused. This is separate
from the local enumeration cache.
For compact-inventory runs, prestaged maps also check evaluation inputs,
architectures, suffix limits, sample size/seed and the toolchain aggregate.

## Cachix

`--cachix-cache NAME --cachix-auth-token-file PATH` enables optional
toolchain uploads. It requires the Cachix CLI. Published dataset ELF
folders are not uploaded by this integration.

## Module map

Paths below are relative to `python/compiler_suit_runner/`.

| Module | Responsibility |
|--------|----------------|
| `cli.py` | CLI, preflight, archive preparation, and framework dispatch |
| `preflight.py` | Toolchain and variant enumeration, source/input identity |
| `incremental_cache.py` | Versioned local enumeration caches |
| `suit_task.py` | Phase topology, discovery, streamed spawn, and lifecycle hooks |
| `manifest_gen.py` | Task identities, manifest payloads, and serialization |
| `dependency_graph_planner/` | Shared dependency and variant planning |
| `streamed_spawn.py` | Build-descriptor messages and reconciliation |
| `workers/eval_worker.py` | Per-package matrix evaluation |
| `workers/dependency_graph_worker/` | Combined graph planning and closure archives |
| `workers/affine_import_worker.py` | Per-node archive imports |
| `workers/build_compilers_worker.py` | Optional in-cluster toolchain builds |
| `workers/build_worker.py` | Worker entry point, dependency/variant builds, and ELF staging |
| `peer_cache.py`, `peer_paths*.py`, `peer_push.py`, `peer_replication.py` | Peer transport and placement tracking |
| `cachix_uploader.py` | Optional toolchain uploads |

## Testing and limitations

From the repository root:

```sh
nix develop -c env PYTHONPATH=.:python pytest -c python/pyproject.toml \
  python/compiler_suit_runner/tests/ python/ssh_debug_runner/tests/
```

The default pytest configuration excludes live Nix and SLURM tests.
Add `-m nix` or `-m slurm_live` to select those checks with the required
infrastructure. Both the repository root and `python/` must be importable;
the command above sets those paths explicitly. Unit tests cover CLI behavior,
cache handling, phase topology, streamed planning, closure import,
publication, and failures. They do not establish cluster correctness or
compiler compatibility on their own.

The runner remains experimental. Large matrices can exhaust memory under
aggressive concurrency. `--build-max-concurrent` caps build-heavy tasks;
select resources and framework retry settings for the target cluster.
Compiler/target compatibility constraints and known exclusions reduce
expected failures, but do not guarantee every package variant builds.

## License

MIT. See the repository's [LICENSE](../LICENSE).

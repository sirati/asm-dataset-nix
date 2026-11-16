# asm-dataset-nix

A Nix flake that builds a matrix of cross-compiled ELF binaries for a
decompiler and disassembler training corpus. Each variant selects:

* A compiler family and version, including GCC 4.4 through 15 and Clang 3.4 through 22.
* A curated upstream package release and pinned recipe.
* A target architecture, including x86_64, i686, ARM, PowerPC, MIPS, and RISC-V.
* An optimization level and code-generation flag set.
* A hardening profile, sanitizer mode, and CPU instruction-set level.

Compiler and target constraints exclude known unsupported combinations.
The exact axes are defined in [lib/architectures.nix](lib/architectures.nix)
and [lib/flags.nix](lib/flags.nix). The curated package catalog is in
[lib/packages.nix](lib/packages.nix).

Each dataset derivation produces an `elf/` folder of symlinks to unique
ELF files across ordinary outputs of the built package, plus `meta.json`.
Captured artifacts and separate debug outputs have their own roles under `auxiliary/`.
The collector deduplicates aliases to the same file and resolves basename
collisions. See [lib/mkBinaryFolder.nix](lib/mkBinaryFolder.nix).
The runner dereferences these symlinks when publishing the dataset, so
published files do not require the originating Nix store. These files
are a corpus, not an installed, runnable package layout.

## Repository layout

```text
flake.nix                     flake outputs and input pins
lib/                          package, compiler, target, and matrix definitions
nix/                          container images, capture packages and checks
rust/compiler-capture/        shared compiler artifact supervisor
tests/compiler-capture/       capture regression tests and bounded experiments
docs/                         design notes and compiler compatibility findings
python/
├── pyproject.toml            Python package metadata
├── README.md                 runner usage and architecture
├── compiler_suit_runner/     local and SLURM dataset runner
└── ssh_debug_runner/         interactive container debugging
```

## Flake outputs

| Output | Purpose |
|--------|---------|
| `dataset.<sys>.<pkg>.<arch>.<variant>` | Per-variant ELF folder derivation |
| `datasetCaptured.<sys>.<pkg>.<arch>.<variant>` | Opt-in ELF folder with required compiler artifacts and debug information |
| `packages.<sys>.capture-example` | One native `hello` build with capture enabled |
| `apps.<sys>.capture-inspect` | Validate and summarize captured artifacts and installed ELF associations |
| `checks.<sys>.compiler-capture-validation` | Fast capture validation, inspection, and ELF collector tests |
| `_meta.<sys>` | Matrix metadata without instantiating variant derivations |
| `_drvPaths.<sys>` | Derivation paths for the matrix |
| `_packageCatalog.<sys>` | Release IDs, exact versions, recipe/source identity, defaults, target availability |
| `_packageVariantCounts.<sys>` | Variant count estimates before Python support filtering and sampling |
| `_rawPackages.<sys>.<packageId>.<arch>.<variant>` | Compiled package before ELF collection |
| `crossToolchains.<sys>` | Cross-toolchain definitions |
| `_crossToolchainMap.<sys>` | Toolchains indexed for runner preflight |
| `_crossToolchainsMeta.<sys>` | Toolchain metadata |
| `dockerImage.<sys>` | Container image for the runner |
| `apps.<sys>.generate-manifest` | Matrix manifest generator |
| `devShells.<sys>.default` | Python, pytest, dynamic-runner, and development tools |

The nested dataset lets callers select one variant without evaluating
all variant derivations. Matrix size depends on the selected packages,
targets, and supported compiler/flag combinations.

## Building a variant

List the available variant names for one package and architecture:

```sh
nix eval --json '.#_meta.x86_64-linux.hello.x86_64' --apply builtins.attrNames
```

Build a baseline GCC variant with hardening and sanitizers disabled:

```sh
nix build '.#dataset.x86_64-linux.hello.x86_64.gcc15-O2-baseline-none-san-off-march-default' \
  --max-jobs 1 --cores 1
ls result/elf
cat result/meta.json
```

The suffix encodes compiler, optimization, flags, hardening, sanitizer,
and CPU-target labels. Use the metadata listing for available keys.

## Compiler artifact capture

Start with one unchanged native `hello` package. Review its prerequisites, then
build the example and validate its artifacts:

```sh
nix build --dry-run --no-write-lock-file .#capture-example
nix build --no-write-lock-file --max-jobs 1 --cores 1 .#capture-example
nix run --no-write-lock-file .#capture-inspect -- ./result
nix run --no-write-lock-file .#capture-inspect -- ./result --json
```

The example produces `elf/`, `meta.json`, `provenance.json`,
`auxiliary/datasetCapture/`, and `auxiliary/debug/`. The inspector verifies file
hashes, the selected compiler and target, installed ELF associations, and separate
debug information. It also accepts a raw `datasetCapture` output or a published
variant directory. It needs no runner framework or cluster. The
[publication format](docs/compiler-capture-format.md) describes provenance,
split-DWARF companion lookup, and atomic publication retries.

After the example, select one existing variant under `datasetCaptured` using
the same suffix as `dataset`. The runner enables capture with
`--compiler-capture`; its sampling can still select many compiler versions.
Keep the first rollout to explicit variants before expanding runner jobs.

Capture is experimental and requires a Linux x86-64 builder with user and mount
namespaces. The default dataset keeps capture disabled. Read the
[capture workflow and compatibility contract](docs/compiler-capture-compatibility.md)
for limits, failure handling, and the path to broader use.

Run the fast repository check without building compilers or the runner:

```sh
nix build --no-write-lock-file .#checks.x86_64-linux.compiler-capture-validation
```
## Selecting package releases

The default `hello` key still selects the current pinned nixpkgs release,
2.12.3. A second curated entry selects the full nixpkgs 24.05 recipe for
hello 2.12.1. Its dependencies come from that provider, while the dataset
selects the compiler independently. No additional input or lockfile update
is needed. Inventory the exact IDs and provenance:

```sh
nix develop -c python -m compiler_suit_runner catalog --packages hello --json
nix develop -c python -m compiler_suit_runner preflight \
  --package-versions hello=2.12.1 hello=2.12.3 --archs x86_64 aarch64
```

Add `--package-versions hello=2.12.1 hello=2.12.3` to a runner `submit`
command to compare both releases in one run. Repeated `--package-versions`
options are accepted. Explicit releases replace that family's implicit
default; other selected families retain their defaults. A version selector
implies its family when `--packages` is absent. Conflicting family filters,
unknown versions, and ambiguous recipes fail before toolchain staging.

Without version options, `--packages hello` selects only `hello`, and an
unfiltered run selects only the catalog defaults. `--all-package-versions`
expands the selected families to their curated entries. `--package-ids`
selects exact recipe entries when a version has multiple recipes. Versions
retain their exact spelling; package IDs use a restricted path alphabet.
Quote the whole Nix installable in the shell, and quote Nix attribute names
containing `+`, for example `'.#dataset.x86_64-linux."family+entry".x86_64.…'`.
Families reserve `--` for release IDs and `__` for worker labels.

The existing compiler/flag suffix remains unchanged. Additional releases
use separate package keys in every matrix output and separate publication
directories. Sampling uses the same compiler/target/optimization seed for
each release, independent of selector order. Completion requires matching
release provenance, the actual derivation, and intact published files.
Legacy sidecars without release identity rebuild.

See the [compatibility record](docs/multiversion-compatibility.md) for native
and cross-build evidence, storage accounting, and pending capture integration.

The runner uses a compact compiler/flag inventory shared across releases and
instantiates only the selected variants. Release IDs and target availability
are narrowed before resolving package provenance. Existing flat dataset and
metadata paths remain available. See [evaluation performance](docs/evaluation-performance.md)
for measurements, upstream references and remaining costs.

## Running the dataset builder

The [Python runner](python/README.md) uses
[dynamic-runner](https://github.com/sirati/dynamic-runner) to evaluate
package matrices, plan shared dependencies, and stream builds onto local
or SLURM workers. An optional compiler-build phase precedes matrix
evaluation. Build tasks can start while dependency planning continues.

Toolchain and derivation archives are imported through per-node affine
gates. Common dependencies use affine output imports by default.
Peer substitution through [harmonia](https://github.com/nix-community/harmonia)
is available when the selected transport mode needs it. A fully affine
run disables substitution on variant/common-dependency workers; the
submitter can still fetch or build prerequisites during preflight.

Enter the development shell and run a local sample:

```sh
nix develop
export PYTHONPATH="$PWD:$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
compiler-suit-runner submit \
  --flake . \
  --multi-computer single-process \
  --packages hello --archs x86_64 \
  --variant-sample 1 \
  --jobs 1 --cores 1 --build-max-concurrent 1 \
  --shared-fs /tmp/csr-smoke
```

Sampling applies per compiler, architecture, and optimization group;
this command can still build many variants and toolchains. The default
sample is two combinations per group. Use `--variant-sample 0` for the
full matrix. See the runner README for SLURM setup, published output,
transport switches, and cache behavior.

## Documentation

* [Runner usage and architecture](python/README.md).
* [Capture publication format](docs/compiler-capture-format.md) and [follow-up verification](docs/compiler-capture-followup-verification.json).
* [SLURM runbook](dynrunner/SLURM_RUNBOOK.md) and [operations notes](dynrunner/LMU_OPERATIONS.md).
* [Old GCC cross-compilation](docs/old-gcc-cross-compilation.md) and [GCC 4.4–4.6 notes](docs/old-gcc-44-45-46-cross.md).
* [Known compiler and target issues](docs/known-issues.md).
* [Support matrix](support_matrix.md), [flags](flags.md), and [failure snapshot](failures.md).

Historical design notes and result snapshots can lag the current code.
The compiler/target definitions and current CLI help describe the active
configuration.

## Development

```sh
nix develop
PYTHONPATH=.:python pytest -c python/pyproject.toml \
  python/compiler_suit_runner/tests/ python/ssh_debug_runner/tests/
```

The development shell supplies the pinned dynamic-runner dependency and
sets `PYTHONPATH` for the packages under `python/`. The command above also
adds the repository root for `template_graph`. Runtime package code
uses the Python standard library plus dynamic-runner for orchestration;
tests require pytest. The default pytest configuration excludes live
Nix and SLURM tests. Run those explicitly with `-m nix` or `-m slurm_live`
when their infrastructure is available.

## License

MIT. See [LICENSE](LICENSE).

# asm-dataset-nix

A Nix flake that builds a multi-hundred-thousand variant matrix of
cross-compiled ELF tarballs for use as a decompiler / disassembler
training corpus. Every build runs the same curated package set under
a different combination of:

* compiler family + version (GCC 4.4 through 15, Clang 3.4 through 22),
* target architecture (x86, x86_64, aarch64, armv7l, ppc64, riscv64),
* optimization level (`-O0` through `-Os`, `-Ofast`, `-Og`),
* code-gen flag set (PIC / no-PIC, frame pointer, LTO, ...),
* hardening profile (default vs. all-disabled).

Each variant's output is a flat `.tar.zst` containing only the ELF
binaries the derivation produced, with no surrounding filesystem
layout — see `lib/mkBinaryTarball.nix`. The matrix is exposed as
`dataset.<system>.<package>.<arch>.<variant>` so callers can pick
slices without forcing the whole tree.

## Repository layout

```
flake.nix                     entry point; wires everything below
lib/                          modular Nix library (~387K-derivation matrix)
nix/
└── docker-image.nix          layered podman image for the runner
docs/                         design notes; cross-compilation caveats
python/
├── pyproject.toml            compiler_suit_runner package metadata
├── README.md                 runner-specific usage docs
└── compiler_suit_runner/     SLURM-aware driver (see below)
```

## Flake outputs (overview)

| Output                            | What it is                                      |
|-----------------------------------|-------------------------------------------------|
| `dataset.<sys>.<pkg>.<arch>.<v>`  | Per-variant ELF tarball derivation              |
| `_meta.<sys>`                     | Pure-eval metadata (no derivation instantiation) |
| `_drvPaths.<sys>`                 | Drv paths for the full matrix                   |
| `crossToolchains.<sys>`           | Compiler closures by `(arch, compiler)` pair    |
| `_crossToolchainMap.<sys>`        | Same set, indexed for SLURM pre-build           |
| `_crossToolchainsMeta.<sys>`      | Toolchain metadata (no instantiation)           |
| `dockerImage.<sys>`               | Layered podman image bundling the runner        |
| `apps.<sys>.generate-manifest`    | Dump the matrix manifest to JSON                |
| `devShells.<sys>.default`         | Dev environment (basedpyright, nil, nixd, ...)  |

Run `nix flake show` for the full tree.

## Building the matrix

A single variant:

```
nix build '.#dataset.x86_64-linux.hello.x86_64.gcc15-O2-default'
```

The full matrix is too large for one machine. Two strategies are
supported:

1. **Local slice.** Pick a `(pkg, arch, variant)` tuple via
   `nix build`; pulls the relevant cross-toolchain on demand.
2. **SLURM cluster.** Use the bundled
   [`compiler_suit_runner`](python/README.md) driver to package the
   build into a single SLURM submission with peer-to-peer Nix store
   sharing. See `python/README.md` for usage.

## Cluster-side runner: `compiler_suit_runner`

For driving the full matrix on a cluster, this repository ships a
Python package — `compiler_suit_runner` — under `python/`. It plugs
the matrix into the [`dynamic-runner`][dynrunner] framework, runs a
three-phase pipeline (partition → toolchain build → variant build)
inside a single SLURM submission, and uses [`harmonia`][harmonia] to
share Nix store paths between secondaries on the cluster. Optional
Cachix federation pushes toolchain closures out to a public cache.

Quick local smoke test (no cluster):

```
pip install -e python/
compiler-suit-runner submit \
  --flake . \
  --multi-computer single-process \
  --packages hello --archs x86_64 \
  --shared-fs /tmp/csr-smoke
```

Full SLURM usage, output layout, the incremental cache, and the
runner's module map are documented in
[`python/README.md`](python/README.md).

[dynrunner]: https://github.com/sirati/dynamic-runner
[harmonia]: https://github.com/nix-community/harmonia

## Documentation

* [`python/README.md`](python/README.md) — runner usage, phases, CLI.
* [`docs/dynamic_runner_pinning_requirements.md`](docs/dynamic_runner_pinning_requirements.md)
  — scheduler-side worker-pinning request to the upstream framework.
* [`docs/old-gcc-cross-compilation.md`](docs/old-gcc-cross-compilation.md)
  and [`docs/old-gcc-44-45-46-cross.md`](docs/old-gcc-44-45-46-cross.md)
  — bootstrap notes for pre-`pkgsCross` GCCs.
* [`docs/known-issues.md`](docs/known-issues.md) — compiler / target
  combinations that don't build, with root-cause notes.
* [`support_matrix.md`](support_matrix.md), [`flags.md`](flags.md),
  [`failures.md`](failures.md) — current-state snapshots of the
  matrix's coverage.

## Development

Enter the dev shell:

```
nix develop
```

Re-run the Python test suite (the runner's tests are stdlib-only and
finish in seconds):

```
nix-shell -p 'python313.withPackages (ps: [ ps.pytest ])' \
  --run 'cd python && PYTHONPATH=. pytest compiler_suit_runner/tests/'
```

See `python/README.md` for runner-specific contributor notes.

## License

MIT — see [`LICENSE`](LICENSE).

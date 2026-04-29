# compiler_suit_runner

Cluster-side runner that drives the [asm-dataset-nix][repo] matrix on SLURM
via the [`dynamic-runner`][dynrunner] framework. A single
SLURM submission orchestrates ~300K Nix derivations across three phases —
partition, toolchains, variants — with peer-to-peer Nix store sharing and
optional Cachix federation, so a fresh cluster cold-boot converges to a full
dataset without per-worker rebuilds.

[repo]: https://github.com/sirati/asm-dataset-nix
[dynrunner]: https://github.com/sirati/dynamic-runner

## Status

Alpha. Single-cluster tested. Smoke-tested locally in
`--multi-computer single-process` mode. Expect rough edges around
heterogeneous-cluster networking and Cachix throttling.

## Architecture

Three phases per submission, all coordinated by one `SuitTask`
`TaskDefinition` and a single `dynamic_runner.run(...)` call:

```
                          one SLURM submission
   ┌─────────────────────── primary host ────────────────────────┐
   │  compiler-suit-runner submit                                 │
   │   1. preflight: emit phase-1a manifests, hash inputs         │
   │   2. dynamic_runner.run(SuitTask())                           │
   │      └─ builds image, sbatch, primary loop                   │
   └──────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌────────────────── compute node (one job, host net) ─────────┐
   │  podman run … python -m compiler_suit_runner --secondary    │
   │   ├── harmonia :5000 (serves /nix/store HTTP)               │
   │   ├── peer-list watcher (peers/*.json on shared FS)         │
   │   └── workers (memory-budgeted by Rust scheduler)           │
   │        ├── partition_worker     (phase 1a)                  │
   │        ├── merge_worker         (phase 1b)                  │
   │        ├── barrier_worker       (phase 1a/1b/2 barriers)    │
   │        └── build_worker         (phase 2 toolchains, then   │
   │                                  phase 3 variants)          │
   └─────────────────────────────────────────────────────────────┘

   shared FS: peers/, partition/, flags/, dataset/
```

* **Phase 1 — partition.** Pure-eval shards of the matrix run in parallel
  (`partition_worker`), then a single-machine `merge_worker` produces a
  global `partition.json`. A `barrier_worker` gates the next phase on a
  sentinel file under `flags/`.
* **Phase 2 — toolchains.** `build_worker` builds the unique
  `(stdenv, host-deps)` set; results are pushed into the local Nix store
  and announced to peers via harmonia + Cachix (if configured).
* **Phase 3 — variants.** `build_worker` runs again, now keyed by the full
  `(pkg, arch, compiler, opt, flags, hardening)` tuple. Variant tarballs
  land in `<shared-fs>/dataset/`.

## Installation

Editable install, runtime only (single-process / local smoke):

```
pip install -e python/
```

With the SLURM bridge (pulls `dynamic-runner` from its own flake):

```
pip install -e 'python/[slurm]'
```

With test dependencies:

```
pip install -e 'python/[test]'
```

The `cachix` extra is a documentation placeholder — Cachix integration
shells out to the `cachix` CLI, so just make sure that binary is on
`$PATH` (the project's Nix dev shell already provides it).

## Quickstart — single-process (no cluster)

Validates the entire pipeline on the local machine, no SLURM, no podman:

```
compiler-suit-runner submit \
  --flake . \
  --multi-computer single-process \
  --packages hello --archs x86_64 \
  --shared-fs /tmp/csr-smoke
```

Expected output: a tiny `dataset/` tree under `/tmp/csr-smoke/` with one
or two `.tar.zst` files, plus `flags/phase{1a,1b,2}_done` sentinels.

## Quickstart — SLURM

Production mode. Submits a single SLURM job, packages the worker image
with podman, federates Nix-store traffic over harmonia between peers:

```
compiler-suit-runner submit \
  --flake . \
  --multi-computer slurm \
  --packaging podman \
  --gateway ssh://user@head \
  --slurm-root-folder /shared/slurm \
  --jobs 4 \
  --packages hello --archs x86_64 aarch64
```

Add `--cachix-cache <name> --cachix-auth-token-file <path>` to enable
toolchain federation across clusters (see below).

## Output layout

For a run rooted at `<shared-fs>` (e.g. `/shared/slurm/<run-id>/`):

```
<shared-fs>/
├── dataset/            # phase 3 output: <variant>.tar.zst per build
├── partition/
│   ├── raw/<shard>.json     # phase 1a per-shard partial results
│   ├── partition.json       # phase 1b merged global partition
│   └── cache.json           # incremental partition cache
├── peers/
│   ├── <secondary-id>.json  # one file per live secondary
│   └── __signing-key        # cluster-wide Nix store signing key
└── flags/              # barrier sentinels: phase1a_done, phase1b_done, phase2_done
```

## Cachix federation

Optional. Use `--cachix-cache NAME` together with
`--cachix-auth-token-file PATH` to push **toolchain** Nix store paths
(phase 2 output) to a Cachix cache so other clusters can fetch instead
of rebuild.

Variant tarballs (phase 3 output) are intentionally **not** pushed —
they're large, of mostly local interest, and may carry experiment-
specific labels we'd rather not publish. See the Limitations section.

## Incremental cache

The runner keeps a per-user incremental cache at:

```
~/.cache/compiler_suit_runner/
```

It memoizes the pre-flight partition+manifest emission step, keyed by a
hash of the flake input + selected packages/archs/flags. A cache hit
short-circuits the (expensive) `nix eval` walk of the matrix and prints
`Cache hit: <input_hash>`. Delete the directory to force a recompute.

## Module map

All paths relative to `python/compiler_suit_runner/`.

| Module                       | Responsibility |
|------------------------------|----------------|
| `cli.py`                     | argparse front-end; `compiler-suit-runner` entry point. |
| `__main__.py`                | `python -m compiler_suit_runner` for secondaries inside the container. |
| `preflight.py`               | input hashing + cache lookup before `dynamic_runner.run`. |
| `suit_task.py`               | the single `TaskDefinition` orchestrator (rank-aware). |
| `manifest_gen.py`            | emits queue-item manifest files with rank-encoded sizes. |
| `partition.py`               | phase 1a/1b utilities used by partition/merge workers. |
| `peer_cache.py`              | peer-list watcher, harmonia control, per-build env assembly. |
| `cachix_uploader.py`         | optional cross-cluster cache federation. |
| `incremental_cache.py`       | local pre-flight cache (`~/.cache/compiler_suit_runner/`). |
| `memory_budget.py`           | `tier_of`, `encode_size`, `decode_size` for the rank-in-size scheme. |
| `workers/partition_worker.py`| phase 1a per-shard partitioner. |
| `workers/merge_worker.py`    | phase 1b single-machine global merge. |
| `workers/barrier_worker.py`  | sentinel-file barriers between phases. |
| `workers/build_worker.py`    | phase 2 toolchain builds + phase 3 variant builds. |

## Limitations

* **Worker pinning.** The current `dynamic_runner._native` scheduler does not
  support hard worker-to-task-class pinning. Phase 2 and phase 3 share
  one worker pool; we approximate pinning via the rank-in-size encoding
  (see [`docs/dynamic_runner._native_pinning_requirements.md`][pinning]) but a
  proper scheduler-side feature is still pending.
* **OOM long tail.** A small fraction of variants get OOM-killed under
  aggressive concurrency. Re-run with `--retry-oom` to retry just the
  killed items at lower concurrency, instead of re-running the whole
  matrix.
* **Variant tarballs are NOT pushed to Cachix.** Only phase 2 (toolchain)
  store paths are federated. This is a deliberate scope/privacy choice;
  if you want shared variant outputs, sync `<shared-fs>/dataset/` out of
  band.
* **Single-cluster tested only.** The `--gateway` / multi-cluster
  federation path exists in code but is not yet exercised end-to-end.

[pinning]: ../docs/dynamic_runner._native_pinning_requirements.md

## Testing

```
pytest python/compiler_suit_runner/tests/
```

The test suite is stdlib-only and runs in a few seconds. It covers
size-encoding round-trips, memory tier classification, manifest filename
regex round-trips, incremental-cache determinism, and per-worker unit
behavior.

## License

MIT — same as the parent `asm-dataset-nix` repository. See the
top-level `LICENSE` file.

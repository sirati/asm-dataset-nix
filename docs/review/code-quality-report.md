# Code-quality review of asm-dataset-nix

Date: 2026-11-15. This review covers three code states, all held to the same standard:

| State | Where | Size |
|-------|-------|------|
| main | `72dd7be` | about 30k lines of Python source, 50k lines of Python tests, 4.9k lines of `template_graph`, 5.5k lines of Nix |
| Compiler-capture change | uncommitted in the main checkout on top of `72dd7be` (new files are marked intent-to-add, nothing is staged) | 105 files, +21k lines (6k Rust, 6.5k capture tests, 2.5k runner Python, Nix, docs) |
| Multi-version support | `feat/multiversion-package-support` at `56219bc` (4 commits on `72dd7be`) | 52 files, +4.6k/-0.4k lines |

Detailed findings with file and line evidence are in [findings/](findings/). There are about 180 of them, and each has an ID (N for Nix, P for the runner core, D for the distributed store code, T for `template_graph`, C for capture, M for multi-version). This report groups them, ranks them, and says which ones I checked myself. The improvement plan is a separate document, [code-quality-plan.md](code-quality-plan.md).

## Verdict

The project does hard things and mostly does them carefully. Comments record real failure modes with the actual error text. Writes to shared storage are atomic. Most modules accept an injected subprocess runner. Some small packages are well cut, such as `dependency_graph_planner/`, `dependency_graph_worker/` and `streamed_spawn.py`.

The structure has not kept up with growth. The same six problems show up in every area, on main and in both new lines of work.

1. A handful of files and functions each do many unrelated jobs. Five Python files hold 13.5k lines between them. `SuitTask.on_run_start` is 560 lines, `cmd_submit` is 690 lines, and the new Rust `collect::run` is 875 lines.
2. Contracts have no single owner. Task IDs, archive names, payload dicts, variant metadata, identity names and the capture config schema are each defined in 3 to 11 places, across Python, Nix and Rust.
3. Failure is hidden by default. Broad `except`, `or` defaults in Nix, `tryEval` filters and functions that return `{}` on error turn real faults into quiet degradation. Several confirmed bugs below come straight from this.
4. Dead code and compatibility shims stay alive because tests still cover them.
5. The repository mixes product code with experiments, generated reports, agent transcripts and work orders.
6. Expensive work is repeated at every layer. Nix re-imports package sets per cell and per variant. Python runs one `nix` subprocess per item where one call would do. Captured artifacts are hashed about a dozen times.

Both new lines of work repeat these patterns. They also conflict with each other in 14 files and build two separate publication, completion and provenance designs. That should be settled before either one merges.

## How the review was done

- Five reviewers each read one area in full: the Nix layer, the runner core, the distributed store code with `template_graph` and `ssh_debug_runner`, the capture change, and the multi-version branch. Each wrote a findings file.
- I re-read the code for each high-severity claim used in this report. The defect table marks each item "verified" (I read the code) or "reported" (taken from the area review without a second read).
- One reviewer claim was wrong and is corrected in the findings files. `lib/matrix.nix` defines `rawPkg` with a second `mkVariant` call, but Nix evaluates lazily and nothing forces `rawPkg`, so it costs nothing. It is dead code, not an evaluation cost (N2, C20).
- My own early note that two tracked files were also gitignored was wrong. `git ls-files -ci --exclude-standard` returns nothing.

Checks I ran on the main checkout, capture change included:

| Check | Result |
|-------|--------|
| `ruff check python/` | 91 errors: 38 unused imports, 43 one-line multi-statements (all 43 come from the capture change; a clean `72dd7be` has none), 7 ambiguous names, 3 other |
| `ruff check template_graph/` | 14 errors |
| `pytest` in `python/` from the dev shell | Fails at collection with `ModuleNotFoundError: No module named 'template_graph'`. The dev shell puts only `python/` on `PYTHONPATH` (P38, T1) |
| `pytest` in `python/` with the repo root added to `PYTHONPATH` | 1420 passed, 3 skipped, 17 deselected (live-cluster and live-nix markers) in 131 s |
| `pytest template_graph` | 139 passed, 5 skipped. The default `pytest` configuration never runs this suite |
| `cargo test` (rust/compiler-capture) | 17 passed. Only 2 of them cover `collect.rs` |
| `cargo clippy --all-targets` | No warnings at the default level |

The multi-version reviewer ran the focused suites on that branch (`test_package_versions`, `test_variant_inventory`, `test_preflight`, `test_cli`, `test_incremental_cache`, `test_eval_worker`), and they pass.

## Defects found during the review

These are bugs, not style issues. They came up as consequences of the structural problems, and each one is a small fix.

| ID | Where | Defect | Status |
|----|-------|--------|--------|
| P1 | `python/compiler_suit_runner/workers/build_worker.py:1404` (also `:1441`, `:1480`, `:1572`) | The import-once flag is set before the import runs. If the import fails and the gate is retried on the same worker, the retry returns early and reports success with nothing imported. | verified |
| P3 | `python/compiler_suit_runner/incremental_cache.py:311` | The cache key runs `git diff`, which ignores staged changes. Flakes evaluate staged content, so a staged-only change reuses a stale enumeration. | verified |
| P4 | `python/compiler_suit_runner/cli.py:1709-1714`, `:1863-1868`, `nix_drv_show.py:93-98` | Failed outpath and build-host evals become `{}` or a log line. The split-archive upload documented as "HARD FAIL" is then skipped without notice, and every build node fails later. | reported |
| P5 | `python/compiler_suit_runner/workers/build_worker.py:2564`, `:2711`, `:2741` | `except BaseException` turns the framework's SIGTERM-driven `SystemExit` into `NonRecoverableError`, so a graceful drain cascade-fails dependents. | verified |
| P2 | `suit_task.py:1727`, `dependency_graph_worker/run.py:255-269` | `import_build_deps` has no dependency on the task that writes its archive, and the producer sometimes writes no archive, which the consumer treats as fatal. Depends on framework timing. | reported, plausible |
| P6 | `python/compiler_suit_runner/support_table.py:48-72` | `table.md` is resolved from the working directory. If it is missing, the filter allows everything without a log line. On the cluster the working directory is `/app`. | reported |
| P8 | `python/compiler_suit_runner/cli.py:1043` | `mkdtemp` for the toolchain archive is never removed, so each submit leaves a large file in `/tmp`. | reported |
| D4 | `python/compiler_suit_runner/peer_replication.py:79`, `:271-308` | K=3 replication uses a 1 s offer timeout, and `on_accept` does nothing. Every accepted offer for a slow `nix copy` times out and is offered again, so toolchains spread to every peer, sometimes fetched twice on one peer. | verified |
| D5 | `python/compiler_suit_runner/peer_replication.py:387-452`, `:614-638` | Network POSTs and `nix path-info` run while holding the replication locks and inside HTTP handler threads. Two nodes can block each other until 2 s timeouts expire. | reported |
| D6 | `python/compiler_suit_runner/peer_push.py:274-278` | A broadcast ID is marked seen before the fetch runs. A failed fetch stays deduplicated, so later forwards are ignored. | verified |
| D19, D20 | `python/compiler_suit_runner/peer_cache.py:2167-2172`, `:1848-1866` | Placements go to the gateway as one base64 shell argument, which breaks past 128 KiB. The bind retry runs `pkill -KILL -f harmonia-cache`, which kills other dispatches' caches. | reported |
| T2 | `template_graph/cowalk/template_histogram.py:125-141`, `dependency_graph_worker/slice.py:22-58` | The package name is taken from the text before the first dash, so `c-ares` becomes `c` and `zlib-ng` merges into `zlib`. The multi-version branch fixes it with another name parser (M6). | verified |
| N13 | `lib/mkVariant.nix:296-346` | Whether five `NIX_*` variables go in `env` or at the top level is decided from `NIX_CFLAGS_COMPILE` alone. A package with mixed placement loses its own value or fails evaluation. | reported |
| N16 | `python/compiler_suit_runner/preflight.py:1605` | Preflight reads `crossToolchains.<arch>.stdenv`, which is the linkFarm's own no-cc stdenv, not the native build stdenv the docstring describes. | reported (the reviewer checked it by eval) |
| C1 | `lib/resolve-toolchain.nix:20-110`, `lib/old-compilers.nix:648-706` | The capture change rewrites toolchains for the default `dataset` as well. Only the last wrapper layer checks `capture`. The flake comment, the README and the follow-up plan all say the default dataset is unchanged. | verified |
| C2 | `lib/mkBinaryFolder.nix:34,82`, `lib/collect-elf.py:14-48` | The capture change replaces the ELF collector for every variant. Relocatable objects are no longer collected, every elf-folder hash changes, and Python joins the shipped build-host closure. | verified |
| C3 | `python/compiler_suit_runner/workers/build_worker.py:2154-2159` | When `auxiliary/datasetCapture` exists without a contract, `capture_contract.get(...)` raises `AttributeError` on `None`. The ordinary publish path also now maps `OSError` to non-recoverable. | reported |
| C21 | `nix/compiler-capture.nix:19-60` | The supervisor's `postCheck` reads the Python validator and test files, so editing a test changes the supervisor hash and rebuilds every captured toolchain and package. | verified |
| M14 | branch `python/compiler_suit_runner/preflight.py:34-60` | The published variant directory name now hashes the derivation path. Every derivation change creates a new directory, nothing removes the old one, and duplicate variants pile up. The docstring still says names never shift. | verified |
| M11 | branch `python/compiler_suit_runner/incremental_cache.py:444-446` | Cache validation rejects valid records for packages that are unavailable on a requested arch, so those runs never get a cache hit. | reported (reproduced by the reviewer) |

## Cross-cutting problems

### 1. Large modules that do many jobs

| Unit | Size | Jobs it holds |
|------|------|---------------|
| `suit_task.py` | 3.5k lines | framework hooks, task-info building, streamed spawn, and the whole peer-mesh bootstrap in `on_run_start` (`:1961-2522`, 560 lines, 8 nested callback closures) with a hand-ordered teardown (P11, D3) |
| `cli.py` | 2.4k lines | argparse plus `cmd_submit` (`:1387-2073`, ruff complexity 59), gateway upload, image-registration parsing, archive staging (P12, P13) |
| `workers/build_worker.py` | 2.8k lines | four task kinds routed by sniffing the payload inside a closure (`:2250-2791`), import ledgers as module globals, archive gates (P14, P30) |
| `preflight.py` | 2.5k lines | six unrelated modules: bad-combo rules, enumeration, archive naming used by workers, toolchain split, local realise, build-deps closure math (P15) |
| `peer_cache.py` | 2.4k lines | nine components, including an 820-line `SubmitterPeer` SSH client (D1) |
| `rust/compiler-capture/src/trace.rs` | 2.4k lines | unsafe ptrace primitives, syscall decoding, a 485-line argv rewrite, assembly text fixes, a 700-line event loop (C6) |
| `collect::run` in `collect.rs` | 875 lines | about fifteen version and target quirk predicates, backend choice, argument injection, three directory scans, record building (C5) |
| `lib/old-compilers.nix` | 757 lines | three strategies picked by probing attributes (N23) |

The effect is the same everywhere. A change in one concern touches a file that holds five others, tests need 8 to 10 monkeypatches per case (P39, P40), and the version-specific rules in the capture code cannot be unit-tested without real compilers.

### 2. Contracts with no single owner

| Contract | Copies |
|----------|--------|
| Task-ID grammar | minted in `manifest_gen.py`, `descriptors.py`, `plan_meta.py`, `suit_task.py`; parsed again in `suit_task.py`, `build_worker.py` (hard-coded literals, "a divergence would be caught by tests") and `counters.py` (P17) |
| Archive file names | `preflight.py`, `build_worker.py` (3 literals), `eval_worker.py`, `archive.py`, `scripts/`; `TOOLCHAIN_ARCHIVE_NAME` defined three times (P16) |
| Variant metadata schema | `lib/matrix.nix` `mkMeta`, `lib/mkVariant.nix` `datasetMeta`, `lib/mkBinaryFolder.nix` (N18). The multi-version branch adds a fourth layer and a test that the copies agree (M26) |
| Subprocess runners | twelve separate `RunSubprocess` definitions with three return conventions (P18) |
| Peer wire messages | the `PeerInfo` JSON written out five times, the placement record five times, an 18-branch `do_POST` (D10) |
| Variant naming and arch tables | three label-slicing copies, four arch tables across `template_graph` and the runner (T4) |
| Version parsing in Nix | seven copies with different fallbacks (N20) |
| Capture config, contract and limits | about eleven copies across Rust, Nix, Python and hand-written test configs. `config_sha256` depends on Rust struct field order (C28) |
| Capture quirk rules | implemented in the Rust producer and again in the Python validator, including exact assembly preamble bytes (C29) |
| Release identity | `recipeId`, `releaseId`, `packageIdentity`, `label`, `package`, `binary`, `pkg` mean different things in different layers (M1); selector rules written in four places (M19) |

Each copy is a place where a later edit can be missed. The capture follow-up plan also invalidates all qualification evidence when any file under `lib/` changes (C35), so duplicated rules there are expensive to fix later.

### 3. Hidden failure

- Nix: compilers, packages and toolchains drop out of the matrix through `tryEval` or false-returning probes, with no report (N7). `or` defaults mean a misspelled constraint key turns the constraint off (N8). `.llvm or null` turns LTO variants into plain `ar` builds (N10). `harmonia or null` can ship an image with no peer cache (N11).
- Python: `except Exception` appears 44 times in `suit_task.py`, 37 in `cli.py` and 18 in `build_worker.py`. Subsystems of the peer mesh fail to a log line and the node keeps running without them (P32, D14). `fetch_from_peer` returns `None` for both "already local" and "every candidate failed" (D14).
- Tests: the `test_cli` submit tests run real `nix` and pass while logging ERROR, because the failures are swallowed (P39).
- New code repeats it. `captured_variant_complete` returns `False` for any exception, so a corrupt publication looks like a missing one and is silently rebuilt (C32). The multi-version completion check does the same (M17).

### 4. Dead code, shims and the tests that keep them alive

- Whole modules with no production caller: `partition.py` (kept for one TypedDict), `prebuild.py`, `drv_graph.py`, most of `compiler_flag_support.py`, `BroadcastSender` (245 lines), `lib/manifest.nix`, `lib/mkBinaryTarball.nix`, `apps.generate-manifest`, the `template_graph` CLI (P23, D17, N27, T3).
- Functions that return empty results but are still wired up: `preflight()` makes `cmd_preflight` always print zeros, and the legacy single-process fallback dispatches nothing and returns 0 (P24, P7).
- Compatibility shims for framework versions the flake no longer pins: `_make_task_info` retries on `TypeError` with fewer kwargs, which also hides real type errors, and `_make_task_dep` returns a `SimpleNamespace` the framework cannot use (P26).
- Each dead module has its own test file (`test_partition.py` 434 lines, `test_prebuild.py` 399 lines and others).

### 5. Repository hygiene

- The root holds agent transcripts and prompts (`convo.md` at 460 KB with 212 absolute home paths, `convo_req.md`, `bootstrapping_human.md`), generated snapshots that cannot be regenerated because their inputs are gitignored (`failures*.md`, `report_hello*.md`), and scripts that build an attribute suffix that no longer exists (`test_*.sh`, N33, N34). `table.md` looks like one of these snapshots, but it is a runtime input. The eval worker's support filter reads it (P6), the multi-version branch hashes it into the evaluation identity, and `lib/architectures.nix` cites it as evidence. It has to stay until that data moves into Nix.
- `docs/` mixes durable design notes with one-off plans. Both new lines of work add JSON run ledgers to `docs/` (C36, M35) and a planning work order at the root.
- Tests sit next to experiments. `tests/compiler-capture/` mixes 18 test files with a qualification harness, impure Nix fixtures and one-off probes (C34). `compiler_suit_runner/tests/slurm/` mixes live tests, a 4k-line harness, unit tests of that harness, and failure injection, with hard-coded home paths in 16 files (D29). The multi-version branch adds root-level `tests/*.nix` that return booleans nothing asserts on (M34).
- Packaging is broken without workarounds. `template_graph` is a runtime import of the runner but is neither packaged nor on the dev shell path, so plain `pytest` fails (P38, T1).

### 6. Repeated expensive work

- Nix evaluation: `crossSystem` targets re-import nixpkgs on every `getPkgsForTarget` call, once per cell and once per variant (N1, verified). The toolchain stdenv is rebuilt per variant (N2). `combosForTarget` filters 416k combos again for every (package, arch) cell (N3, verified). The MIPS compiler-rt overlay changes the derivation of every clang toolchain, native x86_64 included, which pushes them off the binary cache (N5, the reviewer confirmed the drv change by eval). `_meta` is described as instant but imports five old nixpkgs sets (N4).
- Subprocess churn: the build-deps closure spawns two `nix-store` processes per seed derivation, thousands at full scale, on the critical path (P33, verified). Preflight checks, builds and queries toolchains one at a time although a batch helper exists in the same file (P34).
- Import cost: importing any worker loads the whole package through `__init__`, about 1.4 s per spawn (P35).
- Capture runtime: syscall tracing stays on for linkers, LTO threads and split-DWARF `cc1` (C14). Tracee memory is read one word per `PTRACE_PEEKDATA` (C15). Each artifact byte is hashed about a dozen times between the sandbox and the NFS publication (C16). No document measures per-compile overhead.
- Multi-version: every submit resolves the full catalog uncached, while the cache now stores a free Python step (M10). The completion check re-hashes every published byte before every build task (M13).

## Main branch by area

### Nix layer (findings/nix.md)

The top problems are evaluation cost (N1 to N6) and silent shrinkage of the matrix (N7 to N10). Structure comes next.

- `mkVariant` mixes generic flag plumbing with per-package shims (`dash`, `brotli`) and a per-target ppc64 wrapper fix (N19). Toolchains seeded by `_crossToolchainMap` are not the ones variants use, so the ppc64 fix is rebuilt on every node (N15).
- Compiler capability facts (which flags each old compiler supports) are encoded in two Nix files and again as axis minimums (N21). Cross detection and `noMin` are copy-pasted (N22).
- `dedupByLabel` keeps the opposite entry from what its comment says, in two places with two different rules (N17).
- `nix/docker-image.nix` and `nix/ssh-debug-image.nix` duplicate about 200 lines and have drifted (N24). The image's flake source uses a denylist, so editing `convo.md` rebuilds the image (N26). Both images bake sshd host private keys into `/nix/store` and give root an empty password, while a comment claims root is locked (N32).
- The `-variant` pname rename adds no information and forces two workarounds (N14).
- `develop.nix` reaches into dynamic-runner's private file layout (N25).
- Comments contradict the code in at least nine places, including a wrong description of what `tryEval` catches (N9, N29). `matrixSize` and the header comment are wrong by three to four orders of magnitude (N3).

### Runner core (findings/python-core.md)

- The three god functions (P11, P12, P14) and the six-modules-in-one `preflight.py` (P15) are the main structural cost. The router comment in `suit_task.py:586-593` gives a reason that the pinned framework no longer has: it supports a `worker_module` per task type (P14).
- Resolved CLI options travel on the argparse `Namespace` as private attributes read back through nested `getattr`. A comment records a production bug caused by missing one on the secondary path. Defaults such as `22222`, `"x86_64-linux"` and `"42"` are copied three or four times (P28).
- The main data flow is stringly typed. Matrix eval computes full per-variant metadata and throws it away, and later stages rebuild a subset by parsing derivation names, so every dataset sidecar has empty `flag_set`, `hardening`, `sanitizer` and `march` fields (P29).
- Archive naming, export, import and gating live in four modules (P16). Retry ladders exist in three forms and classify errors by message substring (P22). Container paths such as `/app/log-network` are hard-coded in nine modules (P31).
- Comments often run 3 to 10 times longer than the code and cite 29 private plan files or issue tags that are not in the repo (P27).

### Distributed store sharing (findings/python-dist.md)

- The module split follows history, not concepts. The placement writer is in `peer_paths.py`, the reader and watcher are in `peer_cache.py`, and each imports the other's private names. `peer_push.py` holds the whole RPC layer, and `peer_replication.py` holds two unrelated protocols (D1, D2).
- The mesh has no composition root. `SuitTask.on_run_start` wires it with closures, and each block sets its component to `None` on failure, so partial states multiply (D3).
- Concurrency ownership is unclear. Each offer creates a raw `threading.Timer` that bypasses the injected timer factory, threads outlive `join(timeout)`, and `stop()` guarantees nothing documented (D9). Fan-outs are serial with 2 s timeouts, and dead peers are never pruned, so a crashed secondary adds up to 2 s to every later build on every node (D7). Placement reads re-parse every file on every event (D8).
- Three harmonia launchers, two key generators and two sshd spawners exist. One key generator writes the secret before `chmod 0600` (D12).
- The cachix token is set through `os.environ` on a background thread because the default runner has no `env` parameter (D15).

### template_graph and ssh_debug_runner (findings/python-dist.md, T and D24 to D26)

- `template_graph` sits at the repo root as if standalone, but it is a library of the runner with about ten production imports, and it is neither packaged nor in `testpaths` (T1).
- The cowalk/streaming boundary is leaky. Cowalk mutates the planner and calls its private methods, with seven function-level imports to break the cycle. Planner classes are plain strings used in eight files (T6).
- `make_sum_drv.py` mixes runner debugging (reading `/etc/nix/peer.conf`, probing a hard-coded harmonia port) into library code and builds Nix in f-strings. `wrapper_drv.nix` uses an impure `import <nixpkgs> {}`, so its hash depends on `NIX_PATH` (T5, N31).
- `ssh_debug_runner/cli.py` reads `sys.argv` and ignores the `argv` passed to `main`, overwrites the global, and drops flags that are never declared (D24).

### Tests

- Unit tests total about 50k lines against 30k lines of Python source. The largest test files mirror the god modules (3.7k, 3.1k, 2.5k lines) and copy fixtures instead of sharing them. `tests/conftest.py` holds only a `sys.path` hack (P40).
- Some unit tests run real `nix` (P39). The slowest takes 22 s.
- No two-node protocol tests exist for replication, so D4 and D5 went unnoticed (D30).

### Repository layout and docs

Covered in cross-cutting problem 5 and in N33, N34.

## Compiler-capture change (findings/capture.md)

The opt-in design at the flake level is clean. `datasetCaptured`, separate task IDs and separate variant directories keep captured results from satisfying ordinary requests. Resource limits are enforced everywhere, and every Nix text patch fails the build if its marker is missing. The problems are scope, structure and duplication.

1. Scope. The change silently alters the default dataset (C1, C2) and bundles unrelated fixes (C3, C37), including a dynamic-runner bump in `flake.lock`. Nothing marks the dataset version, so a partial rebuild leaves the corpus mixing variants built with old and new toolchain behavior, and nothing tells them apart. Derivations named `capture-*` serve the baseline.
2. Rust structure. `collect::run` and `trace.rs` are god units (C5, C6). The compatibility rules that decide compiler behavior are booleans in a long function with two unit tests. 55 `unsafe` sites have no SAFETY comments, and x86-64 syscall arguments are decoded by hand in four places (C12). One argument table serves GCC, Clang and Darwin, and misses common GCC operand options such as `-z`, so `gcc -z now` fails a required capture (C7). Roles, families and backends are strings, with five copies of the extension-to-role mapping (C8). Records are `json!` literals with no schema (C9). Experiment-only backends ship in the production binary (C10).
3. Duplication across languages. Config, contract and limits exist in about eleven copies (C28). The validator re-implements the producer's quirk rules (C29). ELF and DWARF parsing exists four times (C30).
4. Coupling to the existing runner. A capture boolean is threaded through ten Python files as `**({"compiler_capture": True} if X else {})` splats and string ternaries such as `"datasetCaptured" if capture else "dataset"` in nine places (C4).
5. Nix style and placement. The new Nix uses a dense style (`{cc,family,...}`, `a=b`) unlike the rest of `lib/`, has inline Python heredocs, and builds adapters with `//`, which loses `override` and `overrideAttrs` (C23, C27). `.sh`, `.h` and `.py` helpers sit flat in `lib/`. `nix/compiler-capture.nix` and `lib/compiler-capture.nix` share a basename but do different things. `capture-overlay.nix` is not an overlay (C25). Production Nix carries fixture-only branches (C26).
6. Performance. See cross-cutting problem 6 (C14 to C20).
7. Tests and docs. The supervisor derivation depends on tests (C21). The tests directory mixes tests, harness and probes (C34). The verification JSON files in `docs/` hash gitignored local files, so nobody else can verify them (C36). Evidence is keyed on the text of every file under `lib/`, so any refactor forces a full requalification of 317 compiler/target pairs (C35).

## Multi-version branch (findings/multiversion.md)

The branch makes real structural improvements to the Nix matrix. The catalog validates strictly without evaluating packages. One resolver replaces two older gates and returns a reason for each skipped cell. Axes are filtered before taking the product, and counts are computed arithmetically. Task IDs now come from one helper and include the architecture. Its problems are about naming and about decisions that will be persisted.

1. Identity names are inconsistent (M1). `recipeId` is a record, not an ID. `releaseId` is a recipe hash. `label` and `package` change meaning by layer. One evaluation token has four names. Release fields are camelCase next to snake_case compiler fields in the same dicts. These names are about to be written into sidecars and completion records, which makes later renames a migration.
2. Persistent layout (M14, M2). Variant directories change with every derivation change, and non-default package IDs change whenever the provider pin moves.
3. Two complete matrix-eval paths (M8). The new path formats suffix strings in Python that Nix parses back by longest label prefix (M5). The compiler fast path is a regex special case, and old compilers still force full discovery (M9).
4. Cache and resume (M10, M11, M13). The cache now stores the cheap step and skips the expensive one, its validation rejects valid records, and completion re-hashes every published byte.
5. Selection logic (M19, M20, M21). The rules are written in four places, part of the logic is Nix source assembled in Python f-strings with tests that regex-parse it, and the result is passed through the argparse `Namespace`.
6. Module layout (M30, M15, M18). `package_selection.py` holds eight unrelated things. Lazy imports hide cycles caused by shared helpers living in `preflight.py`. `publication.py` imports from the build worker, which imports `publication.py`.
7. Bundled behavior changes (M33). Planner deduplication, child ordering and the `path:` to `git+file://` switch change behavior for every package inside a feature commit.
8. Docs and tests (M34, M35). A 403-line agent work order sits at the root. JSON ledgers sit in `docs/`. Root-level tests are evidence scripts with hard-coded values that break on the next pin bump.

## How the two new lines of work interact

A trial merge of the capture change (as a temporary commit) with the branch conflicts in 14 of the 17 files both touch: `README.md`, `develop.nix`, `flake.nix`, `lib/matrix.nix`, `lib/mkBinaryFolder.nix`, `python/README.md`, `descriptors.py`, `incremental_cache.py`, `manifest_gen.py`, `partition.py`, `preflight.py`, `suit_task.py`, `build_worker.py` and `eval_worker.py`. Only `.gitignore`, `lib/mkVariant.nix` and `cli.py` merge cleanly. The conflicts are not only textual. Both changes design the same things separately.

| Concern | Capture change | Multi-version branch |
|---------|----------------|----------------------|
| Staging a variant for publication | `capture.stage_captured_variant` | `publication.stage_release` |
| Deciding whether a variant is already published | `capture.captured_variant_complete`, re-hashes everything (C16) | `publication.variant_complete`, re-hashes everything (M13) |
| Build provenance in `mkBinaryFolder` | `provenance.json` with the package drv, outputs, source and a hash of the whole `flake.lock` (C22) | a `datasetMetadata` env with the package drv, outputs and release metadata (M26) |
| Namespacing variant outputs | string ternaries and `__capture_v2` suffixes (C4) | release IDs in task IDs and directory names (M14) |
| Evidence docs | `docs/compiler-capture-*-verification.json` | `docs/multiversion-verification.json`, `docs/evaluation-performance.json` |

The branch already copies a `compilerCapture` field that nothing on the branch produces (M32), which shows the two were meant to meet. Without shared types for the namespace, the release, the variant metadata and the publication layout, the merge will produce a third design out of the two. The plan proposes defining those types first.

## What is good

- The dataset nesting `dataset.<pkg>.<arch>.<suffix>` is lazy, and the axes are data with one generic constraint filter.
- Comments explain why a decision was made and which incident caused it. They are often too long and cite private plans, but the content is valuable.
- Writes to shared storage are atomic (manifests with fsync and replace, staged cache entries, `.tmp` then rename for exports, the capture publication's generation and symlink swap).
- Subprocess injection exists almost everywhere, so making tests hermetic means filling gaps, not redesigning.
- Some critical paths deliberately fail loudly, such as the streamed-spawn reconciliation barrier and the build-deps completeness gate.
- `streamed_spawn.py`, `dependency_graph_planner/` and `dependency_graph_worker/` are already cut into small, typed modules. They are the model for the rest.
- The capture supervisor forwards with a plain `exec` when there is nothing to capture, handles `/dev/fd` response pipes correctly, and bounds every resource.
- The multi-version catalog validation, the single resolver and the arithmetic counts are structural improvements worth keeping.

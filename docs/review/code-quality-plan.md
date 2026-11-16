# Code-quality improvement plan

This plan follows [code-quality-report.md](code-quality-report.md). Finding IDs (N, P, D, T, C, M) refer to the files in [findings/](findings/). Each step is meant to land as its own reviewable change with the test suite green. Sizes are rough: S is under a day, M is a few days, L is a week or more.

## Rules for every step

1. Fix cheap correctness bugs first. They do not depend on any restructuring.
2. Build a safety net before moving code. Python moves need characterization tests. Nix moves need a derivation-path snapshot that proves the move is hash-neutral.
3. Delete before moving. Dead code makes every later move larger.
4. Give each contract one owner before splitting the large modules. When task IDs, archive names, payloads and the subprocess runner live in small leaf modules, the large modules can be split without import cycles or copied literals.
5. Collect every change that alters derivation hashes into one batch and ship it with an explicit dataset version. Without a version boundary, a partial rebuild after a silent hash change leaves old and new corpus semantics mixed, and nothing tells them apart.
6. Settle the shared types of the two new lines of work before merging either one.

## Decisions the owner needs to make first

These choices change what the later steps do. They are not code-quality questions, so this plan does not answer them.

| Decision | Why it blocks | Findings |
|----------|---------------|----------|
| Are the capture change's toolchain and ELF-collector changes meant for the default `dataset`? If so, what dataset version marker do they get? | Decides whether step A2 gates them behind capture or ships them in the hash-changing batch (step 7) | C1, C2 |
| What key names a published variant directory: (packageId, variant label) only, or does it include the derivation? If it includes the derivation, who prunes superseded directories? | It sets the on-disk layout. Changing it after data is published means a migration | M14 |
| Should non-default package IDs be curated names or derived from the pin? | User-facing IDs, task IDs and directories all depend on it | M2 |
| Which lands first, multi-version or capture? | The second one rebases onto the first one's publication and identity types | Report, "How the two new lines of work interact" |
| Can the legacy matrix-eval path, the legacy publication path and the legacy archive shapes be dropped? | Each one doubles the code that later steps touch | M8, M16, D17, P26 |

My recommendation for the merge order is to land multi-version first. It is smaller and already committed. It also makes the identity and publication-layout choices the capture publication should build on. The capture change has to be split and re-qualified anyway (C35), so rebasing it costs little extra.

## Step 1. Correctness fixes (S each, no dependencies)

Each line is one small commit with one focused test that fails without the fix.

| Fix | Findings |
|-----|----------|
| Set import-once flags only after `_import_archive_hard` returns | P1 |
| Use `git diff HEAD` in the cache key, so staged changes count | P3 |
| Catch `Exception`, not `BaseException`, in the worker handlers, and write down one error-mapping rule | P5 |
| Mark a broadcast ID seen only after the fetch succeeds, and reply before fetching | D6 |
| Store the package ID on `VariantArray` instead of parsing it from the derivation name (this replaces the branch's parser fix) | T2, M6 |
| Remove the toolchain-archive temp dir | P8 |
| Resolve `table.md` from the flake root, and warn or fail when it is missing | P6 |
| Kill only the dispatch's own harmonia process group | D20 |
| Send placements to the gateway on stdin and quote remote paths | D19 |
| Make cmd_submit fail when a required outpath or build-host eval fails | P4 |
| Give `import_build_deps` a dependency on its producer and always write an archive, after checking the framework timing | P2 |
| Decide env vs top-level placement per variable in `mkVariant` (hash-changing for affected packages, so it ships with step 7) | N13 |
| Point preflight at an explicit native build stdenv output | N16 |
| Key build-failure logs on task ID, and map spawn errors to the reordered headers | P9, P10 |

The replication timeout bug (D4) needs the actor rewrite in step 5e to be fixed properly. Until then, the smallest stopgap is to replace the 1 s timer with a fetch deadline of minutes when `on_accept` arrives.

## Step 2. Safety net (M)

1. Package `template_graph` so a plain `pytest` works. Move it to `python/template_graph/`, add it to `include` and `testpaths`, ship its `.nix` files as package data, and delete the `sys.path` hacks and `PYTHONPATH` instructions (T1, P38). Today `pytest` in the dev shell fails at collection.
2. Make unit tests hermetic. Add an autouse fixture that fails any test that runs real `nix*` unless the test is marked `nix`. Inject a runner where `cli.py` calls `preflight._default_run_subprocess` directly (P39).
3. Add shared fakes. `tests/fakes.py` gets `FakeRunner` (scripted responses plus a call log), `FakePrimaryHandle` and `FakeTask`, and `conftest.py` gets `suit_config`, `fake_runner` and `ledger` fixtures (P40).
4. Add characterization tests that freeze `discover_items` output, the worker argv per task type, the gate-ID set and the streamed-spawn order. Later steps move this code.
5. Snapshot derivation paths for a few cells: `hello` on every arch, one old-gcc cell, one ppc32 cell, and one captured variant once capture lands. Add a flake check that compares against the snapshot. Hash-neutral steps must leave it unchanged.
6. Add lint configuration to the flake checks: ruff for `python/`, nixfmt for `lib/` and `nix/`, rustfmt and clippy for the Rust crate. Today the repo has no formatter configuration, and the new code drifts in style (C27).

Done when `nix flake check` and a bare `pytest` both pass without environment workarounds.

## Step 3. Delete dead code and clutter (M, no dependencies after step 2)

| Delete | Findings |
|--------|----------|
| `partition.py` (move `VariantSpec` to a types module), `prebuild.py`, `drv_graph.py`, unused parts of `compiler_flag_support.py`, `_nix_eval_utils.py` out of the runtime package, and their test files | P23 |
| `preflight()`, `filter_existing_variants` (if still unused), `query_initial_toolchain_placement` (only tests call it), `_run_import_prelude`, the legacy in-process dispatch and its fallback, unused config fields | P24, P25, P7 |
| Framework compatibility shims (`_make_task_info` retries, `_make_task_dep` namespace, `getattr` probes), after confirming the pinned framework supports `TaskInfo(...)` and `TaskDep(phase_id)` directly | P26 |
| `BroadcastSender`, `fan_out_path_gone`, `has_nix_cli`, `assemble_substituter_env`, `prune_stale`, `read_peer_push_urls`, and their tests | D17 |
| `lib/manifest.nix`, `lib/mkBinaryTarball.nix`, `apps.generate-manifest`, `rawPkg`, unused helpers in `lib/flags.nix` and `lib/packages.nix`, the temporary `RUST_LOG=trace` in the image | N27, N30 |
| The broken `template_graph` CLI, its back-compat shims, the probe scripts and summaries under `tests/fixtures/`, and `sum_drv.nix` with its legacy caller | T3 |
| Root clutter: `convo.md`, `convo_req.md`, `bootstrapping_human.md`, `goal.md`, the stale `test_*.sh` scripts, and the snapshots whose inputs are gitignored (`failures*.md`, `report_hello*.md`), or move those snapshots to dated archive files. Keep `table.md` and `gen_table.sh`. The eval worker reads `table.md` and the multi-version evaluation identity hashes it, so it moves only after that support data lives in Nix | N33, N34, P6 |

Then reorganize `docs/` into `design/`, `compat/`, `ops/` (absorbing the root `dynrunner/` runbooks) and `archive/`, with an index. Fix comments that contradict the code, and remove references to private plan files and issue tags (N9, N29, P27, D18).

## Step 4. One owner per contract (M to L, after step 3)

Python leaf modules, each with no dependencies inside the package:

| New module | Replaces | Findings |
|------------|----------|----------|
| `nix_exec.py`, one `Runner` with `Result(stdout, stderr, rc)` plus batched helpers (`nix_eval_json`, `store_query`, `store_export` streaming via stdin) | twelve `RunSubprocess` copies | P18, P36 |
| `task_ids.py`, typed constructors and `parse_task_id()` | six minting and parsing sites | P17 |
| `container_layout.py` | `/app/...` literals in nine modules | P31 |
| `run_options.py`, a frozen `RunOptions` built once by `resolve_options(args, env)`, and one `env_flag()` | private attributes on `argparse.Namespace`, copied defaults | P28, M21 |
| `payloads.py`, dataclasses `BinaryMeta`, `VariantPayload`, `CommonDepPayload`, `MatrixEvalPayload` with `to_wire`/`from_wire`. Carry the evaluated flag axes through so dataset sidecars stop losing them | stringly-typed dicts | P29, P20 |
| `closure_archive.py` (names, export, import with retry) and `import_gates.py` with an `ImportLedger` instance | archive code in four modules, module-global ledgers | P16, P30, P1 |
| Peer wire messages as frozen dataclasses, `PlacementClass` enum, `SUBMITTER_PEER_ID`, `PeerInfo.push_url()` | five copies of each JSON shape, magic strings | D10, D11, D27, D28 |
| One variant-naming module and one arch table generated from `lib/architectures.nix`, owned by `template_graph` | three label slicers, four arch tables | T4 |

Nix leaf files:

| New file | Replaces | Findings |
|----------|----------|----------|
| `lib/util.nix` with version parsing, `isCross`, shared constants and a `dedupByLabel` that throws on unexpected collisions | seven version parsers, copied predicates, two contradictory dedup rules | N20, N22, N17 |
| An axis and target schema check, an expected-compiler assertion, and an `_audit.<sys>` output listing every dropped (package, arch, compiler) with a reason | silent `tryEval` drops and `or` defaults | N7, N8, N10 |
| One `variantMetadata` function used by `matrix.nix`, `mkVariant.nix` and `mkBinaryFolder.nix` | three or four copies of the metadata schema | N18, M26 |
| One compiler capability table feeding axis constraints and `hardeningUnsupportedFlags` | two Nix copies and the docs tables | N21 |

## Step 5. Split the large modules (L, after step 4)

Order matters here, because later splits import from earlier ones.

a. `preflight.py` into `known_bad_combos.py`, `enumeration.py`, `toolchain_split.py` and `toolchain_realise.py`, with the build-deps closure moving into `dependency_graph_worker`. Batch the per-item nix calls in the same change (P15, P33, P34).

b. The worker entry. Make `compiler_suit_runner/__init__.py` lazy (P35), move routing to a module-level `workers/dispatch.py`, and give each task kind its own module. Then choose between one worker module per task type (the pinned framework supports it) and a shared entry, and write the reason down (P14).

c. `suit_task.py` into `task_infos.py` (one `TaskInfoFactory`), a streamed-spawn handler, and a `PeerMesh` class that owns component start and stop order and marks each component required or optional (P11, P19, P32, D3). `SuitTask` keeps only the framework hooks.

d. `cli.py` into argparse plus a short `cmd_submit`, a `SubmitPipeline` with one method per stage taking `RunOptions` and an injected runner, and `gateway_staging.py` (P12, P13).

e. The peer modules by concept: `nfs_atomic.py`, `peer_discovery.py`, `placement.py`, `local_daemons.py`, `cluster_key.py`, `submitter_peer.py`, `peer_rpc.py`, `replication_k.py`, `broadcast.py`, with one harmonia launcher, one key helper and one sshd spawner (D1, D2, D12). Write two-node protocol tests over an in-memory transport first, so they fail on today's behavior. Then rewrite K-replication as a single-threaded actor with explicit OFFERED and ACCEPTED states, with no I/O under locks (D4, D5, D9). After that, make fan-out parallel, prune dead peers, fix the withdraw order and make placement reads incremental (D7, D8). Remaining fixes go in any order (D13 to D16, D21 to D26).

f. Inside `template_graph`: a `CowalkCtx` interface instead of passing the planner, a frozen `PlanResult`, enums for node classes, and runner debugging moved out of `make_sum_drv.py` (T5, T6).

g. Split each large test file along the new module boundaries as its module is split. Move `tests/slurm/` harness code into its own package and keep the live tests thin and fixture-driven (P40, D29).

## Step 6. Nix evaluation performance, hash-neutral (M, after step 4 Nix files)

1. Compute `targetPkgsByArch` once and pass `targetPkgs` into `mkVariant`. Build old cross package sets once per arch (N1).
2. Build one `toolchains.<arch>.<compiler>` table used by both `mkVariant` and `_crossToolchainMap`, and build `_crossToolchainMap` lazily per key (N2, N6, C20).
3. Memoize `combosByArch`, store parsed versions on compiler entries, and fix `matrixSize` (N3).
4. Put static version strings in the old-nixpkgs specs and assert them inside `mkStdenv`, so `_meta` needs no nixpkgs import for pure metadata (N4).

The derivation snapshot from step 2 must not change.

## Step 7. Hash-changing Nix batch with a dataset version bump (M, after step 6)

Gate the MIPS compiler-rt overlay on MIPS targets (N5), then check whether the three package sets can merge. Drop the `-variant` pname rename (N14). Move per-target wrapper fixes into the toolchain layer (N15). Fix env placement (N13). Add the capture change's baseline toolchain and collector changes here if the owner decides they belong to the default dataset (C1, C2). Record the new dataset version in `meta.json` so mixed corpora can be told apart.

## Step 8. Images (S to M, independent)

Add `nix/image-common.nix` for the shared image parts (N24). Build the flake source from an allowlist with `lib.fileset` (N26). Make harmonia required (N11). Generate sshd host keys at container start and fix the root password comment (N32). Make the `template_graph` wrapper derivation pure (N31). Ask dynamic-runner upstream for a builder function instead of reaching into its files (N25). Group the runner's flake outputs under one attribute and move the test fixture out of `_drvPaths` (N28).

## Track A. Shared types for both new lines of work (do before merging either)

1. A short `docs/design/dataset-identity.md` glossary: recipe, recipe digest, release identity, package ID, package family, variant label, dataset namespace (M1).
2. A Python `Release` dataclass that validates once (M3), and a `DatasetNamespace` dataclass built from the publication schema (C4). Pass these instead of dict slices and boolean splats.
3. One publication module that owns variant directory naming (from the M14 decision), staging, the completion record, and a cheap completion check based on the record, file list and sizes. Keep full re-hashing as an explicit verify command. Both `stage_captured_variant` and `stage_release` move onto it (M13, M15, M18, C16).
4. One build-provenance record in `mkBinaryFolder` that merges capture's `provenance.json` and the branch's `datasetMetadata`, and that names the inputs that actually build the package instead of hashing the whole `flake.lock` (C22, M26). The branch's evaluation identity serves a different purpose. It guards inventory caching and selection consistency, so it is fixed separately in Track C (M27).

## Track B. Capture change

Before committing, in this order:

1. Move the change off `main` onto its own branch and split it into separate commits: the baseline toolchain compatibility work (C1), the ELF collector (C2), the unrelated `read_manifest` fix (C3), and the capture feature. Leave the `flake.lock` dynamic-runner bump and the planning files out (C37).
2. Make the supervisor source Rust-only and move its Python checks into `checks` or `passthru.tests` (C21). This stops test edits from rebuilding every captured package during the steps below.
3. Delete experiment-only backends (`gcc-dumpdir`, `observe`), `job_wrapper.py` and the probes, or put them behind a cargo feature used only by qualification fixtures (C10, C34).
4. Move the verification JSON out of `docs/` and cut the compatibility doc down to the contract (C36).
5. Rename derivations and files that say "capture" but serve the baseline, and move the new Nix into `lib/capture/`, `lib/toolchain-compat/` and `lib/elf-folder/` (C25).

After committing, and before the final qualification run, because any change invalidates the current evidence (C35):

6. A shared `capture-schema.json` (field names, enums, limits, contract) read by Nix, embedded by Rust and loaded by Python. Hash config bytes instead of depending on Rust field order (C28). Typed enums and record structs in Rust (C8, C9).
7. Extract a pure `plan(&CompilerIdentity, &Analysis) -> CapturePlan` from `collect::run`, with table tests per compiler version and target. Put each quirk in a named function in `quirks.rs` (C5). Record expected roles and transformation parameters in the record, so the Python validator does generic checks without version tables (C29).
8. Split `trace.rs` into a `ptrace` layer with a `Tracee` type and SAFETY comments, syscall decoding, and one module per rewrite pass (C6, C12). Use per-family option tables for argument parsing (C7).
9. Performance on top of the new layers: a seccomp filter so only path syscalls stop (C14), `process_vm_readv` and fewer canonicalize calls (C15), single-pass hashing and no fsync on scratch (C16, C17), a PCH producer index (C18), binary wrappers instead of shell launchers (C19). Add an overhead benchmark and record a budget.
10. Python cleanup: validation errors with reasons (C32), one ELF module (C30), normal package imports (C31). Nix cleanup: adapters through `overrideAttrs` or `wrapCCWith` (C23), wrapper hooks in one file with `.sh` fragments (C24, C27), fixture branches out of production Nix (C26), `meta.platforms = [ "x86_64-linux" ]` (C13).
11. Move tests to where they run (`rust/compiler-capture/tests/`, `python/.../tests/capture/`, `tools/capture-qualification/`), key evidence on the supervisor hash and each pair's evaluated wrapper derivation instead of file text (C34, C35), then run the full qualification once.

## Track C. Multi-version branch

Before merging:

1. Settle the directory layout and ID stability (M14, M2), with curated releases moved to a data file (M24).
2. Rename identity fields through the `Release` type from Track A (M1, M3), before any sidecar or completion record is written with the current names.
3. Cache the resolved catalog keyed on the evaluation identity, or remove the variants cache (M10). This removes M11.
4. Restore the `preflight` subcommand output or give the new command its own name, and delete the dead functions (M22).
5. Remove the speculative `compilerCapture` copies (M32), the work order at the root, the JSON ledgers and the `.multiversion-work` references. Add the short design doc from Track A (M35).

After merging:

6. Send structured selections to `_selectDataset` and look axes up by label, with a lazy `compilersByLabel` (M5, M9). Then delete the legacy eval path, the duplicate sampler and the bare-map shim (M8, M28).
7. One selection resolver backed by Nix functions, not f-string Nix (M19, M20).
8. Split `package_selection.py`, make imports top-level, drop the legacy publication branch, and narrow error handling (M30, M16, M17).
9. Nix cleanup in the resolver and flake outputs (M25, M29), and fewer extra evaluations (M12).
10. Turn root-level `tests/*.nix` into flake checks with assertions that read expected values from the catalog (M34). Move the bundled planner behavior changes into their own commits with before and after template counts (M33).

## Suggested order overall

| Order | Work | Can run in parallel with |
|-------|------|--------------------------|
| 1 | Owner decisions | none |
| 2 | Step 1 correctness fixes | Track B steps 1 to 5 |
| 3 | Step 2 safety net | Track B steps 1 to 5 |
| 4 | Track A shared types, Track C before-merge items, merge multi-version | Step 3 |
| 5 | Step 3 deletions | Track B rebase onto the merged branch |
| 6 | Step 4 contract owners | Track B steps 6 and 7 |
| 7 | Step 5 module splits, Step 6 Nix performance | Track B steps 8 to 10, Track C after-merge items |
| 8 | Step 7 hash-changing batch with dataset version bump, then the final capture qualification (Track B step 11) | Step 8 images |

The capture qualification sits last on purpose. Its evidence is invalidated by changes to `lib/` and the Rust sources (C35), so running it before the refactors would mean running it twice.

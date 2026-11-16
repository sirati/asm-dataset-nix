# Code-quality review: python/compiler_suit_runner runner core

Scope: cli.py, suit_task.py, preflight.py, manifest_gen.py, partition.py, incremental_cache.py, prebuild.py, drv_graph.py, nix_drv_show.py, _nix_eval_utils.py, compiler_flag_support.py, support_table.py, workers/*, dependency_graph_planner/*, scripts/, pyproject.toml, python/README.md, and the structure of compiler_suit_runner/tests. Reviewed at main HEAD 72dd7be in /tmp/cq-review/head. All paths are relative to the repo root.

Checks run: `ruff check` (20 F401, 7 E741 in non-test code; C901/PLR0915 flag cmd_submit at complexity 59, on_run_start 46, build_worker.main 37, build_worker 25), the non-SLURM pytest suite (passes with `PYTHONPATH=<repo root>`), `python -X importtime` on a worker entry point, and grep-based caller analysis. I also read the pinned dynamic_runner checkout to confirm two framework behaviours (per-type `worker_module`, SIGTERM raising SystemExit in workers).

Severity: high hurts correctness, maintainability or performance now. Medium is a real cost that can wait one cycle. Low is cleanup.

---

## A. Correctness bugs and failures that get hidden

### P1 (high, correctness) Import-once flags are set before the import succeeds
- Evidence: python/compiler_suit_runner/workers/build_worker.py:1404 sets `_common_archive_imported = True` and only then calls `_import_archive_hard` at :1407. The same order appears at :1441 (build_deps), :1480 (`_imported_toolchain_out_paths.add`) and :1572 (`_imported_common_dep_out_ids.add`). python/compiler_suit_runner/workers/affine_import_worker.py:200-220 maps a nix-daemon-not-ready RuntimeError from these helpers to `RecoverableError`, so the framework retries the gate.
- Problem: if the import raises and the framework retries the gate on the same long-lived worker process, the flag is already set. The retry returns early and reports `WorkerOutput()` success even though nothing was imported. `ensure_binary_archive_imported` (:1255) and `ensure_toolchain_archive_imported` (:1328) do it correctly, so the four helpers disagree on the rule.
- Why it matters: dependents then build without the common, toolchain-delta, build-deps or common-dep closure. In substituter-free mode that ends in a confusing build failure on that node. With substituters it quietly turns into substitution traffic, which is what the affine gates exist to prevent.
- Fix: record success only after `_import_archive_hard` returns. Better, replace the module globals with one `ImportLedger` object that has a `mark_done(key)` called after success (see P30).

### P2 (high, correctness, needs a framework-timing check) build_deps archive: a race, plus a skip that turns into a hard failure
- Evidence: python/compiler_suit_runner/suit_task.py:1675-1677 says the gates have empty deps because "archives are pre-staged on the shared FS before dispatch". :1727-1728 emits `import_build_deps` with no deps. But python/compiler_suit_runner/workers/dependency_graph_worker/run.py:255-269 writes `build_deps.out.archive` only after `_stream_descriptors` has already streamed every build task, and BUILD is `barrier=False` (suit_task.py:637). The producer also returns without writing any archive at run.py:756-761, :926-932 and :988-994, while the consumer `_import_archive_hard` (build_worker.py:1349-1360) raises on an absent archive. affine_import_worker.py:221-230 maps that to NonRecoverableError, which cascade-fails every dependent.
- Problem: build tasks are spawned before their pre-load archive exists. When the producer decides there is nothing to export, every node fails hard.
- Why it matters: `--no-substituters` forces `build_deps_local` on (cli.py:632-633), so this is the substituter-free path. The `nix-store --realise` step before export can take minutes, which leaves a wide window.
- Fix: give `import_build_deps` a cross-phase `TaskDep` on `dependency_graph`, or have the dep_graph worker produce the archive before it sends the summary and gate spawns on it. Always write an archive, possibly an empty marker that the consumer treats as a no-op, so the producer and consumer agree on what absence means.

### P3 (high, correctness) The incremental-cache key ignores staged changes
- Evidence: python/compiler_suit_runner/incremental_cache.py:311 runs `["git", "diff"]`, which compares the worktree with the index.
- Problem: a change that is `git add`-ed but not committed produces an empty diff, so the key equals the HEAD key. Nix flakes do evaluate staged content, which means toolchain and variant enumeration silently reuse a stale cache entry. (Correction from the synthesis pass: the main checkout's new capture files are intent-to-add, not staged, so plain `git diff` does see them. The bug applies to real `git add`.)
- Fix: use `git diff HEAD` (staged and unstaged tracked changes). Untracked files don't need hashing because flakes ignore them.

### P4 (high, hidden failure) cmd_submit swallows preflight failures that later make the run unrecoverable
- Evidence: python/compiler_suit_runner/cli.py:1709-1714 turns a failed toolchain-outpath eval into `{}`. The split-archive upload is then skipped silently because the guard at :1863-1868 includes `and partition_drv_outpaths`, even though `_produce_and_upload_toolchain_split_archives` documents itself as "HARD FAIL" (:1298-1301). python/compiler_suit_runner/nix_drv_show.py:93-98 returns `{}` for every drv when one `nix derivation show` fails. cli.py:1626-1644 logs and continues when the native build-host stdenv eval or the build-host tools eval fails, although P2's substituter-free mode cannot work without them.
- Why it matters: the run dispatches and fails much later on every build node, far from the cause. The unit tests show this happening: the test_cli submit tests log "native build-host stdenv eval failed" at ERROR and still pass (see P39).
- Fix: in cmd_submit, treat these as fatal whenever the result is required (dedup on, gateway set, or no_substituters). Raise from `eval_drv_outpaths` instead of returning `{}`, or return the per-drv failures.

### P5 (high, correctness) Worker handlers turn SIGTERM into a permanent failure
- Evidence: python/compiler_suit_runner/workers/build_worker.py:2564, :2711 and :2741 use `except BaseException` and re-raise as NonRecoverableError. In the pinned framework, `dynamic_runner.worker.run` installs SIGTERM/SIGHUP handlers that raise SystemExit, which the loop treats as a clean exit (dynamic_runner tests/test_worker_runtime.py:821-828).
- Problem: a graceful drain or eviction during `nix build` or eval reports the task as a non-recoverable failure, and the framework cascade-fails its dependents. I did not check whether the framework ignores results it receives after it sent SIGTERM.
- Also: :2556-2563 logs "(retry-eligible)" and then raises NonRecoverableError. Meanwhile python/compiler_suit_runner/workers/eval_worker.py:58-79 and :1065-1067 document that RuntimeError means a retried Errored. The documented error contract and the code disagree.
- Fix: catch `Exception`, not `BaseException`. Pick one error mapping (RuntimeError means recoverable or it doesn't) and state it in one place, the dispatcher.

### P6 (medium, correctness) The support-table filter silently does nothing when the cwd has no table.md
- Evidence: python/compiler_suit_runner/support_table.py:48-56 resolves `table.md` from `Path.cwd()`, and :71-72 returns `{}` if the file is missing. `is_supported` (:108-129) then allows everything. python/compiler_suit_runner/workers/eval_worker.py:918 calls `load_support_table()` with no path on secondaries. The container has `WorkingDir = "/app"` (nix/docker-image.nix:515), while the flake lives at `/app/flake` or `CSR_FLAKE_DIR` (eval_worker.py:847-879).
- Problem: on the cluster the matrix_eval filter is probably disabled with no log line. The same happens on the submitter whenever `--flake` is not the cwd.
- Fix: pass the resolved flake root explicitly. Make a missing table an error, or at least a WARNING, unless the caller opts out.

### P7 (medium, correctness) The legacy single-process fallback dispatches nothing and returns 0
- Evidence: python/compiler_suit_runner/cli.py:962-994 runs `find_binaries`/`dispatch_binary`. discover_items yields synthetic paths (`pathlib.Path(entry.name)` at suit_task.py:1454, `"<task_id>.json"` at :1779), so `read_manifest` fails on every item. `dispatch_binary` (suit_task.py:3440-3457) logs that and returns. `success` stays True.
- Fix: delete the fallback together with the legacy dispatch surface (P24). If the framework is absent, fail fast.

### P8 (medium, correctness) The toolchain-archive temp dir leaks on every submit
- Evidence: python/compiler_suit_runner/cli.py:1043 calls `tempfile.mkdtemp(...)` and never removes it. The sibling split function does clean up (cli.py:1382-1384).
- Why it matters: `toolchains.drv.archive` holds the whole toolchain drv closure, so each SLURM submit leaves a large file in /tmp.
- Fix: use `tempfile.TemporaryDirectory()` in both functions and share the gateway helper (P13).

### P9 (low, correctness) spawn_tasks error indices are mapped to the wrong headers
- Evidence: python/compiler_suit_runner/suit_task.py:3197-3205 reorders the batch into common_dep, then gates, then the rest, and passes `all_infos` to `spawn_tasks`. :3216 then calls `_log_spawn_errors(errors, headers)`, which indexes the original header order (:3341).
- Problem: the comment says only gate errors are affected, but every index after the reorder points at the wrong header, so diagnostics name the wrong task.
- Fix: keep a parallel `all_labels` list built with the same reordering and index that.

### P10 (low, correctness) Build-failure logs overwrite each other
- Evidence: python/compiler_suit_runner/workers/build_worker.py:2082-2084 and :2505-2509 write `/app/log-network/build-failures/{name}.log`. suit_task.py:970-978 documents that `name` is not unique (two flex drvs share a name), and the directory is shared by every secondary.
- Fix: key the file on `task_id` or on the drv hash.

---

## B. Separation of concerns and module boundaries

### P11 (high, maintainability) SuitTask.on_run_start/on_run_end form a 690-line peer-mesh bootstrap inside the TaskDefinition
- Evidence: python/compiler_suit_runner/suit_task.py:1961-2522 (complexity 46, 159 statements) and :2524-2654 (complexity 22). It builds the signing key, PeerListWatcher, PathPlacementWatcher, replication sender/receiver/repair, the lifecycle listener, BroadcastReceiver, eight nested callback closures for PeerPushServer (:2251-2338), announce, Cachix, nix-daemon, harmonia, PeerNixConfWatcher and sshd. Teardown repeats a try/stop/finally block ten times.
- Why it matters: the framework hooks (topology, discovery, spawn) sit behind a component graph that has nothing to do with them. Any peer-mesh change touches the 3.5k-line task class, and the start and stop order are maintained by hand in two places.
- Fix: move this into a `PeerMeshNode` class, probably next to peer_cache, with a list of `(name, start, stop, required)` components. start runs them in order and stop runs them in reverse. SuitTask keeps only `self._mesh = PeerMeshNode(config).start()`.

### P12 (high, maintainability) cmd_submit is a 690-line pipeline inside the CLI module
- Evidence: python/compiler_suit_runner/cli.py:1387-2073 (complexity 59, 201 statements) runs cached enumeration, metadata flattening, native stdenv and build-host evals, local toolchain realisation and GC rooting, outpath eval, manifest emission, config building, two archive uploads, submitter-peer setup, container env/NIX_CONFIG assembly and dispatch. cli.py also holds gateway upload code (:1002-1384) and image-registration parsing (:1183-1280). python/README.md:194 describes cli.py as "CLI, preflight, archive preparation, and framework dispatch".
- Why it matters: cmd_submit has no seams, so tests must monkeypatch 8-10 module globals per case and still miss some, which leads to P39.
- Fix: write a `SubmitPipeline` (submit_pipeline.py) with methods `enumerate()`, `realise_toolchains()`, `stage_archives()`, `build_config()`, `dispatch()` that take a resolved options object (P28) and an injected nix runner (P18). cli.py keeps argparse and a 20-line cmd_submit.

### P13 (medium, cohesion) Gateway staging code lives in the CLI and is duplicated
- Evidence: python/compiler_suit_runner/cli.py:1060-1065 and :1347-1351 build the same gateway config and auth threading. `_upload_archive_with_sidecar` (:1100-1180) and build_worker.py:1777-1782 both hash files for sha256 sidecars. Only the split path uses sidecar skip.
- Fix: put this in a `gateway_staging.py` with `GatewaySession` (connect/upload_with_sidecar/disconnect as a context manager) and one `sha256_file()` helper.

### P14 (high, maintainability) build_worker.main is a payload-sniffing router for four task kinds
- Evidence: python/compiler_suit_runner/workers/build_worker.py:2250-2791 (complexity 37). The nested `handle` closure dispatches build_compilers (:2470-2523), matrix_eval (:2529-2571), dependency_graph with predecessor-output parsing and prestaged sidecar loading (:2588-2718) and builds (:2720-2788). The reason given is stale: suit_task.py:586-593 says "the first registered [worker_module] wins", but the pinned framework spawns per-type `worker_module`s and respawns on a type change (dynamic_runner crates/dynrunner-pyo3/src/subprocess_factory.rs:192-201, :836-850). The affine gates already use their own module (suit_task.py:645-648).
- Why it matters: build_worker.py is 2.8k lines largely because it hosts other workers' entry logic. Because the router is a closure, tests can only reach it by faking `dynamic_runner.worker` in `sys.modules` and capturing the callback (tests/test_build_worker.py:1140-1165).
- Fix: move routing to a module-level `workers/dispatch.py: handle(task, ctx)`, with dep_graph input assembly in dependency_graph_worker and build_compilers in its own worker. Then choose per-type worker modules (drops the router, costs respawns) or the shared entry (keeps one process), and record the reason. P35 lowers respawn cost in either case.

### P15 (medium-high, cohesion) preflight.py holds six unrelated modules
- Evidence: python/compiler_suit_runner/preflight.py contains the known-bad-combo rule table (:199-855), flake enumeration (:858-1188), archive and ID naming contracts used by runtime workers (:1196-1292), toolchain split (:1296-1570), build-host evals (:1573-1680), archive export wrappers (:1683-1816), a dead batch helper (:1824-1904), local realise/GC-root (:2050-2340) and build-deps closure math used only by the dep_graph worker (:2347-2511, called from workers/dependency_graph_worker/run.py:806, :966, :1005).
- Why it matters: workers import preflight to get archive names (build_worker.py:1405, :1442, :1573), so they pull in the submitter preflight. Callers also reach into private helpers: cli.py:1229 uses `preflight._default_run_subprocess`, and dependency_graph_worker/archive.py imports `_short_dataset_name`.
- Fix: split into `known_bad_combos.py` (and move eval_worker's use there), `enumeration.py`, `toolchain_split.py`, `toolchain_realise.py`. Move archive naming into the archive module (P16) and build-deps closure into dependency_graph_worker.

### P16 (medium-high, cohesion) Archive naming, export, import and gating live in four modules
- Evidence: naming is in python/compiler_suit_runner/preflight.py:1196-1292. Export (`export_closure`, `export_closure_exact`) is in workers/build_compilers_worker.py:365-528 and imported by preflight.py:1719, :1770, :2492 and build_worker.py:1726. Import is in workers/dependency_graph_worker/archive.py:447-581. The import-once gate helpers are in workers/build_worker.py:1202-1690 and are imported by affine_import_worker.py:95-97 (a private `_run_affine_import`, which is still listed in `__all__`). Name literals are copied in build_worker.py:1245, :1299, :1481, eval_worker.py:563, :578, archive.py:59-74 and scripts/_phase3_dot_helpers.py:196. `TOOLCHAIN_ARCHIVE_NAME` is defined three times.
- Fix: create one `closure_archive.py` (names, paths, export with streaming, import with retry, `is_present`) and one `import_gates.py` (gate-id to ensure_* routing plus the ledger), owned by affine_import_worker. Nothing else spells an archive filename.

### P17 (medium, cohesion) The task-id grammar is minted and parsed in six places
- Evidence: mint sites are python/compiler_suit_runner/manifest_gen.py:207-284 (`common_dep_task_id` uses the drv basename, a different format from the planner's), dependency_graph_planner/descriptors.py:132-173 and plan_meta.py:50-54. Gate ids are minted in suit_task.py:201-312. Parse sites are `_ident_from_common_dep_task_id` (suit_task.py:260-299), the gate router with hard-coded literals (build_worker.py:1583, :1665-1689, with the comment "a divergence would be caught by tests") and prefix counters (dependency_graph_worker/counters.py:28-32).
- Fix: one `task_ids.py` with typed constructors and a `parse_task_id() -> CommonDepId | VariantId | GateId(kind, key)`. suit_task, build_worker and counters import it, which also removes the reason for duplicating the literals to avoid an import cycle.

---

## C. Duplication

### P18 (medium-high, duplication) Twelve subprocess runners with three return conventions
- Evidence: `RunSubprocess` plus `_default_run_subprocess` are defined in preflight.py:115/118, eval_worker.py:140/155, drv_graph.py:37/58, nix_drv_show.py:17/20, prebuild.py:48/87, build_compilers_worker.py:117/190, build_worker.py:358, dependency_graph_worker/subproc.py:20/111, peer_paths_fetch.py:63/66, incremental_cache.py:57/253 (returns `CompletedProcess` and takes `cwd=`), and cachix_uploader.py:39/88 (returns `str`). Only some resolve argv[0] through `resolve_tool`. `"--extra-experimental-features", "nix-command flakes"` is repeated nine times in preflight.py alone.
- Fix: one `nix_exec.py` with `Runner` (argv, stdin, cwd, timeout returning a `Result(stdout, stderr, rc)`) and helpers such as `nix_eval_raw`, `nix_eval_json`, `store_query(flag, paths, stdin=True)` and `store_export(paths, dest)`. Tests share one `FakeRunner` (P40).

### P19 (medium, duplication) TaskInfo building is copied three ways, gate TaskInfo three more
- Evidence: python/compiler_suit_runner/suit_task.py `_header_to_task_info` (:937-988) and `SuitTask._task_info_from_header` (:1748-1787) differ only in where the flags come from. discover_items builds the same `header_dict` inline (:1443-1448). Gate TaskInfos are built at :1641-1654, :1693-1707 and :3246-3271. `_build_outpath_to_task_hash_lookup` re-implements `_classify`'s phase/type mapping (:2854-2863). The dep_graph header is built twice in `_discover_matrix_eval_and_dep_graph_items` (:1527-1543, :1597-1626), and that function resets and recomputes `toolchain_aggregate_drv` (:1498-1506 vs :1548).
- Fix: write `TaskInfoFactory(config)` with `from_header(header)` and `gate(task_id, phase, deps=())`, and `make_dependency_graph_header()` in manifest_gen.

### P20 (medium, duplication) The build_variant payload is defined twice
- Evidence: python/compiler_suit_runner/manifest_gen.py:528-546 (strict `variant["key"]`, conditional `toolchain_outpath`) versus dependency_graph_planner/descriptors.py:281-305 (`.get(..., "")` defaults, always sets `toolchain_outpath`). The `attr` string is rebuilt in manifest_glue.py:83-84. Only the planner version runs in production (P24).
- Fix: keep one `VariantPayload` dataclass with `to_wire()`, used by the planner. Delete the manifest_gen copy.

### P21 (medium, duplication) Worker helper copies
- Evidence: `_read_substituters_file` appears in build_worker.py:400 and build_compilers_worker.py:205, `_excerpt_log` in build_worker.py:913 and build_compilers_worker.py:220, and `_last_nonblank_line(s)` in build_worker.py:945, build_compilers_worker.py:314 and prebuild.py:122. Worker log setup is copied in build_worker.py:2359-2376, affine_import_worker.py:141-156 and build_compilers_worker.py:780. The `BuildWorkerResult(item_class=..., name=..., success=False, duration_seconds=max(0.0, clock() - start), error=...)` block appears about ten times in `build_worker()` (build_worker.py:1940-2148). Closure-or-fallback logic is copied inside `compute_toolchain_split` (preflight.py:1470-1483 vs :1526-1542). The streaming export is copied in build_compilers_worker.py:420-447 vs :491-519. Two identical id extractors exist, `toolchain_id_for_outpath` and `common_dep_id_for_ident` (preflight.py:1201-1281).
- Fix: a `workers/common.py` for the worker helpers, a local `fail(msg, **kw)` closure in `build_worker()`, and the shared archive module from P16.

### P22 (low-medium, duplication) Three retry/backoff ladders and string-matched error classes
- Evidence: python/compiler_suit_runner/workers/build_worker.py:142-176 and :504-547 (SQLite and substituter retries; the SQLite branch calls `time.sleep` directly instead of the `_retry_sleep` seam and has no test). workers/dependency_graph_worker/archive.py:213-284 and :447-527 add another ladder. affine_import_worker.py:210-220 classifies errors by substring ("cannot connect to socket").
- Fix: one `retry(fn, classify, schedule, sleep)` helper. Raise typed exceptions (`NixDaemonUnavailable`, `TransientStoreError`) at the source instead of matching message text at the edge.

---

## D. Dead code and stale compatibility shims

### P23 (medium-high, dead code) Whole modules with no production callers
- Evidence: python/compiler_suit_runner/partition.py (401 lines) is used only for the `VariantSpec` TypedDict (preflight.py:31, manifest_gen.py:58), and python/README.md:51-52 admits that "the old partition/merge workers remain in the source tree". prebuild.py (264 lines) has no importers. drv_graph.py (122 lines) is used only by tests. In compiler_flag_support.py only `is_known_broken` is called (preflight.py:846); `VersionGate`, `unsupported_hardening_flags`, `flag_supported` and `mandatory_flags_for` have no callers, despite the module calling itself the "single source of truth". _nix_eval_utils.py serves only scripts and tests and refers to a non-existent `suit_task._resolve_bash_store_path` (:8, :49). The dead modules carry their own tests: tests/test_partition.py (434 lines), tests/test_prebuild.py (399), tests/test_drv_graph.py.
- Fix: delete them, move `VariantSpec` into a types module, and move the dev helpers out of the runtime package (P38).

### P24 (medium, dead code) Dead functions and parameters inside live modules
- Evidence:
  - python/compiler_suit_runner/preflight.py:1912-1953 `preflight()` returns empty variants, common deps and drvs, so `cmd_preflight` (cli.py:2197-2219) always prints zeros.
  - `filter_existing_variants` (:1956), `path_info_batch` (:1824) and `query_initial_toolchain_placement` (:2102) are used only by tests.
  - `build_toolchains_locally(aggregate_drv=...)` is ignored (:2191).
  - `check_toolchains_locally` costs N subprocesses and its result only feeds a log line (cli.py:1645-1663).
  - `emit_all_manifests` ignores `num_workers` and `per_binary_metadata` (manifest_gen.py:796-797). Production passes `variants=()` and `common_deps=()` (cli.py:1733-1735), so `make_build_variant_header`/`make_build_common_dep_header` never run.
  - `_run_import_prelude` is a documented no-op that is still called inside a try/except (build_worker.py:1799-1830, :2017-2028).
  - `read_peer_push_urls` and its alias (eval_worker.py:1223-1268) have no callers.
  - `SuitTask.wire_primary_handle`/`apply_unfulfillable_reinject_cap` (suit_task.py:2889-2956) are called only from tests; the framework applies the cap from the CLI namespace.
  - The "legacy in-process dispatch surface" (suit_task.py:3407-3487) is dead (see P7).
- Fix: delete them, along with tests such as test_cli.py:1671-1695 that cover only dead paths.

### P25 (low-medium, dead code) Config fields and listener state that nothing reads
- Evidence: in python/compiler_suit_runner/suit_task.py, `SuitTaskConfig.allow_toolchain_build` (:1054), `allow_observer_as_holder` (:1064), `gateway_url` (:1091) and `slurm_root_folder` (:1096) are never read; the docs at :1084-1096 describe a "quiesce watcher" mirror step that no longer exists. `replication_k` is read through `getattr(self.config, "replication_k", DEFAULT_REPLICATION_K)` (:2112-2115) even though the frozen dataclass always has it. `_PeerLifecycleListener._observer_peers` is written but never read (:735, :760, :798), and `_record_observer_holdings` only logs (:2772-2790).
- Fix: delete the fields, the getattr fallback and the observer branch. Also make `matrix_eval_out_dir` required where it is used: suit_task.py:1867-1871 passes `str(None)` as `--matrix-eval-out-dir` when it is unset.

### P26 (medium, stale shim) Compatibility layers for framework versions the flake no longer pins
- Evidence:
  - `_make_task_info` retries `TaskInfo(...)` on TypeError twice, dropping kwargs each time (python/compiler_suit_runner/suit_task.py:404-468). This also hides genuine TypeErrors such as a wrong type or a typo.
  - `_make_task_dep` returns a `SimpleNamespace` in production when TaskDep rejects `phase_id` (:504-515). The framework can't use that object, and the docstring says that silently zeroes the downstream phase (:476-483).
  - `getattr(handle, "fail_permanent", None)` "framework pin is too old" probes appear at :2675, :2709 and :2745.
  - manifest_glue.py:51-53 and :101-135 probe `dataclasses.fields(ManifestHeader)`, a class in the same package.
  - The planner accepts tuple, list and "hash-name" shapes with bytes-or-str hashes as "legacy" (dependency_graph_planner/shapes.py:21-59).
  - `dependency_graph_planner/__init__.py:85-115` re-exports 19 private names (ruff F401).
- Why it matters: every shim makes a version mismatch degrade quietly instead of failing at import.
- Fix: import the framework types directly and fail at import if the pin is wrong. Tests should use a single real-or-fake framework module (P40). Accept exactly one planner shape.

### P27 (low-medium, stale docs) Comments and docstrings describe code that has changed
- Evidence: suit_task.py:586-593 (P14). build_worker.py:2336-2347, where the `--matrix-eval-out-dir` help talks about "resume markers ... `<binary>/manifest.json`". affine_import_worker.py:77-79 says `--flake-ref` is threaded, but suit_task.py:1867-1876 does not pass it. dependency_graph_worker/run.py:131 cites a non-existent `preflight._build_toolchains_aggregate_drv`. manifest_glue.py:59-64 still calls manifest_gen "mid-rename". The baseline doc at preflight.py:1402-1404 contradicts cli.py:1206-1222. `PreflightResult` docs (preflight.py:64-94) describe fields that are always empty. There are 29 references to private plan files and issue tags (`~/.claude/plans/lively-beaming-summit.md` at eval_worker.py:96, `splendid-snacking-cascade.md` at build_worker.py:21, Q1-Q4, "Framework Ask #3", D.1a, Wave-1).
- Why it matters: comment volume often runs at 3-10 times the code (SuitTaskConfig spends 200 lines on 30 fields), and the history-style prose is the part that goes stale first.
- Fix: keep the contract (what, invariants, why) in docstrings. Move incident history to commit messages or docs/. Grep-delete the private plan references.

---

## E. Typing, configuration and global state

### P28 (high, maintainability) Resolved CLI options ride on argparse.Namespace as private attributes
- Evidence: python/compiler_suit_runner/cli.py:1434 and :1440 assign `args._build_deps_local_resolved` and `args._no_substituters_resolved`. They are read through nested `getattr` fallbacks at :772-782, :805, :1897-1900 and :1998-2001. The secondary path has to rebuild them by hand (:2312-2327), and the comment there records a production bug ("FATAL on cross arches") caused by missing one. Defaults are copied between argparse, getattr and SuitTaskConfig: `22222` at :786, :1979, :2298; `"x86_64-linux"` at :824, :839, :1226, :2292; `"42"` at :843, :1516, :2208; `5005` at :1909. Env booleans are parsed two ways, `{"1","true","yes","on"}` at :637 and :679 versus `== "1"` at :2297 and :2312.
- Fix: a frozen `RunOptions` dataclass built once by `resolve_options(args, env)` and used for both submit and the framework-spawned secondary. One `env_flag(name, default)` helper. `_config_from_args` takes `RunOptions`, never a Namespace.

### P29 (medium, typing) Stringly-typed dicts carry the main data flow
- Evidence:
  - `per_binary_metadata: dict[str, dict]` (python/compiler_suit_runner/suit_task.py:1117) is flattened with renamed keys (`sample_size` becomes `variant_sample`) in cli.py:1559-1586 and re-validated with `isinstance(meta, dict)` in suit_task.py:1499-1506 and :1555-1582.
  - The task payload is a `{item_class, name, size, payload}` wrapper that build_worker.py:2427-2463 sniffs and unwraps.
  - run.py:731-744 reads descriptors with `getattr(d, "kind")`/`hasattr(d, "payload")`.
  - Dict keys are spelled in many places (`toolchain_aggregate_drv`, `toolchain_outpaths_map`, `prestaged_matrix_eval`, `"<arch>/<comp>"` keys).
- Data loss: matrix_eval evaluates full per-variant metadata (flags, hardening, sanitizer, march; used by `is_known_bad_combo`) and throws it away. dependency_graph_worker/archive.py:747-770 later rebuilds a subset by parsing drv names and leaves `flag_set`/`hardening`/`sanitizer`/`march` empty. build_worker.py:2160-2174 writes those empty strings into every dataset sidecar JSON.
- Fix: dataclasses `BinaryMeta`, `MatrixEvalPayload`, `DepGraphPayload`, `VariantPayload`, `CommonDepPayload` with `to_wire`/`from_wire`. Carry the evaluated meta through the matrix-aggregate sidecar (one already exists, `drv_map.json`) so sidecars keep the flag axes.

### P30 (medium, global state) build_worker keeps module-level import ledgers
- Evidence: python/compiler_suit_runner/workers/build_worker.py:82-114 and :1519 define six mutable globals. eval_worker reaches into them (eval_worker.py:629-633). Tests reset only a subset: tests/test_eval_worker.py:69-76 resets three of the six.
- Fix: an `ImportLedger` instance owned by the worker process context and passed to the gate helpers. That also fixes P1, and tests get a fresh ledger each time.

### P31 (medium-low, configuration) Container paths are hard-coded in nine modules
- Evidence: `/app/log-network` at python/compiler_suit_runner/cli.py:2155, :2281, suit_task.py:2405, build_worker.py:1154, :2082, :2359, :2506, build_compilers_worker.py:780, affine_import_worker.py:141, ssh_debug.py:59. `/app/out-tmp` and `/app/out-network` at cli.py:2287, :2291, build_worker.py:2477, eval_worker.py:652. `/app/flake` at eval_worker.py:847. `/app/src-tmp` at dependency_graph_worker/archive.py:231.
- Fix: one `container_layout.py` with named constants or an object injected from config.

---

## F. Error handling

### P32 (medium, error handling) Broad except is the default
- Evidence: `except Exception`/`BaseException` counts are 44 in suit_task.py, 37 in cli.py and 18 in build_worker.py. In `on_run_start`, every subsystem failure (signing key, watchers, replication, push server, harmonia, nix-daemon) is logged and skipped (suit_task.py:2006-2010, :2049-2053, :2066-2070, :2145-2152, :2340-2344, :2434-2437). cmd_secondary continues after on_run_start fails (cli.py:2120-2127). `is_known_bad_combo` swallows any error from `is_known_broken` (preflight.py:844-853). `_publish_common_dep_archive` swallows everything (build_worker.py:1792-1796), which moves the failure to a different node (P2). discover_items skips unreadable manifests at WARNING (suit_task.py:1410-1416).
- Why it matters: a node can run with no peer watcher or push server and nothing reports it beyond a log line. Failures surface far from where they started.
- Fix: mark components required or optional (P11). Required failures abort start-up. Optional ones produce a health summary logged once at INFO. Catch only the exceptions you expect, and let programming errors propagate.

---

## G. Performance

### P33 (high, performance) build-deps closure spawns two nix-store processes per seed drv
- Evidence: python/compiler_suit_runner/workers/dependency_graph_worker/run.py:829-844 runs `nix-store --query --outputs` once per seed, and :859-879 runs `--requisites --include-outputs` once per seed. The seeds are every variant drv plus every common dep, thousands at full scale. :945 then passes every input outpath on argv to `nix-store --realise`.
- Why it matters: this runs serially on the primary-pinned dep_graph task, on the critical path to the build phase.
- Fix: one call each with `--stdin` (or 1k-path chunks). `--requisites --include-outputs d1 d2 ...` already returns the union you want. Feed `--realise` from stdin or in chunks.

### P34 (medium-high, performance) Preflight and eval issue per-item nix calls that could be batched
- Evidence:
  - `check_toolchains_locally`: one `nix path-info` per drv (python/compiler_suit_runner/preflight.py:2086-2098), although `path_info_batch` exists in the same file.
  - `build_toolchains_locally`: one `nix build` per drv (:2243-2253) and one requisites query per drv (:2294-2307), where a single call returns the union.
  - `collect_toolchain_archive_paths`: one requisites query per outpath, unioned (:2456-2473).
  - `compute_toolchain_split`: three nix-store calls per toolchain (:1343-1390).
  - `eval_native_build_host_stdenvs`: two flake evals per arch (:1604-1613). `eval_build_host_tools`: two evals (:1659-1666). `enumerate_variants`: two evals of the same attr where the second alone is enough (:875-893).
  - `_sample_per_arch`: one flake `nix eval` per arch per binary task (eval_worker.py:921-926).
- Why it matters: each flake `nix eval` re-evaluates the flake (seconds each). With about 300 toolchains the per-drv loops reach minutes, which is the problem path_info_batch's docstring describes (:1837-1840).
- Fix: use the batched helpers from P18. Evaluate drvPath and outPath together with `--json --apply`, and evaluate all archs in one `--apply` that returns `{arch: meta}`.

### P35 (medium, performance) Importing any worker loads the whole runtime, about 1.4 s
- Evidence: python/compiler_suit_runner/__init__.py:3 imports `suit_task`, which imports peer_cache, peer_replication, peer_push, build_worker, dependency_graph_worker and template_graph. `python -X importtime -c "import compiler_suit_runner.workers.affine_import_worker"` takes 1.43 s cumulative and loads 62 package modules. Every worker spawn and every per-type respawn pays this.
- Fix: make `__init__` lazy (module `__getattr__`) or empty. Workers import only what they use. That also makes the per-type worker_module option in P14 cheap.

### P36 (medium, performance) Memory and argv size in archive export
- Evidence: python/compiler_suit_runner/workers/eval_worker.py:824-833 buffers the whole `nix-store --export` output in memory before writing. `export_closure` passes the full closure on argv (build_compilers_worker.py:415-419), although eval_worker.py:162-165 explains that argv "trips MAX_ARG_STRLEN at full LMU scale". The streaming branch in export_closure runs only when `run_subprocess is None` (:425-447), so no unit test covers the code that production uses.
- Fix: one streaming `store_export(paths, dest, via_stdin=True)` used by every exporter, and give the fake runner a streaming mode so tests cover the production branch.

### P37 (low, performance) The manifest directory is scanned twice
- Evidence: `discover_items` reads every manifest (python/compiler_suit_runner/suit_task.py:1395-1462), and `_build_outpath_to_task_hash_lookup` re-reads them all (:2808-2885).
- Fix: build the lookup from the headers discover_items already parsed, or cache the parsed headers on the task.

---

## H. Packaging, tests and test structure

### P38 (medium, packaging) template_graph is an undeclared dependency, and the conftest path hack can't work
- Evidence: python/pyproject.toml:63-71 packages only `compiler_suit_runner*` and `ssh_debug_runner*`, while dependency_graph_worker/archive.py:38 imports `template_graph` at module level. tests/conftest.py:10-12 inserts the repo root into sys.path, but importing the conftest first imports the `compiler_suit_runner` package, which reaches template_graph through `__init__`. Plain `pytest` therefore fails with `ModuleNotFoundError: No module named 'template_graph'` before the hack runs. The `pip install -e 'python/[test,slurm]'` instructions in python/README.md:27 produce a broken install. scripts/ and _nix_eval_utils.py ship inside the runtime package.
- Fix: package template_graph (as its own distribution, or with an explicit `package-dir`), and remove the conftest hack and the PYTHONPATH instructions. Move scripts/ and the test-only helpers out of the installed package.

### P39 (high, test quality) "Unit" tests run real nix and pass because errors are swallowed
- Evidence: the test_cli submit tests never stub `eval_native_build_host_stdenvs`, `eval_build_host_tools` or `_compute_baseline_paths` (python/compiler_suit_runner/tests/test_cli.py:532-558 stubs the others). Running them with log capture shows ERROR "native build-host stdenv eval failed" while the test passes, and `test_produce_and_upload_split_archives_happy` really runs `nix build ...#_imageStoreRegistration` ("image-resident closure = 126 paths"). Slowest tests: 22.3 s, 11.2 s and 5-7 s for each `cmd_submit_hit_state_equals_miss_state` case. The cause is cli.py:1229, which calls `preflight._default_run_subprocess` directly with no seam, together with P4's swallowing.
- Why it matters: results depend on the developer's flake state and nix store, a slow suite gets skipped, and real regressions hide behind logged errors.
- Fix: inject one runner into `SubmitPipeline` (P12). Add an autouse fixture that makes any real `subprocess.run` of `nix*` fail the test unless it is marked `nix`.

### P40 (medium, test structure) Giant test files mirror the god modules, and fixtures are copied rather than shared
- Evidence:
  - Sizes: test_dependency_graph_worker.py has 3705 lines and 103 tests, test_build_worker.py 3122/111, test_suit_task.py 2504/86, test_eval_worker.py 2480/57, test_cli.py 2143/70, test_preflight.py 2099/86.
  - `monkeypatch.setattr` counts: 52 in test_build_worker, 43 in test_dependency_graph_worker, 42 in test_cli.
  - tests/conftest.py contains only the sys.path hack, with no shared fixtures. SuitTaskConfig factories are copied in five files (test_cachix_uploader, test_suit_task, test_eval_worker, test_discover_items, test_suit_task_topology), fake primary handles in two, and test_preflight.py alone has 56 ad-hoc runner/call-recorder helpers.
  - test_discover_items.py:63-77 overwrites pytest's private `tmp_path_factory._basetemp` from a session-scoped autouse fixture.
  - The framework is faked by injecting into `sys.modules` (test_build_worker.py:1140-1165).
  - The SLURM harness adds 17.8k lines under tests/slurm, including tests of the harness itself (test_invariants.py 1133, test_cluster_probe.py 1367). Unit tests total 31.4k lines against about 20k lines of source in this area.
- Fix: a shared `tests/fakes.py` (FakeRunner with scripted responses and call log, FakePrimaryHandle, FakeTask) and conftest fixtures (`suit_config`, `ledger`, `fake_runner`). Split test files along the new module boundaries as each module splits. Move tests/slurm to a sibling harness package.

---

## What is good

- Phase ids are a single `Phase` StrEnum (python/compiler_suit_runner/manifest_gen.py:65-84) used for both PhaseSpec and TaskDep, and cross-phase dependencies are modelled explicitly (`build_compilers_depends_on`) instead of encoded in strings.
- Some critical paths fail loudly on purpose: the streamed-spawn reconciliation barrier (suit_task.py:3018-3030), `custom_message_handler` raising by design, `_toolchain_build_closure` refusing to ship partial closures (preflight.py:1366-1386), and the build-deps completeness gate (dependency_graph_worker/run.py:975-986).
- Writes are atomic where it matters: manifests use fsync and replace (manifest_gen.py:608-630), cache entries are staged directories, and exports write to .tmp then replace.
- incremental_cache.py has typed axis dataclasses, versioned entries, length-prefixed hashing, and lookups that never raise.
- dependency_graph_planner/ and workers/dependency_graph_worker/ are already split into small, single-purpose modules (most under 300 lines) with typed `Phase4Descriptor`/`BinaryPlanInput` records. That is the shape the rest of the package should move toward.
- Subprocess injection (`run_subprocess=`) exists almost everywhere, so making tests hermetic means fixing the gaps (P39), not redesigning. The non-SLURM suite passes.
- Comments usually explain why, with the incident behind a decision, which is valuable even where it has grown too long.

## Top 5 issues, ranked

1. P1: import-once flags are set before success, so a retried gate silently skips the import.
2. P2: `import_build_deps` races the archive it gates, and a skipped archive hard-fails every node.
3. P3: the enumeration cache key ignores staged changes, so stale toolchain or variant sets get reused.
4. P4 with P39: cmd_submit swallows outpath and build-host eval failures, so required split archives are skipped quietly. The tests show the same failures being swallowed.
5. P5: `except BaseException` turns SIGTERM/SystemExit into NonRecoverableError, and the documented RuntimeError contract disagrees with the code.

The structural root cause behind most findings is three god functions (P11 on_run_start, P12 cmd_submit, P14 build_worker.main) plus contracts (archive names, task ids, payloads) that are spread across modules instead of owned by one (P16, P17, P29).

## Suggested refactor order

Each step should keep the suite green and land as its own change.

0. Safety net first. Fix P39 (inject a runner, block real nix in unit tests), add `tests/fakes.py` and conftest fixtures (P40), and add characterization tests that freeze `discover_items` output, `build_worker_command_args` argv per type, the gate-id set, and the spawn order of `_handle_streamed_spawn_batch`. Everything below moves code, and these tests make that safe.
1. Correctness fixes, which are small and local: P1, P2, P3, P4, P5, P6, P8, P9, P10. P1 is cleaner after step 4, but a two-line reorder fixes it now.
2. Delete dead code and shims (P23-P26, P7). This needs a check that the flake.lock dynamic_runner pin supports `TaskInfo(task_id, task_depends_on, is_secondary_affine)` and `TaskDep(phase_id)`; it should, since the live run relies on them. Removing code first means the later moves carry less.
3. Extract leaf contract modules with no internal dependencies: `nix_exec.py` (P18), `task_ids.py` (P17), `container_layout.py` (P31), `env_flags`/`RunOptions` (P28), and payload dataclasses (P29, starting with `BinaryMeta` and `VariantPayload`). This step breaks the build_worker/suit_task literal duplication, so later moves don't create import cycles.
4. Archive layer (depends on 3): `closure_archive.py` and `import_gates.py` with `ImportLedger` (P16, P30, P36). Point affine_import_worker, eval_worker, the dep_graph worker and preflight at it.
5. Split preflight (depends on 3-4): known_bad_combos, enumeration, toolchain_split, toolchain_realise. Move the build-deps closure into dependency_graph_worker (P15). Batch the nix calls at the same time (P33, P34).
6. Worker entry (depends on 4): module-level `workers/dispatch.py`, with each branch in its own worker module (P14). Make the package `__init__` lazy (P35) before choosing between per-type modules and a shared entry.
7. Split suit_task (depends on 3): `task_infos.py` (P19), `PeerMeshNode` with ordered required/optional components (P11, P32), and a streamed-spawn handler class. SuitTask becomes a thin facade over the framework hooks.
8. Split cli (depends on 3, 5, 7): `RunOptions` resolution, `SubmitPipeline`, `gateway_staging.py` (P12, P13). cli.py keeps argparse and dispatch.
9. Packaging (P38) and moving the test files to the new module boundaries (P40). This can run in parallel with steps 5-8.

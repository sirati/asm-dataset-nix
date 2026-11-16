# Code-quality review of the uncommitted compiler-capture change

Scope: the uncommitted working tree on `main` (HEAD 72dd7be), 105 files, about 21k lines. I read the code directly and ran no builds. Paths are relative to the repo root. Line numbers refer to the working tree.

Severity: high means it hurts maintainability, correctness or performance now. Medium is a real cost that can wait for a follow-up. Low is cleanup.

## A. Scope and coupling to the default dataset path

### C1 (high, scope/coupling) The change rewrites the ordinary dataset's toolchains

- Where: `lib/resolve-toolchain.nix:20-39` (GCC 4.6 cross and GCC 5 cross go through `gcc-driver-compat.nix`), `:86-103` (Clang 16, Clang 3.7-4.x gold/bfd, RISC-V 9-14, ppc64 and mips64el go through `clang-lto-linker.nix`), `:105-110` (every Clang <3.9 goes through `legacy-clang-response.nix`). Only the last wrapper layer checks capture (`:133`). `lib/old-compilers.nix:648` forces the hybrid wrapper for native Clang <5, `:673-706` removes the `pkgsCross` path for all cross Clang, `:680-684` swaps in a rebuilt Clang 3.4 (`legacy-clang-n32.nix`) and a new LLVMgold (`llvm-gold.nix`), and `:658,703` add `clangResourceFixup`. `lib/legacy-clang-stdlib.sh:33-41` and `lib/legacy-clang-stdlib.h:16-20` force-include a header that sets `_GLIBCXX_USE_CXX11_ABI 0`, and they add `-fPIC` on MIPS and ARM EHABI flags for Clang <3.5.
- Problem: these files change derivation hashes and code generation for `dataset`, not only for `datasetCaptured`. `legacy-clang-response.nix:2` admits this: "shared by baseline and capture toolchain resolution". Three places claim the opposite: `flake.nix:157` ("Keep ordinary dataset defaults unchanged"), `README.md:109`, and `docs/compiler-capture-followup-plan.md:114` ("Do not change ordinary dataset identities").
- Why it matters: every affected compiler/target pair in the default matrix rebuilds, and the compiled code changes (C++ string ABI, PIC, unwind tables, linker choice). There is no dataset version marker, so a partial rebuild leaves the corpus mixing old and new toolchain behavior. (Correction from the sol-loop: the label-based skip in `filter_existing_variants`, `python/compiler_suit_runner/preflight.py:2018`, has no production caller.) The derivation names also say "capture" (`capture-clang-*-lto-linker`, `capture-gcc-linker-search`, `capture-legacy-*`) while the code serves the baseline.
- Fix: move these into their own commit(s) titled as toolchain fixes, with a changelog entry and a dataset namespace or version bump. If they are needed only for capture qualification, gate them on `capture != null`. Rename the derivations and files so they do not say "capture".

### C2 (high, scope/coupling) The ELF collector changed for every variant

- Where: `lib/mkBinaryFolder.nix:34,82` replace the `file -bL | grep ^ELF` pipeline with `lib/collect-elf.py`. `collect-elf.py:14-48` now accepts only ET_EXEC/ET_DYN files that have a file-backed executable PT_LOAD. `lib/elfFolderBuildTools.nix:16` adds `buildPackages.python3`.
- Problem: the corpus definition changes for the default dataset. Relocatable `.o` files, kernel modules and debug-only ELFs used to be collected and now are not. Every elf-folder derivation hash changes. `elfFolderBuildTools` is the list that commits 2a3e47e and 5166e4d ship into `import_common` with all outputs, so every node now receives a full Python closure.
- Why it matters: the corpus changes silently. The label-based skip keeps old variants under the old semantics, and the shipped closure grows.
- Fix: commit the collector separately with an explicit dataset-format note, or keep the old collector for `dataset`. To avoid the Python dependency, put the ELF check into the Rust tool, which is already a build-host package, or keep it in shell with `readelf`/`od`.

### C3 (medium, coupling/error handling) The non-capture worker path now runs through capture code

- Where: `python/compiler_suit_runner/workers/build_worker.py:2791-2799` imports `capture` and `capture_publish` for every publish and widens the error catch to `(PublishError, OSError, ValueError)` as `NonRecoverableError`. `:2146-2153` parses `meta.json` for every variant. `:2154-2159` has a bug: when `auxiliary/datasetCapture` exists but there is no contract, `capture_contract.get(...)` raises AttributeError. `manifest_gen.py:641-664` (chunked NUL-padding parse in `read_manifest`) is unrelated to capture.
- Why it matters: a transient NFS `OSError` in an ordinary publish used to propagate. Now it is non-recoverable. Unrelated fixes inside a 21k-line feature commit are hard to review or revert.
- Fix: decide capture-vs-ordinary once from a typed flag, and leave the ordinary path byte-identical, including its exception policy. Move the `read_manifest` fix into its own commit.

### C4 (medium, duplication/naming) A capture boolean is threaded through ten Python files

- Where: 15 occurrences of `**({"compiler_capture":True} if X else {})` across `cli.py`, `preflight.py`, `manifest_gen.py`, `suit_task.py`, `eval_worker.py`, `build_worker.py` and `dependency_graph_worker/run.py`. String ternaries pick flake roots and names: `"datasetCaptured"`/`"dataset"`, `"_capturedMeta"`/`"_meta"`, `"_capturedCrossToolchainMap"`/`"_crossToolchainMap"` (9 sites), `"_matrix_eval_capture_v2"` (`cli.py:741,2302`), `"__capture_v2"` (`manifest_gen.py:258`, `descriptors.py:310`). Meanwhile `capture.py:57` derives `_capture-v{N}` from `PUBLICATION_SCHEMA`.
- Why it matters: bumping the publication schema means editing literals in four files. A missed site gives a task ID or archive namespace that disagrees with the variant directory.
- Fix: one frozen `DatasetNamespace` dataclass (`dataset_attr`, `meta_attr`, `toolchain_map_attr`, `matrix_eval_dir`, `task_suffix`, `variant_suffix`) built once from `PUBLICATION_SCHEMA` and passed down explicitly. Drop the conditional kwargs splats.

## B. Rust structure

### C5 (high, separation of concerns) `collect::run` is an 875-line function

- Where: `rust/compiler-capture/src/collect.rs:177-1051`. The one function prepares response files, analyzes arguments, and computes about fifteen version- and target-specific predicates: `correct_cfi` 282, `external_cfi` 287, `riscv_alignment` 291, `default_integrated` 304, `native_from_ir` 316, `arm45_cfi` 351, `legacy_lto` 366, `generate_candidate` 381 (pinned to `version == "15.2.0"` at 384), `x86_tuning` 718. It then selects the backend, injects arguments (405-542), runs three post-run directory scans (786-833, 840-911, 912-948), reclassifies roles (949-993) and builds the record (1039-1046).
- Why it matters: the compatibility rules decide compiler behavior, yet they cannot be tested without real compilers. `collect.rs` has 2 unit tests, both for assembler options. A rule for one compiler version sits among unrelated booleans, so changing one risks the others.
- Fix: extract a pure `plan(&CompilerIdentity, &Analysis) -> CapturePlan` that holds all decisions (backend, added arguments, corrections, expected roles) and cover it with table tests per compiler version and target. Keep `run` as plan, execute, collect, record. Put each quirk in a named function in a `quirks.rs`, with a doc comment that cites its measurement.

### C6 (high, separation of concerns) `trace.rs` mixes six responsibilities in 2379 lines

- Where: unsafe ptrace primitives and remote mmap (`trace.rs:72-365`), syscall decoding (`566-624`, `1273-1527`), argv rewrite policy in a 485-line `Mapper::rewrite` (`786-1271`) covering Clang native-IR, PCH, GCC/Clang directives-only, legacy GCC LTO, module maps, CFI and x86 NOP features, assembly text rewriting (`678-784`), the event loop (`run_started`, `1614-2318`, about 700 lines), and snapshot plus JSON metadata building inside the loop. `Mapper` has 23 fields (`540-564`) and `Options` has 18 (`519-538`), mostly per-quirk booleans that `collect.rs` computes. `Task.driver` (`1926-1953`, `2260-2266`) actually means "resume with PTRACE_SYSCALL"; a driver is not required.
- Fix, as a module tree: `ptrace/{sys.rs, remote.rs, syscall.rs}` (C12), `trace/event_loop.rs` (tasks, cleanup, resume policy), `rewrite/{paths.rs, gcc_lto.rs, clang_native.rs, pch.rs, modules.rs, directives.rs, x86.rs}`, each implementing one `RewritePass` that is fed the decoded exec, and `asm_fixups.rs` for the CFI/RISC-V transforms. Rename `driver` to `trace_syscalls`. Replace the 18-field `Options` with the `CapturePlan` from C5.

### C7 (medium, parsing) One argument parser serves GCC and Clang, and some operand options are missing

- Where: `arguments.rs:240-491` uses one table that mixes GCC, Clang and Darwin (`-arch`) options. Family differences live elsewhere: `collect.rs:213` (legacy response dialect), `:222` (the `cpp` driver turns Link into Preprocess), and executable-name matching in `trace.rs:1014-1027,1900-1934`. The separate-operand list (`arguments.rs:335-375`) lacks GCC driver options such as `-T`, `-z`, `-u`, `-e`, `-aux-info`, `-imultilib` and `-iframework`. `gcc -z now x.o -o p` therefore records `now` as an input, `trace_eligible` turns false (`collect.rs:154`), and a required capture fails with "no verified isolated backend". Language names are byte strings repeated in `collect.rs:86-89,140-151,276-281`, `arguments.rs:54-109` and Python `capture.py:26,773-781`.
- Fix: a `Language` enum and per-family option tables (`gcc_options`, `clang_options`) with arity, selected from `Config.family`. Unknown options stay "incomplete". Test both tables against the drivers' `--help=common`/option lists in a fixture.

### C8 (medium, typing) The domain model is stringly typed

- Where: `config.rs:12-30,46-77` stores `family`, `backend` and `debug_policy` as `String` checked against lists, and `collect.rs` compares them by string about 30 times. Artifact `role` appears in 50 literals and `path_base` in 23 across `src/*.rs`. Extension-to-role mapping exists five times: `collect.rs:69-78` (`role`), `:863-885` (LTO kind), `:158-175` (`primary_role`), `trace.rs:626-648` (`output_role`), `:2148-2153` (module role). The version is parsed ad hoc twice (`collect.rs:207-212,439-444`, with `unwrap_or(0)`) and matched by prefix (`config.rs:78` `"3.8."`, `collect.rs:384` `"15.2.0"`).
- Fix: serde enums `Family`, `Backend`, `DebugPolicy`, `Role`, `PathBase` (`#[serde(rename_all = "kebab-case")]`), a `Version { major, minor, patch }` parsed once in `Config::load`, and one `Role::for_output(path, args)`.

### C9 (medium, schema) The record and manifest schemas are implicit `json!` literals

- Where: `collect.rs:1039-1046` (one unformatted statement, which rustfmt skips inside macros), `trace.rs:403,2052-2055,2172,2180,2299`, `storage.rs:505,685-688`. `storage.rs:685` hard-codes `"wrapper_revision":1` although `config::WRAPPER_REVISION` exists.
- Fix: typed `Record`, `Subprocess`, `Transformation` and `Manifest` structs in `record.rs`. Generate a JSON Schema, or a constants file, that Python tests load (see C28).

### C10 (medium, dead weight) Experiment-only backends ship in the production supervisor

- Where: `gcc-dumpdir` (`collect.rs:381-387,496-510,786-833`, GCC 15.2.0 only) and `observe` appear only in `tests/compiler-capture/fixtures.nix:407-412,446`. Production always passes `"${family}-trace"` (`lib/resolve-toolchain.nix:123`). `collect.rs:1041` labels every untraced record `"experimental-gcc15-single-c-cxx"`, including tool-observe and stdout captures. `tests/compiler-capture/job_wrapper.py` describes itself as "Exploratory ... not a supported compiler backend", yet `fixtures.nix:51` still references it.
- Fix: delete `gcc-dumpdir`, `observe` and `job_wrapper.py`, or put them behind a cargo feature used only by the qualification fixtures. Make `coverage_contract` an enum that names the backend actually used.

### C11 (low, simplification) The Clang 3.8 assembler search path is read at runtime from a side file

- Where: `config.rs:78-109` opens `<wrapper>/nix-support/capture-assembler-search` on every invocation, with `O_NOFOLLOW` and size limits. `lib/compiler-capture.nix:9-18` already knows the path at build time.
- Fix: write `assembler_search` into the config JSON and delete the runtime read and the `#[serde(skip)]` field.

## C. Unsafe and ptrace code

### C12 (medium, clarity/safety) 55 `unsafe` sites, no SAFETY comments, and the syscall ABI decoded by hand in five places

- Where: raw `usize` casts into `libc::ptrace` (`trace.rs:72-101`). The `#[repr(C)]` PTRACE_GET_SYSCALL_INFO struct is defined twice (`1274-1282`, `1730-1738`). Magic numbers: `0x33` (CS selector, 174), `0xcc050f` (patched "syscall; int3", 200), `SIGTRAP | 0x80` (five sites), `status >> 16`. x86-64 syscall argument registers are decoded separately in `module_path` (566-597), `header_input` (599-624), `unlink` (1294-1301) and `linker_file` (1330-1343). Four copies of "write the path into scratch memory, then set rsi or rdi" (1372-1379, 1409-1416, 1447-1455, 1515-1523). Open-flag tests are written three times (1348-1363, 1389-1402, 1422-1427/1473-1478). Syscall entry is detected by `rax == -ENOSYS` at 2198 but by GET_SYSCALL_INFO `op` at 1290. `let _ = output;` at 1315 is dead. `0 if signal == SIGSTOP => ()` at 2256 also swallows a real group-stop SIGSTOP.
- Fix: a `ptrace` module with a `Tracee(pid)` type that has `regs`, `set_regs`, `read_cstr`, `write` (via `process_vm_readv`/`writev`, see C15), and `syscall_stop() -> SyscallStop { entry: bool, nr, args: [u64; 6] }`. Add a `PathSyscall::decode(&SyscallStop) -> Option<{ path_arg: ArgSlot, dirfd, flags, kind }>` and `redirect_path(slot, bytes)`. Name the constants. Put a SAFETY comment on each `unsafe` and document the remote-mmap injection invariants (instruction restore, registers, exec-exit stop ordering) in one place.

### C13 (low, platform) The x86-64-only limitation is enforced late

- Where: `main.rs:11` (`#[cfg(target_arch = "x86_64")] mod trace`), `collect.rs:372-375,651-749` (cfg blocks with `unreachable!()`), and `nix/compiler-capture.nix:64` (`platforms = lib.platforms.linux`).
- Why it matters: on an aarch64 builder `datasetCaptured` evaluates, then each compile records "no verified isolated backend" and the build fails at finalize.
- Fix: set `meta.platforms = [ "x86_64-linux" ]`, add an assert in `resolve-toolchain.nix` when `capture != null`, and keep a single cfg at the backend boundary.

## D. Performance

### C14 (high, perf) Syscall tracing covers far more processes than it needs

- Where: tasks with `driver = true` resume with `PTRACE_SYSCALL` (`trace.rs:2260-2266`). That always includes the root driver (1926). It includes any task given a 4 MiB path pool (1910-1935): linkers and coordinators under legacy LTO, debug-object redirection, or the save-temps plugin, plus every task once `pch_sources` is non-empty. It includes every task when `-gsplit-dwarf` is present (`task.modules = split_dwarf || ...`, 1941-1951) and PCH preprocessors (1952). Cloned threads inherit the flag (1808), so every LTO worker thread of `ld` is traced too.
- Why it matters: each read, write, mmap and brk of those processes costs two stops, a GETREGS, a GET_SYSCALL_INFO (1284), and often `readlink /proc/pid/cwd` plus `canonicalize`. LTO links and `cc1` under split DWARF pay this on every syscall. No document measures per-compile overhead.
- Fix: in `pre_exec`, install a seccomp-BPF filter that returns `SECCOMP_RET_TRACE` only for `open`, `openat`, `creat`, `unlink*`, `rename*`, `stat*` and `access*`, and use `PTRACE_O_TRACESECCOMP` plus `PTRACE_CONT`. This removes stops for all other syscalls. Measure wall time on `hello`, `zlib` and one LTO package before and after, and record a budget in the docs.

### C15 (medium, perf) Tracee memory is read one word per syscall, and paths are canonicalized for every argument

- Where: `bytes()` (`trace.rs:131-151`) issues one PEEKDATA per 8 bytes for every argv string of every exec. `gcc_auxiliary_environment` (367-408) walks the entire Nix build environment, often more than 100 KiB, for every `collect2` exec (1895-1896), not only LTO links. `write_bytes` (153-170) does a peek and a poke per word. `absolute()` (410-418) calls `fs::canonicalize(parent)` for every argument, including flags such as `-O2`, in three or four passes per exec (793-797, 866-868, 893-894, 1030-1068). `self.paths.values().flat_map(..).find(..)` is a linear scan per exec (1107-1112).
- Fix: use `process_vm_readv`/`process_vm_writev`. Skip arguments that start with `-` before canonicalizing, cache the canonical cwd per exec, and keep a reverse index from staged path to `Output`.

### C16 (high, perf) Each artifact byte is hashed about a dozen times across its lifecycle

- Where, in the sandbox: `storage.rs:366-377` hashes once, then `414-426` copies and hashes again for each new object. `finalize` re-verifies the source (`644-648`), copies, fsyncs and verifies the destination (`667-681`). That is four SHA-256 passes. In the worker: `stage_captured_variant` validates the source (`capture.py:912`), copies, validates the copy (`948`) and hashes every file for the index (`1013`). `publish_outputs` calls `captured_variant_complete` on the staged generation (`capture_publish.py:89`; it rehashes every file at `capture.py:1073` and calls `validate_capture` again at `1078`), then repeats both on the NFS copy (`capture_publish.py:191`). `filter_existing_variants` (`preflight.py:2039-2046`) would also call `captured_variant_complete` per captured variant, but it has no production caller today.
- Why it matters: with the 1 GiB publication limit this means several GiB of NFS reads per variant at publish time.
- Fix: hash once while copying in the sandbox and treat CAS names as verified within one process. Validate once after transport. For reuse checks, compare the completion document's own digest and file sizes and mtimes (cheap). Keep deep verification as an explicit `capture-inspect` or audit command.

### C17 (medium, perf) Every reservation does a flock, a JSON rewrite and two fsyncs on throwaway tmpfs

- Where: `reserve`/`release` (`storage.rs:283-336`) take `flock`, parse `.usage` JSON, and write a temp file with fsync, rename and directory fsync (`atomic_write`, `159-174`). `metadata()` (`489-502`) does reserve, atomic_write, snapshot and release. `set_counters` fsyncs (`525`). `finalize` fsyncs each object written to `$out` (`679`).
- Why it matters: the scratch root is a private tmpfs discarded when the namespace exits (`isolate.rs`), so the durability buys nothing. The per-object fsync in `$out` costs real disk I/O for up to 4096 objects.
- Fix: keep the byte and file counters in one small fixed-layout file updated under the existing lock (as `.counters` already is), without fsync on scratch, and drop the per-object `sync_all` in `finalize`.

### C18 (medium, perf) Each PCH consumer rescans all records

- Where: `pch::sources` (`pch.rs:87-124`) reads and parses up to 16 MiB of records for every `-include-pch` invocation.
- Fix: the producer writes `pch-producers/<sha>.json` (sources plus input states) when it commits, and a consumer reads that one file.

### C19 (medium, perf) Each compile runs through a long process chain, and nobody has measured it

- Where: cc-wrapper bash, then a `writeShellScript` launcher (`lib/compiler-capture.nix:63-65`, `lib/capture-bintools.nix:24-26`, `lib/capture-raw-compiler.nix:35`), then `compiler-capture`, then the driver. Inside a traced tree, every `as`/`ld` exec also passes through a bash launcher and a supervisor exec, and each extra exec stop pays the argv read and canonicalize passes. `-save-temps` and `-fno-integrated-cc1` (`collect.rs:416-418,445-447`) multiply frontend work.
- Fix: replace the shell launchers with `makeBinaryWrapper`, or let the supervisor locate its config next to `argv[0]`. Add an overhead benchmark to the qualification harness.

### C20 (medium, Nix eval cost) Toolchain resolution is recomputed for every variant

- Where: `lib/matrix.nix:345-364` calls `mkVariant` twice per entry (`elfFolder` and `rawPkg`). Each call runs `resolve-toolchain.nix` (`lib/mkVariant.nix:28`), which now stacks up to four `symlinkJoin`/`runCommand` layers with inline Python per toolchain. The captured matrix adds about 15 more derivations per toolchain (`lib/compiler-capture.nix:27-77`: writeText, runCommand and writeShellScript for each of 5 entries; `capture-bintools.nix`: 5 entries with 2 each; the raw compiler). Nix does not memoize function calls, so all of this is re-evaluated for every variant. (Correction from the synthesis pass: `rawPkg` is never forced, so the duplicate `mkVariant` call costs nothing. The per-variant re-evaluation of `resolve-toolchain.nix` through `elfFolder` is real.)
- Fix: resolve each `(compiler, target)` once in `matrix.nix` (an attrset keyed by label, reused by `crossToolchainMap` at `:474`), pass the stdenv into `mkVariant`, and share one `mkVariant` result between `elfFolder` and `rawPkg`.

### C21 (high, coupling/perf) The supervisor derivation depends on tests and the Python validator

- Where: `nix/compiler-capture.nix:19-34,47-60`. The supervisor's `postCheck` uses `capture.py`, `capture_debug.py`, `test_native.py`, `capabilities.py`, `ppc_debug.py` and other test files. The supervisor store path is embedded in every captured config (`lib/compiler-capture.nix:35`).
- Why it matters: editing a Python test or the validator changes the supervisor hash, which rebuilds every captured compiler wrapper and every captured package.
- Fix: keep `src` limited to the Rust tree and move these checks into `passthru.tests` or `checks.<sys>.*` that depend on the supervisor, not the other way round.

### C22 (low, provenance) Provenance hashes the whole `flake.lock`

- Where: `lib/mkBinaryFolder.nix:61`. The dynamic-runner bump staged in this change (`flake.lock:10-14`) is a runner input unrelated to the compiled bytes, yet it changes every captured elf-folder derivation and its publication identity.
- Fix: record the revisions of the inputs that build the package (nixpkgs and the old-nixpkgs pins).

## E. Nix organization and robustness

### C23 (medium, correctness/maintainability) Adapters are built by merging attribute sets with `//`

- Where: `lib/compiler-capture.nix:22,114`, `capture-bintools.nix:34`, `clang-lto-linker.nix:138`, `gcc-driver-compat.nix:48`, `legacy-clang-response.nix:45`, `llvm-gold.nix:34`, `legacy-clang-n32.nix:62`.
- Problem: the result keeps the original's `override`, `overrideAttrs`, `outputs` and `passthru`. `stdenv.cc.overrideAttrs` or `.override`, which nixpkgs and some packages call, returns the unadapted wrapper and silently drops capture or the compatibility fix. `passthru.datasetCapture` and top-level `datasetCapture` are duplicated (`compiler-capture.nix:118-129`).
- Fix: build adapters with `cc.overrideAttrs (old: { postFixup = old.postFixup + ...; passthru = old.passthru // ...; })` or with `wrapCCWith`/`wrapBintoolsWith`, so overrides compose. Otherwise set `override` and `overrideAttrs` to `throw`.

### C24 (medium, fragility) Text patches against nixpkgs wrapper internals are scattered and untested

- Where: `clang-lto-linker.nix:123-132` inserts before the exact string `if (( "${NIX_CC_USE_RESPONSE_FILE:-1}" >= 1 )); then`. `legacy-clang-response.nix:19-37` replaces `printf "%q\n"` and a regex marker. `compiler-capture.nix:53` regex-reads the response-file default from the wrapper. `gcc-driver-compat.nix:37` substitutes executable paths. `clang-lto-linker.nix:33-115` holds about 80 lines of Nix-interpolated bash with version conditionals and no tests.
- Fix: one `wrapper-hooks.nix` that owns every marker and insertion, failing loudly as now. Move the bash fragments into `.sh` files with `@substitutions@`, as `legacy-clang-stdlib.sh` already does, and test them like `test_legacy_stdlib.py` does.

### C25 (medium, layout/naming) File placement and names do not say what things are

- `nix/compiler-capture.nix` builds the Rust package. `lib/compiler-capture.nix` adapts a cc wrapper. They share a basename.
- `lib/capture-overlay.nix` is not an overlay. It transforms one package.
- `lib/capture-stdenv.nix` is a generic "overrideCC while keeping disallowedRequisites" helper that the baseline also uses.
- Non-Nix helpers (`legacy-*.sh`, `legacy-clang-stdlib.h`, `collect-elf.py`) sit flat in `lib/` beside the dataset definitions. Store names such as `capture-collect-elf.py` (`mkBinaryFolder.nix:34`) and `capture-legacy-response-quote.sh` are used by the baseline.
- Proposed layout:
  - `lib/capture/{wrap-cc.nix, wrap-bintools.nix, raw-drivers.nix, package.nix, contract.nix}`
  - `lib/toolchain-compat/{resolve.nix, gcc-driver.nix, clang-lto-linker.nix, legacy-clang-response.nix, clang34-n32.nix, llvm-gold.nix, override-cc.nix, files/*.sh, files/*.h}`
  - `lib/elf-folder/{mkBinaryFolder.nix, collect-elf.py, build-tools.nix}`
  - `nix/capture-supervisor.nix`

### C26 (medium, test code in production) Production Nix carries fixture-only branches and inconsistent defaults

- Where: `capture-overlay.nix:40-49,85-88`: when `captureTool == null` it writes a fake `"output-only-fixture"` manifest. `reportUsage` writes `experiment-filesystem.txt` (`42-44`). Defaults disagree: the overlay uses `policy ? "observe"` and `limitBytes ? 64 MiB` (`:5`), `mkVariant.nix:353` uses `"required"` and 512 MiB, and `compiler-capture.nix:5` uses `backend ? "observe"` although production never uses it.
- Fix: move the fixture hooks into `tests/`. Make the parameters required with no defaults, or put the defaults in one place (the contract).

### C27 (low, readability) Inline Python heredocs in Nix, written densely

- Where: `compiler-capture.nix:43-61`, `capture-raw-compiler.nix:15-37`, `clang-lto-linker.nix:15-25,120-133`, `legacy-clang-response.nix:28-37`. These are semicolon-joined statements, each pulls `python3` in at build time for a few string edits, and none is tested. The capture Nix files also drop the spacing conventions used in `mkVariant.nix` and `old-compilers.nix` (`{cc,family,...}`, `name="..."`). The same dense style appears in `capture.py` (18 lines over 150 columns, `if ...:raise` one-liners) and `tests/compiler-capture/capabilities.py` (37 semicolon-joined lines). The repo has no ruff, black or nixfmt configuration to keep this consistent.
- Fix: one checked-in `patch-wrapper.py` with tests, or plain `substituteInPlace` where it suffices. Run nixfmt and a Python formatter over the new files.

## F. Duplication across Rust, Python and Nix

### C28 (high, duplication) The config schema, contract and limits exist in about eleven copies

- Config fields: Rust `config.rs:12-30`; Nix `compiler-capture.nix:29-39`, `capture-raw-compiler.nix:8-12`, `capture-bintools.nix:16-23`; Python `capture.py:24,650-671`; written by hand in `test_forwarding.py:37`, `test_storage.py:38`, `test_response.py:31`, `test_stdout.py:25` and `test_native.py`.
- `config_sha256` depends on Rust struct field declaration order. `capture.py:654-658` re-serializes "in this declared field order". Reordering fields in `config.rs` invalidates every existing capture.
- Contract: `lib/capture-contract.nix` and `capture.py:44-49` define the same dict. `build_worker.py:2151` compares the two by equality.
- Limits (1 GiB total, 512 MiB invocation, 128 MiB artifact, 16 MiB manifest, 4096 files) appear in `storage.rs:15-20,233-258`, `capture-overlay.nix:53-56`, `capture.py:18-23,688-694` and `capture_publish.py:30`.
- The scratch prefix `/tmp/.dataset-capture.` appears in `storage.rs:720` and `capture-overlay.nix:90`. FD 198 appears in `storage.rs:22-23` and `nix/compiler-capture.nix:9`.
- Fix: hash the config file's bytes, or canonical sorted-key JSON. Add one `capture-schema.json` holding field names, enums, limits and the contract. Nix reads it with `builtins.fromJSON`, Rust embeds it with `include_str!` and checks it in a unit test, Python loads it, and a shared helper gives tests their configs.

### C29 (high, duplication) Producer and validator both encode the compiler-quirk rules

- Where: `validate_capture` (`capture.py:776-862`) reimplements the expected roles per mode and per compiler version: Clang 3.6 and later emits IR (`842-847`), GCC 4.5 ARM (`728`), RISC-V 14-16 (`730-732`), LTO native objects (`848-860`). This mirrors `collect.rs:912-1005` and `trace.rs` `CfiCorrection`. `_cfi_transformation_hash` (`capture.py:149-190`) copies the exact RISC-V preamble bytes and the ARM `.cfi_sections` line from `trace.rs:704-708,776`. The transformation kind list (`capture.py:725`) is a third copy.
- Why it matters: every new quirk needs edits in two languages that must stay in sync, and a mismatch shows up only as a validation failure on a real build.
- Fix: have the supervisor record the expected role set and each transformation's parameters (prefix bytes, replaced byte range) in the record. Python then checks generic consistency (declared roles present, declared transformation reproduces the hash) without version tables. Alternatively, expose `compiler-capture validate` and call it from Python.

### C30 (medium, duplication) ELF and DWARF parsing is implemented four times

- Where: `lib/collect-elf.py:14-48` (program headers), `capture.py:193-348` `elf_information` (sections, segments, symbols, code hash), `capture_debug.py:19-117,185-292` (sections, DWARF forms and units), `collect.rs:953-974` (header sniff), plus test helpers in `tests/compiler-capture/ppc_debug.py` and `capabilities.py`. LLVM bitcode magic checks are repeated in `collect.rs:958` and `capture.py:801,830,858`.
- Fix: one `elf.py` module used by the collector, the validator and the debug checker. The stdlib-only constraint applies only to the inspector, which is itself a Nix app, so `pyelftools` could replace the hand-written DWARF reader.

### C31 (low, duplication) Python import hacks and repeated helpers

- `capture.py:32-34` loads `capture_debug` through `importlib.util.spec_from_file_location`, and `capture_cli.py:10-13` uses a dual import.
- `capture_publish.py:19-42` reimplements `_unique_keys` and `_read_regular` from `capture.py:104-125`.
- Hex decoding is written twice in `pch.rs:19-33,128-142`.
- `TMPDIR` lookup appears three times (`trace.rs:427-429,2026-2028`, `arguments.rs:517`).
- `storage::hex` (`storage.rs:69-71`) calls `format!` per byte on every argument of every exec.
- Fix: a normal package with relative imports and a `__main__` entry, one shared JSON/IO helper module, and one hex codec in Rust.

## G. Error handling

### C32 (medium, hidden failures) Validation failures lose their reason

- Where: `captured_variant_complete` catches every exception and returns `False` (`capture.py:1113-1114`). `publish_outputs` then raises a generic "staged capture generation failed validation" (`capture_publish.py:89-90,191-192`). Preflight treats `False` as "not built" and silently rebuilds (`preflight.py:2039-2046`).
- Why it matters: a corrupt or contract-mismatched publication looks the same as a missing one, and the rebuild leaves no diagnostic.
- Fix: return `(ok, reason)` or raise `CaptureValidationError`, and log the reason at every call site.

### C33 (medium, policy) Capture failures affect the build inconsistently

- Where: `collect.rs:844-846` uses `?` on `read_dir` of the LTO stage after the compiler has succeeded, which returns `Err`, so `main` exits 125 (`main.rs:111-114`) even under policy `observe`. `response::prepare(..)?` (`collect.rs:186-187`) fails before the compiler runs. Any ptrace error mid-trace aborts the tree, while comparable errors elsewhere go into `errors`. `process::check_cancelled().ok()` (`collect.rs:761`) does nothing. When `Invocation::start` fails, the compile runs uncaptured with only a stderr line (`collect.rs:179-185`). If the failure happens before the attempt counter increments (lock or counter read), finalize cannot detect the missed compile.
- Fix: write the policy down and enforce it in one wrapper. Failures before exec fall back to plain supervision and record the error. Failures after the compiler exits never change its status. Mid-trace failures fail the build only under `required`. Increment the attempt counter before anything that can fail.

## H. Tests, experiments and probes

### C34 (high, organization) `tests/compiler-capture` mixes tests, an experiment harness and one-off probes

- Regression tests: 18 `test_*.py` files.
- Qualification harness: `batch.py` (621 lines), `sweep.py`, `debug_sweep.py`, `framework_batch.py`, `offline_roundtrip.py`, `archive_reports.py`, `matrix_report.py`, `debug_report.py`, `matrix_corners.py`, `baseline_matrix.py`, `capabilities.py`, `ppc_debug.py`, `isolate.sh`, `rust-session.sh`.
- Impure Nix fixtures using `builtins.getFlake ("git+file://" + ...)`: `fixtures.nix:4`, `matrix-fixtures.nix`, `debug-fixture.nix`, `baseline-matrix.nix`.
- One-off probes: `ptrace_probe.c` (`fixtures.nix:352`), `quota_probe.py` (`:344`), `job_wrapper.py` (self-described as exploratory), `encoding.py`.
- Cross-imports: tests import harness modules (`test_batch.py:10` loads `batch.py`). The runner's `python/compiler_suit_runner/tests/test_capture_pipeline.py:22-27` loads `tests/compiler-capture/test_debug_provenance.py` by path. Two hand-maintained filesets pick which tests run (`nix/compiler-capture.nix:19-34`, `nix/capture-tools.nix`).
- Fix:
  - `rust/compiler-capture/tests/` for supervisor integration tests (forwarding, storage, stdout, response, native), run by cargo or a check derivation.
  - `python/compiler_suit_runner/tests/capture/` for validator, publication, CLI, debug provenance and ELF collector tests.
  - `tools/capture-qualification/` with a README for the harness, fixtures and isolation scripts.
  - Delete the probes, or archive them in a notes commit.
  - Add one test helper that builds configs from the shared schema (C28).

### C35 (medium, process) The evidence identity locks the source layout

- Where: `tests/compiler-capture/matrix_coverage.py:44-48` and `debug_sweep.py:27-33` invalidate all evidence when any file under `lib/` or `rust/compiler-capture/` changes. `docs/compiler-capture-followup-plan.md:16-17` says the supervisor "should stay unchanged unless an actual compiler regression requires a correction".
- Why it matters: any refactor in this review forces a full requalification across 317 compiler/target pairs, which discourages maintenance.
- Fix: do the structural refactors before the final qualification run. Key evidence on the supervisor binary hash and on each pair's evaluated wrapper and stdenv derivation paths, not on the text of every file in `lib/`.

## I. Documentation

### C36 (medium, docs) The verification JSON files are test reports, not documentation

- Where: `docs/compiler-capture-verification.json` (496 lines) and `docs/compiler-capture-followup-verification.json` (3385 lines) hold hashes of git-ignored `.capture-experiments/...` files (`followup:3067-3080`, `verification:192-198`) and supervisor binary hashes. `docs/compiler-capture-compatibility.md` (659 lines) is largely a dated experiment log ("1408 runner tests with two skips", 2026-11-11 and -14 counts) that points at local paths (`:187`, `:218-231`).
- Why it matters: nobody else can verify them, and they go stale with the next commit.
- Fix: keep `compiler-capture-format.md` and a short compatibility contract (supported modes, known gaps, how to qualify) in `docs/`. Move the reports to CI artifacts or `tools/capture-qualification/reports/` with a schema and a regeneration command, and summarize the results in a dated changelog entry.

### C37 (low, hygiene) Unrelated and untracked material sits in the change

- `flake.lock` carries a dynamic-runner bump that the compatibility doc calls a pre-existing user modification.
- `planning.md` and `planning-multiversion.md` are untracked at the repo root.
- `Cargo.toml` has no `rust-version` field although the code uses `as_chunks` (Rust 1.88) and `is_multiple_of` (1.87).
- Decide on each of these before committing.

## What is good

- Opt-in at the flake level is clean. `datasetCaptured`, `_capturedMeta`, separate task IDs and variant directories keep captured results from satisfying ordinary requests, and the contracts are versioned.
- When there is no stage, forwarding is a plain `exec` (`main.rs:95-106`), so descriptors, process groups and wait status are untouched. The SIGPIPE handling records the inherited disposition from `.init_array` before the Rust runtime changes it (`signals.rs`), and a comment explains why.
- Resources are bounded everywhere: argv and response sizes, nesting depth, record and manifest sizes, file counts, a tmpfs scratch inside a private user and mount namespace, `O_NOFOLLOW`, regular-file checks, atomic writes, and content-addressed storage.
- `/dev/fd` response pipes are handled correctly (`response.rs`): the bytes are retained and streamed instead of consumed.
- Every Nix text patch checks that its marker exists exactly once and fails the build if not.
- Publication uses a hidden generation plus an atomic relative symlink swap, cleanup only touches names it owns, and execute bits are restored from the validated index (`capture_publish.py`).
- Many comments explain why, especially the compiler-behavior notes in `trace.rs` and `collect.rs`.
- Tests run against real compilers, and the Rust argument and quoting parsers have unit tests.

## Top 5 issues

1. C1 and C2. The commit says the default dataset is unchanged, yet it changes the default toolchains and the ELF collector, so every affected variant rebuilds with different code and the corpus becomes inconsistent.
2. C28 and C29. Schema, limits, contract and compiler-quirk rules are duplicated across Rust, Python, Nix and tests, and `config_sha256` depends on Rust field order.
3. C5 and C6. `collect::run` and `trace.rs` are god objects. The version rules cannot be unit-tested, and the unsafe ptrace layer is interleaved with policy.
4. C14 and C16. Syscall tracing covers whole linkers, LTO threads and split-DWARF `cc1`, and each artifact is hashed and re-read about a dozen times between the sandbox and the NFS publication.
5. C21 and C34. The supervisor derivation depends on Python tests and the validator, so a test edit rebuilds every captured package, and the tests directory mixes regression tests with the experiment harness and probes.

## Suggested refactor order

Before committing:

1. Split the change into separate commits: (a) baseline toolchain compatibility (C1), (b) the ELF collector (C2), (c) the unrelated runner fix in `read_manifest` (C3), (d) the capture feature. Each of (a) and (b) needs a dataset version or namespace decision. Leave the `flake.lock` bump and the planning files out (C37).
2. Make the supervisor source Rust-only and move its Python checks into `checks`/`passthru.tests` (C21). This is small and stops rebuild storms during the steps below.
3. Delete the experiment-only backends, `job_wrapper.py` and the probes (C10, part of C34), so nobody starts depending on them.
4. Move the verification JSON out of `docs/` and trim the compatibility doc to the contract (C36).

After committing, but before the final qualification run, because file moves and code changes invalidate the evidence (C35):

5. Add the shared schema and typed enums (C28, C8, C9), and switch `config_sha256` to hashing bytes. Steps 6 and 8 depend on these types.
6. Extract `plan()` from `collect::run` and add table tests (C5). Then have the record carry explicit expectations and transformation parameters, and simplify the Python validator to generic checks (C29). The Python side depends on the Rust record change.
7. Split `trace.rs`. First the mechanical `ptrace` layer (C12), then syscall decoding, then the rewrite passes (C6). On top of the primitive layer, add the seccomp filter (C14) and `process_vm_readv` (C15).
8. Storage and PCH performance: single-pass hashing, no scratch fsync, the producer index (C16 Rust part, C17, C18).
9. Python: `DatasetNamespace` (C4), validation reasons (C32), a cheap reuse check plus one post-transport validation (C16 Python part), and one ELF module (C30, C31).
10. Nix: per-toolchain memoization (C20), adapters via `overrideAttrs` or `wrapCCWith` (C23), wrapper-hook consolidation and `.sh` fragments (C24, C27), the directory layout (C25), fixture branches out of the overlay (C26), and `meta.platforms` (C13).
11. Move the tests and the harness (C34), re-key the evidence (C35), measure overhead (C14, C19), then run the full qualification once.

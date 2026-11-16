# Compiler artifact capture implementation plan

Status: proposed implementation and experimental procedure, 2026-11-07.

## Objective and constraints

Capture preprocessing output, generated assembly or IR, intermediate objects,
compiler invocations, and debug/symbol information from Nix-built dataset
packages. Package source files, package recipes, and their build commands must
remain unchanged. Add a named output through an opt-in overlay and intercept
compiler execution through the selected toolchain.

One Rust supervisor serves all supported compiler versions. An immutable
configuration file beside each compiler entry point selects its real executable,
exact version, family, target, driver identity, wrapper revision, and capture
capabilities. These are build-time facts, not guesses based on executable names.

Support means every compiler entry currently exposed by this project's matrix,
including legacy native, cross, and hybrid wrappers. Existing excluded
compiler/architecture combinations remain excluded. A version that only forwards
commands but cannot capture required artifacts is not considered complete support.

Do not promise universal observational equivalence merely because arguments are
parsed correctly. Saved intermediates can change compiler execution. Establish a
tested compatibility contract and make remaining capture gaps explicit.

## Evidence and unresolved questions

Previous native experiments used GCC 15.2 and Clang 21.1 from Nix:

- GCC redirected saved intermediates with `-save-temps=obj -dumpdir DIR/` while
  keeping the requested object path. Reversing option order changed placement.
- Clang accepted `-dumpdir` but did not redirect these saved intermediates.
- A single driver invocation compiling and linking `a/foo.c` and `b/foo.c`
  succeeded normally but failed with saved intermediates on both compilers.
- Clang's saved-assembly path introduced a checksum warning that became an error
  with fatal assembler warnings. The baseline compilation succeeded.
- Simple single-source baseline/capture tests had equal `.text` bytes. This is
  insufficient evidence for other inputs, targets, or compiler versions.
- GCC 4.4 documents plain `-save-temps`; GCC 4.5 documents its `obj` form.

These are research observations, not a completed compatibility matrix. Repeat
relevant cases inside actual sandboxed Nix derivations before choosing backends.

Local checks on 2026-11-07 found about 1.2 TiB free on the shared filesystem.
This is not a budget. The nixpkgs source used in earlier research has already
been garbage-collected. Inputs and tools needed across experiments require roots.
The system nix-daemon has its own system cgroup and no memory cap. A user scope
around a daemon client does not constrain daemon-launched builders.

Questions that must be answered experimentally:

1. How can the supervisor sit after Nix argument assembly on every wrapper
   generation without losing compiler metadata or bypassing existing fixes?
2. Which versions can place all extra intermediates outside build directories
   without changing the primary output, working directory, or source identity?
3. How do we preserve the new output against each applicable fixup hook?
4. What parts of compiler response-file syntax require version-specific parsing?
5. Which additional interception is required for direct assembler/linker calls,
   compiler launchers, and LTO subprocesses?
6. Can the strict resource-limited test environment share existing store inputs
   without changing store paths or duplicating large compiler closures?

## Storage and resource policy, established before experiments

### Default budgets

Start with these limits and report them in every test-batch summary:

- One Nix build job and one build core. One experiment at a time initially;
  never exceed ten concurrent tests, including wrapper unit-test processes.
- Tiny fixture capture limit of 64 MiB per derivation. Initial real-package
  limit of 512 MiB. Increase only for a specifically measured case.
- A 4 GiB hard limit for experiment scratch, logs, and local Rust artifacts.
- A 20 GiB total allowance for newly downloaded/built prerequisite closures and
  disposable Nix outputs in an initial batch. Dry-run before acquiring tools.
- Refuse new batches below a 100 GiB free-space reserve, or below the measured
  next-batch requirement plus that reserve, whichever is larger.
- Aim for 1 GiB RAM plus at most 3 GiB swap for fixture tests, with one core.
  Toolchain compilation is a separate cost category and may exceed that limit;
  do not silently relax it or initiate a legacy bootstrap to run a tiny test.

Disk limits must count allocated storage and files/inodes, not just reported
artifact lengths. A free-space precheck alone is not a hard limit.

### Hard isolation versus ordinary daemon builds

First run a tiny environment probe. Identify the actual builder PID/cgroup,
store backend, temporary build directory, cache directories, and filesystem.
Verify limits using a harmless allocation fixture, including failure and cleanup.

For hard disk containment, use a disposable quota-limited filesystem or volume
covering the test store's writable area, build directory, caches, and logs. A
private Nix store in a bounded disposable VM is a reliable fallback when local
namespace/mount permissions are unavailable. Keep `/nix/store` as the logical
store path inside it. Seed only the required existing closures.

Explore a private local/overlay store first, but do not assume support or use it
as the production testing backend until a fixture proves its sandbox, quota,
cleanup, and path behavior. Do not change to an arbitrary logical store prefix;
that can invalidate cached closures and trigger rebuilds. Do not use a container
whose disk is merely an unbounded directory on the host.

If using the normal daemon for cached-tool fixture builds, state that its total
build disk usage is not hard-capped by the client. Enforce wrapper capture caps,
check headroom, and monitor experiment ownership, but do not present that as an
absolute storage guarantee. Use the isolated backend for storage-exhaustion tests
and any test requiring hard process limits. Do not reconfigure the shared daemon
or stop other users' work to establish these limits.

### Ownership and cleanup

Create one owned experiment directory with a machine-readable ledger recording:
input revisions, tool roots, explicit fixture installables, output paths, scratch
paths, result roots, estimated closure sizes, actual sizes, and cleanup state.
Keep inputs/compiler tools rooted for a batch; retain only the current baseline,
current candidate, and one minimal failing example. Release the previous candidate
before producing another. A root for one output does not automatically retain
all sibling outputs, source derivations, or build-time prerequisites.

Keep source trees minimal through Nix source filters. Exclude Rust `target/`,
results, logs, capture archives, and experiment directories from flake inputs.
Otherwise each evaluation can copy a growing checkout into the store.

Use one local `CARGO_TARGET_DIR` inside the bounded scratch area for fast Rust
iteration. Bound Cargo download caches too. Build the Nix-packaged binary at
milestones rather than after every parser edit. Lock Rust dependencies and avoid
repeated changes to the toolchain or dependency set.

Do not use `--keep-failed` or `--keep-going` by default. Save a bounded log excerpt
and a small reproducer on failure. Cap logs and never retain whole failed build
trees automatically. Disable core dumps in the isolated test environment.

After each batch:

1. Save the small result report and necessary minimal failure inputs.
2. Remove only this batch's result roots and owned scratch artifacts.
3. Delete disposable outputs with `nix store delete` where unreferenced. Never
   use `--ignore-liveness`, and never remove `/nix/store` entries manually.
4. For a private store, run its GC or discard the entire owned volume after
   exporting the report. For shared-store GC, first preserve required inputs,
   inspect what is eligible, and use bounded collection only when appropriate.
   Shared-store GC is not scoped to this experiment; prefer targeted deletion.
5. Compare actual usage to the starting ledger. Explain retained references.

`nix store gc --max` limits reclamation, not storage growth. `--no-link` prevents
result roots, not output creation. Removing a result link does not itself free
store data. Account for output-to-output and metadata references when cleaning.

## Phase A: exploratory Nix experiments

### A1. Prove added-output behavior without a compiler wrapper

Write a tiny synthetic `stdenv.mkDerivation` fixture with multiple existing
outputs. Apply the proposed overlay transformation and append `datasetCapture`.
Use fixture-only build commands to write a recognizable marker to that output.
These commands belong to test fixtures, not modifications to real package recipes.

Check through Nix evaluation and a sandboxed build:

- Existing output names/order, default output, and `meta.outputsToInstall` retain
  their intended meaning; the capture output is appended exactly once.
- Existing `env` attributes, structured attributes, and hooks are preserved.
- `datasetCapture` is a writable absolute output path in the builder.
- Empty/no-compilation packages still create a valid capture output.
- Selecting `^datasetCapture` retrieves it, while the ordinary installable keeps
  its original default. All declared outputs are produced by the same build;
  selecting one does not make compilation or its siblings free.
- Existing output checks and reference restrictions either remain valid or
  produce a diagnosed incompatibility. No blanket disabling of output checks.

Determine a central initialization/finalization mechanism in the overlay or
stdenv adapter. It may add generic infrastructure hooks, but must not replace
package phases or require per-package capture instructions. Test custom phases,
`dontFixup`, and packages that manipulate output lists explicitly.

Acceptance: a Nix-built fixture proves the output contract, including default
selection and empty output creation. No real package rollout yet.

### A2. Prove transparent toolchain interception

First implement a supervisor mode that adds no flags and captures no files.
Wrap a tiny recording executable to test arbitrary argument bytes, environment,
working directory, inherited descriptors, stdin/stdout/stderr, child status,
signals, process groups, and cancellation. The recording executable is a fixture,
not a compiler-version emulator claimed as compatibility evidence.

Then place the supervisor in the real Nix compiler chain. Inspect the actual
selected wrapper source and expanded executable paths for modern GCC, modern
Clang, and one legacy/hybrid example. Avoid rebuilding the compiler itself.

Prefer the architecture:

    package -> existing Nix cc-wrapper -> Rust supervisor -> real driver

Prove how it is wired rather than assuming `cc-wrapper-hook` supplies an
after-return callback. The hook runs before execution; collection needs a
supervisor that actually owns the compiler subprocess lifetime. If inserting
inside a legacy wrapper is unsafe, test an outside adapter and explicitly record
which arguments it can observe. Do not repeatedly apply Nix flag assembly.

Preserve cc-wrapper attributes and support files, resource directories, target
prefixes, bintools, libc, compiler symlinks, C++ driver identity, and existing
PowerPC/MIPS compatibility fixes. Avoid recursion and accidental bypass through
an unwrapped absolute path. Test compiler-name and compiler-path probes.

Acceptance: forwarding-only builds have the same success/failure decisions and
observable compiler outputs on the fixtures. Distinguish unavoidable wrapper-path
identity changes from changes introduced by capture options.

### A3. Establish capture capabilities per compiler

Enumerate supported compiler entries from the existing metadata, not every
possible package attribute. Record actual executable/version/target alongside
the configured identity. Deduplicate genuinely identical toolchain tests.

Start with cached modern native GCC and Clang. For every invocation shape test
baseline, forwarding-only, and capture-enabled modes as separate derivations:

- C and C++, `-c`, `-S`, `-E`, dependency-only, syntax-only, and version queries.
- Implicit outputs, `-o FILE`, joined forms accepted by that compiler, stdin,
  `/dev/null`, explicit language, `--`, and paths containing spaces/non-UTF-8 bytes.
- Existing saved intermediates and user-selected auxiliary paths.
- Response files, nested response files where supported, empty arguments,
  quoting/backslashes, missing files, and large argument lists.
- Same basename in separate directories, repeated compilation to different
  outputs, parallel invocations, and one invocation with multiple source inputs.
- `.i`/`.ii` inputs, assembler input, precompiled headers, and modules where
  the version supports them. Do not inject C-only options into other modes.
- Existing `-g0`, debug formats, warning-as-error settings, and stripping flags.

Record every emitted path and identify whether the tool generated it as part of
the actual invocation. Check for extra files in the source/build tree and any
source modification. Input files must never be overwritten or deleted by capture.

Choose a backend only after these measurements. Modern GCC's redirection is one
candidate. Clang and older GCC require their own verified strategies. Do not
change cwd, rewrite primary `-o`, split a multi-source invocation, replace source
paths with symlinks, or replay compilation as an unexamined workaround. Each can
change behavior. If the driver cannot satisfy isolation, explore a version-specific
subprocess interception strategy and budget that work before promising support.

Already-existing intermediates should be observed and copied without adding
capture flags. Do not infer that all required intermediates exist from one flag.
Avoid stealing or moving the user's files. Snapshot before later cleanup.

Acceptance: a capability report identifies the selected mechanism and open cases
for each version. Unsupported cases block the relevant rollout; they are not
silently dropped. Full support requires resolving all selected versions.

### A4. Prove artifact survival and debug behavior

Use a fixture that compiles, modifies/deletes its object after the compiler
returns, installs an ELF, and strips it. Test the normal Nix fixup as well.

Verify captured snapshots survive unchanged. Prefer copies or reflinks; hard
links to mutable build outputs do not provide a snapshot. Do not assume reflinks
work across filesystems. Test output exclusions for strip, patchelf, debug
separation, reference rewriting, compression, and executable-bit handling.

Choose the preservation mechanism based on this experiment. One candidate is a
central finalization hook that packs staged data into the output after ordinary
fixup, plus a tested route for derivations that skip fixup. Another is an explicitly
excluded capture subtree. Do not assume either survives custom package hooks.

Record Nix reference closure size. Raw command lines and preprocessed sources
can reference many store paths. Do not erase provenance strings just to reduce
references. If archiving is selected, define reproducible ordering, timestamps,
ownership, compression, and extraction semantics; no timestamps/PIDs in the
canonical manifest. Treat raw byte paths without lossy Unicode conversion.

Acceptance: artifact hashes and DWARF/symbol checks survive the full Nix build,
including mutation, installation stripping, and fixup.

## Phase B: implementation in reviewable steps

### B1. Define formats and package the Rust binary

Proposed locations:

- `rust/compiler-capture/`: supervisor, argument handling, collectors, tests.
- `nix/compiler-capture.nix`: build-platform Rust package with locked dependencies.
- `lib/compiler-capture.nix`: toolchain adapter and configuration generation.
- `lib/capture-overlay.nix`: opt-in package/output transformation.
- `tests/compiler-capture/`: Nix fixtures and bounded experiment driver.
- `docs/compiler-capture-compatibility.md`: measured version capabilities.

Use build-platform tooling for cross builds. The supervisor executes on the
builder, not the target architecture. Keep its Rust toolchain independent of the
legacy compiler being observed. A binary per supported build platform is fine;
per-version Rust forks are not the design.

Version configuration and record schemas. Validate configuration once per
invocation without spawning the compiler to ask its version every time. Keep
entry-point-specific identities for C/C++ and target-prefixed aliases.

### B2. Implement forwarding and version-aware argument classification

Retain original `OsString` arguments. Parse a separate view without gratuitous
rewriting. Response-file analysis must match GCC/Clang syntax at that version;
never use shell evaluation or assume shell tokenization. Preserve original files
and bytes, detect cycles/depth limits, and handle Nix-generated response files.

Classify mode, language, inputs, primary outputs, dependencies, existing auxiliary
outputs, debug settings, and LTO. Unknown cases remain forwardable but yield an
explicit incomplete-capture record until supported. The final supported matrix
must not rely on that fallback for required normal builds.

Preserve exit codes and signal termination, including cancellation while copying.
Do not buffer unbounded compiler output or consume stdin for logging. Preserve
jobserver descriptors needed by LTO. Test stdin and process-tree termination.

### B3. Implement isolated collection and bounded storage

Initialize the output centrally before package compilation can begin. Use an
output-scoped staging area with one private directory per invocation. Generate
runtime-unique IDs without embedding them into canonical debug paths unnecessarily.
Finalize to content-hashed artifact IDs and stable records; preserve repeated
invocation multiplicity without relying on nondeterministic completion ordering.

Store invocation records and content-addressed snapshots of preprocessing,
assembly, IR, objects, and linked outputs. Preserve associations and original paths.
Use atomic record finalization and never a shared append-only JSON file from many
compiler processes.

Enforce per-artifact, per-invocation, total-output, file-count, and log limits.
A shared reservation/accounting mechanism must be safe across processes and
crashes. Never truncate an artifact and report it complete. Copies must be
bounded while streaming. Also bound compiler-generated intermediates: a copy cap
alone cannot prevent a driver filling the filesystem first. Use the isolated
filesystem quota until a suitable production mechanism is verified.

Record compiler failure and capture failure separately. Intentional configure
failures must retain their compiler status. Capture failure following successful
compilation should not impersonate a failed compiler feature probe. Central
finalization marks capture incomplete, and dataset validation rejects it under a
required-capture policy. Make the policy explicit rather than silently weakening
coverage. A whole failed Nix derivation does not retain its capture output as a
successful store result; preserve only bounded diagnostics through the test driver.

### B4. Implement verified compiler backends

Implement the A3-selected strategies, starting with one modern GCC and one modern
Clang. Avoid compiler-source patches or per-package changes. Record added flags
and actual emitted artifacts. Reuse existing files when capture is already enabled.

Do not silently retry a failed instrumented invocation without instrumentation.
That can alter configure decisions and duplicate side effects. Do not suppress
warnings globally to make saved assembly pass. Fix the strategy or report the
incompatibility. Replay-only artifacts, if introduced as a separate optional
feature, must never be labeled as original-intermediate capture.

### B5. Debug and link capture

Add debug-generation policy to eligible compiler invocations without changing
optimization, inlining, hardening, sanitizer, or instruction-set settings. Define
whether explicit package `-g0` is overridden in required-debug mode and record it.
Evaluate debug-format compatibility across old compilers and current readers.

Snapshot objects before the build system can strip them. Instrument the selected
Nix linker path where needed for map files and pre-install ELF snapshots. Avoid
overwriting a package's existing map file. Record direct linker/assembler bypasses;
compiler interception alone is not complete coverage of those tools.

Associate installed binaries with pre-install snapshots using build IDs and
ELF-aware validation. Patchelf and stripping can change file hashes. Build IDs
are association evidence, not universal unique artifact IDs. Preserve final
installed ELF hashes separately. Verify actual debug sections and source mappings,
not merely file presence or a `debug` output name.

Add GCC LTO and LLVM LTO/ThinLTO only with version/linker-specific tests. Retain
compiler-version-specific IR, link-time generated objects where available, and
links to the final ELF. Do not change LTO partitioning just to simplify capture.
Keep Nix `separateDebugInfo` as a complementary final-output mechanism; it cannot
recover information already stripped during package installation.

### B6. Integrate into the dataset pipeline

Apply the overlay only to selected dataset package variants through a shared
adapter, avoiding global recursive stdenv replacement or rebuilding all deps.
Preserve existing compatibility shims. Use the same wrapped-toolchain resolver in
variant construction and toolchain closure enumeration. Include the Rust binary
and configuration in affine archives and offline build-host prerequisites.

Change `mkBinaryFolder.nix` to handle outputs by role. It currently scans every
output for anything recognized as ELF. Without a change, captured `.o` and `.debug`
files could enter the executable corpus. Preserve collection across ordinary
package outputs, while routing capture/debug outputs separately. Validate ELF type
and artifact role rather than extension alone.

Extend worker staging/publication, sidecars, completion checks, and cache identity.
A successful legacy ELF result is not a complete captured result. Include schema,
wrapper/config identity, required roles, hashes, sizes, completeness, and artifact
relationships. Publish a completion record only after required artifacts and
metadata validate. Make retries atomic and remove stale partial staging safely.

Test substituter-free archive imports on a tiny cross example before any cluster
rollout. No network fetch or incidental compiler rebuild should be needed on the
worker after prerequisite import.

## Phase C: validation and rollout

### C1. Coverage without a full Cartesian build

1. Run fast Rust tests and forwarding tests outside Nix in bounded scratch.
2. Run tiny sandboxed Nix fixtures for output/fixup behavior.
3. Exercise every exposed compiler version natively where available, one at a
   time. Before each acquisition, inspect `nix build --dry-run`; stop an unexpected
   compiler bootstrap rather than consuming the batch budget.
4. Exercise cross wrapper families with tiny objects and link tests. Inspect
   target ELF architecture without attempting to execute foreign binaries.
5. Cover every supported compiler/target pair with a small smoke fixture in
   bounded batches. Share version-independent parser tests, but do not claim a
   compiler/target pair tested solely because another architecture passed.
6. Test actual unchanged packages: `hello`, multi-output `lz4`, a small C++
   package, a generated-source package, and a static-archive user. Add Autotools,
   CMake, Meson, and handwritten make coverage through a small selected set.
7. Expand optimization, PIC, hardening, sanitizer, and LTO cases only where they
   interact with capture. Do not build the full dataset to validate the wrapper.

A compiler that needs an expensive source build can be scheduled as a separate
bounded prerequisite batch. It remains pending coverage until actually tested.

### C2. Behavior and correctness checks

Use baseline, forwarding-only, and capture-enabled builds. Compare configure
feature decisions, command count/shape, exit status, diagnostics, dependency files,
installed file inventories, runtime behavior on native fixtures, ELF symbols,
relocations, DWARF, and linker-map relationships.

Debug sections and build IDs can legitimately differ. Compare executable code,
loadable segments, relocations, and dependencies with known path/build-ID fields
accounted for. Investigate changes rather than declaring all byte differences
harmless. Account for derivation-specific store paths and preexisting
nondeterminism. Rebuild selected cases to check reproducibility of canonical
capture manifests and archives.

### C3. Failure tests

In the quota-limited environment, inject compiler errors, unavailable capture
options, malformed response files, signals, timeouts, disk-full, inode exhaustion,
copy/finalization failures, partial records, and interrupted publication. Verify
bounded usage, child cleanup, accurate incomplete status, and safe retry behavior.
Test quota races under a small parallel build. Ensure no corrupted artifact counts
as valid and no capture failure changes a successful configure test into a false
feature rejection.

### C4. Release gates

- Every supported version has measured argument and capture behavior.
- Every selected supported target passes the applicable tiny smoke tests.
- Required artifacts come from the actual compilation, with declared provenance.
- Primary outputs remain at the original paths and retain validated behavior.
- Capture survives package cleanup and Nix fixup without source/recipe edits.
- The new output does not change ordinary default-output selection.
- Debug/relocatable artifacts cannot enter the executable corpus accidentally.
- Quota, failure, cancellation, publication, and cleanup tests pass.
- Disk measurements remain within the batch budget, including prerequisite closures.
- No full matrix run is needed to claim these gates passed.

## Concrete Nix experiment procedure

The implementation should provide a batch driver so the following steps are
repeatable rather than hand-managed. Names below are proposed fixture outputs,
not attributes that currently exist.

1. Record Git revision, dirty diff identity, existing `flake.lock` hash, free
   space/inodes, store backend, and limits. Preserve the user's existing lockfile
   modification. Never update flake inputs as an incidental test action.
2. Evaluate only the requested fixture and inspect its derivation and outputs.
   Do not enumerate or instantiate the whole dataset.
3. Dry-run that exact fixture before building. Estimate newly fetched closure and
   build-time storage separately; download estimates do not bound build scratch.
4. Establish the batch's input roots and quota-limited environment.
5. Build one explicit fixture with `--no-write-lock-file --max-jobs 1 --cores 1`,
   a timeout, bounded logs, and a result root owned by the batch. Selecting all
   outputs can be useful for a fixture, but never apply `^*` to a whole matrix.
6. Inspect the JSON build result and check every declared output. Run validators
   inside a tiny Nix check or against those rooted results, without recopying all
   artifacts into another unbounded directory.
7. Save a small machine-readable report. Keep only bounded minimal failures.
8. Release the batch outputs and scratch according to the ownership ledger.
   Recheck usage before starting the next compiler/version.

Do not embed the full changing checkout in every fixture. Use explicit filtered
sources for the Rust binary, configuration, and tiny C/C++ test inputs. Cheap
forwarding tests should not require building the complete project dev shell or
its dynamic-runner native extension.

## Work sequence and effort checkpoints

Implement in this order: storage harness; added-output fixture; forwarding-only
supervisor; modern capability experiments; isolated collection; fixup protection;
legacy/version backends; debug/link/LTO capture; dataset publication; cross/offline
checks; bounded rollout.

Re-estimate after A3. The earlier 4–11 hour preprocessing allowance should not be
treated as an estimate for a complete Rust supervisor supporting every legacy
compiler, response-file dialect, multi-source invocation, and LTO path. That scope
includes substantial compatibility and failure testing. Separate engineering time
from toolchain build and queue time, and report open support gaps at each checkpoint.

## References

- [GCC output and auxiliary-file options](https://gcc.gnu.org/onlinedocs/gcc/Overall-Options.html)
- [GCC 4.4 debugging and saved temporaries](https://gcc.gnu.org/onlinedocs/gcc-4.4.7/gcc/Debugging-Options.html)
- [GCC 4.5 debugging and saved temporaries](https://gcc.gnu.org/onlinedocs/gcc-4.5.4/gcc/Debugging-Options.html)
- [Clang command-line reference](https://clang.llvm.org/docs/ClangCommandLineReference.html)
- [Clang 21.1.8 driver implementation](https://github.com/llvm/llvm-project/blob/llvmorg-21.1.8/clang/lib/Driver/Driver.cpp)
- [Pinned nixpkgs compiler wrapper](https://github.com/NixOS/nixpkgs/blob/1c3fe55ad329cbcb28471bb30f05c9827f724c76/pkgs/build-support/cc-wrapper/cc-wrapper.sh)
- [Pinned nixpkgs debug-info hook](https://github.com/NixOS/nixpkgs/blob/1c3fe55ad329cbcb28471bb30f05c9827f724c76/pkgs/build-support/setup-hooks/separate-debug-info.sh)
- [GNU linker options and maps](https://sourceware.org/binutils/docs/ld/Options.html)

Reconfirm source revisions from the actual locked toolchain before implementation.
Modern documentation is not proof of behavior on a legacy release.

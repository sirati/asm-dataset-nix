# Compiler capture compatibility

This implementation is experimental and opt-in. The ordinary `dataset` output
keeps capture disabled. `datasetCaptured` and the runner's `--compiler-capture`
select required capture. The measured contract below covers every exposed
compiler/target pair and the stated additional modes. It does not promise
universal observational equivalence for arbitrary compiler options.

The installed runner accepts `--compiler-capture` alongside its usual submit/run
options. Dataset folders keep executables under `elf/` and capture/debug outputs
under `auxiliary/`. The supervisor can be built separately with
`nix build --no-write-lock-file .#compiler-capture`.

The 2026-11-14 follow-up adds publication revision 2 with explicit Nix
`separateDebugInfo`, selected-build provenance and destination generations.
Its qualification is tracked separately from the dated 2026-11-11 compiler
campaign below. See [the publication format](compiler-capture-format.md) for
debug association, split-DWARF lookup, migration and required failure behavior.

## November 14 follow-up verification

[The publication-revision-2 verification record](compiler-capture-followup-verification.json)
contains the current source identities, supervisor hash and individual results
for every public compiler/target pair. All 317 normal debug-hook fixtures passed
C/C++ driver identity, required-debug behavior comparison, target ABI, separate
debug source-line and native/QEMU runtime checks. Fifteen additional full LTO,
ThinLTO, split-DWARF and split-LTO fixtures passed. These modes supplement the
normal pair coverage; they do not constitute a repeat of every historical mode.

The follow-up also passed all eight package-hook boundary cases, ten GCC 5 C++
normal/LTO baseline modes, 204 capture tests with 16 skips, and 1408 runner tests
with two skips and 17 deselections. Live Nix/SLURM runner tests remain excluded
by the default pytest configuration. Rust's 17 unit tests, rustfmt and Clippy
with warnings denied passed.

Real pinned-framework transport tests cover shrinking publications, cross-mount
copies and execute permissions, three SIGKILL windows, recovery on the next
retry, and preservation of unrelated files. Negative cases reject rehashed
foreign compiler/target/wrapper identities and substituted DWO companions.
Copied publications validate after the original build/debug trees are removed.

The public `capture-example`, inspector and fast check passed offline in the
bounded private environment. The public supervisor and matrix candidate have
identical binary hashes. The real revision-1 example remains inspectable. The
[reviewed follow-up plan](compiler-capture-followup-plan.md) records the T3 review
and the compiler compatibility fixes required by qualification.

## Adoption workflow

The repository exposes a small entry point before matrix or cluster use:

```sh
nix build --dry-run --no-write-lock-file .#capture-example
nix build --no-write-lock-file --max-jobs 1 --cores 1 .#capture-example
nix run --no-write-lock-file .#capture-inspect -- ./result --json
```

`capture-example` uses the pinned native GCC toolchain and unchanged `hello`
recipe, including its normal tests and fixup. It does not enumerate the matrix
or select legacy compilers. Its capture limit is 512 MiB, with the usual bounded
2 GiB compiler staging area. Building through the ordinary daemon does not cap
the package builder's total disk or memory usage. Inspect the dry run before
acquiring prerequisites. Keep the plan's 100 GiB free-space reserve when running
the experimental batches.

The inspector accepts three directory layouts: a raw `datasetCapture` output,
an ELF folder with `meta.json` and `auxiliary/datasetCapture`, or a published
variant with `capture-complete.json`. It validates artifacts before reporting
their compiler identities, role counts and byte totals. Folder inspection also
checks the declared contract and installed ELF associations. Published inspection
checks every indexed file and its executable permission. `--json` emits a summary;
`--allow-incomplete` permits diagnosing incomplete raw capture, while hashes and
publication completion still have to validate. Validation failure exits with 1.
The command uses only Python's standard library and does not load dynamic-runner.

Once the example passes, build one captured matrix variant explicitly:

```sh
nix build --dry-run --no-write-lock-file \
  '.#datasetCaptured.x86_64-linux.hello.x86_64.gcc15-O2-baseline-none-san-off-march-default'
nix build --no-write-lock-file --max-jobs 1 --cores 1 \
  '.#datasetCaptured.x86_64-linux.hello.x86_64.gcc15-O2-baseline-none-san-off-march-default'
nix run --no-write-lock-file .#capture-inspect -- ./result
```

Use `_meta` or `_capturedMeta` to list the available suffixes at the current pin.
Repeat a selected native C++ package and then one cross-target variant before
expanding the runner. Add `--compiler-capture` to runner submissions only for that
next stage. Runner sampling applies per compiler/architecture/optimization group;
it does not limit a submission to one build. Required capture failures leave the
result incomplete and block publication. Investigate the recorded failure before
increasing a budget or expanding the selection.

The fast repository check is independent of the supervisor build:

```sh
nix build --no-write-lock-file .#checks.x86_64-linux.compiler-capture-validation
```

It tests validation, publication retry/completion, the inspector, behavior
comparison and ELF collection with tiny fixtures. `nix build .#compiler-capture`
adds Rust and native GCC checks. The bounded scripts under `tests/compiler-capture`
remain the route for compiler compatibility sweeps, quota/failure experiments and
offline transfer checks. Local experiment ledgers are evidence, not runtime inputs
to the public example or inspector. Capture stays opt-in throughout this sequence.

## Current implementation

`rust/compiler-capture` contains one Linux supervisor, configuration validation,
response-file analysis, bounded snapshots, and finalization. The x86-64 tracing
backend observes compiler subprocess execution and redirects additional saved
intermediates into invocation-owned directories. It keeps package source paths,
the working directory, and primary output paths. It does not replay compilation.

`lib/compiler-capture.nix` copies the selected Nix wrapper scripts and changes
their real-driver execution points. Argument assembly and existing support files
remain in the original wrapper. Each entry has a separate immutable JSON
configuration with the real executable, compiler version, target, driver identity,
supervisor path, backend, debug policy, and wrapper response-file setting.
The adapter reads that setting from the selected wrapper during its build.
The unwrapped `stdenv.cc.cc` interface also adapts matching raw and versioned
driver aliases. Resource directories remain links to the original compiler.
Raw calls receive no Nix argument assembly. A verified parent-tracer marker
prevents nested compiler or binutils adapters from collecting twice.
Missing optional `cpp` aliases stay absent. It does not infer response defaults
from the compiler version.

`lib/capture-overlay.nix` adds `datasetCapture` to one selected package. Its outer
builder initializes capture before the package builder and finalizes after the
builder returns, including custom phases and fixup hooks. Package phases and
source files do not require capture instructions. Dependencies keep their
original derivations. Output-only fixtures have an explicitly incomplete manifest.

Captured files are byte copies with SHA-256 identifiers. Invocation records use
hexadecimal Unix path and argument bytes. Repeated identical records retain a
multiplicity count. Subprocess command records are separate hashed artifacts.
Nix-generated response files are retained by content; their temporary names use
a declared marker in canonical argument records. Runtime staging identifiers do
not form artifact identifiers. A small independent store output contains
`view -> /proc/self/fd/198`. Each invocation leases that descriptor for its private
auxiliary directory. The anchor has no dependency on the captured package, which
avoids output reference cycles when older compilers retain intermediate filenames.
Private staging is outside the package build tree under `/tmp/.dataset-capture.*`.
The supervisor preserves occupied caller descriptors by declining instrumentation.
Older Nix response pipes are copied without consuming the compiler's argument
stream. If their analysis exceeds 1 MiB, a helper streams the entire original
response to the compiler and records incomplete capture.

The required debug policy adds GCC DWARF 2 or Clang DWARF 4 generation flags to
eligible C/C++ compilation. It overrides explicit `-g0` and records that override.
The preserve policy retains the caller's debug selection. Support for a preserve
policy with every Clang warning/debug combination has not been established.

Split DWARF files have the `debug-companion` role. The tracer observes their
actual opens/publication and snapshots them before the producer exits. Empty
Clang placeholder files do not satisfy this role. GCC LTO can emit a nonempty
ELF container before generating debug data; the frontend may defer that data,
but a completed split-DWARF link must retain a companion with compilation-unit
information. Caller source/object and auxiliary-directory names remain in place.
LTO partition companions are retained privately with the other LTO intermediates.
Split-DWARF links use coordinator interception instead of changing the GCC
driver's auxiliary prefix. `-gno-split-dwarf` and Clang's single-file split mode
do not require a separate companion. Validation also recognizes split mode in
older records' debug flags, so an old capture missing its companions is rejected.

`lib/mkBinaryFolder.nix` separates capture/debug outputs from ordinary package
outputs. Its collector requires an ELF executable/shared-object type and a
file-backed executable load segment. Relocatable objects and debug-only files
cannot enter the executable corpus solely because they have an ELF header.

## Measurements

Tests use the existing lockfile, including its pre-existing user modification.
The cached fixture-tool nixpkgs revision is
`1c3fe55ad329cbcb28471bb30f05c9827f724c76`. The public flake's root nixpkgs
revision is `68a8af93ff4297686cb68880845e61e5e2e41d92`. Matrix smoke fixtures
select actual public toolchains while reusing the cached fixture helpers.
The 2026-11-11 verification passed every required mode for all 317 public
compiler/target pairs. The matrix contains 16 GCC and 25 Clang entries across
10 target variants. All 1079 modes passed: 317 smokes, 317 corner suites,
284 full-LTO tests and 161 ThinLTO tests. The corner suites contain 12793 passing
comparisons and 241 optional cases rejected by the baseline compiler.

[The verification record](compiler-capture-verification.json) retains exact
compiler versions, target/mode counts, source hashes, the tested supervisor's
binary hash and report hashes. The complete local coverage report is
`.capture-experiments/all-compilers-current/coverage.json`. It combines
`release-modes.json`, `release-resume.json` and `release-prereq-retry.json`.
The gate found no missing, failed or stale cases. The separate C++/runtime
baseline preflight also passed all 762 applicable modes.

These checks cover C/C++ objects, linking, preprocessing, ELF targets, allocated
sections, relocations, source mappings and native or emulated execution. They
establish the stated capture contract, not every possible compiler-option or
package combination. Additional focused measurements have their own scope below.

### Reproducing full compiler coverage

The matrix driver reads the public metadata instead of maintaining a second
compiler list. The current matrix has 41 compiler entries and 10 target variants.
It requires 317 ordinary smokes, 317 corner suites, 284 full-LTO tests and 161
ThinLTO tests. LTO version exclusions come from the existing support policy.
The cross fixtures execute through QEMU user emulation; the native target runs
directly. C++ string interoperability and normal/throwing exception paths run
in baseline, forwarding and capture modes, including supported LTO modes.

Use an already built supervisor and a reviewed, cached fixture nixpkgs tree:

```sh
python3 tests/compiler-capture/batch.py \
  --nixpkgs /path/to/cached/nixpkgs \
  --baseline-target --target mips64el --timeout 900
capture_tool="$(nix path-info --no-write-lock-file .#compiler-capture)"
python3 tests/compiler-capture/sweep.py \
  --nixpkgs /path/to/cached/nixpkgs \
  --capture-tool "$capture_tool" \
  --shapes smoke corners lto thinlto \
  --report .capture-experiments/matrix.json
python3 tests/compiler-capture/matrix_coverage.py \
  --capture-tool "$capture_tool" \
  --reports .capture-experiments/matrix.json \
  --report .capture-experiments/coverage.json
```

The baseline preflight checks every compiler exposed for the selected target,
including its C++ runtime, full LTO and ThinLTO where supported. Run it for the
other public targets before the slower baseline/forwarding/capture comparisons.
These preflights are reported separately and cannot satisfy capture coverage.
The driver uses the Git working tree for flake resolution. Implementation files
must be tracked or added with intent to add. Fixtures reject a source snapshot
that contains `.capture-experiments`.

Each compiler/target group shares one private store across its applicable modes.
The builds stay sequential and offline, with one Nix job/core, a 1 GiB memory
limit, at most 3 GiB swap, and a bounded writable filesystem. The driver refuses
unreviewed prerequisite acquisition. The small N32 source correction and LLVM
plugin are explicit prerequisites rather than an implicit compiler bootstrap.
The report index points to separate bounded JSON reports for each mode.

`sweep.py --retry-report REPORT` retries failed or pending cases. Pass the original
and retry reports to the coverage gate in that order. The gate requires every
expected case, complete artifact validation and native/emulated runtime evidence.
It rejects reports from other binaries or stale production/fixture sources.
Empty selections, untested pairs and refused prerequisites cannot count as passes.

| Test | Measured result |
|---|---|
| Multiple outputs, structured attributes, custom phases, skipped fixup, empty package, output-reference restrictions | Sandboxed output fixtures pass; default selection excludes `datasetCapture` |
| Forwarding arguments, streams, working directory, environment, jobserver descriptor, exit status and signals | Recording-executable tests pass, including inherited SIGPIPE behavior |
| Snapshot mutation/deletion, corruption, concurrent reservations, interrupted children, capture failure after compiler success | Local bounded tests pass |
| GCC 15.2 and Clang 21.1, native x86-64 | Baseline and instrumented Nix fixtures match success/failure results on 24 invocation shapes |
| Same-basename compile/link inputs, Clang stdin, fatal assembler warnings | Tracing tests pass with the required debug policy |
| Source macros, code/data sections, pre-existing `.i` and `.s` inputs | Native tests preserve source bytes and compare `.text`, `.data`, and `.rodata` hashes |
| Object mutation followed by installation stripping and normal Nix fixup | Captured object and pre-strip program hashes survive |
| ELF corpus output roles | A Nix fixture excludes a relocatable object and executable-looking files in capture/debug outputs |
| GCC 15.2 LTO; Clang 21.1 full LTO and ThinLTO | Native compile/link, same-basename inputs, runtime and stripped-ELF association checks pass; 30 repeated Clang link tests pass |
| C and C++ precompiled headers | Producer/consumer tests pass with GCC 4.4, 4.9, 9 and 15 and Clang 3.4-3.9/21; main/included-header mutation and timestamp invalidation tests pass |
| Clang 21.1 header modules | Native test retains used module maps and PCM files, plus ordinary saved intermediates |
| Direct GNU assembler/linker calls | Native and Nix fixtures retain assembly inputs, objects, commands, maps and the pre-install ELF |
| Raw GCC/Clang interfaces | Sandboxed baseline, forwarding and capture compile/link fixtures pass |
| Input languages, `-x none`, relocatable links and LLVM IR | Modern native tests preserve behavior and retain the correct roles |
| GCC 15/Clang 21 C++20 modules | Native and sandboxed interface/consumer builds, separate object compilation and final executable comparisons pass |
| Direct LLVMgold full/ThinLTO | Native tests retain IR, native objects and final ELF without extra files in the build tree |
| Unchanged `hello`, required policy | Private-store and ordinary-daemon builds pass; the final daemon build validates 331 invocations, 1249 artifacts and 35029723 artifact bytes, then runs successfully |
| Unchanged `lz4`, `zlib`, `jq` and Meson-based `inih` | Required-capture sandboxed builds and installed ELF associations pass |
| Pinned runner framework | Full runner suite passes in a bounded 1 GiB scope, 1408 passed and 18 skipped |
| Legacy native LTO | Full LTO passes on 14 selected GCC versions and 21 Clang versions; applicable Clang versions also pass ThinLTO; Clang 16 has separate full/ThinLTO Nix checks |
| Cross LTO | GCC 4.6 ARM soft-float, GCC 15 AArch64, Clang 3.7 AArch64 and Clang 9 RISC-V ThinLTO pass |
| Long driver options and additional source suffixes | GCC 15/Clang 21 capture separate/joined `--output` and `--language`, `.cp`, `.c++` and `.CPP`; GCC also captures `.sx` assembly. GCC 4.4 and Clang 3.4 pass separate legacy alias/suffix checks |
| Caller linker-map options | Separate `-Xlinker` operands, maps among other `-Wl,` options, paths with spaces/commas and the final map selection are retained without adding a competing map |
| Forwarded linker modes | `-Wl,-r`, `-Wl,--relocatable` and separate `-Xlinker -r` retain the final relocatable object; linker flags do not misclassify a requested `-S` assembly output |
| Directives-only preprocessing | GCC 15/16 and Clang 21 retain source-file macros and counters; caller macro/file prefix maps are checked, including paths with spaces and `=` |
| Split DWARF | GCC 15/16 and Clang 21 retain `.dwo` companions from compilation and linking, including custom object directories and caller auxiliary directories; GCC LTO retains its partition debug data |

The 24-shape fixtures include cases deliberately forwarded with an incomplete
capture record. Matching compiler status does not make those cases supported
capture. The separate LTO, header/module and direct-binutils tests above cover
their stated cases. Existing flag exclusions remain in force: GCC LTO starts at
4.6, Clang LTO at 3.7 and ThinLTO at 3.9. The ordinary pair smokes do not imply
every optional flag combination has been tested.

Plain saved-temporary flags fail the same-basename link case on both modern
compilers. Clang also fails the tested stdin and fatal-assembler cases with those
flags. A subprocess redirection experiment fixes the GCC collision. Clang's
`-ccc-install-dir` experiment does not intercept its compiler children. The Rust
backend therefore uses exec tracing rather than that directory override.

GCC LTO uses the measured modern `-dumpdir` contract to contain coordinator
responses, resolution data and partition outputs. Source intermediates still use
per-job redirection, preserving same-basename compilations. Before GCC 11, the
tracer changes the LTO coordinator's auxiliary prefix in `COLLECT_GCC_OPTIONS`,
redirects resolution and partition operands, and snapshots WPA IR before overwrite.
GCC can invoke LTO from existing objects or archives without a driver `-flto`.
Links prepare retention before that decision and record whether LTO ran.
Duplicate `-dumpdir` environment entries are reduced to the effective last entry,
avoiding GCC 15's plugin decoding bug. LLVMgold's saved IR
and generated native objects are redirected at linker file syscalls. Each linker
thread has separate argument storage. New threads remain stopped until their
parent's clone event assigns tracing state, including when kernel event ordering
reports the child first. LTO partition settings are retained.

LLVM 3.9 ThinLTO names native objects and optimized bitcode after each input,
including archive member offsets and source names. The tracer also tracks those
names for invocation-owned intermediate inputs. It redirects the generated files
into staging, retains their native ELF objects and leaves caller files intact.
Repeated archive-link tests check canonical manifest equality. LLVM LTO
completeness checks require retained linker-generated native objects alongside IR.

LLD's numbered ThinLTO native objects, such as `program1.lto.o`, are also retained
without overwriting same-named caller files. Clang 16 uses its matching cached LLD
except for MIPS, where LLD 16 rejects N32 bitcode's ELF class or O32 exception-table
relocations. Those targets use
GNU BFD and a small LLVMgold plugin built against the original LLVM 16 shared
library and headers. Its optimizer and code generator stay at LLVM 16. Older
hybrid Clang cross wrappers use cached Gold 2.28, whose host libc matches their
LLVMgold plugin. MIPS and PPC64 use the matching cached BFD instead; it also fixes
older Gold's PPC64 programs that link successfully but crash at runtime. Those old N32 LTO
backends also need the 32-bit ELF container selected explicitly while preserving
the MIPS64 CPU and N32 calling convention. PPC64 LTO retains the selected ELFv2
ABI. Clang 9-14 RISC-V
passes the selected ISA features and ABI to its LTO backend. GCC 4.6 cross linker
search exposes its original collect2 with the selected linker aliases. GCC 5
AArch64 uses a cached modern collect2/linker for modern glibc relocation support;
GCC 5 MIPS O32 uses the selected O32 runtime search. These are shared baseline/
capture adapters. Their helper closures
form part of toolchain export.

Old cross Clang and pre-5 native Clang wrappers use the current target wrapper around the selected old
frontend. This supplies C++ headers and runtime libraries that were absent from
the legacy C-only bootstrap GCC. The wrapper preserves its resource setup and
points builtin headers at the selected Clang release. Before Clang 3.9, a
source-only compatibility header supplies missing target-sized macros and selects
libstdc++'s older string ABI because those compilers lack GCC's ABI-tag support.
Explicit caller ABI macros retain precedence, including response-file invocations.
Clang 3.4 ARM source compilation also enables LLVM's EHABI and EHABI descriptors,
which its backend otherwise leaves disabled. This restores interoperability with
the target C++ exception runtime. Object-only links and compiler queries receive
no added source-only compatibility flags.

Clang 3.4 has a separate N32 target bug: it reduces the widths of pointers and
`long` but leaves `size_t` and `ptrdiff_t` as long types. This breaks C++ library
symbols such as `std::string::_Rep::_S_create`. The N32 adapter recompiles the
target definitions and three driver entry files against the original cached
Clang/LLVM 3.4 libraries. It corrects the actual language types, including the
type of `sizeof` and pointer subtraction. It keeps the original frontend and
backend release. This narrow source correction is needed because driver flags
cannot select the correct types. Other targets retain the original binary.
The repair build includes type checks; matrix corners also check the real types
and link C++ strings across ordinary and response-file compilation.

Clang's saved preprocessing can remove include-search options needed to resolve
implicit module imports in the next frontend stage. The tracer retains module
maps actually opened by preprocessing and supplies those maps to the saved-input
stage through a recorded response operand. It observes opened or published PCM,
GCM and PCH paths before compiler subprocesses exit. Modern Clang module interfaces
embed saved preprocessing so later invocations can use the PCM after the producer
releases its staging directory. PCH producer/consumer tests pass with Clang
3.4-3.9 and Clang 21. Old Clang's invalid saved-header language is corrected at
its PCH frontend execution. The added preprocessing output is retained for inspection. The PCH frontend
serializes the original header with its original preprocessing options, preserving
macro definitions, undefinitions and builtin state for consumers. Preprocessing
runs twice for a PCH producer; header compilation and serialization run once.
A consumer can resolve saved header buffers from an older
matching PCH producer's committed artifacts. Only missing reads within the private
capture view can use this mapping; writes cannot reach the retained copies.
The private header and retained copy use a fixed mtime. The canonical record has
no timestamp, and validation checks retained-source bindings against producers.
The tracer observes the preprocessing process's actual file reads. Ephemeral
size/mtime guards preserve rejection after a main header, included header or
its timestamp changes. These guards are bounded and disappear at finalization.

For directives-only compilation, Clang's added preprocessing stage must expand
macros once rather than defer them to another frontend. The original explicit
preprocessing mode retains its behavior. GCC keeps its directives-only macro
rules and gives the saved-input frontend the original main-file spelling.
Only that frontend's read-only opens of the name use the retained preprocessing.
The source file stays unchanged. GCC applies caller macro/file prefix maps itself.
This also covers GCC versions before prefix-map support and source names with
spaces, quotes and `=`. Tests compare allocated data, relocations and source-file
builtins as well as executable instructions; they preserve GCC's rejection of
`__COUNTER__` in a directives-only conditional.

Some saved-assembly paths need a retained private assembler input. Older Clang
places `.cfi_sections` after frame directives; the correction places the same
directive first. For GNU as, it makes that correction only when capture adds
debug information to a command without debug information. An existing caller
debug request retains its original unwind behavior. Clang 3.8 needs the selected GNU assembler on affected targets.
Its i686 case matches the original long-NOP encoding and omits GNU as's automatic
used-ISA note. GCC 4.5 ARM C++ needs its original unwind section retained alongside
the debug frame when required-debug generation is added. Clang 14-16 RISC-V can
lose the direct frontend's text alignment. A leading alignment directive restores
it. Clang 14 at `-O0` also needs its original absence of relaxation relocations
preserved. These assembly corrections remain relevant to explicit assembly and
caller-selected saved intermediates. Original and corrected assembly pairs are
retained, and validation checks the compiler identity and exact transformation.

Ordinary Clang C/C++ compilation retains LLVM bitcode and generates native
objects directly from it. The existing final compiler child performs native
code generation, preserving its original target and optimization options. LLVM
optimization runs once. This avoids instruction-width, NOP and CFI differences
introduced by reassembling text. The output contains preprocessing, IR and objects.
The native backend also retains the integrated assembler's relocation-relaxation
selection. Clang 12-15 PIC objects otherwise change relocation types, including
on i686. Focused native/i686 comparisons cover these releases at `-O0` and `-O2`.
Requested `-S` and caller-selected saved intermediates retain their original modes.
Explicit external-assembler requests and unhandled assembler options retain the
assembly path. A recorded `native_from_ir` flag selects the stricter bitcode
validation contract. Existing `.i`/`.ii` and assembler inputs retain their bytes.

The behavior checker compares allocated sections, public symbols and relocation
identities. For RISC-V objects it verifies that `R_RISCV_ALIGN` ranges contain only
NOPs and removes linker-removable padding for comparison. Section alignment can
satisfy a leading constraint. Code, data, public symbols and their relocations remain strict. Required debug
flags can add unwind metadata, including frames for synthesized C++ helpers on
AArch64. Such differences pass only when every changed section is an unwind or
exception table and the entire allocated representation matches an independent
build with the required debug flags. A debug reference cannot authorize changed
instructions or code relocations. Negative tests cover those boundaries.

Clang 3.8-11 on AArch64 can also swap a W0 constant load and `ADD X29, SP, #0`
when debug information is enabled. The checker accepts this measured case only
after checking the compiler version, ELF ABI, every opcode and operand, unchanged
text layout, unchanged code relocations, and an exact independent debug-reference
match. The instructions update disjoint registers and neither changes flags.
The constant load must be the measured MOVZ form or a valid ORR logical-immediate
form with WZR as its source. Different registers, constants, instructions or
relocation targets fail. Clang 9-11 can also move an ADRP/load sequence past frame setup. Those
relocatable-object checks require the original frame save, unchanged instruction
operands, no FP/SP dependency, and identical relocation targets and addends at
their verified new instruction positions. Every other byte of executable code
stays fixed, and the complete result must match the independent debug build.

PowerPC's debug scheduler can move frame setup past independent instructions.
For bounded relocatable PowerPC objects, the checker verifies unchanged control
flow, instruction operands and relocation targets. Reordered instructions must
have no register, condition-register, memory, LR or XER dependency. PPC32 PIC
literal and local-PC adjustments are checked together. A bounded control-flow
walk checks every reachable path before LR can expose a changed local PC.
Unknown instructions
cannot reorder, and the complete result must also match the independent debug
build. When CFI ordering preserves the original unwind sections instead of the
old assembler's spurious debug-build frames, all other sections must remain
identical to the original build and the verified instructions and relocations
must match the debug build. The checker never changes captured bytes.

The measured i686 Clang 10/11 cases can reorder independent absolute and stack
loads, or move frame setup past register initialization and an absolute-memory
comparison. Their checker fixes instruction boundaries at the original frame
prologue, preserves exact operands, rejects register dependencies and moved
PC-relative relocations, and verifies the new positions of absolute relocations.
The full result must match the independent debug build.

Clang 3.4's saved x86 assembly can encode `cmp $9,%eax` in three bytes where its
direct object emitter uses five. Other legacy versions also change immediate
widths on arithmetic and memory operands. Native generation from retained IR
preserves the original instruction selection. The older, narrowly scoped
semantic verifier remains available for inspecting historical captures; current
matrix fixtures require allocated-section, symbol and relocation equality.

## Dataset integration

`lib/resolve-toolchain.nix` resolves the compiler for both variant construction
and toolchain closure enumeration, including the existing PowerPC and MIPS fixes.
Capture applies to the selected package's stdenv. Dependencies keep their original
recipes and compilers. `lib/capture-stdenv.nix` preserves negative reference
constraints while dropping their dependency contexts, so rebuilding the small
stdenv setup does not fetch unused bootstrap outputs.

The worker validates capture hashes, configuration identities, roles and
completeness before staging it. It copies capture and debug files under
`auxiliary/`, records installed ELF hashes and their pre-install candidates,
and writes `capture-complete.json` after those bytes validate. Association checks
compare the ELF ABI, entry point, allocated sections and executable segments.
They record build IDs as evidence. The section comparison excludes `.interp`,
`.dynstr`, `.dynamic` and `.note.gnu.build-id`, whose contents can change during
fixup. Executable-segment hashing normalizes these same mutable section ranges
and the ELF header's section-table fields. Older linkers can include all of them
in an executable segment. Library dependencies, dynamic flags and dynamic symbol
names are compared separately, so changing linkage does not disappear with RPATH
normalization. Required associations also check retained DWARF line sections and symbols.
Native GCC/Clang tests exercise association after stripping.

Captured-result completion checks include the requested derivation and contract,
then validate every published file. Existing ELF sidecars do not satisfy an
explicit captured-result request. Retry staging uses a lock and replaces one
validated generation. Fault injection verifies that failed copying leaves the
previous valid generation available. Both staging and reuse check the manifest's
policy and wrapper revision and each compiler configuration against the requested
contract. File indexes retain artifact roles and executable permission, and reuse
rejects missing permission metadata, changed execute bits, and unindexed directory
symlinks. Older completion records without permission metadata require restaging.
Malformed analysis byte arrays and completion documents fail validation instead
of raising an attribute error or interpreting an integer as a byte-buffer length.
`--compiler-capture` selects the captured
dataset and toolchain map, changes cache/task/publication identities, and reaches
framework-spawned secondaries through `CSR_COMPILER_CAPTURE`. The November 11
archive/import check used `_matrix_eval_capture_v1`; revision 2 defaults to
`_matrix_eval_capture_v2`. The AArch64 archive/import check passed
with substituters disabled and network isolation. The destination initially
lacked the 57 transferred paths, imported a 1733560-byte archive through the
pipeline's export/import helpers, then rebuilt the identical canonical manifest
`a764a614ec5692d2d82d1aeccbff8a513f682f9945609628dc0eab9dd4bd66aa`.

## Resource limits and ownership

The expanded compiler campaign exposed a source-filtering bug: path-based flake
resolution copied the changing experiment directory into the shared store. This
exceeded the temporary-store allowance. The corrected fixtures use Git source
selection and assert that experiment data is absent. The audited source tree is
about 6 MiB. Targeted deletion removed 310 owned source snapshots after checking
their roots and referrers, recovering about 67 GiB. This cleanup used no shared
GC or liveness override. The ownership audit is retained in
`.capture-experiments/all-compilers-current/source-copy-cleanup.json`.

`tests/compiler-capture/batch.py` selects an explicit fixture, records its
derivation and dry run, and refuses unreviewed prerequisite builds/downloads.
It preserves the lockfile and records source hashes, roots, outputs, allocated
storage, inode counts, and cleanup results.

Fixture builds use a private offline Nix store in a user/mount/PID/network
namespace. A read-only host store is the lower layer; a bounded tmpfs contains
the writable store layer, Nix database, build directories, caches, and logs.
The logical store path remains `/nix/store`. The shared daemon is not changed.

Default limits are one job/core, 64 MiB capture per tiny fixture, 512 MiB for an
initial real package, 1 GiB RAM, and at most 3 GiB swap. The 4 GiB scratch allowance
uses a 3 GiB writable filesystem with room reserved for bounded retained reports.
The filesystem has 131072 inodes. Reports have a 1 MiB limit and the driver limits
retained batch/session counts. New prerequisites/disposable outputs have a 20 GiB
initial allowance. New batches require a 100 GiB free-space reserve.

The allocation fixture uses a smaller 64 MiB/8192-inode filesystem. Both byte and
inode allocation stop with `ENOSPC`; cleanup succeeds. The actual Nix builder is
in the user scope whose memory, swap, and CPU limits the report verifies.

Compiler-generated capture extras are enabled only on a verified bounded tmpfs.
The opt-in builder can create a private user/mount namespace and mount a bounded
scratch filesystem before package execution. A host probe preserved UID 1000 and
GID 100, exposed a 64 MiB/131072-inode mount, and verified zero effective
capabilities after exec. The private mount disappears after the builder exits.
An ordinary-daemon `hello` build also passed initialization, capture, fixup,
finalization, validation and native execution. Its total package-builder disk
usage is not constrained by a client cgroup.
Copy accounting also limits each artifact, invocation, total capture, and file
count. Reservations are shared between processes; interrupted reservations stay
charged. Partial artifacts never count as complete. Capture errors do not replace
a successful compiler's exit status. Finalization marks incompleteness, and a
required policy rejects it.

The builder disables ccache and requests sccache recaching during capture so a
previous object-cache hit cannot silently bypass the compiler supervisor.
The unchanged full `fmt` test build exceeds the measured 1 GiB capture profile
and is rejected at finalization. Its tests and recipe remain intact. This is an
oversized failure measurement; `inih` supplies the small C/C++ and Meson case.

Command records allow up to 4 MiB, with larger records charged against both their
temporary and manifest storage. Finalization and Python validation share a
16 MiB manifest bound and a 4096-record limit. Large valid arguments retain their
records instead of silently crossing the previous 64 KiB commit limit.

The exact pinned Python framework was built as a separate prerequisite profile
with a 4 GiB memory limit and the same bounded writable store. Its export was
verified and imported. An accounting bug then traversed the read-only host store
and exhausted that profile's memory. The driver now traverses only the owned
upper layer and uses filesystem allocation statistics for total writable usage.

The final runner tests found a separate whole-file read of a legacy 2 GiB sparse
manifest. The reader now retains the JSON document and checks its NUL padding in
64 KiB chunks. The unchanged sparse-file test passes under the default 1 GiB
scope. Test scopes also place `XDG_RUNTIME_DIR` inside bounded storage. Permission
tests use an unprivileged mapped UID instead of the mount-setup user.

Cached prerequisite acquisition uses the ordinary daemon. That daemon's total
scratch usage is not capped by the client scope. Compiler bootstraps are not part
of the tiny-fixture procedure. Fixture output roots and writable volumes are
discarded after each batch. Only bounded reports and the current candidate are
exported. Shared-store cleanup uses owned roots and targeted deletion.

## Contract boundaries

Build-platform tracing currently requires Linux x86-64. Targets are the existing
matrix targets, including foreign ELF targets. Compiler-generated capture extras
require the verified bounded filesystem. Occupied capture descriptors, unknown
plugins, unsupported languages and unmeasured invocation forms produce explicit
incompleteness. Required finalization/publication rejects them.

Direct GNU assembler/linker file inputs and LLVMgold full/ThinLTO are measured.
Direct GCC linker-plugin calls and direct assembler stdin are diagnosed as
incomplete. Compiler-owned GCC LTO subprocesses are covered by the supervisor.
The optional preserve-debug policy has not been measured against every legacy
warning/debug combination. Full dataset rollout and cluster deployment remain
separate operations; the default dataset keeps capture disabled.

Completed batch JSON reports can be compacted with
`tests/compiler-capture/archive_reports.py`. It verifies the archive before
removing released batch directories, and preserves active roots and candidates.
A process lock serializes Nix batches; Rust development uses a separate bounded
512 MiB scratch session.

The compiler entry/target reports and per-batch ledgers live in the owned,
gitignored `.capture-experiments` directory while this work is in progress.

To repeat the full compiler/ISA check, set `fixture_nixpkgs` to a reviewed cached
nixpkgs source and `capture_tool` to the packaged supervisor being tested. The
scripts require Python's standard library and the existing namespace/systemd
test environment:

```sh
python3 tests/compiler-capture/sweep.py \
  --nixpkgs "$fixture_nixpkgs" --capture-tool "$capture_tool" \
  --shapes smoke corners lto thinlto --report .capture-experiments/matrix.json
python3 tests/compiler-capture/matrix_coverage.py \
  --capture-tool "$capture_tool" --reports .capture-experiments/matrix.json \
  --report .capture-experiments/matrix-coverage.json
```

The sweep defaults to every public target. It groups one compiler/target's modes
in a bounded private store and builds them sequentially with one job and core.
Each mode keeps its own validation result. A small index links mode reports so
each metadata file stays below 1 MiB. Optional features rejected by the original
compiler are recorded as baseline-unsupported. Capture failures remain failures.
The coverage gate checks all expected pairs and applicable LTO modes against the
current source/fixture hashes and supervisor binary. Missing, stale, pending and
failed results cannot satisfy it. `--retry-report` retries failed or pending modes;
pass both the original and retry indexes to the coverage gate, in that order.

## November 11 verification evidence

The November 11 native regression report is
`.capture-experiments/all-compilers-current/native-checks/result.json`.
It records 183 Python capture tests with four optional groups skipped, 15 Rust
unit tests, rustfmt, Clippy with warnings denied, and 240 related runner
integration tests. Optional real-tool checks cover legacy Clang PCH/CFI,
LLVM 3.9 direct/archive ThinLTO reproducibility and i686 relocation relaxation.
The latest fast Nix validation check also passed.

The all-target baseline preflight passed all 762 C++/runtime modes, including
applicable full LTO and ThinLTO. Its report is
`.capture-experiments/all-compilers-current/baseline-all.json`. Capture evidence
must separately pass the current-source coverage gate described above.

The November 11 repository adoption check is retained in
`.capture-experiments/all-compilers-current/final-adoption/`. The public
`capture-example`, packaged inspector and fast check built offline in a bounded
private store using the pinned Rust 1.94 toolchain. The example retained 331
invocations, 1249 artifacts and 35029619 artifact bytes. The packaged inspector
validated both the folder and its published form, including the installed ELF
association. The published `hello` ran successfully. The memory peak was
663920640 bytes under a 1 GiB limit. The writable store used a 1 GiB filesystem
alongside bounded 2 GiB compiler staging. The 39198472-byte export imported
successfully, and the public build/inspect commands passed offline afterward.
The private volumes and transfer archive were released. The matrix supervisor
uses cached Rust 1.99 with the same locked dependencies and compiler-capture
source; neither check changes the repository lockfile.

The earlier full pinned-framework run passed 1408 tests with 18 skips. The latest
240 runner integration tests cover publication, preflight, workers and caching
without requiring that optional framework build. Normal compiler failures retain
their original status; required finalization rejects incomplete capture.

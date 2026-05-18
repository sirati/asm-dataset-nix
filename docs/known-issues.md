# Known Cross-Compilation Issues

## Clang 3.5 — AArch64 Segfault in Register Allocator

**Status**: Unfixable (upstream compiler bug in an immature backend)

**Symptom**: Clang 3.5.2 segfaults during compilation when targeting
`aarch64-unknown-linux-gnu`. The crash occurs in the Greedy Register
Allocator pass on `@_getopt_internal_r` (from glibc's `getopt.c`):

```
Running pass 'Greedy Register Allocator' on function '@_getopt_internal_r'
clang-3.5: error: unable to execute command: Segmentation fault (core dumped)
clang-3.5: error: clang frontend command failed due to signal
clang version 3.5.2 (tags/RELEASE_352/final)
Target: aarch64-unknown-linux-gnu
```

**Root cause**: LLVM 3.5 (released September 2014) was the first release
after the AArch64/ARM64 backend merger. Apple's ARM64 backend was merged
with the existing AArch64 implementation in late May 2014 — just months
before release. The newly merged backend had register allocation bugs that
were fixed in later releases.

References:
- [The LLVM AArch64 Backend](https://community.arm.com/arm-community-blogs/b/architectures-and-processors-blog/posts/the-llvm-aarch64-backend) — ARM blog post describing the backend merger
- [ARM64 initial backend import](https://github.com/llvm-mirror/llvm/commit/7b837d8c75f78fe55c9b348b9ec2281169a48d2a) — the commit that imported Apple's ARM64 backend
- [LLVM 3.5 Release Notes](https://releases.llvm.org/3.5.0/docs/ReleaseNotes.html) — notes the AArch64/ARM64 merge as a highlight
- [Bug 181566: AArch64 SIGSEGV in Greedy/Basic Register Allocator](http://www.mail-archive.com/llvm-bugs@lists.llvm.org/msg96654.html) — similar register allocator crash on AArch64 (later version, same class of bug)

**Affected targets**: Only `aarch64`. Clang 3.5 cross-compiles successfully
for `i686`, `armv7l`, `mipsel`, and `mips64el`.

**Workaround**: None. The fix requires upgrading to clang 3.6+, which has a
more mature AArch64 backend. Clang 3.4 (which uses the pre-merger AArch64
backend) also works for aarch64.

---

## Compiler-RT MIPS O32 struct stat Size Mismatch

**Status**: Fixed via overlay in `flake.nix`

**Symptom**: Cross-compiling compiler-rt for `mipsel-unknown-linux-gnu`
fails with a static assertion:

```
sanitizer_platform_limits_linux.cpp:67:38: error: static assertion failed
COMPILER_CHECK(struct_kernel_stat_sz == sizeof(struct stat));
note: the comparison reduces to '(160 == 144)'
```

**Root cause**: compiler-rt hardcodes `struct_kernel_stat_sz = 160` for
32-bit MIPS, but this is the N32 ABI value. The O32 ABI (used by mipsel)
has `sizeof(struct stat) == 144`. This was reported as
[LLVM D129749](https://reviews.llvm.org/D129749) and partially fixed in
LLVM 15 (which changed it to 144), then reverted in LLVM 16 back to 160.
As of LLVM 22, the O32 value is still incorrectly 160.

**Fix applied**: A nixpkgs overlay (`compilerRtMipsOverlay` in `flake.nix`)
patches `sanitizer_platform_limits_posix.h` via `postPatch` sed to replace
`FIRST_32_SECOND_64(160, 216)` with `FIRST_32_SECOND_64(144, 216)` in the
MIPS section. The overlay is applied to the current nixpkgs and all old
nixpkgs imports (22.11, 23.11, 24.05).

---

## Old GCC Cross-Compilation Limitations

### gcc4_4, gcc4_6 (nixpkgs 15.09)

**Status**: Expected — cross toolchain not available

Nixpkgs 15.09 predates `pkgsCross` and its cross-compilation infrastructure
is too primitive to build cross-compilers for these GCC versions. The error
is caught gracefully:

```
gcc44: cross-compiler not available in this nixpkgs for <target>
```

### gcc4_5 (nixpkgs 18.03)

**Status**: Expected — cross toolchain build failure

Nixpkgs 18.03 can re-import with `crossSystem` but gcc4_5 is too old to
bootstrap with the modern host GCC. The cross-compiler build fails with
`gnu_inline` attribute errors.

### gcc5 (nixpkgs 18.03) — mips64el

**Status**: Expected — ABI not supported

Nixpkgs 18.03 does not know the `gnuabin32` ABI used by our `mips64el`
target. The error is caught by `builtins.tryEval` and reported as:

```
gcc5: cross-compiler not available in this nixpkgs for mips64el
```

gcc5 works for all other cross targets (i686, aarch64, armv7l, mipsel).

---

## Clang 15 — mips64el compiler-rt SOFTFP ABI

**Status**: Upstream bug, not fixed

Compiler-rt 15.0.7 fails to build for `mips64el-unknown-linux-gnuabin32`
with:

```
fp_compare_impl.inc:33:1: error: static assertion failed:
    "SOFTFP ABI not compatible with GCC"
```

This is a compiler-rt builtins issue specific to the N32 ABI on MIPS64,
unrelated to the O32 struct stat fix above.

---

## `--max-variants` CLI flag is a no-op pending streaming planner cap

**Status**: Deferred — flag accepted as a no-op; help text marked
DEPRECATED.

**Symptom**: Passing `--max-variants N` does not cap the variant set.
The flag was a submit-time post-sample cap in the legacy partition
pipeline; under the new dependency_graph + streaming planner pipeline
the cap is supposed to be applied by the planner itself, but that
plumbing has not landed yet.

**Where**: `python/compiler_suit_runner/cli.py:325-337` (help text),
`python/compiler_suit_runner/tests/slurm/run_helpers.py:410`
(SLURM `default_invocation_for_smoke` still forwards
`max_variants={1, 10, 50}` for the small/medium/large tiers).

**Risk**: SLURM-test workloads on the medium tier (`max_variants=10`)
or higher can now exceed the 3.5 GiB worker cgroup envelope (see
project memory `feedback_slurm_test_env_memory.md` — the 4 GiB cgroup
is the contract, not a target). Small-tier T20 (`max_variants=1`) and
T23 (which overrides to 2) stay safe. T21 medium is the realistic
exposure surface.

**Resolution**: Wire the per-binary size cap into the streaming
planner (`template_graph/streaming.py` + `dependency_graph_planner.py`).
Once that lands, either reactivate the CLI flag's effect or drop it
entirely.

---

## Phase D verification carryover — D.6 (slurm-test-env) and D.8 (cross-version)

**Status**: Deferred from the Phase B/C/D taxonomy revamp (see
`/home/sirati/.claude/plans/lively-beaming-summit.md`).

**D.6 (SLURM T20-T23 in slurm-test-env)**: PARTIAL. The local cluster
came up cleanly (`INSTANCE_ID=asm SSH_PORT=2244 nix run .#up` in
`~/devel/python/dynamic_runner/slurm-test-env/`) and `srun --partition
=debug -N1 hostname` worked. T20 invoked the new CLI through 42s of
preflight then hit the documented `--build-compilers is off and 17
toolchains missing locally` gate — exactly the new-taxonomy error
text emitted from `cli.py:1112-1119` and a behavioural validation
of the rename. To complete the end-to-end SLURM dispatch, the
operator must either:
- pre-build the 17 cross-toolchain wrappers required by `T20_PACKAGES
  = ('hello', 'zlib', 'busybox')` × tiny workload arch set
  (`nix build .#dataset.<sys>.<binary>.<arch>.<compiler>` for each
  combo, ~hours of compute + tens of GiB), OR
- modify `default_invocation_for_smoke` to inject `--build-compilers`
  so the cluster builds the toolchains itself (intentional semantic
  change for the test — not a Phase D verification step).

The unit-test sweep is green (691 pass), the SLURM tests collect
cleanly (206 collected), and the new submit path runs preflight +
error-path code unchanged through 42s of real execution. The
remaining gate is purely an operator-side toolchain pre-stage; not
a code regression.

**D.8 (cross-version fail-loud regression)**: Run a NEW submitter
against a JSONL aggregate previously written by OLD code; confirm
the new submitter raises (rather than silently miscounting). Needs
two deployed versions side-by-side. Defer to operator post-deploy.

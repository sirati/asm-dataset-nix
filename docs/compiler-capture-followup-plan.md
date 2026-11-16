# Debug output and provenance follow-up

Status: implemented and qualified, 2026-11-14.

## Scope and ownership

Complete the remaining compiler-capture work from items 2.1 and 2.2. Retain the
working preprocessing and behavior protections from 2.3.1 and 2.3.2. Another
agent owns package-release selection and multi-version package support. This
work will not change the package catalog or add a package-version matrix axis.
It may record facts about the package already selected by that agent.

The existing 1079-mode compiler campaign and 762 baseline modes passed on
2026-11-11. Their evidence remains a dated baseline. New metadata and debug
behavior must receive new evidence rather than reusing those counts as proof
of the new features. The Rust supervisor should stay unchanged unless an actual
compiler regression requires a correction.

## 1. Establish the separateDebugInfo contract

Enable explicit Nix `separateDebugInfo` for selected Linux capture packages,
centrally in the capture adapter. Ordinary dataset builds retain their existing
policy. Preserve package phases, source recipes, existing outputs/default
selection, hooks and reference restrictions. Append the capture/debug outputs
only once and handle an existing debug output deliberately.

First build tiny fixtures with the actual selected native and cross stdenvs.
Inspect the selected Nix debug hook before choosing compatibility adjustments.
Its build-ID, debug-generation and compression flags can interact with old
GCC/Clang or assembler versions. A measured compatibility adjustment must be
scoped to added infrastructure flags; preserve caller options and the selected
compiler version. Do not emulate successful debug splitting with a placeholder
directory. Packages that skip fixup or strip during installation must retain
their original behavior and receive an explicit error when the new required
debug-output contract cannot be satisfied. Compiler snapshots remain useful
independently of Nix's installed-file debug split.

Acceptance: a linked installed ELF has a real separate debug file with the
matching ABI/build ID and usable DWARF/source lines, and the installed binary
still executes correctly. Default output selection and existing outputs stay
correct. Empty/no-ELF packages do not require a fictitious debug ELF.

## 2. Validate and publish separate debug files

Extend the bounded ELF reader and association checks to verify separate debug
files, including ELF class, machine, endianness, build ID, DWARF information and
line tables. Check debuglink information where it is present. A debug-looking
filename or matching build ID alone must not establish a complete result.

Nix debug outputs contain build-ID files, basename aliases, executable/source
symlinks and possibly source overlays. Publish regular debug files under
`auxiliary/debug`, preserve their relative lookup layout, and avoid following
executable/source directory links into unrelated store closures. Deduplicate or
resolve allowed internal aliases using a documented bounded rule. Reject broken
or escaping debug-file references under a required contract. Keep debug-only
ELFs out of the executable corpus. Revalidate debug associations and file hashes
on publication reuse and through the public inspector.

Acceptance: raw-folder inspection, staging, publication inspection and preflight
reuse all reject missing, mismatched, corrupt or incorrectly associated required
debug data. A copied published result works without the original debug-output
store path. Interrupted staging retains the previous valid generation.

Split DWARF is part of this contract. Match skeleton compilation units to retained
companions by their DWO identities, not their filenames or artifact roles. Publish
an explicit bounded lookup index and the required companion bytes, or a validated
DWARF package when the selected cached tools support that route. Verify lookup
from a copied publication without build-directory or original debug-output
access. Reject substituted/missing companions, including GCC LTO partition
companions. Do not label a skeleton-only debug file complete.

## 3. Complete capture provenance without owning package selection

Create a small versioned provenance document from actual Nix derivation facts:
dataset package label, available pname/version, selected package derivation and
output identities, source store identity and revision when exposed, and the
repository lockfile hash. Represent unavailable source/revision facts explicitly;
do not infer a source checksum or revision from a Nix store hash or a label.
Keep compiler invocation/configuration identities in the existing capture
manifest and associate its hash with the provenance document.

Carry an independently expected compiler family/version, target triple and
selected wrapper from Nix's toolchain selection into the contract/provenance.
Validate the producers of the installed ELF candidates against those facts.
Host build tools may appear in the manifest without becoming target producers.
Reject correctly rehashed captures made by a different compiler, target or
wrapper. Never repair a mismatch by replacing the requested version with the
observed version. Keep the selected package derivation distinct from the outer
ELF-folder derivation requested by the worker.

Write provenance with the ELF-folder output, validate it against the folder
metadata and requested build, and publish it inside the same atomic generation
as the capture/debug files. Extend the worker's sidecar with the validated
provenance summary. Bound document sizes, reject malformed/duplicate fields and
schema mismatches, and include the document in the completion file index.
Package version fields describe the selected derivation; they must not impose
an API or naming convention on the other agent's catalog work.

Acceptance: the inspector and worker report the same selected package/compiler
facts. Tampered provenance, mismatched derivations and stale metadata cannot
satisfy a new capture request. An unknown source revision remains unknown.

## 4. Version the publication contract and reuse identity

Keep compiler-manifest schema 1 unless its data changes. Add explicit publication
and provenance revisions and required separate-debug/provenance fields to the
new capture contract. Centralize the Nix contract construction and keep its
Python counterpart checked for agreement.

Give new captured publications/cache tasks an updated identity so old captured
results cannot satisfy the new policy. Update CLI secondary propagation and
archive/cache namespaces where required. Continue inspecting existing schema-1
published captures using their declared old contract, while new required
requests enforce the new fields. Do not change ordinary dataset identities.

Acceptance: tests prove old captures remain inspectable, old captures do not
complete new requests, new publication retries are atomic, and manifest/task/
preflight identities agree through primary and secondary runner paths.

Publication must also replace the visible generation at its final destination.
The framework publishes file sets without deleting omitted destination files;
local staging replacement alone cannot establish this guarantee. Use a hidden,
complete destination generation and an atomic commit of the visible variant.
Preserve the normal variant lookup path and publish the sidecar with that same
generation. Pin readers to one generation, bound owned retained generations,
and never delete unrelated destination paths. Test the real pinned publisher
with a shrinking file set and injected interruption before the commit. The
visible variant must resolve to exactly the new valid generation or the previous
valid generation, including when source and destination are different mounts.

## 5. Verification and rollout

1. Add focused negative tests for debug association and symlink traversal,
   provenance/schema/derivation mismatches, completion integrity and reuse.
2. Run the Python capture suite and relevant runner integration tests in the
   existing bounded environment. Run the fast Nix validation check.
3. Build tiny native GCC and Clang fixtures through the real Nix debug hook,
   including an already-enabled debug output, structured attributes/reference
   restrictions, an empty package, skipped fixup and installation-time stripping.
4. Exercise every public compiler/target pair with a tiny actual-target debug
   fixture. Verify separate debug association, source lines, installed ELF ABI
   and native/QEMU execution. Cover native, ELF32/ELF64, little/big endian, old
   GCC/Clang and LTO interactions explicitly. Do not count prerequisite refusals
   or unsupported required debug splitting as passes.
5. Rebuild the public unchanged `capture-example`, inspect it before and after
   publication, execute its published ELF, and verify a store-independent copy.
   Repeat capture/compiler modes affected by any measured flag/backend change.
   Broaden reruns only when changes or failures justify them.
6. Preserve the historical verification record as dated evidence, add a fresh
   follow-up report and document the new format, migration and contract limits.

Use sequential private/offline Nix builds with one job/core, 1 GiB RAM and at
most 3 GiB swap by default. Keep the existing 64 MiB tiny-fixture capture cap,
4 GiB scratch allowance, 20 GiB prerequisite allowance and 100 GiB free reserve.
Dry-run/review new prerequisites; do not trigger compiler bootstraps. Root needed
inputs and keep an ownership ledger. Release owned volumes, transfer files and
obsolete roots with targeted deletion. Preserve the user's lockfile and original
planning document. No commits, deployment or full dataset rollout are required.

## Review decisions

The requested Codex GPT 6.1 Sol review ran at xhigh effort through T3. Its
read-only review completed on 2026-11-14.

Review: [Adversarial review of debug-info and provenance plan](t3-thread://v2/6c52d31d-332b-401b-b144-4cd48a7a8e4d/thread:delegated-task:command%3Amcp%3Aa6ff1bc8-1412-44d2-8919-f64dd8a4a7a2%3Adelegate-task%3A49d67e8a-bcf0-4ebe-989e-a5e3dafb08dc).

Accepted all three findings after checking the cited code:

- P1, final-destination replacement. `task.publish_all` replaces its listed files
  but leaves removed build-ID/debug filenames behind. Add destination generation
  replacement and a real publication/retry/interruption test.
- P1, expected compiler identity. The current policy check does not bind the
  producer to the requested version/target/wrapper, and the worker overwrites
  the requested compiler version. Add independent expected identities and
  correctly rehashed mismatch tests.
- P2, split DWARF. Captured `.dwo` bytes alone do not prove that a published
  skeleton can find its companion. Add DWO identity association and an explicit
  publication lookup contract, including GCC LTO.

No findings were rejected. These affect already supported paths rather than
speculative features. A framework-wide publisher redesign and an exhaustive
repeat of unchanged compiler modes remain outside scope; implement the required
capture generation behavior locally and rerun modes affected by actual changes.

Qualification found a justified Rust change. The Nix hook's assembler-compression
option forced Clang 3.8 AArch64 onto saved-text reassembly, changing unwind data
and linked layout. Recognize supported compression/fatal-warning controls for
native IR emission, retain unknown-option fallback, and forward the final
assembler job's compression mode. The exact failing fixture then passed strict
behavior comparison, separate-debug source mapping and QEMU execution. Restart
the complete debug-hook campaign with the rebuilt public supervisor.

Two additional measured corrections were required. Clang's bitcode-only
split-DWARF frontend defers its companion to native LTO emission; validate actual
bitcode before allowing that deferral and continue requiring native companions.
GCC 5 ARM selected its configured legacy linker despite `-B`, so the Nix debug
hook received no build ID. Use the cached target collect2 for GCC 5 cross drivers
while retaining the original frontend/plugin arguments. All five public GCC 5
target debug fixtures and all ten C++/LTO baseline runtime modes passed afterward.

Publication review also found two interrupted-rename windows. A killed writer
can leave a pointer or history temporary file. Recovery now removes those owned
temporary names under the publication lock, rejects links outside the managed
generation directory, and retains a valid visible result. Real-transport tests
kill writers at both renames and require the next retry to succeed without
temporary leftovers.


## Completion evidence

[The follow-up verification record](compiler-capture-followup-verification.json)
records all 317 public compiler/target debug-hook fixtures and 15 additional
LTO, ThinLTO and split-DWARF modes passing. Each normal fixture checks actual C/C++
driver versions, behavior against a required-debug baseline, target ABI, separate
debug source lines, and native or QEMU execution. All eight package-hook boundary
cases produced their expected outcomes.

The final capture suite ran 204 tests with 16 skips; the runner suite passed
1408 tests with two skips and 17 deselections. Rust's 17 unit tests, rustfmt and
Clippy with warnings denied passed. The pinned native publisher passed shrinking
file-set, cross-mount, SIGKILL recovery and unrelated-path preservation checks.
Both newly found interrupted-rename failures were reproduced before the fix.

The public example, inspector and fast check passed offline. The public and
matrix supervisor binaries have identical SHA-256 hashes. Current inspection
also accepts the real revision-1 example. The November 11 compiler campaign
remains a separate historical record. No package-version axis or dataset-wide
rollout was added.

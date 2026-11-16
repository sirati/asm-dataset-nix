# Capture publication format

Compiler manifests retain schema 1. Capture publication revision 2 adds explicit
Nix separate debug information, independently selected toolchain provenance and
complete destination generations. The feature remains opt-in.

## Nix outputs and selected build facts

A captured ELF-folder output contains:

```text
elf/<program>                   installed ELF links
meta.json                       variant metadata and required capture contract
provenance.json                 schema-1 selected build facts
auxiliary/datasetCapture/       compiler manifest and content-addressed artifacts
auxiliary/debug/                Nix separateDebugInfo output
```

The capture adapter enables `separateDebugInfo` for captured dataset variants.
It preserves ordinary outputs and default selection. Existing reference checks
remain active; the adapter uses structured attributes when Nix requires them
for separate debug information. Nix's normal debug-output reference policy still
applies. Capture output restrictions that conflict with retaining compiler
provenance fail explicitly.

Clang 3.4 rejects the assembler-compression option added by Nix's debug hook.
For that release, the adapter removes one hook-added occurrence before running
the package's own `postHook`. An explicit caller request remains effective.
The adjustment is recorded in `build.debug_policy.hook_compatibility`.

GCC 5 cross drivers can select their configured legacy linker through old
`collect2`, bypassing the selected Nix linker wrapper and its build-ID policy.
Those adapters use the cached target collect2 and selected bintools wrapper.
The original GCC 5 driver still supplies its frontend, LTO plugin and lto-wrapper.

Known assembler compression controls retain Clang's native IR-to-object path.
The supervisor forwards the actual integrated assembler's compression selection,
including older LLVM's bare boolean flag. Unknown assembler controls continue to
use their existing assembly path. This avoids the Clang 3.8 AArch64 unwind/layout
change exposed by Nix's new compression flag.

Provenance records the dataset label, actual derivation pname/version when
available, selected package derivation, named output paths/roles, source store
identity and exposed revision, lockfile SHA-256, and selected compiler/target/
wrapper/binutils identities. Missing facts are `null`. A source store path is
an identity, not a claimed source-content checksum. An exposed revision may be
a fetcher tag rather than a resolved Git commit.

The package derivation identifies the compiled package. The completion record's
`drv` identifies the outer ELF-folder derivation requested by the worker. These
are different derivations. Recording the package derivation's text does not
request all outputs in its build-time dependency graph.

## Debug and compiler associations

Each installed ELF must match retained pre-install compiler output, including
its ABI, code, allocated sections and dynamic linkage under the existing fixup
normalization. Its producer must match independently selected Nix toolchain
facts. The worker also checks the requested package/compiler/architecture/flags
against Nix metadata. Observed versions never replace requested versions in the
sidecar; the full observed version remains available in capture provenance.

Separate debug files must match the selected ELF's ABI, flags, type and build ID.
Their decompressed DWARF sections must match the retained pre-install ELF, with
real compilation units and statement programs. GNU debuglink names and CRCs are
checked when present. A matching filename or build ID alone is insufficient.

Nix's build-ID layout and internal debug aliases are published as regular files.
Executable/source symlinks and source-overlay directory links are not followed
into other store closures. The capture manifest retains preprocessed source
artifacts independently. Debug-only ELFs remain outside the executable corpus.

For split DWARF, `debug_associations[program][...].companions` maps each DWO unit
ID to its retained content-addressed companion under
`auxiliary/datasetCapture/objects/`. Consumers resolve companions through this
index instead of the compiler's obsolete build-directory filename. The index
supports the existing GCC LTO companion path. It does not claim automatic GDB
discovery of every original `.dwo` filename. Skeletons without matching retained
unit identities are incomplete.

An LLVM bitcode-only compile step may defer companion creation to the native
LTO link. Validation permits that measured frontend behavior only when the code
outputs are bitcode. Native split-debug output still requires its companions;
the final publication resolves every retained skeleton's DWO identities.

Empty packages need no fictitious debug ELF. Packages that skip fixup or strip
away installed DWARF before Nix's hook cannot meet the new required separate-debug
contract and are rejected. Their retained compiler snapshots do not substitute
for a missing required Nix debug output. The supervisor's preserve-debug policy
does not disable flags introduced by Nix's separateDebugInfo setup hook.

## Published generations and completion

A publication contains regular ELF files at its root and capture/debug/provenance
under `auxiliary/`. Selected build facts and original Nix metadata are copied to
`auxiliary/provenance/build.json` and `metadata.json`. Worker publication also
includes the indexed sidecar at `auxiliary/provenance/variant.json`.

The completion document retains `schema: 1` and declares `publication_schema: 2`.
It binds the requested label/ELF-folder derivation, contract, compiler configuration
identities, capture manifest hash, provenance, ELF/debug associations and every
published file's SHA-256, size, role and execute permission. Reuse and inspection
recheck these relationships. Incomplete/corrupt files, contradictory facts and
unindexed files do not satisfy a required capture request.

The worker transfers a complete generation into a hidden destination directory,
checks it, then atomically replaces a relative variant symlink:

```text
<package>/<variant> -> .<variant>.capture-generations/generation-<id>/
<package>/<variant>.json -> <variant>/auxiliary/provenance/variant.json
```

The normal lookup paths remain stable. Read the completion file index and select
`role: installed-elf` to enumerate corpus files. Do not recursively collect every
ELF beneath a package directory: auxiliary artifacts and retained generations
also contain ELF data. Readers resolve the variant symlink once to pin one
generation. A copied variant can be dereferenced into an ordinary directory.

The current and previous generations are retained. An interrupted replacement
can leave one additional hidden generation; the next writer cleans owned stale
generations and temporary pointer/history files. Cleanup rejects temporary links
that lead outside the managed generation directory. A failed transfer/validation
or a process killed before commit leaves
the previous visible generation intact. The indexed sidecar follows the same
generation. The byte transport's cross-filesystem copy does not preserve execute
bits, so the capture publisher restores them from the validated sender index
before validating or exposing the destination.

## Migration and limits

New captured variant directories use `_capture-v2`, task IDs use `__capture_v2`,
and captured matrix archives default to `_matrix_eval_capture_v2`. Ordinary dataset
namespaces retain their existing rules. Use the updated runner/worker code for
revision-2 publications. Revision-1 results remain inspectable under their old
contract, but cannot complete a revision-2 request.

Revision-2 publications have a 1 GiB combined payload limit, an 8192-entry file index,
16 MiB completion/manifest limits and 1 MiB provenance-document limits. DWARF
analysis bounds section expansion, compilation units, strings and association
indexes. Required failures remain explicit; artifacts are not truncated and
reported complete. Experimental batches retain their stricter fixture budgets.

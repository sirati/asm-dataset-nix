"""Inspect capture outputs without importing the dataset runner or its framework."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

if __package__:
    from . import capture
else:
    import capture


def inspect_capture(path, *, allow_incomplete=False):
    root = Path(path).resolve(strict=True)
    published = (root/"capture-complete.json").exists()
    folder = (root/"auxiliary"/"datasetCapture").is_dir()
    capture_root = root/"auxiliary"/"datasetCapture" if folder else root
    verified = capture.validate_capture(capture_root,require_complete=not allow_incomplete)
    contract = None
    associations = None
    provenance = None
    debug_associations = None
    if published:
        completion = json.loads(capture._read_regular(root/"capture-complete.json",capture.MAX_MANIFEST_BYTES),
            object_pairs_hook=capture._unique_keys)
        if not isinstance(completion,dict) or not capture.captured_variant_complete(root,
                label=completion.get("label"),drv=completion.get("drv"),contract=completion.get("contract")):
            raise capture.CaptureValidationError("published capture failed completion validation")
        contract = completion["contract"]
        associations = completion["elf_associations"]
        provenance = completion.get("provenance")
        debug_associations = completion.get("debug_associations")
    elif folder:
        metadata = json.loads(capture._read_regular(root/"meta.json",capture.MAX_MANIFEST_BYTES),
            object_pairs_hook=capture._unique_keys)
        if not isinstance(metadata,dict):
            raise capture.CaptureValidationError("invalid dataset folder metadata")
        contract = metadata.get("compilerCapture")
        capture._validate_contract(verified,contract)
        provenance = None
        if contract.get("publication_schema", 1) >= 2:
            provenance = capture.validate_provenance(root, contract=contract, metadata=metadata)
        if not (root/"elf").is_dir():
            raise capture.CaptureValidationError("missing executable corpus")
        associations = capture.elf_associations(root/"elf",verified,
            required=contract.get("required_elf_associations",False),
            required_debug=contract.get("required_elf_associations",False) and contract.get("debug_policy")=="required",
            expected_toolchain=provenance["toolchain"] if provenance is not None else None)
        if provenance is not None:
            debug_associations, _ = capture.separate_debug_associations(root/"auxiliary"/"debug", verified, associations, installed_root=root/"elf")
    compilers = {}
    roles = Counter()
    modes = Counter()
    for entry in verified.manifest["invocations"]:
        record = entry["record"]
        config = record["compiler"]
        compilers[record["config_sha256"]] = {
            key:config.get(key) for key in ["family","version","target","driver","backend","debug_policy","wrapper"]}
        modes[record["analysis"].get("mode","unknown")] += entry["count"]
        for artifact in record["artifacts"]:
            roles[artifact["role"]] += entry["count"]
    return dict(schema=capture.SCHEMA,kind="published" if published else "dataset-folder" if folder else "capture-output",
        complete=verified.complete,policy=verified.manifest["policy"],manifest_sha256=verified.manifest_sha256,
        invocation_count=verified.invocation_count,unique_invocations=len(verified.manifest["invocations"]),
        artifact_count=verified.artifact_count,artifact_bytes=verified.artifact_bytes,
        artifact_references_by_role=dict(sorted(roles.items())),invocations_by_mode=dict(sorted(modes.items())),
        compilers=[dict(config_sha256=key,**value) for key,value in sorted(compilers.items())],
        installed_elf_count=len(associations) if associations is not None else None,
        associated_elf_count=sum(bool(value["candidates"]) for value in associations.values()) if associations is not None else None,
        contract=contract,errors=verified.manifest["errors"],
        **(dict(publication_schema=contract["publication_schema"], provenance=provenance,
            separate_debug_file_count=len({entry["sha256"] for entries in debug_associations.values() for entry in entries}),
            split_dwarf_companion_count=len({identity for entries in debug_associations.values() for entry in entries for identity in entry["companions"]}))
            if provenance is not None else {}))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate and summarize a compiler capture output, dataset folder, or published variant.")
    parser.add_argument("path",type=Path,help="datasetCapture output, ELF folder, or published variant directory")
    parser.add_argument("--json",action="store_true",help="write a machine-readable summary")
    parser.add_argument("--allow-incomplete",action="store_true",
        help="inspect incomplete raw capture for diagnosis; publication validation remains required")
    args = parser.parse_args(argv)
    try:
        summary = inspect_capture(args.path,allow_incomplete=args.allow_incomplete)
    except (capture.CaptureValidationError,OSError,ValueError,TypeError,KeyError,RecursionError) as error:
        print(f"capture-inspect: {error}",file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary,sort_keys=True,indent=2))
    else:
        print(f"{summary['kind']}: {'complete' if summary['complete'] else 'incomplete'} ({summary['policy']})")
        print(f"{summary['invocation_count']} invocations, {summary['artifact_count']} artifacts, {summary['artifact_bytes']} artifact bytes")
        for compiler in summary["compilers"]:
            print(f"{compiler['family']} {compiler['version']} {compiler['target']} {compiler['driver']} ({compiler['backend']})")
        if summary["installed_elf_count"] is not None:
            print(f"{summary['associated_elf_count']}/{summary['installed_elf_count']} installed ELFs have pre-install associations")
        for error in summary["errors"]:
            print(f"capture error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

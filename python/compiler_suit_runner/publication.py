"""Stage release-checked results and validate completion without trusting labels."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import tempfile

from .package_selection import RELEASE_FIELDS, validate_release, variant_metadata

COMPLETION = "package-complete.json"


def _digest(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _component(value: str) -> str:
    if not isinstance(value, str) or value in ("", ".", "..") or pathlib.Path(value).name != value:
        raise ValueError(f"invalid publication path component: {value!r}")
    return value


def _matches(actual: dict, expected: dict) -> bool:
    validate_release(actual)
    validate_release(expected)
    return all(actual.get(key) == expected.get(key) for key in (
        *RELEASE_FIELDS, "label", "arch", "drv",
    )) and actual.get("pkg") == expected.get("pkg", expected["packageId"])


def variant_complete(package_dir: pathlib.Path, expected: dict) -> bool:
    """Legacy, partial, stale, or damaged publications are cache misses."""
    try:
        variant_dir = _component(expected["variant_dir"])
        root = pathlib.Path(package_dir) / variant_dir
        metadata_name = _component(expected.get("metadata_name", variant_dir + ".json"))
        sidecar_path = pathlib.Path(package_dir) / metadata_name
        if sidecar_path.is_symlink() or root.is_symlink():
            return False
        sidecar = json.loads(sidecar_path.read_bytes())
        if not _matches(sidecar, expected):
            return False
        complete = json.loads((root / COMPLETION).read_bytes())
        if not isinstance(complete, dict):
            return False
        if complete.get("schema") != 1 or complete.get("packageIdentity") != expected["packageIdentity"] or complete.get("drv") != expected["drv"]:
            return False
        files = complete["files"]
        if not isinstance(files, dict) or not files or "metadata.json" not in files:
            return False
        if set(p.name for p in root.iterdir()) != set(files) | {COMPLETION}:
            return False
        for name, digest in files.items():
            path = root / _component(name)
            if path.is_symlink() or not path.is_file() or _digest(path) != digest:
                return False
        internal = json.loads((root / "metadata.json").read_bytes())
        return internal == sidecar and bool(sidecar.get("packageDrv")) and bool(sidecar.get("packageOutputs")) and bool(sidecar.get("outputPaths"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def stage_release(out_store: pathlib.Path, package_dir: pathlib.Path, payload: dict) -> list[pathlib.Path]:
    """Return one set for the framework's atomic publish_all transaction."""
    validate_release(payload)
    meta = json.loads((out_store / "meta.json").read_bytes())
    validate_release(meta)
    if payload["packageId"] != payload.get("pkg") or meta.get("arch") != payload.get("arch"):
        raise ValueError("built release package/target differs from the manifest")
    for key in RELEASE_FIELDS:
        if payload[key] != meta[key]:
            raise ValueError(f"built release provenance mismatch: {key}")
    translated = variant_metadata(meta)
    for key in ("compiler_id", "compiler_family", "compiler_version", "optimization", "flag_set", "hardening", "sanitizer", "march"):
        if payload.get(key) and payload[key] != translated[key]:
            raise ValueError(f"built compiler/flag metadata mismatch: {key}")
    if not meta.get("packageDrv") or not meta.get("packageOutputs"):
        raise ValueError("built release lacks package derivation/output paths")
    variant_dir = _component(payload["variant_dir"])
    metadata_name = _component(payload.get("metadata_name", variant_dir + ".json"))
    output_paths = payload.get("outputPaths") or {"out": str(out_store)}
    if output_paths.get("out") != str(out_store):
        raise ValueError("realized variant output differs from the archived derivation")
    sidecar = {
        **meta, **translated, "label": payload["label"], "pkg": payload["pkg"],
        "arch": payload["arch"], "drv": payload["drv"],
        "variant_dir": variant_dir, "outputPaths": output_paths,
    }
    package_dir.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=f".{variant_dir}.", dir=package_dir))
    try:
        elfs = sorted((out_store / "elf").iterdir())
        if not elfs:
            raise ValueError("built variant contains no ELF files")
        for path in elfs:
            name = _component(path.name)
            if name in ("metadata.json", COMPLETION):
                raise ValueError(f"ELF name collides with publication metadata: {name}")
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    raise ValueError(f"non-ELF entry in built variant: {name}")
            shutil.copy2(path, temporary / name, follow_symlinks=True)
        (temporary / "metadata.json").write_text(json.dumps(sidecar, sort_keys=True, indent=2) + "\n")
        completion = {
            "schema": 1, "packageIdentity": payload["packageIdentity"], "drv": payload["drv"],
            "files": {path.name: _digest(path) for path in sorted(temporary.iterdir())},
        }
        (temporary / COMPLETION).write_text(json.dumps(completion, sort_keys=True) + "\n")
        final = package_dir / variant_dir
        # Only worker-local staging is replaced here. Shared publication is
        # the framework's set-atomic transaction over the returned paths.
        if final.exists():
            shutil.rmtree(final)
        os.replace(temporary, final)
        from .workers.build_worker import write_sidecar_metadata
        sidecar_path = write_sidecar_metadata(package_dir, metadata_name, sidecar)
        return [*sorted(final.iterdir()), sidecar_path]
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

"""Curated release selection and identities shared by all runner phases."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

CATALOG_SCHEMA = 1
RELEASE_FIELDS = (
    "packageCatalogSchema", "packageFamily", "packageId", "packageVersion",
    "releaseId", "recipeId", "sourceIdentity", "packageIdentity", "isDefault",
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+\-]*\Z")


def validate_package_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or "__" in value:
        raise ValueError(f"unsafe package ID: {value!r}")
    return value


def nix_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("${", r"\${")


def nix_attr_path(*parts: str) -> str:
    """Bare Nix identifiers stay familiar; other attribute names are quoted."""
    return ".".join(
        part if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'\-]*", part) else nix_string(part)
        for part in parts
    )


def select_package_ids(
    catalog: Mapping[str, dict], *, families: Sequence[str] | None = None,
    versions: Sequence[str] | None = None, package_ids: Sequence[str] | None = None,
    all_versions: bool = False,
) -> list[str]:
    """Resolve once to sorted unique package IDs, rejecting empty/ambiguous requests."""
    known = {entry["packageFamily"] for entry in catalog.values()}
    selected_families = set(families or ())
    unknown = selected_families - known
    if unknown:
        raise ValueError(f"unknown package families: {', '.join(sorted(unknown))}")
    explicit: dict[str, set[str]] = {}
    for spec in versions or ():
        family, sep, version = spec.partition("=")
        if not sep or not family or not version:
            raise ValueError(f"expected FAMILY=EXACT_VERSION, got {spec!r}")
        if family not in known:
            raise ValueError(f"unknown package family: {family}")
        matches = [key for key, entry in catalog.items()
                   if entry["packageFamily"] == family and entry.get("packageVersion") == version]
        if not matches:
            raise ValueError(f"unknown package version: {family}={version}")
        if len(matches) != 1:
            candidates = "; ".join(f"{key}: {catalog[key].get('recipeId')}" for key in sorted(matches))
            raise ValueError(f"ambiguous package version {spec!r}; use --package-ids: {candidates}")
        explicit.setdefault(family, set()).update(matches)
    for key in package_ids or ():
        validate_package_id(key)
        if key not in catalog:
            raise ValueError(f"unknown package ID: {key}")
        explicit.setdefault(catalog[key]["packageFamily"], set()).add(key)
    if families is not None:
        conflicts = set(explicit) - selected_families
        if conflicts:
            raise ValueError(f"release selections conflict with --packages: {', '.join(sorted(conflicts))}")
    elif explicit:
        selected_families = set(explicit)
    else:
        selected_families = known
    if all_versions and explicit:
        raise ValueError("--all-package-versions cannot be combined with explicit release selections")
    selected = []
    for family in sorted(selected_families):
        if family in explicit:
            selected.extend(explicit[family])
        else:
            selected.extend(key for key, entry in catalog.items()
                            if entry["packageFamily"] == family and (all_versions or entry["isDefault"]))
    if not selected:
        raise ValueError("package selection is empty")
    return sorted(set(selected))


def release_metadata(value: Mapping) -> dict:
    """Do not fabricate provenance for legacy manifests or cache payloads."""
    return {key: value[key] for key in RELEASE_FIELDS if key in value}


def matrix_evaluation_identity(value: Mapping) -> str | None:
    """Bind prestaged archives to evaluation inputs and the requested sample."""
    if value.get("variant_inventory_schema") != 1:
        return None
    context = {"schema": 1, "sys": value.get("sys"),
               "inventory": value.get("variant_inventory_identity"),
               "archs": sorted(set(value.get("archs", ()))),
               "suffixes": sorted(set(value.get("suffixes", ()))),
               "sample": value.get("variant_sample") or 0,
               "seed": value.get("variant_seed") or None,
               "toolchains": value.get("toolchain_aggregate_drv")}
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_release(value: Mapping) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("release metadata is not an object")
    missing = set(RELEASE_FIELDS) - value.keys()
    if missing:
        raise ValueError(f"release metadata missing {sorted(missing)}")
    validate_package_id(value["packageId"])
    if type(value["packageCatalogSchema"]) is not int or value["packageCatalogSchema"] != CATALOG_SCHEMA:
        raise ValueError("unsupported package catalog schema")
    identity = value.get("packageIdentity")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("invalid resolved package identity")
    if (not isinstance(value.get("packageVersion"), str) or not value["packageVersion"]
            or not isinstance(value.get("recipeId"), dict) or not value["recipeId"]
            or not isinstance(value.get("sourceIdentity"), dict) or not value["sourceIdentity"]
            or not isinstance(value.get("releaseId"), str) or not value["releaseId"]
            or not isinstance(value.get("packageFamily"), str) or not value["packageFamily"]
            or type(value.get("isDefault")) is not bool):
        raise ValueError("incomplete release provenance")


def catalog_token(catalog: Mapping[str, dict]) -> str:
    for entry in catalog.values():
        if any(state["available"] for state in entry.get("availability", {}).values()):
            validate_release(entry)
    canonical = {key: {**release_metadata(value), **{
        field: value[field] for field in ("variantInventorySchema", "variantInventoryIdentity") if field in value
    }} for key, value in sorted(catalog.items())}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_variant_task_id(system: str, package_id: str, arch: str, label: str,
                          package_identity: str | None = None) -> str:
    # Worker labels may contain only a suffix. Architecture must survive that
    # path too. Both manifest and streamed descriptors call this helper.
    identity = f"__{package_identity}" if package_identity else ""
    return f"build_variant__{system}__{package_id}__{arch}__{label}{identity}"


def variant_metadata(value: Mapping) -> dict:
    """Translate authoritative Nix metadata once for both planning paths."""
    result = release_metadata(value)
    result.update({
        "compiler_id": value.get("compiler", ""),
        "compiler_family": value.get("compilerFamily", ""),
        "compiler_version": value.get("compilerVersion", ""),
        "optimization": value.get("optimization", ""),
        "flag_set": value.get("flags", ""),
        "hardening": value.get("hardening", ""),
        "sanitizer": value.get("sanitizer", ""),
        "march": value.get("march", ""),
    })
    for field in ("packageDrv", "packageOutputs", "compilerCapture"):
        if field in value:
            result[field] = value[field]
    return result


def variant_output_names(value: Mapping) -> dict:
    if not value.get("packageIdentity"):
        return {}
    from .preflight import _short_dataset_name
    name = _short_dataset_name(
        compiler_id=value["compiler_id"], arch=value["arch"], optimization=value["optimization"],
        full_label=value["label"], package_identity=value["packageIdentity"], variant_drv=value["drv"],
    )
    capture = value.get("compiler_capture")
    if capture is not None:
        # Captured variants publish beside, never over, the ordinary
        # variant (same suffix rule as capture.captured_spec).
        name += "_capture-v" + str(capture.get("publication_schema", 1))
    return {"variant_dir": name, "metadata_name": name + ".json"}

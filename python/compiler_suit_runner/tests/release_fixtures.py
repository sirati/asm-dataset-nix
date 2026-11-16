"""Small resolved catalog records for scheduler-independent tests."""
import hashlib
import json


def release(package_id="hello", version="2.12.3", *, family="hello", recipe="current", default=True):
    record = {
        "packageFamily": family, "packageVersion": version,
        "recipeId": {"provider": recipe, "revision": "pinned-" + recipe,
                     "attr": family, "dependencyPolicy": "provider-pinned-with-selected-stdenv", "adapterRevision": 0},
        "sourceIdentity": {"outputHash": "source-" + version, "revision": None},
        "releaseId": family + "@" + recipe,
    }
    return {**record, "packageCatalogSchema": 1, "packageId": package_id,
            "isDefault": default,
            "packageIdentity": hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()}


def catalog_entry(*args, **kwargs):
    return {**release(*args, **kwargs), "availability": {
        "x86_64": {"available": True, "reason": None},
        "aarch64": {"available": True, "reason": None},
    }}

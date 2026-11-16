"""Compact compiler/flag inventory shared by release evaluation workers."""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
from pathlib import Path
import random
import re
import tempfile

from .package_selection import nix_attr_path

SCHEMA = 1
MAX_BYTES = 4 * 1024 * 1024
AXES = ("flags", "hardening", "sanitizer", "march")
_LABEL = re.compile(r"[A-Za-z0-9_+\-]+\Z")
_IDENTITY = re.compile(r"[a-f0-9]{64}\Z")
logger = logging.getLogger(__name__)


def validate_inventory(value, identity, arch):
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != SCHEMA:
        raise ValueError("unsupported variant inventory schema")
    if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or value.get("identity") != identity:
        raise ValueError("variant inventory identity mismatch")
    if value.get("arch") != arch:
        raise ValueError("variant inventory target mismatch")
    groups = value.get("groups")
    if not isinstance(groups, list) or len(groups) > 8192:
        raise ValueError("invalid variant inventory groups")
    seen = set()
    total_candidates = 0
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("invalid variant inventory group")
        for key in ("compiler", "optimization"):
            label = group.get(key)
            if not isinstance(label, str) or not _LABEL.fullmatch(label):
                raise ValueError(f"invalid inventory {key}")
        key = (group["compiler"], group["optimization"])
        if key in seen:
            raise ValueError("duplicate compiler/optimization group")
        seen.add(key)
        if group.get("compilerFamily") not in ("gcc", "clang") or not isinstance(group.get("compilerVersion"), str):
            raise ValueError("invalid inventory compiler provenance")
        axes = group.get("axes")
        if not isinstance(axes, dict) or set(axes) != set(AXES):
            raise ValueError("invalid inventory axes")
        for labels in axes.values():
            if not isinstance(labels, list) or len(labels) > 128 or any(
                not isinstance(label, str) or not _LABEL.fullmatch(label) for label in labels
            ):
                raise ValueError("invalid inventory axis labels")
            if len(set(labels)) != len(labels):
                raise ValueError("duplicate inventory axis label")
        count = 1
        for labels in axes.values():
            count *= len(labels)
        if count > 100_000:
            raise ValueError("variant inventory group exceeds candidate limit")
        total_candidates += count
    if total_candidates > 5_000_000:
        raise ValueError("variant inventory exceeds candidate limit")
    return value


def load_inventory(flake_ref, system, arch, *, expected_identity, cache_dir, run_subprocess):
    """Cache only compact axes, keyed by the actual immutable flake source."""
    from .preflight import run_nix_eval

    if not isinstance(expected_identity, str) or not _IDENTITY.fullmatch(expected_identity):
        raise ValueError("invalid expected variant inventory identity")
    identity = run_nix_eval(flake_ref, nix_attr_path("_evaluationIdentity", system),
                            raw=True, no_eval_cache=True, run_subprocess=run_subprocess).strip()
    if identity != expected_identity:
        raise ValueError("flake changed since preflight; regenerate the release selection")
    key = hashlib.sha256(json.dumps([SCHEMA, system, arch, identity]).encode()).hexdigest()
    path = Path(cache_dir) / "_variant_inventory" / (key + ".json") if cache_dir is not None else None
    if path is not None:
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("variant inventory cache exceeds size limit")
            cached = json.loads(data)
            encoded = json.dumps(cached["data"], sort_keys=True, separators=(",", ":")).encode()
            if cached.get("digest") != hashlib.sha256(encoded).hexdigest():
                raise ValueError("variant inventory cache checksum mismatch")
            return validate_inventory(cached["data"], identity, arch)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning("invalid variant inventory cache %s; regenerating", path)
    result = validate_inventory(run_nix_eval(
        flake_ref, nix_attr_path("_variantAxes", system, arch), run_subprocess=run_subprocess,
        no_eval_cache=True,
    ), identity, arch)
    if path is not None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            data = json.dumps({"data": result, "digest": hashlib.sha256(encoded).hexdigest()},
                              sort_keys=True, separators=(",", ":")).encode()
            if len(data) > MAX_BYTES:
                raise ValueError("variant inventory exceeds cache size limit")
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, path)
        except OSError:
            logger.warning("cannot cache variant inventory at %s", path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return result


def sample_inventory(inventory, *, arch, package_metadata, sample_size, seed, ceiling=None, flake_ref=None):
    """Match legacy sorted-candidate random sampling without full metadata JSON.

    ``flake_ref`` locates ``table.md``; without it the working directory
    is used, which is not the flake root inside the worker container.
    """
    from .preflight import is_known_bad_combo
    from .support_table import is_supported, load_support_table, load_support_table_for_flake

    support = load_support_table_for_flake(flake_ref) if flake_ref else load_support_table()
    selected = []
    wanted = set(ceiling) if ceiling else None
    for group in inventory["groups"]:
        if not is_supported(support, group["compiler"], arch):
            continue
        base = {**package_metadata, "package": package_metadata["packageFamily"], "arch": arch,
                **{key: group[key] for key in ("compiler", "compilerFamily", "compilerVersion", "optimization")}}
        prefix = f"{group['compiler']}-{group['optimization']}-"
        candidates = []
        for values in itertools.product(*(group["axes"][axis] for axis in AXES)):
            suffix = prefix + "-".join(values)
            if wanted is not None and suffix not in wanted:
                continue
            if is_known_bad_combo({**base, **dict(zip(AXES, values))}) is None:
                candidates.append(suffix)
        # Labels such as ssp and ssp-all prevent sorting each axis alone
        # from reproducing the legacy ordering of complete suffix strings.
        candidates.sort()
        if sample_size and sample_size > 0 and seed:
            rng = random.Random(f"{seed}:{group['compiler']}:{arch}:{group['optimization']}")
            candidates = rng.sample(candidates, min(sample_size, len(candidates)))
        selected.extend(candidates)
    return sorted(selected)

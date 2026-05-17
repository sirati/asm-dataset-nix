"""Thin wrapper around ``nix derivation show`` that resolves a drv path's
realised ``outputs.out.path``.

Extracted from the deleted ``partition_local`` module — the only
remaining surface a live caller (``cli.py``) needed once the streaming
template_graph planner replaced the local partition+merge pass.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from typing import Any, Optional


RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def _default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    proc = subprocess.run(  # noqa: S603 - argv is constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode


_NIX_STORE_PREFIX = "/nix/store/"

_NIX_SHOW_FLAT_CMD: tuple[str, ...] = (
    "nix",
    "--extra-experimental-features",
    "nix-command flakes",
    "derivation",
    "show",
)


def _normalize_show_output(parsed: Any) -> dict[str, Any]:
    """Normalize ``nix derivation show`` JSON to flat ``{full_drv: node}``.

    Modern nix (``version=4``) wraps as ``{"derivations": {...}, "version": 4}``
    and strips the ``/nix/store/`` prefix from drv keys AND from each
    output's ``path`` field. Older nix returned a flat dict with the
    prefix intact. We re-add the prefix in both places so downstream
    callers can string-compare against ``nix path-info`` output.
    """
    if not isinstance(parsed, dict):
        return {}
    if "derivations" in parsed and isinstance(parsed["derivations"], dict):
        flat = parsed["derivations"]
    else:
        flat = parsed
    normalized: dict[str, Any] = {}
    for key, node in flat.items():
        if not isinstance(key, str) or not isinstance(node, dict):
            continue
        full_drv = key if key.startswith(_NIX_STORE_PREFIX) else _NIX_STORE_PREFIX + key
        outputs = node.get("outputs")
        if isinstance(outputs, dict):
            for entry in outputs.values():
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                if isinstance(path, str) and not path.startswith(_NIX_STORE_PREFIX):
                    entry["path"] = _NIX_STORE_PREFIX + path
        normalized[full_drv] = node
    return normalized


def eval_drv_outpaths(
    drvs: Iterable[str],
    *,
    run_subprocess: Optional[RunSubprocess] = None,
) -> dict[str, str]:
    """Resolve each drv's primary output path (``outputs.out.path``).

    Used to enrich the cluster placement map with toolchain drvs that
    aren't in the variant-graph walk (toolchains live in a disjoint
    subgraph reached via the ``_crossToolchainMap`` flake attribute).
    Wire: ``nix derivation show <drv1> <drv2> ...`` (non-recursive),
    single subprocess. Drvs that fail to resolve are omitted.
    """
    drv_list = [d for d in drvs if d and d.endswith(".drv")]
    if not drv_list:
        return {}
    runner = run_subprocess or _default_run_subprocess
    cmd = [*_NIX_SHOW_FLAT_CMD, *drv_list]
    stdout, _stderr, rc = runner(cmd)
    if rc != 0:
        return {}
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    normalized = _normalize_show_output(parsed)
    out: dict[str, str] = {}
    for drv_path, node in normalized.items():
        if not isinstance(node, dict):
            continue
        outputs = node.get("outputs")
        if not isinstance(outputs, dict):
            continue
        chosen = None
        if "out" in outputs and isinstance(outputs["out"], dict):
            chosen = outputs["out"]
        else:
            for key in sorted(outputs.keys()):
                entry = outputs[key]
                if isinstance(entry, dict):
                    chosen = entry
                    break
        if chosen is None:
            continue
        path = chosen.get("path")
        if isinstance(path, str) and path.startswith(_NIX_STORE_PREFIX):
            out[drv_path] = path
    return out

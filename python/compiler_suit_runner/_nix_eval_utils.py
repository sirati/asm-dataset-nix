"""Shared ``nix eval`` helpers for phase-3 smoke + dot-demo paths.

These two primitives previously lived as private duplicates in
``tests/_phase3_smoke_helpers.py`` and ``scripts/_phase3_dot_helpers.py``.
They are tiny, side-effect-free wrappers around a single subprocess call
each, so consolidating them removes drift risk without touching the
production code paths that resolve bash via
``suit_task._resolve_bash_store_path``.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any, Optional


def nix_eval_json(
    attr: str,
    *,
    root: pathlib.Path,
    apply: Optional[str] = None,
) -> Any:
    """One ``nix eval --json``; tiny scalar/list reads only.

    Raises ``RuntimeError`` on non-zero exit, embedding the trimmed
    stderr so callers can surface a useful failure message.
    """
    argv = [
        "nix", "eval",
        "--extra-experimental-features", "nix-command flakes",
        "--json", f"{root}#{attr}",
    ]
    if apply is not None:
        argv += ["--apply", apply]
    proc = subprocess.run(argv, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"nix eval --json {attr} failed (rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return json.loads(proc.stdout.decode("utf-8", errors="replace"))


def resolve_bash_drv_path(*, root_unused: Optional[pathlib.Path] = None) -> str:
    """``nix eval --raw nixpkgs#bash.outPath`` — the production probe.

    ``suit_task._resolve_bash_store_path`` resolves bash this way for
    the dependency_graph worker subprocess; mirroring it here keeps
    the smoke + dot-demo drivers pointed at the same store object the
    production sum-drv assembly will consume. ``root_unused`` is kept
    only so callers that previously passed a flake root don't need to
    drop the keyword.
    """
    del root_unused  # nixpkgs flake handle is global; root unused
    argv = [
        "nix", "eval", "--raw",
        "--extra-experimental-features", "nix-command flakes",
        "nixpkgs#bash.outPath",
    ]
    proc = subprocess.run(argv, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "nix eval --raw nixpkgs#bash.outPath failed "
            f"(rc={proc.returncode}): "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    payload = proc.stdout.decode("utf-8", errors="replace").strip()
    assert payload.startswith("/nix/store/"), payload
    return payload

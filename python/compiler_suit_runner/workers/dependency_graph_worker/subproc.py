"""Subprocess runner injection point shared by all worker steps."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from collections.abc import Callable


__all__ = [
    "RunSubprocess",
    "default_run_subprocess",
    "resolve_tool",
]


RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]

# Resolved tool paths keyed by bare tool name.  Holds shutil.which
# snapshots taken at import time (and refreshed on later which hits)
# plus /nix/store glob results -- including misses (None), so the
# expensive store-wide glob runs at most once per process per tool.
_TOOL_CACHE: dict[str, str | None] = {}

# Store-path directory names that look like the nix package itself,
# e.g. "23zk8sg...-nix-2.34.7" (or a hashless "nix-2.34.7").  This
# prefers the real package over symlink farms such as
# "...-system-path" that merely re-export nix-store, and over
# lookalikes such as "...-nix-prefetch-git" (no digit after "nix-").
_NIX_PKG_DIR_RE = re.compile(r"(^|-)nix-\d")


def _store_glob(name: str) -> str | None:
    """Locate ``name`` under ``/nix/store/*/bin``, deterministically.

    Matches are sorted; the first one whose store-path name looks like
    the nix package itself (see ``_NIX_PKG_DIR_RE``) wins, otherwise
    the first match.  Returns None when nothing in the store provides
    the tool.
    """
    matches = sorted(glob.glob(f"/nix/store/*/bin/{name}"))
    if not matches:
        return None
    for match in matches:
        store_dir = os.path.basename(os.path.dirname(os.path.dirname(match)))
        if _NIX_PKG_DIR_RE.search(store_dir):
            return match
    return matches[0]


def _snapshot_path_tools(names: tuple[str, ...] = ("nix", "nix-store")) -> None:
    """Seed ``_TOOL_CACHE`` with import-time ``shutil.which`` results.

    A fresh worker imports this module with an intact PATH, so the
    snapshot pins the right binaries even if PATH is torn later in the
    same process.  Misses are NOT cached here: a worker respawned into
    a torn env (empty PATH at import) must still fall through to the
    store glob in :func:`resolve_tool`.
    """
    for name in names:
        found = shutil.which(name)
        if found:
            _TOOL_CACHE[name] = found


def resolve_tool(name: str) -> str:
    """Absolute path for a tool, robust to a torn PATH (respawn-env).

    Workers can be respawned into an environment whose PATH no longer
    contains the nix binaries.  On plain hosts/secondaries the tools
    also live at ``/bin/<name>``, but the worker container is a
    nix-built layered image where ``/bin`` holds almost nothing and
    binaries live under ``/nix/store/<hash>-<pkg>-<ver>/bin/`` -- so a
    bare ``/bin/<name>`` fallback execs into ENOENT there.

    Resolution order:

    1. names containing ``/`` pass through untouched;
    2. ``shutil.which`` (intact PATH);
    3. ``/bin/<name>`` if it exists (cheap, correct on plain hosts);
    4. cached result: the import-time which snapshot (PATH was intact
       when this module loaded) or a previous store-glob outcome;
    5. one-time ``/nix/store/*/bin/<name>`` glob (cached, miss too),
       preferring the nix package's own store path;
    6. the bare name -- exec then fails with a clear error naming the
       tool instead of a bogus absolute path.
    """
    if "/" in name:
        return name
    found = shutil.which(name)
    if found:
        _TOOL_CACHE[name] = found
        return found
    bin_fallback = f"/bin/{name}"
    if os.path.exists(bin_fallback):
        return bin_fallback
    if name in _TOOL_CACHE:
        cached = _TOOL_CACHE[name]
        return cached if cached is not None else name
    globbed = _store_glob(name)
    _TOOL_CACHE[name] = globbed
    return globbed if globbed is not None else name


_snapshot_path_tools()


def default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell.

    ``argv[0]`` is resolved via :func:`resolve_tool` so a bare tool
    name still execs when the respawn environment lost PATH.
    """
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        [resolve_tool(argv[0]), *argv[1:]],
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode

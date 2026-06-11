"""Subprocess runner injection point shared by all worker steps."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable


__all__ = [
    "RunSubprocess",
    "default_run_subprocess",
    "resolve_tool",
]


RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def resolve_tool(name: str) -> str:
    """Absolute path for a tool, robust to a torn PATH (respawn-env).

    Workers can be respawned into an environment whose PATH no longer
    contains the nix binaries; in the worker container they live at
    ``/bin/<name>``, so falling back there is valid even with an empty
    PATH. Paths containing a ``/`` are passed through untouched.
    """
    if "/" in name:
        return name
    return shutil.which(name) or f"/bin/{name}"


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

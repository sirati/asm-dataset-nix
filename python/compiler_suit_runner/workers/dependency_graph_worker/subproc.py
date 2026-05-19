"""Subprocess runner injection point shared by all worker steps."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


__all__ = [
    "RunSubprocess",
    "default_run_subprocess",
]


RunSubprocess = Callable[[list[str]], tuple[bytes, bytes, int]]


def default_run_subprocess(argv: list[str]) -> tuple[bytes, bytes, int]:
    """Real ``subprocess.run`` invocation; never goes through a shell."""
    proc = subprocess.run(  # noqa: S603 - argv constructed in-module
        argv,
        check=False,
        capture_output=True,
        shell=False,
    )
    return proc.stdout, proc.stderr, proc.returncode

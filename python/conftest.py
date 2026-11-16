"""Shared pytest configuration for the runner packages.

Unit tests must be hermetic: a test that runs a real ``nix`` command
depends on the developer's flake state and nix store, is slow, and can
pass while logging errors. The autouse guard below fails any test that
spawns ``nix``, ``nix-store``, ``nix-eval-jobs`` or another ``nix-*``
tool installed in /nix/store, unless the test is marked ``nix`` or
``slurm_live``. Fake tools a test puts on PATH are allowed.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest

_ALLOW_MARKERS = ("nix", "slurm_live")


def _program(args, executable) -> str:
    if executable:
        return os.path.basename(os.fspath(executable))
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        try:
            first = shlex.split(text)[0] if text.strip() else ""
        except ValueError:
            first = text.split()[0] if text.split() else ""
        return os.path.basename(first)
    try:
        first = args[0]
    except (TypeError, IndexError):
        return ""
    return os.path.basename(os.fsdecode(first))


def _is_nix_tool(name: str) -> bool:
    return name == "nix" or name.startswith("nix-")


def _resolves_to_real_tool(args, kw: dict) -> bool:
    """True when the program resolves to an installed tool in /nix/store.

    A test may put a fake ``nix-store`` script on PATH; that is hermetic
    and allowed.
    """
    exe = kw.get("executable")
    if exe is None:
        if isinstance(args, (str, bytes, os.PathLike)):
            try:
                exe = shlex.split(os.fsdecode(args))[0]
            except (ValueError, IndexError):
                return False
        else:
            try:
                exe = os.fsdecode(args[0])
            except (TypeError, IndexError):
                return False
    exe = os.fsdecode(exe)
    if os.sep not in exe:
        env = kw.get("env") or os.environ
        exe = shutil.which(exe, path=env.get("PATH")) or ""
    if not exe:
        return False
    return os.path.realpath(exe).startswith("/nix/store/")


@pytest.fixture(autouse=True)
def _forbid_real_nix(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if any(request.node.get_closest_marker(m) for m in _ALLOW_MARKERS):
        yield
        return
    real_init = subprocess.Popen.__init__
    record = os.environ.get("CSR_TEST_NIX_GUARD") == "record"

    def guarded_init(self, args, *a, **kw):
        prog = _program(args, kw.get("executable"))
        if _is_nix_tool(prog) and _resolves_to_real_tool(args, kw):
            if record:
                with open(os.environ["CSR_TEST_NIX_GUARD_LOG"], "a") as fh:
                    fh.write(f"{request.node.nodeid}\t{prog}\n")
            else:
                raise AssertionError(
                    f"unit test spawned a real {prog!r} process ({args!r}); "
                    "inject a fake runner, or mark the test @pytest.mark.nix"
                )
        real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    yield

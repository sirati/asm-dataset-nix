"""Shared fixtures for the compiler_suit_runner unit tests.

The ``sys.path`` insert makes ``template_graph`` importable when these
tests run without the sibling package installed; the production planner
imports it. (It does not help a bare ``pytest``: the package import that
loads this conftest already needs ``template_graph``. Run with
``PYTHONPATH=<repo>:<repo>/python`` until ``template_graph`` is packaged.)
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[3]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


@pytest.fixture
def fake_nix_keygen(tmp_path_factory, monkeypatch):
    """Put a fake ``nix`` on PATH that implements only the key commands.

    ``nix key generate-secret --key-name N`` prints ``N:<secret>`` and
    ``nix key convert-secret-to-public`` maps it to ``N:<public>``, which
    is enough for the signing-key code paths without the real tool.
    """
    bindir = tmp_path_factory.mktemp("fake-nix-bin")
    script = bindir / "nix"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "key generate-secret" ]; then\n'
        '  printf "%s:c2VjcmV0LSQkCg==" "$4"; exit 0\n'
        "fi\n"
        'if [ "$1 $2" = "key convert-secret-to-public" ]; then\n'
        '  read -r s; printf "%s:cHVibGljCg==" "${s%%:*}"; exit 0\n'
        "fi\n"
        'echo "fake nix: unsupported: $*" >&2; exit 2\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return script

"""The autouse guard in ``python/conftest.py`` keeps unit tests off real nix."""

from __future__ import annotations

import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("nix") is None, reason="no nix on PATH")
def test_guard_blocks_real_nix() -> None:
    with pytest.raises(AssertionError, match="spawned a real 'nix'"):
        subprocess.run(["nix", "--version"], capture_output=True, check=False)


def test_guard_allows_fake_nix_on_path(fake_nix_keygen) -> None:
    out = subprocess.run(
        ["nix", "key", "generate-secret", "--key-name", "k"],
        capture_output=True, check=True,
    )
    assert out.stdout.startswith(b"k:")


def test_guard_allows_other_tools() -> None:
    assert subprocess.run(["true"], check=False).returncode == 0

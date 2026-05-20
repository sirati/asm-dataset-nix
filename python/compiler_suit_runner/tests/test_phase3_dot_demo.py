"""Live integration test for the phase-3 dot demo driver.

Runs ``python -m compiler_suit_runner.scripts.phase3_dot_demo`` as a
subprocess against the local flake, asserts the per-binary merged dot
files land in ``/tmp/phase3-dots/`` with non-empty content, and parses
the printed wall time to verify the ≤30 s warm-cache target the
matrix-aggregate refactor promises.

Marked ``@pytest.mark.nix``; excluded from the default fast suite. Run
explicitly via::

    pytest -m nix python/compiler_suit_runner/tests/test_phase3_dot_demo.py -v
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.nix


WALL_BUDGET_SECONDS = 30.0
DEFAULT_OUTPUT_DIR = Path("/tmp/phase3-dots")
EXPECTED_BINARIES: tuple[str, ...] = ("hello", "busybox")

_WALL_RE = re.compile(r"wall=([0-9]+\.[0-9]+)s")


def _flake_root() -> Path:
    """Repo root — same ``parents[3]`` walk as ``conftest.py``."""
    return Path(__file__).resolve().parents[3]


def _skip_unless_nix_available() -> None:
    """Skip when the live nix toolchain isn't available."""
    for tool in ("nix-instantiate", "nix-store", "nix", "nix-eval-jobs"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not in PATH")
    if not (_flake_root() / "flake.nix").is_file():
        pytest.skip(f"no flake.nix at expected root {_flake_root()}")


def _purge_dot_files(out_dir: Path) -> None:
    """Remove any pre-existing per-binary dots so the test only passes
    when the current driver invocation actually emits them."""
    for binary in EXPECTED_BINARIES:
        path = out_dir / f"{binary}-merged.dot"
        if path.exists():
            path.unlink()


def test_phase3_dot_demo_emits_per_binary_dots():
    """Full end-to-end probe: driver script writes both dots, ≤30 s wall."""
    _skip_unless_nix_available()
    out_dir = DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    _purge_dot_files(out_dir)

    # Inherit env so the user's nix daemon socket / NIX_PATH stay
    # reachable, but force PYTHONPATH so the subprocess imports the
    # in-tree compiler_suit_runner without a separate install step.
    env = dict(os.environ)
    repo_root = _flake_root()
    py_root = repo_root / "python"
    extra = f"{py_root}{os.pathsep}{repo_root}"
    env["PYTHONPATH"] = (
        f"{extra}{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH")
        else extra
    )
    proc = subprocess.run(
        [sys.executable, "-m",
         "compiler_suit_runner.scripts.phase3_dot_demo"],
        capture_output=True, check=False, env=env, cwd=str(repo_root),
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, (
        f"phase3_dot_demo exited rc={proc.returncode}\n"
        f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
    )

    # Wall time parsing — the script prints ``wall=<float>s`` on the
    # summary line. Treat absence as a smoke regression: the budget
    # check is the central acceptance criterion.
    match = _WALL_RE.search(stdout)
    assert match is not None, (
        f"phase3_dot_demo did not print 'wall=<seconds>s' in stdout:\n"
        f"{stdout}"
    )
    wall_seconds = float(match.group(1))
    assert wall_seconds < WALL_BUDGET_SECONDS, (
        f"phase3_dot_demo wall time {wall_seconds:.2f}s exceeded the "
        f"{WALL_BUDGET_SECONDS}s warm-cache budget; the matrix-"
        f"aggregate refactor's 'trivially fast phase 3' guarantee is "
        f"regressing.\nstdout:\n{stdout}"
    )

    # Both per-binary dot files exist and are non-empty.
    for binary in EXPECTED_BINARIES:
        dot_path = out_dir / f"{binary}-merged.dot"
        assert dot_path.is_file(), (
            f"expected {dot_path!s} to exist after phase3_dot_demo; "
            f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
        )
        size = dot_path.stat().st_size
        assert size > 0, (
            f"{dot_path!s} is empty (size={size}) — the dot renderer "
            f"emitted nothing\nstdout:\n{stdout}"
        )
        # Sanity: the renderer always writes a graphviz preamble.
        head = dot_path.read_text(encoding="utf-8").splitlines()[:3]
        assert head and head[0].startswith("digraph "), (
            f"{dot_path!s} first line {head[0]!r} is not a digraph "
            f"declaration; renderer surface drifted"
        )
